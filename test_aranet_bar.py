import copy
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

import aranet_bar as widget
from install import widget_config

ADDRESS = "AA:BB:CC:DD:EE:FF"


class WidgetTests(unittest.TestCase):
    def setUp(self):
        self.readings = SimpleNamespace(co2=742, temperature=22.4, humidity=48,
                                        pressure=1012.5, battery=85, ago=17)
        self.snapshot = widget.make_snapshot(self.readings, ADDRESS, name="Aranet4 example", now=100, boot="test")

    def render(self, snapshot=None, now=105, boot="test"):
        return widget.render_snapshot(self.snapshot if snapshot is None else snapshot, ADDRESS, now=now, boot=boot)

    def test_fresh_reading_and_tooltip(self):
        result = self.render()
        self.assertEqual(result["text"], "CO₂ 742 ppm")
        for text in ("Aranet4 example", "22.4 °C", "48%", "1012.5 hPa", "85%", "22 seconds"):
            self.assertIn(text, result["tooltip"])
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_lost_sensor_hides_at_expiry(self):
        self.assertIsNotNone(self.render(now=219.99))
        self.assertIsNone(self.render(now=220))
        self.assertIsNone(self.render(now=1000))

    def test_five_minute_measurement_does_not_expire_fresh_broadcast(self):
        self.snapshot["sample_age"] = 290
        result = self.render(now=105)
        self.assertEqual(result["text"], "CO₂ 742 ppm")
        self.assertIn("295 seconds", result["tooltip"])

    def test_hybrid_presence_lease_outlives_short_legacy_expiry(self):
        status = {"schema": 1, "address": ADDRESS, "boot_id": "test", "present": True,
                  "last_attempt": 100, "expires_boottime": 490}
        result = widget.render_snapshot(self.snapshot, ADDRESS, now=400, boot="test", status=status)
        self.assertEqual(result["text"], "CO₂ 742 ppm")
        self.assertIsNone(widget.render_snapshot(self.snapshot, ADDRESS, now=490, boot="test", status=status))
        status["present"] = False
        self.assertIsNone(widget.render_snapshot(self.snapshot, ADDRESS, now=105, boot="test", status=status))

    def test_reboot_suspend_and_other_device_hide(self):
        self.assertIsNone(self.render(boot="new-boot"))
        self.assertIsNone(self.render(now=10000))
        self.assertIsNone(self.render(now=99))
        wrong = dict(self.snapshot, address="11:22:33:44:55:66")
        self.assertIsNone(self.render(wrong))

    def test_invalid_co2_never_displays(self):
        for value in (-1, 0, 32768, float("nan"), float("inf"), "742", True):
            with self.subTest(value=value):
                self.readings.co2 = value
                self.assertIsNone(widget.make_snapshot(self.readings, ADDRESS))
                self.assertIsNone(self.render(dict(self.snapshot, co2=value)))

    def test_untrusted_name_and_missing_optional_values(self):
        self.readings.temperature = float("nan")
        self.readings.battery = -1
        result = widget.make_snapshot(self.readings, ADDRESS, name="<img src=x>\n\x1b[31m", now=100, boot="test")
        rendered = self.render(result)
        self.assertNotIn("Temperature:", rendered["tooltip"])
        self.assertNotIn("Battery:", rendered["tooltip"])
        self.assertNotIn("\x1b", rendered["tooltip"])
        self.assertEqual(rendered["tooltip"].splitlines()[0], "<img src=x>[31m")

    def test_cache_is_atomic_private_and_bad_data_hides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache/reading.json"
            widget.write_snapshot(path, self.snapshot)
            self.assertEqual(widget.read_snapshot(path), self.snapshot)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            for raw in ("not json", "x" * 9000):
                path.write_text(raw)
                self.assertIsNone(widget.read_snapshot(path))

    def test_cli_offline_stdout_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, widget.__file__, "render", "--address", ADDRESS,
                                     "--cache-dir", directory], capture_output=True, check=True)
            self.assertEqual(result.stdout, b"")

    def test_cli_fresh_then_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = widget.make_snapshot(self.readings, ADDRESS)
            path = widget.cache_path(directory, ADDRESS)
            command = [sys.executable, widget.__file__, "render", "--address", ADDRESS, "--cache-dir", directory]
            widget.write_snapshot(path, snapshot)
            result = subprocess.run(command, capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout)["text"], "CO₂ 742 ppm")
            snapshot["seen_boottime"] -= 130
            widget.write_snapshot(path, snapshot)
            self.assertEqual(subprocess.run(command, capture_output=True, check=True).stdout, b"")

    def test_install_preserves_layout_and_is_idempotent(self):
        config = {"version": 1, "idle": {"lock": 300}, "bar": {"layout": {
            "center": [{"id": "omarchy.clock", "format": "HH:mm"}],
            "right": [{"id": "omarchy.agents"},
                      {"id": "aranet4", "type": "qml", "source": "/legacy/AranetWidget.qml"},
                      {"id": "omarchy.bluetooth"}]}}}
        original = copy.deepcopy(config)
        updated = widget_config(config, ADDRESS, "/example/reading.json")
        self.assertEqual(config, original)
        self.assertEqual(updated["idle"], config["idle"])
        self.assertEqual(updated["bar"]["layout"]["center"], config["bar"]["layout"]["center"])
        entries = updated["bar"]["layout"]["right"]
        self.assertEqual([entry["id"] for entry in entries], ["omarchy.agents", "kegan.aranet4", "omarchy.bluetooth"])
        self.assertNotIn("text", entries[1])
        self.assertNotIn("exec", entries[1])
        self.assertNotIn("type", entries[1])
        self.assertNotIn("source", entries[1])
        self.assertEqual(entries[1]["address"], ADDRESS)
        self.assertEqual(widget_config(updated, ADDRESS, "/example/reading.json"), updated)

    def test_new_plugin_defaults_beside_clock(self):
        config = {"bar": {"layout": {"centerBegin": [{"id": "omarchy.indicators"}],
                                     "center": [{"id": "omarchy.clock"}],
                                     "right": [{"id": "omarchy.bluetooth"}]}}}
        updated = widget_config(config, ADDRESS, "/example/reading.json")
        self.assertEqual([entry["id"] for entry in updated["bar"]["layout"]["centerBegin"]],
                         ["omarchy.indicators", "kegan.aranet4"])
        self.assertEqual(updated["bar"]["layout"]["center"], config["bar"]["layout"]["center"])
        self.assertEqual(updated["bar"]["layout"]["right"], config["bar"]["layout"]["right"])

    def test_migration_deduplicates_old_and_new_plugin_ids(self):
        config = {"bar": {"layout": {"center": ["aranet4", {"id": "omarchy.clock"}],
                                     "right": [{"id": "kegan.aranet4"}, {"id": "omarchy.bluetooth"}]}}}
        updated = widget_config(config, ADDRESS, "/example/reading.json")
        self.assertEqual([entry["id"] for entry in updated["bar"]["layout"]["center"]],
                         ["kegan.aranet4", "omarchy.clock"])
        self.assertEqual(updated["bar"]["layout"]["right"], [{"id": "omarchy.bluetooth"}])


class ScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_radio_stops_as_soon_as_reading_arrives(self):
        received = asyncio.Event()

        class Scanner:
            stopped = False

            async def start(self):
                received.set()

            async def stop(self):
                self.stopped = True

        scanner = Scanner()
        await asyncio.wait_for(widget.run_scan(scanner, asyncio.Event(), 15, received), 0.2)
        self.assertTrue(scanner.stopped)

    async def test_missing_sensor_has_bounded_scan(self):
        class Scanner:
            stopped = False

            async def start(self):
                pass

            async def stop(self):
                self.stopped = True

        scanner = Scanner()
        await asyncio.wait_for(widget.run_scan(scanner, asyncio.Event(), 0.01, asyncio.Event()), 0.2)
        self.assertTrue(scanner.stopped)

    async def test_stop_signal_releases_radio(self):
        stop = asyncio.Event()

        class Scanner:
            stopped = False

            async def start(self):
                stop.set()

            async def stop(self):
                self.stopped = True

        scanner = Scanner()
        await asyncio.wait_for(widget.run_scan(scanner, stop, None), 0.2)
        self.assertTrue(scanner.stopped)


if __name__ == "__main__":
    unittest.main()
