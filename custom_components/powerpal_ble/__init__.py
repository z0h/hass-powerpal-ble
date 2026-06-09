"""Powerpal BLE integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_NOTIFICATION_INTERVAL,
    CONF_PAIRING_CODE,
    CONF_PULSES_PER_KWH,
)
from .coordinator import PowerpalCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

# HA 2024.4+ supports typed runtime data on the config entry.
type PowerpalConfigEntry = ConfigEntry[PowerpalCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: PowerpalConfigEntry) -> bool:
    coordinator = PowerpalCoordinator(
        hass,
        address=entry.data[CONF_ADDRESS],
        pairing_code=entry.data[CONF_PAIRING_CODE],
        pulses_per_kwh=entry.data[CONF_PULSES_PER_KWH],
        notification_interval=entry.data[CONF_NOTIFICATION_INTERVAL],
    )
    # Start the watcher; if the device is already cached AND the initial
    # connect fails, ConfigEntryNotReady asks HA to retry later.
    if not await coordinator.async_start():
        await coordinator.async_stop()
        from homeassistant.exceptions import ConfigEntryNotReady
        raise ConfigEntryNotReady(
            f"Initial connection to Powerpal {coordinator.address} failed"
        )

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PowerpalConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok
