"""The Sony LSPX-P1 projector integration (Bluetooth LE)."""

from __future__ import annotations

from pathlib import Path

from bleak.backends.device import BLEDevice
from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .client import LSPXClient
from .clock import ClockSync
from .const import CONF_WEATHER_ENTITY, DOMAIN
from .coordinator import LSPXConfigEntry, LSPXCoordinator
from .services import async_setup_services
from .weather import WeatherPoster

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CARD_URL = f"/{DOMAIN}/sony-lspx-p1-card.js"
CARD_PATH = Path(__file__).parent / "frontend" / "sony-lspx-p1-card.js"

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the service actions; serve the dashboard card and load it on every dashboard."""
    async_setup_services(hass)
    if "frontend" not in hass.config.components or "http" not in hass.config.components:
        return True  # e.g. tests or minimal setups without a UI
    from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415
    from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

    version = (await async_get_integration(hass, DOMAIN)).version
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(CARD_PATH), cache_headers=False)])
    add_extra_js_url(hass, f"{CARD_URL}?v={version}")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LSPXConfigEntry) -> bool:
    address: str = entry.data[CONF_ADDRESS]
    # Set up even when the projector is off or out of range: its entities show as unavailable until a
    # poll reaches it. (Failing setup instead makes Home Assistant call them "no longer provided".)
    # Every poll looks the projector up again, so a placeholder device is enough to start with.
    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True) or BLEDevice(
        address, entry.title, details=None
    )
    client = LSPXClient(ble_device)

    @callback
    def _async_update_ble_device(
        service_info: bluetooth.BluetoothServiceInfoBleak, _change: bluetooth.BluetoothChange
    ) -> None:
        # Follow the adapter or proxy Home Assistant currently reaches the projector through.
        client.set_ble_device(service_info.device)

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble_device,
            bluetooth.BluetoothCallbackMatcher(address=address, connectable=True),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )
    coordinator = LSPXCoordinator(hass, entry, client)
    # Before the first poll, so that a projector already on gets its clock set straight away.
    coordinator.clock = ClockSync(hass, coordinator)
    # Not async_config_entry_first_refresh(): a projector that does not answer must not fail setup.
    # A projector that rejects Home Assistant still starts the reauthentication flow.
    await coordinator.async_refresh()
    entry.runtime_data = coordinator

    @callback
    def _async_update_sw_version() -> None:
        # The firmware version is only known after the first successful poll.
        version = coordinator.data.software_version if coordinator.data else None
        if not version:
            return
        device_registry = dr.async_get(hass)
        for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
            if device.sw_version != version:
                device_registry.async_update_device(device.id, sw_version=version)

    entry.async_on_unload(coordinator.async_add_listener(_async_update_sw_version))
    if weather_entity := entry.options.get(CONF_WEATHER_ENTITY):
        coordinator.weather = WeatherPoster(hass, coordinator, weather_entity)
        entry.async_on_unload(coordinator.weather.async_start())
    # Changed options take effect by setting the entry up again. Other updates must not reload it: Home Assistant
    # also updates the entry whenever another Bluetooth adapter or proxy discovers the projector.
    options = dict(entry.options)

    async def _async_entry_updated(hass: HomeAssistant, entry: LSPXConfigEntry) -> None:
        if dict(entry.options) != options:
            await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LSPXConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
