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


def test_decode_notification() -> None:
    # Framed like a reply, as the app parses it; the result byte a real projector sends is not known yet.
    n = p.decode_notification(bytes.fromhex("01801210"))
    assert n == p.Notification(op=p.NotifyOp.APP_STATE, result=1, data=b"\x10")
    assert n.describe() == "0x8012 (APP_STATE), result 1, data 10"
    assert p.decode_notification(bytes.fromhex("018020")).describe() == "0x8020 (unknown), result 1, data -"
    # Replies (any opcode a command has), frames too short to tell and other framings are not notifications.
    assert p.decode_notification(bytes.fromhex("02016151")) is None
    assert p.decode_notification(bytes.fromhex("030161a0")) is None
    assert p.decode_notification(bytes.fromhex("801210")) is None
    assert p.decode_notification(bytes.fromhex("0180")) is None
    assert p.decode_notification(b"") is None


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


def test_usage_time_is_one_counter_of_seconds() -> None:
    # Two readings from a real projector, 16 s of use apart. Read as [hours:u16][minutes][seconds], they showed
    # 7.25 h then 7.20 h (the lowest byte wrapped).
    assert p.decode_usage_time(bytes.fromhex("0003fbff")) == 72.53
    assert p.decode_usage_time(bytes.fromhex("0003fc0f")) == 72.54
    # On that projector, crossing 0x00040000 also made 7.26 h jump to 4.01 h. These two frames (not captured ones)
    # stand either side of it: the value keeps rising.
    assert p.decode_usage_time(bytes.fromhex("0003ffff")) == 72.82
    assert p.decode_usage_time(bytes.fromhex("00040024")) == 72.83
    with pytest.raises(p.ProtocolError, match="usage time"):
        p.decode_usage_time(bytes.fromhex("0003fc"))


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


def test_decode_http_endpoint() -> None:
    endpoint = p.decode_http_endpoint(b"\x00\x50" + b"192.168.1.50\x00\x00\x00")
    assert endpoint == p.HttpEndpoint(80, "192.168.1.50")
    assert endpoint.host == "192.168.1.50"
    assert not endpoint.direct
    assert p.decode_http_endpoint(b"\x00\x50" + b"192.168.8.100\x00").direct  # as the app's isDirect
    # Only that /24: neighbours are ordinary home addresses.
    for address in ("192.168.80.1", "192.168.9.1", "192.168.1.8", "10.192.168.8"):
        assert p.HttpEndpoint(80, address).host == address
        assert not p.HttpEndpoint(80, address).direct
    assert not p.HttpEndpoint(0, "").direct
    # Not on Wi-Fi: no usable address.
    for address in (b"", b"0.0.0.0", b"\x00" * 16, b"not an address", b"::1"):
        assert p.decode_http_endpoint(b"\x00\x00" + address).host is None
    with pytest.raises(p.ProtocolError):
        p.decode_http_endpoint(b"\x00")


@pytest.mark.parametrize(
    ("address", "topology", "home_host"),
    [
        ("192.168.1.50", 17, "192.168.1.50"),
        ("192.168.1.50", None, "192.168.1.50"),  # topology unknown: an ordinary address is fine
        ("192.168.1.50", 33, None),  # Wi-Fi Direct: the projector's Wi-Fi is its own network, whatever it reports
        ("192.168.1.50", 48, None),  # wireless HDMI
        ("192.168.8.1", 17, None),  # its own access point, or a home router on 192.168.8.x (GL.iNet's default)
        ("192.168.8.1", 33, None),
        ("192.168.8.100", 16, None),  # the wireless HDMI unit, e.g. read right after switching back
        ("192.168.8.57", 18, None),  # any 192.168.8.x, as the app's isDirect: also a home network on it
        ("192.168.8.57", None, None),
        ("192.168.80.57", 17, "192.168.80.57"),
        ("0.0.0.0", 17, None),
        ("", 17, None),
    ],
)
def test_http_endpoint_home_host(address: str, topology: int | None, home_host: str | None) -> None:
    assert p.HttpEndpoint(80, address).home_host(topology) == home_host
