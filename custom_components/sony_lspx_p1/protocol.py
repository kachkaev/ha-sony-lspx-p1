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
from dataclasses import dataclass, field
from enum import IntEnum

# GATT characteristics
CHAR_COMMAND = "ee002916-5ab8-446b-2a0f-e1c55a6d871f"  # ApplicationCommandControlPoint
CHAR_BOND_CONTROL = "ee002834-5ab8-446b-2a0f-e1c55a6d871f"  # ManageBondingControlPoint
CHAR_BONDING_NAME = "ee002228-5ab8-446b-2a0f-e1c55a6d871f"  # BondingDeviceName
CHAR_BONDED_LIST = "ee0023c8-5ab8-446b-2a0f-e1c55a6d871f"  # BondedDeviceList
CHAR_SECURITY_PROBE = "ee002193-5ab8-446b-2a0f-e1c55a6d871f"  # requires an encrypted link

SERVICE_COMMAND = "ee0015bf-5ab8-446b-2a0f-e1c55a6d871f"

LOCAL_NAME_PREFIX = "LSPXP1"
SONY_MANUFACTURER_ID = 0x012D

# Bonding: ActivateBondingMode (operation 0x10) followed by this key, as the official app does.
BOND_ACTIVATE_OPERATION = 0x10
BOND_ACTIVATE_KEY = b"T0hrku"
BOND_RESULT_SUCCESS = 1

RESULT_ACK = 2
RESULT_NAK = 3


class Op(IntEnum):
    """Command opcodes (get / set pairs)."""

    GET_WIFI_MAC = 1
    GET_SOC_STATUS = 3
    GET_SOC_SOFTWARE_VERSION = 4
    GET_SOC_MODE = 33
    GET_APP_STATE = 50
    GET_STA_STATUS = 101

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

    SET_POWER = 2048
    GET_POWER = 2049
    GET_BATTERY = 2081


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


# --- payload builders -------------------------------------------------------


def payload_u8(value: int) -> bytes:
    return struct.pack(">B", int(value))


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


def decode_bool(data: bytes) -> bool:
    return decode_u8(data) != 0


def decode_power(data: bytes) -> bool:
    return decode_u8(data) == POWER_ON


def decode_focus(data: bytes) -> int:
    # Seen as 2 bytes (sint16); the app also accepts a 3-byte form with a leading type byte.
    if len(data) > 2:
        return struct.unpack(">h", data[1:3])[0]
    _need(data, 2, "focus")
    return struct.unpack(">h", data[:2])[0]


def decode_keystone(data: bytes) -> int:
    _need(data, 2, "keystone")
    return struct.unpack(">b", data[1:2])[0]


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


def decode_picture_range(data: bytes) -> PictureRange:
    _need(data, 8, "picture range")
    return PictureRange(
        saturation=(data[0], data[1]),
        brightness=(data[2], data[3]),
        contrast=(data[4], data[5]),
        sharpness=(data[6], data[7]),
    )


@dataclass(frozen=True)
class Battery:
    level: int
    charge_state: int


def decode_battery(data: bytes) -> Battery:
    _need(data, 2, "battery")
    return Battery(level=data[0], charge_state=data[1])


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
    focus_limits: FocusLimits | None = None
    projection_range: ProjectionRange | None = None
    picture_range: PictureRange | None = None
    software_version: str | None = None
    errors: dict[str, str] = field(default_factory=dict)


# Fields read on every poll: attribute -> (opcode, decoder)
STATE_READS: dict[str, tuple[Op, object]] = {
    "power": (Op.GET_POWER, decode_power),
    "battery": (Op.GET_BATTERY, decode_battery),
    "volume": (Op.GET_VOLUME, decode_u8),
    "xloud": (Op.GET_XLOUD, decode_bool),
    "app_state": (Op.GET_APP_STATE, decode_u8),
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
}

# Static information read once per connection: attribute -> (opcode, decoder)
STATIC_READS: dict[str, tuple[Op, object]] = {
    "focus_limits": (Op.GET_FOCUS_LIMITS, decode_focus_limits),
    "projection_range": (Op.GET_PROJECTION_RANGE, decode_projection_range),
    "picture_range": (Op.GET_PICTURE_RANGE, decode_picture_range),
    "software_version": (Op.GET_SOC_SOFTWARE_VERSION, decode_string),
}
