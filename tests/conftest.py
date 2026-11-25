"""Shared fixtures."""

from __future__ import annotations

import os
from collections.abc import Generator
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sony_lspx_p1.client import make_ble_device
from custom_components.sony_lspx_p1.const import DOMAIN

from .fake_projector import FakeBleakClient, FakeProjector

ADDRESS = "AA:BB:CC:DD:EE:FF"
NAME = "LSPXP1_1A2B"

# What `pytest -n auto` means here. Every worker also drives its own headless Chrome for the card tests, so one
# per core overloads a laptop, the more so when several checkouts run the tests at once.
AUTO_WORKERS = 2


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers() -> int:
    """Cap `-n auto` (and `-n logical`); `-n 6` or PYTEST_XDIST_AUTO_NUM_WORKERS ask for more."""
    return int(os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS") or AUTO_WORKERS)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components."""


@pytest.fixture(autouse=True)
def fast_pairing_retries() -> Generator[None]:
    with (
        patch("custom_components.sony_lspx_p1.client.PAIRING_RETRY_DELAY", 0),
        patch("custom_components.sony_lspx_p1.client.UNPAIR_SETTLE", 0),
        patch("custom_components.sony_lspx_p1.client.STOP_APP_SETTLE", 0),
        patch("custom_components.sony_lspx_p1.coordinator.WARMUP_RETRY_DELAY", 0),
        # Real time, kept short: the fake projector answers at once.
        patch("custom_components.sony_lspx_p1.coordinator.STARTUP_PROBE_INTERVAL", 0.01),
    ):
        yield


@pytest.fixture
def ble_device() -> BLEDevice:
    return make_ble_device(ADDRESS, NAME)


def make_service_info(
    ble_device: BLEDevice, name: str = NAME, service_uuids: list[str] | None = None, rssi: int = -60
) -> BluetoothServiceInfoBleak:
    if service_uuids is None:
        service_uuids = ["000000ee-0000-1000-8000-00805f9b34fb"]
    adv = AdvertisementData(
        local_name=name,
        manufacturer_data={0x012D: bytes.fromhex("ee005ab8446b2a0fe1c55a6d871f00")},
        service_data={},
        service_uuids=service_uuids,
        tx_power=None,
        rssi=rssi,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=name,
        address=ADDRESS,
        rssi=rssi,
        manufacturer_data=adv.manufacturer_data,
        service_data={},
        service_uuids=adv.service_uuids,
        source="local",
        device=ble_device,
        advertisement=adv,
        connectable=True,
        time=0,
        tx_power=None,
    )


@pytest.fixture
def service_info(ble_device: BLEDevice) -> BluetoothServiceInfoBleak:
    return make_service_info(ble_device)


@pytest.fixture
def projector() -> FakeProjector:
    return FakeProjector()


@pytest.fixture
def fake_connection(projector: FakeProjector) -> Generator[list[FakeBleakClient]]:
    """Make establish_connection return fake clients bound to ``projector``."""
    clients: list[FakeBleakClient] = []

    async def establish_connection(client_class, device, name, disconnected_callback=None, **kwargs):
        client = FakeBleakClient(
            projector,
            disconnected_callback=disconnected_callback,
            use_services_cache=kwargs.get("use_services_cache", True),
        )
        clients.append(client)
        return client

    with patch("custom_components.sony_lspx_p1.client.establish_connection", establish_connection):
        yield clients


@pytest.fixture
def mock_ble_lookup(ble_device: BLEDevice) -> Generator[None]:
    with (
        patch("homeassistant.components.bluetooth.async_ble_device_from_address", return_value=ble_device),
        patch("custom_components.sony_lspx_p1.config_flow.async_ble_device_from_address", return_value=ble_device),
    ):
        yield


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS}, title="LSPX-P1 1A2B")
