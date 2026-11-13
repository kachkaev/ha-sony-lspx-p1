"""Diagnostics support."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .client import describe_error
from .coordinator import COMMUNICATION_ERRORS, LSPXConfigEntry, LSPXCoordinator

# Reading the Weather poster's code connects to the projector; do not let an unreachable one hold up the download.
WEATHER_READ_TIMEOUT = 20.0


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: LSPXConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    diagnostics: dict[str, Any] = {
        "title": entry.title,
        "last_update_success": coordinator.last_update_success,
        "connected": coordinator.client.is_connected,
        # Phone names often contain their owners' names.
        "state": async_redact_data(dataclasses.asdict(coordinator.data), {"name"}) if coordinator.data else None,
    }
    if coordinator.clock is not None:
        diagnostics["clock"] = coordinator.clock.diagnostics()
    if coordinator.web_settings is not None:
        diagnostics["settings_over_wifi"] = coordinator.web_settings.diagnostics()
    if coordinator.weather is not None:
        diagnostics["weather"] = {
            **coordinator.weather.diagnostics(),
            # Read here only, not on every poll: shows whether the projector keeps what was sent.
            "projector_code": await _read_weather(coordinator),
        }
    return diagnostics


async def _read_weather(coordinator: LSPXCoordinator) -> int | str:
    try:
        async with asyncio.timeout(WEATHER_READ_TIMEOUT):
            return await coordinator.client.get_weather()
    except COMMUNICATION_ERRORS as err:
        return f"error: {describe_error(err)}"
