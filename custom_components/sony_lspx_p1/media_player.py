"""Media player entity: power (on/standby), volume and source (HDMI or a poster)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import LSPXConfigEntry, LSPXCoordinator
from .entity import LSPXEntity
from .protocol import CLOCK_POSTERS, POSTER_NAMES, AppState, Poster, ProjectorState

SOURCE_HDMI = "HDMI"
SOURCE_POSTER = "Poster"  # a poster the source list does not offer, such as My slideshow

# What the projector can be switched to: HDMI and every built-in poster, in the app's order.
SOURCE_LIST = [SOURCE_HDMI, *POSTER_NAMES.values()]
POSTERS_BY_NAME = {name: poster for poster, name in POSTER_NAMES.items()}

SOURCE_NAMES: dict[int, str] = {
    AppState.POSTER: SOURCE_POSTER,
    AppState.CONTENT_VIEWER: "Photos and videos",
    AppState.MIRRORING: "Screen mirroring",
    AppState.DLNA: "Home network",
    AppState.YOUTUBE: "YouTube",
    AppState.HDMI: SOURCE_HDMI,
}

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: LSPXConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([LSPXMediaPlayer(entry.runtime_data)])


class LSPXMediaPlayer(LSPXEntity, MediaPlayerEntity):
    """The projector as a media player."""

    _attr_name = None
    _attr_translation_key = "projector"
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.SELECT_SOURCE
    )
    _attr_source_list = SOURCE_LIST
    _attr_volume_step = 0.05
    _state_attr = "power"

    def __init__(self, coordinator: LSPXCoordinator) -> None:
        super().__init__(coordinator, "media_player")

    @property
    def _data(self) -> ProjectorState:
        # Before the first successful poll there is no data; Home Assistant still reads the state
        # (for the entity picture) while the entity is unavailable.
        return self.coordinator.data or ProjectorState()

    @property
    def state(self) -> MediaPlayerState | None:
        if self._data.power is None:
            return None
        return MediaPlayerState.ON if self._data.power else MediaPlayerState.OFF

    @property
    def volume_level(self) -> float | None:
        return None if self._data.volume is None else self._data.volume / 100

    @property
    def source(self) -> str | None:
        if not self._data.power or self._data.app_state is None:
            return None
        if self._data.app_state == AppState.POSTER and self._data.poster in POSTER_NAMES:
            return POSTER_NAMES[Poster(self._data.poster)]
        return SOURCE_NAMES.get(self._data.app_state)

    async def async_select_source(self, source: str) -> None:
        client = self.coordinator.client
        if source == SOURCE_HDMI:

            def apply_hdmi(state: ProjectorState, _result: Any) -> None:
                state.app_state = AppState.HDMI

            await self.coordinator.async_select_source(client.show_hdmi, apply_hdmi)
            return
        poster = POSTERS_BY_NAME.get(source)
        if poster is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_source",
                translation_placeholders={"source": source},
            )

        def apply_poster(state: ProjectorState, _result: Any) -> None:
            state.app_state = AppState.POSTER
            state.poster = poster
            if poster is Poster.WEATHER and self.coordinator.weather is not None:
                self.coordinator.weather.poster_chosen()
            if poster in CLOCK_POSTERS and self.coordinator.clock is not None:
                self.coordinator.clock.poster_chosen()

        version = self._data.software_version
        await self.coordinator.async_select_source(
            lambda: client.show_poster(poster, software_version=version), apply_poster
        )

    async def async_turn_on(self) -> None:
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self) -> None:
        await self.coordinator.async_set_power(False)

    async def async_set_volume_level(self, volume: float) -> None:
        def apply(state: ProjectorState, result: int) -> None:
            state.volume = result

        target = max(0, min(100, round(volume * 100)))
        await self.coordinator.async_command(lambda: self.coordinator.client.set_volume(target), apply)
