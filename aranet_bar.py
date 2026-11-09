#!/usr/bin/env python3
"""Receive Aranet4 advertisements and render fresh readings for Omarchy."""

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unicodedata

STALE_SECONDS = 120


def normalize_address(address):
    if not isinstance(address, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", address):
        raise ValueError("Expected a Bluetooth address such as AA:BB:CC:DD:EE:FF")
    return address.upper()


def boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def clean_name(value):
    return "".join(c for c in str(value or "Aranet4")[:96] if not unicodedata.category(c).startswith("C")).strip() or "Aranet4"


def number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and low <= value <= high else None
    except OverflowError:
        return None


def cache_path(cache_dir, address):
    return Path(cache_dir) / (normalize_address(address).replace(":", "").lower() + ".json")


def make_snapshot(readings, address, *, name=None, now=None, boot=None):
    co2 = number(getattr(readings, "co2", None), 1, 32767)
    if co2 is None or int(co2) != co2:
        return None
    return {
        "schema": 1,
        "address": normalize_address(address),
        "name": clean_name(name or getattr(readings, "name", None)),
        "seen_boottime": boottime() if now is None else now,
        "boot_id": boot_id() if boot is None else boot,
        "co2": int(co2),
        "temperature": number(getattr(readings, "temperature", None), -50, 100),
        "humidity": number(getattr(readings, "humidity", None), 0, 100),
        "pressure": number(getattr(readings, "pressure", None), 1, 2000),
        "battery": number(getattr(readings, "battery", None), 0, 100),
        "sample_age": number(getattr(readings, "ago", None), 0, 65535),
    }


def write_snapshot(path, snapshot):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".reading-", delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            json.dump(snapshot, output, ensure_ascii=False, allow_nan=False)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_snapshot(path):
    try:
        with Path(path).open(encoding="utf-8") as source:
            raw = source.read(8193)
        return json.loads(raw) if len(raw) <= 8192 else None
    except (OSError, ValueError, UnicodeError):
        return None


def render_snapshot(snapshot, address, *, now=None, boot=None, status=None):
    if not isinstance(snapshot, dict) or snapshot.get("schema") != 1:
        return None
    now = boottime() if now is None else now
    boot = boot_id() if boot is None else boot
    seen = number(snapshot.get("seen_boottime"), 0, now)
    co2 = number(snapshot.get("co2"), 1, 32767)
    fresh = seen is not None and now - seen < STALE_SECONDS
    if (isinstance(status, dict) and type(status.get("schema")) is int and status["schema"] == 1
            and status.get("address") == normalize_address(address) and status.get("boot_id") == boot
            and isinstance(status.get("present"), bool)):
        attempt = number(status.get("last_attempt"), 0, now)
        if attempt is not None:
            expires = number(status.get("expires_boottime"), attempt, attempt + 86400)
            fresh = status["present"] and expires is not None and now < expires
    if (snapshot.get("address") != normalize_address(address) or snapshot.get("boot_id") != boot
            or seen is None or not fresh or co2 is None or int(co2) != co2):
        return None
    co2 = int(co2)
    lines = [clean_name(snapshot.get("name")), f"CO₂: {co2} ppm"]
    temp = number(snapshot.get("temperature"), -50, 100)
    if temp is not None:
        lines.append(f"Temperature: {temp:.1f} °C / {temp * 9 / 5 + 32:.1f} °F")
    for key, label, unit, low, high in [
        ("humidity", "Humidity", "%", 0, 100),
        ("pressure", "Pressure", " hPa", 1, 2000),
        ("battery", "Battery", "%", 0, 100),
    ]:
        value = number(snapshot.get(key), low, high)
        if value is not None:
            lines.append(f"{label}: {value:g}{unit}")
    age = number(snapshot.get("sample_age"), 0, 65535)
    if age is not None:
        lines.append(f"Measurement: {int(age + now - seen)} seconds ago")
    return {"text": f"CO₂ {co2} ppm", "tooltip": "\n".join(lines)}


def log(message):
    print(message, file=sys.stderr, flush=True)


async def wait_or_stop(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass


async def run_scan(scanner, stop, duration, received=None):
    started = asyncio.get_running_loop().time()
    try:
        await asyncio.wait_for(scanner.start(), 15)
        if duration is None:
            await stop.wait()
        else:
            waiters = [asyncio.create_task(stop.wait())]
            if received is not None:
                waiters.append(asyncio.create_task(received.wait()))
            try:
                remaining = max(0, duration - (asyncio.get_running_loop().time() - started))
                await asyncio.wait(waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters:
                    waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
    finally:
        try:
            await asyncio.wait_for(scanner.stop(), 5)
        except Exception as error:
            log(f"Scanner cleanup: {clean_name(error)}")


async def listen(address, cache_dir):
    from adaptive_reader import run_listener
    await run_listener(address, cache_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("worker", "listen", "render"))
    parser.add_argument("--address", required=True, type=normalize_address)
    default_cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "omarchy-aranet4"
    parser.add_argument("--cache-dir", type=Path, default=default_cache)
    args = parser.parse_args()
    if args.command in ("worker", "listen"):
        try:
            asyncio.run(listen(args.address, args.cache_dir))
        except KeyboardInterrupt:
            pass
    else:
        path = cache_path(args.cache_dir, args.address)
        output = render_snapshot(read_snapshot(path), args.address)
        if output is not None:
            print(json.dumps(output, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
