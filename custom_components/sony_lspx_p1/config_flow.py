"""Config flow for the Sony LSPX-P1: discover over Bluetooth, then pair."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from bleak.exc import BleakError
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .client import LSPXClient, LSPXError, describe_error
from .const import CONF_WEATHER_ENTITY, CONTROLLER_NAME, DOMAIN
from .protocol import (
    LOCAL_NAME_PREFIX,
    SERVICE_UUID_ADVERTISED,
    SONY_MANUFACTURER_DATA_PREFIX,
    SONY_MANUFACTURER_ID,
    CommandRejected,
    ProtocolError,
)

_LOGGER = logging.getLogger(__name__)

# Hard cap for the whole pairing step, so the dialog never spins forever.
PAIRING_FLOW_TIMEOUT = 300


def is_lspx_p1(info: BluetoothServiceInfoBleak) -> bool:
    if (info.name or "").startswith(LOCAL_NAME_PREFIX):
        return True
    # Without a name (e.g. passive scanning): Sony manufacturer data and the projector's service UUID.
    data = info.manufacturer_data.get(SONY_MANUFACTURER_ID)
    return (
        data is not None
        and data.startswith(SONY_MANUFACTURER_DATA_PREFIX)
        and SERVICE_UUID_ADVERTISED in [u.lower() for u in info.service_uuids]
    )


def title_for(info_name: str | None, address: str) -> str:
    """'LSPXP1_1A2B' -> 'LSPX-P1 1A2B'."""
    if info_name and info_name.startswith(LOCAL_NAME_PREFIX):
        suffix = info_name[len(LOCAL_NAME_PREFIX) :].lstrip("_- ")
        return f"LSPX-P1 {suffix}".strip()
    return f"LSPX-P1 {address[-5:]}"


class LSPXConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> LSPXOptionsFlow:
        return LSPXOptionsFlow()

    def __init__(self) -> None:
        self._address: str | None = None
        self._name: str | None = None
        self._discovered: dict[str, str] = {}
        self._error_detail = ""

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        if not is_lspx_p1(discovery_info):
            return self.async_abort(reason="not_supported")
        self._address = discovery_info.address
        self._name = title_for(discovery_info.name, discovery_info.address)
        self.context["title_placeholders"] = {"name": self._name}
        self._set_confirm_only()
        return await self.async_step_pair()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self._address = address
            self._name = self._discovered[address]
            return await self.async_step_pair()

        current = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in current or not is_lspx_p1(info):
                continue
            self._discovered[info.address] = title_for(info.name, info.address)
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            # Home Assistant 2026.10+ types this as probatio.Schema (API-compatible with voluptuous, which it
            # still accepts); voluptuous keeps older versions working.
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(self._discovered)}),  # type: ignore[arg-type]
        )

    async def _async_pair(self, *, repairing: bool = False) -> str | None:
        """Pair with the projector. Returns an error key, or None on success."""
        assert self._address is not None
        ble_device = async_ble_device_from_address(self.hass, self._address, connectable=True)
        if ble_device is None:
            return "device_not_found"
        client = LSPXClient(ble_device, idle_disconnect=False)
        try:
            async with asyncio.timeout(PAIRING_FLOW_TIMEOUT):
                await client.pair(CONTROLLER_NAME, repairing=repairing)
        except LSPXError as err:
            self._error_detail = str(err).strip() or describe_error(err)
            _LOGGER.warning("Pairing with %s failed: %s", self._address, self._error_detail)
            return "pairing_failed"
        except (BleakError, TimeoutError, CommandRejected, ProtocolError) as err:
            self._error_detail = str(err).strip() or describe_error(err)
            _LOGGER.warning("Could not talk to %s: %s", self._address, self._error_detail)
            return "cannot_connect"
        finally:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(15):
                    await client.disconnect()
        return None

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_pair()
            if error is None:
                return self.async_create_entry(title=self._name or "LSPX-P1", data={CONF_ADDRESS: self._address})
            errors["base"] = error
        return self.async_show_form(
            step_id="pair",
            description_placeholders={"name": self._name or "", "error": self._error_detail},
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._address = entry_data[CONF_ADDRESS]
        self._name = self._get_reauth_entry().title
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # Free the link held by the running entry: some stacks share one connection per device.
            entry = self._get_reauth_entry()
            if entry.state is ConfigEntryState.LOADED:
                await entry.runtime_data.client.disconnect()
            error = await self._async_pair(repairing=True)
            if error is None:
                return self.async_update_reload_and_abort(self._get_reauth_entry())
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={"name": self._name or "", "error": self._error_detail},
            errors=errors,
        )


class LSPXOptionsFlow(OptionsFlow):
    """The options: a weather entity for the Weather poster (experimental), or none."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            # A cleared field is left out of user_input: no weather entity, the poster is left alone.
            return self.async_create_entry(data=user_input)
        schema = vol.Schema({vol.Optional(CONF_WEATHER_ENTITY): EntitySelector(EntitySelectorConfig(domain="weather"))})
        return self.async_show_form(
            step_id="init",
            # A suggested value rather than a default, so that the field can be cleared. Voluptuous: see above.
            data_schema=self.add_suggested_values_to_schema(schema, self.config_entry.options),  # type: ignore[arg-type]
        )
