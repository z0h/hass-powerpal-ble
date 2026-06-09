"""Powerpal BLE sensor entities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PowerpalConfigEntry
from .const import DOMAIN
from .powerpal_client import PowerpalState


@dataclass(frozen=True, kw_only=True)
class PowerpalSensorDescription(SensorEntityDescription):
    """Adds a value-extractor callable to the standard description."""
    value_fn: Callable[[PowerpalState], float | int | None]


# `translation_key` populates `name` from strings.json — don't also set `name`.
SENSORS: tuple[PowerpalSensorDescription, ...] = (
    PowerpalSensorDescription(
        key="power",
        translation_key="power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda s: s.power_w,
    ),
    PowerpalSensorDescription(
        key="daily_energy",
        translation_key="daily_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        value_fn=lambda s: s.daily_energy_kwh,
    ),
    PowerpalSensorDescription(
        key="total_energy",
        translation_key="total_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        value_fn=lambda s: s.total_energy_kwh,
    ),
    PowerpalSensorDescription(
        key="battery",
        translation_key="battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.battery_percent,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerpalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(PowerpalSensor(coordinator, entry, d) for d in SENSORS)


class PowerpalSensor(SensorEntity):
    """A single sensor derived from the live PowerpalState."""

    _attr_has_entity_name = True
    entity_description: PowerpalSensorDescription

    def __init__(
        self,
        coordinator,
        entry: PowerpalConfigEntry,
        description: PowerpalSensorDescription,
    ) -> None:
        self.entity_description = description
        self._coordinator = coordinator
        address = coordinator.address
        # Device-stable unique ID survives re-adds of the config entry.
        self._attr_unique_id = f"{address}_{description.key}"
        formatted = dr.format_mac(address) if ":" in address else address
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(dr.CONNECTION_BLUETOOTH, formatted)},
            name=entry.title,
            manufacturer="Powerpal",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_state)
        )
        # Seed with whatever the coordinator already has.
        self._handle_state(self._coordinator.state)

    @callback
    def _handle_state(self, state: PowerpalState) -> None:
        value = self.entity_description.value_fn(state)
        self._attr_native_value = value
        # Available iff we have a connected client AND a value for this sensor.
        self._attr_available = state.connected and value is not None
        self.async_write_ha_state()
