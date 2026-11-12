"""Diagnostics support."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import LSPXConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: LSPXConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "title": entry.title,
        "last_update_success": coordinator.last_update_success,
        "connected": coordinator.client.is_connected,
        # Phone names often contain their owners' names.
        "state": async_redact_data(dataclasses.asdict(coordinator.data), {"name"}) if coordinator.data else None,
    }
