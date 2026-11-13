"""Settings read and written over the projector's Wi-Fi network (experimental): the daily timers (issue #5), the
mode the projector starts in and the slideshow interval (issue #6).

The app keeps these in the projector's HTTP API (web_api.py), so they only work while the projector is on the home
Wi-Fi. Their entities are disabled by default, and nothing here runs unless one is enabled: the coordinator only
refreshes while it has listeners, and only requests what its enabled entities show (their listener contexts).

Refreshes run in the background right after a poll (so the Bluetooth link is still open if the endpoint has to be
read), every REFRESH_INTERVAL and right after a change, only while the projector is on and not showing wireless
HDMI. HTTP requests hold no Bluetooth lock. Failures only make these entities unavailable and are logged at debug
level; changes made from Home Assistant report them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dt_time
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import web_api
from .client import describe_error
from .const import DOMAIN
from .coordinator import COMMUNICATION_ERRORS, LSPXConfigEntry, LSPXCoordinator
from .protocol import NetTopology, ProjectorState

_LOGGER = logging.getLogger(__name__)

REFRESH_INTERVAL = timedelta(minutes=10)
# A daily timer is written again (seven entries from today) once its last entry is less than five days ahead: about
# once a day, like the app (e0.v.d()). Each entry fires only once, so without that the timer stops after a week.
# (Six days would make a timer written after today's time had passed due again at once.)
TIMER_RENEW_AHEAD = timedelta(days=web_api.TIMER_DAYS - 2)

# Listener contexts: what the enabled entities show (fields of WebSettings), so that only that is requested.
TIMERS = "timers"
POSTER_RESUME = "poster_resume"
POSTER_DURATION = "poster_duration"

TIMER_TAGS = (web_api.TIMER_ON, web_api.TIMER_OFF)


@dataclass
class WebSettings:
    """What the last refresh read; a field is None when it is unknown (its entities are then unavailable)."""

    host: str | None = None
    timers: list[web_api.Timer] | None = None
    poster_resume: bool | None = None  # True: start with a poster; False: with the last mode shown
    poster_duration: int | None = None  # how long each slideshow photo stays up, in milliseconds
    errors: dict[str, str] = field(default_factory=dict)


class WebSettingsCoordinator(DataUpdateCoordinator[WebSettings]):
    """Reads and writes the settings over Wi-Fi; driven by the main coordinator's polls, not by a timer."""

    config_entry: LSPXConfigEntry

    def __init__(self, hass: HomeAssistant, coordinator: LSPXCoordinator) -> None:
        entry = coordinator.config_entry
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.unique_id} settings over Wi-Fi",
            update_interval=None,
            always_update=False,
        )
        self.main = coordinator
        self.data = WebSettings()
        self._lock = asyncio.Lock()  # one refresh or change at a time
        self._task: asyncio.Task[None] | None = None
        self._projector: ProjectorState | None = None  # the last poll's state; None after a failed poll
        self._refreshed_at: datetime | None = None
        self._refresh_soon = True
        self._read_contexts: set[Any] = set()  # what the last refresh read
        # Each timer's time while it is off (the projector then has no entry to read it from), as the app keeps it.
        self.remembered: dict[str, dt_time] = {}

    @callback
    def async_start(self) -> Callable[[], None]:
        """Follow the main coordinator; returns the function that stops it."""
        return self.main.async_add_listener(self._async_main_updated)

    @callback
    def _async_main_updated(self) -> None:
        if not self.main.last_update_success:
            self._projector = None
            self.async_check()

    @callback
    def async_poll_done(self, state: ProjectorState) -> None:
        """Called by the main coordinator after each successful poll."""
        self._projector = state
        self.async_check()

    @property
    def _projector_ready(self) -> bool:
        state = self._projector
        return bool(state is not None and state.power and state.net_topology != NetTopology.WIRELESS_HDMI)

    @callback
    def async_check(self) -> None:
        """Refresh in the background if an entity is enabled and a refresh is due; forget everything if off."""
        if not self._listeners:
            return
        if not self._projector_ready:
            self._refresh_soon = True
            if self.data != WebSettings():
                self.async_set_updated_data(WebSettings())
            return
        now = dt_util.utcnow()
        due = (
            self._refresh_soon
            or self._refreshed_at is None
            or now - self._refreshed_at >= REFRESH_INTERVAL
            or not set(self.async_contexts()) <= self._read_contexts  # an entity was enabled since
        )
        if due and (self._task is None or self._task.done()):
            self._task = self.config_entry.async_create_background_task(
                self.hass, self._async_refresh_and_check(), f"{DOMAIN} settings over Wi-Fi"
            )

    async def _async_refresh_and_check(self) -> None:
        await self.async_refresh()
        self._task = None  # this task is done: let the check start another one
        self.async_check()  # for entities added while it ran (their platforms are set up one after another)

    async def _async_update_data(self) -> WebSettings:
        self._refreshed_at, self._refresh_soon = dt_util.utcnow(), False
        self._read_contexts = set(self.async_contexts())
        if not self._projector_ready:
            return WebSettings()
        async with self._lock:
            data = await self._async_read()
        if data.errors:
            _LOGGER.debug("%s: reading settings over Wi-Fi: %s", self.main.client.address, data.errors)
        return data

    async def _async_read(self) -> WebSettings:
        try:
            endpoint = await self.main.async_http_endpoint()
        except COMMUNICATION_ERRORS as err:
            return WebSettings(errors={"endpoint": f"reading the endpoint over Bluetooth: {describe_error(err)}"})
        host = endpoint.host
        if host is None:
            return WebSettings(errors={"endpoint": f"not on Wi-Fi (endpoint address {endpoint.address!r})"})
        data = WebSettings(host=host)
        session = async_get_clientsession(self.hass)
        contexts = self._read_contexts
        reads: dict[str, Callable[[], Awaitable[Any]]] = {
            TIMERS: lambda: self._async_read_timers(session, host, data),
            POSTER_RESUME: lambda: web_api.get_poster_resume(session, host),
            POSTER_DURATION: lambda: web_api.get_poster_duration(session, host),
        }
        for name, read in reads.items():
            if name not in contexts:
                continue
            try:
                setattr(data, name, await read())
            except web_api.WebApiError as err:
                data.errors[name] = str(err)
        if all(getattr(data, name) is None for name in contexts if name in reads):
            self.main.forget_http_endpoint()  # nothing answered: maybe the address changed
        return data

    async def _async_read_timers(
        self, session: aiohttp.ClientSession, host: str, data: WebSettings
    ) -> list[web_api.Timer]:
        timers = await web_api.get_timers(session, host)
        now = dt_util.as_local(dt_util.utcnow())
        renew_before = web_api.local_unix_seconds(now) + int(TIMER_RENEW_AHEAD.total_seconds())
        renewed = False
        for tag in TIMER_TAGS:
            at = web_api.timer_time(timers, tag)
            if at is None:
                continue
            self.remembered[tag] = at
            if max(t.time for t in timers if t.tag == tag and t.time is not None) >= renew_before:
                continue
            try:
                await web_api.set_daily_timer(session, host, tag, now.date(), at)
            except web_api.WebApiError as err:
                data.errors[f"{tag}_renew"] = str(err)
            _LOGGER.debug("%s: wrote the %s entries again (%s)", self.main.client.address, tag, at)
            renewed = True
        return await web_api.get_timers(session, host) if renewed else timers

    # --- what the entities show ----------------------------------------------------------------------------------

    def timer_on(self, tag: str) -> bool | None:
        """Whether a daily timer is on: whether the projector has entries for it (as the app decides)."""
        timers = self.data.timers
        return None if timers is None else any(t.tag == tag for t in timers)

    def timer_time(self, tag: str) -> dt_time | None:
        timers = self.data.timers
        if timers is None:
            return None
        return web_api.timer_time(timers, tag) or self.remembered.get(tag)

    # --- changes -------------------------------------------------------------------------------------------------

    async def async_set_timer(self, tag: str, on: bool) -> None:
        if not on:
            await self._async_change(lambda session, host: web_api.clear_daily_timer(session, host, tag))
            return
        at = self.timer_time(tag)
        if at is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="timer_time_unknown")
        await self._async_write_timer(tag, at)

    async def async_set_timer_time(self, tag: str, at: dt_time) -> None:
        at = at.replace(second=0, microsecond=0)
        self.remembered[tag] = at
        if self.timer_on(tag):
            await self._async_write_timer(tag, at)
        else:
            self.async_update_listeners()  # only remembered until the timer is switched on

    async def async_set_poster_resume(self, poster: bool) -> None:
        await self._async_change(lambda session, host: web_api.set_poster_resume(session, host, poster))

    async def async_set_poster_duration(self, milliseconds: int) -> None:
        await self._async_change(lambda session, host: web_api.set_poster_duration(session, host, milliseconds))

    async def _async_write_timer(self, tag: str, at: dt_time) -> None:
        today = dt_util.as_local(dt_util.utcnow()).date()
        await self._async_change(lambda session, host: web_api.set_daily_timer(session, host, tag, today, at))

    async def _async_change(self, change: Callable[[aiohttp.ClientSession, str], Awaitable[Any]]) -> None:
        """Make a change over HTTP, then read the settings back. Raises HomeAssistantError."""
        host = self.data.host
        if host is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="web_settings_unreachable")
        try:
            async with self._lock:
                await change(async_get_clientsession(self.hass), host)
        except web_api.WebApiError as err:
            self.main.forget_http_endpoint()
            self._refresh_soon = True  # the next poll shows what the projector has now
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="web_settings_failed",
                translation_placeholders={"host": host, "error": str(err)},
            ) from err
        await self.async_refresh()

    def diagnostics(self) -> dict[str, Any]:
        data = self.data
        return {
            "refreshed_at": self._refreshed_at.isoformat() if self._refreshed_at else None,
            "host": data.host,
            "errors": data.errors,
            "timers": None
            if data.timers is None
            else [
                {"id": t.id, "tag": t.tag, "state": t.state, "time": web_api.wall_clock_text(t.time)}
                for t in data.timers
            ],
            "remembered": {tag: at.isoformat() for tag, at in self.remembered.items()},
            "poster_resume": data.poster_resume,
            "poster_duration_ms": data.poster_duration,
        }
