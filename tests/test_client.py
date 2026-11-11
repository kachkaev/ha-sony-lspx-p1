"""Client behaviour against the fake projector."""

from __future__ import annotations

import pytest
from bleak.backends.device import BLEDevice

from custom_components.sony_lspx_p1.client import LSPXClient, NotPairedError, ProjectorBusyError
from custom_components.sony_lspx_p1.protocol import Op

from .fake_projector import FakeBleakClient, FakeProjector


@pytest.fixture
def client(ble_device: BLEDevice, fake_connection: list[FakeBleakClient]) -> LSPXClient:
    return LSPXClient(ble_device, idle_disconnect=None)


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


async def test_pairing(client: LSPXClient, projector: FakeProjector) -> None:
    projector.bonded = False
    await client.pair("Home Assistant")
    assert projector.bonded is True
    assert projector.controllers[-1] == "Home Assistant"
    assert await client.set_volume(30) == 30


async def test_pairing_without_explicit_pair_support(
    ble_device: BLEDevice, projector: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CoreBluetooth has no pair(); the encrypted read triggers pairing instead."""

    async def establish_connection(*args, **kwargs):
        return FakeBleakClient(projector, pair_supported=False)

    monkeypatch.setattr("custom_components.sony_lspx_p1.client.establish_connection", establish_connection)
    client = LSPXClient(ble_device, idle_disconnect=None)
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
