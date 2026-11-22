"""What the link did, in diagnostics and the debug log: counted as it happens, never asked of the projector."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

from custom_components.sony_lspx_p1.client import LSPXClient, describe_op
from custom_components.sony_lspx_p1.protocol import BondCommandFailed, BondOp, CommandRejected, ErrorCode, Op

from .conftest import ADDRESS
from .fake_projector import FakeBleakClient, FakeProjector

PROXY_MAC = "11:22:33:44:55:66"


@pytest.fixture
def client(ble_device: BLEDevice, fake_connection: list[FakeBleakClient]) -> LSPXClient:
    return LSPXClient(ble_device, idle_disconnect=False)


def test_describe_op() -> None:
    assert describe_op(Op.GET_POWER) == f"GET_POWER (0x{Op.GET_POWER:04x})"
    assert describe_op(0xFFFF) == "0xffff"
    assert describe_op(BondOp.GET_MY_ID, BondOp) == f"GET_MY_ID (0x{BondOp.GET_MY_ID:02x})"


async def test_commands_are_counted_and_logged(
    client: LSPXClient,
    projector: FakeProjector,
    fake_connection: list[FakeBleakClient],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.sony_lspx_p1.client")
    await client.command(Op.GET_POWER)
    stats = client.stats
    assert (stats.connections, stats.commands, stats.last_command_error) == (1, 1, None)
    assert stats.last_connection is not None and stats.last_connection["seconds"] >= 0
    assert stats.slowest_reply is not None and stats.slowest_reply["command"] == describe_op(Op.GET_POWER)
    assert f"{ADDRESS}: connected in " in caplog.text
    assert f"{ADDRESS}: GET_POWER (0x{Op.GET_POWER:04x}) answered in " in caplog.text

    projector.rejections = {Op.GET_VOLUME: ErrorCode.INVALID_ARGUMENT}
    for _ in range(2):
        with pytest.raises(CommandRejected):
            await client.command(Op.GET_VOLUME)
    assert stats.commands_rejected == {"INVALID_ARGUMENT": 2}
    assert stats.last_command_error is not None
    assert stats.last_command_error["command"] == describe_op(Op.GET_VOLUME)
    assert "INVALID_ARGUMENT" in stats.last_command_error["error"]
    assert "GET_VOLUME (0x" in caplog.text and "failed after" in caplog.text
    projector.rejections = {Op.GET_VOLUME: 0x42}  # a code the app does not know
    with pytest.raises(CommandRejected):
        await client.command(Op.GET_VOLUME)
    assert stats.commands_rejected == {"INVALID_ARGUMENT": 2, "0x42": 1}

    monkeypatch.setattr("custom_components.sony_lspx_p1.client.REPLY_TIMEOUT", 0.05)
    fake_connection[-1].silent = True
    with pytest.raises(TimeoutError):
        await client.command(Op.GET_BRIGHTNESS)
    assert stats.command_timeouts == 1
    assert stats.links_lost == 0  # dropped by the client after the timeout, not lost
    assert "disconnecting after " in caplog.text
    assert stats.commands == 5

    async def hang() -> None:
        await asyncio.Event().wait()

    task = asyncio.ensure_future(client._timed("HANG", hang()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "HANG cancelled after " in caplog.text


async def test_lost_links_and_failed_connections_are_counted(
    client: LSPXClient,
    fake_connection: list[FakeBleakClient],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.sony_lspx_p1.client")
    await client.command(Op.GET_POWER)
    fake_connection[-1].drop()
    assert client.stats.links_lost == 1
    assert client.stats.last_link_lost is not None and client.stats.last_link_lost["while_in_use"] is False
    assert "link lost after " in caplog.text

    await client.command(Op.GET_POWER)
    fake = fake_connection[-1]
    fake.silent = True
    asyncio.get_running_loop().call_later(0.05, fake.drop)
    with pytest.raises(BleakError):
        await client.command(Op.GET_VOLUME)
    assert client.stats.links_lost == 2
    assert client.stats.last_link_lost["while_in_use"] is True

    async def failing_connection(*args, **kwargs):
        raise BleakError(f"LSPXP1_1A2B - {ADDRESS}: Failed to connect")

    monkeypatch.setattr("custom_components.sony_lspx_p1.client.establish_connection", failing_connection)
    with pytest.raises(BleakError):
        await client.command(Op.GET_POWER)
    assert client.stats.connection_failures == 1
    assert client.stats.last_connection_error is not None
    assert "Failed to connect" in client.stats.last_connection_error["error"]
    assert client.stats.connections == 2
    assert "connecting failed after " in caplog.text


async def test_slowest_replies_keep_switches_apart_and_count_rejections(
    client: LSPXClient, projector: FakeProjector
) -> None:
    """A source switch answers only once done (17 s on a real one): it does not hide the slowest other reply."""
    projector.app_state, projector.topology = 0x10, 0x11  # a poster on the home network: no StopApp, no topology
    projector.reply_delays = {Op.SET_POSTER: 0.06}
    await client.show_poster(6)
    switch = client.stats.slowest_switch_reply
    assert switch is not None and switch["command"] == describe_op(Op.SET_POSTER) and switch["seconds"] >= 0.05
    assert client.stats.slowest_reply is None or client.stats.slowest_reply["seconds"] < 0.05
    # A rejection is an answer too, and may be the slowest one.
    projector.reply_delays = {Op.GET_VOLUME: 0.03}
    projector.rejections = {Op.GET_VOLUME: ErrorCode.MAIN_BOARD_STARTING}
    with pytest.raises(CommandRejected):
        await client.command(Op.GET_VOLUME)
    slowest = client.stats.slowest_reply
    assert slowest is not None and slowest["command"] == describe_op(Op.GET_VOLUME)
    assert "MAIN_BOARD_STARTING" in slowest["error"]
    assert client.stats.slowest_switch_reply == switch


async def test_failed_connection_attempts_are_counted(
    ble_device: BLEDevice, projector: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first attempt (through one proxy, say) fails and the retry succeeds: both show, not one slow success."""

    class FlakyClient:
        attempts = 0

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def connect(self, **kwargs: object) -> bool:
            FlakyClient.attempts += 1
            if FlakyClient.attempts == 1:
                raise BleakError(f"{ADDRESS}: no connection slot on proxy {PROXY_MAC}")
            return True

    async def establish_connection(client_class, device, name, disconnected_callback=None, **kwargs):
        attempt = client_class(device, disconnected_callback=disconnected_callback)
        for _ in range(2):  # as bleak-retry-connector does
            try:
                await attempt.connect(timeout=1)
                break
            except BleakError:
                pass
        return FakeBleakClient(projector, disconnected_callback=disconnected_callback)

    monkeypatch.setattr("custom_components.sony_lspx_p1.client.establish_connection", establish_connection)
    client = LSPXClient(ble_device, idle_disconnect=False, client_class=FlakyClient)  # type: ignore[arg-type]
    await client.command(Op.GET_POWER)
    stats = client.stats
    assert (stats.connections, stats.connection_failures, stats.failed_attempts) == (1, 0, 1)
    assert stats.last_connection is not None and stats.last_connection["failed_attempts"] == 1
    assert stats.last_failed_attempt is not None and "no connection slot" in stats.last_failed_attempt["error"]
    assert stats.last_failed_attempt["seconds"] >= 0


async def test_failed_bonding_operations_are_counted(client: LSPXClient, projector: FakeProjector) -> None:
    projector.bond_failures = 1
    with pytest.raises(BondCommandFailed):
        await client.get_restrict_new_controllers()
    assert client.stats.commands_rejected == {"bonding result 2": 1}
    assert client.stats.last_command_error["command"] == f"bonding {describe_op(BondOp.GET_PIN_CODE_FLAG, BondOp)}"


def _scanner_device(adapter: str, source: str, rssi: int) -> SimpleNamespace:
    scanner = SimpleNamespace(adapter=adapter, source=source)
    return SimpleNamespace(scanner=scanner, advertisement=SimpleNamespace(rssi=rssi))


@pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")
async def test_diagnostics_show_the_link_without_addresses(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    projector: FakeProjector,
    hass_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("custom_components.sony_lspx_p1.client.TRANSITION_RETRY_DELAY", 0)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = config_entry.runtime_data
    projector.busy = True
    await coordinator.async_refresh()
    projector.busy = False
    coordinator.client.stats.attempt_failed(1.5, BleakError(f"no connection slot on proxy {PROXY_MAC}"))
    coordinator.client.stats.last_connection_error = {
        "at": "2026-01-01T00:00:00+00:00",
        "seconds": 1.0,
        "error": (
            f"BleakError: {ADDRESS.lower()}: no backend via proxy ({PROXY_MAC}), "
            f"/org/bluez/hci0/dev_{PROXY_MAC.replace(':', '_')}"
        ),
    }
    heard = [_scanner_device("hci0", "local", -80), _scanner_device("hall-proxy", PROXY_MAC, -55)]
    last = SimpleNamespace(source=PROXY_MAC, time=0.0)
    with (
        patch("homeassistant.components.bluetooth.async_scanner_devices_by_address", return_value=heard),
        patch("homeassistant.components.bluetooth.async_last_service_info", return_value=last),
        patch("homeassistant.components.bluetooth.MONOTONIC_TIME", return_value=12.34),
    ):
        diag = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)

    bt = diag["bluetooth"]
    assert bt["heard_by"] == [
        {"adapter": "hall-proxy", "type": "SimpleNamespace", "rssi": -55, "seconds_since_last_advertisement": 12.3},
        {"adapter": "hci0", "type": "SimpleNamespace", "rssi": -80},
    ]
    assert bt["link"]["connections"] >= 1
    assert bt["link"]["commands"] >= 1
    assert "TIMEOUT" in bt["link"]["commands_rejected"]
    error = bt["link"]["last_connection_error"]["error"]
    assert error == "BleakError: **REDACTED**: no backend via proxy (**REDACTED**), /org/bluez/hci0/dev_**REDACTED**"
    assert bt["link"]["last_failed_attempt"]["error"] == "BleakError: no connection slot on proxy **REDACTED**"
    first, busy = bt["recent_polls"]
    assert first["result"] == "ok" and first["seconds"] >= 0
    assert busy["result"].startswith("ProjectorBusyError")
    text = json.dumps(diag).lower()
    assert ADDRESS.lower() not in text and PROXY_MAC not in text
