"""The temporary BlueZ pairing agent."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from dbus_fast import DBusError, MessageType, Variant

from custom_components.sony_lspx_p1 import bluez_agent

ADDRESS = "DD:77:D5:94:25:80"
OUR_DEVICE = "/org/bluez/hci0/dev_DD_77_D5_94_25_80"
OTHER_DEVICE = "/org/bluez/hci0/dev_11_22_33_44_55_66"


class FakeBus:
    def __init__(self, *, fail: str | None = None, objects: dict | None = None) -> None:
        self.fail = fail
        self.objects = objects or {}  # GetManagedObjects' answer
        self.calls: list[str] = []
        self.messages: list = []
        self.exported: dict[str, object] = {}
        self.disconnected = False

    async def connect(self) -> FakeBus:
        return self

    def export(self, path: str, interface: object) -> None:
        self.exported[path] = interface

    async def call(self, message) -> SimpleNamespace:
        self.calls.append(message.member)
        self.messages.append(message)
        if message.member == self.fail:
            return SimpleNamespace(message_type=MessageType.ERROR, error_name="org.bluez.Error.Failed", body=[])
        if message.member == "GetManagedObjects":
            return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[self.objects])
        return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[])

    def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bluez_agent.sys, "platform", "linux")


def use_bus(monkeypatch: pytest.MonkeyPatch, bus: FakeBus) -> None:
    monkeypatch.setattr(bluez_agent, "MessageBus", lambda bus_type: bus)


async def test_noop_outside_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bluez_agent.sys, "platform", "darwin")
    async with bluez_agent.just_works_agent(ADDRESS) as active:
        assert active is False


@pytest.mark.usefixtures("linux")
async def test_registers_and_unregisters(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus()
    use_bus(monkeypatch, bus)
    async with bluez_agent.just_works_agent(ADDRESS) as active:
        assert active is True
        assert bus.calls == ["RegisterAgent", "RequestDefaultAgent"]
    assert bus.calls[-1] == "UnregisterAgent"
    assert bus.disconnected


@pytest.mark.usefixtures("linux")
async def test_cleans_up_when_pairing_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus()
    use_bus(monkeypatch, bus)
    with pytest.raises(RuntimeError):
        async with bluez_agent.just_works_agent(ADDRESS):
            raise RuntimeError("pairing failed")
    assert bus.calls[-1] == "UnregisterAgent"
    assert bus.disconnected


@pytest.mark.usefixtures("linux")
async def test_registration_failure_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus(fail="RegisterAgent")
    use_bus(monkeypatch, bus)
    async with bluez_agent.just_works_agent(ADDRESS) as active:
        assert active is False
    assert "UnregisterAgent" not in bus.calls
    assert bus.disconnected


def test_agent_only_approves_the_projector() -> None:
    agent = bluez_agent._JustWorksAgent(ADDRESS)
    agent._check(OUR_DEVICE, "confirmation")  # does not raise
    with pytest.raises(DBusError):
        agent._check(OTHER_DEVICE, "confirmation")


def test_device_path_suffix() -> None:
    assert bluez_agent.device_path_suffix("dd:77:d5:94:25:80") == "dev_DD_77_D5_94_25_80"


@pytest.mark.usefixtures("linux")
async def test_missing_system_bus_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_bus(bus_type):
        raise FileNotFoundError("/run/dbus/system_bus_socket")

    monkeypatch.setattr(bluez_agent, "MessageBus", no_bus)
    async with bluez_agent.just_works_agent(ADDRESS) as active:
        assert active is False


def test_agent_answers_bluez_requests() -> None:
    agent = bluez_agent._JustWorksAgent(ADDRESS)
    # Just Works: confirmation and authorisation for the projector only.
    agent.RequestConfirmation(OUR_DEVICE, 0)
    agent.RequestAuthorization(OUR_DEVICE)
    agent.AuthorizeService(OUR_DEVICE, "0000ee00-0000-1000-8000-00805f9b34fb")
    for refused in (
        lambda: agent.RequestConfirmation(OTHER_DEVICE, 0),
        lambda: agent.RequestAuthorization(OTHER_DEVICE),
        lambda: agent.AuthorizeService(OTHER_DEVICE, "1234"),
        # No PIN or passkey entry: the projector pairs without one.
        lambda: agent.RequestPinCode(OUR_DEVICE),
        lambda: agent.RequestPasskey(OUR_DEVICE),
    ):
        with pytest.raises(DBusError):
            refused()
    # Notifications only.
    agent.DisplayPinCode(OUR_DEVICE, "0000")
    agent.DisplayPasskey(OUR_DEVICE, 0, 0)
    agent.Release()
    agent.Cancel()


@pytest.mark.usefixtures("linux")
async def test_agent_works_without_becoming_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus(fail="RequestDefaultAgent")
    use_bus(monkeypatch, bus)
    async with bluez_agent.just_works_agent(ADDRESS) as active:
        assert active is True
    assert bus.calls[-1] == "UnregisterAgent"


def device(*, paired: bool, bonded: bool | None = None) -> dict:
    props = {"Address": Variant("s", ADDRESS), "Paired": Variant("b", paired)}
    if bonded is not None:
        props["Bonded"] = Variant("b", bonded)
    return {"org.bluez.Device1": props, "org.freedesktop.DBus.Properties": {}}


BONDS = {
    "/org/bluez/hci0": {"org.bluez.Adapter1": {}},
    OUR_DEVICE: device(paired=True, bonded=True),
    "/org/bluez/hci1/dev_DD_77_D5_94_25_80": device(paired=False, bonded=True),  # bonded only
    "/org/bluez/hci2/dev_DD_77_D5_94_25_80": device(paired=False),  # seen, not paired
    OTHER_DEVICE: device(paired=True),
    OUR_DEVICE + "/service0027": {"org.bluez.GattService1": {}},
}


async def test_bonds_are_left_alone_outside_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bluez_agent.sys, "platform", "darwin")
    assert await bluez_agent.bluez_bonds(ADDRESS) == []
    assert await bluez_agent.remove_bluez_bonds(ADDRESS) == []


@pytest.mark.usefixtures("linux")
async def test_finds_the_bonds_of_the_projector_only(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus(objects=BONDS)
    use_bus(monkeypatch, bus)
    assert await bluez_agent.bluez_bonds(ADDRESS) == [OUR_DEVICE, "/org/bluez/hci1/dev_DD_77_D5_94_25_80"]
    assert bus.calls == ["GetManagedObjects"]
    assert bus.disconnected


@pytest.mark.usefixtures("linux")
async def test_removes_each_bond_through_its_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus(objects=BONDS)
    use_bus(monkeypatch, bus)
    assert await bluez_agent.remove_bluez_bonds(ADDRESS) == [OUR_DEVICE, "/org/bluez/hci1/dev_DD_77_D5_94_25_80"]
    removals = [(m.path, m.interface, m.body) for m in bus.messages if m.member == "RemoveDevice"]
    assert removals == [
        ("/org/bluez/hci0", "org.bluez.Adapter1", [OUR_DEVICE]),
        ("/org/bluez/hci1", "org.bluez.Adapter1", ["/org/bluez/hci1/dev_DD_77_D5_94_25_80"]),
    ]
    assert bus.disconnected


@pytest.mark.usefixtures("linux")
async def test_a_bond_bluez_will_not_remove_is_not_reported_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    use_bus(monkeypatch, FakeBus(objects={OUR_DEVICE: device(paired=True)}, fail="RemoveDevice"))
    assert await bluez_agent.remove_bluez_bonds(ADDRESS) == []


@pytest.mark.usefixtures("linux")
async def test_no_bonds_when_bluez_does_not_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    use_bus(monkeypatch, FakeBus(objects=BONDS, fail="GetManagedObjects"))
    assert await bluez_agent.bluez_bonds(ADDRESS) == []

    def no_bus(bus_type):
        raise FileNotFoundError("/run/dbus/system_bus_socket")

    monkeypatch.setattr(bluez_agent, "MessageBus", no_bus)
    assert await bluez_agent.remove_bluez_bonds(ADDRESS) == []
