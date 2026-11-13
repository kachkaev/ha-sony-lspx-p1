"""Async Bluetooth LE client for the Sony LSPX-P1 projector.

Uses bleak and bleak-retry-connector only (no Home Assistant APIs), so tools/lspx_cli.py
can drive a projector with it from a terminal. One command is in flight at a time; the
connection is opened on demand and dropped soon after use so that the official
smartphone app can still connect and Bluetooth proxy slots are freed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from .bluez_agent import just_works_agent
from .protocol import (
    AUTO_STANDBY_INACTIVITY,
    BOND_RESULT_SUCCESS,
    CHAR_BOND_CONTROL,
    CHAR_BONDED_LIST,
    CHAR_BONDING_NAME,
    CHAR_COMMAND,
    CHAR_SECURITY_PROBE,
    CONTROLLER_OPS,
    SELF_LOCKOUT_OPS,
    STATE_READS,
    STATIC_READS,
    AppState,
    AutoStandby,
    BondCommandFailed,
    BondOp,
    CommandRejected,
    Controller,
    ErrorCode,
    HttpEndpoint,
    NetTopology,
    Notification,
    Op,
    PictureMode,
    ProjectorState,
    ProtocolError,
    apply_controller_op,
    check_bond_reply,
    check_reply,
    decode_auto_brightness,
    decode_auto_standby,
    decode_bool,
    decode_controllers,
    decode_focus,
    decode_http_endpoint,
    decode_keystone,
    decode_notification,
    decode_power,
    decode_rgb,
    decode_u8,
    decode_u16,
    encode_bond_activation,
    encode_bond_op,
    encode_command,
    parse_bonded_list,
    payload_auto_brightness,
    payload_bool,
    payload_focus,
    payload_keystone,
    payload_power,
    payload_u8,
    payload_u16,
)

_LOGGER = logging.getLogger(__name__)

REPLY_TIMEOUT = 8.0
# Switching between HDMI and posters: the projector answers only once the switch is done,
# which can take much longer (the app waits up to 60 s).
SWITCH_REPLY_TIMEOUT = 60.0
# How long to keep the link open after the last command: longer after user actions
# (sliders are often moved several times in a row), short after background polls.
COMMAND_LINGER = 15.0
POLL_LINGER = 2.0
TRANSITION_RETRIES = 3
TRANSITION_RETRY_DELAY = 1.5
# Time for BlueZ to see the device again after its bond was removed.
UNPAIR_SETTLE = 3.0
# The projector drops the link after a failed pairing attempt; wait before the next one.
PAIRING_RETRY_DELAY = 2.0
PAIR_BY_READ = "encrypted read"
PAIR_BY_CALL = "explicit pair"
# (method, remove the host-side bond first). The explicit call works on BlueZ (with the
# temporary agent) and ESPHome proxies and fails instantly where unsupported (macOS), where
# the encrypted read pairs instead. The read is tried twice because the projector may need
# a moment after a failed attempt.
PAIRING_ATTEMPTS = ((PAIR_BY_CALL, False), (PAIR_BY_READ, False), (PAIR_BY_READ, False))
# When re-pairing a known projector, the host may still hold a bond the projector forgot.
REPAIRING_EXTRA_ATTEMPTS = ((PAIR_BY_READ, True),)
NOTIFY_RETRIES = 4
# After a failed read through the bonding service, polls skip it for this long (seconds),
# so a projector that does not answer there cannot slow down every poll.
BONDING_RETRY_INTERVAL = 600.0
# The app waits this long after closing what the projector shows before choosing a poster.
STOP_APP_SETTLE = 3.3
# Upper bounds for pairing steps, so a stuck Bluetooth stack cannot hang the setup dialog.
STEP_TIMEOUT = 20.0
CONNECT_TIMEOUT = 60.0
PAIRING_TIMEOUT = 40.0
NOTIFY_RETRY_DELAY = 0.5
# Characteristics that must be present for pairing and control.
REQUIRED_CHARACTERISTICS = (CHAR_COMMAND, CHAR_BOND_CONTROL, CHAR_BONDING_NAME)

# Pushed into the reply queue when the link drops, so a pending command fails at once.
_DISCONNECTED = b""

# Called with each change notification from the projector, in the event loop.
type NotificationCallback = Callable[[Notification], None]


class LSPXError(Exception):
    """Base error for the client."""


class NotPairedError(LSPXError):
    """The projector rejected the link: this controller is not (or no longer) bonded."""


class ProjectorBusyError(LSPXError):
    """The projector's main board does not answer (standby, booting or stuck)."""


class ControllerNotFoundError(LSPXError):
    """No entry with this id in the projector's "Smartphones added" list."""


class OwnControllerError(LSPXError):
    """Removing or blocking this entry would cut this client off: it is the controller we connect as."""


class OwnControllerUnknownError(LSPXError):
    """The projector did not tell which entry is this client, so removing or blocking anything is unsafe."""


async def bluez_state(client: BleakClient) -> dict[str, object] | None:
    """BlueZ's view of the device (paired, bonded, ...), for debug logs. None on other stacks."""
    try:
        backend = getattr(client, "_backend", None)
        path = getattr(backend, "_device_path", None)
        if not path:
            return None
        from bleak.backends.bluezdbus.manager import get_global_bluez_manager  # noqa: PLC0415 - Linux only

        manager = await get_global_bluez_manager()
        props = manager._properties.get(path, {}).get("org.bluez.Device1", {})
        return {key: props.get(key) for key in ("Connected", "ServicesResolved", "Paired", "Bonded", "Trusted")}
    except Exception:
        return None


def describe_error(err: BaseException) -> str:
    """Readable text for an exception whose message may be empty (e.g. TimeoutError)."""
    text = str(err).strip()
    return f"{type(err).__name__}: {text}" if text else type(err).__name__


class LSPXClient:
    """Talks to one projector."""

    def __init__(
        self,
        ble_device: BLEDevice,
        *,
        idle_disconnect: bool = True,
        client_class: type[BleakClient] = BleakClientWithServiceCache,
    ) -> None:
        self._ble_device = ble_device
        self._idle_disconnect = idle_disconnect
        self._client_class = client_class
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._replies: asyncio.Queue[bytes] = asyncio.Queue()
        self._idle_handle: asyncio.TimerHandle | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._idle_deadline = 0.0
        self._secure_attempted = False
        self._bonding_retry_at = 0.0
        self._notification_callback: NotificationCallback | None = None

    # --- connection management ----------------------------------------------

    @property
    def address(self) -> str:
        return self._ble_device.address

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Update the device handle (Home Assistant may route through another adapter or proxy)."""
        self._ble_device = ble_device

    def _on_disconnect(self, client: BleakClient) -> None:
        if client is not self._client:
            return  # a connection we already replaced or dropped
        _LOGGER.debug("%s: disconnected", self.address)
        self._client = None
        self._secure_attempted = False
        self._replies.put_nowait(_DISCONNECTED)

    def set_notification_callback(self, callback: NotificationCallback | None) -> None:
        """Have ``callback`` called with the projector's change notifications (NotifyOp), or stop with None.

        They arrive only while connected; the client never stays connected longer to wait for them.
        """
        self._notification_callback = callback

    def _on_notify(self, _sender: object, data: bytearray) -> None:
        frame = bytes(data)
        notification = decode_notification(frame)
        if notification is None:
            # A reply, or something else (such as a late reply) that _exchange ignores.
            self._replies.put_nowait(frame)
            return
        # Never a reply (see decode_notification), so it stays out of the reply queue.
        _LOGGER.debug("%s: notification %s (frame %s)", self.address, notification.describe(), frame.hex())
        if self._notification_callback is None:
            return
        try:
            self._notification_callback(notification)
        except Exception:
            _LOGGER.exception("%s: handling notification %s failed", self.address, frame.hex())

    async def _establish(self, *, use_services_cache: bool = True) -> BleakClient:
        return await establish_connection(
            self._client_class,
            self._ble_device,
            self._ble_device.name or self.address,
            disconnected_callback=self._on_disconnect,
            ble_device_callback=lambda: self._ble_device,
            use_services_cache=use_services_cache,
        )

    @staticmethod
    def _missing_characteristics(client: BleakClient) -> list[str]:
        services = client.services
        return [uuid for uuid in REQUIRED_CHARACTERISTICS if services.get_characteristic(uuid) is None]

    async def _connect(self) -> BleakClient:
        if self._client is not None and self._client.is_connected:
            return self._client
        _LOGGER.debug("%s: connecting", self.address)
        client = await self._establish()
        self._client = client
        self._secure_attempted = False
        try:
            if missing := self._missing_characteristics(client):
                # An outdated or partial service list (seen with BlueZ after the device was
                # re-added): drop the cache and discover the services again.
                _LOGGER.debug("%s: characteristics %s missing, rediscovering services", self.address, missing)
                # Only BleakClientWithServiceCache has a cache to clear.
                clear_cache: Callable[[], Awaitable[object]] | None = getattr(client, "clear_cache", None)
                with contextlib.suppress(AttributeError, BleakError, TimeoutError):
                    if clear_cache is not None:
                        await clear_cache()
                await self._drop()
                client = await self._establish(use_services_cache=False)
                self._client = client
                if missing := self._missing_characteristics(client):
                    raise BleakError(f"projector did not expose characteristics {', '.join(missing)}")
            await self._start_command_notifications(client)
        except (BleakError, TimeoutError):
            await self._drop()
            raise
        return client

    async def _start_command_notifications(self, client: BleakClient) -> None:
        await self._start_notify(client, CHAR_COMMAND, self._on_notify)

    async def _start_notify(
        self, client: BleakClient, char: str, callback: Callable[[BleakGATTCharacteristic, bytearray], None]
    ) -> None:
        # After a connection that reused cached services, BlueZ may not have exported the
        # characteristic objects yet ("Method StartNotify ... doesn't exist"); give it a moment.
        for attempt in range(NOTIFY_RETRIES):
            try:
                await client.start_notify(char, callback)
                return
            except BleakError as err:
                if not client.is_connected or attempt == NOTIFY_RETRIES - 1:
                    raise
                _LOGGER.debug("%s: enabling notifications failed (%s), retrying", self.address, err)
                await asyncio.sleep(NOTIFY_RETRY_DELAY * (attempt + 1))

    async def _drop(self) -> None:
        """Close the current connection. The caller must hold the lock."""
        client, self._client = self._client, None
        self._secure_attempted = False
        if client is not None:
            _LOGGER.debug("%s: disconnecting", self.address)
            with contextlib.suppress(BleakError, TimeoutError):
                await client.disconnect()

    async def _secure(self, client: BleakClient, *, force: bool = False) -> None:
        """Make sure the link is encrypted with the stored bond (once per connection).

        CoreBluetooth encrypts a bonded link when an encrypted attribute is accessed;
        BlueZ and ESPHome proxies also accept an explicit pair() for a bonded device.
        A failed attempt is not retried on every command: an ACCESS_DENIED reply from the
        projector triggers a forced retry instead.
        """
        if self._secure_attempted and not force:
            return
        self._secure_attempted = True
        try:
            await client.read_gatt_char(CHAR_SECURITY_PROBE)
            return
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("%s: security probe read failed: %s", self.address, err)
        with contextlib.suppress(NotImplementedError, BleakError, TimeoutError):
            await client.pair()
        try:
            await client.read_gatt_char(CHAR_SECURITY_PROBE)
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("%s: link still not encrypted: %s", self.address, err)

    def _schedule_idle_disconnect(self, delay: float) -> None:
        """Disconnect ``delay`` seconds from now, unless a later disconnect is already planned."""
        if not self._idle_disconnect:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + delay
        if self._idle_handle is not None and self._idle_deadline >= deadline:
            return
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        self._idle_deadline = deadline
        self._idle_handle = loop.call_at(deadline, self._on_idle)

    def _on_idle(self) -> None:
        self._idle_handle = None
        self._idle_task = asyncio.get_running_loop().create_task(self._idle_disconnect_task())

    async def _idle_disconnect_task(self) -> None:
        async with self._lock:
            # A command may have run (and planned a later disconnect) while we waited for the lock.
            if self._idle_handle is not None or asyncio.get_running_loop().time() < self._idle_deadline:
                return
            await self._drop()

    async def disconnect(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        self._idle_deadline = 0.0
        async with self._lock:
            await self._drop()

    # --- raw commands --------------------------------------------------------

    async def _exchange(
        self, client: BleakClient, op: int, payload: bytes, reply_timeout: float | None = None
    ) -> bytes:
        while not self._replies.empty():
            if stale := self._replies.get_nowait():  # not the disconnect marker of a link already gone
                _LOGGER.debug("%s: dropping frame %s that arrived between commands", self.address, stale.hex())
        await client.write_gatt_char(CHAR_COMMAND, encode_command(op, payload), response=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (REPLY_TIMEOUT if reply_timeout is None else reply_timeout)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"no reply to command {op}")
            frame = await asyncio.wait_for(self._replies.get(), remaining)
            if frame == _DISCONNECTED:
                raise BleakError(f"disconnected while waiting for the reply to command {op}")
            try:
                return check_reply(op, frame)
            except ProtocolError as err:
                # Late reply to an earlier command or an unsolicited notification.
                _LOGGER.debug("%s: ignoring frame %s (%s)", self.address, frame.hex(), err)

    async def command(
        self,
        op: int,
        payload: bytes = b"",
        *,
        linger: float = COMMAND_LINGER,
        reply_timeout: float | None = None,  # REPLY_TIMEOUT by default
    ) -> bytes:
        """Send one command and return the reply data."""
        async with self._lock:
            try:
                client = await self._connect()
                await self._secure(client)
                try:
                    return await self._exchange(client, op, payload, reply_timeout)
                except CommandRejected as err:
                    if err.code != ErrorCode.ACCESS_DENIED:
                        raise
                    # The bond may exist but the link is not encrypted yet: secure once more.
                    await self._secure(client, force=True)
                    try:
                        return await self._exchange(client, op, payload, reply_timeout)
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
            except (BleakError, TimeoutError):
                # Do not reuse a link in an unknown state.
                await self._drop()
                raise
            finally:
                self._schedule_idle_disconnect(linger)

    # --- pairing ----------------------------------------------------------------

    async def pair(self, controller_name: str = "Home Assistant", *, repairing: bool = False) -> None:
        """Register this controller with the projector.

        Each attempt opens the projector's bonding window, writes the controller name and
        then makes the Bluetooth host pair, on a fresh connection:

        1. by an explicit pair() call (the Android app's createBond(); works on BlueZ with
           the temporary pairing agent and on ESPHome proxies, unsupported on macOS);
        2. by reading a characteristic that requires encryption (how iOS and macOS pair);
        3. by reading again (the projector may need a moment after a failed attempt);
        4. only when re-pairing a projector that was set up before: after removing a host-side
           bond the projector may have forgotten, by reading again. (On BlueZ removing the
           bond drops the whole device, so it is not done for a first pairing.)

        The projector drops the link after a failed attempt, so attempts are spaced out.
        """
        async with just_works_agent(self.address):
            await self._pair_attempts(controller_name, repairing=repairing)

    async def _pair_attempts(self, controller_name: str, *, repairing: bool) -> None:
        attempts = PAIRING_ATTEMPTS + (REPAIRING_EXTRA_ATTEMPTS if repairing else ())
        failures: list[str] = []
        only_connect_failures = True
        last_error: BaseException | None = None
        for method, remove_host_bond in attempts:
            label = f"{method}{' after removing the old pairing' if remove_host_bond else ''}"
            stage = "connecting"
            try:
                async with self._lock:
                    _LOGGER.debug("%s: [%s] connecting", self.address, label)
                    client = await asyncio.wait_for(self._connect(), CONNECT_TIMEOUT)
                    if remove_host_bond:
                        stage = "removing the old pairing"
                        with contextlib.suppress(NotImplementedError, BleakError, TimeoutError):
                            await client.unpair()
                        await self._drop()
                        await asyncio.sleep(UNPAIR_SETTLE)
                        stage = "reconnecting"
                        client = await asyncio.wait_for(self._connect(), CONNECT_TIMEOUT)
                    stage = "opening the bonding window"
                    _LOGGER.debug("%s: [%s] opening the bonding window", self.address, label)
                    await asyncio.wait_for(self._open_bonding_window(client, controller_name), STEP_TIMEOUT)
                    stage = "pairing"
                    _LOGGER.debug("%s: [%s] bonding window open, pairing", self.address, label)
                    await asyncio.wait_for(self._trigger_pairing(client, method), PAIRING_TIMEOUT)
                    _LOGGER.debug("%s: [%s] pairing step done", self.address, label)
                stage = "checking that the projector accepts commands"
                await asyncio.wait_for(self._verify_authorised(), STEP_TIMEOUT)
                _LOGGER.debug("%s: paired using %s", self.address, label)
                return
            except (NotPairedError, BleakError, TimeoutError) as err:
                last_error = err
                if stage != "connecting":
                    only_connect_failures = False
                failures.append(f"{label} failed while {stage}: {describe_error(err)}")
                _LOGGER.debug("%s: pairing attempt %s failed while %s", self.address, label, stage, exc_info=True)
                async with self._lock:
                    if self._client is not None:
                        _LOGGER.debug(
                            "%s: host view after the attempt: %s", self.address, await bluez_state(self._client)
                        )
                    await self._drop()
                await asyncio.sleep(PAIRING_RETRY_DELAY)
        if only_connect_failures and last_error is not None:
            raise BleakError(f"connecting: {describe_error(last_error)}") from last_error
        raise LSPXError("; ".join(failures)) from last_error

    async def _open_bonding_window(self, client: BleakClient, controller_name: str) -> None:
        """Ask the projector to accept a new controller and tell it our name. Caller holds the lock."""
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
        finally:
            with contextlib.suppress(BleakError, TimeoutError):
                await client.stop_notify(CHAR_BOND_CONTROL)

    async def _trigger_pairing(self, client: BleakClient, method: str) -> None:
        """Make the host pair with the projector. Caller holds the lock."""
        self._secure_attempted = True
        if method == PAIR_BY_CALL:
            try:
                await client.pair()
            except NotImplementedError as err:
                raise BleakError("explicit pairing is not supported by this Bluetooth stack") from err
            try:
                await client.read_gatt_char(CHAR_SECURITY_PROBE)
            except BleakError:
                if client.is_connected:
                    raise
                # The projector drops the link right after a new bond; the check that
                # follows reconnects and proves whether pairing took.
                _LOGGER.debug("%s: projector disconnected after pairing, reconnecting to check", self.address)
            return
        # Reading this characteristic needs an encrypted link and triggers pairing.
        await client.read_gatt_char(CHAR_SECURITY_PROBE)

    async def _verify_authorised(self) -> None:
        # ACCESS_DENIED raises NotPairedError; a busy main board still means we are authorised.
        with contextlib.suppress(ProjectorBusyError):
            await self.command(Op.GET_POWER)

    async def bonded_controllers(self) -> list[tuple[int, int, str]]:
        async with self._lock:
            client = await self._connect()
            try:
                return parse_bonded_list(bytes(await client.read_gatt_char(CHAR_BONDED_LIST)))
            finally:
                self._schedule_idle_disconnect(COMMAND_LINGER)

    # --- bonding service: registered controllers and the restriction on adding them ---

    async def _bond_exchange(self, client: BleakClient, op: int, payload: bytes) -> bytes:
        """One ManageBondingControlPoint operation. Caller holds the lock."""
        replies: asyncio.Queue[bytes] = asyncio.Queue()
        await self._start_notify(client, CHAR_BOND_CONTROL, lambda _s, d: replies.put_nowait(bytes(d)))
        try:
            await client.write_gatt_char(CHAR_BOND_CONTROL, encode_bond_op(op, payload), response=True)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + REPLY_TIMEOUT
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(f"no reply to bonding operation {op}")
                frame = await asyncio.wait_for(replies.get(), remaining)
                try:
                    return check_bond_reply(op, frame)
                except ProtocolError as err:
                    _LOGGER.debug("%s: ignoring bonding frame %s (%s)", self.address, frame.hex(), err)
        finally:
            with contextlib.suppress(BleakError, TimeoutError):
                await client.stop_notify(CHAR_BOND_CONTROL)

    async def bond_command(self, op: int, payload: bytes = b"", *, linger: float = COMMAND_LINGER) -> bytes:
        """Run one bonding-control operation on a secured link and return the reply data."""
        async with self._lock:
            try:
                client = await self._connect()
                await self._secure(client)
                return await self._bond_exchange(client, op, payload)
            except (BleakError, TimeoutError):
                await self._drop()
                raise
            finally:
                self._schedule_idle_disconnect(linger)

    async def read_controllers(self, *, linger: float = COMMAND_LINGER) -> list[Controller]:
        """The projector's "Smartphones added" list."""
        async with self._lock:
            try:
                client = await self._connect()
                await self._secure(client)
                return decode_controllers(bytes(await client.read_gatt_char(CHAR_BONDED_LIST)))
            except (BleakError, TimeoutError):
                await self._drop()
                raise
            finally:
                self._schedule_idle_disconnect(linger)

    async def get_restrict_new_controllers(self, *, linger: float = COMMAND_LINGER) -> bool:
        """Whether "Restrict adding smartphones" is on (a new controller then needs a PIN)."""
        data = await self.bond_command(BondOp.GET_PIN_CODE_FLAG, linger=linger)
        if not data:
            raise ProtocolError("empty reply to the restriction flag")
        return data[0] != 0

    async def set_restrict_new_controllers(self, on: bool) -> bool:
        await self.bond_command(BondOp.SET_PIN_CODE_FLAG, payload_bool(on))
        return await self.get_restrict_new_controllers()

    async def get_my_controller_id(self) -> int:
        """The id of this client's own entry in "Smartphones added" (GetBondedCurConDevID)."""
        return decode_u16(await self.bond_command(BondOp.GET_MY_ID))

    async def manage_controller(self, op: BondOp, controller_id: int) -> list[Controller]:
        """Remove, allow or block one entry of "Smartphones added"; return the list afterwards.

        Like the app (which greys out its own entry), this never removes or blocks the controller
        it connects as. The list is read back afterwards; if that read fails, the returned list is
        the one read before with the change applied.
        """
        if op not in CONTROLLER_OPS:
            raise ValueError(f"not an operation on a controller: {op!r}")
        my_id: int | None = None
        if op in SELF_LOCKOUT_OPS:
            try:
                my_id = await self.get_my_controller_id()
            except (BondCommandFailed, ProtocolError) as err:
                raise OwnControllerUnknownError(f"the projector did not tell which entry is this one: {err}") from err
        controllers = await self.read_controllers()
        ids = {c.id for c in controllers}
        if my_id is not None and my_id not in ids:
            # A wrong id (say, misread) could be someone else's or ours: change nothing.
            raise OwnControllerUnknownError(f"the projector says this is controller {my_id}, which is not in the list")
        if controller_id not in ids:
            raise ControllerNotFoundError(f"no controller with id {controller_id}")
        if controller_id == my_id:
            raise OwnControllerError(f"controller {controller_id} is this one")
        await self.bond_command(op, payload_u16(controller_id))
        _LOGGER.debug("%s: bonding operation 0x%02x on controller %d done", self.address, op, controller_id)
        try:
            return await self.read_controllers()
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("%s: reading the controllers after the change failed: %s", self.address, err)
            return apply_controller_op(controllers, op, controller_id)

    # --- state ------------------------------------------------------------------

    async def _read[T](self, op: Op, decoder: Callable[[bytes], T], *, linger: float = COMMAND_LINGER) -> T:
        return decoder(await self.command(op, linger=linger))

    async def read_state(self, previous: ProjectorState | None = None) -> ProjectorState:
        """Read everything. Static ranges are reused from ``previous`` when available."""
        state = ProjectorState()
        # While switching between standby and on, the projector briefly answers
        # UNKNOWN_COMMAND or TIMEOUT. Retry a few times before giving up.
        for attempt in range(TRANSITION_RETRIES):
            try:
                state.power = await self._read(Op.GET_POWER, decode_power, linger=POLL_LINGER)
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
                setattr(state, attr, await self._read(op, decoder, linger=POLL_LINGER))
            except (ProjectorBusyError, CommandRejected, ProtocolError) as err:
                state.errors[attr] = str(err)
        for attr, (op, decoder) in STATIC_READS.items():
            cached = getattr(previous, attr, None) if previous else None
            if cached is not None:
                setattr(state, attr, cached)
                continue
            try:
                setattr(state, attr, await self._read(op, decoder, linger=POLL_LINGER))
            except (ProjectorBusyError, CommandRejected, ProtocolError) as err:
                state.errors[attr] = str(err)
        await self._read_bonding_state(state, previous)
        return state

    async def _read_bonding_state(self, state: ProjectorState, previous: ProjectorState | None) -> None:
        """Read the restriction flag and the controller list into ``state``.

        The bonding service is separate from the projector controls: a failure here only makes
        these values unknown, and polls skip them for a while afterwards (keeping the last values).
        """
        bonding_reads = (
            ("restrict_new_controllers", lambda: self.get_restrict_new_controllers(linger=POLL_LINGER)),
            ("controllers", lambda: self.read_controllers(linger=POLL_LINGER)),
        )
        now = asyncio.get_running_loop().time()
        if now < self._bonding_retry_at:
            for attr, _read in bonding_reads:
                setattr(state, attr, getattr(previous, attr, None) if previous else None)
            return
        for attr, read in bonding_reads:
            try:
                setattr(state, attr, await read())
            except (BondCommandFailed, ProtocolError, BleakError, TimeoutError) as err:
                _LOGGER.debug("%s: reading %s failed: %s", self.address, attr, err)
                state.errors[attr] = str(err) or type(err).__name__
                self._bonding_retry_at = now + BONDING_RETRY_INTERVAL

    # --- setters ----------------------------------------------------------------
    # Each returns the value read back from the projector.

    async def _set_and_read[T](self, set_op: Op, payload: bytes, get_op: Op, decoder: Callable[[bytes], T]) -> T:
        await self.command(set_op, payload)
        return await self._read(get_op, decoder)

    async def set_power(self, on: bool) -> None:
        # Reading the power state right after switching is unreliable while the projector
        # goes to standby or boots, so callers should poll later.
        await self.command(Op.SET_POWER, payload_power(on))

    async def set_volume(self, value: int) -> int:
        return await self._set_and_read(Op.SET_VOLUME, payload_u8(value), Op.GET_VOLUME, decode_u8)

    async def set_xloud(self, on: bool) -> bool:
        return await self._set_and_read(Op.SET_XLOUD, payload_bool(on), Op.GET_XLOUD, decode_bool)

    async def set_auto_brightness(self, on: bool) -> bool:
        return await self._set_and_read(
            Op.SET_AUTO_BRIGHTNESS,
            payload_auto_brightness(on),
            Op.GET_AUTO_BRIGHTNESS,
            decode_auto_brightness,
        )

    async def set_brightness(self, value: int, *, disable_auto: bool = True) -> int:
        if disable_auto and await self._read(Op.GET_AUTO_BRIGHTNESS, decode_auto_brightness):
            await self.set_auto_brightness(False)
        return await self._set_and_read(Op.SET_BRIGHTNESS, payload_u8(value), Op.GET_BRIGHTNESS, decode_u8)

    async def set_focus_assist(self, on: bool) -> bool:
        return await self._set_and_read(Op.SET_FOCUS_ASSIST, payload_bool(on), Op.GET_FOCUS_ASSIST, decode_bool)

    async def set_focus(self, value: int, *, disable_auto: bool = True) -> int:
        if disable_auto and await self._read(Op.GET_FOCUS_ASSIST, decode_bool):
            await self.set_focus_assist(False)
        return await self._set_and_read(Op.SET_FOCUS, payload_focus(value), Op.GET_FOCUS, decode_focus)

    async def set_keystone(self, value: int) -> int:
        return await self._set_and_read(Op.SET_KEYSTONE, payload_keystone(value), Op.GET_KEYSTONE, decode_keystone)

    async def set_auto_direction(self, on: bool) -> bool:
        return await self._set_and_read(Op.SET_AUTO_DIRECTION, payload_bool(on), Op.GET_AUTO_DIRECTION, decode_bool)

    async def set_direction(self, value: int, *, disable_auto: bool = True) -> int:
        if disable_auto and await self._read(Op.GET_AUTO_DIRECTION, decode_bool):
            await self.set_auto_direction(False)
        return await self._set_and_read(Op.SET_DIRECTION, payload_u8(value), Op.GET_DIRECTION, decode_u8)

    async def set_picture_mode(self, mode: int) -> int:
        return await self._set_and_read(
            Op.SET_PICTURE_MODE, payload_u8(PictureMode(mode)), Op.GET_PICTURE_MODE, decode_u8
        )

    async def set_picture_rgb_channel(self, channel: int, value: int) -> tuple[int, int, int]:
        """Set one white balance channel (0 red, 1 green, 2 blue), keeping the other two."""
        rgb = list(await self._read(Op.GET_PICTURE_RGB, decode_rgb))
        rgb[channel] = int(value)
        return await self._set_and_read(Op.SET_PICTURE_RGB, bytes(rgb), Op.GET_PICTURE_RGB, decode_rgb)

    async def set_standby_after_inactivity(self, on: bool) -> tuple[int, int, int]:
        """The app's "Switch to Networked Standby after inactivity", keeping the other auto triggers."""
        flags = list(await self._read(Op.GET_AUTO_STANDBY, decode_auto_standby))
        if on:
            flags[AUTO_STANDBY_INACTIVITY] |= AutoStandby.OFF
        else:
            flags[AUTO_STANDBY_INACTIVITY] &= ~AutoStandby.OFF
        return await self._set_and_read(Op.SET_AUTO_STANDBY, bytes(flags), Op.GET_AUTO_STANDBY, decode_auto_standby)

    async def reset_custom_picture(self, mode: int) -> None:
        """Restore a custom picture mode's defaults (the app's Reset button)."""
        await self.command(Op.RESET_CUSTOM_PICTURE, payload_u8(PictureMode(mode)))

    async def reboot(self) -> str:
        """Restart the projector. Returns how the command ended ("acknowledged", "link dropped", "no reply").

        The app sends the opcode alone and, as the projector may restart before it answers, counts the
        link dropping before the reply as success (BleControl.writeSync); no reply in time counts too.
        Unlike command(), this tells a failure to connect, which fails, from a link lost once the
        command is out.
        """
        async with self._lock:
            try:
                client = await self._connect()
                await self._secure(client)
            except (BleakError, TimeoutError):
                await self._drop()
                raise
            try:
                try:
                    await self._exchange(client, Op.REBOOT, b"")
                except CommandRejected as err:
                    if err.code != ErrorCode.ACCESS_DENIED:
                        raise
                    await self._secure(client, force=True)
                    await self._exchange(client, Op.REBOOT, b"")
                outcome = "acknowledged"
            except CommandRejected as err:
                if err.code == ErrorCode.TIMEOUT:
                    raise ProjectorBusyError(str(err)) from err
                if err.code == ErrorCode.ACCESS_DENIED:
                    raise NotPairedError(
                        "the projector does not accept commands from this controller; pair again"
                    ) from err
                raise
            except TimeoutError:
                outcome = "no reply"
            except BleakError:
                if client.is_connected:
                    raise  # the projector refused the write and is still there
                outcome = "link dropped"
            finally:
                # The old link is of no use once the projector restarts; the next poll reconnects.
                await self._drop()
            _LOGGER.debug("%s: reboot sent, %s", self.address, outcome)
            return outcome

    async def set_picture_value(self, attr: str, value: int) -> int:
        ops = {
            "picture_brightness": (Op.SET_PICTURE_BRIGHTNESS, Op.GET_PICTURE_BRIGHTNESS),
            "picture_contrast": (Op.SET_PICTURE_CONTRAST, Op.GET_PICTURE_CONTRAST),
            "picture_saturation": (Op.SET_PICTURE_SATURATION, Op.GET_PICTURE_SATURATION),
            "picture_sharpness": (Op.SET_PICTURE_SHARPNESS, Op.GET_PICTURE_SHARPNESS),
        }
        set_op, get_op = ops[attr]
        return await self._set_and_read(set_op, payload_u8(value), get_op, decode_u8)

    # --- what the projector shows -------------------------------------------------
    # Both follow the app's Bluetooth-only path. The projector may answer each step only once it is
    # done (or once an earlier switch has settled), so every step waits as long as the app does.

    async def _switch_step(self, op: Op, payload: bytes = b"") -> bytes:
        return await self.command(op, payload, reply_timeout=SWITCH_REPLY_TIMEOUT)

    async def show_hdmi(self) -> None:
        """Switch to the wireless HDMI unit (the app's HDMI button)."""
        await self._switch_step(Op.SET_NET_TOPOLOGY, payload_u8(NetTopology.WIRELESS_HDMI))

    async def show_poster(self, theme: int, *, software_version: str | None = None) -> None:
        """Show a poster theme, leaving HDMI or any other source if needed."""
        topology = decode_u8(await self._switch_step(Op.GET_NET_TOPOLOGY))
        app_state = decode_u8(await self._switch_step(Op.GET_APP_STATE))
        if app_state != AppState.POSTER:
            await self._switch_step(Op.STOP_APP)
            await asyncio.sleep(STOP_APP_SETTLE)
        await self._switch_step(Op.SET_POSTER, payload_u16(theme))
        if topology == NetTopology.WIRELESS_HDMI:
            # Give the Wi-Fi back to the home network, as the app does after leaving HDMI.
            await self._switch_step(Op.SET_NET_TOPOLOGY, payload_u8(home_topology(software_version)))

    async def set_weather(self, code: int) -> None:
        """Tell the projector the weather for its Weather poster (SetWeather, one of WEATHER_CODES).

        From the app's Bluetooth SDK; the app itself never sends it, and it is not yet tried on a projector.
        Like a poster switch, the projector may answer only once the poster is redrawn, so this waits as long.
        """
        await self.command(Op.SET_WEATHER, payload_u16(code), reply_timeout=SWITCH_REPLY_TIMEOUT)

    async def get_weather(self) -> int:
        """The weather code the projector holds for its Weather poster (GetWeather)."""
        return await self._read(Op.GET_WEATHER, decode_u16)

    async def get_http_endpoint(self, *, linger: float = COMMAND_LINGER) -> HttpEndpoint:
        """Where the projector's HTTP API is on its Wi-Fi network (GetHttpEndpoint)."""
        return await self._read(Op.GET_HTTP_ENDPOINT, decode_http_endpoint, linger=linger)


def home_topology(software_version: str | None) -> NetTopology:
    """The topology the app returns to from HDMI: HomePoster from firmware 3.0.0 on, Home before."""
    try:
        major = int((software_version or "").split(".")[0])
    except ValueError:
        return NetTopology.HOME
    return NetTopology.HOME_POSTER if major >= 3 else NetTopology.HOME
