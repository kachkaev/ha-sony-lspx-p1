"""Wire protocol of the Sony LSPX-P1 projector over Bluetooth LE.

This module has no Bluetooth or Home Assistant dependencies so it can be unit-tested
and reused. See docs/protocol.md in the repository for the full description.

Commands are written to the ApplicationCommandControlPoint characteristic as a
big-endian uint16 opcode followed by an optional payload. The projector answers with
a notification on the same characteristic: ``[result, opcode_hi, opcode_lo, data...]``
where result is 2 (ACK) or 3 (NAK, first data byte is the error code).
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag

# GATT characteristics
CHAR_COMMAND = "ee002916-5ab8-446b-2a0f-e1c55a6d871f"  # ApplicationCommandControlPoint
CHAR_BOND_CONTROL = "ee002834-5ab8-446b-2a0f-e1c55a6d871f"  # ManageBondingControlPoint
CHAR_BONDING_NAME = "ee002228-5ab8-446b-2a0f-e1c55a6d871f"  # BondingDeviceName
CHAR_BONDED_LIST = "ee0023c8-5ab8-446b-2a0f-e1c55a6d871f"  # BondedDeviceList
CHAR_SECURITY_PROBE = "ee002193-5ab8-446b-2a0f-e1c55a6d871f"  # requires an encrypted link

LOCAL_NAME_PREFIX = "LSPXP1"
SONY_MANUFACTURER_ID = 0x012D
# Advertised manufacturer data starts with the vendor UUID base (ee00 5ab8-446b-2a0f-e1c55a6d871f).
SONY_MANUFACTURER_DATA_PREFIX = bytes.fromhex("ee005ab8446b2a0fe1c55a6d871f")
# 16-bit service UUID 0x00EE in the advertisement.
SERVICE_UUID_ADVERTISED = "000000ee-0000-1000-8000-00805f9b34fb"

# Bonding: ActivateBondingMode (operation 0x10) followed by this key, as the official app does.
BOND_ACTIVATE_OPERATION = 0x10
BOND_ACTIVATE_KEY = b"T0hrku"
BOND_RESULT_SUCCESS = 1


class BondOp(IntEnum):
    """Operations on ManageBondingControlPoint (ManageBondOperationList in the official app).

    Write ``[op][args...]``; the projector notifies ``[result][op][data...]`` with result 1 on success.
    """

    DELETE_CONTROLLER = 0x01  # [id:u16]
    ALLOW_CONTROLLER = 0x02  # [id:u16]
    BLOCK_CONTROLLER = 0x03  # [id:u16]
    GET_MY_ID = 0x06
    GET_CONTROLLER_COUNT = 0x07
    ACTIVATE_BONDING_MODE = 0x10  # + BOND_ACTIVATE_KEY
    # "Restrict adding smartphones" in the app: a new controller must enter a PIN shown on screen.
    GET_PIN_CODE_FLAG = 0x13
    SET_PIN_CODE_FLAG = 0x14  # [flag:u8]


RESULT_ACK = 2
RESULT_NAK = 3


class Op(IntEnum):
    """Command opcodes (get / set pairs)."""

    GET_WIFI_MAC = 1
    GET_SOC_STATUS = 3
    GET_SOC_SOFTWARE_VERSION = 4
    GET_HDMI_TX_VERSION = 5  # the wireless HDMI unit's firmware
    GET_SOC_MODE = 33
    SET_NET_TOPOLOGY = 48
    GET_NET_TOPOLOGY = 49
    GET_APP_STATE = 50
    STOP_APP = 51  # no payload: closes whatever the projector is showing
    GET_STA_STATUS = 101
    GET_HDMI_STATUS = 112  # [enabled][HdmiStatus]
    SET_POSTER = 102  # [theme:u16]
    GET_POSTER = 103

    SET_FOCUS_ASSIST = 256
    GET_FOCUS_ASSIST = 257
    SET_AUTO_BRIGHTNESS = 288
    GET_AUTO_BRIGHTNESS = 289
    SET_AUTO_DIRECTION = 304
    GET_AUTO_DIRECTION = 305
    SET_FOCUS = 320
    GET_FOCUS = 321
    GET_FOCUS_LIMITS = 322
    SET_KEYSTONE = 336
    GET_KEYSTONE = 337
    SET_BRIGHTNESS = 352
    GET_BRIGHTNESS = 353
    SET_DIRECTION = 368
    GET_DIRECTION = 369
    GET_PROJECTION_RANGE = 385

    SET_VOLUME = 512
    GET_VOLUME = 513
    SET_XLOUD = 528
    GET_XLOUD = 529

    SET_PICTURE_MODE = 1280
    GET_PICTURE_MODE = 1281
    GET_PICTURE_RANGE = 1297
    SET_PICTURE_SATURATION = 1312
    GET_PICTURE_SATURATION = 1313
    SET_PICTURE_BRIGHTNESS = 1328
    GET_PICTURE_BRIGHTNESS = 1329
    SET_PICTURE_CONTRAST = 1344
    GET_PICTURE_CONTRAST = 1345
    SET_PICTURE_SHARPNESS = 1360
    GET_PICTURE_SHARPNESS = 1361
    SET_PICTURE_RGB = 1376  # white balance [red, green, blue]
    GET_PICTURE_RGB = 1377
    RESET_CUSTOM_PICTURE = 1393  # payload: the custom picture mode to reset

    SET_POWER = 2048
    GET_POWER = 2049
    SET_AUTO_STANDBY = 2064  # [phone nearby][inactivity][timer], each an AutoStandby value
    GET_AUTO_STANDBY = 2065
    GET_BATTERY = 2081
    GET_SYSCON_VERSION = 2097  # system controller firmware
    GET_LIGHT_ENGINE_VERSION = 2113  # laser light engine firmware ("LD" in the app)
    GET_USAGE_TIME = 2116  # [hours:u16][minutes][seconds] the projector has been on

    REBOOT = 3840  # no payload; the projector restarts, possibly before it answers


class ErrorCode(IntEnum):
    """Error codes carried in NAK replies (BleLowLayerError in the official app)."""

    CHECKSUM = 0x01
    INVALID_ARGUMENT = 0x02
    UNKNOWN_COMMAND = 0x03
    ACCESS_DENIED = 0xA0  # the link is not bonded/encrypted
    TIMEOUT = 0xC0  # the projector's main board did not answer the Bluetooth chip
    MODULE_ERROR = 0xD0
    RUNTIME_ERROR = 0xFF


POWER_ON = 0x10
POWER_STANDBY = 0x00


class Direction(IntEnum):
    """Projection direction (ProjectionDirection in the official app)."""

    MIRROR_HORIZONTAL = 0
    ROTATE_180 = 1
    NORMAL = 2
    MIRROR_VERTICAL = 3


class PictureMode(IntEnum):
    """Picture presets. Picture parameters can only be tuned in the custom modes."""

    PRESET_1 = 0
    PRESET_2 = 1
    PRESET_3 = 2
    CUSTOM_1 = 16
    CUSTOM_2 = 17


class ChargeState(IntEnum):
    """Battery charge state."""

    ON_BATTERY = 0
    CHARGING = 1
    FULL = 2


class AppState(IntEnum):
    """What the projector is currently showing (SocAppState in the official app)."""

    UNKNOWN = 0
    POSTER = 16
    CONTENT_VIEWER = 32
    MIRRORING = 48
    DLNA = 64
    YOUTUBE = 80
    HDMI = 96


class HdmiStatus(IntEnum):
    """The wireless HDMI unit's link (WirelessHdmiStatus.HdmiStatus in the official app)."""

    DISCONNECTED = 0
    UNPLUGGED = 1  # the unit is connected but no HDMI source is plugged in
    NO_SIGNAL = 2
    CONNECTING = 3
    CONNECTED = 4
    CHANGING_RESOLUTION = 5
    CHANGING_SOURCE = 6
    UPDATING = 16


class AutoStandby(IntFlag):
    """One trigger of the app's "Auto on/Networked Standby" settings (AutoSleep.Status in the app)."""

    OFF = 1  # switch to Networked Standby
    ON = 2  # switch on


# Byte positions in GET/SET_AUTO_STANDBY.
AUTO_STANDBY_PHONE = 0  # when the smartphone moves away / comes close
AUTO_STANDBY_INACTIVITY = 1  # after a while in the dark without interaction
AUTO_STANDBY_TIMER = 2  # at a set time


class NetTopology(IntEnum):
    """How the projector's Wi-Fi is used (NetworkTopology in the official app)."""

    HOME = 16
    HOME_POSTER = 17
    HOME_CONTENT_VIEWER = 18
    GUEST = 32
    GUEST_POSTER = 33
    GUEST_CONTENT_VIEWER = 34
    WIRELESS_HDMI = 48  # talking to the wireless HDMI unit


class Poster(IntEnum):
    """Built-in poster themes (PosterTheme.V10 in the official app)."""

    A_DAY = 3
    JOURNEY = 4
    NIGHT_DREAMS = 5
    WINDOW = 6
    ANALOG_CLOCK_1 = 7
    ANALOG_CLOCK_2 = 8
    DIGITAL_CLOCK_1 = 9
    DIGITAL_CLOCK_2 = 10  # the factory default
    WEATHER = 11
    MY_SLIDESHOW_1 = 13
    MY_SLIDESHOW_2 = 14


# The posters the app offers under "Change poster", with its names and in its order.
# The app's "Clock 1" is DIGITAL_CLOCK_2 and "Clock 2" is DIGITAL_CLOCK_1.
POSTER_NAMES: dict[Poster, str] = {
    Poster.WEATHER: "Weather",
    Poster.DIGITAL_CLOCK_2: "Clock 1",
    Poster.DIGITAL_CLOCK_1: "Clock 2",
    Poster.ANALOG_CLOCK_1: "Clock 3",
    Poster.ANALOG_CLOCK_2: "Clock 4",
    Poster.A_DAY: "A day",
    Poster.JOURNEY: "Journey",
    Poster.NIGHT_DREAMS: "Night dreams",
    Poster.WINDOW: "Window",
}


class BondCommandFailed(Exception):
    """The projector answered a bonding-control operation with a failure."""

    def __init__(self, op: int, result: int) -> None:
        self.op = op
        self.result = result
        super().__init__(f"bonding operation 0x{op:02x} failed with result {result}")


class ProtocolError(Exception):
    """A reply could not be parsed or did not match the request."""


class CommandRejected(Exception):
    """The projector answered with NAK."""

    def __init__(self, op: int, code: int) -> None:
        self.op = op
        self.code = code
        try:
            name = ErrorCode(code).name
        except ValueError:
            name = "UNKNOWN"
        super().__init__(f"command {op} rejected with error 0x{code:02x} ({name})")


def encode_command(op: int, payload: bytes = b"") -> bytes:
    """Build the bytes written to the command characteristic."""
    return struct.pack(">H", op) + payload


def decode_reply(frame: bytes) -> tuple[int, int, bytes]:
    """Split a notification into (result, opcode, data)."""
    if len(frame) < 3:
        raise ProtocolError(f"reply too short: {frame.hex()}")
    return frame[0], struct.unpack(">H", frame[1:3])[0], bytes(frame[3:])


def check_reply(op: int, frame: bytes) -> bytes:
    """Validate a reply for ``op`` and return its data, raising on NAK."""
    result, echoed, data = decode_reply(frame)
    if echoed != op:
        raise ProtocolError(f"reply for opcode {echoed}, expected {op}")
    if result == RESULT_NAK:
        raise CommandRejected(op, data[0] if data else 0)
    if result != RESULT_ACK:
        raise ProtocolError(f"unexpected result {result} for opcode {op}")
    return data


def encode_bond_activation() -> bytes:
    """Payload for ManageBondingControlPoint that opens the bonding window."""
    return bytes([BOND_ACTIVATE_OPERATION]) + BOND_ACTIVATE_KEY


def encode_bond_op(op: int, payload: bytes = b"") -> bytes:
    """Bytes written to ManageBondingControlPoint."""
    return bytes([op]) + payload


def check_bond_reply(op: int, frame: bytes) -> bytes:
    """Validate a ManageBondingControlPoint notification for ``op`` and return its data."""
    if len(frame) < 2:
        raise ProtocolError(f"bonding reply too short: {frame.hex()}")
    if frame[1] != op:
        raise ProtocolError(f"bonding reply for operation {frame[1]}, expected {op}")
    if frame[0] != BOND_RESULT_SUCCESS:
        raise BondCommandFailed(op, frame[0])
    return bytes(frame[2:])


# --- payload builders -------------------------------------------------------


def payload_u8(value: int) -> bytes:
    return struct.pack(">B", int(value))


def payload_u16(value: int) -> bytes:
    return struct.pack(">H", int(value))


def payload_bool(value: bool) -> bytes:
    return payload_u8(1 if value else 0)


def payload_power(on: bool) -> bytes:
    return payload_u8(POWER_ON if on else POWER_STANDBY)


def payload_focus(value: int) -> bytes:
    return b"\x00" + struct.pack(">h", int(value))


def payload_keystone(value: int) -> bytes:
    return b"\x01" + struct.pack(">b", int(value))


def payload_auto_brightness(enabled: bool) -> bytes:
    # [caused by AC adapter, caused by illumination sensor]; the app toggles both together.
    flag = 1 if enabled else 0
    return bytes([flag, flag])


# --- decoders --------------------------------------------------------------


def _need(data: bytes, n: int, what: str) -> None:
    if len(data) < n:
        raise ProtocolError(f"{what}: expected {n} bytes, got {data.hex()}")


def decode_u8(data: bytes) -> int:
    _need(data, 1, "u8")
    return data[0]


def decode_u16(data: bytes) -> int:
    _need(data, 2, "u16")
    value: int = struct.unpack(">H", data[:2])[0]
    return value


def decode_hdmi_status(data: bytes) -> tuple[bool, int]:
    """[enabled][status]: whether wireless HDMI is set up, and its HdmiStatus."""
    _need(data, 2, "HDMI status")
    return data[0] != 0, data[1]


def decode_usage_time(data: bytes) -> float:
    """[hours:u16][minutes][seconds] as hours."""
    _need(data, 4, "usage time")
    hours: int
    minutes: int
    seconds: int
    hours, minutes, seconds = struct.unpack(">HBB", data[:4])
    return round(hours + minutes / 60 + seconds / 3600, 2)


def decode_auto_standby(data: bytes) -> tuple[int, int, int]:
    _need(data, 3, "auto standby")
    return data[0], data[1], data[2]


def decode_bool(data: bytes) -> bool:
    return decode_u8(data) != 0


def decode_power(data: bytes) -> bool:
    return decode_u8(data) == POWER_ON


def decode_focus(data: bytes) -> int:
    # Seen as 2 bytes (sint16); the app also accepts a 3-byte form with a leading type byte.
    value: int
    if len(data) > 2:
        value = struct.unpack(">h", data[1:3])[0]
        return value
    _need(data, 2, "focus")
    value = struct.unpack(">h", data[:2])[0]
    return value


def decode_keystone(data: bytes) -> int:
    _need(data, 2, "keystone")
    value: int = struct.unpack(">b", data[1:2])[0]
    return value


def decode_auto_brightness(data: bytes) -> bool:
    _need(data, 2, "auto brightness")
    return data[0] != 0 or data[1] != 0


def decode_string(data: bytes) -> str:
    return data.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()


@dataclass(frozen=True)
class FocusLimits:
    near: int
    far: int


def decode_focus_limits(data: bytes) -> FocusLimits:
    _need(data, 8, "focus limits")
    near, _inch25, _origin, far = struct.unpack(">hhhh", data[:8])
    return FocusLimits(near=near, far=far)


@dataclass(frozen=True)
class ProjectionRange:
    brightness_min: int
    brightness_max: int
    keystone_min: int
    keystone_max: int


def decode_projection_range(data: bytes) -> ProjectionRange:
    _need(data, 4, "projection range")
    bmin, bmax, kmin, kmax = struct.unpack(">BBbb", data[:4])
    return ProjectionRange(bmin, bmax, kmin, kmax)


@dataclass(frozen=True)
class PictureRange:
    saturation: tuple[int, int]
    brightness: tuple[int, int]
    contrast: tuple[int, int]
    sharpness: tuple[int, int]
    rgb: tuple[int, int] | None = None


def decode_picture_range(data: bytes) -> PictureRange:
    _need(data, 8, "picture range")
    return PictureRange(
        saturation=(data[0], data[1]),
        brightness=(data[2], data[3]),
        contrast=(data[4], data[5]),
        sharpness=(data[6], data[7]),
        rgb=(data[8], data[9]) if len(data) >= 10 else None,
    )


def decode_rgb(data: bytes) -> tuple[int, int, int]:
    _need(data, 3, "white balance")
    return data[0], data[1], data[2]


@dataclass(frozen=True)
class Battery:
    level: int
    charge_state: int


def decode_battery(data: bytes) -> Battery:
    _need(data, 2, "battery")
    return Battery(level=data[0], charge_state=data[1])


@dataclass(frozen=True)
class Controller:
    """A smartphone (or Home Assistant) registered with the projector."""

    id: int
    name: str
    allowed: bool


def decode_controllers(data: bytes) -> list[Controller]:
    """BondedDeviceList as Controller records (flag 0 means allowed)."""
    return [Controller(id=i, name=name, allowed=flag == 0) for i, flag, name in parse_bonded_list(data)]


def parse_bonded_list(data: bytes) -> list[tuple[int, int, str]]:
    """Parse BondedDeviceList: repeated ``[id:u16][flag:u8][name...]\\n``."""
    out: list[tuple[int, int, str]] = []
    for chunk in data.split(b"\n"):
        if len(chunk) < 3:
            continue
        dev_id = struct.unpack(">H", chunk[:2])[0]
        out.append((dev_id, chunk[2], chunk[3:].decode("utf-8", errors="replace")))
    return out


@dataclass
class ProjectorState:
    """Snapshot of everything the integration reads from the projector."""

    power: bool | None = None
    volume: int | None = None
    xloud: bool | None = None
    battery: Battery | None = None
    app_state: int | None = None
    poster: int | None = None
    net_topology: int | None = None
    brightness: int | None = None
    auto_brightness: bool | None = None
    focus: int | None = None
    focus_assist: bool | None = None
    keystone: int | None = None
    direction: int | None = None
    auto_direction: bool | None = None
    picture_mode: int | None = None
    picture_brightness: int | None = None
    picture_contrast: int | None = None
    picture_saturation: int | None = None
    picture_sharpness: int | None = None
    picture_rgb: tuple[int, int, int] | None = None
    focus_limits: FocusLimits | None = None
    projection_range: ProjectionRange | None = None
    picture_range: PictureRange | None = None
    hdmi_status: tuple[bool, int] | None = None
    usage_hours: float | None = None
    auto_standby: tuple[int, int, int] | None = None
    software_version: str | None = None
    syscon_version: str | None = None
    light_engine_version: str | None = None
    hdmi_tx_version: str | None = None
    # Read through the bonding service, not the command characteristic.
    restrict_new_controllers: bool | None = None
    controllers: list[Controller] | None = None
    errors: dict[str, str] = field(default_factory=dict)


# Fields read on every poll: attribute -> (opcode, decoder)
STATE_READS: dict[str, tuple[Op, Callable[[bytes], object]]] = {
    "power": (Op.GET_POWER, decode_power),
    "battery": (Op.GET_BATTERY, decode_battery),
    "volume": (Op.GET_VOLUME, decode_u8),
    "xloud": (Op.GET_XLOUD, decode_bool),
    "app_state": (Op.GET_APP_STATE, decode_u8),
    "poster": (Op.GET_POSTER, decode_u16),
    "net_topology": (Op.GET_NET_TOPOLOGY, decode_u8),
    "hdmi_status": (Op.GET_HDMI_STATUS, decode_hdmi_status),
    "usage_hours": (Op.GET_USAGE_TIME, decode_usage_time),
    "auto_standby": (Op.GET_AUTO_STANDBY, decode_auto_standby),
    "brightness": (Op.GET_BRIGHTNESS, decode_u8),
    "auto_brightness": (Op.GET_AUTO_BRIGHTNESS, decode_auto_brightness),
    "focus": (Op.GET_FOCUS, decode_focus),
    "focus_assist": (Op.GET_FOCUS_ASSIST, decode_bool),
    "keystone": (Op.GET_KEYSTONE, decode_keystone),
    "direction": (Op.GET_DIRECTION, decode_u8),
    "auto_direction": (Op.GET_AUTO_DIRECTION, decode_bool),
    "picture_mode": (Op.GET_PICTURE_MODE, decode_u8),
    "picture_brightness": (Op.GET_PICTURE_BRIGHTNESS, decode_u8),
    "picture_contrast": (Op.GET_PICTURE_CONTRAST, decode_u8),
    "picture_saturation": (Op.GET_PICTURE_SATURATION, decode_u8),
    "picture_sharpness": (Op.GET_PICTURE_SHARPNESS, decode_u8),
    "picture_rgb": (Op.GET_PICTURE_RGB, decode_rgb),
}

# Static information, read on the first successful poll and then reused: attribute -> (opcode, decoder)
STATIC_READS: dict[str, tuple[Op, Callable[[bytes], object]]] = {
    "focus_limits": (Op.GET_FOCUS_LIMITS, decode_focus_limits),
    "projection_range": (Op.GET_PROJECTION_RANGE, decode_projection_range),
    "picture_range": (Op.GET_PICTURE_RANGE, decode_picture_range),
    "software_version": (Op.GET_SOC_SOFTWARE_VERSION, decode_string),
    "syscon_version": (Op.GET_SYSCON_VERSION, decode_string),
    "light_engine_version": (Op.GET_LIGHT_ENGINE_VERSION, decode_string),
    "hdmi_tx_version": (Op.GET_HDMI_TX_VERSION, decode_string),
}
