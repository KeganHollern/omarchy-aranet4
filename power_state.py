"""Observe desktop power and session state over D-Bus without polling.

The observer only reads properties and subscribes to signals. It does not
change power profiles, take a suspend inhibitor, or change session state.
"""

import asyncio
import contextlib
from dataclasses import dataclass, replace
import logging
import os

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus


LOG = logging.getLogger(__name__)
LOGIN = "org.freedesktop.login1"
LOGIN_PATH = "/org/freedesktop/login1"
SESSION = LOGIN + ".Session"
UPOWER = "org.freedesktop.UPower"
UPOWER_PATH = "/org/freedesktop/UPower"
PROPERTIES = "org.freedesktop.DBus.Properties"
PROFILES = (
    ("org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles"),
    ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles"),
)


@dataclass(frozen=True)
class PowerState:
    sleeping: bool = False
    lid_closed: bool = False
    lid_present: bool = False
    on_battery: bool = False
    power_saver: bool = False
    locked: bool = False
    inactive: bool = False
    idle: bool = False
    session_id: str = ""
    connected: bool = False

    @property
    def paused(self):
        return self.sleeping or self.lid_closed or self.locked or self.inactive or self.idle


def _value(value):
    return getattr(value, "value", value)


class SystemPowerObserver:
    """Call ``callback(PowerState)`` when relevant system state changes.

    ``await start(callback)`` waits for the first bounded snapshot attempt.
    Optional absent services leave their fields at permissive defaults. A
    dropped system-bus connection is retried without periodic property reads.
    """

    def __init__(self, uid=None):
        self.uid = os.getuid() if uid is None else uid
        self.state = PowerState()
        self._callback = None
        self._bus = None
        self._task = None
        self._refresh_task = None
        self._ready = asyncio.Event()
        self._closed = False
        self._session_path = ""

    async def start(self, callback):
        if self._task is not None:
            raise RuntimeError("Power observer is already started")
        self._callback = callback
        self._task = asyncio.create_task(self._run(), name="aranet-power-state")
        await self._ready.wait()
        return self.state

    async def close(self):
        self._closed = True
        for task in (self._task, self._refresh_task):
            if task is not None:
                task.cancel()
        for task in (self._task, self._refresh_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._task = self._refresh_task = None

    def _update(self, **fields):
        updated = replace(self.state, **fields)
        if updated != self.state:
            self.state = updated
            if self._callback is not None:
                try:
                    self._callback(updated)
                except Exception:
                    LOG.exception("Power-state callback failed")

    async def _call(self, destination, path, interface, member, signature="", body=None):
        reply = await asyncio.wait_for(self._bus.call(Message(
            destination=destination, path=path, interface=interface,
            member=member, signature=signature, body=body or [],
        )), timeout=3)
        if reply is None or reply.message_type == MessageType.ERROR:
            raise RuntimeError(reply.error_name if reply else "No D-Bus reply")
        return reply.body

    async def _properties(self, service, path, interface):
        try:
            body = await self._call(service, path, PROPERTIES, "GetAll", "s", [interface])
            return {key: _value(value) for key, value in body[0].items()}
        except Exception:
            return {}

    async def _subscribe(self):
        matches = [
            f"type='signal',sender='{LOGIN}',interface='{LOGIN}.Manager'",
            f"type='signal',sender='{LOGIN}',interface='{PROPERTIES}'",
            f"type='signal',sender='{UPOWER}',interface='{PROPERTIES}',path='{UPOWER_PATH}'",
        ]
        matches.extend(
            f"type='signal',sender='{service}',interface='{PROPERTIES}',path='{path}'"
            for service, path in PROFILES
        )
        matches.extend(
            "type='signal',sender='org.freedesktop.DBus',"
            "interface='org.freedesktop.DBus',member='NameOwnerChanged',"
            f"arg0='{service}'"
            for service in (LOGIN, UPOWER, *(service for service, _ in PROFILES))
        )
        await asyncio.gather(*(
            self._call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                       "org.freedesktop.DBus", "AddMatch", "s", [match])
            for match in matches
        ))

    async def _snapshot(self):
        manager, upower, profiles, legacy = await asyncio.gather(
            self._properties(LOGIN, LOGIN_PATH, LOGIN + ".Manager"),
            self._properties(UPOWER, UPOWER_PATH, UPOWER),
            self._properties(*PROFILES[0], PROFILES[0][0]),
            self._properties(*PROFILES[1], PROFILES[1][0]),
        )
        self._update(
            sleeping=bool(manager.get("PreparingForSleep", self.state.sleeping)),
            lid_present=bool(upower.get("LidIsPresent", False)),
            lid_closed=bool(upower.get("LidIsClosed", False)),
            on_battery=bool(upower.get("OnBattery", False)),
            power_saver=(profiles or legacy).get("ActiveProfile") == "power-saver",
        )
        await self._select_session()

    async def _select_session(self):
        try:
            body = await self._call(LOGIN, LOGIN_PATH, LOGIN + ".Manager", "ListSessions")
            rows = [row for row in body[0] if len(row) >= 5 and row[1] == self.uid][:16]
        except Exception:
            return
        properties = await asyncio.gather(*(
            self._properties(LOGIN, row[4], SESSION) for row in rows
        ))
        if rows and not any(properties):
            # A transient failure reading session properties is not evidence
            # that the user logged out or moved away.
            return
        choices = [
            (row, props) for row, props in zip(rows, properties)
            if props.get("Type") in ("wayland", "x11")
            and props.get("Class", "user") in ("user", "user-early", "user-light")
        ]
        if not choices:
            self._session_path = ""
            self._update(session_id="", inactive=True, locked=False, idle=False)
            return
        row, props = max(choices, key=lambda item: (
            bool(item[1].get("Active")), not bool(item[1].get("Remote")),
            int(item[1].get("TimestampMonotonic", 0)),
        ))
        self._session_path = row[4]
        self._update(session_id=str(row[0]), inactive=not bool(props.get("Active")),
                     locked=bool(props.get("LockedHint")), idle=bool(props.get("IdleHint")))

    def _schedule_refresh(self):
        if self._closed or self._bus is None:
            return
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._snapshot(), name="aranet-power-refresh")

    def _on_message(self, message):
        if message.message_type != MessageType.SIGNAL:
            return False
        if message.interface == LOGIN + ".Manager":
            if message.member == "PrepareForSleep" and message.body:
                self._update(sleeping=bool(message.body[0]))
            elif message.member in ("SessionNew", "SessionRemoved"):
                self._schedule_refresh()
            return False
        if message.interface == "org.freedesktop.DBus" and message.member == "NameOwnerChanged":
            self._schedule_refresh()
            return False
        if message.interface != PROPERTIES or message.member != "PropertiesChanged" or len(message.body) != 3:
            return False
        interface, changed, invalidated = message.body
        changed = {key: _value(value) for key, value in changed.items()}
        updates = {}
        if message.path == UPOWER_PATH and interface == UPOWER:
            mapping = {"LidIsPresent": "lid_present", "LidIsClosed": "lid_closed", "OnBattery": "on_battery"}
            updates = {field: bool(changed[key]) for key, field in mapping.items() if key in changed}
        elif message.path == LOGIN_PATH and interface == LOGIN + ".Manager":
            if "PreparingForSleep" in changed:
                updates["sleeping"] = bool(changed["PreparingForSleep"])
        elif message.path == self._session_path and interface == SESSION:
            mapping = {"LockedHint": "locked", "IdleHint": "idle"}
            updates = {field: bool(changed[key]) for key, field in mapping.items() if key in changed}
            if "Active" in changed:
                updates["inactive"] = not bool(changed["Active"])
                self._schedule_refresh()
        elif (interface, message.path) in PROFILES and "ActiveProfile" in changed:
            updates["power_saver"] = changed["ActiveProfile"] == "power-saver"
        if updates:
            self._update(**updates)
        if invalidated:
            self._schedule_refresh()
        return False

    async def _run(self):
        retry = 5
        try:
            while not self._closed:
                bus = None
                try:
                    bus = MessageBus(bus_type=BusType.SYSTEM, auth_timeout=3)
                    self._bus = await asyncio.wait_for(bus.connect(), timeout=5)
                    bus.add_message_handler(self._on_message)
                    await self._subscribe()
                    await self._snapshot()
                    self._update(connected=True)
                    self._ready.set()
                    retry = 5
                    await bus.wait_for_disconnect()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    LOG.warning("Power-state observation unavailable: %s", error)
                finally:
                    self._update(connected=False)
                    self._ready.set()
                    if self._refresh_task is not None:
                        self._refresh_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await self._refresh_task
                        self._refresh_task = None
                    if bus is not None:
                        bus.remove_message_handler(self._on_message)
                        bus.disconnect()
                    self._bus = None
                if not self._closed:
                    await asyncio.sleep(retry)
                    retry = min(60, retry * 2)
        finally:
            self._ready.set()
