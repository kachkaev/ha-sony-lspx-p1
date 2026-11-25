"""Settings the projector keeps in its HTTP API (experimental): the daily timers (issue #5), the mode the projector
starts in and the slideshow interval (issue #6).

Read and written through the Bluetooth tunnel to the HTTP API (tunnel.py), as the clock is set (clock.py). The app
sends the poster settings through the tunnel too; the timers it sends only over Wi-Fi. None of these requests was seen
answering on a projector.

Each GET through the tunnel holds the Bluetooth link, which the smartphone app also wants, so the settings are read
seldom: right after a poll (the link is still open) that finds the projector on, its main board up and a poster
showing (as the clock's automatic syncs), the first time and then every REFRESH_INTERVAL, and only what enabled
entities show (their listener contexts). A setting that fails is tried again after RETRY_DELAYS, so one the projector
never answered costs a request a day and stays unavailable (one it answered before keeps its value and is asked
hourly); failures are only logged at debug level. What was read is kept through standby, start-up and other sources,
while the polls succeed. Changes are refused while the projector
is in standby or starting up, and report their errors.

A timer is written as the app writes it (a week of single entries, renewed every other day) but only what is missing
is created, so a write that stopped part way is finished by the next one: a change that failed is tried again with
the next reads (an hour later, then a day after that). Timer writes back off on their own after failures,
as the reads do.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dt_time
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import tunnel, web_api
from .client import describe_error
from .const import DOMAIN
from .coordinator import COMMUNICATION_ERRORS, LSPXConfigEntry, LSPXCoordinator
from .protocol import AppState, ProjectorState

_LOGGER = logging.getLogger(__name__)

# The settings change only from the app or Home Assistant; reading them hourly shows the app's changes soon enough.
REFRESH_INTERVAL = timedelta(hours=1)
# After failed reads (or timer writes) in a row: an hour, then daily, so a setting the projector does not answer
# costs a request a day. One that was read before is asked hourly, so a passing failure does not hide it for long.
RETRY_DELAYS = (timedelta(hours=1), timedelta(days=1))
# A timer change that failed is tried again with the next reads for this long, then given up: as writes back off
# (RETRY_DELAYS), an hour later and a day after that.
PENDING_MAX_AGE = timedelta(days=2)
# A daily timer is written again (seven entries from today) once its last entry is less than four days ahead: every
# other day, creating the two days missing and deleting the two past. Each entry fires only once, so without that the
# timer stops after a week. (Six days would make a timer written after today's time had passed due again at once.)
TIMER_RENEW_AHEAD = timedelta(days=web_api.TIMER_DAYS - 3)

# Listener contexts: what the enabled entities show (fields of WebSettings), so that only that is requested.
TIMERS = "timers"
POSTER_RESUME = "poster_resume"
POSTER_DURATION = "poster_duration"
SETTINGS = (POSTER_RESUME, POSTER_DURATION, TIMERS)

TIMER_TAGS = (web_api.TIMER_ON, web_api.TIMER_OFF)

READS: dict[str, Callable[[web_api.Api], Awaitable[Any]]] = {
    POSTER_RESUME: web_api.get_poster_resume,
    POSTER_DURATION: web_api.get_poster_duration,
    TIMERS: web_api.get_timers,
}


@dataclass
class WebSettings:
    """What was last read; a field is None when it is unknown (its entities are then unavailable)."""

    timers: list[web_api.Timer] | None = None
    poster_resume: bool | None = None  # True: start with a poster; False: with the last mode shown
    poster_duration: int | None = None  # how long each slideshow photo stays up, in milliseconds


@dataclass
class _Status:
    """How a setting was last read (or the timers last written in the background)."""

    failures: int = 0  # in a row; a Bluetooth link that fails does not count
    last_attempt: datetime | None = None
    last_success: datetime | None = None
    last_error: str | None = None

    def due(self, now: datetime, interval: timedelta = REFRESH_INTERVAL) -> bool:
        if self.last_attempt is None:
            return True
        delay = interval
        if self.failures:
            delay = RETRY_DELAYS[0 if self.last_success else min(self.failures, len(RETRY_DELAYS)) - 1]
        return now - self.last_attempt >= delay

    def failed(self, detail: str, *, count: bool = True) -> None:
        self.failures += count
        self.last_error = detail

    def succeeded(self) -> None:
        self.failures, self.last_error, self.last_success = 0, None, dt_util.utcnow()

    def as_dict(self) -> dict[str, Any]:
        return {
            "failures": self.failures,
            "last_attempt": _iso(self.last_attempt),
            "last_success": _iso(self.last_success),
            "last_error": self.last_error,
        }


class _Failed(Exception):
    """The tunnel or the HTTP API did not do it; ``detail`` says why, in English, for diagnostics and errors."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class TunnelApi:
    """The HTTP API through the Bluetooth tunnel (web_api.Api): one request at a time, the link held for each."""

    def __init__(self, coordinator: LSPXCoordinator) -> None:
        self._coordinator = coordinator

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        text = None if body is None else json.dumps(body, separators=(",", ":"))
        response = await self._coordinator.async_web_request(
            tunnel.Method[method], path, None if text is None else lambda: text
        )
        return web_api.parse_reply(response.http_status, response.text())


class WebSettingsCoordinator(DataUpdateCoordinator[WebSettings]):
    """Reads and writes the settings; driven by the main coordinator's polls, not by a timer."""

    config_entry: LSPXConfigEntry

    def __init__(self, hass: HomeAssistant, coordinator: LSPXCoordinator) -> None:
        entry = coordinator.config_entry
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.unique_id} settings",
            update_interval=None,
            always_update=False,
        )
        self.main = coordinator
        self.data = WebSettings()
        self._lock = asyncio.Lock()  # one refresh or change at a time
        self._task: asyncio.Task[None] | None = None
        self._projector: ProjectorState | None = None  # the last poll's state; None after a failed poll
        self._polls_ok = True
        # The Bluetooth link failed in a refresh, or the projector said its power or what it shows changed: wait for the
        # next poll.
        self._link_failed = False
        self._tunnel = TunnelApi(coordinator)
        self.status = {name: _Status() for name in SETTINGS}
        self.timer_writes = _Status()  # renewals and changes that failed, written with the reads
        # Timer changes that failed, tried again with the next reads: tag -> (the time, None for off; when asked).
        self.pending: dict[str, tuple[dt_time | None, datetime]] = {}
        # Each timer's time while it is off (the projector then has no entry to read it from), as the app keeps it.
        self.remembered: dict[str, dt_time] = {}

    @callback
    def async_start(self) -> Callable[[], None]:
        """Follow the main coordinator; returns the function that stops it."""
        return self.main.async_add_listener(self._async_main_updated)

    @callback
    def _async_main_updated(self) -> None:
        ok = self.main.last_update_success
        if not ok:
            self._projector = None
        if ok != self._polls_ok:
            self._polls_ok = ok
            self.async_update_listeners()  # the entities are available only while the polls succeed

    @callback
    def async_poll_done(self, state: ProjectorState) -> None:
        """Called by the main coordinator after each successful poll."""
        self._projector, self._link_failed = state, False
        self.async_check()

    def ready(self, state: ProjectorState | None) -> bool:
        """Whether the projector is on with its main board up, which runs the HTTP API (as for the clock)."""
        return bool(state and state.power and state.app_state is not None and not self.main.starting_up)

    @callback
    def async_check(self) -> None:
        """Read in the background what enabled entities show, if due and the projector shows a poster."""
        state = self._projector
        if self._link_failed or not self._listeners or not self.ready(state) or state is None:
            return
        if state.app_state != AppState.POSTER:
            return
        now = dt_util.utcnow()
        if not any(self.status[name].due(now) for name in self._shown()):
            return
        if self._task is None or self._task.done():
            self._task = self.config_entry.async_create_background_task(
                self.hass, self._async_refresh_and_check(), f"{DOMAIN} settings"
            )

    def _shown(self) -> list[str]:
        contexts = set(self.async_contexts())
        return [name for name in SETTINGS if name in contexts]

    async def _async_refresh_and_check(self) -> None:
        await self.async_refresh()
        self._task = None  # this task is done: let the check start another one
        self.async_check()  # for entities added while it ran (their platforms are set up one after another)

    async def _async_update_data(self) -> WebSettings:
        """Read what is due. Never raises: a setting that fails keeps its last value or stays unknown."""
        async with self._lock:
            data = dataclasses.replace(self.data)
            now = dt_util.utcnow()
            notified = self.main.changes_notified
            for name in self._shown():
                if not self.status[name].due(now):
                    continue
                if self.main.changes_notified != notified:
                    # Switched off by its button, say: the rest waits for the poll the notification calls for.
                    self._link_failed = True
                    break
                try:
                    value = await self._async_run(name, READS[name])
                    setattr(data, name, value)  # kept should the writes after it fail
                    if name == TIMERS:
                        data.timers = await self._async_keep_timers(value)
                except _Failed:
                    pass  # kept if it was read before; unknown otherwise
                except COMMUNICATION_ERRORS:
                    self._link_failed = True  # the rest waits for the next poll
                    break
            return data

    async def _async_run[T](self, name: str, op: Callable[[web_api.Api], Awaitable[T]]) -> T:
        """Run ``op``, recorded in ``status``. Raises _Failed, or COMMUNICATION_ERRORS when the Bluetooth link fails
        (tried again an hour later, as after a read)."""
        status = self.status[name]
        status.last_attempt = dt_util.utcnow()
        try:
            result = await self._async_request(op)
        except _Failed as err:
            self._failed(status, name, err.detail)
            raise
        except COMMUNICATION_ERRORS as err:
            self._failed(status, name, f"over Bluetooth: {describe_error(err)}", count=False)
            raise
        status.succeeded()
        return result

    def _failed(self, status: _Status, name: str, detail: str, *, count: bool = True) -> None:
        status.failed(detail, count=count)
        _LOGGER.debug("%s: %s: %s", self.main.client.address, name, detail)

    async def _async_request[T](self, op: Callable[[web_api.Api], Awaitable[T]]) -> T:
        try:
            return await op(self._tunnel)
        except (tunnel.TunnelError, web_api.WebApiError) as err:
            # The projector answered, but not as hoped. (A Bluetooth link that fails is raised.)
            raise _Failed(f"over Bluetooth: {err or type(err).__name__}") from err

    # --- the timers ----------------------------------------------------------------------------------------------

    async def _async_keep_timers(self, timers: list[web_api.Timer]) -> list[web_api.Timer]:
        """Finish a change that failed, and renew a timer that is due (a write runs again from what the projector
        has, so the next try finishes it).

        Writes back off on their own (``timer_writes``): a failure leaves the reads alone, and the timers keep what was
        read. Raises COMMUNICATION_ERRORS when the Bluetooth link fails.
        """
        now = dt_util.as_local(dt_util.utcnow())
        changes: dict[str, tuple[dt_time | None, list[int], bool]] = {}  # tag -> (time, entries, renewal)
        for tag in TIMER_TAGS:
            if (change := self._wanted(timers, tag, now)) is None:
                pass
            elif any(web_api.timer_changes(timers, tag, change[1])):
                changes[tag] = (*change, tag not in self.pending)
            else:  # nothing to write: done already, or only entries that cannot be changed
                self._written(tag, change[0], timers, change[1])
            if (
                tag not in self.pending
                and len(_times_of_day(timers, tag)) == 1
                and (at := web_api.timer_time(timers, tag))
            ):
                self.remembered[tag] = at
        status = self.timer_writes
        if not changes or not _writes_due(status, dt_util.utcnow()):
            return timers
        status.last_attempt = dt_util.utcnow()
        try:
            for tag, (_at, wanted, renewal) in changes.items():
                # A renewal deletes the entries that fired first: should the projector refuse to delete, nothing is
                # created, so the list does not grow. The other tag's entries do not matter.
                await web_api.write_daily_timer(self._tunnel, tag, wanted, timers, delete_first=renewal)
                _LOGGER.debug("%s: wrote the %s entries (%d)", self.main.client.address, tag, len(wanted))
            written = await web_api.get_timers(self._tunnel)
        except (tunnel.TunnelError, web_api.WebApiError) as err:
            self._failed(status, "writing the timers", str(err) or type(err).__name__)
            return timers
        except COMMUNICATION_ERRORS as err:
            self._failed(status, "writing the timers", f"over Bluetooth: {describe_error(err)}", count=False)
            raise
        status.succeeded()
        for tag, (at, wanted, _renewal) in changes.items():
            self._written(tag, at, written, wanted)
        return written

    def _wanted(self, timers: list[web_api.Timer], tag: str, now: datetime) -> tuple[dt_time | None, list[int]] | None:
        """The time and the entries ``tag`` should have, when they need writing: a change that failed, or renewal."""
        if (pending := self.pending.get(tag)) is not None:
            at, since = pending
            if dt_util.utcnow() - since <= PENDING_MAX_AGE:
                return at, [] if at is None else web_api.daily_times(now.date(), at)
            del self.pending[tag]  # given up: what the projector has now stands
        entries = [t for t in timers if t.tag == tag]
        at = self._timer_time(timers, tag)
        if at is None:
            return None  # off
        times = [t.time for t in entries if t.time is not None]
        renew_before = web_api.local_unix_seconds(now) + int(TIMER_RENEW_AHEAD.total_seconds())
        tidy = (
            len(times) == len(entries) == len(set(times)) <= web_api.TIMER_DAYS and len(_times_of_day(timers, tag)) == 1
        )
        return None if tidy and max(times) >= renew_before else (at, web_api.daily_times(now.date(), at))

    def _written(self, tag: str, at: dt_time | None, timers: list[web_api.Timer], wanted: list[int]) -> None:
        """Forget a change once the projector has what it wanted (more old entries than one write deletes take
        another, with the next reads)."""
        if any(web_api.timer_changes(timers, tag, wanted)):
            return
        if (pending := self.pending.get(tag)) is not None and pending[0] == at:
            del self.pending[tag]

    def _timer_time(self, timers: list[web_api.Timer], tag: str) -> dt_time | None:
        """A timer's time: its first entry's, as the app reads it, unless its entries also have the time Home
        Assistant last knew (a change that stopped part way: renewing then finishes it rather than undoing it)."""
        known = self.remembered.get(tag)
        if known is not None and known.hour * 60 + known.minute in _times_of_day(timers, tag):
            return known
        return web_api.timer_time(timers, tag)

    # --- what the entities show ----------------------------------------------------------------------------------

    def timer_on(self, tag: str) -> bool | None:
        """Whether a daily timer is on: whether the projector has entries for it (as the app decides)."""
        timers = self.data.timers
        return None if timers is None else any(t.tag == tag for t in timers)

    def timer_time(self, tag: str) -> dt_time | None:
        timers = self.data.timers
        if timers is None:
            return None
        return self._timer_time(timers, tag) or self.remembered.get(tag)

    # --- changes -------------------------------------------------------------------------------------------------

    async def async_set_timer(self, tag: str, on: bool) -> None:
        if not on:
            await self._async_write_timer(tag, None)
            return
        at = self.timer_time(tag)
        if at is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="timer_time_unknown")
        await self._async_write_timer(tag, at)

    async def async_set_timer_time(self, tag: str, at: dt_time) -> None:
        at = at.replace(second=0, microsecond=0)
        if self.timer_on(tag):
            self._check_ready()  # before keeping a time the projector did not get
            self.remembered[tag] = at
            await self._async_write_timer(tag, at)
            return
        self.remembered[tag] = at
        self.async_update_listeners()  # only remembered until the timer is switched on

    async def async_set_poster_resume(self, poster: bool) -> None:
        value = await self._async_change(lambda api: web_api.set_poster_resume(api, poster))
        self.async_set_updated_data(dataclasses.replace(self.data, poster_resume=value))

    async def async_set_poster_duration(self, milliseconds: int) -> None:
        value = await self._async_change(lambda api: web_api.set_poster_duration(api, milliseconds))
        self.async_set_updated_data(dataclasses.replace(self.data, poster_duration=value))

    async def _async_write_timer(self, tag: str, at: dt_time | None) -> None:
        """Switch a timer on at ``at``, or off (None), then read the timers back."""
        self._check_ready()
        self.pending[tag] = (at, dt_util.utcnow())  # until it is done; tried again with the next reads if it fails
        today = dt_util.as_local(dt_util.utcnow()).date()
        wanted = [] if at is None else web_api.daily_times(today, at)

        async def write(api: web_api.Api) -> list[web_api.Timer]:
            await web_api.write_daily_timer(api, tag, wanted)
            return await web_api.get_timers(api)

        timers = await self._async_change(write, self.timer_writes)
        self._written(tag, at, timers, wanted)
        self.async_set_updated_data(dataclasses.replace(self.data, timers=timers))

    def _check_ready(self) -> None:
        if not self.ready(self.main.data):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="settings_not_ready")

    async def _async_change[T](self, op: Callable[[web_api.Api], Awaitable[T]], status: _Status | None = None) -> T:
        """Make a change. Raises HomeAssistantError.

        It leaves the reads' schedule alone; a timer change is counted with the timer writes (``status``), so that
        its retries back off as theirs do. A projector that rejects Home Assistant or is stuck says so here like any
        failure; the polls deal with that.
        """
        self._check_ready()
        async with self._lock:
            self._check_ready()  # again: a refresh may have held the lock for a while
            if status is not None:
                status.last_attempt = dt_util.utcnow()
            try:
                result = await self._async_request(op)
            except _Failed as err:
                detail, link = err.detail, False
            except COMMUNICATION_ERRORS as err:
                detail, link = f"over Bluetooth: {describe_error(err)}", True
            else:
                if status is not None:
                    status.succeeded()
                return result
            if status is not None:  # a Bluetooth link that fails does not count, as for the reads
                self._failed(status, "writing the timers", detail, count=not link)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="settings_failed", translation_placeholders={"error": detail}
        )

    def diagnostics(self) -> dict[str, Any]:
        data = self.data
        return {
            "reads": {name: self.status[name].as_dict() for name in SETTINGS},
            "timer_writes": self.timer_writes.as_dict(),
            "timers": None
            if data.timers is None
            else [
                {"id": t.id, "tag": t.tag, "state": t.state, "time": web_api.wall_clock_text(t.time)}
                for t in data.timers
            ],
            "pending_timers": {
                tag: {"time": None if at is None else at.isoformat(), "since": since.isoformat()}
                for tag, (at, since) in self.pending.items()
            },
            "remembered": {tag: at.isoformat() for tag, at in self.remembered.items()},
            "poster_resume": data.poster_resume,
            "poster_duration_ms": data.poster_duration,
        }


def _writes_due(status: _Status, now: datetime) -> bool:
    """Whether timer writes may run: unless they failed lately (an hour, then daily, whatever worked before)."""
    if not status.failures or status.last_attempt is None:
        return True
    return now - status.last_attempt >= RETRY_DELAYS[min(status.failures, len(RETRY_DELAYS)) - 1]


def _times_of_day(timers: list[web_api.Timer], tag: str) -> set[int]:
    """The times of day of a timer's entries, in minutes."""
    return {t.time % 86400 // 60 for t in timers if t.tag == tag and t.time is not None}


def _iso(when: datetime | None) -> str | None:
    return when.isoformat() if when else None
