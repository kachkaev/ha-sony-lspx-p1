"""Async Bluetooth LE client for the Sony LSPX-P1 projector.

Depends only on bleak and bleak-retry-connector so it also runs outside Home Assistant
(see tools/lspx_cli.py). One command is in flight at a time; the connection is opened on
demand and dropped after a short idle period so that the official smartphone app can
still connect.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from .protocol import (
    BOND_RESULT_SUCCESS,
    CHAR_BOND_CONTROL,
    CHAR_BONDED_LIST,
    CHAR_BONDING_NAME,
    CHAR_COMMAND,
    CHAR_SECURITY_PROBE,
    STATE_READS,
    STATIC_READS,
    CommandRejected,
    ErrorCode,
    Op,
    PictureMode,
    ProjectorState,
    ProtocolError,
    check_reply,
    encode_bond_activation,
    encode_command,
    parse_bonded_list,
    payload_auto_brightness,
    payload_bool,
    payload_focus,
    payload_keystone,
    payload_power,
    payload_u8,
)

_LOGGER = logging.getLogger(__name__)

REPLY_TIMEOUT = 8.0
DEFAULT_IDLE_DISCONNECT = 20.0
TRANSITION_RETRIES = 3
TRANSITION_RETRY_DELAY = 1.5


class LSPXError(Exception):
    """Base error for the client."""


class NotPairedError(LSPXError):
    """The projector rejected the link: this controller is not (or no longer) bonded."""


class ProjectorBusyError(LSPXError):
    """The projector's main board does not answer (standby, booting or stuck)."""


class LSPXClient:
    """Talks to one projector."""

    def __init__(
        self,
        ble_device: BLEDevice,
        *,
        idle_disconnect: float | None = DEFAULT_IDLE_DISCONNECT,
        client_class: type[BleakClient] = BleakClientWithServiceCache,
    ) -> None:
        self._ble_device = ble_device
        self._idle_disconnect = idle_disconnect
        self._client_class = client_class
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._replies: asyncio.Queue[bytes] = asyncio.Queue()
        self._idle_handle: asyncio.TimerHandle | None = None
        self._secured = False
        self._listeners: list[Callable[[], None]] = []

    # --- connection management ----------------------------------------------

    @property
    def address(self) -> str:
        return self._ble_device.address

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Update the device handle (Home Assistant may route through another proxy)."""
        self._ble_device = ble_device

    def add_disconnect_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    def _on_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.debug("%s: disconnected", self.address)
        self._client = None
        self._secured = False
        for cb in list(self._listeners):
            cb()

    def _on_notify(self, _sender: object, data: bytearray) -> None:
        self._replies.put_nowait(bytes(data))

    async def _connect(self) -> BleakClient:
        if self._client is not None and self._client.is_connected:
            return self._client
        _LOGGER.debug("%s: connecting", self.address)
        client = await establish_connection(
            self._client_class,
            self._ble_device,
            self._ble_device.name or self.address,
            disconnected_callback=self._on_disconnect,
            ble_device_callback=lambda: self._ble_device,
        )
        self._client = client
        self._secured = False
        await client.start_notify(CHAR_COMMAND, self._on_notify)
        return client

    async def _secure(self, client: BleakClient) -> None:
        """Make sure the link is encrypted using the stored bond.

        CoreBluetooth and most stacks encrypt a bonded link when an encrypted attribute is
        accessed; ESPHome proxies and BlueZ also accept an explicit pair() call.
        """
        if self._secured:
            return
        try:
            await client.read_gatt_char(CHAR_SECURITY_PROBE)
            self._secured = True
            return
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("%s: security probe read failed: %s", self.address, err)
        with contextlib.suppress(NotImplementedError, BleakError, TimeoutError):
            await client.pair()
        try:
            await client.read_gatt_char(CHAR_SECURITY_PROBE)
            self._secured = True
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("%s: link still not encrypted: %s", self.address, err)

    def _schedule_idle_disconnect(self) -> None:
        if self._idle_disconnect is None:
            return
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        loop = asyncio.get_running_loop()
        self._idle_handle = loop.call_later(self._idle_disconnect, lambda: asyncio.ensure_future(self.disconnect()))

    async def disconnect(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        async with self._lock:
            client, self._client = self._client, None
            self._secured = False
            if client is not None and client.is_connected:
                with contextlib.suppress(BleakError, TimeoutError):
                    await client.disconnect()

    # --- raw commands --------------------------------------------------------

    async def _exchange(self, client: BleakClient, op: int, payload: bytes) -> bytes:
        while not self._replies.empty():
            self._replies.get_nowait()
        await client.write_gatt_char(CHAR_COMMAND, encode_command(op, payload), response=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + REPLY_TIMEOUT
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"no reply to command {op}")
            frame = await asyncio.wait_for(self._replies.get(), remaining)
            try:
                return check_reply(op, frame)
            except ProtocolError as err:
                # Late reply to an earlier command or an unsolicited notification.
                _LOGGER.debug("%s: ignoring frame %s (%s)", self.address, frame.hex(), err)

    async def command(self, op: int, payload: bytes = b"") -> bytes:
        """Send one command and return the reply data."""
        async with self._lock:
            try:
                client = await self._connect()
                await self._secure(client)
                try:
                    return await self._exchange(client, op, payload)
                except CommandRejected as err:
                    if err.code != ErrorCode.ACCESS_DENIED:
                        raise
                    # Re-secure once: the bond may exist but the link was not encrypted yet.
                    self._secured = False
                    with contextlib.suppress(NotImplementedError, BleakError, TimeoutError):
                        await client.pair()
                    await self._secure(client)
                    try:
                        return await self._exchange(client, op, payload)
                    except CommandRejected as err2:
                        if err2.code == ErrorCode.ACCESS_DENIED:
                            raise NotPairedError(
                                "the projector does not accept commands from this controller; pair again"
                            ) from err2
                        raise
            except CommandRejected as err:
                if err.code == ErrorCode.TIMEOUT:
                    raise ProjectorBusyError(str(err)) from err
                raise
            finally:
                self._schedule_idle_disconnect()

    # --- pairing ----------------------------------------------------------------

    async def pair(self, controller_name: str = "Home Assistant") -> None:
        """Register this controller with the projector (same steps as the official app)."""
        async with self._lock:
            client = await self._connect()
            bond_replies: asyncio.Queue[bytes] = asyncio.Queue()
            await client.start_notify(CHAR_BOND_CONTROL, lambda _s, d: bond_replies.put_nowait(bytes(d)))
            try:
                await client.write_gatt_char(CHAR_BOND_CONTROL, encode_bond_activation(), response=True)
                try:
                    reply = await asyncio.wait_for(bond_replies.get(), 5)
                except TimeoutError as err:
                    raise LSPXError("projector did not confirm bonding mode") from err
                if not reply or reply[0] != BOND_RESULT_SUCCESS:
                    raise LSPXError(f"projector refused bonding mode (reply {reply.hex()})")
                await client.write_gatt_char(CHAR_BONDING_NAME, controller_name.encode("utf-8")[:32], response=True)
                with contextlib.suppress(NotImplementedError):
                    await client.pair()
                self._secured = False
                await self._secure(client)
            finally:
                with contextlib.suppress(BleakError):
                    await client.stop_notify(CHAR_BOND_CONTROL)
        # Prove it works: an ACCESS_DENIED here means pairing did not take.
        # A busy main board still means we are authorised.
        with contextlib.suppress(ProjectorBusyError):
            await self.command(Op.GET_POWER)

    async def bonded_controllers(self) -> list[tuple[int, int, str]]:
        async with self._lock:
            client = await self._connect()
            try:
                return parse_bonded_list(bytes(await client.read_gatt_char(CHAR_BONDED_LIST)))
            finally:
                self._schedule_idle_disconnect()

    # --- state ------------------------------------------------------------------

    async def _read(self, op: Op, decoder) -> object:
        return decoder(await self.command(op))

    async def read_state(self, previous: ProjectorState | None = None) -> ProjectorState:
        """Read everything. Static ranges are reused from ``previous`` when available."""
        state = ProjectorState()
        # While switching between standby and on, the projector briefly answers
        # UNKNOWN_COMMAND or TIMEOUT. Retry a few times before giving up.
        for attempt in range(TRANSITION_RETRIES):
            try:
                state.power = await self._read(Op.GET_POWER, STATE_READS["power"][1])
                break
            except (ProjectorBusyError, CommandRejected) as err:
                if isinstance(err, CommandRejected) and err.code != ErrorCode.UNKNOWN_COMMAND:
                    raise
                if attempt == TRANSITION_RETRIES - 1:
                    raise
                await asyncio.sleep(TRANSITION_RETRY_DELAY)
        for attr, (op, decoder) in STATE_READS.items():
            if attr == "power":
                continue
            try:
                setattr(state, attr, await self._read(op, decoder))
            except (ProjectorBusyError, CommandRejected, ProtocolError) as err:
                state.errors[attr] = str(err)
        for attr, (op, decoder) in STATIC_READS.items():
            cached = getattr(previous, attr, None) if previous else None
            if cached is not None:
                setattr(state, attr, cached)
                continue
            try:
                setattr(state, attr, await self._read(op, decoder))
            except (ProjectorBusyError, CommandRejected, ProtocolError) as err:
                state.errors[attr] = str(err)
        return state

    # --- setters ----------------------------------------------------------------
    # Each returns the value read back from the projector.

    async def _set_and_read(self, set_op: Op, payload: bytes, get_op: Op, decoder) -> object:
        await self.command(set_op, payload)
        return await self._read(get_op, decoder)

    async def set_power(self, on: bool) -> None:
        # Reading the power state right after switching is unreliable while the projector
        # goes to standby or boots, so callers should poll later.
        await self.command(Op.SET_POWER, payload_power(on))

    async def set_volume(self, value: int) -> int:
        return await self._set_and_read(Op.SET_VOLUME, payload_u8(value), Op.GET_VOLUME, STATE_READS["volume"][1])

    async def set_xloud(self, on: bool) -> bool:
        return await self._set_and_read(Op.SET_XLOUD, payload_bool(on), Op.GET_XLOUD, STATE_READS["xloud"][1])

    async def set_auto_brightness(self, on: bool) -> bool:
        return await self._set_and_read(
            Op.SET_AUTO_BRIGHTNESS,
            payload_auto_brightness(on),
            Op.GET_AUTO_BRIGHTNESS,
            STATE_READS["auto_brightness"][1],
        )

    async def set_brightness(self, value: int, *, disable_auto: bool = True) -> int:
        if disable_auto and await self._read(Op.GET_AUTO_BRIGHTNESS, STATE_READS["auto_brightness"][1]):
            await self.set_auto_brightness(False)
        return await self._set_and_read(
            Op.SET_BRIGHTNESS, payload_u8(value), Op.GET_BRIGHTNESS, STATE_READS["brightness"][1]
        )

    async def set_focus_assist(self, on: bool) -> bool:
        return await self._set_and_read(
            Op.SET_FOCUS_ASSIST, payload_bool(on), Op.GET_FOCUS_ASSIST, STATE_READS["focus_assist"][1]
        )

    async def set_focus(self, value: int, *, disable_auto: bool = True) -> int:
        if disable_auto and await self._read(Op.GET_FOCUS_ASSIST, STATE_READS["focus_assist"][1]):
            await self.set_focus_assist(False)
        return await self._set_and_read(Op.SET_FOCUS, payload_focus(value), Op.GET_FOCUS, STATE_READS["focus"][1])

    async def set_keystone(self, value: int) -> int:
        return await self._set_and_read(
            Op.SET_KEYSTONE, payload_keystone(value), Op.GET_KEYSTONE, STATE_READS["keystone"][1]
        )

    async def set_auto_direction(self, on: bool) -> bool:
        return await self._set_and_read(
            Op.SET_AUTO_DIRECTION, payload_bool(on), Op.GET_AUTO_DIRECTION, STATE_READS["auto_direction"][1]
        )

    async def set_direction(self, value: int, *, disable_auto: bool = True) -> int:
        if disable_auto and await self._read(Op.GET_AUTO_DIRECTION, STATE_READS["auto_direction"][1]):
            await self.set_auto_direction(False)
        return await self._set_and_read(
            Op.SET_DIRECTION, payload_u8(value), Op.GET_DIRECTION, STATE_READS["direction"][1]
        )

    async def set_picture_mode(self, mode: int) -> int:
        return await self._set_and_read(
            Op.SET_PICTURE_MODE, payload_u8(PictureMode(mode)), Op.GET_PICTURE_MODE, STATE_READS["picture_mode"][1]
        )

    async def set_picture_value(self, attr: str, value: int) -> int:
        ops = {
            "picture_brightness": (Op.SET_PICTURE_BRIGHTNESS, Op.GET_PICTURE_BRIGHTNESS),
            "picture_contrast": (Op.SET_PICTURE_CONTRAST, Op.GET_PICTURE_CONTRAST),
            "picture_saturation": (Op.SET_PICTURE_SATURATION, Op.GET_PICTURE_SATURATION),
            "picture_sharpness": (Op.SET_PICTURE_SHARPNESS, Op.GET_PICTURE_SHARPNESS),
        }
        set_op, get_op = ops[attr]
        return await self._set_and_read(set_op, payload_u8(value), get_op, STATE_READS[attr][1])
