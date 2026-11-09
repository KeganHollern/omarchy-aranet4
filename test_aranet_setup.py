import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import aranet_setup as setup


ADDRESS = "AA:BB:CC:DD:EE:FF"


class SetupStatusTests(unittest.TestCase):
    def test_missing_interpreter_reports_not_ready_and_uv_availability(self):
        with patch.object(setup.shutil, "which", return_value="/usr/bin/uv"):
            status = setup.setup_status("/nonexistent/aranet-venv/bin/python")
        self.assertEqual(status, {"ok": True, "dependencies_ready": False,
                                  "python_path": "/nonexistent/aranet-venv/bin/python", "uv_available": True})

    def test_status_does_not_create_files_or_install_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "venv/bin/python"
            with patch.object(setup, "run_command") as command:
                self.assertFalse(setup.setup_status(path)["dependencies_ready"])
                command.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_readiness_requires_all_runtime_imports(self):
        with patch.object(setup.Path, "is_file", return_value=True), patch.object(setup, "run_command", return_value=(1, "missing dependency")) as command:
            self.assertFalse(setup.dependencies_ready("/example/venv/bin/python"))
            self.assertIn("import aranet4.client, bleak, dbus_fast", command.call_args.args[0][2])
            expected = json.loads(command.call_args.args[0][3])
            self.assertEqual(expected["aranet4"], "2.6.0")

    def test_old_dependency_version_is_not_ready_even_when_importable(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "requirements.txt").write_text("pip==0.0.0\n")
            code = ("import sys,types; sys.modules['aranet4']=types.ModuleType('aranet4'); "
                    "sys.modules['aranet4.client']=types.ModuleType('aranet4.client'); "
                    "sys.modules['bleak']=types.ModuleType('bleak'); "
                    "sys.modules['dbus_fast']=types.ModuleType('dbus_fast'); ")
            original = setup.run_command
            def execute(command, **kwargs):
                command[2] = code + command[2]
                return original(command, **kwargs)
            with patch.object(setup, "run_command", side_effect=execute):
                self.assertFalse(setup.dependencies_ready(sys.executable, source=source))

    def test_python_path_must_be_absolute_and_bounded(self):
        for path in ("relative/python", "/tmp/bad\x00python", "/" + "x" * 4096):
            with self.subTest(path=path[:25]), self.assertRaises(setup.SetupError):
                setup.checked_python(path)


class DependencyInstallationTests(unittest.TestCase):
    def test_missing_uv_is_actionable_and_creates_no_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "venv/bin/python"
            with patch.object(setup.shutil, "which", return_value=None):
                with self.assertRaisesRegex(setup.SetupError, "Install uv"):
                    setup.install_dependencies(target)
            self.assertFalse(target.parent.parent.exists())

    def test_system_python_is_not_modified(self):
        with self.assertRaisesRegex(setup.SetupError, "outside a virtual environment"):
            setup.install_dependencies(sys.executable)

    def test_fresh_install_uses_pinned_file_and_explicit_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "venv/bin/python"
            source = Path(directory) / "plugin"
            source.mkdir()
            (source / "requirements.txt").write_text("example==1.0\n")
            with patch.object(setup.shutil, "which", return_value="/tools/uv"), patch.object(setup, "run_command", return_value=(0, "")) as command, patch.object(setup, "dependencies_ready", return_value=True):
                result = setup.install_dependencies(target, source=source)
            self.assertTrue(result["dependencies_ready"])
            self.assertEqual(command.call_args_list[0].args[0], ["/tools/uv", "venv", "--python", "/usr/bin/python", target.parent.parent])
            self.assertEqual(command.call_args_list[1].args[0], ["/tools/uv", "pip", "install", "--python", target, "--no-progress", "-r", source / "requirements.txt"])

    def test_existing_environment_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "venv/bin/python"
            target.parent.mkdir(parents=True)
            target.touch()
            (target.parent.parent / "pyvenv.cfg").write_text("home = /usr/bin\n")
            with patch.object(setup.shutil, "which", return_value="/tools/uv"), patch.object(setup, "run_command", return_value=(0, "")) as command, patch.object(setup, "dependencies_ready", return_value=True):
                setup.install_dependencies(target)
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[0][1:3], ["pip", "install"])


class CancellationTests(unittest.TestCase):
    def test_pipe_eof_cancels_before_launch(self):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        try:
            with self.assertRaises(setup.Cancelled), patch.object(setup.subprocess, "Popen") as command:
                setup.run_command([sys.executable, "-c", "pass"], cancellation=setup.Cancellation(read_fd))
            command.assert_not_called()
        finally:
            os.close(read_fd)

    def test_cancellation_reaps_the_command_process(self):
        class CancelSoon:
            def __init__(self): self.deadline = time.monotonic() + .2
            def poll(self):
                if time.monotonic() >= self.deadline:
                    raise setup.Cancelled("test cancellation")
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "pid"
            code = "import os,time,pathlib; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(60)" % str(pid_file)
            with self.assertRaises(setup.Cancelled):
                setup.run_command([sys.executable, "-c", code], cancellation=CancelSoon(), diagnostics=False)
            pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_nonzero_exit_is_reported_with_bounded_output(self):
        code, output = setup.run_command([sys.executable, "-c", "import sys; print('x'*20000); sys.exit(7)"], diagnostics=False)
        self.assertEqual(code, 7)
        self.assertLessEqual(len(output), 2048)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.decoded = SimpleNamespace(readings=None)
        client = SimpleNamespace(Aranet4Advertisement=lambda *_: self.decoded,
                                 AranetType=SimpleNamespace(ARANET4=0))
        self.enterContext(patch.dict(sys.modules, {"aranet4": SimpleNamespace(), "aranet4.client": client}))

    def test_named_sensor_without_smart_home_data_remains_selectable_as_unreadable(self):
        device = SimpleNamespace(address=ADDRESS, name="Aranet4 example")
        result = setup.discovered_device(device, SimpleNamespace())
        self.assertEqual(result, {"address": ADDRESS, "name": "Aranet4 example", "readable": False})

    def test_nameless_valid_aranet4_is_recognized(self):
        self.decoded.readings = SimpleNamespace(type=0, co2=650)
        result = setup.discovered_device(SimpleNamespace(address=ADDRESS, name=None), SimpleNamespace())
        self.assertEqual(result, {"address": ADDRESS, "name": "Aranet4", "readable": True})

    def test_unknown_or_other_sensor_is_ignored(self):
        for name in (None, "Keyboard", "Aranet2"):
            self.assertIsNone(setup.discovered_device(SimpleNamespace(address=ADDRESS, name=name), SimpleNamespace()))

    def test_invalid_address_is_ignored(self):
        self.assertIsNone(setup.discovered_device(SimpleNamespace(address="bad", name="Aranet4"), SimpleNamespace()))

    def test_malformed_advertisement_does_not_hide_named_sensor(self):
        client = sys.modules["aranet4.client"]
        client.Aranet4Advertisement = lambda *_: (_ for _ in ()).throw(ValueError("bad broadcast"))
        self.assertFalse(setup.discovered_device(SimpleNamespace(address=ADDRESS, name="Aranet4"), SimpleNamespace())["readable"])

    async def test_discovery_deduplicates_caps_devices_and_stops_scanner(self):
        stopped = []
        class Scanner:
            def __init__(self, **options): self.callback = options["detection_callback"]
            async def start(self):
                for index in range(20):
                    self.callback(SimpleNamespace(address=f"AA:BB:CC:DD:EE:{index:02X}", name="Aranet4 example"), SimpleNamespace())
                self.callback(SimpleNamespace(address="AA:BB:CC:DD:EE:00", name="Aranet4 duplicate"), SimpleNamespace())
            async def stop(self): stopped.append(True)
        with patch.dict(sys.modules, {"bleak": SimpleNamespace(BleakScanner=Scanner)}):
            result = await setup.discover_devices(.001)
        self.assertEqual(len(result["devices"]), 16)
        self.assertEqual(len({value["address"] for value in result["devices"]}), 16)
        self.assertEqual(stopped, [True])

    async def test_cancelled_discovery_stops_scanner(self):
        stopped = []
        class Scanner:
            def __init__(self, **_options): pass
            async def start(self): pass
            async def stop(self): stopped.append(True)
        cancellation = setup.Cancellation()
        cancellation.requested = True
        with patch.dict(sys.modules, {"bleak": SimpleNamespace(BleakScanner=Scanner)}), self.assertRaises(setup.Cancelled):
            await setup.discover_devices(.1, cancellation)
        self.assertEqual(stopped, [True])


if __name__ == "__main__":
    unittest.main()
