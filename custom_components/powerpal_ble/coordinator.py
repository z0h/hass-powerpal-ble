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

from .powerpal_client import PowerpalClient, PowerpalState

_LOGGER = logging.getLogger(__name__)


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
        self._available: bool = False

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
        for listener in list(self._listeners):
            listener(state)

    async def async_start(self) -> bool:
        """Begin watching for the device. Returns True if an initial connection
        was successfully started (used by setup to decide ConfigEntryNotReady)."""

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
            self._available = False
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
        else:
            self._client.update_ble_device(ble_device)

        try:
            await self._client.start()
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to (re)connect to Powerpal %s: %s", self.address, err
            )
            return False

    async def async_stop(self) -> None:
        if self._unregister_bt_cb:
            self._unregister_bt_cb()
            self._unregister_bt_cb = None
        if self._unregister_unavailable:
            self._unregister_unavailable()
            self._unregister_unavailable = None
        if self._client is not None:
            await self._client.stop()
            self._client = None
