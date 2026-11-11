"""Number entities: projection brightness, focus, keystone and picture parameters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import LSPXClient
from .coordinator import LSPXConfigEntry, LSPXCoordinator
from .entity import LSPXEntity
from .protocol import PictureMode, ProjectorState

PARALLEL_UPDATES = 1

CUSTOM_PICTURE_MODES = {PictureMode.CUSTOM_1, PictureMode.CUSTOM_2}


@dataclass(frozen=True, kw_only=True)
class LSPXNumberDescription(NumberEntityDescription):
    attr: str
    set_fn: Callable[[LSPXClient, int], Awaitable[int]]
    # Returns (min, max) from the projector's own ranges, when known.
    range_fn: Callable[[ProjectorState], tuple[int, int] | None]
    # Extra state updates after a successful write (e.g. the Auto switch turned off).
    after_set: Callable[[ProjectorState], None] | None = None
    # For values that are part of a larger reading (white balance channels).
    value_fn: Callable[[ProjectorState], int | None] | None = None
    requires_custom_picture_mode: bool = False


def _projection(attr: str) -> Callable[[ProjectorState], tuple[int, int] | None]:
    def fn(state: ProjectorState) -> tuple[int, int] | None:
        r = state.projection_range
        if r is None:
            return None
        return (r.brightness_min, r.brightness_max) if attr == "brightness" else (r.keystone_min, r.keystone_max)

    return fn


def _picture(attr: str) -> Callable[[ProjectorState], tuple[int, int] | None]:
    def fn(state: ProjectorState) -> tuple[int, int] | None:
        return None if state.picture_range is None else getattr(state.picture_range, attr)

    return fn


def _picture_setter(attr: str) -> Callable[[LSPXClient, int], Awaitable[int]]:
    def fn(client: LSPXClient, value: int) -> Awaitable[int]:
        return client.set_picture_value(attr, value)

    return fn


def _rgb_setter(channel: int) -> Callable[[LSPXClient, int], Awaitable[tuple[int, int, int]]]:
    def fn(client: LSPXClient, value: int) -> Awaitable[tuple[int, int, int]]:
        return client.set_picture_rgb_channel(channel, value)

    return fn


def _rgb_value(channel: int) -> Callable[[ProjectorState], int | None]:
    def fn(state: ProjectorState) -> int | None:
        return None if state.picture_rgb is None else state.picture_rgb[channel]

    return fn


def _focus_range(state: ProjectorState) -> tuple[int, int] | None:
    limits = state.focus_limits
    return None if limits is None else (limits.far, limits.near)


def _disable(attr: str) -> Callable[[ProjectorState], None]:
    def fn(state: ProjectorState) -> None:
        setattr(state, attr, False)

    return fn


NUMBERS: tuple[LSPXNumberDescription, ...] = (
    LSPXNumberDescription(
        key="brightness",
        translation_key="brightness",
        attr="brightness",
        native_min_value=40,
        native_max_value=100,
        mode=NumberMode.SLIDER,
        set_fn=lambda c, v: c.set_brightness(v),
        range_fn=_projection("brightness"),
        after_set=_disable("auto_brightness"),
    ),
    LSPXNumberDescription(
        key="focus",
        translation_key="focus",
        attr="focus",
        native_min_value=-383,
        native_max_value=149,
        mode=NumberMode.SLIDER,
        set_fn=lambda c, v: c.set_focus(v),
        range_fn=_focus_range,
        after_set=_disable("focus_assist"),
    ),
    LSPXNumberDescription(
        key="keystone",
        translation_key="keystone",
        attr="keystone",
        native_min_value=-30,
        native_max_value=30,
        mode=NumberMode.SLIDER,
        set_fn=lambda c, v: c.set_keystone(v),
        range_fn=_projection("keystone"),
    ),
    *(
        LSPXNumberDescription(
            key=f"picture_{name}",
            translation_key=f"picture_{name}",
            attr=f"picture_{name}",
            native_min_value=lo,
            native_max_value=hi,
            mode=NumberMode.SLIDER,
            entity_category=EntityCategory.CONFIG,
            set_fn=_picture_setter(f"picture_{name}"),
            range_fn=_picture(name),
            requires_custom_picture_mode=True,
        )
        # Same order as the app's Custom picture screen.
        for name, lo, hi in (
            ("saturation", 0, 120),
            ("brightness", 100, 160),
            ("contrast", 10, 40),
            ("sharpness", 0, 15),
        )
    ),
    *(
        LSPXNumberDescription(
            key=f"white_balance_{colour}",
            translation_key=f"white_balance_{colour}",
            attr="picture_rgb",
            native_min_value=70,
            native_max_value=100,
            mode=NumberMode.SLIDER,
            entity_category=EntityCategory.CONFIG,
            set_fn=_rgb_setter(channel),
            value_fn=_rgb_value(channel),
            range_fn=_picture("rgb"),
            requires_custom_picture_mode=True,
        )
        for channel, colour in enumerate(("red", "green", "blue"))
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: LSPXConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(LSPXNumber(coordinator, d) for d in NUMBERS)


class LSPXNumber(LSPXEntity, NumberEntity):
    entity_description: LSPXNumberDescription
    _attr_native_step = 1

    def __init__(self, coordinator: LSPXCoordinator, description: LSPXNumberDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._state_attr = description.attr

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self.entity_description.requires_custom_picture_mode:
            return self.coordinator.data.picture_mode in CUSTOM_PICTURE_MODES
        return True

    @property
    def native_value(self) -> int | None:
        if self.entity_description.value_fn is not None:
            return self.entity_description.value_fn(self.coordinator.data)
        return getattr(self.coordinator.data, self.entity_description.attr)

    @property
    def native_min_value(self) -> float:
        r = self.entity_description.range_fn(self.coordinator.data) if self.coordinator.data else None
        return r[0] if r else self.entity_description.native_min_value

    @property
    def native_max_value(self) -> float:
        r = self.entity_description.range_fn(self.coordinator.data) if self.coordinator.data else None
        return r[1] if r else self.entity_description.native_max_value

    async def async_set_native_value(self, value: float) -> None:
        desc = self.entity_description

        def apply(state: ProjectorState, result: int) -> None:
            setattr(state, desc.attr, result)
            if desc.after_set:
                desc.after_set(state)

        await self.coordinator.async_command(lambda: desc.set_fn(self.coordinator.client, int(value)), apply)
