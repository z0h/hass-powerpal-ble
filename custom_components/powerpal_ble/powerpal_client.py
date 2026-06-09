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

Average watts within an interval =
    pulses * 60 * batch_size / (pulses_per_kwh / 1000)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.util import dt as dt_util

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
    day_of_last_measurement: int = field(default=0, repr=False)


# A listener gets called for every state change. Sync or async permitted.
StateCallback = Callable[[PowerpalState], None] | Callable[[PowerpalState], Awaitable[None]]


class PowerpalClient:
    """Manages the persistent BLE connection to a Powerpal device.

    Push-driven: when the device emits a measurement notification we recompute
    state and invoke `on_update`. Reconnection is driven by the coordinator
    re-calling `start()` on the next advertisement.
    """

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
        # Bleak may dispatch notifications from a non-loop thread; we marshal
        # back via call_soon_threadsafe on the loop captured at start time.
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def address(self) -> str:
        return self._ble_device.address

    def update_ble_device(self, ble_device: BLEDevice) -> None:
        """HA may rediscover the device via a different scanner; swap the
        underlying BLEDevice without reconnecting. Atomic rebind in CPython."""
        self._ble_device = ble_device

    # -------- Public lifecycle --------

    async def start(self) -> None:
        """Establish the connection and complete the auth handshake. Idempotent."""
        async with self._connect_lock:
            if self._closed:
                return
            if self._client and self._client.is_connected:
                return
            # Tear down any half-alive client from a prior session.
            await self._teardown_client_locked()
            self._loop = asyncio.get_running_loop()
            await self._connect_and_setup()

    async def stop(self) -> None:
        """Disconnect and stop reconnecting."""
        async with self._connect_lock:
            self._closed = True
            await self._teardown_client_locked()

    async def _teardown_client_locked(self) -> None:
        """Disconnect and drop the current client. Caller must hold the lock."""
        if self._client is None:
            return
        try:
            if self._client.is_connected:
                await self._client.disconnect()
        except BleakError as err:
            _LOGGER.debug("Disconnect raised (ignored): %s", err)
        self._client = None
        self.state.connected = False
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
            # stop() raced us; tear down and bail.
            await self._teardown_client_locked()
            return

        # 1. Authenticate by writing the pairing code (4-byte little-endian).
        pairing_bytes = struct.pack("<I", self._pairing_code)
        await self._client.write_gatt_char(PAIRING_CODE_UUID, pairing_bytes, response=True)
        _LOGGER.debug("Wrote pairing code to %s", self._ble_device.address)

        # 2. Read current batch size; if it doesn't match, write the desired value.
        current_batch = await self._client.read_gatt_char(READING_BATCH_SIZE_UUID)
        desired_batch = bytes([self._notification_interval & 0xFF, 0x00, 0x00, 0x00])
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

        # 4. Battery is optional but cheap; subscribe + initial read.
        try:
            await self._client.start_notify(
                BATTERY_LEVEL_UUID, self._on_battery_notify
            )
            initial_battery = await self._client.read_gatt_char(BATTERY_LEVEL_UUID)
            if initial_battery:
                self._handle_battery(bytes(initial_battery))
        except BleakError as err:
            _LOGGER.debug("Battery characteristic unavailable: %s", err)

        self.state.connected = True
        self._notify_listeners()

    def _on_disconnect(self, _client) -> None:
        """Bleak disconnect callback. Runs on bleak's thread; marshal to loop."""
        _LOGGER.debug("Powerpal disconnected: %s", self._ble_device.address)
        self.state.connected = False
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._notify_listeners)

    # -------- Notification handlers (called from bleak's thread) --------

    def _on_measurement_notify(self, _handle, data: bytearray) -> None:
        self._dispatch_on_loop(self._handle_measurement, bytes(data))

    def _on_battery_notify(self, _handle, data: bytearray) -> None:
        self._dispatch_on_loop(self._handle_battery, bytes(data))

    def _dispatch_on_loop(self, fn, *args) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(fn, *args)

    # -------- Packet parsing (always invoked on the loop thread) --------

    def _handle_measurement(self, data: bytes) -> None:
        if len(data) < 6:
            _LOGGER.debug("Short measurement packet: %s", data.hex())
            return
        unix_time, pulses = struct.unpack("<IH", data[:6])

        # Use HA's local timezone for the rollover boundary so "daily" lines up
        # with the user's wall clock, not UTC.
        ts_local = dt_util.as_local(dt_util.utc_from_timestamp(unix_time))

        pulse_multiplier = (
            60.0 * self._notification_interval
        ) / (self._pulses_per_kwh / 1000.0)
        power_w = pulses * pulse_multiplier

        self.state.total_pulses += pulses
        total_kwh = self.state.total_pulses / self._pulses_per_kwh

        day_of_year = ts_local.toordinal()
        if self.state.day_of_last_measurement == 0:
            self.state.day_of_last_measurement = day_of_year
        elif self.state.day_of_last_measurement != day_of_year:
            self.state.daily_pulses = 0
            self.state.day_of_last_measurement = day_of_year
        self.state.daily_pulses += pulses
        daily_kwh = self.state.daily_pulses / self._pulses_per_kwh

        self.state.power_w = power_w
        self.state.daily_energy_kwh = daily_kwh
        self.state.total_energy_kwh = total_kwh
        self.state.last_measurement_at = ts_local

        _LOGGER.debug(
            "Measurement ts=%s pulses=%d power=%.1fW daily=%.3fkWh total=%.3fkWh",
            ts_local.isoformat(), pulses, power_w, daily_kwh, total_kwh,
        )
        self._notify_listeners()

    def _handle_battery(self, data: bytes) -> None:
        if len(data) >= 1:
            self.state.battery_percent = data[0]
            _LOGGER.debug("Battery: %d%%", data[0])
            self._notify_listeners()

    def _notify_listeners(self) -> None:
        cb = self._on_update
        if cb is None:
            return
        result = cb(self.state)
        if asyncio.iscoroutine(result):
            asyncio.create_task(result)
