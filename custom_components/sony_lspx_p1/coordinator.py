"""Data update coordinator for the Sony LSPX-P1."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    ControllerNotFoundError,
    LSPXClient,
    LSPXError,
    NotPairedError,
    OwnControllerError,
    OwnControllerUnknownError,
    ProjectorBusyError,
)
from .const import DOMAIN, POWER_SETTLE_REFRESH_DELAYS, UPDATE_INTERVAL
from .protocol import (
    AppState,
    BondCommandFailed,
    BondOp,
    CommandRejected,
    Controller,
    ErrorCode,
    Poster,
    ProjectorState,
    ProtocolError,
)

if TYPE_CHECKING:
    from .weather import WeatherPoster

_LOGGER = logging.getLogger(__name__)

type LSPXConfigEntry = ConfigEntry[LSPXCoordinator]

# A source chosen in standby is applied once a poll sees the projector on, if that happens this soon.
PENDING_SOURCE_TIMEOUT = 90.0
# After switching on, the projector rejects commands for a while (UNKNOWN_COMMAND or TIMEOUT) as it starts
# up. Commands sent in this window are retried instead of failing, so changes made right away still land.
POWER_ON_WARMUP = 30.0
WARMUP_RETRY_DELAY = 1.0
# A stuck internal link makes every poll time out until RESET is pressed. A few busy polls in a row
# (about five minutes) are not just a slow start-up, so a repair issue then explains the button.
STUCK_POLLS_BEFORE_ISSUE = 5

type SourceCall = tuple[Callable[[], Awaitable[Any]], Callable[[ProjectorState, Any], None]]

COMMUNICATION_ERRORS = (LSPXError, CommandRejected, BondCommandFailed, ProtocolError, BleakError, TimeoutError)


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
        self._pending_source: tuple[SourceCall, float] | None = None
        self._warmup_until = 0.0
        # A source switch is several commands; a power change must not land between them. Home Assistant
        # already runs the media player's actions one at a time; this also covers a source chosen in standby,
        # which a poll applies once the projector is on.
        self._switching = asyncio.Lock()
        # Polls in a row that timed out with 0xC0. Other failures (out of range, connection errors) say
        # nothing about the link and leave it as is; only a successful poll resets it.
        self._busy_polls = 0
        self._stuck_issue_id = f"projector_stuck_{entry.entry_id}"
        # Keeps the Weather poster in sync with a weather entity, when one is chosen in the options.
        self.weather: WeatherPoster | None = None

    async def _async_update_data(self) -> ProjectorState:
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.client.address, connectable=True)
        if ble_device is None:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="device_not_found")
        self.client.set_ble_device(ble_device)
        try:
            state = await self.client.read_state(self.data)
        except NotPairedError as err:
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="not_paired") from err
        except ProjectorBusyError as err:
            self._busy_polls += 1
            if self._busy_polls == STUCK_POLLS_BEFORE_ISSUE:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    self._stuck_issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="projector_stuck",
                    translation_placeholders={"name": self.config_entry.title},
                )
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="projector_busy") from err
        except COMMUNICATION_ERRORS as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="communication_error",
                translation_placeholders={"error": str(err) or type(err).__name__},
            ) from err
        if self._busy_polls:
            self._busy_polls = 0
            ir.async_delete_issue(self.hass, DOMAIN, self._stuck_issue_id)
        if (pending := self._pending_source) is not None and state.power:
            await self._apply_pending_source(pending, state)
        return state

    async def _apply_pending_source(self, pending: tuple[SourceCall, float], state: ProjectorState) -> None:
        (func, apply), deadline = pending
        self._pending_source = None
        if asyncio.get_running_loop().time() > deadline:
            return
        try:
            async with self._switching:
                result = await self._through_warmup(func)
        except COMMUNICATION_ERRORS as err:
            _LOGGER.warning("%s: switching the source after power-on failed: %s", self.client.address, err)
            return
        apply(state, result)
        self._schedule_settle_refreshes()

    async def async_select_source(
        self, func: Callable[[], Awaitable[Any]], apply: Callable[[ProjectorState, Any], None]
    ) -> None:
        """Switch what the projector shows; from standby, switch it on first (as the app does)."""
        if self.data is not None and self.data.power is False:
            deadline = asyncio.get_running_loop().time() + PENDING_SOURCE_TIMEOUT
            self._pending_source = ((func, apply), deadline)
            await self.async_set_power(True)
            return
        async with self._switching:
            await self.async_command(func, apply)
        self._schedule_settle_refreshes()

    async def async_command(
        self, func: Callable[[], Awaitable[Any]], apply: Callable[[ProjectorState, Any], None] | None = None
    ) -> None:
        """Run a client call from an entity and merge its read-back into the state."""
        try:
            result = await self._through_warmup(func)
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

    async def _through_warmup(self, func: Callable[[], Awaitable[Any]], *, lock: asyncio.Lock | None = None) -> Any:
        """Run ``func``, retrying while the projector is still starting up after being switched on.

        With ``lock``, each attempt holds it but the waits between attempts do not.
        """
        loop = asyncio.get_running_loop()
        while True:
            try:
                async with lock if lock is not None else contextlib.nullcontext():
                    return await func()
            except (ProjectorBusyError, CommandRejected) as err:
                starting_up = isinstance(err, ProjectorBusyError) or err.code == ErrorCode.UNKNOWN_COMMAND
                if not starting_up or loop.time() + WARMUP_RETRY_DELAY > self._warmup_until:
                    raise
                _LOGGER.debug("%s: still starting up, retrying: %s", self.client.address, err)
                await asyncio.sleep(WARMUP_RETRY_DELAY)

    async def async_set_power(self, on: bool) -> None:
        def apply(state: ProjectorState, _result: Any) -> None:
            state.power = on

        if not on:
            self._pending_source = None
        async with self._switching:
            await self.async_command(lambda: self.client.set_power(on), apply)
        self._warmup_until = asyncio.get_running_loop().time() + POWER_ON_WARMUP if on else 0.0
        self._schedule_settle_refreshes()

    @property
    def weather_poster_shown(self) -> bool:
        """Whether the last poll reached the projector and it is on, showing the Weather poster."""
        data = self.data
        return bool(
            self.last_update_success
            and data is not None
            and data.power
            and data.app_state == AppState.POSTER
            and data.poster == Poster.WEATHER
        )

    async def async_set_weather(self, code: int) -> bool:
        """Send the Weather poster's code while it is shown; False if it is not. Raises COMMUNICATION_ERRORS.

        Only then: what SetWeather does while HDMI or another poster shows is unknown (it might switch to the
        Weather poster). For background updates: it waits for a source switch or power change in progress and
        for the start-up after switching on. A projector that does not know the command answers like one still
        starting up, so the waits between attempts do not hold up power changes and source switches.
        """

        async def attempt() -> bool:
            if not self.weather_poster_shown:
                return False
            await self.client.set_weather(code)
            return True

        sent: bool = await self._through_warmup(attempt, lock=self._switching)
        return sent

    async def async_reboot(self) -> None:
        """Restart the projector. Polls fail while it boots; the settle refreshes notice when it is back."""
        async with self._switching:
            await self.async_command(self.client.reboot)
        self._schedule_settle_refreshes()

    async def async_manage_controller(self, op: BondOp, controller_id: int) -> None:
        """Remove, allow or block an entry of "Smartphones added", then show the list as it is now."""
        placeholders = {"smartphone_id": str(controller_id)}

        async def run() -> list[Controller]:
            try:
                return await self.client.manage_controller(op, controller_id)
            except ControllerNotFoundError as err:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="unknown_smartphone",
                    translation_placeholders=placeholders,
                ) from err
            except OwnControllerError as err:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="own_smartphone", translation_placeholders=placeholders
                ) from err
            except OwnControllerUnknownError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="own_smartphone_unknown",
                    translation_placeholders={"error": str(err)},
                ) from err
            except BondCommandFailed as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="smartphone_change_failed",
                    translation_placeholders={**placeholders, "error": str(err)},
                ) from err

        def apply(state: ProjectorState, controllers: list[Controller]) -> None:
            state.controllers = controllers
            state.errors.pop("controllers", None)

        await self.async_command(run, apply)

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
        ir.async_delete_issue(self.hass, DOMAIN, self._stuck_issue_id)
        await super().async_shutdown()
        await asyncio.shield(self.client.disconnect())
