"""Base entities for the Sony LSPX-P1."""

from __future__ import annotations

from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import LSPXCoordinator
from .web_settings import WebSettingsCoordinator


def device_info(coordinator: LSPXCoordinator) -> DeviceInfo:
    address = coordinator.client.address
    data = coordinator.data
    return DeviceInfo(
        identifiers={(DOMAIN, address)},
        connections={(CONNECTION_BLUETOOTH, address)},
        name=coordinator.config_entry.title,
        manufacturer=MANUFACTURER,
        model=MODEL,
        sw_version=data.software_version if data else None,
    )


class LSPXEntity(CoordinatorEntity[LSPXCoordinator]):
    """Common device info and availability."""

    _attr_has_entity_name = True
    # Attribute of ProjectorState this entity depends on; unavailable when it is unknown.
    _state_attr: str | None = None

    def __init__(self, coordinator: LSPXCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.client.address}_{key}"
        self._attr_device_info = device_info(coordinator)

    @property
    def available(self) -> bool:
        if not super().available or self.coordinator.data is None:
            return False
        if self._state_attr is None:
            return True
        return getattr(self.coordinator.data, self._state_attr) is not None


class LSPXWebEntity(CoordinatorEntity[WebSettingsCoordinator]):
    """A setting read and written over the projector's Wi-Fi (experimental), disabled by default.

    ``context`` is the field of WebSettings it shows: only the settings of enabled entities are read.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: WebSettingsCoordinator, key: str, context: str) -> None:
        super().__init__(coordinator, context)
        self._attr_unique_id = f"{coordinator.main.client.address}_{key}"
        self._attr_device_info = device_info(coordinator.main)

    @property
    def available(self) -> bool:
        return super().available and getattr(self.coordinator.data, str(self.coordinator_context)) is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.async_check()  # read the setting now rather than after the next poll
