"""Client behaviour against the fake projector."""

from __future__ import annotations

import asyncio

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from custom_components.sony_lspx_p1.client import LSPXClient, NotPairedError, ProjectorBusyError
from custom_components.sony_lspx_p1.protocol import Op

from .fake_projector import FakeBleakClient, FakeProjector


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
    assert projector.controllers[-1] == "Home Assistant"
    assert await client.set_volume(30) == 30


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
    monkeypatch.setattr("custom_components.sony_lspx_p1.client.UNPAIR_SETTLE", 0)
    projector.bonded = False  # the projector forgot this controller...
    projector.host_bond_stale = True  # ...but the host still has a bond, so pair() does nothing
    await client.pair("Home Assistant")
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
