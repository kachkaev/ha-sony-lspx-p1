"""Keeping the projector's clock in sync over its Wi-Fi network (when, what is sent, failures, the button)."""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
from collections.abc import Awaitable, Callable, Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse

from custom_components.sony_lspx_p1 import clock, web_api
from custom_components.sony_lspx_p1.protocol import ErrorCode, Op

from .fake_projector import FakeProjector

pytestmark = pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")

MP = "media_player.lspx_p1_1a2b"
SYNC = "button.lspx_p1_1a2b_sync_clock"
HOST = "192.168.1.50"
URL = f"http://{HOST}/v1/settings/time"
REPLY = {"status": "200", "message": "OK", "result": {"time": 0, "timezone": "GMT+0"}}
CLOCK_LOGGER = "custom_components.sony_lspx_p1.clock"


def on_wifi(projector: FakeProjector, *, poster: int = 10) -> None:
    """On the home Wi-Fi, showing a poster (Clock 1 by default)."""
    projector.topology, projector.app_state, projector.poster = 17, 0x10, poster
    projector.endpoint = (80, HOST)


def posts(aioclient_mock: AiohttpClientMocker) -> list[dict]:
    """The bodies of the times posted, in order."""
    return [json.loads(data) for method, url, data, _headers in aioclient_mock.mock_calls if method == "POST"]


def endpoint_reads(projector: FakeProjector) -> int:
    return sum(1 for op, _payload in projector.log if op == Op.GET_HTTP_ENDPOINT)


def wall_clock(*fields: int) -> int:
    """Wall-clock time as Unix seconds, as the app sends it."""
    return calendar.timegm((*fields, 0, 0, 0))


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await settle(hass)


class Clock:
    """The time the clock sync sees (freezegun would stop the fake projector's replies)."""

    def __init__(self) -> None:
        self.now = datetime(2026, 7, 1, 12, tzinfo=UTC)

    def move_to(self, iso: str) -> None:
        self.now = datetime.fromisoformat(iso)

    def tick(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def frozen() -> Generator[Clock]:
    fake = Clock()
    with patch.object(clock, "dt_util", SimpleNamespace(utcnow=lambda: fake.now, as_local=dt_util.as_local)):
        yield fake


@pytest.fixture
async def berlin(hass: HomeAssistant, frozen: Clock) -> Clock:
    await hass.config.async_set_time_zone("Europe/Berlin")
    return frozen  # 2026-07-01 12:00 UTC, 14:00 in Berlin (summer time)


type Start = Callable[[], Awaitable[MockConfigEntry]]


@pytest.fixture
async def start(hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker, berlin):
    """Set up the projector when called (after the test has prepared it), and unload it afterwards."""

    async def setup() -> MockConfigEntry:
        config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await settle(hass)
        assert config_entry.state is ConfigEntryState.LOADED
        return config_entry

    yield setup
    if config_entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


@pytest.fixture
async def loaded(start: Start) -> MockConfigEntry:
    return await start()


@pytest.fixture
async def synced(projector: FakeProjector, aioclient_mock: AiohttpClientMocker, start: Start) -> MockConfigEntry:
    """Set up with the projector on the home Wi-Fi showing Clock 1: the clock is set at once."""
    on_wifi(projector)
    aioclient_mock.post(URL, json=REPLY)
    entry = await start()
    assert len(posts(aioclient_mock)) == 1
    return entry


def diagnostics(entry: MockConfigEntry) -> dict:
    return entry.runtime_data.clock.diagnostics()


async def test_sets_the_clock_when_set_up_showing_a_clock(
    hass: HomeAssistant, synced: MockConfigEntry, aioclient_mock: AiohttpClientMocker, hass_client
) -> None:
    method, url, data, headers = aioclient_mock.mock_calls[0]
    assert (method, str(url)) == ("POST", URL)
    # Local wall-clock time, as the app sends it over Wi-Fi.
    assert json.loads(data) == {"time": wall_clock(2026, 7, 1, 14, 0), "timezone": "GMT+0"}
    assert headers["Content-Type"] == "application/json; charset=utf-8"

    diag = await get_diagnostics_for_config_entry(hass, hass_client, synced)
    assert diag["clock"] == {
        "endpoint": {"address": HOST, "port": 80},
        "last_attempt": "2026-07-01T12:00:00+00:00",
        "last_success": "2026-07-01T12:00:00+00:00",
        "last_error": None,
        "sent_time": "2026-07-01T14:00:00",
        "projector_reply": {"time": "1970-01-01T00:00:00", "timezone": "GMT+0"},
    }


async def test_once_a_day_while_on(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    projector: FakeProjector,
    aioclient_mock: AiohttpClientMocker,
    berlin: Clock,
) -> None:
    reads = endpoint_reads(projector)
    berlin.tick(timedelta(hours=23))
    await poll(hass, synced)
    assert len(posts(aioclient_mock)) == 1
    assert endpoint_reads(projector) == reads  # the endpoint is only read when the clock is set

    berlin.tick(timedelta(hours=1))
    projector.power = 0  # in standby: nothing is sent
    await poll(hass, synced)
    assert len(posts(aioclient_mock)) == 1

    projector.power = 0x10
    await poll(hass, synced)
    assert posts(aioclient_mock)[-1]["time"] == wall_clock(2026, 7, 2, 14, 0)


async def test_after_a_daylight_saving_change(
    hass: HomeAssistant, projector: FakeProjector, aioclient_mock: AiohttpClientMocker, synced: MockConfigEntry
) -> None:
    await hass.config.async_set_time_zone("Europe/London")
    # Another time zone changes the UTC offset too; the next poll notices.
    await poll(hass, synced)
    assert posts(aioclient_mock)[-1]["time"] == wall_clock(2026, 7, 1, 13, 0)


async def test_after_summer_time_ends(
    hass: HomeAssistant, projector: FakeProjector, aioclient_mock: AiohttpClientMocker, start: Start, frozen: Clock
) -> None:
    await hass.config.async_set_time_zone("Europe/London")
    frozen.move_to("2026-10-25 00:30:00+00:00")  # 01:30 BST; summer time ends at 01:00 UTC
    on_wifi(projector)
    aioclient_mock.post(URL, json=REPLY)
    config_entry = await start()
    assert posts(aioclient_mock)[-1]["time"] == wall_clock(2026, 10, 25, 1, 30)

    frozen.tick(timedelta(minutes=20))
    await poll(hass, config_entry)
    assert len(posts(aioclient_mock)) == 1
    frozen.tick(timedelta(minutes=20))  # 01:10 UTC, 01:10 GMT
    await poll(hass, config_entry)
    assert len(posts(aioclient_mock)) == 2
    assert posts(aioclient_mock)[-1]["time"] == wall_clock(2026, 10, 25, 1, 10)


async def test_when_a_clock_poster_is_chosen_or_appears(
    hass: HomeAssistant, synced: MockConfigEntry, projector: FakeProjector, aioclient_mock: AiohttpClientMocker
) -> None:
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Clock 1"}, blocking=True
    )
    await settle(hass)
    await poll(hass, synced)  # the clock is set after the poll that follows
    assert len(posts(aioclient_mock)) == 2

    # Shown from the app or the projector itself: the next poll sees it appear.
    projector.poster = 6  # Window
    await poll(hass, synced)
    assert len(posts(aioclient_mock)) == 2
    projector.poster = 7  # Clock 3
    await poll(hass, synced)
    assert len(posts(aioclient_mock)) == 3
    await poll(hass, synced)
    assert len(posts(aioclient_mock)) == 3

    # Other posters do not count as choosing a clock.
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
    )
    await poll(hass, synced)
    assert len(posts(aioclient_mock)) == 3


async def test_nothing_while_showing_hdmi(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector, aioclient_mock: AiohttpClientMocker
) -> None:
    # The fake projector starts on wireless HDMI, whose Wi-Fi talks to the HDMI unit.
    assert endpoint_reads(projector) == 0
    assert aioclient_mock.call_count == 0


async def test_quiet_when_not_on_wifi(
    hass: HomeAssistant,
    projector: FakeProjector,
    *,
    aioclient_mock: AiohttpClientMocker,
    start: Start,
    berlin: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    on_wifi(projector)
    projector.endpoint = (0, "0.0.0.0")
    with caplog.at_level(logging.DEBUG, CLOCK_LOGGER):
        config_entry = await start()
    assert aioclient_mock.call_count == 0
    assert endpoint_reads(projector) == 1
    assert [r.levelno for r in caplog.records if r.name == CLOCK_LOGGER] == [logging.DEBUG]
    assert hass.states.get(MP).state == "on"
    assert diagnostics(config_entry)["last_error"] == "not on Wi-Fi (endpoint address '0.0.0.0')"
    assert diagnostics(config_entry)["last_success"] is None

    # Tried again 5 minutes later, then hourly; polls in between leave the projector alone.
    berlin.tick(timedelta(minutes=4))
    await poll(hass, config_entry)
    assert endpoint_reads(projector) == 1
    berlin.tick(timedelta(minutes=1))
    await poll(hass, config_entry)
    assert endpoint_reads(projector) == 2
    berlin.tick(timedelta(minutes=30))
    await poll(hass, config_entry)
    assert endpoint_reads(projector) == 2
    berlin.tick(timedelta(minutes=30))
    await poll(hass, config_entry)
    assert endpoint_reads(projector) == 3

    # Once on Wi-Fi, the next attempt sets the clock.
    projector.endpoint = (80, HOST)
    aioclient_mock.post(URL, json=REPLY)
    berlin.tick(timedelta(hours=1))
    await poll(hass, config_entry)
    assert len(posts(aioclient_mock)) == 1


@pytest.mark.parametrize(
    ("mock", "error"),
    [
        ({"status": 500}, "HTTP 500"),
        ({"exc": aiohttp.ClientConnectionError("Connection refused")}, "ClientConnectionError: Connection refused"),
        ({"exc": TimeoutError()}, "TimeoutError"),
        ({"json": {"status": "400", "message": "Bad Request"}}, "status 400: Bad Request"),
    ],
)
async def test_failing_http_api_is_only_logged(
    hass: HomeAssistant,
    projector: FakeProjector,
    *,
    aioclient_mock: AiohttpClientMocker,
    start: Start,
    mock: dict,
    error: str,
) -> None:
    on_wifi(projector)
    aioclient_mock.post(URL, **mock)
    config_entry = await start()
    assert aioclient_mock.call_count == 1
    assert hass.states.get(MP).state == "on"
    assert diagnostics(config_entry)["last_error"] == f"http://{HOST}: {error}"


async def test_failing_endpoint_read_is_only_logged(
    hass: HomeAssistant, projector: FakeProjector, start: Start
) -> None:
    on_wifi(projector)
    projector.rejections[Op.GET_HTTP_ENDPOINT] = ErrorCode.UNKNOWN_COMMAND
    config_entry = await start()
    assert hass.states.get(MP).state == "on"
    assert diagnostics(config_entry)["last_error"].startswith("reading the endpoint over Bluetooth: CommandRejected")
    assert diagnostics(config_entry)["endpoint"] is None


async def test_http_request_holds_no_lock(
    projector: FakeProjector, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry, start: Start
) -> None:
    seen: list[int] = []

    async def side_effect(method, url, data):
        coordinator = config_entry.runtime_data
        # Bluetooth commands and source switches go ahead while the HTTP request is in flight.
        seen.append(await asyncio.wait_for(coordinator.client.get_weather(), 1))
        async with asyncio.timeout(1), coordinator._switching:
            pass
        return AiohttpClientMockResponse(method, url, json=REPLY)

    on_wifi(projector)
    aioclient_mock.post(URL, side_effect=side_effect)
    await start()
    assert seen == [0]
    assert diagnostics(config_entry)["last_error"] is None


async def test_sync_clock_button(
    hass: HomeAssistant, synced: MockConfigEntry, projector: FakeProjector, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = er.async_get(hass).async_get(SYNC)
    assert entry.entity_category is EntityCategory.CONFIG
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: SYNC}, blocking=True)
    assert len(posts(aioclient_mock)) == 2


async def test_sync_clock_button_errors(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector, aioclient_mock: AiohttpClientMocker
) -> None:
    # The fake projector starts on wireless HDMI; the button tries anyway.
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: SYNC}, blocking=True)
    assert err.value.translation_key == "clock_not_on_wifi"
    assert str(err.value) == (
        "The projector is not on a Wi-Fi network, so Home Assistant cannot set its clock. "
        "Connect it to your home Wi-Fi in the smartphone app"  # Home Assistant drops the full stop
    )

    projector.endpoint = (80, HOST)
    aioclient_mock.post(URL, status=404)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: SYNC}, blocking=True)
    assert err.value.translation_key == "clock_sync_failed"
    assert str(err.value) == f"Could not set the projector's clock over Wi-Fi at {HOST}: HTTP 404"

    projector.busy = True
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: SYNC}, blocking=True)
    assert err.value.translation_key == "projector_busy"
    await loaded.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(SYNC).state == STATE_UNAVAILABLE


async def test_web_api_replies(aioclient_mock: AiohttpClientMocker) -> None:
    session = aioclient_mock.create_session(asyncio.get_running_loop())
    try:
        aioclient_mock.get(URL, json={"status": "200", "result": {"time": 1782914400, "timezone": "GMT+0"}})
        assert await web_api.get_time(session, HOST) == web_api.ProjectorTime(1782914400, "GMT+0")
        aioclient_mock.clear_requests()

        # Replies without a usable body still count as done, as the app's postData treats an empty 200.
        for reply in ({"text": ""}, {"text": "OK"}, {"json": [1]}, {"json": {"result": "x"}}):
            aioclient_mock.clear_requests()
            aioclient_mock.post(URL, **reply)
            assert await web_api.set_time(session, HOST, 5) == web_api.ProjectorTime(None, None)
        aioclient_mock.clear_requests()
        aioclient_mock.post(URL, json={"result": {"time": True, "timezone": 3}})
        assert await web_api.set_time(session, HOST, 5) == web_api.ProjectorTime(None, None)
    finally:
        await session.close()

    assert web_api.base_url(HOST, 8080) == f"http://{HOST}:8080"
    assert clock._wall_clock(10**20) == str(10**20)
