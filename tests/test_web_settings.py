"""Settings over Wi-Fi: timers, initial mode, slideshow interval (disabled by default; reading, writing, renewing,
availability)."""

from __future__ import annotations

import asyncio
import calendar
from collections.abc import Awaitable, Callable, Generator
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.sony_lspx_p1 import clock, web_api, web_settings
from custom_components.sony_lspx_p1.entity import LSPXWebEntity
from custom_components.sony_lspx_p1.protocol import ErrorCode, Op

from .fake_projector import FakeProjector
from .fake_web_api import HOST, FakeWebApi

pytestmark = pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")

ON_SWITCH = "switch.lspx_p1_1a2b_switch_on_at_a_set_time"
OFF_SWITCH = "switch.lspx_p1_1a2b_standby_at_a_set_time"
ON_TIME = "time.lspx_p1_1a2b_switch_on_time"
OFF_TIME = "time.lspx_p1_1a2b_standby_time"
TIMER_ENTITIES = (ON_SWITCH, OFF_SWITCH, ON_TIME, OFF_TIME)
INITIAL_MODE = "select.lspx_p1_1a2b_initial_mode"
INTERVAL = "number.lspx_p1_1a2b_slideshow_interval"
WEB_ENTITIES = (*TIMER_ENTITIES, INITIAL_MODE, INTERVAL)
TIMERS = "/v1/settings/timers"
POSTER_RESUME = "/v1/settings/posterResume"
DURATION = "/v2/poster/duration"
TODAY = date(2026, 7, 1)


def wall_clock(*fields: int) -> int:
    return calendar.timegm((*fields, 0, 0, 0))


def endpoint_reads(projector: FakeProjector) -> int:
    return sum(1 for op, _payload in projector.log if op == Op.GET_HTTP_ENDPOINT)


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await settle(hass)


def state(hass: HomeAssistant, entity_id: str) -> str:
    return hass.states.get(entity_id).state


class Clock:
    """The time the clock sync and the settings see (freezegun would stop the fake projector's replies)."""

    def __init__(self) -> None:
        self.now = datetime(2026, 7, 1, 12, tzinfo=UTC)  # 14:00 in Berlin

    def tick(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
async def frozen(hass: HomeAssistant) -> Generator[Clock]:
    await hass.config.async_set_time_zone("Europe/Berlin")
    fake = Clock()
    fake_dt = SimpleNamespace(utcnow=lambda: fake.now, as_local=dt_util.as_local)
    with patch.object(clock, "dt_util", fake_dt), patch.object(web_settings, "dt_util", fake_dt):
        yield fake


@pytest.fixture
def enabled() -> Generator[None]:
    """The timer entities enabled, as a user would do on the device page."""
    with patch.object(LSPXWebEntity, "_attr_entity_registry_enabled_default", True):
        yield


@pytest.fixture
def api(aioclient_mock: AiohttpClientMocker) -> FakeWebApi:
    return FakeWebApi(aioclient_mock)


@pytest.fixture
def on_wifi(projector: FakeProjector) -> FakeProjector:
    """On the home Wi-Fi, showing the Window poster."""
    projector.topology, projector.app_state, projector.poster = 17, 0x10, 6
    projector.endpoint = (80, HOST)
    return projector


type Start = Callable[[], Awaitable[MockConfigEntry]]


@pytest.fixture
async def start(hass: HomeAssistant, config_entry: MockConfigEntry, frozen: Clock, api: FakeWebApi):
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


def add_daily(api: FakeWebApi, tag: str, at: time, today: date = TODAY) -> None:
    for at_time in web_api.daily_times(today, at):
        api.add_timer(tag, at_time)


async def call(hass: HomeAssistant, domain: str, service: str, entity_id: str, **data) -> None:
    await hass.services.async_call(domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)
    await settle(hass)


async def test_disabled_by_default(hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start) -> None:
    add_daily(api, "timer_on", time(20, 0))
    await start()
    registry = er.async_get(hass)
    for entity_id in WEB_ENTITIES:
        entry = registry.async_get(entity_id)
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert entry.entity_category is EntityCategory.CONFIG
        assert hass.states.get(entity_id) is None
    # Only the clock used the HTTP API, and the endpoint was read once, for it.
    assert [(m, path) for m, path, _body in api.requests] == [("POST", "/v1/settings/time")]
    assert endpoint_reads(on_wifi) == 1


@pytest.mark.usefixtures("enabled")
async def test_reads_the_timers(hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start) -> None:
    add_daily(api, "timer_on", time(20, 15))
    await start()
    assert state(hass, ON_SWITCH) == STATE_ON
    assert state(hass, ON_TIME) == "20:15:00"
    assert state(hass, OFF_SWITCH) == STATE_OFF
    assert state(hass, OFF_TIME) == STATE_UNKNOWN
    # The clock and the settings share one read of the endpoint.
    assert endpoint_reads(on_wifi) == 1
    assert len(api.calls("POST", TIMERS)) == 0  # not due for renewal yet


@pytest.mark.usefixtures("enabled")
async def test_diagnostics(
    hass: HomeAssistant, hass_client, on_wifi: FakeProjector, api: FakeWebApi, start: Start
) -> None:
    add_daily(api, "timer_off", time(23, 0))
    entry = await start()
    diag = (await get_diagnostics_for_config_entry(hass, hass_client, entry))["settings_over_wifi"]
    assert diag["host"] == HOST
    assert diag["errors"] == {}
    assert diag["refreshed_at"] == "2026-07-01T12:00:00+00:00"
    assert diag["timers"][0] == {"id": "id1", "tag": "timer_off", "state": "off", "time": "2026-07-01T23:00:00"}
    assert len(diag["timers"]) == 7
    assert diag["remembered"] == {"timer_off": "23:00:00"}


@pytest.mark.usefixtures("enabled")
async def test_switch_on_and_off(hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start) -> None:
    await start()
    assert state(hass, OFF_SWITCH) == STATE_OFF

    # While the timer is off, its time is only remembered.
    await call(hass, "time", "set_value", OFF_TIME, time="23:05:30")
    assert state(hass, OFF_TIME) == "23:05:00"
    assert api.calls("POST", TIMERS) == []

    await call(hass, "switch", "turn_on", OFF_SWITCH)
    assert state(hass, OFF_SWITCH) == STATE_ON
    # Seven entries, from today, as the app writes them.
    assert api.calls("POST", TIMERS) == [
        {"state": "off", "time": wall_clock(2026, 7, day, 23, 5), "creation": 0, "modification": 0, "tag": "timer_off"}
        for day in range(1, 8)
    ]
    assert state(hass, ON_SWITCH) == STATE_OFF

    await call(hass, "switch", "turn_off", OFF_SWITCH)
    assert state(hass, OFF_SWITCH) == STATE_OFF
    assert len(api.calls("DELETE", f"{TIMERS}/*")) == 7
    assert api.timers == []
    assert state(hass, OFF_TIME) == "23:05:00"  # kept for switching it on again


@pytest.mark.usefixtures("enabled")
async def test_changing_the_time_of_a_timer_that_is_on(
    hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start
) -> None:
    add_daily(api, "timer_on", time(20, 0))
    add_daily(api, "timer_off", time(23, 0))
    await start()
    await call(hass, "time", "set_value", ON_TIME, time="06:45:00")
    assert state(hass, ON_TIME) == "06:45:00"
    assert api.times("timer_on") == [wall_clock(2026, 7, day, 6, 45) for day in range(1, 8)]
    assert api.times("timer_off") == [wall_clock(2026, 7, day, 23, 0) for day in range(1, 8)]  # left alone
    assert len(api.calls("DELETE", f"{TIMERS}/*")) == 7


@pytest.mark.usefixtures("enabled")
async def test_switching_on_needs_a_time(
    hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start
) -> None:
    await start()
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "switch", "turn_on", ON_SWITCH)
    assert err.value.translation_key == "timer_time_unknown"
    assert api.calls("POST", TIMERS) == []


@pytest.mark.usefixtures("enabled")
async def test_time_restored_after_restart(
    hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start
) -> None:
    mock_restore_cache(hass, [State(ON_TIME, "07:10:00"), State(OFF_TIME, STATE_UNAVAILABLE)])
    await start()
    assert state(hass, ON_TIME) == "07:10:00"
    assert state(hass, OFF_TIME) == STATE_UNKNOWN
    await call(hass, "switch", "turn_on", ON_SWITCH)
    assert api.times("timer_on")[0] == wall_clock(2026, 7, 1, 7, 10)


@pytest.mark.usefixtures("enabled")
async def test_refreshes_every_ten_minutes_and_renews_daily(
    hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start, frozen: Clock
) -> None:
    add_daily(api, "timer_on", time(20, 0))
    entry = await start()
    gets = len(api.calls("GET", TIMERS))  # one, or more if platforms came up during the first refresh

    frozen.tick(timedelta(minutes=9))
    await poll(hass, entry)
    assert len(api.calls("GET", TIMERS)) == gets
    frozen.tick(timedelta(minutes=1))
    await poll(hass, entry)
    assert len(api.calls("GET", TIMERS)) == gets + 1
    assert api.calls("POST", TIMERS) == []
    assert endpoint_reads(on_wifi) == 1  # still the first read

    # A day later (the last entry is then less than five days ahead), all are written again, from today.
    frozen.tick(timedelta(hours=6, minutes=10))  # 20:20 in Berlin
    await poll(hass, entry)
    assert api.times("timer_on") == [wall_clock(2026, 7, day, 20, 0) for day in range(1, 8)]
    frozen.tick(timedelta(days=1))
    await poll(hass, entry)
    assert api.times("timer_on") == [wall_clock(2026, 7, day, 20, 0) for day in range(2, 9)]
    assert state(hass, ON_SWITCH) == STATE_ON
    assert state(hass, ON_TIME) == "20:00:00"


@pytest.mark.usefixtures("enabled")
async def test_unavailable_in_standby_and_back(
    hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start
) -> None:
    add_daily(api, "timer_on", time(20, 0))
    entry = await start()
    on_wifi.power = 0
    await poll(hass, entry)
    for entity_id in WEB_ENTITIES:
        assert state(hass, entity_id) == STATE_UNAVAILABLE
    gets = len(api.calls("GET", TIMERS))

    # Back on: read again on the next poll, and the endpoint too.
    on_wifi.power = 0x10
    await poll(hass, entry)
    assert len(api.calls("GET", TIMERS)) == gets + 1
    assert endpoint_reads(on_wifi) == 2
    assert state(hass, ON_SWITCH) == STATE_ON


@pytest.mark.usefixtures("enabled")
async def test_unavailable_when_polls_fail(
    hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start
) -> None:
    entry = await start()
    assert state(hass, ON_SWITCH) == STATE_OFF
    on_wifi.busy = True
    await poll(hass, entry)
    assert state(hass, ON_SWITCH) == STATE_UNAVAILABLE
    on_wifi.busy = False
    await poll(hass, entry)
    assert state(hass, ON_SWITCH) == STATE_OFF


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize(
    ("topology", "address"),
    [(0x30, HOST), (17, "0.0.0.0")],
    ids=["wireless_hdmi", "not_on_wifi"],
)
async def test_unavailable_without_wifi(
    hass: HomeAssistant,
    on_wifi: FakeProjector,
    api: FakeWebApi,
    start: Start,
    *,
    topology: int,
    address: str,
) -> None:
    on_wifi.topology, on_wifi.endpoint = topology, (80, address)
    entry = await start()
    for entity_id in WEB_ENTITIES:
        assert state(hass, entity_id) == STATE_UNAVAILABLE
    assert [m for m, _path, _body in api.requests if m != "POST"] == []
    if topology == 17:
        assert entry.runtime_data.web_settings.data.errors == {"endpoint": "not on Wi-Fi (endpoint address '0.0.0.0')"}
    # Changes need the projector on Wi-Fi.
    with pytest.raises(HomeAssistantError) as err:
        await entry.runtime_data.web_settings.async_set_timer("timer_on", False)
    assert err.value.translation_key == "web_settings_unreachable"


@pytest.mark.usefixtures("enabled")
async def test_failing_endpoint_read(
    hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start
) -> None:
    on_wifi.rejections[Op.GET_HTTP_ENDPOINT] = ErrorCode.UNKNOWN_COMMAND
    entry = await start()
    assert state(hass, ON_SWITCH) == STATE_UNAVAILABLE
    assert state(hass, "media_player.lspx_p1_1a2b") == "on"
    errors = entry.runtime_data.web_settings.data.errors
    assert errors["endpoint"].startswith("reading the endpoint over Bluetooth: CommandRejected")


@pytest.mark.usefixtures("enabled")
async def test_failing_http_api(
    hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start, frozen: Clock
) -> None:
    api.fail[f"GET {TIMERS}"] = 500
    entry = await start()
    # Each setting fails on its own.
    assert state(hass, ON_SWITCH) == STATE_UNAVAILABLE
    assert state(hass, INITIAL_MODE) == "poster"
    assert entry.runtime_data.web_settings.data.errors == {"timers": "HTTP 500"}

    api.fail[f"GET {POSTER_RESUME}"] = api.fail[f"GET {DURATION}"] = aiohttp.ClientConnectionError("refused")
    frozen.tick(timedelta(minutes=10))
    reads = endpoint_reads(on_wifi)
    await poll(hass, entry)
    assert state(hass, INITIAL_MODE) == STATE_UNAVAILABLE
    assert endpoint_reads(on_wifi) == reads
    # Nothing answered, so the endpoint is read again next time.
    api.fail.clear()
    frozen.tick(timedelta(minutes=10))
    await poll(hass, entry)
    assert endpoint_reads(on_wifi) == reads + 1
    assert state(hass, ON_SWITCH) == STATE_OFF
    assert state(hass, INITIAL_MODE) == "poster"


@pytest.mark.usefixtures("enabled")
async def test_failing_change(hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start) -> None:
    add_daily(api, "timer_on", time(20, 0))
    entry = await start()
    api.fail[f"POST {TIMERS}"] = aiohttp.ClientConnectionError("Connection refused")
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "time", "set_value", ON_TIME, time="06:00:00")
    assert err.value.translation_key == "web_settings_failed"
    assert str(err.value) == (
        f"Could not change the projector's settings over Wi-Fi at {HOST}: ClientConnectionError: Connection refused"
    )
    # The next poll reads the timers (and the endpoint) again.
    del api.fail[f"POST {TIMERS}"]
    await poll(hass, entry)
    assert endpoint_reads(on_wifi) == 2
    assert state(hass, ON_TIME) == "20:00:00"


@pytest.mark.usefixtures("enabled")
async def test_failing_renewal_keeps_the_timers(
    hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start
) -> None:
    add_daily(api, "timer_on", time(8, 0), today=date(2026, 6, 30))  # written yesterday: due for renewal
    api.fail[f"POST {TIMERS}"] = 500
    entry = await start()
    assert state(hass, ON_SWITCH) == STATE_ON
    assert entry.runtime_data.web_settings.data.errors == {"timer_on_renew": "HTTP 500"}


async def test_web_api_timers(aioclient_mock: AiohttpClientMocker) -> None:
    session = aioclient_mock.create_session(asyncio.get_running_loop())
    api = FakeWebApi(aioclient_mock)
    try:
        assert web_api.daily_times(date(2026, 3, 28), time(2, 30))[:2] == [
            wall_clock(2026, 3, 28, 2, 30),
            wall_clock(2026, 3, 29, 2, 30),  # wall-clock time: daylight saving does not matter
        ]
        api.timers = [
            {"id": "a", "tag": "other", "state": "on", "time": wall_clock(2026, 7, 1, 5, 0)},
            {"id": 7, "tag": "timer_on", "state": "on", "time": True},
            {"id": "b", "tag": "timer_on", "state": "on", "time": wall_clock(2026, 7, 2, 6, 30) + 15},
            "junk",
        ]
        timers = await web_api.get_timers(session, HOST)
        assert timers[1] == web_api.Timer(None, "timer_on", "on", None)
        assert web_api.timer_time(timers, "timer_on") == time(6, 30)
        assert web_api.timer_time(timers, "timer_off") is None

        # A failing delete does not stop the others; the first failure is reported.
        api.timers = [{"id": str(n), "tag": "timer_off", "state": "off", "time": n} for n in range(3)]
        api.fail[f"DELETE {TIMERS}/0"] = 500
        with pytest.raises(web_api.WebApiError, match="HTTP 500"):
            await web_api.clear_daily_timer(session, HOST, "timer_off")
        assert [t["id"] for t in api.timers] == ["0"]

        aioclient_mock.clear_requests()
        aioclient_mock.get(f"http://{HOST}{TIMERS}", json={"status": "200", "result": {"x": 1}})
        with pytest.raises(web_api.WebApiError, match="no list of timers"):
            await web_api.get_timers(session, HOST)
    finally:
        await session.close()
    assert web_api.wall_clock_text(None) is None


@pytest.mark.usefixtures("enabled")
async def test_initial_mode(hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start) -> None:
    api.poster_resume = False
    entry = await start()
    assert state(hass, INITIAL_MODE) == "last_mode"
    assert er.async_get(hass).async_get(INITIAL_MODE).entity_category is EntityCategory.CONFIG
    await call(hass, "select", "select_option", INITIAL_MODE, option="poster")
    assert api.calls("POST", POSTER_RESUME) == [{"posterResume": True}]
    assert state(hass, INITIAL_MODE) == "poster"
    assert (await _diagnostics(hass, entry))["poster_resume"] is True


@pytest.mark.usefixtures("enabled")
async def test_slideshow_interval(hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start) -> None:
    api.poster_duration = 300_000  # the app's 5 min.
    await start()
    interval = hass.states.get(INTERVAL)
    assert interval.state == "300.0"
    assert interval.attributes["unit_of_measurement"] == "s"
    assert (interval.attributes["min"], interval.attributes["max"]) == (1, 43200)
    await call(hass, "number", "set_value", INTERVAL, value=7199)  # a custom time: 1:59:59
    assert api.calls("POST", DURATION) == [{"value": 7_199_000}]
    assert state(hass, INTERVAL) == "7199.0"


@pytest.mark.usefixtures("enabled")
async def test_refused_poster_settings(
    hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start
) -> None:
    await start()
    # Like the app, a reply with another value than the one sent counts as a failure.
    api.keep_poster_settings = True
    with pytest.raises(HomeAssistantError, match="kept posterResume True"):
        await call(hass, "select", "select_option", INITIAL_MODE, option="last_mode")
    with pytest.raises(HomeAssistantError, match="kept a duration of 10000 ms"):
        await call(hass, "number", "set_value", INTERVAL, value=60)


async def test_web_api_poster_replies(aioclient_mock: AiohttpClientMocker) -> None:
    session = aioclient_mock.create_session(asyncio.get_running_loop())
    try:
        for path, read in ((POSTER_RESUME, web_api.get_poster_resume), (DURATION, web_api.get_poster_duration)):
            for result in ({}, {"posterResume": "yes", "value": True}, None):
                aioclient_mock.clear_requests()
                aioclient_mock.get(f"http://{HOST}{path}", json={"status": "200", "result": result})
                with pytest.raises(web_api.WebApiError, match="in the reply"):
                    await read(session, HOST)
    finally:
        await session.close()


async def _diagnostics(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    return entry.runtime_data.web_settings.diagnostics()


@pytest.mark.usefixtures("enabled")
async def test_entities_enabled_later_are_read_at_once(
    hass: HomeAssistant, on_wifi: FakeProjector, api: FakeWebApi, start: Start
) -> None:
    entry = await start()
    # Every enabled setting was read at setup, although the entities' platforms come up one after another.
    for path in (TIMERS, POSTER_RESUME, DURATION):
        assert len(api.calls("GET", path)) >= 1, path
    gets = len(api.calls("GET", TIMERS))
    # A listener with something not read yet makes the next check refresh, without waiting 10 minutes.
    remove = entry.runtime_data.web_settings.async_add_listener(lambda: None, "something_new")
    entry.runtime_data.web_settings.async_check()
    await settle(hass)
    assert len(api.calls("GET", TIMERS)) == gets + 1
    remove()
