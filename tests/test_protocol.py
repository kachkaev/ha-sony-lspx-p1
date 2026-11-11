"""Protocol encoding/decoding, using bytes captured from a real LSPX-P1."""

import pytest

from custom_components.sony_lspx_p1 import protocol as p


def test_encode_command_is_big_endian() -> None:
    assert p.encode_command(p.Op.GET_BRIGHTNESS).hex() == "0161"
    assert p.encode_command(p.Op.SET_BRIGHTNESS, p.payload_u8(50)).hex() == "016032"
    assert p.encode_command(p.Op.SET_FOCUS, p.payload_focus(-370)).hex() == "014000fe8e"
    assert p.encode_command(p.Op.SET_KEYSTONE, p.payload_keystone(-5)).hex() == "015001fb"
    assert p.encode_command(p.Op.SET_POWER, p.payload_power(True)).hex() == "080010"


def test_check_reply_ack_and_nak() -> None:
    assert p.check_reply(353, bytes.fromhex("02016151")) == b"\x51"
    with pytest.raises(p.CommandRejected) as err:
        p.check_reply(353, bytes.fromhex("030161a0"))
    assert err.value.code == p.ErrorCode.ACCESS_DENIED
    with pytest.raises(p.ProtocolError):
        p.check_reply(353, bytes.fromhex("02016051"))


def test_decoders_on_captured_replies() -> None:
    assert p.decode_power(b"\x10") is True
    assert p.decode_power(b"\x00") is False
    assert p.decode_focus(bytes.fromhex("fe8e")) == -370
    assert p.decode_keystone(bytes.fromhex("0105")) == 5
    assert p.decode_keystone(bytes.fromhex("01fb")) == -5
    assert p.decode_auto_brightness(b"\x00\x00") is False
    assert p.decode_focus_limits(bytes.fromhex("009500000000fe81")) == p.FocusLimits(near=149, far=-383)
    assert p.decode_projection_range(bytes.fromhex("2864e21e")) == p.ProjectionRange(40, 100, -30, 30)
    pr = p.decode_picture_range(bytes.fromhex("007864a00a28000f4664"))
    assert pr.saturation == (0, 120)
    assert pr.brightness == (100, 160)
    assert pr.contrast == (10, 40)
    assert pr.sharpness == (0, 15)
    assert p.decode_battery(b"\x64\x02") == p.Battery(level=100, charge_state=p.ChargeState.FULL)
    assert p.decode_string(b"3.1.15" + b"\x00" * 10 + b"\x04") == "3.1.15"


def test_bond_activation_payload() -> None:
    assert p.encode_bond_activation() == b"\x10T0hrku"


def test_parse_bonded_list() -> None:
    raw = bytes.fromhex("6531006950686f6e650ab31d006950686f6e650a3cd600486f6d6520417373697374616e742070726f62650a")
    assert p.parse_bonded_list(raw) == [
        (0x6531, 0, "iPhone"),
        (0xB31D, 0, "iPhone"),
        (0x3CD6, 0, "Home Assistant probe"),
    ]
