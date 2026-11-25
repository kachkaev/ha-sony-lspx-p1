"""Keeps the projector's clock in sync, through the Bluetooth tunnel (issue #1).

The clock posters show the projector's own clock, which only the smartphone app sets: over HTTP, with the local
wall-clock time (no time zone), so without the app it drifts and stays an hour out after a daylight-saving change.
This sends Home Assistant's local time the same way, through the Bluetooth tunnel to the projector's HTTP API
(tunnel.py), as the app does on its Bluetooth-only path: POST /v1/settings/time with {"time": N} alone. Seen once to
set the clock on one projector (firmware 3.1.15, 2026-11-24), which then reported time zone "GMT+0"; the time zone is
never sent this way (the app does not, and it was never tried), so a projector holding another one might show the
time shifted by it (unknown).

When the tunnel is missing or refuses (not seen), setting the clock fails with the tunnel's reason; there is no other
way (issue #66).

Nothing here may disturb the rest of the integration: syncs run in the background right after a poll (so the
Bluetooth link is still open), only while the projector is on with its main board up, and failures are only logged at
debug level. Only the Sync clock button reports errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from . import tunnel, web_api
from .client import describe_error
from .const import DOMAIN
from .coordinator import COMMUNICATION_ERRORS, LSPXCoordinator
from .protocol import CLOCK_POSTERS, AppState, ProjectorState

_LOGGER = logging.getLogger(__name__)

# The app sets the clock at most once an hour while it is open. Once a day is plenty for drift, because a change
# of Home Assistant's UTC offset (daylight saving, another time zone) is noticed on the next poll anyway.
SYNC_INTERVAL = timedelta(days=1)
# After failed syncs in a row, automatic attempts wait this long: soon after the first, then hourly. Choosing a clock
# poster in Home Assistant and the Sync clock button always try at once.
RETRY_DELAYS = (timedelta(minutes=5), timedelta(hours=1))


class ClockError(HomeAssistantError):
    """Setting the clock failed; ``detail`` says why, in English, for diagnostics and the log."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            translation_domain=DOMAIN, translation_key="clock_sync_failed", translation_placeholders={"error": detail}
        )
        self.detail = detail


class ClockSync:
    """Sets the projector's clock: when a clock poster appears, once a day and when the UTC offset changes.

    Automatic syncs only start after a successful poll that finds the projector on and showing a poster, with its
    main board up: the tunnel's HTTP API runs on the main board, and was only tried then.
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
        self.last_attempt: datetime | None = None
        self.last_success: datetime | None = None
        self.last_error: str | None = None
        self.sent_time: int | None = None  # the local time last sent, as Unix seconds
        self.reply: web_api.ProjectorTime | None = None  # what the projector answered to it

    def diagnostics(self) -> dict[str, Any]:
        reply = self.reply
        return {
            "last_attempt": _iso(self.last_attempt),
            "last_success": _iso(self.last_success),
            "last_error": self.last_error,
            "sent_time": _wall_clock(self.sent_time),
            "projector_reply": None if reply is None else {"time": _wall_clock(reply.time), "timezone": reply.timezone},
        }

    @callback
    def poster_chosen(self) -> None:
        """A clock poster was chosen in Home Assistant: sync after the next poll that can, as the app does."""
        self._chosen = True

    def ready(self, state: ProjectorState | None) -> bool:
        """Whether the projector is on with its main board up (answering what it shows, not starting up).

        The tunnel's HTTP API runs on the main board; it was never tried in standby, asleep or starting up.
        """
        return bool(state and state.power and state.app_state is not None and not self._coordinator.starting_up)

    @callback
    def async_poll_done(self, state: ProjectorState, *, left_by_redraw: bool = False) -> None:
        """Called by the coordinator after each successful poll.

        Automatic syncs also wait for a poster (the only thing shown when the tunnel was tried): not over HDMI,
        mirroring or the other apps. A clock poster chosen or seen meanwhile is kept for the first poll that can.
        ``left_by_redraw``: the clock poster shown is the one a failed redraw of the Weather poster left up, so it
        did not appear (it is noted as shown, so it does not count as appearing later either).
        """
        showing = bool(state.power and state.app_state == AppState.POSTER and state.poster in CLOCK_POSTERS)
        self._chosen = self._chosen or (showing and not self._was_showing and not left_by_redraw)
        self._was_showing = showing
        if not self.ready(state) or state.app_state != AppState.POSTER:
            return
        appeared, self._chosen = self._chosen, False
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
            await self.async_sync(check_ready=False)
        except (ClockError, *COMMUNICATION_ERRORS):
            _LOGGER.debug("%s: setting the clock failed: %s", self._coordinator.client.address, self.last_error)

    async def async_sync(self, *, check_ready: bool = True) -> None:
        """Set the projector's clock now. Raises ClockError, or COMMUNICATION_ERRORS (the Bluetooth link).

        Nothing is sent unless the projector is ready by the coordinator's last state (HomeAssistantError
        ``clock_not_ready``), in any app state. ``check_ready`` is off for the sync a poll starts, which checked what
        it read (the coordinator may not hold that yet).
        """
        if check_ready and not self.ready(self._coordinator.data):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="clock_not_ready")
        async with self._lock:
            self.last_attempt = dt_util.utcnow()
            try:
                await self._async_sync()
            except ClockError as err:
                self._failed(err.detail)
                raise
            except COMMUNICATION_ERRORS as err:
                self._failed(f"over Bluetooth: {describe_error(err)}")
                raise

    def _failed(self, detail: str) -> None:
        self._failures += 1
        self.last_error = detail

    async def _async_sync(self) -> None:
        sent: list[datetime] = []

        def body() -> str:
            # Called once the link is ours, so that waiting for it (a source switch, say) does not age the time.
            sent.append(dt_util.as_local(dt_util.utcnow()))  # in Home Assistant's time zone
            return json.dumps({"time": web_api.local_unix_seconds(sent[-1])}, separators=(",", ":"))

        try:
            # As the app's BleWebApiPassThroughIfs.setTime: {"time": N}, no time zone.
            response = await self._coordinator.async_web_request(tunnel.Method.POST, web_api.TIME_PATH, body)
            reply = web_api.projector_time(web_api.parse_reply(response.http_status, response.text()))
        except (tunnel.TunnelError, web_api.WebApiError) as err:
            # The projector answered, but not with the time. (A Bluetooth link that fails is raised.)
            raise ClockError(f"over Bluetooth: {_describe(err)}") from err
        now = sent[-1]
        self.sent_time, self.reply = web_api.local_unix_seconds(now), reply
        self.last_success, self.last_error, self._failures = dt_util.utcnow(), None, 0
        self._synced_offset = now.utcoffset()
        _LOGGER.debug("%s: set the clock to %s (reply: %s)", self._coordinator.client.address, now, reply)


def _describe(err: Exception) -> str:
    return str(err) or type(err).__name__


def _iso(when: datetime | None) -> str | None:
    return when.isoformat() if when else None


def _wall_clock(seconds: int | None) -> str | None:
    """A time as the projector holds it (wall-clock time as Unix seconds), as text without a time zone."""
    return web_api.wall_clock_text(seconds)
