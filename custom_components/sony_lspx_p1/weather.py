"""The Weather poster follows a Home Assistant weather entity (experimental, issue #2).

Only set up when a weather entity is chosen in the options. The code goes to the projector with SetWeather, a
command from the official app's Bluetooth SDK that the app itself never sends (it changes the poster's picture
over HTTP instead), so whether the poster follows it is not yet known. Nothing here may disturb the rest of the
integration: sends run in the background, only while the Weather poster is shown (what SetWeather does while
HDMI or another poster shows is unknown), and failures are only logged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .client import describe_error
from .const import DOMAIN
from .coordinator import COMMUNICATION_ERRORS, LSPXCoordinator
from .protocol import AppState, Poster

_LOGGER = logging.getLogger(__name__)

# Home Assistant's weather conditions as the app's poster codes (protocol.WEATHER_CODES), after the AccuWeather
# icons the app maps to each. 3 (hazy), 4 (mostly cloudy, hot) and 9 (flurries) have no matching condition;
# "exceptional" (and unknown or unavailable) has no picture, so the poster keeps what it shows.
POSTER_CODES: dict[str, int] = {
    "sunny": 1,
    "clear-night": 1,
    "partlycloudy": 2,
    "cloudy": 5,
    "fog": 6,
    "windy": 6,
    "windy-variant": 6,
    "rainy": 7,
    "pouring": 8,
    "hail": 8,
    "snowy-rainy": 8,
    "lightning": 10,
    "lightning-rainy": 10,
    "snowy": 11,
}


class WeatherPoster:
    """Sends the weather entity's condition to the projector.

    Only while the Weather poster is shown and the last poll reached the projector: when the poster appears
    (chosen in Home Assistant, the app or on the projector, also by switching on onto it), when the condition
    changes and when polls reach the projector again. The same code is not sent twice in a row, except when the
    poster appears, so returning to it always sends the code again.
    """

    def __init__(self, hass: HomeAssistant, coordinator: LSPXCoordinator, entity_id: str) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self.entity_id = entity_id
        self._was_reachable = False
        self._was_showing = False
        self._chosen = False
        self._queued: int | None = None
        self._in_flight: int | None = None
        self._task: asyncio.Task[None] | None = None
        self.sent: int | None = None  # the last code the projector accepted
        self.last_error: str | None = None

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
        appeared = self._chosen or (showing and not self._was_showing)
        back = reachable and not self._was_reachable
        self._was_reachable, self._was_showing, self._chosen = reachable, showing, False
        if appeared or back:
            self._async_request(force=appeared)

    @callback
    def _async_weather_changed(self, _event: Event[EventStateChangedData]) -> None:
        self._async_request(force=False)

    @callback
    def _async_request(self, *, force: bool) -> None:
        code = self.code
        if code is None or not self._coordinator.weather_poster_shown:
            return
        latest = next((c for c in (self._queued, self._in_flight, self.sent) if c is not None), None)
        if code == latest and not force:
            return
        self._queued = code
        if self._task is None or self._task.done():
            self._task = self._coordinator.config_entry.async_create_background_task(
                self._hass, self._async_send_queued(), f"{DOMAIN} weather poster"
            )

    async def _async_send_queued(self) -> None:
        while (code := self._queued) is not None:
            self._queued, self._in_flight = None, code
            try:
                sent = await self._coordinator.async_set_weather(code)
            except COMMUNICATION_ERRORS as err:
                self.last_error = describe_error(err)
                _LOGGER.debug("%s: sending weather code %d failed: %s", self.entity_id, code, self.last_error)
                continue
            finally:
                self._in_flight = None
            if sent:
                self.sent, self.last_error = code, None
                _LOGGER.debug("%s: sent weather code %d (%s)", self.entity_id, code, self.condition)
