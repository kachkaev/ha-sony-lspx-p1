"""The Weather poster follows a Home Assistant weather entity (issue #2).

Only set up when a weather entity is chosen in the options. The code goes to the projector with SetWeather, a
command from the official app's Bluetooth SDK that the app itself never sends (it changes the poster's picture
over HTTP instead). A projector stores it at once but draws it only when the Weather poster is next shown, so a
change while the poster shows is followed by showing it again, as the app does after its own change. Nothing here
may disturb the rest of the integration: sends run in the background, only while the Weather poster is shown (what
SetWeather does while HDMI or another poster shows is unknown), and failures are only logged.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .client import describe_error
from .const import DOMAIN
from .coordinator import LSPXCoordinator
from .protocol import AppState, Poster

_LOGGER = logging.getLogger(__name__)

# Home Assistant's weather conditions as the projector's Weather poster codes, by what each picture shows on a
# projector (docs/protocol.md, "Weather"), which is not always what the app's names say. 6 (dark clouds through a
# window, with the sound of rain but none to see) fits no condition better than these. "exceptional" (and unknown or
# unavailable) has no picture, so the poster keeps what it shows.
POSTER_CODES: dict[str, int] = {
    "sunny": 1,  # a forest on a clear day
    "clear-night": 2,  # sunset or sunrise by a pool: the one clear sky that is not broad daylight
    "partlycloudy": 4,  # bright, a bit cloudy
    "windy": 4,  # bright and a bit cloudy over the ocean: no picture shows wind; this one is dry and open
    "cloudy": 5,  # rain clouds over a field, no rain
    "windy-variant": 5,  # windy and cloudy: clouds without rain
    "fog": 3,  # fog in a forest
    "rainy": 7,  # a tree in daylight, raining
    "pouring": 9,  # heavy rain in a dark forest
    "hail": 8,  # drops running down a window, the sound of heavy rain
    "snowy-rainy": 8,  # as the app does for sleet: wet rather than white
    "lightning": 10,  # lightning over a rainforest
    "lightning-rainy": 10,
    "snowy": 11,  # snow on spruce branches, snowing
}


# After a failed send, seconds before the one retry (about a poll later).
RETRY_DELAY = 60.0


@dataclass(frozen=True)
class _Send:
    code: int
    redraw: bool  # show the poster again, so that it draws the code
    unless_held: bool  # not if the projector holds the code already


class WeatherPoster:
    """Sends the weather entity's condition to the projector.

    Only while the Weather poster is shown and the last poll reached the projector:

    - When it is chosen in Home Assistant: right after the switch, so that it draws the code as it appears (seen
      once on a projector, 0.07 s after the switch).
    - When the condition changes, or polls reach the projector again with a code not sent yet: the poster is then
      shown again (LSPXCoordinator.async_set_weather) so that it draws the code.
    - When it appears otherwise (chosen in the app or on the projector, or shown when switched on): the projector's
      code is read first, and only if it is another one is the code sent and the poster shown again (it draws the
      code it holds as it appears). So a restart of Home Assistant does not redraw a poster that is right already.

    A failed send is tried once more a minute later; otherwise only a change of condition or the poster appearing
    again sends after it (not each update of the weather entity, nor polls reaching the projector again).
    """

    def __init__(self, hass: HomeAssistant, coordinator: LSPXCoordinator, entity_id: str) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self.entity_id = entity_id
        self._was_reachable = False
        self._was_showing = False
        self._chosen = False
        self._queued: _Send | None = None
        self._in_flight: int | None = None
        self._task: asyncio.Task[None] | None = None
        # The last code the projector took (and drew, when it had to be redrawn), or found holding.
        self.sent: int | None = None
        self.last_error: str | None = None
        # After a failed send, one more try a little later (only one until a send succeeds, so a projector that keeps
        # failing is not redrawn again and again).
        self._retry_unsub: CALLBACK_TYPE | None = None
        self._retry_spent = False
        self._failed: _Send | None = None  # the last send that failed (until one succeeds), which the retry repeats

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Follow the weather entity and the projector; returns the function that stops it."""
        unsubs = [
            async_track_state_change_event(self._hass, self.entity_id, self._async_weather_changed),
            self._coordinator.async_add_listener(self._async_projector_updated),
        ]
        self._async_projector_updated()

        @callback
        def stop() -> None:
            for unsub in unsubs:
                unsub()
            if self._retry_unsub is not None:
                self._retry_unsub()
                self._retry_unsub = None

        return stop

    @property
    def condition(self) -> str | None:
        state = self._hass.states.get(self.entity_id)
        return state.state if state else None

    @property
    def code(self) -> int | None:
        """The poster code for the current condition; None when there is no picture for it."""
        return POSTER_CODES.get(self.condition or "")

    def diagnostics(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "condition": self.condition,
            "code": self.code,
            "sent": self.sent,
            "last_error": self.last_error,
        }

    @callback
    def poster_chosen(self) -> None:
        """The Weather poster was chosen in Home Assistant: send the code with the update that follows."""
        self._chosen = True

    @callback
    def _async_projector_updated(self) -> None:
        data = self._coordinator.data
        reachable = self._coordinator.weather_poster_shown
        # Unlike `reachable`, unaffected by failing polls: the poster does not reappear when the link comes back.
        showing = bool(
            data is not None and data.power and data.app_state == AppState.POSTER and data.poster == Poster.WEATHER
        )
        chosen = self._chosen
        appeared = showing and not self._was_showing
        back = reachable and not self._was_reachable
        self._was_reachable, self._was_showing, self._chosen = reachable, showing, False
        if chosen:
            self._async_request(redraw=False, force=True)
        elif appeared:
            self._async_request(redraw=True, force=True, unless_held=True)
        elif back:
            self._async_request(redraw=True, force=False)

    @callback
    def _async_retry(self, _now: Any) -> None:
        self._retry_unsub = None
        failed = self._failed  # kept until a send succeeds: see _async_request
        if failed is not None and failed.code == self.code:
            # The same send again, even if its code is the one last sent (the projector may have lost it); redrawn
            # now, as a minute after the poster appeared it no longer draws a code as it appears.
            self._async_request(redraw=True, force=True, unless_held=failed.unless_held)
        else:
            self._async_request(redraw=True, force=False)

    @callback
    def _async_weather_changed(self, _event: Event[EventStateChangedData]) -> None:
        self._async_request(redraw=True, force=False)

    @callback
    def _async_request(self, *, redraw: bool, force: bool, unless_held: bool = False) -> None:
        code = self.code
        if code is None or not self._coordinator.weather_poster_shown:
            return
        queued = self._queued.code if self._queued is not None else None
        # After a failed send, its code: every update of the weather entity (its temperature, say) comes here, and
        # re-sending the same code is left to the one retry and to the poster appearing again (both forced). Another
        # code is sent, also the one sent before the failure (self.sent is None then: the projector's is not known).
        failed = self._failed.code if self._failed is not None else None
        latest = next((c for c in (queued, self._in_flight, self.sent, failed) if c is not None), None)
        if code == latest and not force:
            return
        self._queued = _Send(code, redraw, unless_held)
        if self._task is None or self._task.done():
            self._task = self._coordinator.config_entry.async_create_background_task(
                self._hass, self._async_send_queued(), f"{DOMAIN} weather poster"
            )

    async def _async_send_queued(self) -> None:
        while (send := self._queued) is not None:
            self._queued, self._in_flight = None, send.code
            try:
                sent = await self._coordinator.async_set_weather(
                    send.code, redraw=send.redraw, unless_held=send.unless_held
                )
            except HomeAssistantError as err:
                cause = err.__cause__ if isinstance(err.__cause__, Exception) else err
                self.last_error = describe_error(cause)
                self._failed = send
                # SetWeather may have been taken (and drawn) before the failure: what the projector holds is not
                # known, so any later code is sent, also the one sent before.
                self.sent = None
                if not self._retry_spent and self._retry_unsub is None:
                    self._retry_spent = True
                    self._retry_unsub = async_call_later(self._hass, RETRY_DELAY, self._async_retry)
                _LOGGER.debug("%s: sending weather code %d failed: %s", self.entity_id, send.code, self.last_error)
                continue
            finally:
                self._in_flight = None
            if sent:
                self.sent, self.last_error, self._failed = send.code, None, None
                self._retry_spent = False
                if self._retry_unsub is not None:  # from an earlier failure: nothing left to retry
                    self._retry_unsub()
                    self._retry_unsub = None
                _LOGGER.debug("%s: sent weather code %d (%s)", self.entity_id, send.code, self.condition)
