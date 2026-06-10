"""BLE client for Powerpal energy monitor.

Protocol ported from WeekendWarrior1/powerpal_ble (ESPHome C++ component).
The Powerpal advertises its presence, then a client must:

  1. Connect (BLE link + bond if device requests it).
  2. Write the 4-byte little-endian pairing code to PAIRING_CODE_UUID.
  3. Read READING_BATCH_SIZE_UUID. If the first byte doesn't match the desired
     `notification_interval`, write it (4-byte LE).
  4. Subscribe to notifications on MEASUREMENT_UUID. Each notification is
     [unix_time:LE u32][pulses:LE u16] (>=6 bytes).
  5. Optionally read + subscribe to BATTERY_LEVEL_UUID (1 byte percent).

Byte-level operations live in `_protocol.py` so they can be unit-tested.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.util import dt as dt_util

from ._protocol import (
    average_power_watts,
    encode_batch_size,
    encode_pairing_code,
    kwh_from_pulses,
    parse_measurement,
)
from .const import (
    BATTERY_LEVEL_UUID,
    MEASUREMENT_UUID,
    PAIRING_CODE_UUID,
    READING_BATCH_SIZE_UUID,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class PowerpalState:
    """Latest measurements from the device. None until first received."""

    power_w: float | None = None
    daily_energy_kwh: float | None = None
    total_energy_kwh: float | None = None
    battery_percent: int | None = None
    last_measurement_at: dt.datetime | None = None
    connected: bool = False

    # internal accumulators
    daily_pulses: int = field(default=0, repr=False)
    total_pulses: int = field(default=0, repr=False)
    day_key: int = field(default=0, repr=False)  # ordinal of local date


StateCallback = Callable[[PowerpalState], None]


class PowerpalClient:
    """Manages the persistent BLE connection to a Powerpal device."""

    def __init__(
        self,
        ble_device: BLEDevice,
        pairing_code: int,
        pulses_per_kwh: float,
        notification_interval: int,
        on_update: StateCallback | None = None,
    ) -> None:
        self._ble_device = ble_device
        self._pairing_code = pairing_code
        self._pulses_per_kwh = float(pulses_per_kwh)
        self._notification_interval = int(notification_interval)
        self._on_update = on_update

        self.state = PowerpalState()
        self._client: BleakClientWithServiceCache | None = None
        self._connect_lock = asyncio.Lock()
        self._closed = False
        self._authenticated = False
        # Bleak notification callbacks: marshal to the loop captured at start().
        self._loop: asyncio.AbstractEventLoop | None = None
        # Watchdog: a wrong pairing code is a silent failure (no GATT error,
        # no notifications). We arm a timer at handshake completion; if no
        # measurement arrives within `notification_interval*2 + 30s`, we log
        # a warning to give the user a hint.
        self._auth_watchdog: asyncio.TimerHandle | None = None

    @property
    def address(self) -> str:
        return self._ble_device.address

    @property
    def is_gatt_connected(self) -> bool:
        """True while a live GATT link exists, regardless of `state.connected`.

        `state.connected` is the *published* availability and can be flipped
        False by HA's advertisement-unavailability tracker even though the
        link is healthy (Powerpal stops advertising while connected). This
        property reports the underlying link so callers can distinguish the
        two.
        """
        return self._client is not None and self._client.is_connected

    def update_ble_device(self, ble_device: BLEDevice) -> None:
        """Swap the BLEDevice (e.g. seen by a different scanner) atomically."""
        self._ble_device = ble_device

    # -------- Public lifecycle --------

    async def start(self) -> None:
        """Establish the connection and complete the auth handshake.

        Idempotent under concurrent calls; safe to call from every advertisement
        callback (it short-circuits if already authenticated).
        """
        async with self._connect_lock:
            if self._closed:
                return
            if (
                self._client is not None
                and self._client.is_connected
                and self._authenticated
            ):
                # The link is healthy. If the HA unavailability tracker had
                # flipped published state to disconnected (advertisements
                # stop while connected), restore it now rather than waiting
                # for the next measurement — at notification_interval=60
                # that wait is up to an hour of false "unavailable".
                if not self.state.connected:
                    self.state.connected = True
                    self._notify_listeners()
                return
            # Tear down any wedged client (connected but not authenticated, or
            # disconnected). Quiet teardown — no spurious "disconnected"
            # notification if state was already disconnected.
            await self._teardown_client_locked(notify=False)
            self._loop = asyncio.get_running_loop()
            try:
                await self._connect_and_setup()
            except Exception:
                # Setup failed mid-handshake: tear down so the next advert
                # actually retries instead of seeing a half-alive client.
                await self._teardown_client_locked(notify=True)
                raise

    async def stop(self) -> None:
        async with self._connect_lock:
            self._closed = True
            await self._teardown_client_locked(notify=True)

    async def _teardown_client_locked(self, *, notify: bool) -> None:
        """Drop the client cleanly. Caller must hold the lock.

        If `notify` is True and we had been "connected" before, fire a state
        update so listeners see the disconnect. If we never were connected,
        skip the notification to avoid spurious flicker.
        """
        was_connected = self.state.connected
        self._authenticated = False
        self._cancel_auth_watchdog()
        if self._client is not None:
            try:
                if self._client.is_connected:
                    await self._client.disconnect()
            except BleakError as err:
                _LOGGER.debug("Disconnect raised (ignored): %s", err)
            self._client = None
        if was_connected:
            self.state.connected = False
            if notify:
                self._notify_listeners()

    # -------- Connection / handshake --------

    async def _connect_and_setup(self) -> None:
        _LOGGER.debug("Connecting to Powerpal at %s", self._ble_device.address)
        self._client = await establish_connection(
            BleakClientWithServiceCache,
            self._ble_device,
            self._ble_device.address,
            disconnected_callback=self._on_disconnect,
        )
        if self._closed:
            return  # stop() raced; outer start() will teardown.

        # 0. Powerpal requires BLE-level bonding before it accepts the
        # application-level pairing-code GATT write — the GATT write
        # returns "Insufficient Authentication" (BLE error 5) otherwise.
        # The C++ component (the reference) waits for ESP_GAP_BLE_AUTH_CMPL_EVT;
        # the equivalent in bleak is explicit pair() before any protected
        # operation. Some backends/devices auto-pair and pair() is a no-op
        # or unsupported — that's fine, log and continue; the subsequent
        # write will surface the real error if bonding actually failed.
        try:
            await self._client.pair()
            _LOGGER.debug("BLE pair OK on %s", self._ble_device.address)
        except (BleakError, NotImplementedError) as err:
            _LOGGER.debug("pair() unavailable or skipped: %s", err)

        # 1. Authenticate by writing the pairing code (4-byte LE).
        await self._client.write_gatt_char(
            PAIRING_CODE_UUID, encode_pairing_code(self._pairing_code), response=True
        )
        _LOGGER.debug("Wrote pairing code to %s", self._ble_device.address)

        # 2. Read current batch size; write if different.
        current_batch = await self._client.read_gatt_char(READING_BATCH_SIZE_UUID)
        desired_batch = encode_batch_size(self._notification_interval)
        if len(current_batch) != 4 or current_batch[0] != desired_batch[0]:
            await self._client.write_gatt_char(
                READING_BATCH_SIZE_UUID, desired_batch, response=True
            )
            _LOGGER.debug(
                "Updated batch size from %s to %s",
                current_batch.hex() if current_batch else "?",
                desired_batch.hex(),
            )

        # 3. Subscribe to measurement notifications.
        await self._client.start_notify(MEASUREMENT_UUID, self._on_measurement_notify)
        _LOGGER.info("Subscribed to measurements on %s", self._ble_device.address)

        # Mark connected *before* the optional battery setup so that any
        # battery notification arriving during init publishes against the
        # correct state.connected=True (instead of briefly flapping the
        # sensor unavailable→available).
        self._authenticated = True
        self.state.connected = True
        self._arm_auth_watchdog()

        # 4. Battery is optional but cheap.
        try:
            await self._client.start_notify(BATTERY_LEVEL_UUID, self._on_battery_notify)
            initial_battery = await self._client.read_gatt_char(BATTERY_LEVEL_UUID)
            if initial_battery:
                self._handle_battery(bytes(initial_battery))
        except BleakError as err:
            _LOGGER.debug("Battery characteristic unavailable: %s", err)

        self._notify_listeners()

    def _on_disconnect(self, client) -> None:
        """Bleak fires this; backend threading is not guaranteed across
        platforms, so marshal to the captured event loop before mutating
        state or notifying listeners."""
        _LOGGER.debug("Powerpal disconnected: %s", self._ble_device.address)
        self._dispatch_on_loop(self._handle_disconnect, client)

    def _handle_disconnect(self, client) -> None:
        # Backend disconnect detection can lag the physical drop. If a
        # reconnect already produced a NEW client by the time the OLD
        # client's callback lands here, acting on it would mark the healthy
        # connection unauthenticated and trigger a teardown/reconnect loop.
        # Only honour callbacks from the client we currently own.
        if client is not self._client:
            _LOGGER.debug(
                "Ignoring disconnect from a superseded client on %s",
                self._ble_device.address,
            )
            return
        self._authenticated = False
        self._cancel_auth_watchdog()
        if self.state.connected:
            self.state.connected = False
            self._notify_listeners()

    def _arm_auth_watchdog(self) -> None:
        """Arms a timer for `notification_interval*2 + 30s`. If we haven't
        received a measurement by then, the pairing code is almost certainly
        wrong (the device accepts the GATT write either way; it just won't
        push notifications)."""
        self._cancel_auth_watchdog()
        loop = self._loop
        if loop is None:
            return
        delay = self._notification_interval * 2 * 60 + 30
        self._auth_watchdog = loop.call_later(delay, self._on_auth_watchdog_timeout)

    def _cancel_auth_watchdog(self) -> None:
        if self._auth_watchdog is not None:
            self._auth_watchdog.cancel()
            self._auth_watchdog = None

    def _on_auth_watchdog_timeout(self) -> None:
        self._auth_watchdog = None
        if self.state.last_measurement_at is None:
            _LOGGER.warning(
                "Powerpal %s: connected and authenticated but received no "
                "measurements after %d minutes — the pairing code may be "
                "wrong (Powerpal does not return a GATT error for an "
                "incorrect code; it just stops sending notifications). "
                "Double-check the code in your Powerpal app.",
                self._ble_device.address,
                self._notification_interval * 2,
            )

    # -------- Notification handlers (may be off-loop on some backends) --------

    def _on_measurement_notify(self, _handle, data: bytearray) -> None:
        self._dispatch_on_loop(self._handle_measurement, bytes(data))

    def _on_battery_notify(self, _handle, data: bytearray) -> None:
        self._dispatch_on_loop(self._handle_battery, bytes(data))

    def _dispatch_on_loop(self, fn, *args) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        # Loop may close between the is_closed() check and the call (HA shutdown).
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(fn, *args)

    # -------- Packet handling (always on the loop thread) --------

    def _handle_measurement(self, data: bytes) -> None:
        if len(data) < 6:
            _LOGGER.debug("Short measurement packet: %s", data.hex())
            return
        try:
            # Receiving a measurement IS proof of an active GATT link, so
            # if the HA-bluetooth unavailable watchdog previously flipped us
            # to disconnected (advertisements stopped but GATT held up),
            # flip it back.
            if not self.state.connected:
                self.state.connected = True
            unix_time, pulses = parse_measurement(data)
            ts_local = dt_util.as_local(dt_util.utc_from_timestamp(unix_time))

            power_w = average_power_watts(
                pulses, self._pulses_per_kwh, self._notification_interval
            )

            self.state.total_pulses += pulses
            total_kwh = kwh_from_pulses(self.state.total_pulses, self._pulses_per_kwh)

            day_key = ts_local.toordinal()
            if self.state.day_key == 0:
                self.state.day_key = day_key
            elif self.state.day_key != day_key:
                self.state.daily_pulses = 0
                self.state.day_key = day_key
            self.state.daily_pulses += pulses
            daily_kwh = kwh_from_pulses(self.state.daily_pulses, self._pulses_per_kwh)

            self.state.power_w = power_w
            self.state.daily_energy_kwh = daily_kwh
            self.state.total_energy_kwh = total_kwh
            self.state.last_measurement_at = ts_local

            _LOGGER.debug(
                "ts=%s pulses=%d power=%.1fW daily=%.3fkWh total=%.3fkWh",
                ts_local.isoformat(), pulses, power_w, daily_kwh, total_kwh,
            )
            self._notify_listeners()
        except (OverflowError, OSError, ValueError) as err:
            _LOGGER.warning("Bad measurement packet %s: %s", data.hex(), err)

    def _handle_battery(self, data: bytes) -> None:
        if len(data) >= 1:
            self.state.battery_percent = data[0]
            _LOGGER.debug("Battery: %d%%", data[0])
            self._notify_listeners()

    def _notify_listeners(self) -> None:
        cb = self._on_update
        if cb is None:
            return
        # The only registered listener is the coordinator's `_async_handle_state`,
        # which is `@callback`-decorated and synchronous. We do NOT accept
        # coroutine callbacks here — wrapping with create_task() would risk
        # losing the task reference and the result silently disappearing.
        cb(self.state)
