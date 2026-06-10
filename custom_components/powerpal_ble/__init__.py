"""Powerpal BLE integration."""

from __future__ import annotations

import logging

from homeassistant.components.bluetooth import async_address_present
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

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


def _settings_from_entry(entry: PowerpalConfigEntry) -> dict:
    """Effective settings: data + any options overrides."""
    return {
        CONF_PULSES_PER_KWH: entry.options.get(
            CONF_PULSES_PER_KWH, entry.data[CONF_PULSES_PER_KWH]
        ),
        CONF_NOTIFICATION_INTERVAL: entry.options.get(
            CONF_NOTIFICATION_INTERVAL, entry.data[CONF_NOTIFICATION_INTERVAL]
        ),
    }


async def async_setup_entry(hass: HomeAssistant, entry: PowerpalConfigEntry) -> bool:
    settings = _settings_from_entry(entry)
    coordinator = PowerpalCoordinator(
        hass,
        address=entry.data[CONF_ADDRESS],
        pairing_code=entry.data[CONF_PAIRING_CODE],
        pulses_per_kwh=settings[CONF_PULSES_PER_KWH],
        notification_interval=settings[CONF_NOTIFICATION_INTERVAL],
    )
    # Start the watcher; if the device is already cached AND the initial
    # connect fails, ConfigEntryNotReady asks HA to retry later. If it's
    # not cached we set up "lazily" and wait for the first advertisement —
    # this is the normal path for a battery device that advertises sparsely.
    if not await coordinator.async_start():
        await coordinator.async_stop()
        raise ConfigEntryNotReady(
            f"Initial connection to Powerpal {coordinator.address} failed"
        )
    if not await _initial_advert_seen(hass, entry):
        _LOGGER.info(
            "Powerpal %s not currently in range; sensors will populate "
            "after the first advertisement is received via any HA bluetooth scanner.",
            coordinator.address,
        )

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _initial_advert_seen(hass: HomeAssistant, entry: PowerpalConfigEntry) -> bool:
    return async_address_present(hass, entry.data[CONF_ADDRESS])


async def _async_update_listener(
    hass: HomeAssistant, entry: PowerpalConfigEntry
) -> None:
    """Reload the entry so settings changes (pulses, interval) take effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PowerpalConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok
