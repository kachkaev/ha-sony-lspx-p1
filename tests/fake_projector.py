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
    BondOp,
    ErrorCode,
    Op,
    encode_bond_activation,
)


class FakeProjector:
    """State and behaviour of one projector, based on captures from a real unit."""

    def __init__(self, *, bonded: bool = True) -> None:
        self.bonded = bonded
        self.bonding_open = False
        self.controllers: dict[int, str] = {0: "iPhone"}  # "Smartphones added": id -> name
        self.blocked: set[int] = set()  # ids of controllers switched off in "Smartphones added"
        self.my_id: int | None = None  # the id of the controller that is connected (GetBondedCurConDevID)
        self.failing_bond_ops: set[int] = set()  # bonding operations that always fail
        self.restrict = 0  # "Restrict adding smartphones"
        self.bond_failures = 0  # answer this many bonding operations (other than activation) with a failure
        self.bond_ops: list[int] = []
        self.bond_writes: list[bytes] = []  # every write to ManageBondingControlPoint other than activation
        self.power = 0x10
        self.app_state = 0x60  # HDMI
        self.topology = 0x30  # wireless HDMI
        self.poster = 10  # the factory default, Clock 1
        self.weather = 0  # the Weather poster's code (SetWeather/GetWeather; what a real one holds is unknown)
        self.hdmi = [1, 4]  # wireless HDMI set up, connected
        # GetHttpEndpoint: (port, address). What a real one reports off Wi-Fi is unknown; 0.0.0.0 is what the app
        # stores while it has no address yet.
        self.endpoint: tuple[int, str] = (80, "0.0.0.0")
        self.usage = (412, 30, 0)  # hours, minutes, seconds
        self.auto_standby = [3, 1, 0]  # phone nearby: off and on; inactivity: off; timer: none
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
        self.rgb = [100, 100, 100]
        self.resets: list[int] = []
        self.busy = False  # simulate a stuck main board (TIMEOUT)
        # Host-side state shared by every connection:
        self.host_bond_stale = False  # the host keeps a bond the projector forgot, so pair() is a no-op
        self.stale_service_cache = False  # cached service list lacks the bonding characteristics
        self.pairing_via_read = True  # reading an encrypted characteristic triggers pairing (iOS, macOS)
        self.pairing_via_call = True  # an explicit pair() call is accepted (Android, BlueZ)
        self.notify_failures = 0  # fail this many start_notify calls, as BlueZ does right after a cached connect
        self.transition_replies = 0  # answer this many commands with UNKNOWN_COMMAND, as while switching power
        self.reply_delays: dict[int, float] = {}  # opcode -> seconds before the reply, as while switching source
        self.reboot_drops_link = True  # Reboot drops the link instead of answering, which the app allows for
        self.rejections: dict[int, int] = {}  # opcode -> error code answered instead (NAK)
        self.unsolicited: list[bytes] = []  # frames notified once before the next reply (late replies and such)
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
        if self.busy or op in self.rejections:
            return nak(ErrorCode.TIMEOUT if self.busy else self.rejections[op])
        if self.transition_replies:
            self.transition_replies -= 1
            return nak(ErrorCode.UNKNOWN_COMMAND)

        u8 = lambda v: bytes([v])  # noqa: E731
        table: dict[int, Callable[[], bytes]] = {
            Op.GET_POWER: lambda: ack(u8(self.power)),
            Op.GET_BATTERY: lambda: ack(b"\x64\x02"),
            Op.GET_VOLUME: lambda: ack(u8(self.volume)),
            Op.GET_XLOUD: lambda: ack(u8(self.xloud)),
            Op.GET_APP_STATE: lambda: ack(u8(self.app_state)),
            Op.GET_NET_TOPOLOGY: lambda: ack(u8(self.topology)),
            Op.GET_POSTER: lambda: ack(struct.pack(">H", self.poster)),
            Op.GET_WEATHER: lambda: ack(struct.pack(">H", self.weather)),
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
            Op.GET_PICTURE_RGB: lambda: ack(bytes(self.rgb)),
            Op.GET_SOC_SOFTWARE_VERSION: lambda: ack(b"3.1.15" + b"\x00" * 11 + b"\x04"),
            Op.GET_SYSCON_VERSION: lambda: ack(b"1.0.4\x00\x00\x00"),
            Op.GET_LIGHT_ENGINE_VERSION: lambda: ack(b"2.01\x00\x00\x00\x00"),
            Op.GET_HDMI_TX_VERSION: lambda: ack(b"1.2.0\x00\x00\x00"),
            Op.GET_HDMI_STATUS: lambda: ack(bytes(self.hdmi)),
            Op.GET_USAGE_TIME: lambda: ack(struct.pack(">HBB", *self.usage)),
            Op.GET_AUTO_STANDBY: lambda: ack(bytes(self.auto_standby)),
            Op.GET_HTTP_ENDPOINT: lambda: ack(struct.pack(">H", self.endpoint[0]) + self.endpoint[1].encode()),
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
            Op.SET_PICTURE_RGB: lambda p: setattr(self, "rgb", list(p[:3])),
            Op.RESET_CUSTOM_PICTURE: self._reset_picture,
            Op.SET_NET_TOPOLOGY: self._set_topology,
            Op.STOP_APP: lambda p: setattr(self, "app_state", 0),
            Op.SET_POSTER: self._set_poster,
            Op.SET_WEATHER: lambda p: setattr(self, "weather", struct.unpack(">H", p[:2])[0]),
            Op.SET_AUTO_STANDBY: lambda p: setattr(self, "auto_standby", list(p[:3])),
            Op.REBOOT: lambda p: None,
        }
        if op in setters:
            setters[op](payload)
            return ack(payload)
        return nak(ErrorCode.UNKNOWN_COMMAND)

    def _set_topology(self, payload: bytes) -> None:
        self.topology = payload[0]
        if self.topology == 0x30:
            self.app_state = 0x60

    def _set_poster(self, payload: bytes) -> None:
        self.poster = struct.unpack(">H", payload[:2])[0]
        self.app_state = 0x10

    def handle_bond(self, data: bytes, encrypted: bool) -> bytes:
        """Reply to a write on ManageBondingControlPoint: [result][op][data...]."""
        if data == encode_bond_activation():
            self.bonding_open = True
            return b"\x01\x10"
        op = data[0]
        self.bond_ops.append(op)
        self.bond_writes.append(data)
        if not (self.bonded and encrypted) or self.bond_failures or op in self.failing_bond_ops:
            self.bond_failures = max(0, self.bond_failures - 1)
            return bytes([2, op])
        return bytes(self._bond_reply(op, data[1:]))

    def _bond_reply(self, op: int, args: bytes) -> tuple[int, ...]:
        """[result][op][data...] for an operation the projector accepts from this controller."""
        if op == BondOp.GET_PIN_CODE_FLAG:
            return (1, op, self.restrict)
        if op == BondOp.SET_PIN_CODE_FLAG and len(args) == 1:
            self.restrict = args[0]
            return (1, op)
        if op == BondOp.GET_MY_ID:
            return (2, op) if self.my_id is None else (1, op, *struct.pack(">H", self.my_id))
        if op in (BondOp.DELETE_CONTROLLER, BondOp.ALLOW_CONTROLLER, BondOp.BLOCK_CONTROLLER) and len(args) == 2:
            return (self._change_controller(op, struct.unpack(">H", args)[0]), op)
        return (4, op)  # invalid parameter

    def _change_controller(self, op: int, controller_id: int) -> int:
        """Delete, allow or block one entry; returns the result code."""
        if controller_id not in self.controllers:
            return 2
        if op == BondOp.DELETE_CONTROLLER:
            del self.controllers[controller_id]
        if op == BondOp.BLOCK_CONTROLLER:
            self.blocked.add(controller_id)
        else:
            self.blocked.discard(controller_id)
        return 1

    def _reset_picture(self, payload: bytes) -> None:
        self.resets.append(payload[0])
        self.picture = {"brightness": 128, "contrast": 32, "saturation": 88, "sharpness": 7}
        self.rgb = [100, 100, 100]


class FakeBleakClient:
    """Implements the subset of BleakClient that LSPXClient uses."""

    def __init__(
        self,
        projector: FakeProjector,
        *,
        pair_supported: bool = True,
        disconnected_callback: Callable | None = None,
        use_services_cache: bool = True,
    ) -> None:
        self.projector = projector
        self.services = _FakeServices(hide_bonding=projector.stale_service_cache and use_services_cache)
        self.pair_supported = pair_supported
        self.encrypted = False
        self.connected = True
        self.silent = False  # swallow command replies, e.g. to simulate a link that dies mid-command
        self.pair_calls = 0
        self.unpair_calls = 0
        self.probe_reads = 0
        self.probe_fails = False  # the security probe errors although the link works
        self._notify: dict[str, Callable] = {}
        self._disconnected_callback = disconnected_callback

    def drop(self) -> None:
        """Simulate the link going away."""
        self.connected = False
        self.encrypted = False
        if self._disconnected_callback:
            self._disconnected_callback(self)

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def start_notify(self, char: str, callback: Callable) -> None:
        if self.projector.notify_failures:
            self.projector.notify_failures -= 1
            raise BleakError('[org.freedesktop.DBus.Error.UnknownObject] Method "StartNotify" doesn\'t exist')
        self._notify[char] = callback

    async def stop_notify(self, char: str) -> None:
        self._notify.pop(char, None)

    async def pair(self) -> bool:
        self.pair_calls += 1
        if not self.pair_supported:
            raise NotImplementedError
        if self.projector.host_bond_stale:
            return True  # BlueZ: already paired, nothing to do
        if self.projector.bonding_open:
            if not self.projector.pairing_via_call:
                self.drop()  # the projector disconnects after a failed pairing
                raise BleakError("[org.bluez.Error.AuthenticationFailed] Authentication Failed")
            self.projector.bonded = True
            self.projector.bonding_open = False
            self.drop()  # the real projector disconnects right after a new bond
            return True
        self.encrypted = self.projector.bonded
        return True

    async def unpair(self) -> bool:
        self.unpair_calls += 1
        if not self.pair_supported:
            raise NotImplementedError
        self.projector.host_bond_stale = False
        self.encrypted = False
        return True

    async def read_gatt_char(self, char: str) -> bytearray:
        if char == CHAR_SECURITY_PROBE:
            self.probe_reads += 1
            if not self.connected:
                raise BleakError("[org.freedesktop.DBus.Error.NoReply] Remote peer disconnected")
            if self.probe_fails:
                raise BleakError("probe not readable on this backend")
            if self.projector.host_bond_stale:
                raise BleakError("Authentication Failed")  # the host tries a key the projector forgot
            if not self.projector.bonded and self.projector.bonding_open and self.projector.pairing_via_read:
                self.projector.bonded = True  # the OS paired after the encryption error
                self.projector.bonding_open = False
            if not self.projector.bonded:
                raise BleakError("Insufficient Encryption")
            self.encrypted = True
            return bytearray(b"\x00")
        if char == CHAR_BONDED_LIST:
            out = b"".join(
                i.to_bytes(2, "big") + bytes([1 if i in self.projector.blocked else 0]) + n.encode() + b"\n"
                for i, n in self.projector.controllers.items()
            )
            return bytearray(out)
        raise BleakError(f"unexpected read {char}")

    async def write_gatt_char(self, char: str, data: bytes, response: bool = True) -> None:
        if char == CHAR_COMMAND:
            reply = self.projector.handle(bytes(data), self.encrypted)
            if self.silent:
                return
            for frame in self.projector.unsolicited:
                asyncio.get_running_loop().call_soon(self._notify[CHAR_COMMAND], None, bytearray(frame))
            self.projector.unsolicited = []
            op = int.from_bytes(bytes(data[:2]), "big")
            if op == Op.REBOOT and reply[0] == RESULT_ACK and self.projector.reboot_drops_link:
                asyncio.get_running_loop().call_soon(self.drop)
                return
            delay = self.projector.reply_delays.get(op, 0)
            asyncio.get_running_loop().call_later(delay, self._notify[CHAR_COMMAND], None, bytearray(reply))
        elif char == CHAR_BOND_CONTROL:
            reply = self.projector.handle_bond(bytes(data), self.encrypted)
            asyncio.get_running_loop().call_soon(self._notify[CHAR_BOND_CONTROL], None, bytearray(reply))
        elif char == CHAR_BONDING_NAME:
            # The real projector picks a random-looking id; the last one written is the one that pairs.
            new_id = max(self.projector.controllers, default=0) + 0x1001
            self.projector.controllers[new_id] = bytes(data).decode()
            self.projector.my_id = new_id
        else:
            raise BleakError(f"unexpected write {char}")

    async def clear_cache(self) -> bool:
        self.projector.stale_service_cache = False
        return True

    async def disconnect(self) -> bool:
        self.connected = False
        return True


class _FakeServices:
    def __init__(self, *, hide_bonding: bool) -> None:
        self._hidden = {CHAR_BOND_CONTROL, CHAR_BONDING_NAME} if hide_bonding else set()

    def get_characteristic(self, uuid: str) -> object | None:
        return None if uuid in self._hidden else object()
