"""Diagnostics support: dumps non-sensitive state for the user to share when
filing an issue. Pairing code and BLE address are redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from . import PowerpalConfigEntry
from .const import CONF_PAIRING_CODE

REDACT = {CONF_PAIRING_CODE, CONF_ADDRESS}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: PowerpalConfigEntry,
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    state = coordinator.state
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), REDACT),
            "options": dict(entry.options),
            "version": entry.version,
        },
        "state": {
            "connected": state.connected,
            "power_w": state.power_w,
            "daily_energy_kwh": state.daily_energy_kwh,
            "total_energy_kwh": state.total_energy_kwh,
            "battery_percent": state.battery_percent,
            "last_measurement_at": (
                state.last_measurement_at.isoformat()
                if state.last_measurement_at
                else None
            ),
            "total_pulses": state.total_pulses,
            "daily_pulses": state.daily_pulses,
            "day_key": state.day_key,
        },
    }
