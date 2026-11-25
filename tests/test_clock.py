"""Keeping the projector's clock in sync: through the Bluetooth tunnel."""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
from collections.abc import Awaitable, Callable, Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

from custom_components.sony_lspx_p1 import clock, tunnel, web_api
from custom_components.sony_lspx_p1.number import LSPXSlideshowInterval
from custom_components.sony_lspx_p1.protocol import ErrorCode
from custom_components.sony_lspx_p1.select import LSPXInitialModeSelect

from .fake_projector import FakeProjector, FakeTunnel

pytestmark = pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")

MP = "media_player.lspx_p1_1a2b"
SYNC = "button.lspx_p1_1a2b_sync_clock"
CLOCK_LOGGER = "custom_components.sony_lspx_p1.clock"


def showing_clock(projector: FakeProjector, *, poster: int = 10) -> None:
    """On the home network topology, showing a poster (Clock 1 by default)."""
    projector.topology, projector.app_state, projector.poster = 17, 0x10, poster


def fake_tunnel(projector: FakeProjector) -> FakeTunnel:
    assert projector.tunnel is not None
    return projector.tunnel


def sent(projector: FakeProjector) -> list[dict]:
    """The bodies of the times posted through the tunnel, in order."""
    return [json.loads(body) for method, _path, body in fake_tunnel(projector).requests if method == "POST"]


def wall_clock(*fields: int) -> int:
    """Wall-clock time as Unix seconds, as the app sends it."""
    return calendar.timegm((*fields, 0, 0, 0))


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await settle(hass)


async def press(hass: HomeAssistant) -> None:
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: SYNC}, blocking=True)


async def press_fails(hass: HomeAssistant) -> HomeAssistantError:
    with pytest.raises(HomeAssistantError) as err:
        await press(hass)
    return err.value


class Clock:
    """The time the clock sync sees (freezegun would stop the fake projector's replies)."""

    def __init__(self) -> None:
        self.now = datetime(2026, 7, 1, 12, tzinfo=UTC)

    def move_to(self, iso: str) -> None:
        self.now = datetime.fromisoformat(iso)

    def tick(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture(autouse=True)
def clock_only() -> Generator[None]:
    """The settings two entities enabled by default show not read: these tests count the clock's requests alone."""
    with (
        patch.object(LSPXInitialModeSelect, "_attr_entity_registry_enabled_default", False),
        patch.object(LSPXSlideshowInterval, "_attr_entity_registry_enabled_default", False),
    ):
        yield


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
async def start(hass: HomeAssistant, config_entry: MockConfigEntry, berlin):
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
async def synced(projector: FakeProjector, start: Start) -> MockConfigEntry:
    """Set up with the projector showing Clock 1: the clock is set at once, through the tunnel."""
    showing_clock(projector)
    entry = await start()
    assert len(sent(projector)) == 1
    return entry


def diagnostics(entry: MockConfigEntry) -> dict:
    return entry.runtime_data.clock.diagnostics()


# --- Through the Bluetooth tunnel --------------------------------------------------------------------------------


async def test_sets_the_clock_through_the_tunnel(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    projector: FakeProjector,
    hass_client,
) -> None:
    # Local wall-clock time, as the app sends it through the tunnel: the time alone, no time zone.
    assert fake_tunnel(projector).requests == [("POST", "/v1/settings/time", b'{"time":1782914400}')]
    assert wall_clock(2026, 7, 1, 14, 0) == 1782914400

    diag = await get_diagnostics_for_config_entry(hass, hass_client, synced)
    assert diag["clock"] == {
        "last_attempt": "2026-07-01T12:00:00+00:00",
        "last_success": "2026-07-01T12:00:00+00:00",
        "last_error": None,
        "sent_time": "2026-07-01T14:00:00",
        "projector_reply": {"time": "2026-07-01T14:00:00", "timezone": "GMT+0"},
    }
    # Recorded as it happened: downloading diagnostics asked the projector nothing more.
    assert len(fake_tunnel(projector).requests) == 1


async def test_once_a_day_while_on(
    hass: HomeAssistant, synced: MockConfigEntry, projector: FakeProjector, berlin: Clock
) -> None:
    berlin.tick(timedelta(hours=23))
    await poll(hass, synced)
    assert len(sent(projector)) == 1

    berlin.tick(timedelta(hours=1))
    projector.power = 0  # in standby: nothing is sent
    await poll(hass, synced)
    assert len(sent(projector)) == 1

    projector.power = 0x10
    await poll(hass, synced)
    assert sent(projector)[-1] == {"time": wall_clock(2026, 7, 2, 14, 0)}


async def test_after_a_daylight_saving_change(
    hass: HomeAssistant, projector: FakeProjector, synced: MockConfigEntry
) -> None:
    await hass.config.async_set_time_zone("Europe/London")
    # Another time zone changes the UTC offset too; the next poll notices.
    await poll(hass, synced)
    assert sent(projector)[-1] == {"time": wall_clock(2026, 7, 1, 13, 0)}


async def test_after_summer_time_ends(
    hass: HomeAssistant, projector: FakeProjector, start: Start, frozen: Clock
) -> None:
    await hass.config.async_set_time_zone("Europe/London")
    frozen.move_to("2026-10-25 00:30:00+00:00")  # 01:30 BST; summer time ends at 01:00 UTC
    showing_clock(projector)
    config_entry = await start()
    assert sent(projector)[-1] == {"time": wall_clock(2026, 10, 25, 1, 30)}

    frozen.tick(timedelta(minutes=20))
    await poll(hass, config_entry)
    assert len(sent(projector)) == 1
    frozen.tick(timedelta(minutes=20))  # 01:10 UTC, 01:10 GMT
    await poll(hass, config_entry)
    assert len(sent(projector)) == 2
    assert sent(projector)[-1] == {"time": wall_clock(2026, 10, 25, 1, 10)}


async def test_when_a_clock_poster_is_chosen_or_appears(
    hass: HomeAssistant, synced: MockConfigEntry, projector: FakeProjector
) -> None:
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Clock 1"}, blocking=True
    )
    await settle(hass)
    await poll(hass, synced)  # the clock is set after the poll that follows
    assert len(sent(projector)) == 2

    # Shown from the app or the projector itself: the next poll sees it appear.
    projector.poster = 6  # Window
    await poll(hass, synced)
    assert len(sent(projector)) == 2
    projector.poster = 7  # Clock 3
    await poll(hass, synced)
    assert len(sent(projector)) == 3
    await poll(hass, synced)
    assert len(sent(projector)) == 3

    # Other posters do not count as choosing a clock.
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
    )
    await poll(hass, synced)
    assert len(sent(projector)) == 3


async def test_automatically_only_while_a_poster_shows(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    # The fake projector starts on wireless HDMI, where the tunnel was never tried: nothing automatically.
    assert sent(projector) == []
    assert diagnostics(loaded)["last_attempt"] is None

    # The button tries all the same (the app uses the tunnel then too; not tried on a projector).
    await press(hass)
    assert len(sent(projector)) == 1

    # Once a poster shows, the automatic syncs go on as usual.
    projector.app_state, projector.topology, projector.poster = 0x10, 17, 6  # Window
    await poll(hass, loaded)
    assert len(sent(projector)) == 1  # synced just now
    projector.poster = 9  # Clock 2 appears
    await poll(hass, loaded)
    assert len(sent(projector)) == 2


async def test_button_only_while_ready(hass: HomeAssistant, synced: MockConfigEntry, projector: FakeProjector) -> None:
    projector.power = 0
    await poll(hass, synced)
    err = await press_fails(hass)
    assert err.translation_key == "clock_not_ready"
    assert str(err) == (
        "Home Assistant sets the projector's clock only while it is on and ready (not in standby or starting up). "
        "Try again once it is ready"
    )
    with patch.object(type(synced.runtime_data), "starting_up", True):
        projector.power = 0x10
        await poll(hass, synced)
        assert (await press_fails(hass)).translation_key == "clock_not_ready"
    assert len(sent(projector)) == 1
    assert diagnostics(synced)["last_error"] is None  # not an attempt


async def test_clock_poster_seen_while_starting_up_is_kept(
    hass: HomeAssistant, synced: MockConfigEntry, projector: FakeProjector
) -> None:
    projector.poster = 6
    await poll(hass, synced)
    with patch.object(type(synced.runtime_data), "starting_up", True):
        projector.poster = 7  # Clock 3 appears while the start-up is still under way
        await poll(hass, synced)
        assert len(sent(projector)) == 1
    await poll(hass, synced)
    assert len(sent(projector)) == 2


async def test_not_in_standby_nor_while_the_main_board_is_asleep_or_starting(
    hass: HomeAssistant, projector: FakeProjector, start: Start, berlin: Clock
) -> None:
    projector.power, projector.main_board_asleep = 0, True
    entry = await start()
    assert sent(projector) == []
    assert diagnostics(entry)["last_attempt"] is None

    # Chosen in standby, Clock 1 switches it on; the main board starts up (cold) and answers only later.
    projector.cold_start = 3600
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Clock 1"}, blocking=True
    )
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()  # not the background tasks: the start-up's probe runs till the board is up
    assert entry.runtime_data.starting_up
    assert sent(projector) == []

    projector.main_board_up_at = 0  # up: the probe polls, which shows Clock 1, and the clock is set
    await settle(hass)
    assert not entry.runtime_data.starting_up
    assert projector.poster == 10
    assert len(sent(projector)) == 1


async def test_reply_with_another_time_zone_is_shown_in_diagnostics(
    hass: HomeAssistant, projector: FakeProjector, start: Start
) -> None:
    """Only "GMT+0" was seen; the time zone is never written through the tunnel, so another one stays."""
    showing_clock(projector)
    fake_tunnel(projector).timezone = "GMT+9"
    entry = await start()
    assert diagnostics(entry)["projector_reply"] == {"time": "2026-07-01T14:00:00", "timezone": "GMT+9"}
    assert fake_tunnel(projector).timezone == "GMT+9"
    assert diagnostics(entry)["last_error"] is None


@pytest.mark.parametrize(
    ("setup", "error"),
    [
        (lambda t: t.errors.__setitem__(0x40, 0xC0), "error 0xC0 to op 0x40"),
        (lambda t: setattr(t, "http_status", 500), "HTTP 500"),
        (lambda t: setattr(t, "body", b'{"code": 3, "message": "busy"}'), "code 3: busy"),
        (lambda t: t.silent.add(0x42), "no answer to op 0x42 within 1 s"),
    ],
    ids=["refused", "http_error", "code", "silent"],
)
async def test_tunnel_failure_is_only_logged(
    hass: HomeAssistant,
    projector: FakeProjector,
    *,
    start: Start,
    berlin: Clock,
    caplog: pytest.LogCaptureFixture,
    setup,
    error: str,
) -> None:
    showing_clock(projector)
    setup(fake_tunnel(projector))
    with patch.object(tunnel, "STEP_TIMEOUT", 1), caplog.at_level(logging.DEBUG, CLOCK_LOGGER):
        entry = await start()
    assert hass.states.get(MP).state == "on"
    assert {r.levelno for r in caplog.records if r.name == CLOCK_LOGGER} == {logging.DEBUG}
    assert diagnostics(entry)["last_error"] == f"over Bluetooth: {error}"
    assert diagnostics(entry)["last_success"] is None
    attempt = diagnostics(entry)["last_attempt"]

    # Tried again 5 minutes later, then hourly; polls in between leave the projector alone.
    berlin.tick(timedelta(minutes=4))
    await poll(hass, entry)
    assert diagnostics(entry)["last_attempt"] == attempt
    berlin.tick(timedelta(minutes=1))
    with patch.object(tunnel, "STEP_TIMEOUT", 1):
        await poll(hass, entry)
    second = diagnostics(entry)["last_attempt"]
    assert second > attempt
    berlin.tick(timedelta(minutes=30))
    await poll(hass, entry)
    assert diagnostics(entry)["last_attempt"] == second

    # Once the tunnel answers, the next attempt sets the clock.
    projector.tunnel = FakeTunnel()
    berlin.tick(timedelta(minutes=30))
    await poll(hass, entry)
    assert len(sent(projector)) == 1
    assert diagnostics(entry)["last_error"] is None


async def test_link_failing_during_the_request_is_only_logged(
    hass: HomeAssistant,
    projector: FakeProjector,
    fake_connection,
    start: Start,
) -> None:
    showing_clock(projector)
    fake = fake_tunnel(projector)
    answer = fake.control

    def control(frame: bytes) -> bytes | None:
        if frame[2:4] == b"\x00\x42":  # asked for the response: the link drops instead
            asyncio.get_running_loop().call_soon(fake_connection[-1].drop)
            return None
        return answer(frame)

    fake.control = control  # type: ignore[method-assign]
    entry = await start()
    assert diagnostics(entry)["last_error"] == (
        "over Bluetooth: BleakError: disconnected during a request through the tunnel"
    )
    assert hass.states.get(MP).state == "on"


async def test_diagnostics_redact_bluetooth_addresses(
    hass: HomeAssistant, synced: MockConfigEntry, projector: FakeProjector, hass_client
) -> None:
    synced.runtime_data.clock.last_error = "over Bluetooth: BleakError: AA:BB:CC:DD:EE:FF via 11:22:33:44:55:66"
    diag = await get_diagnostics_for_config_entry(hass, hass_client, synced)
    assert diag["clock"]["last_error"] == "over Bluetooth: BleakError: **REDACTED** via **REDACTED**"


async def test_request_waits_for_a_source_switch(
    hass: HomeAssistant, synced: MockConfigEntry, projector: FakeProjector, berlin: Clock
) -> None:
    coordinator = synced.runtime_data
    async with coordinator._switching:
        task = asyncio.create_task(synced.runtime_data.clock.async_sync())
        await asyncio.sleep(0.05)
        assert not task.done()
        berlin.tick(timedelta(minutes=1))  # the switch takes a while
    await task
    # The time is taken once the link is free, not when the sync began.
    assert sent(projector)[-1] == {"time": wall_clock(2026, 7, 1, 14, 1)}
    assert diagnostics(synced)["sent_time"] == "2026-07-01T14:01:00"


async def test_without_a_tunnel(hass: HomeAssistant, projector: FakeProjector, start: Start) -> None:
    """A projector without the tunnel (never seen): the rest works, and the button says why the clock does not."""
    showing_clock(projector)
    projector.tunnel = None
    entry = await start()
    assert hass.states.get(MP).state == "on"
    missing = "over Bluetooth: the projector has no Bluetooth tunnel to its HTTP API"
    assert diagnostics(entry)["last_error"] == missing
    err = await press_fails(hass)
    assert err.translation_key == "clock_sync_failed"
    assert str(err) == f"Could not set the projector's clock: {missing}"


# --- The Sync clock button ------------------------------------------------------------------------------------------


async def test_sync_clock_button_enabled_by_default(
    hass: HomeAssistant, synced: MockConfigEntry, projector: FakeProjector
) -> None:
    entry = er.async_get(hass).async_get(SYNC)
    assert entry is not None
    assert entry.disabled_by is None
    assert entry.entity_category is EntityCategory.CONFIG
    await press(hass)
    assert len(sent(projector)) == 2


async def test_sync_clock_button_errors(hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector) -> None:
    fake_tunnel(projector).errors[0x40] = ErrorCode.MODULE_ERROR
    err = await press_fails(hass)
    assert err.translation_key == "clock_sync_failed"
    assert str(err) == "Could not set the projector's clock: over Bluetooth: error 0xD0 to op 0x40"

    projector.busy = True  # stuck: the main board answers nothing (what the tunnel answers then is a guess)
    err = await press_fails(hass)
    assert err.translation_key == "clock_sync_failed"
    assert diagnostics(loaded)["last_error"] == "over Bluetooth: error 0xC0 to op 0x40"

    projector.busy = False
    await loaded.runtime_data.client.close()  # the link itself fails: as for any command
    err = await press_fails(hass)
    assert err.translation_key == "communication_error"
    assert diagnostics(loaded)["last_error"] == "over Bluetooth: BleakError: connection closed"

    projector.busy = True
    await loaded.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(SYNC).state == STATE_UNAVAILABLE


def test_web_api_replies() -> None:
    # Replies without a usable body still count as done, as the app's postData treats an empty 200.
    for text in ("", "OK", "[1]", '{"result": "x"}', '{"result": {"time": true, "timezone": 3}}'):
        assert web_api.projector_time(web_api.parse_reply(200, text)) == web_api.ProjectorTime(None, None)
    # As the app's code describes replies (over Wi-Fi; through the tunnel only those below were seen).
    assert web_api.parse_reply(200, '{"status": "200", "message": "OK", "result": 1}') == 1
    with pytest.raises(web_api.WebApiError, match="status 400: Bad Request"):
        web_api.parse_reply(200, '{"status": "400", "message": "Bad Request"}')

    assert clock._wall_clock(10**20) == str(10**20)
    # As the tunnel answered (firmware 3.1.15).
    reply = '{"code": 0, "result": {"timezone": "GMT+0", "time": 1795563364}}'
    assert web_api.projector_time(web_api.parse_reply(200, reply)) == web_api.ProjectorTime(1795563364, "GMT+0")
    assert web_api.parse_reply(200, '{"code": "0", "result": 1}') == 1
