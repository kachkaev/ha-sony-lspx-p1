"""Setup and entity tests against the fake projector."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from homeassistant.components.bluetooth import async_get_advertisement_callback
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

from custom_components.sony_lspx_p1.const import DOMAIN
from custom_components.sony_lspx_p1.coordinator import STUCK_POLLS_BEFORE_ISSUE
from custom_components.sony_lspx_p1.protocol import ErrorCode, Op

from .conftest import make_service_info
from .fake_projector import FakeBleakClient, FakeProjector

pytestmark = pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")

MP = "media_player.lspx_p1_1a2b"


@pytest.fixture
async def loaded(hass: HomeAssistant, config_entry: MockConfigEntry):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    yield config_entry
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_entities_reflect_projector(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    mp = hass.states.get(MP)
    assert mp.state == STATE_ON
    assert mp.attributes["volume_level"] == 0.67
    assert mp.attributes["source"] == "HDMI"
    assert hass.states.get("number.lspx_p1_1a2b_brightness").state == "81"
    focus = hass.states.get("number.lspx_p1_1a2b_focus")
    assert focus.state == "-370"
    assert focus.attributes["min"] == -383
    assert focus.attributes["max"] == 149
    assert hass.states.get("number.lspx_p1_1a2b_keystone").state == "5"
    assert hass.states.get("switch.lspx_p1_1a2b_auto_focus").state == STATE_ON
    assert hass.states.get("switch.lspx_p1_1a2b_auto_brightness").state == STATE_OFF
    assert hass.states.get("select.lspx_p1_1a2b_screen_rotation").state == "normal"
    assert hass.states.get("select.lspx_p1_1a2b_picture_mode").state == "preset_1"
    assert hass.states.get("sensor.lspx_p1_1a2b_battery").state == "100"
    assert hass.states.get("sensor.lspx_p1_1a2b_charge_state").state == "full"
    # Picture parameters only apply to the custom modes.
    assert hass.states.get("number.lspx_p1_1a2b_picture_contrast").state == STATE_UNAVAILABLE


async def test_power_and_volume(hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector) -> None:
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    assert projector.power == 0
    assert hass.states.get(MP).state == STATE_OFF

    await hass.services.async_call("media_player", "turn_on", {ATTR_ENTITY_ID: MP}, blocking=True)
    assert projector.power == 0x10
    assert hass.states.get(MP).state == STATE_ON

    await hass.services.async_call(
        "media_player", "volume_set", {ATTR_ENTITY_ID: MP, "volume_level": 0.3}, blocking=True
    )
    assert projector.volume == 30
    assert hass.states.get(MP).attributes["volume_level"] == 0.3


async def test_sources(hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector) -> None:
    assert hass.states.get(MP).attributes["source_list"] == [
        "HDMI",
        "Weather",
        "Clock 1",
        "Clock 2",
        "Clock 3",
        "Clock 4",
        "A day",
        "Journey",
        "Night dreams",
        "Window",
    ]
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Clock 2"}, blocking=True
    )
    assert (projector.app_state, projector.poster) == (0x10, 9)
    assert hass.states.get(MP).attributes["source"] == "Clock 2"

    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "HDMI"}, blocking=True
    )
    assert projector.topology == 0x30
    assert hass.states.get(MP).attributes["source"] == "HDMI"

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Netflix"}, blocking=True
        )


async def test_hdmi_status_usage_time_and_inactivity(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    hdmi = "sensor.lspx_p1_1a2b_hdmi_status"
    assert hass.states.get(hdmi).state == "connected"
    assert hass.states.get("sensor.lspx_p1_1a2b_usage_time").state == "412.5"
    projector.hdmi = [1, 2]
    await loaded.runtime_data.async_refresh()
    assert hass.states.get(hdmi).state == "no_signal"
    projector.hdmi = [0, 0]
    await loaded.runtime_data.async_refresh()
    assert hass.states.get(hdmi).state == "off"

    switch = "switch.lspx_p1_1a2b_standby_after_inactivity"
    assert hass.states.get(switch).state == STATE_ON
    await hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: switch}, blocking=True)
    assert projector.auto_standby == [3, 0, 0]
    assert hass.states.get(switch).state == STATE_OFF


async def test_poster_outside_the_list_shows_as_poster(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    projector.app_state, projector.poster = 0x10, 13  # My slideshow
    await loaded.runtime_data.async_refresh()
    assert hass.states.get(MP).attributes["source"] == "Poster"


async def test_source_chosen_in_standby_switches_on_first(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
    )
    assert projector.power == 0x10
    assert projector.poster == 10  # not yet: the poster is chosen once a poll sees the projector on
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))
    await hass.async_block_till_done()
    assert (projector.app_state, projector.poster) == (0x10, 6)
    assert hass.states.get(MP).attributes["source"] == "Window"


async def test_manual_focus_turns_auto_focus_off(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    await hass.services.async_call(
        "number", "set_value", {ATTR_ENTITY_ID: "number.lspx_p1_1a2b_focus", "value": -200}, blocking=True
    )
    assert projector.focus == -200
    assert projector.focus_assist == 0
    assert hass.states.get("switch.lspx_p1_1a2b_auto_focus").state == STATE_OFF


async def test_rotation_turns_auto_rotation_off(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: "select.lspx_p1_1a2b_screen_rotation", "option": "rotate_180"},
        blocking=True,
    )
    assert projector.direction == 1
    assert projector.auto_direction == 0
    assert hass.states.get("switch.lspx_p1_1a2b_auto_rotation").state == STATE_OFF


async def test_custom_picture_mode_enables_picture_controls(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: "select.lspx_p1_1a2b_picture_mode", "option": "custom_1"},
        blocking=True,
    )
    assert projector.picture_mode == 16
    contrast = "number.lspx_p1_1a2b_picture_contrast"
    assert hass.states.get(contrast).state == "32"
    await hass.services.async_call("number", "set_value", {ATTR_ENTITY_ID: contrast, "value": 20}, blocking=True)
    assert projector.picture["contrast"] == 20


async def test_switches(hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector) -> None:
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: "switch.lspx_p1_1a2b_auto_brightness"}, blocking=True
    )
    assert projector.auto_brightness == [1, 1]
    await hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: "switch.lspx_p1_1a2b_xloud"}, blocking=True)
    assert projector.xloud == 0


async def test_restrict_adding_smartphones(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    switch = "switch.lspx_p1_1a2b_restrict_adding_smartphones"
    assert hass.states.get(switch).state == STATE_OFF
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: switch}, blocking=True)
    assert projector.restrict == 1
    assert hass.states.get(switch).state == STATE_ON


async def test_smartphones_added(hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector) -> None:
    sensor = hass.states.get("sensor.lspx_p1_1a2b_smartphones_added")
    assert sensor.state == "1"
    assert sensor.attributes["smartphones"] == ["iPhone"]
    assert sensor.attributes["blocked_smartphones"] == []
    assert hass.states.get("sensor.lspx_p1_1a2b_smartphone_names").state == "iPhone"


async def test_bonding_failure_only_affects_its_entities(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    projector.bond_failures = 1
    await loaded.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("switch.lspx_p1_1a2b_restrict_adding_smartphones").state == STATE_UNAVAILABLE
    assert hass.states.get(MP).state == STATE_ON


async def test_lost_pairing_starts_reauth(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector, fake_connection: list[FakeBleakClient]
) -> None:
    projector.bonded = False
    for client in fake_connection:
        client.connected = False
    await loaded.runtime_data.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_busy_projector_marks_entities_unavailable(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    projector.busy = True
    await loaded.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(MP).state == STATE_UNAVAILABLE


async def test_stuck_projector_raises_repair_issue(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    issue_id = f"projector_stuck_{loaded.entry_id}"
    issues = ir.async_get(hass)
    projector.busy = True
    for _ in range(STUCK_POLLS_BEFORE_ISSUE - 1):
        await loaded.runtime_data.async_refresh()
    # Out of range in between: not a sign of a stuck link, but no reason to start counting again either.
    with patch("homeassistant.components.bluetooth.async_ble_device_from_address", return_value=None):
        await loaded.runtime_data.async_refresh()
    assert issues.async_get_issue(DOMAIN, issue_id) is None

    await loaded.runtime_data.async_refresh()
    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert (issue.translation_key, issue.severity, issue.is_fixable) == (
        "projector_stuck",
        ir.IssueSeverity.WARNING,
        False,
    )
    assert issue.translation_placeholders == {"name": "LSPX-P1 1A2B"}

    projector.busy = False  # RESET pressed
    await loaded.runtime_data.async_refresh()
    assert issues.async_get_issue(DOMAIN, issue_id) is None
    assert hass.states.get(MP).state == STATE_ON


async def test_repair_issue_removed_on_unload(
    hass: HomeAssistant, config_entry: MockConfigEntry, projector: FakeProjector
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    projector.busy = True
    for _ in range(STUCK_POLLS_BEFORE_ISSUE):
        await config_entry.runtime_data.async_refresh()
    issue_id = f"projector_stuck_{config_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_diagnostics(hass: HomeAssistant, loaded: MockConfigEntry, hass_client) -> None:
    diag = await get_diagnostics_for_config_entry(hass, hass_client, loaded)
    assert diag["title"] == "LSPX-P1 1A2B"
    assert diag["state"]["volume"] == 67
    assert diag["state"]["errors"] == {}
    assert diag["state"]["controllers"] == [{"id": 0, "name": "**REDACTED**", "allowed": True}]


async def test_command_on_forgotten_pairing_starts_reauth(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector, fake_connection: list[FakeBleakClient]
) -> None:
    projector.bonded = False
    for client in fake_connection:
        client.drop()
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "media_player", "volume_set", {ATTR_ENTITY_ID: MP, "volume_level": 0.2}, blocking=True
        )
    await hass.async_block_till_done()
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_command_on_busy_projector_raises(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    projector.busy = True
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "number", "set_value", {ATTR_ENTITY_ID: "number.lspx_p1_1a2b_keystone", "value": 3}, blocking=True
        )


async def test_power_change_polls_again_while_settling(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    reads_before = sum(1 for op, _ in projector.log if op == 0x0801)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))
    await hass.async_block_till_done()
    assert sum(1 for op, _ in projector.log if op == 0x0801) > reads_before


async def test_loads_while_projector_is_out_of_range(
    hass: HomeAssistant, config_entry: MockConfigEntry, ble_device: BLEDevice
) -> None:
    """A projector that is off or away at startup leaves the entities unavailable, not missing."""
    config_entry.add_to_hass(hass)
    with patch("homeassistant.components.bluetooth.async_ble_device_from_address", return_value=None):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(MP).state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.lspx_p1_1a2b_battery").state == STATE_UNAVAILABLE
    with patch("homeassistant.components.bluetooth.async_ble_device_from_address", return_value=None):
        await config_entry.runtime_data.async_refresh()
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)
    assert device.sw_version is None  # not known before a poll reaches the projector

    # The projector comes back: the next poll brings everything up, firmware version included.
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(MP).state == STATE_ON
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)
    assert device.sw_version == "3.1.15"


async def test_loads_when_connecting_times_out(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    async def failing_connection(*args, **kwargs):
        raise BleakError("LSPXP1_1A2B - AA:BB: Failed to connect after 4 attempt(s): TimeoutError")

    config_entry.add_to_hass(hass)
    with patch("custom_components.sony_lspx_p1.client.establish_connection", failing_connection):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(MP).state == STATE_UNAVAILABLE


async def test_white_balance_and_picture_reset(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    red = "number.lspx_p1_1a2b_white_balance_red"
    reset = "button.lspx_p1_1a2b_reset_picture_settings"
    assert hass.states.get(red).state == STATE_UNAVAILABLE  # preset mode
    assert hass.states.get(reset).state == STATE_UNAVAILABLE
    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: "select.lspx_p1_1a2b_picture_mode", "option": "custom_2"},
        blocking=True,
    )
    assert hass.states.get(red).state == "100"
    assert hass.states.get(red).attributes["min"] == 70
    await hass.services.async_call("number", "set_value", {ATTR_ENTITY_ID: red, "value": 85}, blocking=True)
    assert projector.rgb == [85, 100, 100]
    assert hass.states.get("number.lspx_p1_1a2b_white_balance_green").state == "100"

    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: reset}, blocking=True)
    await hass.async_block_till_done()
    assert projector.resets == [17]
    assert hass.states.get(red).state == "100"


async def test_keystone_reset(hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector) -> None:
    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: "button.lspx_p1_1a2b_reset_keystone"}, blocking=True
    )
    assert projector.keystone == 0
    assert hass.states.get("number.lspx_p1_1a2b_keystone").state == "0"


async def test_changes_right_after_power_on_wait_for_the_projector(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    await hass.services.async_call("media_player", "turn_on", {ATTR_ENTITY_ID: MP}, blocking=True)
    projector.transition_replies = 3  # still starting up: commands are rejected for a moment
    await hass.services.async_call(
        "media_player", "volume_set", {ATTR_ENTITY_ID: MP, "volume_level": 0.25}, blocking=True
    )
    assert projector.volume == 25


async def test_rejected_command_fails_when_not_starting_up(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    projector.transition_replies = 1
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "media_player", "volume_set", {ATTR_ENTITY_ID: MP, "volume_level": 0.25}, blocking=True
        )


RESTART = "button.lspx_p1_1a2b_restart"


async def test_restart(hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector) -> None:
    state = hass.states.get(RESTART)
    assert state.attributes["device_class"] == "restart"
    entry = er.async_get(hass).async_get(RESTART)
    assert entry.entity_category is EntityCategory.CONFIG
    reads_before = projector.log.count((0x0801, b""))

    # The fake projector drops the link instead of answering, as a restarting one may.
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: RESTART}, blocking=True)
    assert projector.log.count((0x0F00, b"")) == 1

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))
    await hass.async_block_till_done()
    assert projector.log.count((0x0801, b"")) > reads_before
    assert hass.states.get(MP).state == STATE_ON


async def test_restart_available_while_polls_fail(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    projector.busy = True
    await loaded.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(MP).state == STATE_UNAVAILABLE
    assert hass.states.get(RESTART).state != STATE_UNAVAILABLE


async def test_failing_restart_raises(hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector) -> None:
    projector.busy = True
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: RESTART}, blocking=True)
    assert err.value.translation_key == "projector_busy"

    async def failing_connection(*args, **kwargs):
        raise BleakError("Failed to connect")

    with (
        patch("custom_components.sony_lspx_p1.client.establish_connection", failing_connection),
        pytest.raises(HomeAssistantError) as err,
    ):
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: RESTART}, blocking=True)
    assert err.value.translation_key == "communication_error"


async def test_follows_the_adapter_or_proxy_that_sees_the_projector(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    proxy_device = BLEDevice("AA:BB:CC:DD:EE:FF", "LSPXP1_1A2B", details={"source": "esphome-proxy"})
    async_get_advertisement_callback(hass)(make_service_info(proxy_device))
    await hass.async_block_till_done()
    assert loaded.runtime_data.client._ble_device is proxy_device


async def _choose_window_in_standby(hass: HomeAssistant) -> None:
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
    )


async def test_source_chosen_in_standby_is_dropped_when_it_takes_too_long(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    with patch("custom_components.sony_lspx_p1.coordinator.PENDING_SOURCE_TIMEOUT", -1):
        await _choose_window_in_standby(hass)
    await loaded.runtime_data.async_refresh()
    assert projector.power == 0x10
    assert projector.poster == 10  # not switched long after it was chosen


async def test_source_chosen_in_standby_that_fails_leaves_the_poll_alone(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector, caplog: pytest.LogCaptureFixture
) -> None:
    await _choose_window_in_standby(hass)
    projector.rejections = {Op.SET_POSTER: ErrorCode.INVALID_ARGUMENT}
    await loaded.runtime_data.async_refresh()
    assert loaded.runtime_data.last_update_success
    assert hass.states.get(MP).state == STATE_ON
    assert projector.poster == 10
    assert "switching the source after power-on failed" in caplog.text


async def test_unreadable_firmware_version_leaves_the_device_alone(
    hass: HomeAssistant, config_entry: MockConfigEntry, projector: FakeProjector
) -> None:
    projector.rejections = {Op.GET_SOC_SOFTWARE_VERSION: ErrorCode.RUNTIME_ERROR}
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    projector.volume = 50  # a poll with news (unchanged data does not notify the listeners)
    await config_entry.runtime_data.async_refresh()
    assert hass.states.get(MP).attributes["volume_level"] == 0.5
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)
    assert device.sw_version is None
    assert config_entry.runtime_data.data.errors.keys() == {"software_version"}
