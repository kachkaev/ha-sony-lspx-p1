"""The settings kept in the projector's HTTP API: timers, initial mode, slideshow interval (through the Bluetooth
tunnel, else over Wi-Fi; reading, writing, renewing, availability)."""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
from collections.abc import Awaitable, Callable, Generator
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from bleak.exc import BleakError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed, mock_restore_cache
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.sony_lspx_p1 import clock, tunnel, web_api, web_settings
from custom_components.sony_lspx_p1.client import ProjectorBusyError
from custom_components.sony_lspx_p1.entity import LSPXWebEntity
from custom_components.sony_lspx_p1.protocol import ErrorCode, Op

from .fake_projector import FakeBleakClient, FakeProjector
from .fake_web_api import HOST, FakeSettings, FakeWebApi

pytestmark = pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")

ON_SWITCH = "switch.lspx_p1_1a2b_switch_on_at_a_set_time"
OFF_SWITCH = "switch.lspx_p1_1a2b_standby_at_a_set_time"
ON_TIME = "time.lspx_p1_1a2b_switch_on_time"
OFF_TIME = "time.lspx_p1_1a2b_standby_time"
TIMER_ENTITIES = (ON_SWITCH, OFF_SWITCH, ON_TIME, OFF_TIME)
INITIAL_MODE = "select.lspx_p1_1a2b_initial_mode"
INTERVAL = "number.lspx_p1_1a2b_slideshow_interval"
POSTER_ENTITIES = (INITIAL_MODE, INTERVAL)
WEB_ENTITIES = (*TIMER_ENTITIES, *POSTER_ENTITIES)
TIMERS = "/v1/settings/timers"
POSTER_RESUME = "/v1/settings/posterResume"
DURATION = "/v2/poster/duration"
TODAY = date(2026, 7, 1)
LOGGER = "custom_components.sony_lspx_p1.web_settings"
POWER_STANDBY = bytes.fromhex("01800000")  # a notification, as in test_notifications.py


def wall_clock(*fields: int) -> int:
    return calendar.timegm((*fields, 0, 0, 0))


def endpoint_reads(projector: FakeProjector) -> int:
    return sum(1 for op, _payload in projector.log if op == Op.GET_HTTP_ENDPOINT)


def settings_requests(projector: FakeProjector) -> list[tuple[str, str]]:
    """The requests through the tunnel other than the clock's, in order."""
    assert projector.tunnel is not None
    return [(m, p) for m, p, _body in projector.tunnel.requests if p != web_api.TIME_PATH]


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await settle(hass)


def state(hass: HomeAssistant, entity_id: str) -> str:
    return hass.states.get(entity_id).state


def diagnostics(entry: MockConfigEntry) -> dict[str, Any]:
    return entry.runtime_data.web_settings.diagnostics()


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
def poster(projector: FakeProjector) -> FakeProjector:
    """On, showing the Window poster, on no home Wi-Fi (as one real projector)."""
    projector.topology, projector.app_state, projector.poster = 16, 0x10, 6
    return projector


@pytest.fixture
def store(projector: FakeProjector) -> FakeSettings:
    """The settings the projector answers through the tunnel."""
    assert projector.tunnel is not None and projector.tunnel.settings is not None
    return projector.tunnel.settings


@pytest.fixture
def api(aioclient_mock: AiohttpClientMocker) -> FakeWebApi:
    """The same settings over Wi-Fi (a projector holds one set: tests use one route or the other)."""
    return FakeWebApi(aioclient_mock)


type Start = Callable[[], Awaitable[MockConfigEntry]]


@pytest.fixture
async def start(hass: HomeAssistant, config_entry: MockConfigEntry, frozen: Clock):
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


def add_daily(settings: FakeSettings, tag: str, at: time, today: date = TODAY) -> None:
    for at_time in web_api.daily_times(today, at):
        settings.add_timer(tag, at_time)


async def call(hass: HomeAssistant, domain: str, service: str, entity_id: str, **data: Any) -> None:
    await hass.services.async_call(domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)
    await settle(hass)


# --- Reading ------------------------------------------------------------------------------------------------------


async def test_defaults(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, api: FakeWebApi, start: Start
) -> None:
    add_daily(store, "timer_on", time(20, 0))
    store.poster_duration = 300_000
    await start()
    registry = er.async_get(hass)
    # The timers are disabled by default; the poster settings, which the app also sends through the tunnel, are not.
    for entity_id in WEB_ENTITIES:
        entry = registry.async_get(entity_id)
        assert entry.entity_category is EntityCategory.CONFIG
        if entity_id in TIMER_ENTITIES:
            assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
            assert hass.states.get(entity_id) is None
        else:
            assert entry.disabled_by is None
    assert state(hass, INITIAL_MODE) == "poster"
    interval = hass.states.get(INTERVAL)
    assert interval.state == "300.0"
    assert interval.attributes["unit_of_measurement"] == "s"
    assert (interval.attributes["min"], interval.attributes["max"]) == (1, 43200)
    # Through the Bluetooth tunnel, once each, and only what enabled entities show: nothing over Wi-Fi.
    assert sorted(settings_requests(poster)) == [("GET", POSTER_RESUME), ("GET", DURATION)]
    assert api.requests == []
    assert endpoint_reads(poster) == 0


@pytest.mark.usefixtures("enabled")
async def test_reads_the_timers(hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start) -> None:
    add_daily(store, "timer_on", time(20, 15))
    add_daily(store, "timer_off", time(23, 0))
    assert poster.tunnel is not None
    poster.tunnel.chunk = 64  # a reply of 14 entries comes in many reads (how many a real one takes is unknown)
    await start()
    assert state(hass, ON_SWITCH) == STATE_ON
    assert state(hass, ON_TIME) == "20:15:00"
    assert state(hass, OFF_SWITCH) == STATE_ON
    assert state(hass, OFF_TIME) == "23:00:00"
    assert store.calls("POST", TIMERS) == []  # not due for renewal yet


@pytest.mark.usefixtures("enabled")
async def test_read_hourly_on_a_poster_and_kept_meanwhile(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start, frozen: Clock
) -> None:
    add_daily(store, "timer_on", time(20, 0))
    entry = await start()
    reads = len(settings_requests(poster))
    assert reads == 3

    frozen.tick(timedelta(minutes=59))
    await poll(hass, entry)
    assert len(settings_requests(poster)) == reads
    frozen.tick(timedelta(minutes=1))
    await poll(hass, entry)
    assert len(settings_requests(poster)) == reads + 3

    # On HDMI, in standby or starting up: not read, but what was read stays.
    for setup in (
        lambda: setattr(poster, "app_state", 0x60),
        lambda: setattr(poster, "power", 0),
    ):
        setup()
        frozen.tick(timedelta(hours=2))
        await poll(hass, entry)
        assert len(settings_requests(poster)) == reads + 3
        assert state(hass, ON_SWITCH) == STATE_ON
        assert state(hass, INITIAL_MODE) == "poster"

    # On a poster again: read at once.
    poster.power, poster.app_state = 0x10, 0x10
    await poll(hass, entry)
    assert len(settings_requests(poster)) == reads + 6


@pytest.mark.usefixtures("enabled")
async def test_unavailable_when_polls_fail(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start
) -> None:
    entry = await start()
    assert state(hass, ON_SWITCH) == STATE_OFF
    poster.busy = True
    await poll(hass, entry)
    for entity_id in WEB_ENTITIES:
        assert state(hass, entity_id) == STATE_UNAVAILABLE
    poster.busy = False
    await poll(hass, entry)
    assert state(hass, ON_SWITCH) == STATE_OFF
    assert state(hass, INITIAL_MODE) == "poster"


@pytest.mark.usefixtures("enabled")
async def test_a_setting_not_answered_costs_a_request_a_day(
    hass: HomeAssistant,
    *,
    poster: FakeProjector,
    store: FakeSettings,
    start: Start,
    frozen: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert poster.tunnel is not None
    poster.tunnel.settings = None  # every settings path answers {"code": 404}
    with caplog.at_level(logging.DEBUG):
        entry = await start()
        for entity_id in WEB_ENTITIES:
            assert state(hass, entity_id) == STATE_UNAVAILABLE  # unavailable, not gone
        assert state(hass, "media_player.lspx_p1_1a2b") == "on"
        assert len(settings_requests(poster)) == 3
        status = diagnostics(entry)["reads"]["timers"]
        assert status["failures"] == 1
        assert status["last_error"] == (
            "over Bluetooth: code 404: None; not over Wi-Fi: not on Wi-Fi (endpoint address '0.0.0.0')"
        )
        # Once a refresh at most (the entities' platforms come up one after another, each starting one).
        assert endpoint_reads(poster) <= 2

        # Every poll in between leaves it alone: tried again after an hour, then daily.
        for _ in range(5):
            frozen.tick(timedelta(minutes=10))
            await poll(hass, entry)
        assert len(settings_requests(poster)) == 3
        frozen.tick(timedelta(minutes=10))
        await poll(hass, entry)
        assert len(settings_requests(poster)) == 6
        frozen.tick(timedelta(hours=23))
        await poll(hass, entry)
        assert len(settings_requests(poster)) == 6
        frozen.tick(timedelta(hours=1))
        await poll(hass, entry)
        assert len(settings_requests(poster)) == 9
    # Nothing but debug messages from the settings.
    assert {r.levelno for r in caplog.records if r.name == LOGGER} == {logging.DEBUG}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "sony_lspx_p1" in r.name]

    # Once it answers, it shows, and is read hourly again.
    poster.tunnel.settings = store
    frozen.tick(timedelta(days=1))
    await poll(hass, entry)
    assert state(hass, INITIAL_MODE) == "poster"
    assert diagnostics(entry)["reads"]["poster_resume"]["failures"] == 0


async def test_each_setting_fails_on_its_own(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start
) -> None:
    store.fail[f"GET {DURATION}"] = 404  # firmware before 2.0.0 has no such setting
    entry = await start()
    assert state(hass, INITIAL_MODE) == "poster"
    assert state(hass, INTERVAL) == STATE_UNAVAILABLE
    assert diagnostics(entry)["reads"]["poster_duration"]["last_error"].startswith("over Bluetooth: HTTP 404; ")


async def test_bluetooth_link_failing_while_reading(
    hass: HomeAssistant, poster: FakeProjector, start: Start, frozen: Clock
) -> None:
    entry = await start()
    assert state(hass, INITIAL_MODE) == "poster"
    frozen.tick(web_settings.REFRESH_INTERVAL)
    with patch.object(type(entry.runtime_data), "async_web_request", side_effect=BleakError("gone")) as request:
        await poll(hass, entry)
    assert request.call_count == 1  # the rest waits for the next poll
    assert state(hass, INITIAL_MODE) == "poster"  # kept
    status = diagnostics(entry)["reads"]
    assert status["poster_resume"]["last_error"] == "over Bluetooth: BleakError: gone"
    assert status["poster_duration"]["failures"] == 0


@pytest.mark.usefixtures("enabled")
async def test_switched_off_by_its_button_while_reading(
    hass: HomeAssistant,
    poster: FakeProjector,
    start: Start,
    frozen: Clock,
    fake_connection: list[FakeBleakClient],
) -> None:
    """A notification while a tunnel request holds the link polls as at any other time, and stops the refresh."""
    entry = await start()
    reads = len(settings_requests(poster))
    assert reads == 3
    client = entry.runtime_data.client
    web_request = client.web_request

    async def pressed_meanwhile(*args: Any, **kwargs: Any) -> Any:
        response = await web_request(*args, **kwargs)
        if poster.power:
            poster.power = 0
            fake_connection[-1].notify(POWER_STANDBY)
        return response

    frozen.tick(web_settings.REFRESH_INTERVAL)
    with patch.object(client, "web_request", pressed_meanwhile):
        await poll(hass, entry)
    assert len(settings_requests(poster)) == reads + 1  # the rest is not asked of a projector going to standby
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))  # polled as the power settles
    await settle(hass)
    assert state(hass, "media_player.lspx_p1_1a2b") == STATE_OFF

    # Switched on again: the settings not read are read with the next poll on a poster.
    poster.power = 0x10
    await poll(hass, entry)
    assert len(settings_requests(poster)) == reads + 3


# --- Over Wi-Fi, when the tunnel is missing or refuses ---------------------------------------------------------


@pytest.fixture
def without_tunnel(poster: FakeProjector) -> FakeProjector:
    """A projector without the tunnel (never seen), on the home Wi-Fi."""
    poster.tunnel = None
    poster.topology, poster.endpoint = 17, (80, HOST)
    return poster


@pytest.mark.usefixtures("enabled")
async def test_over_wifi_without_the_tunnel(
    hass: HomeAssistant, without_tunnel: FakeProjector, api: FakeWebApi, start: Start
) -> None:
    add_daily(api, "timer_off", time(23, 0))
    entry = await start()
    assert state(hass, OFF_SWITCH) == STATE_ON
    assert state(hass, INITIAL_MODE) == "poster"
    assert endpoint_reads(without_tunnel) == 1  # once for the three
    assert diagnostics(entry)["reads"]["timers"]["route"] == "wifi"

    # Changes go the same way.
    await call(hass, "switch", "turn_off", OFF_SWITCH)
    assert state(hass, OFF_SWITCH) == STATE_OFF
    assert len(api.calls("DELETE", f"{TIMERS}/*")) == 7
    await call(hass, "select", "select_option", INITIAL_MODE, option="last_mode")
    assert api.poster_resume is False

    # A failure over Wi-Fi says where it went.
    api.fail[f"POST {POSTER_RESUME}"] = aiohttp.ClientConnectionError("Connection refused")
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "select", "select_option", INITIAL_MODE, option="poster")
    assert err.value.translation_key == "settings_failed"
    assert str(err.value) == (
        "Could not change the projector's settings: over Bluetooth: the projector has no Bluetooth tunnel to its "
        f"HTTP API; over Wi-Fi: http://{HOST}: ClientConnectionError: Connection refused"
    )


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize(
    ("topology", "address", "why"),
    [
        (32, HOST, "on its own network, not the home one (topology 32)"),
        (0x30, HOST, "on its own network, not the home one (topology 48)"),
        (17, "192.168.8.1", "on its own network, not the home one (endpoint address '192.168.8.1')"),
    ],
    ids=["wifi_direct", "wireless_hdmi", "direct_address"],
)
async def test_nothing_sent_to_the_projectors_own_network(
    hass: HomeAssistant,
    without_tunnel: FakeProjector,
    aioclient_mock: AiohttpClientMocker,
    start: Start,
    *,
    topology: int,
    address: str,
    why: str,
) -> None:
    # Something answers there (a home router at GL.iNet's default address, say), but nothing may be sent to it.
    there = FakeWebApi(aioclient_mock, address)
    without_tunnel.topology, without_tunnel.endpoint = topology, (80, address)
    entry = await start()
    for entity_id in WEB_ENTITIES:
        assert state(hass, entity_id) == STATE_UNAVAILABLE
    assert diagnostics(entry)["reads"]["timers"]["last_error"].endswith(f"; not over Wi-Fi: {why}")
    assert there.requests == []


@pytest.mark.usefixtures("enabled")
async def test_failing_endpoint_read(hass: HomeAssistant, without_tunnel: FakeProjector, start: Start) -> None:
    without_tunnel.rejections[Op.GET_HTTP_ENDPOINT] = ErrorCode.UNKNOWN_COMMAND
    entry = await start()
    assert state(hass, ON_SWITCH) == STATE_UNAVAILABLE
    assert state(hass, "media_player.lspx_p1_1a2b") == "on"
    error = diagnostics(entry)["reads"]["timers"]["last_error"]
    assert "; not over Wi-Fi: reading the endpoint over Bluetooth: CommandRejected" in error


async def test_stuck_projector_while_looking_for_wifi(
    hass: HomeAssistant, without_tunnel: FakeProjector, start: Start
) -> None:
    with patch.object(
        web_settings.LSPXCoordinator, "async_http_endpoint", side_effect=ProjectorBusyError("stuck")
    ) as endpoint:
        entry = await start()
    assert endpoint.call_count == 2  # the clock's, and one for the settings: the rest waits for the next poll
    assert state(hass, INITIAL_MODE) == STATE_UNAVAILABLE
    status = diagnostics(entry)["reads"]
    tried = [name for name, s in status.items() if s["last_attempt"] is not None]
    assert len(tried) == 1
    assert status[tried[0]]["last_error"] == "over Bluetooth: ProjectorBusyError: stuck"


@pytest.mark.usefixtures("enabled")
async def test_failing_http_api_reads_the_endpoint_again(
    hass: HomeAssistant, without_tunnel: FakeProjector, api: FakeWebApi, start: Start, frozen: Clock
) -> None:
    api.fail[f"GET {TIMERS}"] = 500
    entry = await start()
    assert state(hass, ON_SWITCH) == STATE_UNAVAILABLE
    assert state(hass, INITIAL_MODE) == "poster"
    del api.fail[f"GET {TIMERS}"]
    frozen.tick(web_settings.RETRY_DELAYS[0])
    reads = len(api.calls("GET", TIMERS))
    await poll(hass, entry)
    assert len(api.calls("GET", TIMERS)) == reads + 1
    assert state(hass, ON_SWITCH) == STATE_OFF


# --- Changing the timers --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("enabled")
async def test_switch_on_and_off(hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start) -> None:
    entry = await start()
    assert state(hass, OFF_SWITCH) == STATE_OFF

    # While the timer is off, its time is only remembered.
    await call(hass, "time", "set_value", OFF_TIME, time="23:05:30")
    assert state(hass, OFF_TIME) == "23:05:00"
    assert store.calls("POST", TIMERS) == []

    store.requests.clear()
    await call(hass, "switch", "turn_on", OFF_SWITCH)
    assert state(hass, OFF_SWITCH) == STATE_ON
    # Seven entries, from today, as the app writes them, through the tunnel; the list read before and after.
    assert store.calls("POST", TIMERS) == [
        {"state": "off", "time": wall_clock(2026, 7, day, 23, 5), "creation": 0, "modification": 0, "tag": "timer_off"}
        for day in range(1, 8)
    ]
    assert len(store.requests) == 9
    assert state(hass, ON_SWITCH) == STATE_OFF

    store.requests.clear()
    await call(hass, "switch", "turn_off", OFF_SWITCH)
    assert state(hass, OFF_SWITCH) == STATE_OFF
    assert [m for m, _p, _b in store.requests] == ["GET", *["DELETE"] * 7, "GET"]
    assert store.timers == []
    assert state(hass, OFF_TIME) == "23:05:00"  # kept for switching it on again
    assert diagnostics(entry)["pending_timers"] == {}


@pytest.mark.usefixtures("enabled")
async def test_changing_the_time_of_a_timer_that_is_on(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start
) -> None:
    add_daily(store, "timer_on", time(20, 0))
    add_daily(store, "timer_off", time(23, 0))
    await start()
    await call(hass, "time", "set_value", ON_TIME, time="06:45:00")
    assert state(hass, ON_TIME) == "06:45:00"
    assert store.times("timer_on") == [wall_clock(2026, 7, day, 6, 45) for day in range(1, 8)]
    assert store.times("timer_off") == [wall_clock(2026, 7, day, 23, 0) for day in range(1, 8)]  # left alone
    assert len(store.calls("DELETE", f"{TIMERS}/*")) == 7


@pytest.mark.usefixtures("enabled")
async def test_switching_on_needs_a_time(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start
) -> None:
    await start()
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "switch", "turn_on", ON_SWITCH)
    assert err.value.translation_key == "timer_time_unknown"
    assert store.calls("POST", TIMERS) == []


@pytest.mark.usefixtures("enabled")
async def test_time_restored_after_restart(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start
) -> None:
    mock_restore_cache(hass, [State(ON_TIME, "07:10:00"), State(OFF_TIME, STATE_UNAVAILABLE)])
    await start()
    assert state(hass, ON_TIME) == "07:10:00"
    assert state(hass, OFF_TIME) == STATE_UNKNOWN
    await call(hass, "switch", "turn_on", ON_SWITCH)
    assert store.times("timer_on")[0] == wall_clock(2026, 7, 1, 7, 10)


@pytest.mark.usefixtures("enabled")
async def test_a_change_that_stopped_part_way_is_finished_later(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start, frozen: Clock
) -> None:
    add_daily(store, "timer_on", time(20, 0))
    entry = await start()
    store.fail[f"POST {TIMERS}"], store.fail_after[f"POST {TIMERS}"] = 500, 3
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "time", "set_value", ON_TIME, time="06:00:00")
    assert err.value.translation_key == "settings_failed"
    assert str(err.value).startswith("Could not change the projector's settings: over Bluetooth: HTTP 500; not over")
    # Three new entries, the old ones kept: the timer still fires every day, at both times for now.
    assert len(store.times("timer_on")) == 10
    assert diagnostics(entry)["pending_timers"] == {
        "timer_on": {"time": "06:00:00", "since": "2026-07-01T12:00:00+00:00"}
    }

    # Tried again with the reads after an hour: only what is missing is created, then the old ones go.
    del store.fail[f"POST {TIMERS}"]
    store.requests.clear()
    frozen.tick(timedelta(minutes=30))
    await poll(hass, entry)
    assert store.requests == []
    frozen.tick(timedelta(minutes=30))
    await poll(hass, entry)
    assert store.times("timer_on") == [wall_clock(2026, 7, day, 6, 0) for day in range(1, 8)]
    assert len(store.calls("POST", TIMERS)) == 4
    assert state(hass, ON_TIME) == "06:00:00"
    assert diagnostics(entry)["pending_timers"] == {}


@pytest.mark.usefixtures("enabled")
async def test_a_failed_change_is_given_up_after_a_day(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start, frozen: Clock
) -> None:
    add_daily(store, "timer_on", time(20, 0))
    entry = await start()
    store.fail[f"DELETE {TIMERS}/*"] = 500
    with pytest.raises(HomeAssistantError):
        await call(hass, "switch", "turn_off", ON_SWITCH)
    assert state(hass, ON_SWITCH) == STATE_ON  # what the projector has
    frozen.tick(timedelta(hours=1))
    await poll(hass, entry)
    assert len(store.calls("DELETE", f"{TIMERS}/*")) == 2  # one try each time, stopping at the first failure
    status = diagnostics(entry)["timer_writes"]
    assert (status["failures"], status["last_error"]) == (2, "HTTP 500")
    assert diagnostics(entry)["reads"]["timers"]["failures"] == 0  # the reads go on
    assert state(hass, ON_SWITCH) == STATE_ON  # read before the write, kept
    frozen.tick(timedelta(days=1))
    await poll(hass, entry)
    assert len(store.calls("DELETE", f"{TIMERS}/*")) == 3  # once more a day later
    frozen.tick(timedelta(days=1))
    await poll(hass, entry)
    assert len(store.calls("DELETE", f"{TIMERS}/*")) == 3
    assert diagnostics(entry)["pending_timers"] == {}


@pytest.mark.usefixtures("enabled")
async def test_a_bounded_number_deleted_each_time(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start, frozen: Clock
) -> None:
    for day in range(30):  # entries that fired and were never deleted
        store.add_timer("timer_off", wall_clock(2026, 6, 1, 23, 0) + day * 86400)
    entry = await start()
    # Written again (more than a week of entries): a week from today, and as many old ones deleted as one write may.
    assert len(store.calls("POST", TIMERS)) == 7
    assert len(store.calls("DELETE", f"{TIMERS}/*")) == web_api.MAX_TIMER_DELETES
    assert state(hass, OFF_SWITCH) == STATE_ON

    # Switched off meanwhile: what one write may not delete goes with the next reads.
    store.requests.clear()
    await call(hass, "switch", "turn_off", OFF_SWITCH)
    assert len(store.calls("DELETE", f"{TIMERS}/*")) == web_api.MAX_TIMER_DELETES
    assert state(hass, OFF_SWITCH) == STATE_ON
    assert "timer_off" in diagnostics(entry)["pending_timers"]
    frozen.tick(web_settings.REFRESH_INTERVAL)
    await poll(hass, entry)
    assert store.timers == []
    assert state(hass, OFF_SWITCH) == STATE_OFF
    assert diagnostics(entry)["pending_timers"] == {}


@pytest.mark.usefixtures("enabled")
async def test_renewal_writes_only_what_is_missing(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start, frozen: Clock
) -> None:
    add_daily(store, "timer_on", time(20, 0))
    entry = await start()
    assert store.calls("POST", TIMERS) == []

    # Two days later, once the last entry is less than four days ahead: the two days missing are added, the past
    # two go.
    frozen.tick(timedelta(days=2, hours=6))
    await poll(hass, entry)
    assert store.calls("POST", TIMERS) == []
    frozen.tick(timedelta(hours=1))  # 21:00 in Berlin
    store.requests.clear()
    await poll(hass, entry)
    assert [m for m, p, _b in store.requests if p.startswith(TIMERS)] == [
        "GET",
        "DELETE",
        "DELETE",
        "POST",
        "POST",
        "GET",
    ]
    assert store.times("timer_on") == [wall_clock(2026, 7, day, 20, 0) for day in range(3, 10)]
    assert state(hass, ON_SWITCH) == STATE_ON
    assert state(hass, ON_TIME) == "20:00:00"


@pytest.mark.usefixtures("enabled")
async def test_untidy_timer_written_again(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start
) -> None:
    add_daily(store, "timer_on", time(7, 0))
    store.add_timer("timer_on", wall_clock(2026, 7, 2, 8, 0))  # another time of day (a write that stopped)
    await start()
    assert store.times("timer_on") == [wall_clock(2026, 7, day, 7, 0) for day in range(1, 8)]


@pytest.mark.usefixtures("enabled")
async def test_failing_renewal_keeps_the_timers(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start
) -> None:
    add_daily(store, "timer_on", time(8, 0), today=date(2026, 6, 28))  # due for renewal
    store.fail[f"POST {TIMERS}"] = 500
    entry = await start()
    assert state(hass, ON_SWITCH) == STATE_ON
    status = diagnostics(entry)["timer_writes"]
    assert (status["failures"], status["last_error"]) == (1, "HTTP 500")
    assert diagnostics(entry)["reads"]["timers"]["route"] == "bluetooth"


@pytest.mark.usefixtures("enabled")
async def test_bluetooth_link_failing_while_writing(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start, frozen: Clock
) -> None:
    add_daily(store, "timer_on", time(8, 0), today=date(2026, 6, 28))  # due for renewal
    entry = await start()
    frozen.tick(timedelta(hours=1))
    original = type(entry.runtime_data).async_web_request

    async def drop_unless_get(self, method: tunnel.Method, path: str, body=None):
        if method is not tunnel.Method.GET:
            raise BleakError("gone")
        return await original(self, method, path, body)

    store.add_timer("timer_on", wall_clock(2026, 7, 2, 9, 0))  # untidy: written again
    with patch.object(type(entry.runtime_data), "async_web_request", drop_unless_get):
        await poll(hass, entry)
        status = diagnostics(entry)["timer_writes"]
        assert (status["failures"], status["last_error"]) == (0, "over Bluetooth: BleakError: gone")
        with pytest.raises(HomeAssistantError) as err:
            await call(hass, "switch", "turn_off", ON_SWITCH)
        assert str(err.value) == "Could not change the projector's settings: over Bluetooth: BleakError: gone"
        assert diagnostics(entry)["timer_writes"]["failures"] == 0  # a Bluetooth link that fails does not count


@pytest.mark.usefixtures("enabled")
async def test_writes_that_keep_failing_are_tried_daily(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start, frozen: Clock
) -> None:
    add_daily(store, "timer_on", time(20, 0), today=date(2026, 6, 27))  # due for renewal
    store.fail[f"DELETE {TIMERS}/*"] = 500  # never tried on a projector
    entry = await start()
    for _ in range(5 * 24):
        frozen.tick(timedelta(hours=1))
        await poll(hass, entry)
    # At setup, an hour later, then daily; the reads stay hourly.
    assert len(store.calls("DELETE", f"{TIMERS}/*")) == 6
    assert len(store.calls("GET", TIMERS)) == 1 + 5 * 24
    assert state(hass, ON_SWITCH) == STATE_ON
    # A renewal deletes first: nothing was created, so the list does not grow.
    assert store.calls("POST", TIMERS) == []
    assert len(store.times("timer_on")) == 7


@pytest.mark.usefixtures("enabled")
async def test_delete_refused_by_the_tunnel(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start, frozen: Clock
) -> None:
    add_daily(store, "timer_on", time(20, 0), today=date(2026, 6, 27))  # due for renewal
    assert poster.tunnel is not None
    poster.tunnel.errors[0x58] = ErrorCode.INVALID_ARGUMENT  # the DELETE request refused (never tried)
    entry = await start()
    status = diagnostics(entry)["timer_writes"]
    assert (status["failures"], status["last_error"]) == (1, "error 0x02 to op 0x58")
    assert state(hass, ON_SWITCH) == STATE_ON
    assert store.calls("POST", TIMERS) == []
    # A change falls back to Wi-Fi, which the projector is not on.
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "switch", "turn_off", ON_SWITCH)
    assert str(err.value) == (
        "Could not change the projector's settings: over Bluetooth: error 0x02 to op 0x58; "
        "not over Wi-Fi: not on Wi-Fi (endpoint address '0.0.0.0')"
    )
    # The reads go on (the link dropped by the refusal is connected again).
    frozen.tick(timedelta(hours=1))
    await poll(hass, entry)
    assert diagnostics(entry)["reads"]["timers"]["last_success"] == "2026-07-01T13:00:00+00:00"


@pytest.mark.usefixtures("enabled")
async def test_link_failing_while_renewing_keeps_the_first_read(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start
) -> None:
    add_daily(store, "timer_on", time(20, 0), today=date(2026, 6, 27))  # due for renewal
    original = web_settings.LSPXCoordinator.async_web_request

    async def drop_unless_get(self, method: tunnel.Method, path: str, body=None):
        if method is not tunnel.Method.GET:
            raise BleakError("gone")
        return await original(self, method, path, body)

    with patch.object(web_settings.LSPXCoordinator, "async_web_request", drop_unless_get):
        await start()
    assert state(hass, ON_SWITCH) == STATE_ON
    assert state(hass, ON_TIME) == "20:00:00"


@pytest.mark.usefixtures("enabled")
async def test_a_time_change_that_stopped_part_way_is_not_undone(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start, frozen: Clock
) -> None:
    add_daily(store, "timer_on", time(20, 0))
    entry = await start()
    store.fail[f"DELETE {TIMERS}/*"] = 500
    with pytest.raises(HomeAssistantError):
        await call(hass, "time", "set_value", ON_TIME, time="06:00:00")
    frozen.tick(timedelta(hours=1))
    await poll(hass, entry)
    assert len(store.times("timer_on")) == 14
    assert state(hass, ON_TIME) == "06:00:00"  # its entries are there, with the old ones
    del store.fail[f"DELETE {TIMERS}/*"]
    # Given up after a day: the timer is untidy, so it is written again, with the time last set.
    frozen.tick(timedelta(days=1))
    await poll(hass, entry)
    assert diagnostics(entry)["pending_timers"] == {}
    assert store.times("timer_on") == [wall_clock(2026, 7, day, 6, 0) for day in range(2, 9)]
    assert state(hass, ON_TIME) == "06:00:00"


@pytest.mark.usefixtures("enabled")
async def test_a_failed_change_found_done_is_forgotten(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start, frozen: Clock
) -> None:
    add_daily(store, "timer_on", time(20, 0))
    entry = await start()
    store.fail[f"POST {TIMERS}"] = 500
    with pytest.raises(HomeAssistantError):
        await call(hass, "time", "set_value", ON_TIME, time="06:00:00")
    # Made meanwhile in the app: nothing left to write, and the change is forgotten.
    store.timers = []
    add_daily(store, "timer_on", time(6, 0))
    del store.fail[f"POST {TIMERS}"]
    frozen.tick(timedelta(hours=1))
    await poll(hass, entry)
    assert diagnostics(entry)["pending_timers"] == {}
    assert store.calls("POST", TIMERS) == [store.calls("POST", TIMERS)[0]]  # only the one that failed
    # So switching it off in the app later sticks.
    store.timers = []
    frozen.tick(timedelta(hours=2))
    await poll(hass, entry)
    assert state(hass, ON_SWITCH) == STATE_OFF
    assert len(store.calls("POST", TIMERS)) == 1


async def test_a_setting_read_before_is_kept_and_asked_hourly(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start, frozen: Clock
) -> None:
    entry = await start()
    store.fail[f"GET {POSTER_RESUME}"] = 500
    for _ in range(3):
        frozen.tick(timedelta(hours=1))
        await poll(hass, entry)
    assert state(hass, INITIAL_MODE) == "poster"  # kept
    assert len(store.calls("GET", POSTER_RESUME)) == 4
    assert diagnostics(entry)["reads"]["poster_resume"]["failures"] == 3

    # A Bluetooth link that fails does not count as a failure of the setting.
    del store.fail[f"GET {POSTER_RESUME}"]
    frozen.tick(timedelta(hours=1))
    with patch.object(type(entry.runtime_data), "async_web_request", side_effect=BleakError("gone")):
        await poll(hass, entry)
    status = diagnostics(entry)["reads"]["poster_resume"]
    assert (status["failures"], status["last_error"]) == (3, "over Bluetooth: BleakError: gone")
    frozen.tick(timedelta(hours=1))
    await poll(hass, entry)
    assert diagnostics(entry)["reads"]["poster_resume"]["failures"] == 0


# --- Changing the poster settings, and when changes are refused --------------------------------------------------


async def test_initial_mode(hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start) -> None:
    store.poster_resume = False
    entry = await start()
    assert state(hass, INITIAL_MODE) == "last_mode"
    await call(hass, "select", "select_option", INITIAL_MODE, option="poster")
    assert store.calls("POST", POSTER_RESUME) == [{"posterResume": True}]
    assert state(hass, INITIAL_MODE) == "poster"
    assert diagnostics(entry)["poster_resume"] is True


async def test_slideshow_interval(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start
) -> None:
    await start()
    store.requests.clear()
    await call(hass, "number", "set_value", INTERVAL, value=7199)  # a custom time: 1:59:59
    assert store.requests == [("POST", DURATION, {"value": 7_199_000})]  # the reply says what it holds
    assert state(hass, INTERVAL) == "7199.0"


async def test_refused_poster_settings(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start
) -> None:
    await start()
    # Like the app, a reply with another value than the one sent counts as a failure.
    store.keep_poster_settings = True
    with pytest.raises(HomeAssistantError, match="kept posterResume True"):
        await call(hass, "select", "select_option", INITIAL_MODE, option="last_mode")
    with pytest.raises(HomeAssistantError, match="kept a duration of 10000 ms"):
        await call(hass, "number", "set_value", INTERVAL, value=60)
    assert state(hass, INITIAL_MODE) == "poster"


async def test_changes_in_any_source(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start
) -> None:
    entry = await start()
    poster.app_state = 0x60  # HDMI
    await poll(hass, entry)
    await call(hass, "select", "select_option", INITIAL_MODE, option="last_mode")
    assert store.poster_resume is False


@pytest.mark.usefixtures("enabled")
async def test_changes_refused_in_standby(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start
) -> None:
    add_daily(store, "timer_on", time(20, 0))
    entry = await start()
    poster.power = 0
    await poll(hass, entry)
    store.requests.clear()
    for change in (
        lambda: call(hass, "select", "select_option", INITIAL_MODE, option="last_mode"),
        lambda: call(hass, "switch", "turn_off", ON_SWITCH),
        lambda: call(hass, "time", "set_value", ON_TIME, time="06:00:00"),
    ):
        with pytest.raises(HomeAssistantError) as err:
            await change()
        assert err.value.translation_key == "settings_not_ready"
    assert store.requests == []
    assert diagnostics(entry)["pending_timers"] == {}


@pytest.mark.usefixtures("enabled")
async def test_diagnostics(
    hass: HomeAssistant, hass_client, poster: FakeProjector, store: FakeSettings, start: Start
) -> None:
    add_daily(store, "timer_off", time(23, 0))
    entry = await start()
    diag = (await get_diagnostics_for_config_entry(hass, hass_client, entry))["settings"]
    assert diag["reads"]["timers"] == {
        "route": "bluetooth",
        "failures": 0,
        "last_attempt": "2026-07-01T12:00:00+00:00",
        "last_success": "2026-07-01T12:00:00+00:00",
        "last_error": None,
    }
    assert diag["timers"][0] == {"id": "id1", "tag": "timer_off", "state": "off", "time": "2026-07-01T23:00:00"}
    assert len(diag["timers"]) == 7
    assert diag["remembered"] == {"timer_off": "23:00:00"}
    assert (diag["poster_resume"], diag["poster_duration_ms"]) == (True, 10_000)


async def test_entities_enabled_later_are_read_at_once(
    hass: HomeAssistant, poster: FakeProjector, store: FakeSettings, start: Start
) -> None:
    entry = await start()
    assert store.calls("GET", TIMERS) == []  # disabled
    remove = entry.runtime_data.web_settings.async_add_listener(lambda: None, web_settings.TIMERS)
    entry.runtime_data.web_settings.async_check()
    await settle(hass)
    assert len(store.calls("GET", TIMERS)) == 1
    remove()


# --- web_api ----------------------------------------------------------------------------------------------------


class ListApi:
    """An Api answering from a FakeSettings, as the tunnel does."""

    def __init__(self, settings: FakeSettings) -> None:
        self.settings = settings

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        answer = self.settings.answer(method, path, body)
        if isinstance(answer, int):
            raise web_api.WebApiError(f"HTTP {answer}")
        return answer.get("result")


async def test_web_api_timers() -> None:
    settings = FakeSettings()
    api = ListApi(settings)
    assert web_api.daily_times(date(2026, 3, 28), time(2, 30))[:2] == [
        wall_clock(2026, 3, 28, 2, 30),
        wall_clock(2026, 3, 29, 2, 30),  # wall-clock time: daylight saving does not matter
    ]
    settings.timers = [
        {"id": "a", "tag": "other", "state": "on", "time": wall_clock(2026, 7, 1, 5, 0)},
        {"id": 7, "tag": "timer_on", "state": "on", "time": True},
        {"id": "b", "tag": "timer_on", "state": "on", "time": wall_clock(2026, 7, 2, 6, 30) + 15},
        "junk",
    ]
    timers = await web_api.get_timers(api)
    assert timers[1] == web_api.Timer(None, "timer_on", "on", None)
    assert web_api.timer_time(timers, "timer_on") == time(6, 30)
    assert web_api.timer_time(timers, "timer_off") is None

    # What a write changes: duplicates go, an entry without an id cannot, other tags are left alone.
    wanted = [100, 200]
    timers = [
        web_api.Timer("1", "timer_on", "on", 100),
        web_api.Timer("2", "timer_on", "on", 100),
        web_api.Timer(None, "timer_on", "on", 300),
        web_api.Timer("4", "timer_on", "on", 400),
        web_api.Timer("5", "timer_off", "off", 300),
    ]
    assert web_api.timer_changes(timers, "timer_on", wanted) == ([200], ["2", "4"])

    settings.timers = []
    await web_api.write_daily_timer(api, "timer_on", [100, 200])
    assert [m for m, _p, _b in settings.requests[-3:]] == ["GET", "POST", "POST"]
    settings.fail["GET /v1/settings/timers"] = 500
    with pytest.raises(web_api.WebApiError, match="HTTP 500"):
        await web_api.write_daily_timer(api, "timer_on", [])

    class NoList:
        async def call(self, method: str, path: str, body: Any = None) -> Any:
            return {"x": 1}

    with pytest.raises(web_api.WebApiError, match="no list of timers"):
        await web_api.get_timers(NoList())
    assert web_api.wall_clock_text(None) is None


async def test_web_api_over_wifi(aioclient_mock: AiohttpClientMocker) -> None:
    session = aioclient_mock.create_session(asyncio.get_running_loop())
    api = FakeWebApi(aioclient_mock)
    add_daily(api, "timer_off", time(22, 0))
    try:
        wifi = web_api.WifiApi(session, HOST)
        assert web_api.timer_time(await web_api.get_timers(wifi), "timer_off") == time(22, 0)
        await web_api.write_daily_timer(wifi, "timer_off", [])
        assert api.timers == []
        assert await web_api.set_poster_duration(wifi, 5000) == 5000
        assert json.loads(aioclient_mock.mock_calls[-1][2]) == {"value": 5000}
    finally:
        await session.close()


async def test_web_api_poster_replies() -> None:
    class Reply:
        def __init__(self, result: Any) -> None:
            self.result = result

        async def call(self, method: str, path: str, body: Any = None) -> Any:
            assert (method, path) in {("GET", POSTER_RESUME), ("GET", DURATION)}
            return self.result

    for read in (web_api.get_poster_resume, web_api.get_poster_duration):
        for result in ({}, {"posterResume": "yes", "value": True}, None):
            with pytest.raises(web_api.WebApiError, match="in the reply"):
                await read(Reply(result))
