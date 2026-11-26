"""The Connection sensor: why a poll could not reach the projector, and the repair issue for a lasting reason."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bleak.exc import BleakError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

from custom_components.sony_lspx_p1 import coordinator as coordinator_module
from custom_components.sony_lspx_p1.const import DOMAIN
from custom_components.sony_lspx_p1.coordinator import LSPXCoordinator
from custom_components.sony_lspx_p1.link_status import (
    LOW_BATTERY,
    QUIET_AFTER,
    STRONG_SIGNAL,
    LinkStatus,
    Observation,
    classify,
)
from custom_components.sony_lspx_p1.protocol import Battery, ChargeState

from .fake_projector import FakeProjector

pytestmark = pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")

MP = "media_player.lspx_p1_1a2b"
CONNECTION = "sensor.lspx_p1_1a2b_connection"
NOW = 1000.0  # bluetooth.MONOTONIC_TIME in these tests


# --- the classification ---


@pytest.mark.parametrize(
    ("seen", "status"),
    [
        (Observation(not_paired=True), LinkStatus.NOT_PAIRED),
        (Observation(connect_failed=True, stale_bond=True, heard_ago=1, last_signal=-70), LinkStatus.NOT_PAIRED),
        (Observation(busy=True), LinkStatus.NOT_RESPONDING),
        (Observation(), LinkStatus.ERROR),  # connected, then something else went wrong
        (Observation(connect_failed=True), LinkStatus.NOT_HEARD),  # never heard
        (Observation(connect_failed=True, heard_ago=QUIET_AFTER + 1, last_signal=-72), LinkStatus.IN_USE_ELSEWHERE),
        (Observation(connect_failed=True, heard_ago=600, last_signal=STRONG_SIGNAL), LinkStatus.IN_USE_ELSEWHERE),
        # Quiet after a weak signal: carried away, most likely.
        (Observation(connect_failed=True, heard_ago=600, last_signal=STRONG_SIGNAL - 1), LinkStatus.NOT_HEARD),
        # Quiet with a battery running out.
        (
            Observation(connect_failed=True, heard_ago=600, last_signal=-60, battery_low=True),
            LinkStatus.NOT_HEARD,
        ),
        (Observation(connect_failed=True, heard_ago=QUIET_AFTER, last_signal=-72), LinkStatus.NOT_ACCEPTING),
    ],
)
def test_classify(seen: Observation, status: LinkStatus) -> None:
    assert classify(seen) is status


# --- in Home Assistant ---


@pytest.fixture
async def loaded(hass: HomeAssistant, config_entry: MockConfigEntry, mock_bluetooth, mock_ble_lookup, fake_connection):
    """Set up after the module's fixtures, which a test's own usefixtures could otherwise come before."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    yield config_entry
    if config_entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


class Heard:
    """What Home Assistant keeps of the projector's advertisements: when the last came (MONOTONIC_TIME) and how
    strongly; None once it has forgotten the projector."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.last: SimpleNamespace | None = SimpleNamespace(time=NOW, rssi=-72)
        bluetooth = coordinator_module.bluetooth
        monkeypatch.setattr(bluetooth, "MONOTONIC_TIME", lambda: NOW)
        monkeypatch.setattr(bluetooth, "async_last_service_info", lambda hass, address, connectable: self.last)

    def quiet_for(self, seconds: float, rssi: int = -72) -> None:
        self.last = SimpleNamespace(time=NOW - seconds, rssi=rssi)


@pytest.fixture
def heard(monkeypatch: pytest.MonkeyPatch) -> Heard:
    return Heard(monkeypatch)


async def drop_link(entry: MockConfigEntry) -> None:
    """End the link the last poll left open (the client lingers a moment), so the next poll connects."""
    client = coordinator_of(entry).client
    async with client._lock:
        await client._drop()


@pytest.fixture
async def no_link(loaded: MockConfigEntry, fake_connection) -> AsyncGenerator[None]:
    """Every connection times out, as while another client holds the projector's one link."""

    async def establish_connection(*args, **kwargs):
        raise BleakError("LSPXP1_6320 - DD:77:D5:94:25:80: Failed to connect after 4 attempt(s): TimeoutError")

    await drop_link(loaded)
    with patch("custom_components.sony_lspx_p1.client.establish_connection", establish_connection):
        yield


def coordinator_of(entry: MockConfigEntry) -> LSPXCoordinator:
    return entry.runtime_data


async def poll(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    await coordinator_of(entry).async_refresh()
    await hass.async_block_till_done()
    return hass.states.get(CONNECTION).state


async def test_ok_and_how_it_shows(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    state = hass.states.get(CONNECTION)
    assert state.state == LinkStatus.OK
    assert state.attributes["error"] is None
    assert state.attributes["options"] == [s.value for s in LinkStatus]
    entity = er.async_get(hass).async_get(CONNECTION)
    assert entity is not None
    assert (entity.entity_category, entity.disabled_by, entity.translation_key) == (
        EntityCategory.DIAGNOSTIC,
        None,
        "connection",
    )


@pytest.mark.usefixtures("no_link")
async def test_in_use_elsewhere_while_everything_else_is_unavailable(
    hass: HomeAssistant, loaded: MockConfigEntry, heard: Heard
) -> None:
    heard.quiet_for(136)  # as seen while Sony's app was connected
    assert await poll(hass, loaded) == LinkStatus.IN_USE_ELSEWHERE
    assert hass.states.get(MP).state == STATE_UNAVAILABLE
    error = hass.states.get(CONNECTION).attributes["error"]
    assert "Failed to connect after 4 attempt(s)" in error


@pytest.mark.usefixtures("no_link")
async def test_each_failed_poll_tells_the_sensor(hass: HomeAssistant, loaded: MockConfigEntry, heard: Heard) -> None:
    """Failed polls in a row notify no listeners by themselves: a change between them still reaches the sensor."""
    assert await poll(hass, loaded) == LinkStatus.NOT_ACCEPTING  # its advertisements not old yet
    heard.quiet_for(60)
    assert await poll(hass, loaded) == LinkStatus.IN_USE_ELSEWHERE
    heard.last = None  # Home Assistant forgot it: the last one heard still counts
    assert await poll(hass, loaded) == LinkStatus.IN_USE_ELSEWHERE


@pytest.mark.usefixtures("no_link")
async def test_quiet_with_a_flat_battery_is_not_heard(
    hass: HomeAssistant, loaded: MockConfigEntry, heard: Heard
) -> None:
    coordinator_of(loaded).data.battery = Battery(LOW_BATTERY, ChargeState.ON_BATTERY)
    heard.quiet_for(600)
    assert await poll(hass, loaded) == LinkStatus.NOT_HEARD


async def test_not_found_never_heard(hass: HomeAssistant, loaded: MockConfigEntry, heard: Heard) -> None:
    heard.last = None
    coordinator_of(loaded)._last_heard = None
    with patch("homeassistant.components.bluetooth.async_ble_device_from_address", return_value=None):
        assert await poll(hass, loaded) == LinkStatus.NOT_HEARD


async def test_busy_rejected_and_other_failures(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector, heard: Heard
) -> None:
    projector.busy = True
    assert await poll(hass, loaded) == LinkStatus.NOT_RESPONDING
    projector.busy = False
    projector.bonded = False
    assert await poll(hass, loaded) == LinkStatus.NOT_PAIRED
    projector.bonded = True
    with patch.object(coordinator_of(loaded).client, "read_state", side_effect=BleakError("lost mid-poll")):
        assert await poll(hass, loaded) == LinkStatus.ERROR
    assert await poll(hass, loaded) == LinkStatus.OK


async def test_forgotten_bond_asks_to_pair_again(hass: HomeAssistant, loaded: MockConfigEntry, heard: Heard) -> None:
    """Links that drop as they are made, while BlueZ holds a bond (the entry deleted in Sony's app)."""
    client = coordinator_of(loaded).client

    async def establish_connection(*args, **kwargs):
        err = BleakError("failed to discover services, device disconnected")
        for _ in range(4):
            client._attempt_failed(10.0, err)
        raise err

    async def bonds(address: str) -> list[str]:
        return ["/org/bluez/hci0/dev_DD_77_D5_94_25_80"]

    await drop_link(loaded)
    with (
        patch("custom_components.sony_lspx_p1.client.establish_connection", establish_connection),
        patch("custom_components.sony_lspx_p1.coordinator.bluez_bonds", bonds),
    ):
        assert await poll(hass, loaded) == LinkStatus.NOT_PAIRED
        assert not _reauth_started(hass)
        assert await poll(hass, loaded) == LinkStatus.NOT_PAIRED
    assert _reauth_started(hass)


@pytest.mark.usefixtures("no_link")
async def test_dropped_links_without_a_bluez_bond(hass: HomeAssistant, loaded: MockConfigEntry, heard: Heard) -> None:
    """A proxy's drops, or a link at the edge of range: no pairing asked for."""
    client = coordinator_of(loaded).client

    async def establish_connection(*args, **kwargs):
        err = BleakError("failed to discover services, device disconnected")
        for _ in range(4):
            client._attempt_failed(10.0, err)
        raise err

    with patch("custom_components.sony_lspx_p1.client.establish_connection", establish_connection):
        for _ in range(3):
            assert await poll(hass, loaded) == LinkStatus.NOT_ACCEPTING
    assert not _reauth_started(hass)


@pytest.mark.usefixtures("no_link")
async def test_lasting_reason_raises_an_issue(
    hass: HomeAssistant, loaded: MockConfigEntry, heard: Heard, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue_id = f"projector_unreachable_{loaded.entry_id}"
    issues = ir.async_get(hass)
    heard.quiet_for(60)
    assert await poll(hass, loaded) == LinkStatus.IN_USE_ELSEWHERE
    assert issues.async_get_issue(DOMAIN, issue_id) is None  # not yet: a phone's app may be in use for a while

    monkeypatch.setattr(coordinator_module, "UNREACHABLE_ISSUE_AFTER", 0)
    await poll(hass, loaded)
    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert (issue.translation_key, issue.severity, issue.is_fixable) == (
        "projector_in_use_elsewhere",
        ir.IssueSeverity.WARNING,
        False,
    )
    assert issue.translation_placeholders == {"name": "LSPX-P1 1A2B"}

    heard.quiet_for(0)  # heard again, connections still refused
    assert await poll(hass, loaded) == LinkStatus.NOT_ACCEPTING
    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "projector_not_accepting"


async def test_issue_goes_once_reached_again(
    hass: HomeAssistant, loaded: MockConfigEntry, heard: Heard, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue_id = f"projector_unreachable_{loaded.entry_id}"
    issues = ir.async_get(hass)
    monkeypatch.setattr(coordinator_module, "UNREACHABLE_ISSUE_AFTER", 0)

    async def establish_connection(*args, **kwargs):
        raise BleakError("TimeoutError")

    heard.quiet_for(60)
    await drop_link(loaded)
    with patch("custom_components.sony_lspx_p1.client.establish_connection", establish_connection):
        await poll(hass, loaded)
        await poll(hass, loaded)
    assert issues.async_get_issue(DOMAIN, issue_id) is not None
    assert await poll(hass, loaded) == LinkStatus.OK
    await drop_link(loaded)
    assert issues.async_get_issue(DOMAIN, issue_id) is None

    with patch("custom_components.sony_lspx_p1.client.establish_connection", establish_connection):
        await poll(hass, loaded)
        await poll(hass, loaded)
    assert issues.async_get_issue(DOMAIN, issue_id) is not None
    assert await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.usefixtures("no_link")
async def test_diagnostics(hass: HomeAssistant, loaded: MockConfigEntry, heard: Heard, hass_client) -> None:
    heard.quiet_for(60)
    await poll(hass, loaded)
    diag = await get_diagnostics_for_config_entry(hass, hass_client, loaded)
    connection = diag["bluetooth"]["connection"]
    assert connection["status"] == LinkStatus.IN_USE_ELSEWHERE
    assert connection["since"]
    assert "**REDACTED**" in connection["error"]  # the projector's address


def _reauth_started(hass: HomeAssistant) -> bool:
    return any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
