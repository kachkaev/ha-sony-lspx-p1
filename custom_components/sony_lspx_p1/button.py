"""Button entities: the app's Reset buttons and Restart."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LSPXConfigEntry, LSPXCoordinator
from .entity import LSPXEntity
from .number import CUSTOM_PICTURE_MODES
from .protocol import ProjectorState

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class LSPXButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[LSPXCoordinator], Awaitable[None]]
    available_fn: Callable[[ProjectorState], bool] = lambda _state: True
    # Pressable while polls fail: restarting is meant for a projector that stopped answering.
    always_available: bool = False


async def _reset_keystone(coordinator: LSPXCoordinator) -> None:
    def apply(state: ProjectorState, result: int) -> None:
        state.keystone = result

    await coordinator.async_command(lambda: coordinator.client.set_keystone(0), apply)


async def _reset_picture(coordinator: LSPXCoordinator) -> None:
    mode = coordinator.data.picture_mode
    assert mode is not None  # the button is unavailable without a custom picture mode
    await coordinator.async_command(lambda: coordinator.client.reset_custom_picture(mode))
    await coordinator.async_request_refresh()


BUTTONS: tuple[LSPXButtonDescription, ...] = (
    LSPXButtonDescription(
        key="keystone_reset",
        translation_key="keystone_reset",
        press_fn=_reset_keystone,
    ),
    LSPXButtonDescription(
        key="picture_reset",
        translation_key="picture_reset",
        entity_category=EntityCategory.CONFIG,
        press_fn=_reset_picture,
        available_fn=lambda state: state.picture_mode in CUSTOM_PICTURE_MODES,
    ),
    # Only known from the decompiled app, which sends it after switching demo mode; not yet tried on a projector.
    LSPXButtonDescription(
        key="restart",
        translation_key="restart",
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
        press_fn=LSPXCoordinator.async_reboot,
        always_available=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: LSPXConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(LSPXButton(entry.runtime_data, d) for d in BUTTONS)


class LSPXButton(LSPXEntity, ButtonEntity):
    entity_description: LSPXButtonDescription

    def __init__(self, coordinator: LSPXCoordinator, description: LSPXButtonDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        if self.entity_description.always_available:
            return True
        return super().available and self.entity_description.available_fn(self.coordinator.data)

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.coordinator)
