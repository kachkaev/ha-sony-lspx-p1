"""A short-lived BlueZ pairing agent for "Just Works" pairing on Linux.

BlueZ asks a registered agent to approve every pairing, even one without a PIN.
Desktops and phones always have one; Home Assistant OS does not, so pairing either fails
at once (``AuthenticationFailed``) or hangs (a read that needs encryption stays
``InProgress``). While pairing, we register an agent with the ``NoInputNoOutput``
capability that approves requests for the projector being paired only, and remove it
afterwards. On other platforms (macOS, ESPHome proxies) this is a no-op.

It also finds and removes a bond BlueZ holds with the projector, without a connection: a projector that forgot the
key (its entry deleted in the app) drops every link BlueZ makes with it, so the bond cannot be removed through one.

No ``from __future__ import annotations`` here: dbus-fast reads the D-Bus signatures from
the method annotations, and postponed evaluation would turn ``-> None`` into a string.
"""

import contextlib
import logging
import sys
from collections.abc import AsyncIterator

_LOGGER = logging.getLogger(__name__)

AGENT_PATH = "/io/github/kachkaev/ha_sony_lspx_p1/agent"
CAPABILITY = "NoInputNoOutput"
BLUEZ = "org.bluez"
AGENT_MANAGER = "org.bluez.AgentManager1"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
REJECTED = "org.bluez.Error.Rejected"

try:
    from dbus_fast import BusType, DBusError, Message, MessageType
    from dbus_fast.aio import MessageBus
    from dbus_fast.service import ServiceInterface, method

    HAS_DBUS = True
except ImportError:  # pragma: no cover - dbus-fast is only guaranteed on Linux
    HAS_DBUS = False


def device_path_suffix(address: str) -> str:
    """BlueZ object path suffix for a device address, e.g. 'dev_AA_BB_CC_DD_EE_FF'."""
    return "dev_" + address.upper().replace(":", "_")


if HAS_DBUS:

    class _JustWorksAgent(ServiceInterface):
        """Approves Just Works pairing for one device and refuses everything else."""

        def __init__(self, address: str) -> None:
            super().__init__("org.bluez.Agent1")
            self._suffix = device_path_suffix(address)

        def _check(self, device: str, what: str) -> None:
            if not device.endswith(self._suffix):
                _LOGGER.debug("Pairing agent: refusing %s for %s", what, device)
                raise DBusError(REJECTED, "not the device being paired")
            _LOGGER.debug("Pairing agent: approving %s for %s", what, device)

        @method()
        def Release(self) -> None:
            _LOGGER.debug("Pairing agent released")

        @method()
        def RequestPinCode(self, device: "o") -> "s":
            raise DBusError(REJECTED, "PIN pairing is not supported")

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s") -> None:
            _LOGGER.debug("Pairing agent: projector asked to display PIN code for %s", device)

        @method()
        def RequestPasskey(self, device: "o") -> "u":
            raise DBusError(REJECTED, "passkey entry is not supported")

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:
            _LOGGER.debug("Pairing agent: passkey display requested for %s", device)

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u") -> None:
            self._check(device, "confirmation")

        @method()
        def RequestAuthorization(self, device: "o") -> None:
            self._check(device, "authorization")

        @method()
        def AuthorizeService(self, device: "o", uuid: "s") -> None:
            self._check(device, f"service {uuid}")

        @method()
        def Cancel(self) -> None:
            _LOGGER.debug("Pairing agent: request cancelled")


async def _call(
    bus: "MessageBus",
    member: str,
    signature: str,
    body: list[str],
    *,
    path: str = "/org/bluez",
    interface: str = AGENT_MANAGER,
) -> str | None:
    """Call a BlueZ method (AgentManager1's by default); return an error description, or None on success."""
    reply = await bus.call(
        Message(destination=BLUEZ, path=path, interface=interface, member=member, signature=signature, body=body)
    )
    if reply is None or reply.message_type == MessageType.ERROR:
        return f"{getattr(reply, 'error_name', 'no reply')}: {getattr(reply, 'body', '')}"
    return None


async def _bonded_device_paths(bus: "MessageBus", address: str) -> list[str]:
    """BlueZ's objects for the device (one per adapter that knows it) that are paired or bonded."""
    reply = await bus.call(Message(destination=BLUEZ, path="/", interface=OBJECT_MANAGER, member="GetManagedObjects"))
    if reply is None or reply.message_type == MessageType.ERROR or not reply.body:
        return []
    suffix = "/" + device_path_suffix(address)
    paths = []
    for path, interfaces in reply.body[0].items():
        props = interfaces.get(DEVICE)
        if not path.endswith(suffix) or props is None:
            continue
        if any(getattr(props.get(key), "value", False) for key in ("Paired", "Bonded")):
            paths.append(path)
    return paths


async def _with_bonds(address: str, remove: bool) -> list[str]:
    """The device paths BlueZ holds a bond for, each removed first if ``remove``. Empty off Linux or on failure."""
    if not sys.platform.startswith("linux") or not HAS_DBUS:
        return []
    bus: "MessageBus | None" = None
    found: list[str] = []
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        for path in await _bonded_device_paths(bus, address):
            if remove:
                adapter = path.rsplit("/", 1)[0]
                if error := await _call(bus, "RemoveDevice", "o", [path], path=adapter, interface=ADAPTER):
                    _LOGGER.debug("Could not remove BlueZ's bond %s: %s", path, error)
                    continue
                _LOGGER.debug("Removed BlueZ's bond %s", path)
            found.append(path)
    except Exception as err:
        _LOGGER.debug("BlueZ's bonds not available: %s", err)
    finally:
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.disconnect()
    return found


async def bluez_bonds(address: str) -> list[str]:
    """BlueZ's objects holding a bond with the device; empty on other platforms (macOS, or none held)."""
    return await _with_bonds(address, remove=False)


async def remove_bluez_bonds(address: str) -> list[str]:
    """Remove every bond BlueZ holds with the device, with no connection needed. Returns the objects removed.

    BlueZ drops the device with its bond; it comes back with the next advertisement.
    """
    return await _with_bonds(address, remove=True)


@contextlib.asynccontextmanager
async def just_works_agent(address: str) -> AsyncIterator[bool]:
    """Register the agent for the duration of the block. Yields whether it is active."""
    if not sys.platform.startswith("linux") or not HAS_DBUS:
        yield False
        return
    bus: "MessageBus | None" = None
    registered = False
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        bus.export(AGENT_PATH, _JustWorksAgent(address))
        if error := await _call(bus, "RegisterAgent", "os", [AGENT_PATH, CAPABILITY]):
            _LOGGER.debug("Could not register the pairing agent: %s", error)
        else:
            registered = True
            if error := await _call(bus, "RequestDefaultAgent", "o", [AGENT_PATH]):
                _LOGGER.debug("Could not make the pairing agent the default: %s", error)
    except Exception as err:
        _LOGGER.debug("Pairing agent not available: %s", err)
    try:
        _LOGGER.debug("Pairing agent active: %s", registered)
        yield registered
    finally:
        if bus is not None:
            if registered:
                with contextlib.suppress(Exception):
                    await _call(bus, "UnregisterAgent", "o", [AGENT_PATH])
            with contextlib.suppress(Exception):
                bus.disconnect()
