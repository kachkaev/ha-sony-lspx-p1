"""Data update coordinator for the Sony LSPX-P1."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import LSPXClient, LSPXError, NotPairedError, ProjectorBusyError
from .const import DOMAIN, POWER_SETTLE_REFRESH_DELAYS, UPDATE_INTERVAL
from .protocol import CommandRejected, ProjectorState, ProtocolError

_LOGGER = logging.getLogger(__name__)

type LSPXConfigEntry = ConfigEntry[LSPXCoordinator]

COMMUNICATION_ERRORS = (LSPXError, CommandRejected, ProtocolError, BleakError, TimeoutError)


class LSPXCoordinator(DataUpdateCoordinator[ProjectorState]):
    """Polls the projector and serialises commands."""

    config_entry: LSPXConfigEntry

    def __init__(self, hass: HomeAssistant, entry: LSPXConfigEntry, client: LSPXClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.unique_id}",
            update_interval=UPDATE_INTERVAL,
            always_update=False,
        )
        self.client = client
        self._settle_unsubs: list[Callable[[], None]] = []

    async def _async_update_data(self) -> ProjectorState:
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.client.address, connectable=True)
        if ble_device is None:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="device_not_found")
        self.client.set_ble_device(ble_device)
        try:
            return await self.client.read_state(self.data)
        except NotPairedError as err:
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="not_paired") from err
        except ProjectorBusyError as err:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="projector_busy") from err
        except COMMUNICATION_ERRORS as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="communication_error",
                translation_placeholders={"error": str(err) or type(err).__name__},
            ) from err

    async def async_command(
        self, func: Callable[[], Awaitable[Any]], apply: Callable[[ProjectorState, Any], None] | None = None
    ) -> None:
        """Run a client call from an entity and merge its read-back into the state."""
        try:
            result = await func()
        except NotPairedError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="not_paired") from err
        except ProjectorBusyError as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="projector_busy") from err
        except COMMUNICATION_ERRORS as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="communication_error",
                translation_placeholders={"error": str(err) or type(err).__name__},
            ) from err
        if apply is not None and self.data is not None:
            apply(self.data, result)
            self.async_set_updated_data(self.data)

    async def async_set_power(self, on: bool) -> None:
        def apply(state: ProjectorState, _result: Any) -> None:
            state.power = on

        await self.async_command(lambda: self.client.set_power(on), apply)
        self._schedule_settle_refreshes()

    @callback
    def _schedule_settle_refreshes(self) -> None:
        for unsub in self._settle_unsubs:
            unsub()
        self._settle_unsubs = [
            async_call_later(self.hass, delay, self._settle_refresh) for delay in POWER_SETTLE_REFRESH_DELAYS
        ]

    async def _settle_refresh(self, _now: Any) -> None:
        await self.async_request_refresh()

    async def async_shutdown(self) -> None:
        for unsub in self._settle_unsubs:
            unsub()
        self._settle_unsubs = []
        await super().async_shutdown()
        await asyncio.shield(self.client.disconnect())
