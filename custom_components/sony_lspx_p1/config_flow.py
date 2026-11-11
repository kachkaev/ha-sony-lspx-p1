"""Config flow for the Sony LSPX-P1: discover over Bluetooth, then pair."""

from __future__ import annotations

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
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .client import LSPXClient, LSPXError
from .const import CONTROLLER_NAME, DOMAIN
from .protocol import LOCAL_NAME_PREFIX, SONY_MANUFACTURER_ID, CommandRejected, ProtocolError

_LOGGER = logging.getLogger(__name__)


def is_lspx_p1(info: BluetoothServiceInfoBleak) -> bool:
    name = info.name or ""
    if name.startswith(LOCAL_NAME_PREFIX):
        return True
    data = info.manufacturer_data.get(SONY_MANUFACTURER_ID)
    return data is not None and data.startswith(b"\xee\x00\x5a\xb8")


def title_for(info_name: str | None, address: str) -> str:
    """'LSPXP1_6320' -> 'LSPX-P1 6320'."""
    if info_name and info_name.startswith(LOCAL_NAME_PREFIX):
        suffix = info_name[len(LOCAL_NAME_PREFIX) :].lstrip("_- ")
        return f"LSPX-P1 {suffix}".strip()
    return f"LSPX-P1 {address[-5:]}"


class LSPXConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._name: str | None = None
        self._discovered: dict[str, str] = {}

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        if not is_lspx_p1(discovery_info):
            return self.async_abort(reason="not_supported")
        self._address = discovery_info.address
        self._name = title_for(discovery_info.name, discovery_info.address)
        self.context["title_placeholders"] = {"name": self._name}
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
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(self._discovered)}),
        )

    async def _async_pair(self) -> str | None:
        """Pair with the projector. Returns an error key, or None on success."""
        assert self._address is not None
        ble_device = async_ble_device_from_address(self.hass, self._address, connectable=True)
        if ble_device is None:
            return "device_not_found"
        client = LSPXClient(ble_device, idle_disconnect=None)
        try:
            await client.pair(CONTROLLER_NAME)
        except LSPXError as err:
            _LOGGER.warning("Pairing with %s failed: %s", self._address, err)
            return "pairing_failed"
        except (BleakError, TimeoutError, CommandRejected, ProtocolError) as err:
            _LOGGER.warning("Could not talk to %s: %s", self._address, err)
            return "cannot_connect"
        finally:
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
            description_placeholders={"name": self._name or ""},
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._address = entry_data[CONF_ADDRESS]
        self._name = self._get_reauth_entry().title
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_pair()
            if error is None:
                return self.async_update_reload_and_abort(self._get_reauth_entry())
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={"name": self._name or ""},
            errors=errors,
        )
