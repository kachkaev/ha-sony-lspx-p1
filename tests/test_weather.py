"""The Weather poster following a weather entity (options flow, when codes are sent, failures)."""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from homeassistant.components import weather
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, CONF_ADDRESS, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

from custom_components.sony_lspx_p1.client import LSPXClient
from custom_components.sony_lspx_p1.const import CONF_WEATHER_ENTITY, DOMAIN
from custom_components.sony_lspx_p1.protocol import WEATHER_CODES, ErrorCode, Op
from custom_components.sony_lspx_p1.weather import POSTER_CODES

from .conftest import ADDRESS
from .fake_projector import FakeBleakClient, FakeProjector

pytestmark = pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")

MP = "media_player.lspx_p1_1a2b"
WEATHER = "weather.home"


def sent(projector: FakeProjector) -> list[int]:
    """The codes sent with SetWeather, in order."""
    return [struct.unpack(">H", payload)[0] for op, payload in projector.log if op == Op.SET_WEATHER]


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    assert entry.state is ConfigEntryState.LOADED
    return entry


@pytest.fixture
async def plain(hass: HomeAssistant, config_entry: MockConfigEntry):
    """Set up without options, as before the Weather poster option existed."""
    hass.states.async_set(WEATHER, "sunny")
    yield await setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)


def weather_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS},
        options={CONF_WEATHER_ENTITY: WEATHER},
        title="LSPX-P1 1A2B",
    )


@pytest.fixture
async def following(hass: HomeAssistant):
    """Set up with a weather entity that is sunny; the projector is on, showing HDMI."""
    hass.states.async_set(WEATHER, "sunny")
    entry = await setup(hass, weather_entry())
    yield entry
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
async def on_weather(hass: HomeAssistant, projector: FakeProjector):
    """As ``following``, but the projector shows the Weather poster when Home Assistant starts."""
    projector.app_state, projector.poster = 0x10, 11
    hass.states.async_set(WEATHER, "sunny")
    entry = await setup(hass, weather_entry())
    yield entry
    assert await hass.config_entries.async_unload(entry.entry_id)


async def select_source(hass: HomeAssistant, source: str) -> None:
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": source}, blocking=True
    )
    await settle(hass)


async def power(hass: HomeAssistant, on: bool) -> None:
    await hass.services.async_call("media_player", "turn_on" if on else "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    await settle(hass)


async def set_condition(hass: HomeAssistant, condition: str, **attributes: object) -> None:
    hass.states.async_set(WEATHER, condition, attributes)
    await settle(hass)


def test_every_condition_but_exceptional_has_a_poster_code() -> None:
    conditions = {
        value
        for name, value in vars(weather).items()
        if name.startswith("ATTR_CONDITION_") and name != "ATTR_CONDITION_CLASS"
    }
    assert set(POSTER_CODES) == conditions - {weather.ATTR_CONDITION_EXCEPTIONAL}
    assert set(POSTER_CODES.values()) <= set(WEATHER_CODES)


async def test_options_flow_chooses_and_clears_the_weather_entity(
    hass: HomeAssistant, plain: MockConfigEntry, projector: FakeProjector
) -> None:
    assert plain.runtime_data.weather is None
    result = await hass.config_entries.options.async_init(plain.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_WEATHER_ENTITY: WEATHER})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert plain.options == {CONF_WEATHER_ENTITY: WEATHER}
    await settle(hass)  # the entry is set up again with the option
    assert plain.state is ConfigEntryState.LOADED
    assert plain.runtime_data.weather is not None
    assert sent(projector) == []  # HDMI shows
    await select_source(hass, "Weather")
    assert sent(projector) == [1]

    result = await hass.config_entries.options.async_init(plain.entry_id)
    (key,) = result["data_schema"].schema
    assert key.description == {"suggested_value": WEATHER}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert plain.options == {}
    await settle(hass)
    assert plain.runtime_data.weather is None
    await set_condition(hass, "rainy")
    await select_source(hass, "Weather")
    assert sent(projector) == [1]


async def test_other_entry_updates_do_not_reload(hass: HomeAssistant, plain: MockConfigEntry) -> None:
    """Home Assistant updates the entry when another adapter or proxy discovers the projector, for example."""
    coordinator = plain.runtime_data
    hass.config_entries.async_update_entry(plain, title="Living room projector")
    await settle(hass)
    assert plain.runtime_data is coordinator


async def test_nothing_is_sent_without_a_weather_entity(
    hass: HomeAssistant, plain: MockConfigEntry, projector: FakeProjector, hass_client
) -> None:
    await set_condition(hass, "rainy")
    await select_source(hass, "Weather")
    await power(hass, False)
    await set_condition(hass, "snowy")
    await power(hass, True)
    await plain.runtime_data.async_refresh()
    await settle(hass)
    assert sent(projector) == []
    assert Op.GET_WEATHER not in {op for op, _ in projector.log}
    diag = await get_diagnostics_for_config_entry(hass, hass_client, plain)
    assert "weather" not in diag


async def test_nothing_is_sent_while_another_source_shows(
    hass: HomeAssistant, following: MockConfigEntry, projector: FakeProjector
) -> None:
    await set_condition(hass, "rainy")
    await power(hass, False)
    await set_condition(hass, "pouring")
    await power(hass, True)
    await select_source(hass, "Clock 1")
    await set_condition(hass, "snowy")
    projector.volume = 20  # a poll with news
    await following.runtime_data.async_refresh()
    await settle(hass)
    assert sent(projector) == []
    await select_source(hass, "Weather")
    assert sent(projector) == [11]


async def test_condition_changes_are_sent(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector
) -> None:
    assert sent(projector) == [1]  # the Weather poster showed when Home Assistant started
    await set_condition(hass, "rainy")
    assert sent(projector) == [1, 7]
    assert projector.weather == 7
    await set_condition(hass, "rainy", temperature=12)  # only an attribute changed
    await set_condition(hass, "pouring")
    await set_condition(hass, "hail")  # the same picture
    await set_condition(hass, "clear-night")
    assert sent(projector) == [1, 7, 8, 1]
    assert hass.states.get(MP).state == STATE_ON


@pytest.mark.parametrize("condition", ["exceptional", STATE_UNAVAILABLE, STATE_UNKNOWN, "tornado"])
async def test_conditions_without_a_picture_change_nothing(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector, condition: str
) -> None:
    await set_condition(hass, condition)
    await select_source(hass, "Weather")
    assert sent(projector) == [1]


async def test_missing_weather_entity_changes_nothing(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector
) -> None:
    hass.states.async_remove(WEATHER)
    await settle(hass)
    await select_source(hass, "Weather")
    assert sent(projector) == [1]
    assert on_weather.runtime_data.weather.diagnostics()["condition"] is None


async def test_choosing_the_weather_poster_sends_the_code(
    hass: HomeAssistant, following: MockConfigEntry, projector: FakeProjector
) -> None:
    await select_source(hass, "Weather")
    assert sent(projector) == [1]
    ops = [op for op, _ in projector.log]
    assert ops.index(Op.SET_WEATHER) > ops.index(Op.SET_POSTER)  # after the switch
    await select_source(hass, "Weather")  # chosen again while it shows
    assert sent(projector) == [1, 1]
    await select_source(hass, "Clock 1")
    await select_source(hass, "Weather")  # back to it: sent again, although the same
    assert sent(projector) == [1, 1, 1]
    await select_source(hass, "HDMI")
    await set_condition(hass, "rainy")
    assert sent(projector) == [1, 1, 1]
    await select_source(hass, "Weather")
    assert sent(projector) == [1, 1, 1, 7]


async def test_weather_poster_chosen_elsewhere_is_sent_on_the_next_poll(
    hass: HomeAssistant, following: MockConfigEntry, projector: FakeProjector
) -> None:
    projector.app_state, projector.poster = 0x10, 11  # chosen in the app or on the projector
    await following.runtime_data.async_refresh()
    await settle(hass)
    assert sent(projector) == [1]
    projector.volume = 20  # a poll with news, still showing the Weather poster
    await following.runtime_data.async_refresh()
    await settle(hass)
    assert sent(projector) == [1]


async def test_weather_poster_chosen_in_standby_is_sent_once_shown(
    hass: HomeAssistant, following: MockConfigEntry, projector: FakeProjector
) -> None:
    await power(hass, False)
    await set_condition(hass, "cloudy")
    await select_source(hass, "Weather")  # switches on, then a poll applies the poster
    assert sent(projector) == []  # on, but still showing HDMI
    await following.runtime_data.async_refresh()
    await settle(hass)
    assert (projector.app_state, projector.poster) == (0x10, 11)
    assert sent(projector) == [5]


async def test_switching_on_onto_the_weather_poster_sends_the_code(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector
) -> None:
    await power(hass, False)
    await set_condition(hass, "snowy")
    assert sent(projector) == [1]
    await power(hass, True)
    assert sent(projector) == [1, 11]
    await power(hass, False)
    await power(hass, True)  # the poster appears again
    assert sent(projector) == [1, 11, 11]


async def test_nothing_is_sent_while_polls_fail(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector
) -> None:
    projector.busy = True
    await on_weather.runtime_data.async_refresh()
    await set_condition(hass, "rainy")
    projector.busy = False
    assert sent(projector) == [1]
    await on_weather.runtime_data.async_refresh()  # back: the change is sent
    await settle(hass)
    assert sent(projector) == [1, 7]
    projector.busy = True
    await on_weather.runtime_data.async_refresh()
    projector.busy = False
    await on_weather.runtime_data.async_refresh()  # back, nothing new
    await settle(hass)
    assert sent(projector) == [1, 7]


async def test_failed_sends_are_only_logged(
    hass: HomeAssistant,
    on_weather: MockConfigEntry,
    projector: FakeProjector,
    fake_connection: list[FakeBleakClient],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", "custom_components.sony_lspx_p1.weather")
    projector.rejections = {Op.SET_WEATHER: ErrorCode.UNKNOWN_COMMAND}
    await set_condition(hass, "rainy")
    assert "sending weather code 7 failed" in caplog.text
    diag = on_weather.runtime_data.weather.diagnostics()
    assert diag["sent"] == 1
    assert "UNKNOWN_COMMAND" in diag["last_error"]

    for client in fake_connection:
        client.drop()
    with patch("custom_components.sony_lspx_p1.client.establish_connection", side_effect=BleakError("out of range")):
        await set_condition(hass, "snowy")
    assert "sending weather code 11 failed: BleakError" in caplog.text

    projector.volume = 30  # polls and commands are unaffected
    await on_weather.runtime_data.async_refresh()
    assert on_weather.runtime_data.last_update_success
    assert hass.states.get(MP).attributes["volume_level"] == 0.3
    projector.rejections = {}
    await set_condition(hass, "pouring")
    assert projector.weather == 8
    assert on_weather.runtime_data.weather.last_error is None


async def test_diagnostics_read_the_projector_code(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector, hass_client
) -> None:
    diag = await get_diagnostics_for_config_entry(hass, hass_client, on_weather)
    assert diag["weather"] == {
        "entity_id": WEATHER,
        "condition": "sunny",
        "code": 1,
        "sent": 1,
        "last_error": None,
        "projector_code": 1,
    }
    assert "weather" not in diag["state"]  # not read by the polls
    projector.rejections = {Op.GET_WEATHER: ErrorCode.UNKNOWN_COMMAND}
    diag = await get_diagnostics_for_config_entry(hass, hass_client, on_weather)
    assert diag["weather"]["projector_code"].startswith("error: CommandRejected")


async def test_sending_waits_for_the_projector_to_start_up(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector
) -> None:
    coordinator = on_weather.runtime_data
    await power(hass, False)
    await power(hass, True)  # warming up from here on
    projector.transition_replies = 2
    assert await coordinator.async_set_weather(9)
    assert projector.weather == 9
    coordinator.data.app_state = 0x60  # HDMI by now
    assert not await coordinator.async_set_weather(3)
    coordinator.data.app_state, coordinator.data.power = 0x10, False
    assert not await coordinator.async_set_weather(3)
    assert projector.weather == 9


async def test_unknown_command_does_not_hold_up_power_changes(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector
) -> None:
    """A projector without SetWeather answers UNKNOWN_COMMAND, as one still starting up does: retries do not block."""
    coordinator = on_weather.runtime_data
    projector.rejections = {Op.SET_WEATHER: ErrorCode.UNKNOWN_COMMAND}
    with patch("custom_components.sony_lspx_p1.coordinator.WARMUP_RETRY_DELAY", 0.05):
        coordinator._warmup_until = asyncio.get_running_loop().time() + 5
        task = hass.async_create_task(coordinator.async_set_weather(7))
        await asyncio.sleep(0.12)
        assert not task.done()  # still retrying
        await coordinator.async_set_power(False)
        assert projector.power == 0
        assert await task is False  # gives up once the projector is off


async def test_client_weather_commands(
    ble_device: BLEDevice, projector: FakeProjector, fake_connection: list[FakeBleakClient]
) -> None:
    client = LSPXClient(ble_device, idle_disconnect=False)
    await client.set_weather(7)
    assert projector.log[-1] == (Op.SET_WEATHER, b"\x00\x07")
    assert Op.SET_WEATHER == 0x0069
    assert await client.get_weather() == 7
    assert projector.log[-1] == (0x006A, b"")
    await client.disconnect()
