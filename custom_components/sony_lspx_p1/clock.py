"""Keeps the projector's clock in sync over its Wi-Fi network (experimental, issue #1).

The clock posters show the projector's own clock, which only the smartphone app sets: over HTTP, with the local
wall-clock time (no time zone), so without the app it drifts and stays an hour out after a daylight-saving change.
This sends Home Assistant's local time the same way: it asks the projector over Bluetooth where its HTTP API is
(GetHttpEndpoint) and posts the time there, so it only works while the projector is on the home Wi-Fi.

Nothing here may disturb the rest of the integration: syncs run in the background right after a poll (so the
Bluetooth link is still open for the one short read they need), the HTTP request holds no lock, and failures are
only logged at debug level, since many projectors are never on Wi-Fi. Only the Sync clock button reports errors.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from . import web_api
from .client import describe_error
from .const import DOMAIN
from .coordinator import COMMUNICATION_ERRORS, LSPXCoordinator
from .protocol import CLOCK_POSTERS, AppState, HttpEndpoint, NetTopology, ProjectorState

_LOGGER = logging.getLogger(__name__)

# The app sets the clock at most once an hour while it is open. Once a day is plenty for drift, because a change
# of Home Assistant's UTC offset (daylight saving, another time zone) is noticed on the next poll anyway.
SYNC_INTERVAL = timedelta(days=1)
# After failed syncs in a row (typically: not on Wi-Fi), automatic attempts wait this long: soon after the first
# (Wi-Fi may come up a little after switching on), then hourly. Choosing a clock poster in Home Assistant and the
# Sync clock button always try at once.
RETRY_DELAYS = (timedelta(minutes=5), timedelta(hours=1))


class ClockError(HomeAssistantError):
    """Setting the clock failed; ``detail`` says why, in English, for diagnostics and the log."""

    def __init__(self, detail: str, translation_key: str, placeholders: dict[str, str] | None = None) -> None:
        super().__init__(
            translation_domain=DOMAIN, translation_key=translation_key, translation_placeholders=placeholders
        )
        self.detail = detail


class ClockSync:
    """Sets the projector's clock: when a clock poster appears, once a day and when the UTC offset changes.

    Automatic syncs only start after a successful poll that finds the projector on and not showing wireless
    HDMI (its Wi-Fi then talks to the HDMI unit, not to the home network).
    """

    def __init__(self, hass: HomeAssistant, coordinator: LSPXCoordinator) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._lock = asyncio.Lock()  # one sync at a time
        self._task: asyncio.Task[None] | None = None
        self._was_showing = False
        self._chosen = False
        self._failures = 0  # failed syncs in a row
        self._synced_offset: timedelta | None = None  # Home Assistant's UTC offset at the last sync
        self.endpoint: HttpEndpoint | None = None
        self.last_attempt: datetime | None = None
        self.last_success: datetime | None = None
        self.last_error: str | None = None
        self.sent_time: int | None = None  # the local time last sent, as Unix seconds
        self.reply: web_api.ProjectorTime | None = None  # what the projector answered to it

    def diagnostics(self) -> dict[str, Any]:
        endpoint, reply = self.endpoint, self.reply
        return {
            "endpoint": None if endpoint is None else {"address": endpoint.address, "port": endpoint.port},
            "last_attempt": _iso(self.last_attempt),
            "last_success": _iso(self.last_success),
            "last_error": self.last_error,
            "sent_time": _wall_clock(self.sent_time),
            "projector_reply": None if reply is None else {"time": _wall_clock(reply.time), "timezone": reply.timezone},
        }

    @callback
    def poster_chosen(self) -> None:
        """A clock poster was chosen in Home Assistant: sync after the next poll, as the app does."""
        self._chosen = True

    @callback
    def async_poll_done(self, state: ProjectorState) -> None:
        """Called by the coordinator after each successful poll."""
        showing = bool(state.power and state.app_state == AppState.POSTER and state.poster in CLOCK_POSTERS)
        appeared = self._chosen or (showing and not self._was_showing)
        self._was_showing, self._chosen = showing, False
        if not state.power or state.net_topology == NetTopology.WIRELESS_HDMI:
            return
        if appeared or self._due():
            self._async_start_sync()

    def _due(self) -> bool:
        now = dt_util.utcnow()
        if self._failures and self.last_attempt is not None:
            delay = RETRY_DELAYS[min(self._failures, len(RETRY_DELAYS)) - 1]
            if now - self.last_attempt < delay:
                return False
        return (
            self.last_success is None
            or now - self.last_success >= SYNC_INTERVAL
            or dt_util.as_local(now).utcoffset() != self._synced_offset
        )

    @callback
    def _async_start_sync(self) -> None:
        if self._task is None or self._task.done():
            self._task = self._coordinator.config_entry.async_create_background_task(
                self._hass, self._async_sync_quietly(), f"{DOMAIN} clock sync"
            )

    async def _async_sync_quietly(self) -> None:
        try:
            await self.async_sync()
        except (ClockError, *COMMUNICATION_ERRORS):
            _LOGGER.debug("%s: setting the clock failed: %s", self._coordinator.client.address, self.last_error)

    async def async_sync(self) -> None:
        """Set the projector's clock now. Raises ClockError or COMMUNICATION_ERRORS (reading the endpoint)."""
        async with self._lock:
            self.last_attempt = dt_util.utcnow()
            try:
                await self._async_sync()
            except ClockError as err:
                self._failed(err.detail)
                raise
            except COMMUNICATION_ERRORS as err:
                self._failed(f"reading the endpoint over Bluetooth: {describe_error(err)}")
                raise

    def _failed(self, detail: str) -> None:
        self._failures += 1
        self.last_error = detail

    async def _async_sync(self) -> None:
        endpoint = await self._coordinator.async_read_http_endpoint()
        self.endpoint = endpoint
        host = endpoint.host
        if host is None:
            raise ClockError(f"not on Wi-Fi (endpoint address {endpoint.address!r})", "clock_not_on_wifi")
        now = dt_util.as_local(dt_util.utcnow())  # in Home Assistant's time zone
        local = web_api.local_unix_seconds(now)
        try:
            reply = await web_api.set_time(async_get_clientsession(self._hass), host, local)
        except web_api.WebApiError as err:
            raise ClockError(
                f"{web_api.base_url(host)}: {err}", "clock_sync_failed", {"host": host, "error": str(err)}
            ) from err
        self.sent_time, self.reply = local, reply
        self.last_success, self.last_error, self._failures = dt_util.utcnow(), None, 0
        self._synced_offset = now.utcoffset()
        _LOGGER.debug("%s: set the clock to %s at %s (reply: %s)", self._coordinator.client.address, now, host, reply)


def _iso(when: datetime | None) -> str | None:
    return when.isoformat() if when else None


def _wall_clock(seconds: int | None) -> str | None:
    """A time as the projector holds it (wall-clock time as Unix seconds), as text without a time zone."""
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None).isoformat()
    except (OverflowError, OSError, ValueError):
        return str(seconds)
