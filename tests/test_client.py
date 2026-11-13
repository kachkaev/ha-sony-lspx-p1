"""Client behaviour against the fake projector."""

from __future__ import annotations

import asyncio

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from custom_components.sony_lspx_p1.client import (
    ControllerNotFoundError,
    LSPXClient,
    LSPXError,
    NotPairedError,
    OwnControllerError,
    OwnControllerUnknownError,
    ProjectorBusyError,
    home_topology,
)
from custom_components.sony_lspx_p1.protocol import (
    AppState,
    BondCommandFailed,
    BondOp,
    CommandRejected,
    Controller,
    ErrorCode,
    NetTopology,
    Op,
    Poster,
    ProtocolError,
)

from .fake_projector import FakeBleakClient, FakeProjector, _FakeServices

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
    assert list(projector.controllers.values())[-1] == "Home Assistant"
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


REBOOT = (Op.REBOOT, b"")


async def test_reboot_counts_the_link_dropping_as_done(
    client: LSPXClient, projector: FakeProjector, fake_connection
) -> None:
    assert await client.reboot() == "link dropped"
    assert projector.log.count(REBOOT) == 1
    assert not client.is_connected
    # The next command reconnects.
    assert await client.command(Op.GET_VOLUME) == b"\x43"
    assert len(fake_connection) == 2


async def test_reboot_acknowledged_or_unanswered(
    client: LSPXClient, projector: FakeProjector, fake_connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    projector.reboot_drops_link = False
    assert await client.reboot() == "acknowledged"
    assert not client.is_connected  # dropped anyway: the projector is restarting

    monkeypatch.setattr("custom_components.sony_lspx_p1.client.REPLY_TIMEOUT", 0.05)
    await client.command(Op.GET_POWER)
    fake_connection[-1].silent = True
    assert await client.reboot() == "no reply"
    assert projector.log.count(REBOOT) == 2


async def test_reboot_fails_when_it_cannot_be_sent(
    client: LSPXClient, projector: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    projector.busy = True
    with pytest.raises(ProjectorBusyError):
        await client.reboot()
    projector.busy = False
    projector.bonded = False
    with pytest.raises(NotPairedError):
        await client.reboot()

    async def failing_connection(*args, **kwargs):
        raise BleakError("Failed to connect")

    monkeypatch.setattr("custom_components.sony_lspx_p1.client.establish_connection", failing_connection)
    with pytest.raises(BleakError):
        await client.reboot()


async def test_rejected_reads_only_affect_their_own_values(client: LSPXClient, projector: FakeProjector) -> None:
    projector.rejections = {Op.GET_VOLUME: ErrorCode.INVALID_ARGUMENT, Op.GET_PICTURE_RANGE: ErrorCode.RUNTIME_ERROR}
    state = await client.read_state()
    assert state.power is True
    assert (state.volume, state.picture_range) == (None, None)
    assert set(state.errors) == {"volume", "picture_range"}
    assert state.brightness == 81
    # A static value that could not be read is tried again on the next poll.
    projector.rejections = {}
    again = await client.read_state(state)
    assert again.picture_range is not None
    assert again.errors == {}


async def test_power_read_rejected_for_other_reasons_fails_the_poll(
    client: LSPXClient, projector: FakeProjector
) -> None:
    projector.rejections = {Op.GET_POWER: ErrorCode.MODULE_ERROR}
    with pytest.raises(CommandRejected) as err:
        await client.read_state()
    assert err.value.code == ErrorCode.MODULE_ERROR


async def test_stray_frames_are_ignored_while_waiting_for_a_reply(client: LSPXClient, projector: FakeProjector) -> None:
    # A late reply to another command and a frame too short to parse arrive before the real reply.
    projector.unsolicited = [bytes.fromhex("02016151"), b"\x02"]
    assert await client.command(Op.GET_VOLUME) == b"\x43"


async def test_late_disconnect_of_a_replaced_link_is_ignored(
    client: LSPXClient, fake_connection: list[FakeBleakClient]
) -> None:
    await client.command(Op.GET_POWER)
    old = fake_connection[0]
    old.connected = False
    await client.command(Op.GET_POWER)
    old.drop()  # BlueZ reports the old link's disconnection only now
    assert client.is_connected
    await client.command(Op.GET_POWER)
    assert len(fake_connection) == 2


async def test_missing_characteristics_after_rediscovery_fail(
    ble_device: BLEDevice, projector: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def establish_connection(*args, **kwargs):
        client = FakeBleakClient(projector)
        client.services = _FakeServices(hide_bonding=True)  # not even a fresh discovery finds them
        return client

    monkeypatch.setattr("custom_components.sony_lspx_p1.client.establish_connection", establish_connection)
    client = LSPXClient(ble_device, idle_disconnect=False)
    with pytest.raises(BleakError, match="did not expose characteristics"):
        await client.command(Op.GET_POWER)
    assert not client.is_connected


async def test_notifications_that_never_start_fail(
    client: LSPXClient, projector: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_components.sony_lspx_p1.client.NOTIFY_RETRY_DELAY", 0)
    projector.notify_failures = 10
    with pytest.raises(BleakError, match="StartNotify"):
        await client.command(Op.GET_POWER)
    assert not client.is_connected


async def test_pairing_refused_bonding_mode(
    client: LSPXClient, projector: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    projector.bonded = False
    monkeypatch.setattr(projector, "handle_bond", lambda data, encrypted: b"\x02\x10")
    with pytest.raises(LSPXError, match="refused bonding mode"):
        await client.pair("Home Assistant")
    assert projector.bonded is False


async def test_pairing_on_a_stack_without_explicit_pairing(
    ble_device: BLEDevice, projector: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CoreBluetooth: the explicit pair() attempt fails at once and the encrypted read pairs instead."""

    async def establish_connection(*args, **kwargs):
        return FakeBleakClient(projector, pair_supported=False, disconnected_callback=kwargs["disconnected_callback"])

    monkeypatch.setattr("custom_components.sony_lspx_p1.client.establish_connection", establish_connection)
    projector.bonded = False
    client = LSPXClient(ble_device, idle_disconnect=False)
    await client.pair("Home Assistant")
    assert projector.bonded is True
    assert await client.set_volume(40) == 40


async def test_bonded_controllers(client: LSPXClient, projector: FakeProjector) -> None:
    projector.blocked = {1}
    projector.controllers = {0: "iPhone", 1: "Pixel"}
    assert await client.bonded_controllers() == [(0, 0, "iPhone"), (1, 1, "Pixel")]


async def test_empty_restriction_reply_is_a_protocol_error(
    client: LSPXClient, projector: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(projector, "handle_bond", lambda data, encrypted: bytes([1, data[0]]))
    with pytest.raises(ProtocolError):
        await client.get_restrict_new_controllers()


async def test_bonding_service_calls_fail_on_bluetooth_errors(
    client: LSPXClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_connection(*args, **kwargs):
        raise BleakError("Failed to connect")

    monkeypatch.setattr("custom_components.sony_lspx_p1.client.establish_connection", failing_connection)
    with pytest.raises(BleakError):
        await client.get_restrict_new_controllers()
    with pytest.raises(BleakError):
        await client.read_controllers()
    assert not client.is_connected


async def test_reboot_rejected(client: LSPXClient, projector: FakeProjector) -> None:
    projector.rejections = {Op.REBOOT: ErrorCode.UNKNOWN_COMMAND}
    with pytest.raises(CommandRejected):
        await client.reboot()
    assert not client.is_connected


@pytest.fixture
def three_controllers(projector: FakeProjector) -> FakeProjector:
    """Two entries named Home Assistant, as after pairing again through another adapter; 0xB31D is us."""
    projector.controllers = {0x6531: "iPhone", 0xB31D: "Home Assistant", 0x0C07: "Home Assistant"}
    projector.my_id = 0xB31D
    return projector


async def test_remove_block_and_allow_controllers(client: LSPXClient, three_controllers: FakeProjector) -> None:
    projector = three_controllers
    controllers = await client.manage_controller(BondOp.BLOCK_CONTROLLER, 0x6531)
    assert projector.bond_writes[-1] == b"\x03\x65\x31"  # [op][id:u16 big-endian]
    assert [(c.id, c.allowed) for c in controllers] == [(0x6531, False), (0xB31D, True), (0x0C07, True)]

    controllers = await client.manage_controller(BondOp.ALLOW_CONTROLLER, 0x6531)
    assert projector.bond_writes[-1] == b"\x02\x65\x31"
    assert all(c.allowed for c in controllers)

    projector.bond_writes.clear()
    controllers = await client.manage_controller(BondOp.DELETE_CONTROLLER, 0x0C07)
    # Removing or blocking asks which entry is this controller first.
    assert projector.bond_writes == [b"\x06", b"\x01\x0c\x07"]
    assert [c.id for c in controllers] == [0x6531, 0xB31D]
    assert set(projector.controllers) == {0x6531, 0xB31D}


async def test_never_removes_or_blocks_itself(client: LSPXClient, three_controllers: FakeProjector) -> None:
    for op in (BondOp.DELETE_CONTROLLER, BondOp.BLOCK_CONTROLLER):
        with pytest.raises(OwnControllerError):
            await client.manage_controller(op, 0xB31D)
    assert BondOp.DELETE_CONTROLLER not in three_controllers.bond_ops
    assert BondOp.BLOCK_CONTROLLER not in three_controllers.bond_ops
    # Allowing itself cannot cut it off.
    assert len(await client.manage_controller(BondOp.ALLOW_CONTROLLER, 0xB31D)) == 3


async def test_unknown_controller_is_refused(client: LSPXClient, three_controllers: FakeProjector) -> None:
    for op in (BondOp.DELETE_CONTROLLER, BondOp.ALLOW_CONTROLLER):
        with pytest.raises(ControllerNotFoundError):
            await client.manage_controller(op, 0x4242)
    assert [w[0] for w in three_controllers.bond_writes] == [BondOp.GET_MY_ID]


@pytest.mark.parametrize("my_id", [None, 0x4242], ids=["no answer", "not in the list"])
async def test_nothing_removed_without_knowing_which_entry_is_this_one(
    client: LSPXClient, three_controllers: FakeProjector, my_id: int | None
) -> None:
    three_controllers.my_id = my_id
    with pytest.raises(OwnControllerUnknownError):
        await client.manage_controller(BondOp.DELETE_CONTROLLER, 0x0C07)
    assert len(three_controllers.controllers) == 3


async def test_failed_controller_operation(client: LSPXClient, three_controllers: FakeProjector) -> None:
    three_controllers.failing_bond_ops = {BondOp.DELETE_CONTROLLER}
    with pytest.raises(BondCommandFailed):
        await client.manage_controller(BondOp.DELETE_CONTROLLER, 0x0C07)
    assert len(three_controllers.controllers) == 3


async def test_list_after_a_change_that_cannot_be_read_back(
    client: LSPXClient, three_controllers: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = client.read_controllers
    calls = 0

    async def flaky_read(*, linger: float = 15.0) -> list[Controller]:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise BleakError("link lost")
        return await read(linger=linger)

    monkeypatch.setattr(client, "read_controllers", flaky_read)
    controllers = await client.manage_controller(BondOp.DELETE_CONTROLLER, 0x0C07)
    assert [c.id for c in controllers] == [0x6531, 0xB31D]
    assert set(three_controllers.controllers) == {0x6531, 0xB31D}


async def test_manage_controller_rejects_other_operations(client: LSPXClient) -> None:
    with pytest.raises(ValueError, match="not an operation"):
        await client.manage_controller(BondOp.SET_PIN_CODE_FLAG, 1)
