"""Event-driven Aranet sampling, with idle/sleep and absence backoff."""

import asyncio
from dataclasses import dataclass
import fcntl
import json
import math
import os
import signal
import struct
import sys

from adaptive_policy import Context, Schedule, absent_interval, pause_reason, present_interval
from aranet_bar import (
    boot_id, boottime, cache_path, clean_name, log, make_snapshot,
    normalize_address, read_snapshot, run_scan, write_snapshot,
)

SCAN_TIMEOUT = 45
MAX_MESSAGE_BYTES = 16 * 1024


@dataclass(frozen=True)
class Control:
    idle: bool = False
    panel_open: bool = False
    request: float = 0


def parse_control(value, address, boot):
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        return None
    if value.get("address") != address or value.get("boot_id") != boot:
        return None
    idle, opened, request = value.get("idle"), value.get("panel_open"), value.get("request", 0)
    if not isinstance(idle, bool) or not isinstance(opened, bool):
        return None
    if isinstance(request, bool) or not isinstance(request, (int, float)):
        return None
    try:
        valid_request = math.isfinite(request) and 0 <= request <= 1e15
    except OverflowError:
        valid_request = False
    if not valid_request:
        return None
    return Control(idle, opened, request)


def emit_message(message):
    """One bounded UTF-8 JSON object per line; diagnostics go to stderr."""
    encoded = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError("Worker output exceeds the protocol limit")
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


async def consume_commands(reader, stream):
    """Stop on EOF so the worker cannot outlive its owning plugin."""
    buffer = bytearray()
    try:
        while not reader.stop.is_set():
            chunk = await stream.read(4096)
            if not chunk:
                break
            buffer.extend(chunk)
            while b"\n" in buffer:
                line, _, remaining = buffer.partition(b"\n")
                buffer = bytearray(remaining)
                if len(line) > MAX_MESSAGE_BYTES:
                    log("Oversized worker control message; stopping.")
                    return
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeError, ValueError, RecursionError):
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "stop":
                    return
                if message.get("type") == "control":
                    reader.on_control(message)
            if len(buffer) > MAX_MESSAGE_BYTES:
                log("Oversized worker control message; stopping.")
                break
    finally:
        reader.request_stop()


class AdaptiveReader:
    def __init__(self, address, directory, emit=None):
        self.address = normalize_address(address)
        self.path = cache_path(directory, self.address)
        self.emit = emit or emit_message
        self.boot = boot_id()
        self.schedule = Schedule(next_scan=boottime())
        self.power = Context()
        self.control = Control()
        self.stop = asyncio.Event()
        self.wakeup = asyncio.Event()
        self.scan_cancel = asyncio.Event()
        self.refreshing = False
        self.scan_manual = False
        self.manual_requested = False
        self.attempt_started = 0.0
        self.last_status = None
        self.scan_mode = "passive"

    def context(self):
        values = vars(self.power).copy()
        values["idle"] = (self.power.idle or self.control.idle) and not self.control.panel_open
        return Context(**values)

    def publish(self):
        state = self.context()
        status = {
            "schema": 1, "address": self.address, "boot_id": self.boot,
            "present": self.schedule.present, "refreshing": self.refreshing,
            "last_attempt": self.attempt_started if self.refreshing else self.schedule.last_attempt,
            "expires_boottime": self.schedule.expires,
            "next_scan": self.schedule.next_scan,
            "pause_reason": "stopped" if self.stop.is_set() else pause_reason(state, self.control.panel_open),
        }
        if status != self.last_status:
            self.send({"type": "status", "status": status})
            self.last_status = status.copy()

    def send(self, event):
        try:
            self.emit(event)
        except (BrokenPipeError, ConnectionResetError):
            self.request_stop()

    def context_changed(self, previous):
        current = self.context()
        was_paused, paused = pause_reason(previous), pause_reason(current)
        if paused:
            if self.refreshing and pause_reason(current, self.scan_manual):
                self.scan_cancel.set()
        elif was_paused:
            # A wake/unlock/return may mean a different physical location.
            self.schedule.request_check(boottime(), invalidate=True)
        elif (previous.on_battery, previous.power_saver) != (current.on_battery, current.power_saver):
            self.schedule.adjust_power(boottime(), current)
        self.publish()
        self.wakeup.set()

    def on_power(self, state):
        previous = self.context()
        self.power = Context(**{key: bool(getattr(state, key, False)) for key in Context.__dataclass_fields__})
        self.context_changed(previous)

    def on_control(self, message):
        value = parse_control(message, self.address, self.boot)
        if value is None:
            return
        previous = self.context()
        old = self.control
        self.control = value
        if value.panel_open and (not old.panel_open or (value.request and value.request != old.request)):
            self.manual_requested = True
        if not value.panel_open:
            self.manual_requested = False
            if self.refreshing and self.scan_manual:
                self.scan_cancel.set()
        self.context_changed(previous)

    def request_stop(self):
        self.stop.set()
        self.scan_cancel.set()
        self.wakeup.set()

    async def scan(self, manual):
        from aranet4.client import Aranet4Advertisement
        from bleak import BleakScanner

        received = asyncio.Event()
        request_at_start = self.control.request
        self.scan_cancel.clear()
        self.refreshing, self.scan_manual = True, manual
        self.attempt_started = boottime()
        self.publish()

        def advertisement(device, data):
            if getattr(device, "address", "").upper() != self.address:
                return
            try:
                decoded = Aranet4Advertisement(device, data)
                if decoded.readings is None:
                    return
                snapshot = make_snapshot(decoded.readings, self.address, name=getattr(device, "name", None), boot=self.boot)
                if snapshot is not None and not received.is_set():
                    write_snapshot(self.path, snapshot)
                    self.send({"type": "snapshot", "snapshot": snapshot})
                    received.set()
            except (ValueError, TypeError, IndexError, AttributeError, OverflowError, struct.error):
                return
            except OSError as error:
                log(f"Could not save Aranet4 reading: {clean_name(error)}")

        try:
            while not self.scan_cancel.is_set():
                options = {"detection_callback": advertisement, "scanning_mode": self.scan_mode}
                if self.scan_mode == "passive":
                    options["bluez"] = {"or_patterns": [(0, 0xff, b"\x02\x07")]}
                else:
                    options["bluez"] = {"filters": {"DuplicateData": True}}
                try:
                    await run_scan(BleakScanner(**options), self.scan_cancel, SCAN_TIMEOUT, received)
                    break
                except Exception as error:
                    if self.scan_mode == "passive":
                        self.scan_mode = "active"
                        log("Using bounded active scans; passive monitoring is unavailable.")
                        continue
                    log(f"Aranet4 scan failed: {clean_name(error)}")
                    break
        finally:
            self.refreshing = False
            self.manual_requested = bool(self.scan_cancel.is_set() and not self.stop.is_set()
                and self.control.panel_open and self.control.request != request_at_start)
            now = boottime()
            if not self.scan_cancel.is_set():
                self.schedule.record_result(received.is_set(), now, self.context())
                if self.control.panel_open:
                    # The user's open details panel may refresh each sensor interval.
                    self.schedule.next_scan = min(self.schedule.next_scan, now + 300)
                    if self.schedule.present:
                        self.schedule.expires = self.schedule.next_scan + 90
                log(f"Aranet4 {'found' if received.is_set() else 'not found'}; next check in {int(self.schedule.next_scan - now)}s.")
            elif manual:
                # Closing the panel does not count as a failed presence check.
                delay = present_interval(self.context()) if self.schedule.present else absent_interval(self.schedule.misses)
                self.schedule.next_scan = max(self.schedule.next_scan, now + delay)
            self.publish()

    async def run(self, stream):
        from power_state import SystemPowerObserver

        observer = SystemPowerObserver()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.request_stop)
        commands = asyncio.create_task(consume_commands(self, stream))
        try:
            cached = read_snapshot(self.path)
            if isinstance(cached, dict) and cached.get("address") == self.address:
                self.send({"type": "snapshot", "snapshot": cached})
            await observer.start(self.on_power)
            self.publish()
            while not self.stop.is_set():
                self.wakeup.clear()
                manual = self.manual_requested and self.control.panel_open
                if pause_reason(self.context(), manual or self.control.panel_open):
                    await self.wakeup.wait()
                    continue
                now = boottime()
                if manual or now >= self.schedule.next_scan:
                    await self.scan(manual or self.control.panel_open)
                    continue
                try:
                    await asyncio.wait_for(self.wakeup.wait(), self.schedule.next_scan - now)
                except TimeoutError:
                    pass
        finally:
            commands.cancel()
            await asyncio.gather(commands, return_exceptions=True)
            await observer.close()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
            self.schedule.present = False
            self.publish()


async def run_listener(address, directory, stream=None, emit=None):
    reader = AdaptiveReader(address, directory, emit=emit)
    reader.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    reader.path.parent.chmod(0o700)
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    with os.fdopen(os.open(reader.path.with_suffix(".lock"), flags, 0o600), "w") as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("An Aranet4 listener is already running for this device.")
            return
        transport = None
        try:
            if stream is None:
                stream = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)
                transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                    lambda: asyncio.StreamReaderProtocol(stream), sys.stdin.buffer)
            await reader.run(stream)
        finally:
            if transport is not None:
                transport.close()
