"""Media player entity: power (on/standby), volume and current source."""

from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LSPXConfigEntry, LSPXCoordinator
from .entity import LSPXEntity
from .protocol import AppState, ProjectorState

SOURCE_NAMES: dict[int, str] = {
    AppState.POSTER: "Poster",
    AppState.CONTENT_VIEWER: "Photos and videos",
    AppState.MIRRORING: "Screen mirroring",
    AppState.DLNA: "Home network",
    AppState.YOUTUBE: "YouTube",
    AppState.HDMI: "HDMI",
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
    )
    _attr_volume_step = 0.05
    _state_attr = "power"

    def __init__(self, coordinator: LSPXCoordinator) -> None:
        super().__init__(coordinator, "media_player")

    @property
    def _data(self) -> ProjectorState:
        return self.coordinator.data

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
        return SOURCE_NAMES.get(self._data.app_state)

    async def async_turn_on(self) -> None:
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self) -> None:
        await self.coordinator.async_set_power(False)

    async def async_set_volume_level(self, volume: float) -> None:
        def apply(state: ProjectorState, result: int) -> None:
            state.volume = result

        target = max(0, min(100, round(volume * 100)))
        await self.coordinator.async_command(lambda: self.coordinator.client.set_volume(target), apply)
