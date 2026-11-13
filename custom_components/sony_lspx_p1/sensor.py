"""Sensor entities: battery, charge state, HDMI status, usage time and the registered smartphones."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import MAX_LENGTH_STATE_STATE as MAX_STATE_LENGTH
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LSPXConfigEntry, LSPXCoordinator
from .entity import LSPXEntity
from .protocol import ChargeState, Controller, HdmiStatus, ProjectorState

PARALLEL_UPDATES = 0

CHARGE_STATES = {s.value: s.name.lower() for s in ChargeState}
HDMI_STATES = {s.value: s.name.lower() for s in HdmiStatus}
HDMI_OFF = "off"  # wireless HDMI is not set up


def _hdmi_state(state: ProjectorState) -> str | None:
    if state.hdmi_status is None:
        return None
    enabled, status = state.hdmi_status
    return HDMI_STATES.get(status) if enabled else HDMI_OFF


@dataclass(frozen=True, kw_only=True)
class LSPXSensorDescription(SensorEntityDescription):
    value_fn: Callable[[ProjectorState], float | str | None]
    state_attr: str = "battery"  # ProjectorState field; the entity is unavailable while it is unknown
    attributes_fn: Callable[[ProjectorState], dict[str, Any]] | None = None


def _controllers_attributes(state: ProjectorState) -> dict[str, Any]:
    # Plain lists of names: Home Assistant shows them comma-separated in the entity's attributes.
    controllers = state.controllers or []
    return {
        "smartphones": [c.name for c in controllers],
        "blocked_smartphones": [c.name for c in controllers if not c.allowed],
        # For the remove/block/allow_smartphone actions, which take the id (names need not be unique).
        "smartphone_ids": {c.id: c.name for c in controllers},
    }


def _names_text(controllers: list[Controller]) -> str:
    text = ", ".join(c.name for c in controllers)
    return text if len(text) <= MAX_STATE_LENGTH else text[: MAX_STATE_LENGTH - 1] + "…"


SENSORS: tuple[LSPXSensorDescription, ...] = (
    LSPXSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.battery.level if s.battery else None,
    ),
    LSPXSensorDescription(
        key="charge_state",
        translation_key="charge_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(CHARGE_STATES.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: CHARGE_STATES.get(s.battery.charge_state) if s.battery else None,
    ),
    # The wireless HDMI unit: whether a source is plugged in and sending a signal.
    LSPXSensorDescription(
        key="hdmi_status",
        translation_key="hdmi_status",
        device_class=SensorDeviceClass.ENUM,
        options=[HDMI_OFF, *HDMI_STATES.values()],
        state_attr="hdmi_status",
        value_fn=_hdmi_state,
    ),
    # How long the projector has been on in total.
    LSPXSensorDescription(
        key="usage_time",
        translation_key="usage_time",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_attr="usage_hours",
        value_fn=lambda s: s.usage_hours,
    ),
    # The app's "Smartphones added": every registered controller, Home Assistant included.
    LSPXSensorDescription(
        key="smartphones_added",
        translation_key="smartphones_added",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_attr="controllers",
        value_fn=lambda s: len(s.controllers) if s.controllers is not None else None,
        attributes_fn=_controllers_attributes,
    ),
    # The same list as text, so the names show on the device page.
    LSPXSensorDescription(
        key="smartphone_names",
        translation_key="smartphone_names",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_attr="controllers",
        value_fn=lambda s: _names_text(s.controllers) if s.controllers is not None else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: LSPXConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(LSPXSensor(entry.runtime_data, d) for d in SENSORS)


class LSPXSensor(LSPXEntity, SensorEntity):
    entity_description: LSPXSensorDescription

    def __init__(self, coordinator: LSPXCoordinator, description: LSPXSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._state_attr = description.state_attr

    @property
    def native_value(self) -> float | str | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attributes_fn
        if fn is None or self.coordinator.data is None:
            return None
        return fn(self.coordinator.data)
