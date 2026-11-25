"""The Weather poster following a weather entity (options flow, when codes are sent, failures)."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from homeassistant.components import weather
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, CONF_ADDRESS, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

from custom_components.sony_lspx_p1.client import POLL_LINGER, REDRAW_POSTER, LSPXClient, RedrawStoppedError
from custom_components.sony_lspx_p1.const import CONF_WEATHER_ENTITY, DOMAIN
from custom_components.sony_lspx_p1.protocol import WEATHER_CODES, ErrorCode, Op
from custom_components.sony_lspx_p1.weather import POSTER_CODES, RETRY_DELAY

from .conftest import ADDRESS
from .fake_projector import FakeBleakClient, FakeProjector

pytestmark = pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")

MP = "media_player.lspx_p1_1a2b"
WEATHER = "weather.home"


def sent(projector: FakeProjector) -> list[int]:
    """The codes sent with SetWeather, in order."""
    return [struct.unpack(">H", payload)[0] for op, payload in projector.log if op == Op.SET_WEATHER]


def posters(projector: FakeProjector) -> list[int]:
    """The posters chosen with SetPoster, in order."""
    return [struct.unpack(">H", payload)[0] for op, payload in projector.log if op == Op.SET_POSTER]


def follow_sources(hass: HomeAssistant) -> list[str | None]:
    """Every source the media player shows from now on, other than the Weather poster."""
    seen: list[str | None] = []

    @callback
    def changed(event: Event[EventStateChangedData]) -> None:
        if (state := event.data["new_state"]) is not None and state.attributes.get("source") != "Weather":
            seen.append(state.attributes.get("source"))

    async_track_state_change_event(hass, MP, changed)
    return seen


def watch_in_between(client: LSPXClient) -> tuple[asyncio.Event, Any]:
    """An event set once the poster in between is chosen, and the patch that sets it."""
    shown = asyncio.Event()
    set_poster = client.set_poster

    async def watched(theme: int) -> None:
        await set_poster(theme)
        if theme == REDRAW_POSTER:
            shown.set()

    return shown, patch.object(client, "set_poster", watched)


@pytest.fixture(autouse=True)
def no_redraw_pause() -> Iterator[None]:
    with patch("custom_components.sony_lspx_p1.client.REDRAW_PAUSE", 0):
        yield


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


async def test_condition_changes_are_sent_and_drawn(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector
) -> None:
    assert sent(projector) == [1]  # the Weather poster showed when Home Assistant started
    assert projector.drawn_weather == 1
    sources = follow_sources(hass)
    projector.log.clear()
    await set_condition(hass, "rainy")
    # Checked afresh, stored, then drawn by showing the poster in between and the Weather poster again.
    assert projector.log == [
        (Op.GET_POWER, b""),
        (Op.GET_APP_STATE, b""),
        (Op.GET_POSTER, b""),
        (Op.SET_WEATHER, b"\x00\x07"),
        (Op.SET_POSTER, struct.pack(">H", REDRAW_POSTER)),
        (Op.GET_POWER, b""),
        (Op.SET_POSTER, struct.pack(">H", 11)),
    ]
    assert (projector.weather, projector.drawn_weather, projector.poster) == (7, 7, 11)
    await set_condition(hass, "rainy", temperature=12)  # only an attribute changed
    await set_condition(hass, "pouring")
    await set_condition(hass, "hail")
    await set_condition(hass, "snowy-rainy")  # the same picture
    await set_condition(hass, "clear-night")
    assert sent(projector) == [7, 9, 8, 2]
    assert projector.drawn_weather == 2
    assert hass.states.get(MP).state == STATE_ON
    assert sources == []  # the source never showed the poster in between
    assert hass.states.get(MP).attributes["source"] == "Weather"
    assert on_weather.runtime_data.weather.diagnostics()["sent"] == 2


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
    assert posters(projector) == [11]  # drawn as the poster appears: not shown again
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
    assert posters(projector) == [REDRAW_POSTER, 11]  # it showed the code it held before: drawn again
    assert projector.drawn_weather == 1
    projector.volume = 20  # a poll with news, still showing the Weather poster
    await following.runtime_data.async_refresh()
    await settle(hass)
    assert sent(projector) == [1]
    # Chosen elsewhere again with the code it holds: it draws that one as it appears.
    projector.poster = 10
    await following.runtime_data.async_refresh()
    projector.poster = 11
    await following.runtime_data.async_refresh()
    await settle(hass)
    assert sent(projector) == [1]
    assert posters(projector) == [REDRAW_POSTER, 11]


async def test_home_assistant_starting_does_not_redraw_a_poster_that_holds_the_code(
    hass: HomeAssistant, projector: FakeProjector
) -> None:
    projector.app_state, projector.poster, projector.weather = 0x10, 11, 1
    hass.states.async_set(WEATHER, "sunny")
    entry = await setup(hass, weather_entry())
    assert Op.GET_WEATHER in {op for op, _ in projector.log}
    assert sent(projector) == []
    assert posters(projector) == []
    assert entry.runtime_data.weather.sent == 1
    await set_condition(hass, "rainy")
    assert sent(projector) == [7]
    assert projector.drawn_weather == 7
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_weather_poster_chosen_in_standby_is_sent_once_shown(
    hass: HomeAssistant, following: MockConfigEntry, projector: FakeProjector
) -> None:
    await power(hass, False)
    await set_condition(hass, "cloudy")
    await select_source(hass, "Weather")  # switches on; once the main board answers, a poll applies the poster
    await settle(hass)
    assert (projector.app_state, projector.poster) == (0x10, 11)
    assert sent(projector) == [5]


async def test_switching_on_onto_the_weather_poster_sends_the_code(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector
) -> None:
    await power(hass, False)
    await set_condition(hass, "snowy")
    assert sent(projector) == [1]
    await power(hass, True)  # it appears with the code it held, then is drawn again with the new one
    assert sent(projector) == [1, 11]
    assert projector.drawn_weather == 11
    await power(hass, False)
    await power(hass, True)  # the poster appears again, with the code it holds: nothing to send
    assert sent(projector) == [1, 11]


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
    assert projector.weather == 9
    assert on_weather.runtime_data.weather.last_error is None


async def test_diagnostics_read_the_projector_code(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector, hass_client
) -> None:
    client = on_weather.runtime_data.client
    with patch.object(client, "get_weather", wraps=client.get_weather) as get_weather:
        diag = await get_diagnostics_for_config_entry(hass, hass_client, on_weather)
    get_weather.assert_awaited_once_with(linger=POLL_LINGER, expected=())  # not kept open 15 s after a download
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
    # In deep standby the read answers TIMEOUT, as the polls' do: expected, so not the link's last command error.
    projector.rejections = {}
    await power(hass, False)
    projector.main_board_asleep = True
    last_error = client.stats.last_command_error
    expected = client.stats.expected_rejections.get("TIMEOUT", 0)
    diag = await get_diagnostics_for_config_entry(hass, hass_client, on_weather)
    assert diag["weather"]["projector_code"].startswith("error: ProjectorBusyError")
    assert client.stats.expected_rejections["TIMEOUT"] == expected + 1
    assert client.stats.last_command_error == last_error


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


async def test_client_redraws_a_poster(
    ble_device: BLEDevice, projector: FakeProjector, fake_connection: list[FakeBleakClient]
) -> None:
    client = LSPXClient(ble_device, idle_disconnect=False)
    projector.app_state, projector.poster, projector.weather = 0x10, 11, 4
    await client.set_weather(9)
    assert projector.drawn_weather is None
    await client.redraw_poster(11, pause=0)
    assert posters(projector) == [REDRAW_POSTER, 11]
    assert projector.drawn_weather == 9

    # Off, or switching power (rejected): not chosen again, as that would switch it on.
    projector.power = 0
    with pytest.raises(RedrawStoppedError, match="standby"):
        await client.redraw_poster(11, pause=0)
    projector.power = 0x10
    projector.rejections = {Op.GET_POWER: ErrorCode.UNKNOWN_COMMAND}
    with pytest.raises(RedrawStoppedError, match="rejected"):
        await client.redraw_poster(11, pause=0)
    assert posters(projector) == [REDRAW_POSTER, 11, REDRAW_POSTER, REDRAW_POSTER]
    projector.rejections = {}
    assert not await client.shows_poster(11)  # the poster in between
    projector.poster = 11
    assert await client.shows_poster(11)
    projector.app_state = 0x60
    assert not await client.shows_poster(11)
    projector.power = 0
    assert not await client.shows_poster(11)
    projector.power, projector.app_state = 0x10, 0x10

    # A power read lost with the link says nothing about the power: the poster is put back.
    projector.rejections = {}
    command = client.command

    async def power_read_lost(op: int, *args: object, **kwargs: object) -> bytes:
        if op == Op.GET_POWER:
            raise TimeoutError
        return await command(op, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(client, "command", power_read_lost):
        await client.redraw_poster(11, pause=0)
    assert posters(projector)[-2:] == [REDRAW_POSTER, 11]
    await client.disconnect()


async def test_a_poll_during_a_redraw_shows_the_weather_poster(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector
) -> None:
    coordinator = on_weather.runtime_data
    sources = follow_sources(hass)
    in_between, watching = watch_in_between(coordinator.client)
    # Long enough for the poll to end before the redraw does.
    with watching, patch("custom_components.sony_lspx_p1.client.REDRAW_PAUSE", 0.5):
        task = hass.async_create_task(coordinator.async_set_weather(7, redraw=True))
        await in_between.wait()
        projector.volume = 21  # a poll with news, while the poster in between shows
        await coordinator.async_refresh()
        assert projector.poster == REDRAW_POSTER
        assert coordinator.data.poster == 11  # read as the poster in between, shown as the Weather poster
        assert await task
    assert projector.poster == 11
    await settle(hass)
    assert sources == []
    assert hass.states.get(MP).attributes["volume_level"] == 0.21


async def test_source_chosen_during_a_redraw_waits_for_it(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector
) -> None:
    coordinator = on_weather.runtime_data
    projector.log.clear()
    in_between, watching = watch_in_between(coordinator.client)
    with watching, patch("custom_components.sony_lspx_p1.client.REDRAW_PAUSE", 0.05):
        task = hass.async_create_task(coordinator.async_set_weather(7, redraw=True))
        await in_between.wait()
        await select_source(hass, "HDMI")
        assert await task
    ops = [op for op, _ in projector.log]
    assert ops.index(Op.SET_NET_TOPOLOGY) > max(i for i, op in enumerate(ops) if op == Op.SET_POSTER)
    assert projector.app_state == 0x60
    assert hass.states.get(MP).attributes["source"] == "HDMI"


async def test_switched_off_during_a_redraw(
    hass: HomeAssistant,
    on_weather: MockConfigEntry,
    projector: FakeProjector,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = on_weather.runtime_data.client
    set_poster = client.set_poster

    async def power_button_pressed(theme: int) -> None:
        await set_poster(theme)
        if theme == REDRAW_POSTER:
            projector.power = 0  # its power button, while the poster in between shows

    projector.log.clear()
    with patch.object(client, "set_poster", power_button_pressed):
        await set_condition(hass, "rainy")
    assert posters(projector) == [REDRAW_POSTER]  # not chosen again: that would switch it on
    assert projector.power == 0
    assert "switched to standby while the Weather poster was redrawn; it is put back from Night dreams" in caplog.text
    assert "RedrawStoppedError" in on_weather.runtime_data.weather.last_error
    await settle(hass)
    assert hass.states.get(MP).state == "off"  # polled again after the failure

    # Switched on again, still on the poster in between: the Weather poster is put back.
    projector.log.clear()
    projector.power = 0x10
    await on_weather.runtime_data.async_refresh()
    await settle(hass)
    assert posters(projector) == [11]
    assert projector.drawn_weather == 7  # the code it took before
    assert hass.states.get(MP).attributes["source"] == "Weather"


async def test_failing_to_show_the_weather_poster_again(
    hass: HomeAssistant,
    on_weather: MockConfigEntry,
    projector: FakeProjector,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = on_weather.runtime_data.client
    set_poster = client.set_poster
    failures = {"left": 1}

    async def flaky(theme: int) -> None:
        if theme == 11 and failures["left"]:
            failures["left"] -= 1
            raise TimeoutError
        await set_poster(theme)

    # Once: tried again at once.
    with patch.object(client, "set_poster", flaky):
        await set_condition(hass, "rainy")
    assert (projector.poster, projector.drawn_weather) == (11, 7)
    assert on_weather.runtime_data.weather.sent == 7
    assert "redrawing" not in caplog.text

    # Twice: left on the poster in between, said so, and the source shows it. The poll that follows tries to put
    # the Weather poster back, and fails a third time.
    failures["left"] = 3
    with patch.object(client, "set_poster", flaky):
        await set_condition(hass, "snowy")
    assert projector.poster == REDRAW_POSTER
    assert "redrawing the Weather poster failed; it may show Night dreams instead until it can be put back" in (
        caplog.text
    )
    weather_poster = on_weather.runtime_data.weather
    assert weather_poster.sent == 7
    assert weather_poster.last_error == "TimeoutError"
    assert hass.states.get(MP).attributes["source"] == "Night dreams"

    # The next poll puts it back; it draws the code it holds as it appears.
    projector.log.clear()
    projector.volume = 22  # a poll with news
    await on_weather.runtime_data.async_refresh()
    await settle(hass)
    assert posters(projector) == [11]
    assert sent(projector) == []
    assert (projector.drawn_weather, weather_poster.sent) == (11, 11)
    assert hass.states.get(MP).attributes["source"] == "Weather"


async def test_another_source_chosen_is_not_undone_by_putting_back_the_weather_poster(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector
) -> None:
    coordinator = on_weather.runtime_data
    client = coordinator.client
    set_poster = client.set_poster

    async def never_weather(theme: int) -> None:
        if theme == 11:
            raise TimeoutError
        await set_poster(theme)

    with patch.object(client, "set_poster", never_weather):
        await set_condition(hass, "rainy")
    assert projector.poster == REDRAW_POSTER
    await select_source(hass, "Clock 1")  # chosen in Home Assistant: nothing to put back any more
    await select_source(hass, "Night dreams")
    projector.log.clear()
    projector.volume = 22
    await coordinator.async_refresh()
    await settle(hass)
    assert posters(projector) == []
    assert hass.states.get(MP).attributes["source"] == "Night dreams"

    # Left on it again, then the projector shows something else (the app): forgotten too.
    await select_source(hass, "Weather")
    with patch.object(client, "set_poster", never_weather):
        await set_condition(hass, "snowy")
    assert projector.poster == REDRAW_POSTER
    projector.poster = 10
    await coordinator.async_refresh()
    projector.poster = REDRAW_POSTER
    projector.log.clear()
    await coordinator.async_refresh()
    await settle(hass)
    assert posters(projector) == []


async def test_a_retry_sends_the_condition_as_it_is_then(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector
) -> None:
    projector.rejections = {Op.SET_WEATHER: ErrorCode.UNKNOWN_COMMAND}
    await set_condition(hass, "rainy")
    await set_condition(hass, "snowy")  # fails too; the one retry is already set
    projector.rejections = {}
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RETRY_DELAY + 1))
    await settle(hass)
    assert projector.drawn_weather == 11
    assert on_weather.runtime_data.weather.sent == 11

    # A condition without a picture by the time of the retry: nothing is sent.
    projector.rejections = {Op.SET_WEATHER: ErrorCode.UNKNOWN_COMMAND}
    await set_condition(hass, "rainy")
    await set_condition(hass, "exceptional")
    projector.rejections = {}
    projector.log.clear()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RETRY_DELAY + 1))
    await settle(hass)
    assert sent(projector) == []


async def test_failing_to_show_the_poster_in_between_still_puts_back_the_weather_poster(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector, caplog: pytest.LogCaptureFixture
) -> None:
    client = on_weather.runtime_data.client
    set_poster = client.set_poster

    async def failing_in_between(theme: int) -> None:
        if theme == REDRAW_POSTER:
            raise BleakError("link lost")
        await set_poster(theme)

    projector.log.clear()
    with patch.object(client, "set_poster", failing_in_between):
        await set_condition(hass, "rainy")
    assert posters(projector) == [11]
    assert projector.poster == 11
    assert "it may still show the picture from before: RedrawIncompleteError: BleakError: link lost" in caplog.text
    weather_poster = on_weather.runtime_data.weather
    assert weather_poster.sent == 1  # perhaps not drawn
    assert weather_poster.last_error == "RedrawIncompleteError: BleakError: link lost"

    # Tried once more a minute later.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RETRY_DELAY + 1))
    await settle(hass)
    assert sent(projector) == [7, 7]
    assert (projector.poster, projector.drawn_weather) == (11, 7)
    assert weather_poster.sent == 7
    assert weather_poster.last_error is None


async def test_a_failed_send_is_retried_only_once(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector
) -> None:
    projector.rejections = {Op.SET_WEATHER: ErrorCode.UNKNOWN_COMMAND}
    await set_condition(hass, "rainy")
    for _ in range(3):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RETRY_DELAY + 1))
        await settle(hass)
        await asyncio.sleep(0)
    assert [op for op, _ in projector.log].count(Op.SET_WEATHER) == 3  # the first, the code at start, one retry
    projector.rejections = {}
    await set_condition(hass, "snowy")
    assert projector.drawn_weather == 11


async def test_the_retry_repeats_the_send_that_failed(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector
) -> None:
    """An appearance whose GetWeather failed, with the code last sent: read again, as the projector may have lost it."""
    coordinator = on_weather.runtime_data
    projector.poster = 10
    await coordinator.async_refresh()
    projector.poster, projector.weather = 11, 0  # back, after the projector lost the code
    projector.rejections = {Op.GET_WEATHER: ErrorCode.UNKNOWN_COMMAND}
    await coordinator.async_refresh()
    await settle(hass)
    assert "UNKNOWN_COMMAND" in on_weather.runtime_data.weather.last_error
    projector.rejections = {}
    projector.log.clear()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RETRY_DELAY + 1))
    await settle(hass)
    assert Op.GET_WEATHER in {op for op, _ in projector.log}
    assert sent(projector) == [1]
    assert projector.drawn_weather == 1
    assert on_weather.runtime_data.weather.last_error is None


async def test_nothing_is_redrawn_once_the_weather_poster_no_longer_shows(
    hass: HomeAssistant, on_weather: MockConfigEntry, projector: FakeProjector
) -> None:
    """The last poll is out of date: switched off by its button, or another source chosen in the app."""
    for change in ({"power": 0}, {"power": 0x10, "poster": 10}, {"app_state": 0x60}):
        projector.log.clear()
        for attr, value in change.items():
            setattr(projector, attr, value)
        await set_condition(hass, "rainy" if projector.weather != 7 else "snowy")
        assert posters(projector) == []
        assert sent(projector) == []
        assert projector.power == change.get("power", 0x10)  # not switched on
        await settle(hass)  # polled again: the source shows what the projector shows
        assert hass.states.get(MP).attributes.get("source") != "Weather"
        projector.power, projector.app_state, projector.poster = 0x10, 0x10, 11
        await on_weather.runtime_data.async_refresh()  # back on the Weather poster: drawn with the new code
        await settle(hass)
        assert projector.drawn_weather == projector.weather
