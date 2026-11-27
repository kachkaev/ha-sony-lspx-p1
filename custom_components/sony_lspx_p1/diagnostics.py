"""Diagnostics support."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .client import POLL_LINGER, describe_error
from .coordinator import COMMUNICATION_ERRORS, LSPXConfigEntry, LSPXCoordinator
from .protocol import ErrorCode
from .redaction import scrub

# Reading the Weather poster's code connects to the projector; do not let an unreachable one hold up the download.
WEATHER_READ_TIMEOUT = 20.0


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: LSPXConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    diagnostics: dict[str, Any] = {
        "title": entry.title,
        "last_update_success": coordinator.last_update_success,
        "connected": coordinator.client.is_connected,
        # On, but the main board has not answered since it was switched on.
        "starting_up": coordinator.starting_up,
        # Phone names often contain their owners' names.
        "state": async_redact_data(dataclasses.asdict(coordinator.data), {"name"}) if coordinator.data else None,
        # Nothing here is asked of the projector: what Home Assistant hears of it, and what the link did since setup.
        "bluetooth": scrub(
            {
                "heard_by": _heard_by(hass, coordinator.client.address),
                # The Connection sensor's: why the last poll could not reach the projector (link_status.py).
                "connection": {
                    "status": coordinator.link_status,
                    "since": coordinator.link_status_since.isoformat() if coordinator.link_status_since else None,
                    "error": coordinator.link_error,
                },
                "link": coordinator.client.stats.as_dict(),
                "recent_polls": list(coordinator.recent_polls),
            },
            coordinator.client.address,
        ),
    }
    if coordinator.clock is not None:
        # Its errors may carry Bluetooth addresses, as the link's do.
        diagnostics["clock"] = scrub(coordinator.clock.diagnostics(), coordinator.client.address)
    if coordinator.web_settings is not None:
        # Recorded as they were read or written; errors may carry Bluetooth addresses, as the clock's do.
        diagnostics["settings"] = scrub(coordinator.web_settings.diagnostics(), coordinator.client.address)
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
            # Disconnecting soon after, as after a poll: the download does not keep other clients out for 15 s.
            # In standby a sleeping main board answers TIMEOUT, as to the polls: no error for the link's diagnostics.
            in_standby = coordinator.data is not None and coordinator.data.power is False
            return await coordinator.client.get_weather(
                linger=POLL_LINGER, expected=(ErrorCode.TIMEOUT,) if in_standby else ()
            )
    except COMMUNICATION_ERRORS as err:
        return f"error: {describe_error(err)}"


def _heard_by(hass: HomeAssistant, address: str) -> list[dict[str, Any]]:
    """Every adapter or proxy that can connect and hears the projector now, strongest signal first.

    Home Assistant picks one of these for each connection (by signal and free connection slots); which one it
    picked is in the debug log (habluetooth.wrappers, enabled with the integration's debug logging).
    """
    last = bluetooth.async_last_service_info(hass, address, connectable=True)
    heard: list[dict[str, Any]] = []
    for device in bluetooth.async_scanner_devices_by_address(hass, address, connectable=True):
        entry: dict[str, Any] = {
            "adapter": device.scanner.adapter,  # its name, without the address (unlike scanner.name)
            "type": type(device.scanner).__name__,
            "rssi": device.advertisement.rssi,
        }
        # Only for the advertisement Home Assistant's lookups use: per-scanner times are not exposed alike everywhere.
        if last is not None and last.source == device.scanner.source:
            entry["seconds_since_last_advertisement"] = round(bluetooth.MONOTONIC_TIME() - last.time, 1)
        heard.append(entry)
    return sorted(heard, key=lambda entry: -entry["rssi"])
