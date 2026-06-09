"""Config flow for Powerpal BLE."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import (
    CONF_NOTIFICATION_INTERVAL,
    CONF_PAIRING_CODE,
    CONF_PULSES_PER_KWH,
    DEFAULT_NOTIFICATION_INTERVAL,
    DEFAULT_PULSES_PER_KWH,
    DOMAIN,
)


class PowerpalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two-step flow: discover (or pick) device, then enter pairing code & meter rate."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}
        self._address: str | None = None
        self._name: str | None = None

    # --- entered when HA finds an advertised Powerpal ---------------------
    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self._name = discovery_info.name or discovery_info.address
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_settings()

    # --- entered when the user adds the integration manually --------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
            self._address = address
            info = self._discovered.get(address)
            self._name = info.name if info else address
            return await self.async_step_settings()

        # Build picker from any Powerpals currently in the bluetooth cache.
        current_ids = self._async_current_ids()
        self._discovered = {
            info.address: info
            for info in bluetooth.async_discovered_service_info(self.hass, False)
            if info.address not in current_ids
            and (info.name or "").lower().startswith("powerpal")
        }
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            addr: f"{info.name} ({addr})"
                            for addr, info in self._discovered.items()
                        }
                    )
                }
            ),
        )

    # --- pairing code / pulses / interval ---------------------------------
    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            return self.async_create_entry(
                title=self._name or self._address,
                data={
                    CONF_ADDRESS: self._address,
                    CONF_PAIRING_CODE: user_input[CONF_PAIRING_CODE],
                    CONF_PULSES_PER_KWH: user_input[CONF_PULSES_PER_KWH],
                    CONF_NOTIFICATION_INTERVAL: user_input[CONF_NOTIFICATION_INTERVAL],
                },
            )

        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PAIRING_CODE): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=99_999_999)
                    ),
                    vol.Required(
                        CONF_PULSES_PER_KWH, default=DEFAULT_PULSES_PER_KWH
                    ): vol.All(vol.Coerce(float), vol.Range(min=1)),
                    vol.Required(
                        CONF_NOTIFICATION_INTERVAL,
                        default=DEFAULT_NOTIFICATION_INTERVAL,
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
                }
            ),
            description_placeholders={"name": self._name or self._address or ""},
            errors=errors,
        )
