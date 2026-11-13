"""Client behaviour against the fake projector."""

from __future__ import annotations

import asyncio

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from custom_components.sony_lspx_p1.client import (
    LSPXClient,
    LSPXError,
    NotPairedError,
    ProjectorBusyError,
    home_topology,
)
from custom_components.sony_lspx_p1.protocol import AppState, NetTopology, Op, Poster

from .fake_projector import FakeBleakClient, FakeProjector

SOURCE_WRITES = (Op.STOP_APP, Op.SET_POSTER, Op.SET_NET_TOPOLOGY)


@pytest.fixture
def client(ble_device: BLEDevice, fake_connection: list[FakeBleakClient]) -> LSPXClient:
    return LSPXClient(ble_device, idle_disconnect=False)


async def test_read_state(client: LSPXClient) -> None:
    state = await client.read_state()
    assert state.errors == {}
    assert state.power is True
    assert state.volume == 67
    assert state.brightness == 81
    assert state.focus == -370
    assert state.keystone == 5
    assert state.focus_limits.far == -383
    assert state.software_version == "3.1.15"
    assert (state.app_state, state.poster, state.net_topology) == (AppState.HDMI, 10, NetTopology.WIRELESS_HDMI)
    assert state.hdmi_status == (True, 4)
    assert state.usage_hours == 412.5
    assert state.auto_standby == (3, 1, 0)
    assert (state.syscon_version, state.light_engine_version, state.hdmi_tx_version) == ("1.0.4", "2.01", "1.2.0")
    # Static ranges are reused instead of read again.
    again = await client.read_state(state)
    assert again.focus_limits is state.focus_limits


async def test_manual_values_switch_auto_off(client: LSPXClient, projector: FakeProjector) -> None:
    projector.auto_brightness = [1, 1]
    assert await client.set_brightness(55) == 55
    assert projector.auto_brightness == [0, 0]

    assert await client.set_focus(-100) == -100
    assert projector.focus_assist == 0

    assert await client.set_direction(1) == 1
    assert projector.auto_direction == 0


async def test_unpaired_controller_is_reported(client: LSPXClient, projector: FakeProjector) -> None:
    projector.bonded = False
    with pytest.raises(NotPairedError):
        await client.command(Op.GET_POWER)


async def test_pairing(client: LSPXClient, projector: FakeProjector, fake_connection) -> None:
    projector.bonded = False
    await client.pair("Home Assistant")
    assert projector.bonded is True
    # A first pairing must not remove anything on the host: BlueZ would drop the device.
    assert sum(c.unpair_calls for c in fake_connection) == 0
    # Paired with the explicit call, which the projector answers by dropping the link.
    assert sum(c.pair_calls for c in fake_connection) == 1
    assert projector.controllers[-1] == "Home Assistant"
    assert await client.set_volume(30) == 30


async def test_pairing_falls_back_to_encrypted_read(
    client: LSPXClient, projector: FakeProjector, fake_connection
) -> None:
    projector.bonded = False
    projector.pairing_via_call = False
    await client.pair("Home Assistant")
    assert projector.bonded is True
    assert sum(c.probe_reads for c in fake_connection) >= 1


async def test_pairing_reports_every_failed_attempt(client: LSPXClient, projector: FakeProjector) -> None:
    projector.bonded = False
    projector.pairing_via_read = False
    projector.pairing_via_call = False
    with pytest.raises(LSPXError) as err:
        await client.pair("Home Assistant")
    message = str(err.value)
    assert "encrypted read failed while pairing" in message
    assert "explicit pair failed while pairing" in message
    assert "AuthenticationFailed" in message


async def test_pairing_without_explicit_pair_support(
    ble_device: BLEDevice, projector: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CoreBluetooth has no pair(); the encrypted read triggers pairing instead."""

    async def establish_connection(*args, **kwargs):
        return FakeBleakClient(projector, pair_supported=False)

    monkeypatch.setattr("custom_components.sony_lspx_p1.client.establish_connection", establish_connection)
    client = LSPXClient(ble_device, idle_disconnect=False)
    assert (await client.read_state()).power is True


async def test_busy_projector(client: LSPXClient, projector: FakeProjector) -> None:
    projector.busy = True
    with pytest.raises(ProjectorBusyError):
        await client.command(Op.GET_VOLUME)


async def test_reconnects_after_disconnect(client: LSPXClient, fake_connection: list[FakeBleakClient]) -> None:
    await client.command(Op.GET_POWER)
    fake_connection[-1].connected = False
    await client.command(Op.GET_POWER)
    assert len(fake_connection) == 2


async def test_security_is_attempted_once_per_connection(
    ble_device: BLEDevice, projector: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that keeps failing must not trigger pair() before every command."""
    fake = FakeBleakClient(projector)
    fake.encrypted = True
    fake.probe_fails = True

    async def establish_connection(*args, **kwargs):
        return fake

    monkeypatch.setattr("custom_components.sony_lspx_p1.client.establish_connection", establish_connection)
    client = LSPXClient(ble_device, idle_disconnect=False)
    state = await client.read_state()
    assert state.errors == {}
    assert fake.pair_calls == 1
    assert fake.probe_reads == 2


async def test_repairing_resets_stale_host_bond(
    client: LSPXClient, projector: FakeProjector, fake_connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    projector.bonded = False  # the projector forgot this controller...
    projector.host_bond_stale = True  # ...but the host still has a bond, so pair() does nothing
    await client.pair("Home Assistant", repairing=True)
    assert sum(c.unpair_calls for c in fake_connection) == 1
    assert projector.bonded is True
    assert await client.set_volume(20) == 20


async def test_stale_service_cache_is_rediscovered(
    client: LSPXClient, projector: FakeProjector, fake_connection
) -> None:
    projector.stale_service_cache = True
    projector.bonded = False
    await client.pair("Home Assistant")
    assert projector.bonded is True
    assert len(fake_connection) >= 2  # reconnected with fresh service discovery


async def test_link_dropping_mid_command_fails_fast(client: LSPXClient, fake_connection) -> None:
    await client.command(Op.GET_POWER)
    fake = fake_connection[-1]
    fake.silent = True
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, fake.drop)
    start = loop.time()
    with pytest.raises(BleakError):
        await client.command(Op.GET_VOLUME)
    assert loop.time() - start < 1
    # The next command reconnects.
    assert await client.command(Op.GET_VOLUME) == b"\x43"
    assert len(fake_connection) == 2


async def test_power_read_retries_through_transition(
    client: LSPXClient, projector: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_components.sony_lspx_p1.client.TRANSITION_RETRY_DELAY", 0)
    projector.transition_replies = 2
    assert (await client.read_state()).power is True


async def test_disconnects_soon_after_poll_and_later_after_commands(
    ble_device: BLEDevice, fake_connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_components.sony_lspx_p1.client.POLL_LINGER", 0.05)
    client = LSPXClient(ble_device)
    await client.read_state()
    assert client.is_connected
    await asyncio.sleep(0.15)
    assert not client.is_connected

    await client.command(Op.GET_VOLUME, linger=0.3)
    await client.read_state()  # a poll must not cut a user's linger short
    await asyncio.sleep(0.15)
    assert client.is_connected
    await asyncio.sleep(0.3)
    assert not client.is_connected


async def test_first_pairing_never_removes_the_host_bond(
    client: LSPXClient, projector: FakeProjector, fake_connection
) -> None:
    projector.bonded = False
    projector.host_bond_stale = True
    with pytest.raises(LSPXError):
        await client.pair("Home Assistant")
    assert sum(c.unpair_calls for c in fake_connection) == 0


async def test_notifications_are_retried_while_bluez_catches_up(
    client: LSPXClient, projector: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_components.sony_lspx_p1.client.NOTIFY_RETRY_DELAY", 0)
    projector.notify_failures = 2
    assert (await client.read_state()).power is True


async def test_restriction_and_controllers(client: LSPXClient, projector: FakeProjector) -> None:
    projector.blocked = {0}
    state = await client.read_state()
    assert state.restrict_new_controllers is False
    assert [(c.name, c.allowed) for c in state.controllers] == [("iPhone", False)]

    assert await client.set_restrict_new_controllers(True) is True
    assert projector.restrict == 1
    assert await client.set_restrict_new_controllers(False) is False
    assert projector.restrict == 0


async def test_failing_bonding_reads_leave_the_controls_alone(client: LSPXClient, projector: FakeProjector) -> None:
    projector.bond_failures = 1
    state = await client.read_state()
    assert state.restrict_new_controllers is None
    assert "restrict_new_controllers" in state.errors
    assert state.controllers is not None
    assert state.volume == 67

    # The next polls leave the bonding service alone for a while, keeping the last values.
    projector.bond_ops.clear()
    again = await client.read_state(state)
    assert projector.bond_ops == []
    assert again.controllers == state.controllers
    assert again.errors == {}


async def test_poster_from_hdmi_follows_the_app(client: LSPXClient, projector: FakeProjector) -> None:
    projector.log.clear()
    await client.show_poster(Poster.DIGITAL_CLOCK_1, software_version="3.1.15")
    writes = [(op, payload) for op, payload in projector.log if op in SOURCE_WRITES]
    # Close HDMI, choose the poster, then give the Wi-Fi back to the home network (HomePoster on firmware 3).
    assert writes == [(Op.STOP_APP, b""), (Op.SET_POSTER, b"\x00\x09"), (Op.SET_NET_TOPOLOGY, b"\x11")]
    assert (projector.app_state, projector.poster) == (AppState.POSTER, 9)


async def test_poster_to_poster_only_sets_the_poster(client: LSPXClient, projector: FakeProjector) -> None:
    projector.app_state, projector.topology = AppState.POSTER, NetTopology.HOME_POSTER
    projector.log.clear()
    await client.show_poster(Poster.WINDOW)
    assert [(op, p) for op, p in projector.log if op in SOURCE_WRITES] == [(Op.SET_POSTER, b"\x00\x06")]


async def test_hdmi(client: LSPXClient, projector: FakeProjector) -> None:
    projector.app_state, projector.topology = AppState.POSTER, NetTopology.HOME_POSTER
    await client.show_hdmi()
    assert projector.topology == NetTopology.WIRELESS_HDMI
    assert projector.app_state == AppState.HDMI


def test_home_topology_depends_on_firmware() -> None:
    assert home_topology("3.1.15") == NetTopology.HOME_POSTER
    assert home_topology("2.0.1") == NetTopology.HOME
    assert home_topology(None) == NetTopology.HOME


async def test_switching_waits_for_a_slow_projector(
    client: LSPXClient, projector: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The projector answers a source switch only once it is done, which can take longer than other replies.
    monkeypatch.setattr("custom_components.sony_lspx_p1.client.REPLY_TIMEOUT", 0.05)
    projector.reply_delays = {Op.SET_POSTER: 0.2, Op.SET_NET_TOPOLOGY: 0.2}
    await client.show_poster(Poster.ANALOG_CLOCK_1, software_version="3.1.15")
    assert (projector.poster, projector.topology) == (7, NetTopology.HOME_POSTER)
    await client.show_hdmi()
    assert projector.topology == NetTopology.WIRELESS_HDMI
    # Other commands keep the short timeout.
    projector.reply_delays = {Op.GET_VOLUME: 0.2}
    with pytest.raises(TimeoutError):
        await client.command(Op.GET_VOLUME)
    await asyncio.sleep(0.2)  # let the late reply arrive before the test ends


async def test_standby_after_inactivity_keeps_the_other_triggers(client: LSPXClient, projector: FakeProjector) -> None:
    assert await client.set_standby_after_inactivity(False) == (3, 0, 0)
    projector.auto_standby = [2, 0, 3]
    assert await client.set_standby_after_inactivity(True) == (2, 1, 3)
