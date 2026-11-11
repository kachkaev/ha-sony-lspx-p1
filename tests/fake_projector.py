"""An in-memory LSPX-P1 that speaks the real wire protocol, for tests."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable

from bleak.exc import BleakError

from custom_components.sony_lspx_p1.protocol import (
    CHAR_BOND_CONTROL,
    CHAR_BONDED_LIST,
    CHAR_BONDING_NAME,
    CHAR_COMMAND,
    CHAR_SECURITY_PROBE,
    RESULT_ACK,
    RESULT_NAK,
    ErrorCode,
    Op,
    encode_bond_activation,
)


class FakeProjector:
    """State and behaviour of one projector, based on captures from a real unit."""

    def __init__(self, *, bonded: bool = True) -> None:
        self.bonded = bonded
        self.bonding_open = False
        self.controllers: list[str] = ["iPhone"]
        self.power = 0x10
        self.volume = 67
        self.xloud = 1
        self.brightness = 81
        self.auto_brightness = [0, 0]
        self.focus = -370
        self.focus_assist = 1
        self.keystone = 5
        self.direction = 2
        self.auto_direction = 1
        self.picture_mode = 0
        self.picture = {"brightness": 128, "contrast": 32, "saturation": 88, "sharpness": 7}
        self.busy = False  # simulate a stuck main board (TIMEOUT)
        self.log: list[tuple[int, bytes]] = []

    # Replies -----------------------------------------------------------------

    def handle(self, frame: bytes, encrypted: bool) -> bytes:
        op = struct.unpack(">H", frame[:2])[0]
        payload = frame[2:]
        self.log.append((op, payload))

        def ack(data: bytes = b"") -> bytes:
            return bytes([RESULT_ACK]) + frame[:2] + data

        def nak(code: int) -> bytes:
            return bytes([RESULT_NAK]) + frame[:2] + bytes([code])

        if not (self.bonded and encrypted):
            return nak(ErrorCode.ACCESS_DENIED)
        if self.busy:
            return nak(ErrorCode.TIMEOUT)

        u8 = lambda v: bytes([v])  # noqa: E731
        table: dict[int, Callable[[], bytes]] = {
            Op.GET_POWER: lambda: ack(u8(self.power)),
            Op.GET_BATTERY: lambda: ack(b"\x64\x02"),
            Op.GET_VOLUME: lambda: ack(u8(self.volume)),
            Op.GET_XLOUD: lambda: ack(u8(self.xloud)),
            Op.GET_APP_STATE: lambda: ack(b"\x60"),
            Op.GET_BRIGHTNESS: lambda: ack(u8(self.brightness)),
            Op.GET_AUTO_BRIGHTNESS: lambda: ack(bytes(self.auto_brightness)),
            Op.GET_FOCUS: lambda: ack(struct.pack(">h", self.focus)),
            Op.GET_FOCUS_LIMITS: lambda: ack(bytes.fromhex("009500000000fe81")),
            Op.GET_FOCUS_ASSIST: lambda: ack(u8(self.focus_assist)),
            Op.GET_KEYSTONE: lambda: ack(b"\x01" + struct.pack(">b", self.keystone)),
            Op.GET_DIRECTION: lambda: ack(u8(self.direction)),
            Op.GET_AUTO_DIRECTION: lambda: ack(u8(self.auto_direction)),
            Op.GET_PROJECTION_RANGE: lambda: ack(bytes.fromhex("2864e21e")),
            Op.GET_PICTURE_MODE: lambda: ack(u8(self.picture_mode)),
            Op.GET_PICTURE_RANGE: lambda: ack(bytes.fromhex("007864a00a28000f4664")),
            Op.GET_PICTURE_BRIGHTNESS: lambda: ack(u8(self.picture["brightness"])),
            Op.GET_PICTURE_CONTRAST: lambda: ack(u8(self.picture["contrast"])),
            Op.GET_PICTURE_SATURATION: lambda: ack(u8(self.picture["saturation"])),
            Op.GET_PICTURE_SHARPNESS: lambda: ack(u8(self.picture["sharpness"])),
            Op.GET_SOC_SOFTWARE_VERSION: lambda: ack(b"3.1.15" + b"\x00" * 11 + b"\x04"),
        }
        if op in table:
            return table[op]()

        setters: dict[int, Callable[[bytes], None]] = {
            Op.SET_POWER: lambda p: setattr(self, "power", p[0]),
            Op.SET_VOLUME: lambda p: setattr(self, "volume", p[0]),
            Op.SET_XLOUD: lambda p: setattr(self, "xloud", p[0]),
            Op.SET_BRIGHTNESS: lambda p: setattr(self, "brightness", p[0]),
            Op.SET_AUTO_BRIGHTNESS: lambda p: setattr(self, "auto_brightness", list(p[:2])),
            Op.SET_FOCUS: lambda p: setattr(self, "focus", struct.unpack(">h", p[1:3])[0]),
            Op.SET_FOCUS_ASSIST: lambda p: setattr(self, "focus_assist", p[0]),
            Op.SET_KEYSTONE: lambda p: setattr(self, "keystone", struct.unpack(">b", p[1:2])[0]),
            Op.SET_DIRECTION: lambda p: setattr(self, "direction", p[0]),
            Op.SET_AUTO_DIRECTION: lambda p: setattr(self, "auto_direction", p[0]),
            Op.SET_PICTURE_MODE: lambda p: setattr(self, "picture_mode", p[0]),
            Op.SET_PICTURE_BRIGHTNESS: lambda p: self.picture.__setitem__("brightness", p[0]),
            Op.SET_PICTURE_CONTRAST: lambda p: self.picture.__setitem__("contrast", p[0]),
            Op.SET_PICTURE_SATURATION: lambda p: self.picture.__setitem__("saturation", p[0]),
            Op.SET_PICTURE_SHARPNESS: lambda p: self.picture.__setitem__("sharpness", p[0]),
        }
        if op in setters:
            setters[op](payload)
            return ack(payload)
        return nak(ErrorCode.UNKNOWN_COMMAND)


class FakeBleakClient:
    """Implements the subset of BleakClient that LSPXClient uses."""

    def __init__(self, projector: FakeProjector, *, pair_supported: bool = True) -> None:
        self.projector = projector
        self.pair_supported = pair_supported
        self.encrypted = False
        self.connected = True
        self._notify: dict[str, Callable] = {}

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def start_notify(self, char: str, callback: Callable) -> None:
        self._notify[char] = callback

    async def stop_notify(self, char: str) -> None:
        self._notify.pop(char, None)

    async def pair(self) -> bool:
        if not self.pair_supported:
            raise NotImplementedError
        if self.projector.bonding_open:
            self.projector.bonded = True
            self.projector.bonding_open = False
        self.encrypted = self.projector.bonded
        return True

    async def read_gatt_char(self, char: str) -> bytearray:
        if char == CHAR_SECURITY_PROBE:
            if not self.projector.bonded:
                raise BleakError("Insufficient Encryption")
            self.encrypted = True
            return bytearray(b"\x00")
        if char == CHAR_BONDED_LIST:
            out = b"".join(
                i.to_bytes(2, "big") + b"\x00" + n.encode() + b"\n" for i, n in enumerate(self.projector.controllers)
            )
            return bytearray(out)
        raise BleakError(f"unexpected read {char}")

    async def write_gatt_char(self, char: str, data: bytes, response: bool = True) -> None:
        if char == CHAR_COMMAND:
            reply = self.projector.handle(bytes(data), self.encrypted)
            asyncio.get_running_loop().call_soon(self._notify[CHAR_COMMAND], None, bytearray(reply))
        elif char == CHAR_BOND_CONTROL:
            assert bytes(data) == encode_bond_activation()
            self.projector.bonding_open = True
            asyncio.get_running_loop().call_soon(self._notify[CHAR_BOND_CONTROL], None, bytearray(b"\x01\x10"))
        elif char == CHAR_BONDING_NAME:
            self.projector.controllers.append(bytes(data).decode())
        else:
            raise BleakError(f"unexpected write {char}")

    async def disconnect(self) -> bool:
        self.connected = False
        return True
