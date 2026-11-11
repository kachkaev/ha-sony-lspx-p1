"""Select entities: screen rotation and picture mode."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import LSPXClient
from .coordinator import LSPXConfigEntry, LSPXCoordinator
from .entity import LSPXEntity
from .protocol import Direction, PictureMode, ProjectorState

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class LSPXSelectDescription(SelectEntityDescription):
    attr: str
    values: dict[str, int]
    set_fn: Callable[[LSPXClient, int], Awaitable[int]]
    after_set: Callable[[ProjectorState], None] | None = None


def _disable_auto_direction(state: ProjectorState) -> None:
    state.auto_direction = False


ROTATION = {d.name.lower(): d.value for d in Direction}
PICTURE_MODES = {m.name.lower(): m.value for m in PictureMode}

SELECTS: tuple[LSPXSelectDescription, ...] = (
    LSPXSelectDescription(
        key="rotation",
        translation_key="rotation",
        attr="direction",
        values=ROTATION,
        options=list(ROTATION),
        set_fn=lambda c, v: c.set_direction(v),
        after_set=_disable_auto_direction,
    ),
    LSPXSelectDescription(
        key="picture_mode",
        translation_key="picture_mode",
        attr="picture_mode",
        values=PICTURE_MODES,
        options=list(PICTURE_MODES),
        entity_category=EntityCategory.CONFIG,
        set_fn=lambda c, v: c.set_picture_mode(v),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: LSPXConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(LSPXSelect(entry.runtime_data, d) for d in SELECTS)


class LSPXSelect(LSPXEntity, SelectEntity):
    entity_description: LSPXSelectDescription

    def __init__(self, coordinator: LSPXCoordinator, description: LSPXSelectDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._state_attr = description.attr

    @property
    def current_option(self) -> str | None:
        raw = getattr(self.coordinator.data, self.entity_description.attr)
        for option, value in self.entity_description.values.items():
            if value == raw:
                return option
        return None

    async def async_select_option(self, option: str) -> None:
        desc = self.entity_description

        def apply(state: ProjectorState, result: int) -> None:
            setattr(state, desc.attr, result)
            if desc.after_set:
                desc.after_set(state)

        value = desc.values[option]
        await self.coordinator.async_command(lambda: desc.set_fn(self.coordinator.client, value), apply)
