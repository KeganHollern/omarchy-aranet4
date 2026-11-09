#!/usr/bin/env python3
"""Explicit, cancellable dependency setup and broadcast discovery for Aranet4."""

import argparse
import asyncio
import ctypes
import json
import os
from pathlib import Path
import select
import shutil
import signal
import struct
import subprocess
import sys
import time

from aranet_bar import clean_name, normalize_address

MAX_DIAGNOSTICS = 16384
MAX_DEVICES = 16
DISCOVERY_SECONDS = 15


class SetupError(RuntimeError):
    pass


class Cancelled(SetupError):
    pass


class Cancellation:
    """A pipe owned by the shell stays open for the life of an operation."""

    def __init__(self, input_fd=None):
        self.requested = False
        self.input_fd = input_fd

    def poll(self):
        if self.input_fd is not None:
            try:
                ready, _, _ = select.select([self.input_fd], [], [], 0)
                if ready and not os.read(self.input_fd, 4096):
                    self.requested = True
                    self.input_fd = None
            except (OSError, ValueError):
                self.input_fd = None
        if self.requested:
            raise Cancelled("Setup cancelled.")


def default_python():
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data / "omarchy-aranet4/venv/bin/python"


def checked_python(value):
    if not isinstance(value, (str, Path)):
        raise SetupError("The Python path must be an absolute path.")
    value = str(value)
    if not value or len(value) > 4096 or "\x00" in value or not Path(value).is_absolute():
        raise SetupError("The Python path must be an absolute path.")
    return Path(value)


def _child_lifetime(parent_pid):
    # Linux delivers SIGTERM even if the shell/helper is killed abruptly.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Could not bind setup child lifetime")
    if os.getppid() != parent_pid:
        os._exit(1)


def _stop_process(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def run_command(command, *, cancellation=None, timeout=300, diagnostics=True):
    """Run direct argv, bound captured output, and reap the entire child group."""
    if cancellation:
        cancellation.poll()
    parent_pid = os.getpid()
    process = subprocess.Popen(
        [str(value) for value in command], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        preexec_fn=lambda: _child_lifetime(parent_pid),
    )
    deadline = time.monotonic() + timeout
    tail = bytearray()
    forwarded = 0
    try:
        while True:
            if cancellation:
                cancellation.poll()
            if time.monotonic() >= deadline:
                raise SetupError("Setup command timed out.")
            readable, _, _ = select.select([process.stdout], [], [], 0.1)
            if readable:
                chunk = os.read(process.stdout.fileno(), 4096)
                if chunk:
                    tail.extend(chunk)
                    del tail[:-2048]
                    if diagnostics and forwarded < MAX_DIAGNOSTICS:
                        output = chunk[:MAX_DIAGNOSTICS - forwarded]
                        sys.stderr.write(output.decode("utf-8", "replace"))
                        sys.stderr.flush()
                        forwarded += len(output)
                elif process.poll() is not None:
                    break
            elif process.poll() is not None:
                break
        return process.wait(), tail.decode("utf-8", "replace")
    finally:
        if process.poll() is None:
            _stop_process(process)
        process.stdout.close()


def required_versions(source=None):
    requirements = (Path(source) if source else Path(__file__).resolve().parent) / "requirements.txt"
    expected = {}
    for line in requirements.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, version = line.partition("==")
        if not separator or not name or not version:
            raise SetupError("The plugin requires exact dependency versions.")
        expected[name] = version
    return expected


def dependencies_ready(python_path, cancellation=None, source=None):
    python_path = checked_python(python_path)
    if not python_path.is_file():
        return False
    try:
        probe = ("import aranet4.client, bleak, dbus_fast; "
                 "import importlib.metadata as m, json, sys; "
                 "expected=json.loads(sys.argv[1]); "
                 "sys.exit(0 if all(m.version(name)==version for name,version in expected.items()) else 1)")
        code, _ = run_command(
            [python_path, "-c", probe, json.dumps(required_versions(source))],
            cancellation=cancellation, timeout=15, diagnostics=False,
        )
        return code == 0
    except Cancelled:
        raise
    except (OSError, SetupError):
        return False


def setup_status(python_path, cancellation=None):
    python_path = checked_python(python_path)
    return {"ok": True, "dependencies_ready": dependencies_ready(python_path, cancellation),
            "python_path": str(python_path), "uv_available": shutil.which("uv") is not None}


def install_dependencies(python_path=None, *, cancellation=None, source=None):
    """Install only on an explicit request; also reused by the CLI installer."""
    python_path = checked_python(python_path or default_python())
    environment = python_path.parent.parent
    if python_path.name != "python" or python_path.parent.name != "bin":
        raise SetupError("Setup requires a virtual environment path ending in /bin/python.")
    if python_path.exists() and not (environment / "pyvenv.cfg").is_file():
        raise SetupError("Refusing to install into a Python outside a virtual environment.")
    uv = shutil.which("uv")
    if not uv:
        raise SetupError("Install uv, then choose Install Bluetooth support again.")
    requirements = (Path(source) if source else Path(__file__).resolve().parent) / "requirements.txt"
    if not requirements.is_file():
        raise SetupError("The plugin's requirements.txt is missing.")
    environment.parent.mkdir(parents=True, exist_ok=True)
    commands = []
    if not python_path.exists():
        commands.append([uv, "venv", "--python", "/usr/bin/python", environment])
    commands.append([uv, "pip", "install", "--python", python_path, "--no-progress", "-r", requirements])
    for command in commands:
        code, output = run_command(command, cancellation=cancellation)
        if code:
            detail = clean_name(output.strip().splitlines()[-1] if output.strip() else "")
            raise SetupError("Dependency installation failed." + (" " + detail if detail else ""))
    if not dependencies_ready(python_path, cancellation, source):
        raise SetupError("The installed Python dependencies could not be imported.")
    return {"ok": True, "dependencies_ready": True, "python_path": str(python_path), "uv_available": True}


def discovered_device(device, advertisement):
    """Accept named Aranet4s without readings, and decoded nameless broadcasts."""
    from aranet4.client import Aranet4Advertisement, AranetType

    raw_name = getattr(device, "name", None) or getattr(advertisement, "local_name", None)
    name = clean_name(raw_name) if isinstance(raw_name, str) and raw_name.strip() else ""
    named = name.startswith("Aranet4")
    readable = False
    decoded_aranet4 = False
    try:
        decoded = Aranet4Advertisement(device, advertisement)
        reading = decoded.readings
        decoded_aranet4 = reading is not None and reading.type == AranetType.ARANET4
        readable = decoded_aranet4 and isinstance(reading.co2, int) and not isinstance(reading.co2, bool) and 0 < reading.co2 <= 32767
    except (ValueError, TypeError, IndexError, AttributeError, OverflowError, struct.error):
        pass
    if not named and not decoded_aranet4:
        return None
    try:
        address = normalize_address(getattr(device, "address", None))
    except ValueError:
        return None
    return {"address": address, "name": name or "Aranet4", "readable": readable}


async def discover_devices(duration=DISCOVERY_SECONDS, cancellation=None):
    from bleak import BleakScanner

    found = {}

    def received(device, advertisement):
        result = discovered_device(device, advertisement)
        if result is None or (result["address"] not in found and len(found) >= MAX_DEVICES):
            return
        previous = found.get(result["address"])
        if previous and previous["readable"]:
            result["readable"] = True
        found[result["address"]] = result

    scanner = BleakScanner(detection_callback=received, scanning_mode="active",
                           bluez={"filters": {"DuplicateData": True}})
    deadline = asyncio.get_running_loop().time() + duration
    starting = None
    try:
        if cancellation:
            cancellation.poll()
        starting = asyncio.create_task(scanner.start())
        start_deadline = min(deadline, asyncio.get_running_loop().time() + 10)
        while not starting.done():
            if cancellation:
                cancellation.poll()
            if asyncio.get_running_loop().time() >= start_deadline:
                raise SetupError("Bluetooth discovery could not start in time.")
            await asyncio.wait({starting}, timeout=.05)
        await starting
        while asyncio.get_running_loop().time() < deadline:
            if cancellation:
                cancellation.poll()
            await asyncio.sleep(min(.1, max(0, deadline - asyncio.get_running_loop().time())))
    finally:
        if starting is not None and not starting.done():
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        try:
            await asyncio.wait_for(scanner.stop(), 5)
        except Exception as error:
            print("Bluetooth discovery cleanup: " + clean_name(str(error)), file=sys.stderr)
    return {"ok": True, "devices": sorted(found.values(), key=lambda item: (item["name"], item["address"]))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "install", "discover"))
    parser.add_argument("--python", default=str(default_python()))
    parser.add_argument("--timeout", type=float, default=DISCOVERY_SECONDS)
    args = parser.parse_args()
    cancellation = Cancellation(None if sys.stdin.isatty() else sys.stdin.fileno())
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _signal, _frame: setattr(cancellation, "requested", True))
    try:
        if args.action == "status":
            result = setup_status(args.python, cancellation)
        elif args.action == "install":
            result = install_dependencies(args.python, cancellation=cancellation)
        else:
            if not 1 <= args.timeout <= 45:
                raise SetupError("Discovery timeout must be between 1 and 45 seconds.")
            result = asyncio.run(discover_devices(args.timeout, cancellation))
    except Exception as error:
        result = {"ok": False, "dependencies_ready": False,
                  "error": clean_name(str(error)) or type(error).__name__}
        if args.action != "discover":
            result["python_path"] = str(args.python)
        if isinstance(error, Cancelled):
            result["cancelled"] = True
    try:
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")), flush=True)
    except BrokenPipeError:
        pass
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
