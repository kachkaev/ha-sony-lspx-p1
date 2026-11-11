"""Base entity for the Sony LSPX-P1."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import LSPXCoordinator


class LSPXEntity(CoordinatorEntity[LSPXCoordinator]):
    """Common device info and availability."""

    _attr_has_entity_name = True
    # Attribute of ProjectorState this entity depends on; unavailable when it is unknown.
    _state_attr: str | None = None

    def __init__(self, coordinator: LSPXCoordinator, key: str) -> None:
        super().__init__(coordinator)
        address = coordinator.client.address
        self._attr_unique_id = f"{address}_{key}"
        data = coordinator.data
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            name=coordinator.config_entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
            sw_version=data.software_version if data else None,
        )

    @property
    def available(self) -> bool:
        if not super().available or self.coordinator.data is None:
            return False
        if self._state_attr is None:
            return True
        return getattr(self.coordinator.data, self._state_attr) is not None
