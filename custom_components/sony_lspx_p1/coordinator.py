"""Data update coordinator for the Sony LSPX-P1."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import tunnel
from .client import (
    POLL_LINGER,
    REDRAW_POSTER,
    ControllerNotFoundError,
    LSPXClient,
    LSPXError,
    NotPairedError,
    OwnControllerError,
    OwnControllerUnknownError,
    ProjectorBusyError,
    RedrawIncompleteError,
    RedrawStoppedError,
    describe_error,
    main_board_starting,
)
from .const import DOMAIN, POWER_SETTLE_REFRESH_DELAYS, UPDATE_INTERVAL
from .protocol import (
    POSTER_NAMES,
    AppState,
    BondCommandFailed,
    BondOp,
    CommandRejected,
    Controller,
    ErrorCode,
    HttpEndpoint,
    Notification,
    NotifyOp,
    Op,
    Poster,
    ProjectorState,
    ProtocolError,
    decode_u8,
)

if TYPE_CHECKING:
    from .clock import ClockSync
    from .weather import WeatherPoster
    from .web_settings import WebSettingsCoordinator

_LOGGER = logging.getLogger(__name__)

type LSPXConfigEntry = ConfigEntry[LSPXCoordinator]

# Switched on after a while in standby, the projector's main board starts up again: until it is up, every command it
# handles (the source, HDMI status, firmware versions, the Wi-Fi endpoint) answers MAIN_BOARD_STARTING, while the
# system controller's (power, volume, picture...) work. On one projector (firmware 3.1.15) that lasted 34-48 s and
# 39-46 s after the power-on was acknowledged after half an hour in standby, 32-96 s after days; after six minutes
# there was none. What decides it is unknown, so every power-on counts as a start-up until the main board answers;
# after Restart (tried twice: the board back about 46-50 s after the acknowledgement, once then 13 s of app state 0)
# until it says what it shows.
# Commands it rejects wait for it, for this long at most since the start-up was noticed: 96 s with a margin. Waiting
# ends as soon as the board is up, so the margin costs nothing then.
STARTUP_TIMEOUT = 120.0
# Meanwhile one read the main board handles (GetAppState) is sent this often, on a link dropped soon after each, so
# the smartphone app and proxies still get their turn; once the board is up, a poll fills in what it reports.
STARTUP_PROBE_INTERVAL = 10.0
# Right after switching power the projector may also answer UNKNOWN_COMMAND for a moment, to any command. Those are
# retried every WARMUP_RETRY_DELAY for this long after the start-up began.
POWER_ON_WARMUP = 30.0
WARMUP_RETRY_DELAY = 1.0
# A source chosen in standby is applied once a poll sees the projector on and its main board up, if that happens
# this soon.
PENDING_SOURCE_TIMEOUT = STARTUP_TIMEOUT + 30.0
# A stuck internal link makes every poll time out until RESET is pressed. A few busy polls in a row
# (about five minutes) are not just a slow start-up, so a repair issue then explains the button and offers Restart.
STUCK_POLLS_BEFORE_ISSUE = 5
# The projector rejects a controller it does not know (ACCESS_DENIED twice, NotPairedError), and also one that reaches
# it through a Bluetooth adapter or proxy without the pairing: Home Assistant picks one for each connection attempt,
# and the next may go through the one that holds it. So pairing again is asked for (reauthentication, which stops the
# polls) only after this many rejected connections with no successful poll in between (other failed polls do not
# count, nor do commands, some of which go over Wi-Fi).
REJECTIONS_BEFORE_REAUTH = 2
# The HTTP endpoint (the projector's address on its Wi-Fi network) read over Bluetooth is reused for this long, by
# the clock and the settings over Wi-Fi alike. It is read again sooner after switching power, a change of network
# topology or a failed HTTP request.
HTTP_ENDPOINT_MAX_AGE = 3600.0
# Change notifications that make a poll worthwhile (power, and what the projector shows); others are only logged.
REFRESH_NOTIFICATIONS = frozenset({NotifyOp.POWER, NotifyOp.NET_TOPOLOGY, NotifyOp.APP_STATE})

type Apply = Callable[[ProjectorState, Any], None]
type SourceCall = tuple[Callable[[], Awaitable[Any]], Apply]

# Polls kept for diagnostics: ten minutes at the usual interval.
RECENT_POLLS = 10

# No redraw left the poster in between up (see LSPXCoordinator._left_on_redraw_poster).
_NOTHING_LEFT = object()

COMMUNICATION_ERRORS = (LSPXError, CommandRejected, BondCommandFailed, ProtocolError, BleakError, TimeoutError)


class _Dropped(Exception):
    """A source choice not sent yet: replaced by a later one, or the projector went to standby."""


class _InStandby(_Dropped):
    """The projector went to standby before the source chosen was sent."""


@dataclass
class _StartUp:
    """The projector is on and its main board may still be starting up."""

    began: float  # event loop time
    deadline: float
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    # A main-board command answered MAIN_BOARD_STARTING: not a start-up that was over at once (a warm one).
    confirmed: bool = False
    # Begun by Restart: the board is up only once it says what it shows (not app state 0).
    after_restart: bool = False
    # Ended because the projector went to standby.
    in_standby: bool = False
    probe: asyncio.Task[None] | None = None


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
        self._startup: _StartUp | None = None
        # UNKNOWN_COMMAND is retried until then (POWER_ON_WARMUP after a start-up began), whether or not it is over.
        self._warmup_until = 0.0
        # The last start-up ended without the main board answering: MAIN_BOARD_STARTING does not start another
        # until power goes off and on, Restart or the board answers, so a board that never comes up is not waited for
        # forever.
        self._startup_spent = False
        # When the last start-up ended because the main board answered (event loop time).
        self._board_up_at = float("-inf")
        # A source switch is several commands; a power change must not land between them. The media player's actions
        # may run at the same time (PARALLEL_UPDATES = 0, so a later source can replace one waiting for a start-up);
        # this also covers a source chosen in standby, which a poll applies once the projector is on.
        self._switching = asyncio.Lock()
        # The latest source chosen (see async_select_source).
        self._source_choice: object | None = None
        # Polls in a row that timed out with 0xC0. Other failures (out of range, connection errors) say
        # nothing about the link and leave it as is; only a successful poll resets it.
        self._busy_polls = 0
        # Connections in a row the projector rejected (see REJECTIONS_BEFORE_REAUTH).
        self._rejections = 0
        self._stuck_issue_id = f"projector_stuck_{entry.entry_id}"
        # When a poll last read the power (event loop time): the projector answered then. Commands that succeed
        # say less, as the bonding service answers apart from the projector controls.
        self.power_read_at: float | None = None
        # Keeps the Weather poster in sync with a weather entity, when one is chosen in the options.
        self.weather: WeatherPoster | None = None
        # Keeps the projector's clock in sync, through the Bluetooth tunnel or else over its Wi-Fi network.
        self.clock: ClockSync | None = None
        # Settings read and written over the projector's Wi-Fi network (experimental, entities disabled by default).
        self.web_settings: WebSettingsCoordinator | None = None
        # The last endpoint read that had an address, and when (event loop time).
        self._http_endpoint: HttpEndpoint | None = None
        self._http_endpoint_read_at = 0.0
        self._http_endpoint_lock = asyncio.Lock()
        # The last frame of each kind of notification since a poll last saw power or the source change, so that a
        # projector repeating itself cannot set off poll after poll.
        self._notified: dict[int, tuple[int, bytes]] = {}
        # A poll is about twenty commands, and one from an entity may run between any two of them. What such a
        # command changed is replayed onto the poll's result, which may hold the value read before it: a poll must
        # not undo a command that finished while it ran (power above all, which the card's source chips follow).
        # The log is kept only while polls are in flight. Known limit: a field the poll read after the command is
        # replaced by the command's read-back too, so a change made elsewhere in between shows at the next poll.
        self._polls_in_flight = 0
        self._applied: list[tuple[Apply, Any]] = []
        # How the last polls went, for diagnostics.
        self.recent_polls: deque[dict[str, Any]] = deque(maxlen=RECENT_POLLS)
        # Start-ups over whose last poll is still running (see _refresh_ending_startup).
        self._startups_ending = 0
        # The Weather poster is being redrawn (async_set_weather): a poll that read the poster shown in between shows
        # the Weather poster instead, so the source does not flicker.
        self._redrawing = False
        # A redraw that failed may have left the poster in between up: the source choice it failed under, to put the
        # Weather poster back while no other source is chosen (_restore_after_redraw).
        self._left_on_redraw_poster: object | None = _NOTHING_LEFT
        self._restoring: asyncio.Task[None] | None = None
        client.set_notification_callback(self._async_on_notification)

    async def _async_update_data(self) -> ProjectorState:
        self._polls_in_flight += 1
        start = len(self._applied)
        began = asyncio.get_running_loop().time()
        try:
            state = await self._async_read_state()
            # On what the projector said, not on what a command made of it: one just switched on is starting up.
            # While its main board is, the source waits for the poll made once the board is up.
            board_starting = (
                (state.app_state is None and (state.main_board_starting or self._startup is not None))
                or self._shows_nothing_yet(state.app_state)
                or self._began_since(began)
            )
            if (pending := self._pending_source) is not None and state.power and not board_starting:
                await self._apply_pending_source(pending)
            # In the order they finished, the pending source included.
            for apply, result in self._applied[start:]:
                apply(state, result)
            if self._redrawing and state.power and state.app_state == AppState.POSTER and state.poster == REDRAW_POSTER:
                state.poster = Poster.WEATHER
        except Exception as err:
            self._record_poll(began, err)
            raise
        finally:
            self._polls_in_flight -= 1
            if not self._polls_in_flight:
                self._applied.clear()
        previous = self.data
        was_starting_up = self.shows_starting_up
        self._track_startup(state, previous, began)
        if self.shows_starting_up != was_starting_up:
            # Once this poll's state is stored: the base class writes the entities only if the state changed.
            self.hass.loop.call_soon(self.async_update_listeners)
        if not state.power or previous is None or state.net_topology != previous.net_topology:
            self.forget_http_endpoint()
        if previous is None or (state.power, state.app_state, state.net_topology) != (
            previous.power,
            previous.app_state,
            previous.net_topology,
        ):
            self._notified.clear()
        if self.clock is not None:
            self.clock.async_poll_done(state)  # only starts a background task, if any
        if self.web_settings is not None:
            self.web_settings.async_poll_done(state)  # likewise
        self._restore_after_redraw(state)  # likewise
        self._record_poll(began, None)
        return state

    def _record_poll(self, began: float, err: Exception | None) -> None:
        """Remember how long the poll took and how it ended, for diagnostics (Home Assistant logs it too)."""
        cause = err.__cause__ if isinstance(err, UpdateFailed) and isinstance(err.__cause__, Exception) else err
        self.recent_polls.append(
            {
                "at": dt_util.utcnow().isoformat(timespec="seconds"),
                "seconds": round(asyncio.get_running_loop().time() - began, 2),
                "result": "ok" if cause is None else describe_error(cause),
            }
        )

    async def _async_read_state(self) -> ProjectorState:
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.client.address, connectable=True)
        if ble_device is None:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="device_not_found")
        self.client.set_ble_device(ble_device)
        try:
            # A start-up is bounded (STARTUP_TIMEOUT): a power read that keeps being rejected fails polls after it.
            state = await self.client.read_state(self.data, starting_up=self._startup is not None)
        except NotPairedError as err:
            if self._rejected():
                raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="not_paired") from err
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="not_accepted") from err
        except ProjectorBusyError as err:
            self._busy_polls += 1
            if self._busy_polls == STUCK_POLLS_BEFORE_ISSUE:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    self._stuck_issue_id,
                    is_fixable=True,  # by trying Restart (repairs.py), with RESET as the fallback
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="projector_stuck",
                    translation_placeholders={"name": self.config_entry.title},
                    data={"entry_id": self.config_entry.entry_id},
                )
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="projector_busy") from err
        except COMMUNICATION_ERRORS as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="communication_error",
                translation_placeholders={"error": str(err) or type(err).__name__},
            ) from err
        self._rejections = 0
        # Not when the power read failed and the poll kept the last one (starting up): that is no sign of an answer.
        if "power" not in state.errors:
            self.power_read_at = asyncio.get_running_loop().time()
            if self._busy_polls:
                self._busy_polls = 0
                ir.async_delete_issue(self.hass, DOMAIN, self._stuck_issue_id)
        return state

    async def _apply_pending_source(self, pending: tuple[SourceCall, float]) -> None:
        """Switch to the source chosen in standby, from a poll, which replays the result onto what it read."""
        (func, apply), deadline = pending
        if asyncio.get_running_loop().time() > deadline:
            self._pending_source = None
            return
        try:
            async with self._switching:
                if self._pending_source is not pending:
                    return  # another source chosen or power switched off since, while waiting for the lock
                self._pending_source = None
                result = await self._through_warmup(func)
        except COMMUNICATION_ERRORS as err:
            _LOGGER.warning("%s: switching the source after power-on failed: %s", self.client.address, err)
            return
        self._applied.append((apply, result))
        self._schedule_settle_refreshes()

    async def async_select_source(
        self, func: Callable[[], Awaitable[Any]], apply: Callable[[ProjectorState, Any], None]
    ) -> None:
        """Switch what the projector shows; from standby, switch it on first (as the app does).

        Only the latest choice is sent: one still waiting (for the main board to start up, or for a switch in
        progress) when another is made is dropped, and its call returns without error. One already sent finishes
        first.
        """
        choice = self._source_choice = object()
        if self.data is not None and self.data.power is False:
            deadline = asyncio.get_running_loop().time() + PENDING_SOURCE_TIMEOUT
            pending = self._pending_source = ((func, apply), deadline)
            try:
                # Not again if another action switched it on meanwhile (a choice or turn_on made at the same time).
                await self.async_set_power(True, unless_on=True)
            except BaseException:
                # Not to be switched to once the projector is on some other way. A power-on that timed out may
                # still switch it on; the user was told it failed, so the choice is dropped all the same.
                if self._pending_source is pending:
                    self._pending_source = None
                raise
            return
        # A start-up known to be a cold one: the main board would reject the switch, or, after Restart while it
        # answers app state 0, may take it and then go back to what it showed before. Sent once it is up, without
        # holding the lock meanwhile, so Restart and the background updates are not held up; dropped if the
        # projector goes to standby meanwhile. (One not known yet to be cold waits after a rejection, in
        # async_command, likewise without the lock.)
        self._pending_source = None  # the latest choice wins over one made in standby, which a poll may apply meanwhile
        while (startup := self._startup) is not None and startup.confirmed:
            self.client.linger_at_most(POLL_LINGER)
            await startup.ended.wait()
            if self._source_choice is not choice:
                _LOGGER.debug(
                    "%s: source choice dropped before it was sent: another source chosen since", self.client.address
                )
                return
            if startup.in_standby:
                self._warn_source_dropped_in_standby()
                return

        async def send() -> Any:
            # Before each attempt, holding the lock: also the one after waiting for the main board in async_command.
            if self._source_choice is not choice:
                raise _Dropped("another source chosen since")
            if self.data is not None and self.data.power is False:
                raise _InStandby  # sent now, the switch would switch it on
            return await func()

        try:
            await self.async_command(send, apply, lock=self._switching)
        except _InStandby:
            self._warn_source_dropped_in_standby()
            return
        except _Dropped as err:
            _LOGGER.debug("%s: source choice dropped before it was sent: %s", self.client.address, err)
            return
        self._schedule_settle_refreshes()

    def _warn_source_dropped_in_standby(self) -> None:
        """The latest source chosen, not sent yet when the projector went (or is being switched) to standby."""
        _LOGGER.warning("%s: switched to standby before it was sent: the source chosen is dropped", self.client.address)

    async def async_command(
        self,
        func: Callable[[], Awaitable[Any]],
        apply: Callable[[ProjectorState, Any], None] | None = None,
        *,
        before_apply: Callable[[], None] | None = None,
        lock: asyncio.Lock | None = None,
    ) -> None:
        """Run a client call from an entity and merge its read-back into the state.

        ``before_apply`` runs once the call succeeded, before the state is applied and the entities are written: for
        bookkeeping the entities show too, so that they are written once with both. With ``lock``, each attempt holds
        it, not the waits between them (see _through_warmup).
        """
        try:
            result = await self._through_warmup(func, lock=lock)
        except NotPairedError as err:
            if self._rejected():
                self.config_entry.async_start_reauth(self.hass)
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="not_paired") from err
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="not_accepted") from err
        except ProjectorBusyError as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="projector_busy") from err
        except COMMUNICATION_ERRORS as err:
            starting = self._startup is not None or self._startup_spent
            if isinstance(err, CommandRejected) and err.code == ErrorCode.MAIN_BOARD_STARTING and starting:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="starting_up") from err
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="communication_error",
                translation_placeholders={"error": str(err) or type(err).__name__},
            ) from err
        if before_apply is not None:
            before_apply()
        if apply is not None:
            self._apply(apply, result)

    def _apply(self, apply: Apply, result: Any) -> None:
        """Apply what a command changed, now and to the polls in flight (which may have read the state before it)."""
        if self._polls_in_flight:
            self._applied.append((apply, result))
        if self.data is not None:
            apply(self.data, result)
            self.async_set_updated_data(self.data)

    def _rejected(self) -> bool:
        """Count a connection the projector rejected; whether it is time to ask for pairing again.

        Only a successful poll starts the count again: some commands go over Wi-Fi, so their success says nothing
        about the pairing.
        """
        self._rejections += 1
        _LOGGER.debug(
            "%s: the projector rejected Home Assistant (%d of %d connections in a row before asking to pair again)",
            self.client.address,
            self._rejections,
            REJECTIONS_BEFORE_REAUTH,
        )
        return self._rejections >= REJECTIONS_BEFORE_REAUTH

    async def _through_warmup(self, func: Callable[[], Awaitable[Any]], *, lock: asyncio.Lock | None = None) -> Any:
        """Run ``func``, waiting for the projector while it is still starting up after being switched on.

        MAIN_BOARD_STARTING waits until the main board is up or the start-up times out, then tries once more;
        UNKNOWN_COMMAND and TIMEOUT soon after switching on (seen from the system controller too) are retried every
        second. With ``lock``, each attempt holds it but the waits between attempts do not.
        """
        loop = asyncio.get_running_loop()
        waited = False
        while True:
            try:
                async with lock if lock is not None else contextlib.nullcontext():
                    return await func()
            except (ProjectorBusyError, CommandRejected) as err:
                if isinstance(err, ProjectorBusyError) or err.code == ErrorCode.UNKNOWN_COMMAND:
                    if loop.time() + WARMUP_RETRY_DELAY > self._warmup_until:
                        raise
                    _LOGGER.debug("%s: still switching on, retrying: %s", self.client.address, err)
                    await asyncio.sleep(WARMUP_RETRY_DELAY)
                    continue
                if err.code != ErrorCode.MAIN_BOARD_STARTING or waited:
                    raise
                waited = True
                startup = self._startup
                if startup is None and loop.time() < self._board_up_at + STARTUP_PROBE_INTERVAL:
                    # The board answered just now, after this command was rejected: once more, with no new start-up.
                    continue
                if startup is None and self._may_start_up():
                    startup = self._begin_startup()
                if startup is None:
                    raise
                startup.confirmed = True
                _LOGGER.debug("%s: waiting for the main board to start up: %s", self.client.address, err)
                self.client.linger_at_most(POLL_LINGER)  # the probe reconnects; nothing needs the link till then
                await startup.ended.wait()

    def _may_start_up(self) -> bool:
        """Whether MAIN_BOARD_STARTING with no start-up under way starts one (switched on some other way).

        Not in standby (where it is no sign of a start-up), and not again after one timed out.
        """
        return not self._startup_spent and (self.data is None or self.data.power is not False)

    @property
    def starting_up(self) -> bool:
        """Whether the projector is on and its main board has not shown what it shows since it was switched on."""
        return self._startup is not None

    @property
    def shows_starting_up(self) -> bool:
        """What the media player says: starting up, or the poll made as the start-up ended still running.

        So that it says the start-up is over only with what that poll read.
        """
        return self.starting_up or self._startups_ending > 0

    def _shows_nothing_yet(self, app_state: int | None) -> bool:
        """Whether ``app_state`` is the main board's answer while it still starts up after Restart.

        After Restart (one projector, tried twice) it once answered app state and topology 0 for about 13 s before
        what it showed before came back (the other time not). Only after Restart: no power-on was seen to do that, and
        app state 0 is also what the projector reports after StopApp, so a power-on does not wait for something else.
        """
        startup = self._startup
        return app_state == AppState.UNKNOWN and startup is not None and startup.after_restart

    @callback
    def _begin_startup(self, *, confirmed: bool = True, notify: bool = True) -> _StartUp:
        if (startup := self._startup) is not None:
            startup.confirmed |= confirmed
            return startup
        now = asyncio.get_running_loop().time()
        startup = self._startup = _StartUp(began=now, deadline=now + STARTUP_TIMEOUT, confirmed=confirmed)
        self._warmup_until = now + POWER_ON_WARMUP
        _LOGGER.debug("%s: start-up: waiting up to %.0f s for the main board", self.client.address, STARTUP_TIMEOUT)
        startup.probe = self.config_entry.async_create_background_task(
            self.hass, self._probe_main_board(startup), f"{DOMAIN} wait for the main board"
        )
        if notify:
            self.async_update_listeners()  # the media player's starting_up
        return startup

    @callback
    def _end_startup(self, *, notify: bool = True) -> None:
        startup, self._startup = self._startup, None
        if startup is None:
            return
        startup.ended.set()
        if startup.probe is not None and startup.probe is not asyncio.current_task():
            startup.probe.cancel()
        if notify:
            self.async_update_listeners()  # the media player's starting_up

    def _began_since(self, poll_began: float) -> bool:
        """Whether the start-up under way began after a poll that began then (its reads may be from before it)."""
        startup = self._startup
        return startup is not None and startup.began > poll_began

    @callback
    def _track_startup(self, state: ProjectorState, previous: ProjectorState | None, poll_began: float) -> None:
        """Follow the start-up from a poll: its end, or one after the projector was switched on some other way."""
        if state.power and self._began_since(poll_began):
            return  # switched on or restarted while this poll ran: what it read may be from before
        # In standby, or the main board shows what it shows.
        if not state.power or (state.app_state is not None and not self._shows_nothing_yet(state.app_state)):
            if self._startup is not None and not state.power:
                self._startup.in_standby = True
                # Switched off some other way (its button, the app, a timer): not for when it is on again.
                if self._pending_source is not None:
                    self._pending_source = None
                    self._warn_source_dropped_in_standby()
            if self._startup is not None:
                _LOGGER.debug("%s: start-up over (%s)", self.client.address, "main board up" if state.power else "off")
                if state.power:
                    self._board_up_at = asyncio.get_running_loop().time()
            self._startup_spent = False
            self._end_startup(notify=False)
        elif previous is not None and previous.power is False:  # switched on by its button, the app or a timer
            self._startup_spent = False
            self._begin_startup(confirmed=state.main_board_starting, notify=False)
        elif (
            state.main_board_starting
            and self._board_up_at < poll_began  # not read before the board came up while this poll ran
            and (self._startup is not None or self._may_start_up())
        ):
            self._begin_startup(notify=False)  # or mark the one under way as a cold start

    async def _probe_main_board(self, startup: _StartUp) -> None:
        """Ask the main board now and then until it shows what it shows, then poll; end the start-up at its deadline.

        At the deadline a board that answers, if only with app state 0 after Restart, counts as up; one that does
        not answer at all as not starting, and a poll then shows how the projector is.
        """
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(max(0.0, min(STARTUP_PROBE_INTERVAL, startup.deadline - loop.time())))
            began = startup.began
            try:
                app_state = decode_u8(await self.client.command(Op.GET_APP_STATE, linger=POLL_LINGER))
            except COMMUNICATION_ERRORS as err:
                _LOGGER.debug("%s: main board not up yet: %s", self.client.address, err)
                startup.confirmed |= main_board_starting(Op.GET_APP_STATE, err)
                if loop.time() < startup.deadline:
                    continue
                dropped = " (dropping the source chosen in standby)" if self._pending_source else ""
                self._pending_source = None
                _LOGGER.warning(
                    "%s: the projector did not finish starting up within %.0f s%s",
                    self.client.address,
                    STARTUP_TIMEOUT,
                    dropped,
                )
                self._startup_spent = True
                self._end_startup(notify=False)
                await self._refresh_ending_startup()  # settle polls may have been skipped meanwhile
                return
            if startup.began != began:
                continue  # restarted while this was asked: the answer may be from before
            if self._shows_nothing_yet(app_state) and loop.time() < startup.deadline:
                _LOGGER.debug("%s: main board answers, showing nothing yet", self.client.address)
                continue
            elapsed = loop.time() - startup.began
            _LOGGER.debug("%s: main board up %.0f s into the start-up, polling", self.client.address, elapsed)
            self._startup_spent = False
            self._board_up_at = loop.time()
            self._end_startup(notify=False)
            # Now, not after the debouncer's cooldown from the settle polls; it switches to a source chosen meanwhile.
            await self._refresh_ending_startup()
            return

    async def _refresh_ending_startup(self) -> None:
        """Poll as a start-up ends; only then write the entities with starting_up off.

        Not before, with the state from before: an automation waiting for starting_up to turn off would read the
        source shown before the one it chose in standby, which the poll switches to (up to a minute). Commands that
        finish while it runs (say, ones woken by the end of the start-up) still say it is starting up. Known limit: a
        source chosen while the projector was on and starting up is switched to outside the poll, and may show only
        after starting_up has turned off.
        """
        self._startups_ending += 1
        try:
            await self.async_refresh()
        finally:
            self._startups_ending -= 1
        # Also when the poll failed (the entities unavailable) or was skipped for a shutdown.
        self.async_update_listeners()

    async def async_set_power(self, on: bool, *, unless_on: bool = False) -> None:
        def apply(state: ProjectorState, _result: Any) -> None:
            state.power = on

        def follow_startup() -> None:
            # Before the power is applied, so that the media player is written once, with its starting_up.
            self._startup_spent = False
            if on:
                self._begin_startup(confirmed=False, notify=False)
            else:
                if self._startup is not None:
                    self._startup.in_standby = True
                self._end_startup(notify=False)
                self._warmup_until = 0.0

        if not on and self._pending_source is not None:
            self._pending_source = None  # chosen in standby, waiting for the main board
            self._warn_source_dropped_in_standby()
        async with self._switching:
            skip = unless_on and self.data is not None and self.data.power  # switched on while this waited
            if not skip:
                await self.async_command(lambda: self.client.set_power(on), apply, before_apply=follow_startup)
        # Also when skipped: switched on another way (seen by a poll), nothing may have scheduled them.
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

    async def async_set_weather(self, code: int, *, redraw: bool = False, unless_held: bool = False) -> bool:
        """Send the Weather poster's code while it is shown; False if it is not. Raises HomeAssistantError.

        Only then: what SetWeather does while HDMI or another poster shows is unknown (it might switch to the
        Weather poster). The projector draws the code only when the poster is next shown, so with ``redraw`` the
        poster is shown again (client.redraw_poster) while the source switches wait, once a fresh read shows it is
        still on, showing the Weather poster (the last poll may be a minute old); the source still shows the
        Weather poster meanwhile, also in a poll that read the poster shown in between. With ``unless_held``, nothing
        is sent if the projector holds the code already (GetWeather). For background updates: it waits for a source
        switch or power change in progress and for the start-up after switching on. A projector that does not know
        the command answers like one still starting up, so the waits between attempts do not hold up power changes
        and source switches.
        """
        sent = redrawn = False

        async def attempt() -> None:
            nonlocal sent, redrawn
            if not self.weather_poster_shown:
                return
            if unless_held and await self.client.get_weather() == code:
                sent = True
                return
            if redraw and not await self.client.shows_poster(Poster.WEATHER):
                # The last poll is out of date (its power button, the app): a redraw would switch the projector
                # on, or away from what it shows now.
                self._refresh_soon("the Weather poster no longer shows")
                return
            await self.client.set_weather(code)
            if redraw:
                await self._redraw_weather_poster()
                redrawn = True
            sent = True

        def show_weather(state: ProjectorState, _result: Any) -> None:
            # Also for a poll that read the poster in between, or ran from before the redraw.
            state.app_state, state.poster = AppState.POSTER, Poster.WEATHER

        await self.async_command(attempt, lock=self._switching)
        if redrawn:
            self._apply(show_weather, None)
        return sent

    def _refresh_soon(self, why: str) -> None:
        _LOGGER.debug("%s: polling again: %s", self.client.address, why)
        self.config_entry.async_create_background_task(
            self.hass, self.async_request_refresh(), f"{DOMAIN} refresh: {why}"
        )

    async def _redraw_weather_poster(self) -> None:
        self._redrawing = True
        try:
            await self.client.redraw_poster(Poster.WEATHER)
        except RedrawIncompleteError as err:
            _LOGGER.warning(
                "%s: redrawing the Weather poster failed; it may still show the picture from before: %s",
                self.client.address,
                describe_error(err),
            )
            self._refresh_soon("a redraw failed")
            raise
        except COMMUNICATION_ERRORS as err:
            # What it shows is not known now; the next poll tells, and the entities show it. If that is the poster in
            # between, the Weather poster is put back (_restore_after_redraw).
            self._left_on_redraw_poster = self._source_choice
            if isinstance(err, RedrawStoppedError) and err.in_standby:
                _LOGGER.info(
                    "%s: switched to standby while the Weather poster was redrawn; it is put back from %s when on",
                    self.client.address,
                    POSTER_NAMES[REDRAW_POSTER],
                )
            else:
                _LOGGER.warning(
                    "%s: redrawing the Weather poster failed; it may show %s instead until it can be put back: %s",
                    self.client.address,
                    POSTER_NAMES[REDRAW_POSTER],
                    describe_error(err),
                )
            self._refresh_soon("a redraw failed")
            raise
        finally:
            self._redrawing = False

    @callback
    def _restore_after_redraw(self, state: ProjectorState) -> None:
        """After a poll: put back the Weather poster if a redraw that failed left the poster in between up.

        Only while nothing else was chosen: no source in Home Assistant since, and every poll since showed the
        poster in between or standby.
        """
        if self._left_on_redraw_poster is _NOTHING_LEFT or not state.power:
            return
        if self._left_on_redraw_poster is not self._source_choice or (state.app_state, state.poster) != (
            AppState.POSTER,
            REDRAW_POSTER,
        ):
            self._left_on_redraw_poster = _NOTHING_LEFT
            return
        if self._restoring is None or self._restoring.done():
            self._restoring = self.config_entry.async_create_background_task(
                self.hass, self._async_put_back_weather_poster(), f"{DOMAIN} put back the Weather poster"
            )

    async def _async_put_back_weather_poster(self) -> None:
        token = self._left_on_redraw_poster

        async def put_back() -> bool:
            if self._left_on_redraw_poster is not token or self._source_choice is not token:
                return False  # chosen since
            if not await self.client.shows_poster(REDRAW_POSTER):
                return False
            await self.client.set_poster(Poster.WEATHER)
            return True

        def show_weather(state: ProjectorState, put: bool) -> None:
            if put:
                state.app_state, state.poster = AppState.POSTER, Poster.WEATHER

        try:
            await self.async_command(put_back, show_weather, lock=self._switching)
        except HomeAssistantError as err:
            _LOGGER.debug("%s: putting back the Weather poster failed: %s", self.client.address, err)
            return  # tried again after the next poll that shows the poster in between
        _LOGGER.debug("%s: Weather poster put back after a failed redraw", self.client.address)
        self._left_on_redraw_poster = _NOTHING_LEFT

    async def async_read_http_endpoint(self) -> HttpEndpoint:
        """Ask the projector where its HTTP API is, for background use. Raises COMMUNICATION_ERRORS.

        Like async_set_weather: waits for a source switch or power change in progress (a read sent during a switch
        may go unanswered until it is done) and for the start-up after switching on.
        """

        async def read() -> HttpEndpoint:
            return await self.client.get_http_endpoint(linger=POLL_LINGER)

        endpoint: HttpEndpoint = await self._through_warmup(read, lock=self._switching)
        return endpoint

    async def async_web_request(
        self, method: tunnel.Method, path: str, body: Callable[[], str] | None = None
    ) -> tunnel.Response:
        """One HTTP request through the Bluetooth tunnel, for background use. Raises TunnelError or
        COMMUNICATION_ERRORS.

        Like async_read_http_endpoint: waits for a source switch or power change in progress and for the start-up
        after switching on; disconnects soon after, as after a poll. ``body`` is called once the link is
        ours, connected and secured, before each attempt (so a time in it is not aged by the wait).
        """

        async def send() -> tunnel.Response:
            return await self.client.web_request(method, path, body, linger=POLL_LINGER)

        response: tunnel.Response = await self._through_warmup(send, lock=self._switching)
        return response

    async def async_http_endpoint(self) -> HttpEndpoint:
        """Where the projector's HTTP API is: read over Bluetooth, or the last such read if recent enough.

        Only endpoints with an address are reused, so a projector that is not on Wi-Fi is asked again each time.
        Raises COMMUNICATION_ERRORS.
        """
        loop = asyncio.get_running_loop()
        async with self._http_endpoint_lock:  # the clock and the settings may ask at the same time: read once
            cached = self._http_endpoint
            if cached is not None and loop.time() - self._http_endpoint_read_at < HTTP_ENDPOINT_MAX_AGE:
                return cached
            endpoint = await self.async_read_http_endpoint()
            if endpoint.host is None:
                self.forget_http_endpoint()
            else:
                self._http_endpoint, self._http_endpoint_read_at = endpoint, loop.time()
            return endpoint

    @callback
    def forget_http_endpoint(self) -> None:
        """Read the endpoint again next time (after a failed HTTP request, a network change or standby)."""
        self._http_endpoint = None

    async def async_reboot(self) -> None:
        """Restart the projector: its main board starts up again, as after a cold power-on (tried twice).

        Counted as a cold start-up from the start (confirmed), so a source chosen meanwhile waits even if nothing
        sees MAIN_BOARD_STARTING before the board answers app state 0. The settle polls still run (they show the
        source going unknown at once), and the probe polls once the board is up (within 10 s, also if Restart did
        not restart it), or when it gives up.
        """
        async with self._switching:
            await self.async_command(self.client.reboot)
        # A start-up that timed out (perhaps why it was restarted) does not keep this one from being waited for.
        self._startup_spent = False
        if self.data is None or self.data.power is not False:
            startup = self._begin_startup()
            startup.after_restart = True
            startup.began = asyncio.get_running_loop().time()
            startup.deadline = startup.began + STARTUP_TIMEOUT
            self._warmup_until = startup.began + POWER_ON_WARMUP
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
    def _async_on_notification(self, notification: Notification) -> None:
        """Poll soon after the projector says its power or what it shows changed (say, by its power button).

        The client logs every notification. These only arrive while the link is open anyway (right after a poll or a
        command), and the poll they cause disconnects as soon as any other. Which values they carry is not confirmed
        yet, so they are not applied directly.
        """
        if notification.op not in REFRESH_NOTIFICATIONS:
            return
        frame = (notification.result, notification.data)
        if self._notified.get(notification.op) == frame:
            _LOGGER.debug("%s: notification repeated, not polling again", self.client.address)
            return
        self._notified[notification.op] = frame
        if self._switching.locked():
            # Our own power change or source switch, which polls when done.
            _LOGGER.debug("%s: notification during a switch, polling when it is done", self.client.address)
            return
        if notification.op == NotifyOp.POWER:
            # Reads fail for a few seconds after switching power: poll as after switching it from here.
            self._schedule_settle_refreshes()
            return
        self.config_entry.async_create_background_task(
            self.hass, self.async_request_refresh(), f"{DOMAIN} refresh after a notification"
        )

    @callback
    def _schedule_settle_refreshes(self) -> None:
        for unsub in self._settle_unsubs:
            unsub()
        self._settle_unsubs = [
            async_call_later(self.hass, delay, self._settle_refresh) for delay in POWER_SETTLE_REFRESH_DELAYS
        ]

    async def _settle_refresh(self, _now: Any) -> None:
        if (startup := self._startup) is not None and startup.confirmed and not startup.after_restart:
            # The main board is still starting up: nothing new before it answers, and the probe polls then. (After
            # Restart they run: the source shown before is otherwise kept until the board is up.)
            return
        await self.async_request_refresh()

    async def async_shutdown(self) -> None:
        self.client.set_notification_callback(None)
        for unsub in self._settle_unsubs:
            unsub()
        self._settle_unsubs = []
        self._end_startup()
        ir.async_delete_issue(self.hass, DOMAIN, self._stuck_issue_id)
        await super().async_shutdown()
        # Cuts short a source switch in flight rather than wait up to a minute for its reply.
        await asyncio.shield(self.client.close())
