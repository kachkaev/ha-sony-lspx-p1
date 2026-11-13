"""Config flow tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sony_lspx_p1 import async_setup_entry as _real_setup_entry
from custom_components.sony_lspx_p1.const import DOMAIN
from custom_components.sony_lspx_p1.protocol import Op

from .conftest import ADDRESS, make_service_info
from .fake_projector import FakeBleakClient, FakeProjector

pytestmark = pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup")


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.sony_lspx_p1.async_setup_entry", return_value=True):
        yield


async def test_bluetooth_discovery_pairs_and_creates_entry(
    hass: HomeAssistant,
    service_info: BluetoothServiceInfoBleak,
    projector: FakeProjector,
    fake_connection: list[FakeBleakClient],
) -> None:
    projector.bonded = False
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "LSPX-P1 1A2B"
    assert result["data"] == {CONF_ADDRESS: ADDRESS}
    assert result["result"].unique_id == ADDRESS
    assert projector.bonded is True
    assert list(projector.controllers.values())[-1] == "Home Assistant"


async def test_pairing_refused_shows_error(
    hass: HomeAssistant,
    service_info: BluetoothServiceInfoBleak,
    projector: FakeProjector,
    fake_connection: list[FakeBleakClient],
) -> None:
    projector.bonded = False

    async def refuse(self):
        raise NotImplementedError

    with patch.object(FakeBleakClient, "write_gatt_char", autospec=True) as write:

        async def fake_write(self, char, data, response=True):
            # Swallow the bonding request: the projector never confirms.
            return None

        write.side_effect = fake_write
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "pairing_failed"}


async def test_user_flow(
    hass: HomeAssistant,
    service_info: BluetoothServiceInfoBleak,
    fake_connection: list[FakeBleakClient],
) -> None:
    with patch("custom_components.sony_lspx_p1.config_flow.async_discovered_service_info", return_value=[service_info]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: ADDRESS})
    assert result["step_id"] == "pair"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_no_devices(hass: HomeAssistant) -> None:
    with patch("custom_components.sony_lspx_p1.config_flow.async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_already_configured(
    hass: HomeAssistant, service_info: BluetoothServiceInfoBleak, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    projector: FakeProjector,
    fake_connection: list[FakeBleakClient],
) -> None:
    config_entry.add_to_hass(hass)
    projector.bonded = False
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert projector.bonded is True


async def test_user_flow_skips_configured_projectors(
    hass: HomeAssistant, service_info: BluetoothServiceInfoBleak, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    with patch("custom_components.sony_lspx_p1.config_flow.async_discovered_service_info", return_value=[service_info]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_other_sony_devices_are_ignored(hass: HomeAssistant, ble_device: BLEDevice) -> None:
    other = make_service_info(ble_device, name="", service_uuids=[])
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=other)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


async def test_connection_error_is_shown_with_details(
    hass: HomeAssistant, service_info: BluetoothServiceInfoBleak, fake_connection: list[FakeBleakClient]
) -> None:
    async def fail(*args, **kwargs):
        raise TimeoutError

    with patch("custom_components.sony_lspx_p1.client.establish_connection", fail):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "cannot_connect"}
    assert result["description_placeholders"]["error"] == "connecting: TimeoutError"

    # The projector is reachable again: submitting once more pairs.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_discovery_without_a_name(
    hass: HomeAssistant, ble_device: BLEDevice, fake_connection: list[FakeBleakClient]
) -> None:
    """Passive scanning sees no name; Sony's data and the service UUID identify the projector."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=make_service_info(ble_device, name="")
    )
    assert result["step_id"] == "pair"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "LSPX-P1 EE:FF"


async def test_projector_out_of_reach_when_pairing(
    hass: HomeAssistant, service_info: BluetoothServiceInfoBleak, fake_connection: list[FakeBleakClient]
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info)
    with patch("custom_components.sony_lspx_p1.config_flow.async_ble_device_from_address", return_value=None):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "device_not_found"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_reauth_of_a_loaded_entry_retries_after_an_error(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    projector: FakeProjector,
    fake_connection: list[FakeBleakClient],
) -> None:
    config_entry.add_to_hass(hass)
    with patch("custom_components.sony_lspx_p1.async_setup_entry", _real_setup_entry):  # undo no_setup
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        assert config_entry.state is ConfigEntryState.LOADED
        running = config_entry.runtime_data.client
        await running.command(Op.GET_POWER)
        assert running.is_connected

        projector.bonded = False
        projector.pairing_via_call = projector.pairing_via_read = False
        result = await config_entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "pairing_failed"}
        assert "explicit pair failed while pairing" in result["description_placeholders"]["error"]
        # The running entry let go of the projector, so pairing could use the link.
        assert not running.is_connected

        projector.pairing_via_call = True
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        await hass.async_block_till_done()
    assert projector.bonded is True
    assert config_entry.state is ConfigEntryState.LOADED
