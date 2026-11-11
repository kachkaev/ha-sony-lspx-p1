"""Diagnostics support."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import LSPXConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: LSPXConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "title": entry.title,
        "last_update_success": coordinator.last_update_success,
        "connected": coordinator.client.is_connected,
        "state": dataclasses.asdict(coordinator.data) if coordinator.data else None,
    }
