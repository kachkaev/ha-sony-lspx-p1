"""Replies that arrive twice, and a stale frame at the start of each connection.

Seen on a real projector through Home Assistant (firmware 3.1.15, 2026-11-22): every reply was notified twice, the
copy at most about one command later, and each new connection began with the last frame of the connection before.
The protocol has no sequence numbers, so a copy that arrives after the next command with the same opcode was sent
would pass for its reply.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sony_lspx_p1 import client as client_module
from custom_components.sony_lspx_p1.client import LSPXClient, LSPXError
from custom_components.sony_lspx_p1.protocol import (
    BOND_ACTIVATE_OPERATION,
    CHAR_BOND_CONTROL,
    CHAR_COMMAND,
    CHAR_SECURITY_PROBE,
    STATE_READS,
    STATIC_READS,
    AppState,
    BondOp,
    CommandRejected,
    NetTopology,
    Op,
    Poster,
)

from .fake_projector import FakeBleakClient, FakeProjector

# Replies take a moment and their copies come a little later, after the next command is written but before its reply,
# as for about a third of the replies in the real log (the others came before the next command and were dropped).
REPLY_LATENCY = 0.02
COPY_DELAY = 0.01
MP = "media_player.lspx_p1_1a2b"


@pytest.fixture(autouse=True)
async def copies_delivered(projector: FakeProjector) -> AsyncGenerator[None]:
    """Let the last copies arrive before the test ends, so no timer outlives it."""
    yield
    if projector.duplicate_delay is not None or projector.replay_on_subscribe is not None:
        await asyncio.sleep(0.2)


@pytest.fixture
def repeating(projector: FakeProjector) -> FakeProjector:
    projector.reply_latency = REPLY_LATENCY
    projector.duplicate_delay = COPY_DELAY
    projector.replay_on_subscribe = 0  # as seen: before the first command of a connection
    return projector


@pytest.fixture
def client(ble_device: BLEDevice, fake_connection: list[FakeBleakClient]) -> LSPXClient:
    return LSPXClient(ble_device, idle_disconnect=False)


@pytest.fixture
def long_window(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """A window far longer than any reply here, so waiting it out cannot pass for waiting for a copy."""
    monkeypatch.setattr(client_module, "DUPLICATE_WINDOW", 1.0)
    yield


def ops(projector: FakeProjector) -> list[int]:
    return [op for op, _payload in projector.log]


async def test_poll_reads_every_value_once(
    client: LSPXClient, repeating: FakeProjector, fake_connection: list[FakeBleakClient]
) -> None:
    state = await client.read_state()
    assert state.errors == {}
    assert (state.volume, state.brightness, state.focus, state.usage_hours) == (67, 81, -370, 412.5)
    assert state.software_version == "3.1.15"
    assert state.restrict_new_controllers is False
    assert [c.name for c in state.controllers] == ["iPhone"]
    assert ops(repeating) == [op for op, _decoder in (*STATE_READS.values(), *STATIC_READS.values())]
    assert repeating.bond_ops == [BondOp.GET_PIN_CODE_FLAG]

    # The next poll, on a new connection that starts with the last frame of the one before.
    await client.disconnect()
    repeating.log.clear()
    repeating.volume = 30
    again = await client.read_state(state)
    assert again.errors == {}
    assert again.volume == 30
    assert ops(repeating) == [op for op, _decoder in STATE_READS.values()]
    assert len(fake_connection) == 2
    assert repeating.copies_sent > 0


async def test_set_and_read_back(client: LSPXClient, repeating: FakeProjector) -> None:
    repeating.auto_brightness = [1, 1]
    assert await client.set_brightness(55) == 55
    assert repeating.auto_brightness == [0, 0]
    assert await client.set_volume(20) == 20
    assert await client.set_picture_rgb_channel(1, 40) == (100, 40, 100)
    assert await client.set_direction(1) == 1
    assert ops(repeating) == [
        Op.GET_AUTO_BRIGHTNESS,
        Op.SET_AUTO_BRIGHTNESS,
        Op.GET_AUTO_BRIGHTNESS,
        Op.SET_BRIGHTNESS,
        Op.GET_BRIGHTNESS,
        Op.SET_VOLUME,
        Op.GET_VOLUME,
        Op.GET_PICTURE_RGB,
        Op.SET_PICTURE_RGB,
        Op.GET_PICTURE_RGB,
        Op.GET_AUTO_DIRECTION,
        Op.SET_AUTO_DIRECTION,
        Op.GET_AUTO_DIRECTION,
        Op.SET_DIRECTION,
        Op.GET_DIRECTION,
    ]


async def test_source_switch(client: LSPXClient, repeating: FakeProjector) -> None:
    # Switch steps answer only once done; their copies follow as for any reply.
    repeating.reply_delays = {Op.SET_POSTER: 0.05, Op.SET_NET_TOPOLOGY: 0.05}
    await client.show_poster(Poster.WINDOW, software_version="3.1.15")
    assert (repeating.app_state, repeating.poster, repeating.topology) == (
        AppState.POSTER,
        Poster.WINDOW,
        NetTopology.HOME_POSTER,
    )
    await client.show_hdmi()
    assert repeating.topology == NetTopology.WIRELESS_HDMI
    assert ops(repeating) == [
        Op.GET_NET_TOPOLOGY,
        Op.GET_APP_STATE,
        Op.STOP_APP,
        Op.SET_POSTER,
        Op.SET_NET_TOPOLOGY,
        Op.SET_NET_TOPOLOGY,
    ]


async def test_same_read_twice_in_a_row_gets_the_new_value(client: LSPXClient, repeating: FakeProjector) -> None:
    assert await client.command(Op.GET_VOLUME) == b"\x43"
    repeating.volume = 30  # changed meanwhile, say with the remote
    assert await client.command(Op.GET_VOLUME) == b"\x1e"
    assert ops(repeating) == [Op.GET_VOLUME, Op.GET_VOLUME]


async def test_command_retried_after_a_rejection_is_not_rejected_again(
    client: LSPXClient, repeating: FakeProjector
) -> None:
    """Retrying at once (as while the projector starts up) must not take the first attempt's rejection again."""
    repeating.transition_code = 0xE0
    repeating.transition_replies = 1
    with pytest.raises(CommandRejected):
        await client.show_hdmi()
    repeating.app_state, repeating.topology = AppState.POSTER, NetTopology.HOME_POSTER
    await client.show_hdmi()
    assert repeating.topology == NetTopology.WIRELESS_HDMI
    assert ops(repeating) == [Op.SET_NET_TOPOLOGY, Op.SET_NET_TOPOLOGY]


async def test_rejection_retried_once_secured_is_not_rejected_again(
    ble_device: BLEDevice, repeating: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ACCESS_DENIED reply makes the client secure the link and resend at once; its copy is not the answer."""

    fake = FakeBleakClient(repeating, pair_supported=False)
    failing_probes = iter([True, True])  # the first securing fails, so the link stays unencrypted until the retry
    real_read = fake.read_gatt_char

    async def read_gatt_char(char: str) -> bytearray:
        if char == CHAR_SECURITY_PROBE and next(failing_probes, False):
            raise BleakError("Insufficient Encryption")
        return await real_read(char)

    fake.read_gatt_char = read_gatt_char  # type: ignore[method-assign]

    async def establish_connection(*args: object, **kwargs: object) -> FakeBleakClient:
        return fake

    monkeypatch.setattr(client_module, "establish_connection", establish_connection)
    client = LSPXClient(ble_device, idle_disconnect=False)
    assert await client.command(Op.GET_VOLUME) == b"\x43"
    assert ops(repeating) == [Op.GET_VOLUME, Op.GET_VOLUME]


async def test_new_connection_does_not_take_the_last_frame_for_a_reply(
    client: LSPXClient, projector: FakeProjector, fake_connection: list[FakeBleakClient]
) -> None:
    """The frame a connection starts with may come only after the first command is written."""
    projector.replay_on_subscribe = COPY_DELAY
    projector.reply_delays = {Op.GET_PICTURE_RGB: 2 * COPY_DELAY}  # so it comes while the first reply is awaited
    assert await client.set_picture_rgb_channel(0, 80) == (80, 100, 100)
    await client.disconnect()
    projector.rgb = [50, 60, 70]  # changed meanwhile
    assert await client.set_picture_rgb_channel(2, 90) == (50, 60, 90)
    assert len(fake_connection) == 2
    assert projector.copies_sent == 1


async def test_bonding_reads_on_a_new_subscription(
    client: LSPXClient, repeating: FakeProjector, fake_connection: list[FakeBleakClient]
) -> None:
    """Each bonding operation subscribes anew; a frame resent on subscribing is not its reply."""
    state = await client.read_state()
    assert state.restrict_new_controllers is False
    repeating.restrict = 1  # switched on meanwhile, say in the app
    await client.disconnect()
    again = await client.read_state(state)
    assert again.restrict_new_controllers is True

    assert await client.set_restrict_new_controllers(False) is False
    assert repeating.restrict == 0
    assert repeating.bond_ops == [
        BondOp.GET_PIN_CODE_FLAG,
        BondOp.GET_PIN_CODE_FLAG,
        BondOp.SET_PIN_CODE_FLAG,
        BondOp.GET_PIN_CODE_FLAG,
    ]


async def test_controllers_managed_with_repeated_frames(client: LSPXClient, repeating: FakeProjector) -> None:
    repeating.controllers = {0x3101: "iPhone", 0x3202: "Home Assistant"}
    repeating.my_id = 0x3202
    assert await client.get_my_controller_id() == 0x3202
    controllers = await client.manage_controller(BondOp.BLOCK_CONTROLLER, 0x3101)
    assert [(c.id, c.allowed) for c in controllers] == [(0x3101, False), (0x3202, True)]
    assert repeating.bond_ops == [BondOp.GET_MY_ID, BondOp.GET_MY_ID, BondOp.BLOCK_CONTROLLER]


@pytest.mark.parametrize(
    "resent",
    [bytes([1, BondOp.GET_PIN_CODE_FLAG, 0]), bytes([1, BOND_ACTIVATE_OPERATION])],
    ids=["another_operation", "an_earlier_bonding_window"],
)
async def test_pairing_does_not_take_a_resent_frame_for_the_bonding_window(
    client: LSPXClient, projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, resent: bytes
) -> None:
    """Opening the bonding window waits for its own answer, not a frame sent again on subscribing."""
    monkeypatch.setattr(client_module, "PAIRING_ATTEMPTS", ((client_module.PAIR_BY_CALL, False),))
    controllers = dict(projector.controllers)
    projector.bonded = False
    projector.replay_on_subscribe = 0
    projector.last_frames[CHAR_BOND_CONTROL] = resent
    monkeypatch.setattr(projector, "handle_bond", lambda data, encrypted: b"\x02\x10")  # bonding mode refused
    with pytest.raises(LSPXError, match="refused bonding mode"):
        await client.pair("Home Assistant")
    assert projector.controllers == controllers  # no name written


async def test_pairing_with_repeated_frames(client: LSPXClient, repeating: FakeProjector) -> None:
    repeating.bonded = False
    repeating.last_frames[CHAR_COMMAND] = b"\x02\x08\x01\x10"  # left from an earlier session
    await client.pair("Home Assistant")
    assert repeating.bonded is True
    assert await client.set_volume(30) == 30


@pytest.mark.usefixtures("long_window")
async def test_without_copies_only_a_repeated_command_waits(client: LSPXClient, projector: FakeProjector) -> None:
    """Where replies come once, the same command twice in a row waits out the window, other commands not at all."""
    loop = asyncio.get_running_loop()
    await client.command(Op.GET_VOLUME)
    start = loop.time()
    await client.command(Op.GET_BRIGHTNESS)
    assert loop.time() - start < 0.5
    start = loop.time()
    assert await client.command(Op.GET_BRIGHTNESS) == b"\x51"
    assert loop.time() - start >= 0.5


@pytest.mark.usefixtures("long_window")
async def test_with_copies_a_repeated_command_waits_only_for_the_copy(
    client: LSPXClient, repeating: FakeProjector
) -> None:
    loop = asyncio.get_running_loop()
    await client.command(Op.GET_VOLUME)
    start = loop.time()
    await client.command(Op.GET_VOLUME)
    assert loop.time() - start < 0.5  # the copy (after COPY_DELAY), not the whole window


@pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")
async def test_source_switch_during_start_up_with_repeated_replies(
    hass: HomeAssistant, config_entry: MockConfigEntry, projector: FakeProjector
) -> None:
    """A switch rejected during the start-up waits for the main board (the probe takes the other rejections) and
    tries again: each attempt must get its own answer, not the copy of an earlier rejection."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    projector.reply_latency = REPLY_LATENCY
    projector.duplicate_delay = COPY_DELAY
    projector.replay_on_subscribe = 0
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    await hass.services.async_call("media_player", "turn_on", {ATTR_ENTITY_ID: MP}, blocking=True)
    projector.transition_code = 0xE0
    projector.transition_replies = 3
    projector.log.clear()
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
    )
    assert hass.states.get(MP).attributes["source"] == "Window"
    switch = [op for op in ops(projector) if op in (Op.GET_NET_TOPOLOGY, Op.GET_APP_STATE, Op.STOP_APP, Op.SET_POSTER)]
    # Rejected at least once, then through: none answered by the copy of an earlier rejection.
    assert switch.count(Op.GET_NET_TOPOLOGY) >= 2
    assert switch.count(Op.SET_POSTER) == 1
    assert projector.transition_replies == 0
    assert projector.poster == Poster.WINDOW
    await hass.async_block_till_done(wait_background_tasks=True)  # the probe's poll, once the board answered
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_waiting_for_a_copy_ends_when_the_link_drops(
    client: LSPXClient,
    projector: FakeProjector,
    fake_connection: list[FakeBleakClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "DUPLICATE_WINDOW", 5.0)
    await client.command(Op.GET_VOLUME)
    link = fake_connection[-1]
    write = link.write_gatt_char

    async def write_while_connected(char: str, data: bytes, response: bool = True) -> None:
        if not link.connected:
            raise BleakError("Not connected")
        await write(char, data, response)

    monkeypatch.setattr(link, "write_gatt_char", write_while_connected)
    loop = asyncio.get_running_loop()
    loop.call_later(0.01, link.drop)
    start = loop.time()
    with pytest.raises(BleakError):
        await client.command(Op.GET_VOLUME)
    assert loop.time() - start < 1
