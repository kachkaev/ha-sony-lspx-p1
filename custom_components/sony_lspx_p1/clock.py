"""Keeps the projector's clock in sync, through the Bluetooth tunnel or else over its Wi-Fi network (issue #1).

The clock posters show the projector's own clock, which only the smartphone app sets: over HTTP, with the local
wall-clock time (no time zone), so without the app it drifts and stays an hour out after a daylight-saving change.
This sends Home Assistant's local time the same way, through the Bluetooth tunnel to the projector's HTTP API
(tunnel.py), as the app does on its Bluetooth-only path: POST /v1/settings/time with {"time": N} alone. Seen once to
set the clock on one projector (firmware 3.1.15, 2026-11-24), which then reported time zone "GMT+0"; the time zone is
never sent this way (the app does not, and it was never tried), so a projector holding another one might show the
time shifted by it (unknown).

When the tunnel is missing or refuses (not seen), the time goes over Wi-Fi as before, if the projector is on the home
Wi-Fi: it asks the projector over Bluetooth where its HTTP API is (GetHttpEndpoint, shared with the settings over
Wi-Fi) and posts the time there, with "timezone": "GMT+0" as the app does over Wi-Fi (also never seen to work).

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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from . import tunnel, web_api
from .client import NotPairedError, ProjectorBusyError, describe_error
from .const import DOMAIN
from .coordinator import COMMUNICATION_ERRORS, LSPXCoordinator
from .protocol import CLOCK_POSTERS, OWN_NETWORK_TOPOLOGIES, AppState, HttpEndpoint, ProjectorState

_LOGGER = logging.getLogger(__name__)

# The app sets the clock at most once an hour while it is open. Once a day is plenty for drift, because a change
# of Home Assistant's UTC offset (daylight saving, another time zone) is noticed on the next poll anyway.
SYNC_INTERVAL = timedelta(days=1)
# After failed syncs in a row, automatic attempts wait this long: soon after the first, then hourly. Choosing a clock
# poster in Home Assistant and the Sync clock button always try at once.
RETRY_DELAYS = (timedelta(minutes=5), timedelta(hours=1))
ROUTE_BLUETOOTH = "bluetooth"
ROUTE_WIFI = "wifi"


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
        self.endpoint: HttpEndpoint | None = None  # read only when the tunnel failed
        self.route: str | None = None  # how the clock was last set
        self.last_attempt: datetime | None = None
        self.last_success: datetime | None = None
        self.last_error: str | None = None
        self.sent_time: int | None = None  # the local time last sent, as Unix seconds
        self.reply: web_api.ProjectorTime | None = None  # what the projector answered to it

    def diagnostics(self) -> dict[str, Any]:
        endpoint, reply, state = self.endpoint, self.reply, self._coordinator.data
        return {
            "route": self.route,
            "net_topology": None if state is None else state.net_topology,  # from the last poll
            "endpoint": None
            if endpoint is None
            else {"address": endpoint.address, "port": endpoint.port, "direct_address": endpoint.direct},
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
    def async_poll_done(self, state: ProjectorState) -> None:
        """Called by the coordinator after each successful poll.

        Automatic syncs also wait for a poster (the only thing shown when the tunnel was tried): not over HDMI,
        mirroring or the other apps. A clock poster chosen or seen meanwhile is kept for the first poll that can.
        """
        showing = bool(state.power and state.app_state == AppState.POSTER and state.poster in CLOCK_POSTERS)
        self._chosen = self._chosen or (showing and not self._was_showing)
        self._was_showing = showing
        if not self.ready(state) or state.app_state != AppState.POSTER:
            return
        appeared, self._chosen = self._chosen, False
        if appeared or self._due():
            self._async_start_sync(state)

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
    def _async_start_sync(self, state: ProjectorState) -> None:
        if self._task is None or self._task.done():
            self._task = self._coordinator.config_entry.async_create_background_task(
                self._hass, self._async_sync_quietly(state), f"{DOMAIN} clock sync"
            )

    async def _async_sync_quietly(self, state: ProjectorState) -> None:
        try:
            await self.async_sync(state)
        except (ClockError, *COMMUNICATION_ERRORS):
            _LOGGER.debug("%s: setting the clock failed: %s", self._coordinator.client.address, self.last_error)

    async def async_sync(self, state: ProjectorState | None = None) -> None:
        """Set the projector's clock now. Raises ClockError, or COMMUNICATION_ERRORS (the Bluetooth link).

        ``state``: what the poll that started it read. Without it (Sync clock), the coordinator's, and nothing is
        sent unless the projector is ready (HomeAssistantError ``clock_not_ready``); in any app state, though.
        """
        if state is None:
            state = self._coordinator.data
            if not self.ready(state):
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="clock_not_ready")
        async with self._lock:
            self.last_attempt = dt_util.utcnow()
            try:
                await self._async_sync(state)
            except ClockError as err:
                self._failed(err.detail)
                raise
            except COMMUNICATION_ERRORS as err:
                self._failed(f"over Bluetooth: {describe_error(err)}")
                raise

    def _failed(self, detail: str) -> None:
        self._failures += 1
        self.last_error = detail

    async def _async_sync(self, state: ProjectorState | None) -> None:
        sent: list[datetime] = []

        def body() -> str:
            # Called once the link is ours, so that waiting for it (a source switch, say) does not age the time.
            sent.append(dt_util.as_local(dt_util.utcnow()))  # in Home Assistant's time zone
            return json.dumps({"time": web_api.local_unix_seconds(sent[-1])}, separators=(",", ":"))

        try:
            # As the app's BleWebApiPassThroughIfs.setTime: {"time": N}, no time zone.
            response = await self._coordinator.async_web_request(tunnel.Method.POST, web_api.TIME_PATH, body)
            reply = web_api.projector_time(web_api.parse_reply(response.http_status, response.text()))
            route = ROUTE_BLUETOOTH
        except (tunnel.TunnelError, web_api.WebApiError) as err:
            # The projector answered, but not with the time: try Wi-Fi. (A Bluetooth link that fails is raised: the
            # Wi-Fi route needs it too, to read the address.)
            over_bluetooth = f"over Bluetooth: {_describe(err)}"
            _LOGGER.debug("%s: setting the clock %s; trying Wi-Fi", self._coordinator.client.address, over_bluetooth)
            try:
                reply = await self._set_over_wifi(state, sent)
            except ClockError as wifi_err:
                raise ClockError(f"{over_bluetooth}; {wifi_err.detail}") from err
            route = ROUTE_WIFI
        now = sent[-1]
        self.sent_time, self.reply, self.route = web_api.local_unix_seconds(now), reply, route
        self.last_success, self.last_error, self._failures = dt_util.utcnow(), None, 0
        self._synced_offset = now.utcoffset()
        _LOGGER.debug(
            "%s: set the clock to %s over %s (reply: %s)", self._coordinator.client.address, now, route, reply
        )

    async def _set_over_wifi(self, state: ProjectorState | None, sent: list[datetime]) -> web_api.ProjectorTime:
        """POST the time over Wi-Fi, to the address the projector reports over Bluetooth. Raises ClockError.

        ``state`` is what the poll that started the sync read (the coordinator may not hold it yet), or the last.
        The time sent is appended to ``sent``. A projector that rejects Home Assistant or is stuck raises as for any
        command (NotPairedError, ProjectorBusyError).
        """
        topology = None if state is None else state.net_topology
        if topology in OWN_NETWORK_TOPOLOGIES:  # Wi-Fi Direct or wireless HDMI: no need to ask for the address
            raise ClockError(f"not over Wi-Fi: on its own network, not the home one (topology {topology})")
        try:
            endpoint = await self._coordinator.async_http_endpoint()
        except (NotPairedError, ProjectorBusyError):
            raise
        except COMMUNICATION_ERRORS as err:
            raise ClockError(f"not over Wi-Fi: reading the endpoint over Bluetooth: {describe_error(err)}") from err
        self.endpoint = endpoint
        if endpoint.host is None:
            raise ClockError(f"not over Wi-Fi: not on Wi-Fi (endpoint address {endpoint.address!r})")
        host = endpoint.home_host(topology)
        if host is None:
            # Maybe left over from its own network: read the address again next time, as the app does.
            self._coordinator.forget_http_endpoint()
            raise ClockError(
                f"not over Wi-Fi: on its own network, not the home one (endpoint address {endpoint.address!r})"
            )
        sent.append(dt_util.as_local(dt_util.utcnow()))
        try:
            return await web_api.set_time(
                async_get_clientsession(self._hass), host, web_api.local_unix_seconds(sent[-1])
            )
        except web_api.WebApiError as err:
            self._coordinator.forget_http_endpoint()
            raise ClockError(f"over Wi-Fi: {web_api.base_url(host)}: {err}") from err


def _describe(err: Exception) -> str:
    return str(err) or type(err).__name__


def _iso(when: datetime | None) -> str | None:
    return when.isoformat() if when else None


def _wall_clock(seconds: int | None) -> str | None:
    """A time as the projector holds it (wall-clock time as Unix seconds), as text without a time zone."""
    return web_api.wall_clock_text(seconds)
