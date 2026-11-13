"""Switch entities: the Auto toggles, xLOUD, standby after inactivity and the restriction on adding smartphones."""

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
from .protocol import AUTO_STANDBY_INACTIVITY, AutoStandby, ProjectorState

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class LSPXSwitchDescription(SwitchEntityDescription):
    attr: str
    set_fn: Callable[[LSPXClient, bool], Awaitable[Any]]  # returns the new value of attr
    value_fn: Callable[[Any], bool] = bool  # attr's value -> on/off


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
    # The app's Settings → Network → "Restrict adding smartphones": a new phone then has to
    # enter a PIN shown on the projector. Controllers already registered keep working.
    LSPXSwitchDescription(
        key="restrict_adding_smartphones",
        translation_key="restrict_adding_smartphones",
        attr="restrict_new_controllers",
        entity_category=EntityCategory.CONFIG,
        set_fn=lambda c, v: c.set_restrict_new_controllers(v),
    ),
    # The app's Settings → Startup/Networked Standby settings → "Switch to Networked Standby after
    # inactivity": the screen goes off after a while in the dark without interaction.
    LSPXSwitchDescription(
        key="standby_after_inactivity",
        translation_key="standby_after_inactivity",
        attr="auto_standby",
        entity_category=EntityCategory.CONFIG,
        set_fn=lambda c, v: c.set_standby_after_inactivity(v),
        value_fn=lambda flags: bool(flags[AUTO_STANDBY_INACTIVITY] & AutoStandby.OFF),
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
        value = getattr(self.coordinator.data, self.entity_description.attr)
        return None if value is None else self.entity_description.value_fn(value)

    async def _set(self, on: bool) -> None:
        desc = self.entity_description

        def apply(state: ProjectorState, result: bool) -> None:
            setattr(state, desc.attr, result)

        await self.coordinator.async_command(lambda: desc.set_fn(self.coordinator.client, on), apply)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
