"""Owns the PowerpalClient lifecycle and re-broadcasts state to entities."""

from __future__ import annotations

import logging
from collections.abc import Callable

from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_address_present,
    async_ble_device_from_address,
    async_register_callback,
    async_track_unavailable,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from ._protocol import restore_pulses_from_snapshot
from .const import DOMAIN
from .powerpal_client import PowerpalClient, PowerpalState

_LOGGER = logging.getLogger(__name__)

# Bumped if the persisted-state schema changes meaningfully.
_STORAGE_VERSION = 1
# Save at most once a minute. Notifications arrive at most once per
# `notification_interval` (default 1 min); debouncing this short still
# coalesces bursts on reconnect but limits the data lost to <1 min on a
# hard crash that bypasses HA's clean-shutdown flush.
_SAVE_DEBOUNCE_S = 60


class PowerpalCoordinator:
    """Glue between HA's bluetooth integration and our PowerpalClient.

    HA's bluetooth integration fires a callback whenever it sees an
    advertisement for our configured address (from any registered scanner,
    including ESPHome bluetooth proxies). On the first such callback we kick
    off connection + handshake. Entities subscribe to coordinator updates to
    receive state changes pushed by Powerpal notifications.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        pairing_code: int,
        pulses_per_kwh: float,
        notification_interval: int,
    ) -> None:
        self.hass = hass
        # Don't transform the address — HA stores it canonically (uppercase MAC
        # on Linux, CoreBluetooth UUID on macOS) and the matcher compares to
        # whatever it gave us at discovery time.
        self.address = address
        self._pairing_code = pairing_code
        self._pulses_per_kwh = pulses_per_kwh
        self._notification_interval = notification_interval

        self._client: PowerpalClient | None = None
        self._unregister_bt_cb: Callable[[], None] | None = None
        self._unregister_unavailable: Callable[[], None] | None = None
        self._listeners: list[Callable[[PowerpalState], None]] = []
        # Persisted accumulators so total/daily energy survive HA restarts —
        # without this, sensors with state_class=TOTAL_INCREASING reset to 0
        # on every restart and break the Energy Dashboard.
        self._store: Store = Store(
            hass,
            _STORAGE_VERSION,
            f"{DOMAIN}.energy.{address}",
        )
        self._restored_state: dict | None = None

    @property
    def state(self) -> PowerpalState:
        return self._client.state if self._client else PowerpalState()

    @callback
    def async_add_listener(
        self, update: Callable[[PowerpalState], None]
    ) -> Callable[[], None]:
        """Register an entity update callback. Returns an unregister fn."""
        self._listeners.append(update)

        def _unsub() -> None:
            if update in self._listeners:
                self._listeners.remove(update)

        return _unsub

    @callback
    def _async_handle_state(self, state: PowerpalState) -> None:
        # Listener exceptions must not break iteration or skip persistence —
        # log and continue.
        for listener in list(self._listeners):
            try:
                listener(state)
            except Exception:
                _LOGGER.exception(
                    "Powerpal listener raised; continuing with remaining listeners"
                )
        # Persist accumulators (debounced — Store.async_delay_save coalesces).
        # Skip writes while no measurements have arrived yet.
        # Snapshot the dict NOW rather than letting Store call _build_snapshot
        # at fire time — a reconnect cycle (teardown sets _client=None, then
        # _connect_and_setup reassigns it) could otherwise land the 60-s-later
        # fire on `_client is None` and persist a zero snapshot, clobbering
        # the real totals.
        if state.total_pulses or state.daily_pulses:
            snapshot = self._build_snapshot()
            self._store.async_delay_save(lambda: snapshot, _SAVE_DEBOUNCE_S)

    @callback
    def _build_snapshot(self) -> dict:
        """Pulses are the canonical lossless integer representation. kWh
        values are stored alongside for human readability only. The
        `calibration_ppkwh` records the pulses-per-kWh value at save time so
        a later change via the options flow can rescale the pulse count and
        preserve kWh continuity (otherwise state_class=TOTAL_INCREASING would
        see a step change)."""
        s = self._client.state if self._client else PowerpalState()
        return {
            "total_pulses": s.total_pulses,
            "daily_pulses": s.daily_pulses,
            "day_key": s.day_key,
            "calibration_ppkwh": self._pulses_per_kwh,
            "total_energy_kwh": s.total_energy_kwh or 0.0,  # informational
            "daily_energy_kwh": s.daily_energy_kwh or 0.0,  # informational
        }

    async def async_start(self) -> bool:
        """Begin watching for the device. Returns True if an initial connection
        was successfully started (used by setup to decide ConfigEntryNotReady)."""
        # Load persisted accumulators *before* any client is constructed so the
        # first measurement after restart adds onto the saved totals.
        self._restored_state = await self._store.async_load()

        @callback
        def _advertisement_cb(
            info: BluetoothServiceInfoBleak, change: BluetoothChange
        ) -> None:
            self.hass.async_create_task(self._async_handle_advertisement(info))

        self._unregister_bt_cb = async_register_callback(
            self.hass,
            _advertisement_cb,
            BluetoothCallbackMatcher(address=self.address),
            BluetoothScanningMode.ACTIVE,
        )

        # Track unavailability — when HA hasn't heard any advertisement from
        # this address for the unavailability timeout, sensors go unavailable.
        @callback
        def _unavailable_cb(_info) -> None:
            _LOGGER.debug("Powerpal %s marked unavailable", self.address)
            # Push a synthetic state with connected=False to flip sensors.
            if self._client is not None:
                self._client.state.connected = False
            self._async_handle_state(self.state)

        self._unregister_unavailable = async_track_unavailable(
            self.hass, _unavailable_cb, self.address
        )

        # If the device is already in the cache, kick a connection immediately.
        if async_address_present(self.hass, self.address):
            ble_device = async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if ble_device:
                return await self._async_ensure_client(ble_device)
        # No device cached yet — that's fine; first advert will trigger setup.
        return True

    async def _async_handle_advertisement(
        self, info: BluetoothServiceInfoBleak
    ) -> None:
        await self._async_ensure_client(info.device)

    async def _async_ensure_client(self, ble_device) -> bool:
        if self._client is None:
            self._client = PowerpalClient(
                ble_device,
                pairing_code=self._pairing_code,
                pulses_per_kwh=self._pulses_per_kwh,
                notification_interval=self._notification_interval,
                on_update=self._async_handle_state,
            )
            # Seed accumulators from persisted state (consumed once).
            #
            # Pulses are canonical. If the pulses_per_kwh was different at
            # save time (user changed the calibration via the options flow),
            # rescale to preserve total_kwh = total_pulses / pulses_per_kwh
            # across the change. Otherwise sensors with TOTAL_INCREASING
            # would emit a step delta on reload.
            #
            # Backwards-compat: pre-v0.7 snapshots stored only kWh. If we see
            # a snapshot without `total_pulses` but with `total_energy_kwh`,
            # back-compute pulses from kWh at the current rate (the
            # calibration at that time wasn't recorded, so this assumes the
            # rate hasn't changed — acceptable approximation for migration).
            if self._restored_state is not None:
                # A raise here must NOT escape: self._client is already
                # assigned, so the next advertisement would skip seeding and
                # connect with zeroed accumulators — and the first save would
                # then overwrite the on-disk snapshot with near-zero totals.
                # Degrading loudly to fresh accumulators is the lesser harm.
                try:
                    self._seed_state_from_restored()
                except Exception:
                    _LOGGER.exception(
                        "Powerpal %s: persisted energy snapshot could not be "
                        "restored; starting accumulators from zero",
                        self.address,
                    )
                finally:
                    self._restored_state = None
        else:
            self._client.update_ble_device(ble_device)

        try:
            await self._client.start()
            return True
        except Exception as err:
            _LOGGER.warning(
                "Failed to (re)connect to Powerpal %s: %s", self.address, err
            )
            return False

    def _seed_state_from_restored(self) -> None:
        """Apply persisted accumulators to the freshly-constructed client.

        Decoding (migration + rescale) is in
        `_protocol.restore_pulses_from_snapshot`.
        """
        assert self._client is not None
        total_pulses, daily_pulses, day_key = restore_pulses_from_snapshot(
            self._restored_state, self._pulses_per_kwh
        )
        s = self._client.state
        s.total_pulses = total_pulses
        s.daily_pulses = daily_pulses
        s.day_key = day_key
        if total_pulses:
            s.total_energy_kwh = total_pulses / self._pulses_per_kwh
        if daily_pulses:
            s.daily_energy_kwh = daily_pulses / self._pulses_per_kwh
        _LOGGER.debug(
            "Restored: total_pulses=%d daily_pulses=%d day_key=%d total=%.3fkWh",
            s.total_pulses, s.daily_pulses, s.day_key,
            s.total_energy_kwh or 0.0,
        )

    async def async_stop(self) -> None:
        if self._unregister_bt_cb:
            self._unregister_bt_cb()
            self._unregister_bt_cb = None
        if self._unregister_unavailable:
            self._unregister_unavailable()
            self._unregister_unavailable = None
        # Flush any pending debounced save so the latest accumulators land
        # on disk before we drop the client.
        if self._client is not None and (
            self._client.state.total_pulses
            or self._client.state.daily_pulses
        ):
            await self._store.async_save(self._build_snapshot())
        if self._client is not None:
            await self._client.stop()
            self._client = None
