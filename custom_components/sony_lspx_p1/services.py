"""Service actions: remove, block or allow an entry of the projector's "Smartphones added" list.

The list changes at runtime, so these take the entry's id (the Smartphones added sensor's
``smartphone_ids`` attribute) rather than having an entity per smartphone.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID, CONF_ADDRESS
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import LSPXCoordinator
from .protocol import BondOp

ATTR_SMARTPHONE_ID = "smartphone_id"

SERVICE_OPS: dict[str, BondOp] = {
    "remove_smartphone": BondOp.DELETE_CONTROLLER,
    "block_smartphone": BondOp.BLOCK_CONTROLLER,
    "allow_smartphone": BondOp.ALLOW_CONTROLLER,
}

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_SMARTPHONE_ID): vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
    }
)


def _coordinator(hass: HomeAssistant, device_id: str) -> LSPXCoordinator:
    device = dr.async_get(hass).async_get(device_id)
    identifiers = device.identifiers if device is not None else set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        # Entities register the device under the projector's address (entity.py).
        if (DOMAIN, entry.data[CONF_ADDRESS]) in identifiers and entry.state is ConfigEntryState.LOADED:
            coordinator: LSPXCoordinator = entry.runtime_data
            return coordinator
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_device")


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    async def handle(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call.data[ATTR_DEVICE_ID])
        await coordinator.async_manage_controller(SERVICE_OPS[call.service], call.data[ATTR_SMARTPHONE_ID])

    for service in SERVICE_OPS:
        # Home Assistant 2026.10+ types this as probatio.Schema (API-compatible with voluptuous, which it
        # still accepts); voluptuous keeps older versions working, as in config_flow.py.
        hass.services.async_register(DOMAIN, service, handle, schema=SERVICE_SCHEMA)  # type: ignore[arg-type]
