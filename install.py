#!/usr/bin/env python3
"""Install the Aranet4 listener and native Omarchy bar widget."""

import argparse
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

from aranet_bar import cache_path, normalize_address
from aranet_setup import install_dependencies

WIDGET_ID = "kegan.aranet4"
LEGACY_WIDGET_ID = "aranet4"
PLUGIN_FILES = (
    "AranetWidget.qml", "Service.qml", "Snapshot.js", "Setup.js", "manifest.json",
    "aranet_bar.py", "adaptive_reader.py", "adaptive_policy.py", "power_state.py",
    "requirements.txt", "install.py", "aranet_setup.py", "README.md", "LICENSE",
)
LEGACY_UNIT = "omarchy-aranet4.service"


def widget_config(document, address, reading_path, python_path=None):
    result = copy.deepcopy(document)
    layout = result.setdefault("bar", {}).setdefault("layout", {})
    widget = {"id": WIDGET_ID, "address": address, "cachePath": str(reading_path)}
    if python_path is not None:
        widget["pythonPath"] = str(python_path)
    replaced = False
    for section, entries in layout.items():
        if isinstance(entries, list):
            updated = []
            for entry in entries:
                entry_id = entry.get("id") if isinstance(entry, dict) else entry
                if entry_id in (LEGACY_WIDGET_ID, WIDGET_ID):
                    if not replaced:
                        updated.append(widget)
                        replaced = True
                else:
                    updated.append(entry)
            layout[section] = updated
    if not replaced:
        layout.setdefault("centerBegin", []).append(widget)
    return result


def atomic_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temp = Path(output.name)
            output.write(content)
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def copy_changed(source, target):
    """Copy a source file without touching a matching installed checkout."""
    source, target = Path(source), Path(target)
    if target.is_file() and source.read_bytes() == target.read_bytes():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def stage_plugin(source, staged):
    for name in PLUGIN_FILES:
        copy_changed(source / name, staged / name)
    shutil.copytree(source / "i18n", staged / "i18n", dirs_exist_ok=True)


def publish_plugin(staged, plugin):
    """Publish the service manifest last, after its executable files exist."""
    plugin.mkdir(parents=True, exist_ok=True)
    for source in staged.rglob("*"):
        if source.is_file() and source.name != "manifest.json":
            copy_changed(source, plugin / source.relative_to(staged))
    copy_changed(staged / "manifest.json", plugin / "manifest.json")


def retire_legacy_service(config, data, reading_path):
    """Retire only the service installed by the old Aranet installer."""
    unit = config / "systemd/user" / LEGACY_UNIT
    if not unit.exists():
        return None
    if unit.is_symlink() or not unit.is_file():
        raise RuntimeError(f"Refusing to replace an unfamiliar service: {unit}")
    raw = unit.read_text()
    starts = [line.split("=", 1)[1] for line in raw.splitlines()
              if line.startswith("ExecStart=")]
    expected = [str(data / "venv/bin/python"), str(data / "aranet_bar.py"), "listen"]
    if len(starts) != 1 or shlex.split(starts[0])[:3] != expected:
        raise RuntimeError(f"Refusing to stop a service not owned by the old Aranet installer: {unit}")
    backup = data / "migration-backups" / datetime.now().strftime("%Y%m%d%H%M%S%f")
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(unit, backup / unit.name)
    subprocess.run(["systemctl", "--user", "disable", "--now", LEGACY_UNIT], check=True)
    if unit.read_text() != raw:
        raise RuntimeError(f"Service changed during migration; leaving it in place: {unit}")
    unit.unlink()
    for suffix in (".control.json", ".status.json"):
        old = reading_path.with_suffix(suffix)
        if old.is_file() and not old.is_symlink():
            shutil.copy2(old, backup / old.name)
            old.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "reset-failed", LEGACY_UNIT],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    print(f"Retired legacy service; migration backup: {backup}")
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True, type=normalize_address)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    subprocess.run(["omarchy", "plugin", "validate", str(source)], check=True)
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "omarchy-aranet4"
    plugin = config / "omarchy/plugins" / WIDGET_ID
    shell_file = config / "omarchy/shell.json"
    data.mkdir(parents=True, exist_ok=True)
    python = data / "venv/bin/python"
    install_dependencies(python, source=source)
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "omarchy-aranet4"
    cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    reading_path = cache_path(cache_root, args.address)
    with tempfile.TemporaryDirectory(prefix="aranet-plugin-") as directory:
        staged = Path(directory)
        stage_plugin(source, staged)
        subprocess.run(["omarchy", "plugin", "validate", str(staged)], check=True)
        # No new worker can be enabled until the legacy owner has released BLE.
        retire_legacy_service(config, data, reading_path)
        publish_plugin(staged, plugin)
    subprocess.run(["omarchy", "plugin", "validate", str(plugin)], check=True)
    subprocess.run(["omarchy-shell", "shell", "rescanPlugins"], check=True)
    # Dependencies may take time to install. Read the current bar layout last.
    document = json.loads(shell_file.read_text())
    updated = widget_config(document, args.address, reading_path, python)
    if document != updated:
        backup = shell_file.with_name(shell_file.name + ".bak.aranet4." + datetime.now().strftime("%Y%m%d%H%M%S%f"))
        shutil.copy2(shell_file, backup)
        atomic_text(shell_file, json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
        print(f"Bar configuration backup: {backup}")
    subprocess.run(["omarchy-shell", "shell", "reloadPlugins"], check=True)
    print(f"Installed Aranet4 widget for {args.address}.")


if __name__ == "__main__":
    main()
