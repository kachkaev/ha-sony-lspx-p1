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
    assert pr.rgb == (70, 100)
    assert p.decode_rgb(bytes([100, 90, 80])) == (100, 90, 80)
    assert p.decode_battery(b"\x64\x02") == p.Battery(level=100, charge_state=p.ChargeState.FULL)
    assert p.decode_string(b"3.1.15" + b"\x00" * 10 + b"\x04") == "3.1.15"
    assert p.decode_u16(b"\x00\x0a") == 10
    assert p.payload_u16(p.Poster.WEATHER) == b"\x00\x0b"


def test_bond_activation_payload() -> None:
    assert p.encode_bond_activation() == b"\x10T0hrku"


def test_parse_bonded_list() -> None:
    raw = bytes.fromhex("6531006950686f6e650ab31d006950686f6e650a3cd600486f6d6520417373697374616e742070726f62650a")
    assert p.parse_bonded_list(raw) == [
        (0x6531, 0, "iPhone"),
        (0xB31D, 0, "iPhone"),
        (0x3CD6, 0, "Home Assistant probe"),
    ]


def test_bond_operations() -> None:
    assert p.encode_bond_op(p.BondOp.GET_PIN_CODE_FLAG) == b"\x13"
    assert p.encode_bond_op(p.BondOp.SET_PIN_CODE_FLAG, b"\x01") == b"\x14\x01"
    assert p.check_bond_reply(0x13, b"\x01\x13\x01") == b"\x01"
    assert p.check_bond_reply(0x14, b"\x01\x14") == b""
    with pytest.raises(p.BondCommandFailed):
        p.check_bond_reply(0x13, b"\x02\x13")
    with pytest.raises(p.ProtocolError):
        p.check_bond_reply(0x13, b"\x01\x10")  # a reply to another operation


def test_decode_controllers() -> None:
    raw = bytes.fromhex("6531006950686f6e650ab31d01486f6d6520417373697374616e740a")
    assert p.decode_controllers(raw) == [
        p.Controller(id=0x6531, name="iPhone", allowed=True),
        p.Controller(id=0xB31D, name="Home Assistant", allowed=False),
    ]


def test_malformed_replies_are_protocol_errors() -> None:
    with pytest.raises(p.ProtocolError, match="too short"):
        p.check_reply(353, b"\x02\x01")
    with pytest.raises(p.ProtocolError, match="unexpected result"):
        p.check_reply(353, bytes.fromhex("05016151"))
    with pytest.raises(p.ProtocolError, match="too short"):
        p.check_bond_reply(0x13, b"\x01")
    with pytest.raises(p.ProtocolError, match="battery"):
        p.decode_battery(b"\x64")


def test_rejection_names_the_error_code() -> None:
    assert "TIMEOUT" in str(p.CommandRejected(353, 0xC0))
    assert "UNKNOWN" in str(p.CommandRejected(353, 0x42))


def test_focus_with_a_leading_type_byte() -> None:
    assert p.decode_focus(bytes.fromhex("00fe8e")) == -370


def test_apply_controller_op() -> None:
    before = [p.Controller(id=1, name="a", allowed=True), p.Controller(id=2, name="b", allowed=True)]
    assert p.apply_controller_op(before, p.BondOp.DELETE_CONTROLLER, 1) == [before[1]]
    blocked = p.apply_controller_op(before, p.BondOp.BLOCK_CONTROLLER, 2)
    assert [c.allowed for c in blocked] == [True, False]
    assert p.apply_controller_op(blocked, p.BondOp.ALLOW_CONTROLLER, 2) == before
