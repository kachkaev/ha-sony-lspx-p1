"""Switch entities: the Auto toggles and xLOUD."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import LSPXClient
from .coordinator import LSPXConfigEntry, LSPXCoordinator
from .entity import LSPXEntity
from .protocol import ProjectorState

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class LSPXSwitchDescription(SwitchEntityDescription):
    attr: str
    set_fn: Callable[[LSPXClient, bool], Awaitable[bool]]


SWITCHES: tuple[LSPXSwitchDescription, ...] = (
    LSPXSwitchDescription(
        key="auto_brightness",
        translation_key="auto_brightness",
        attr="auto_brightness",
        set_fn=lambda c, v: c.set_auto_brightness(v),
    ),
    LSPXSwitchDescription(
        key="auto_focus",
        translation_key="auto_focus",
        attr="focus_assist",
        set_fn=lambda c, v: c.set_focus_assist(v),
    ),
    LSPXSwitchDescription(
        key="auto_rotation",
        translation_key="auto_rotation",
        attr="auto_direction",
        set_fn=lambda c, v: c.set_auto_direction(v),
    ),
    LSPXSwitchDescription(
        key="xloud",
        translation_key="xloud",
        attr="xloud",
        entity_category=EntityCategory.CONFIG,
        set_fn=lambda c, v: c.set_xloud(v),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: LSPXConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(LSPXSwitch(entry.runtime_data, d) for d in SWITCHES)


class LSPXSwitch(LSPXEntity, SwitchEntity):
    entity_description: LSPXSwitchDescription

    def __init__(self, coordinator: LSPXCoordinator, description: LSPXSwitchDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._state_attr = description.attr

    @property
    def is_on(self) -> bool | None:
        return getattr(self.coordinator.data, self.entity_description.attr)

    async def _set(self, on: bool) -> None:
        desc = self.entity_description

        def apply(state: ProjectorState, result: bool) -> None:
            setattr(state, desc.attr, result)

        await self.coordinator.async_command(lambda: desc.set_fn(self.coordinator.client, on), apply)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
