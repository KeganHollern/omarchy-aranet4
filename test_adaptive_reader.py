import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import adaptive_reader as runtime
from adaptive_policy import Context


ADDRESS = "AA:BB:CC:DD:EE:FF"
BOOT = "test-boot"


def control_value(**updates):
    value = {"schema": 1, "address": ADDRESS, "boot_id": BOOT,
             "idle": False, "panel_open": False, "request": 0}
    value.update(updates)
    return value


class ControlParsingTests(unittest.TestCase):
    def test_valid_control(self):
        parsed = runtime.parse_control(control_value(panel_open=True, request=1760000000000), ADDRESS, BOOT)
        self.assertEqual(parsed, runtime.Control(False, True, 1760000000000))

    def test_unrelated_or_malformed_control_is_ignored(self):
        invalid = [None, [], "text", {}, control_value(schema="1"), control_value(schema=True),
                   control_value(address="11:22:33:44:55:66"), control_value(boot_id="previous-boot"),
                   control_value(idle=1), control_value(panel_open="true"),
                   control_value(request=True), control_value(request="now"),
                   control_value(request=-1), control_value(request=float("nan")),
                   control_value(request=float("inf")), control_value(request=1e16),
                   control_value(request=10 ** 10000)]
        for index, value in enumerate(invalid):
            with self.subTest(case=index):
                self.assertIsNone(runtime.parse_control(value, ADDRESS, BOOT))


class ReaderFixture:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = self.enterContext(patch.object(runtime, "boottime", return_value=100.0))
        self.enterContext(patch.object(runtime, "boot_id", return_value=BOOT))
        self.events = []
        self.reader = runtime.AdaptiveReader(ADDRESS, self.directory.name, emit=self.events.append)

    def write_control(self, **updates):
        self.reader.on_control(control_value(**updates))

    def latest_status(self):
        return next(event["status"] for event in reversed(self.events) if event["type"] == "status")


class ReaderTransitionsTests(ReaderFixture, unittest.TestCase):
    def test_panel_open_requests_refresh_without_waiting_for_background_deadline(self):
        self.reader.schedule.next_scan = 10000
        self.write_control(panel_open=True, request=1)
        self.assertTrue(self.reader.manual_requested)
        self.assertTrue(self.reader.control.panel_open)
        self.assertTrue(self.reader.wakeup.is_set())

    def test_closing_panel_cancels_its_scan_and_queued_request(self):
        self.reader.control = runtime.Control(panel_open=True, request=1)
        self.reader.refreshing = self.reader.scan_manual = self.reader.manual_requested = True
        self.write_control(panel_open=False, request=1)
        self.assertTrue(self.reader.scan_cancel.is_set())
        self.assertFalse(self.reader.manual_requested)

    def test_replayed_unchanged_open_control_does_not_queue_another_scan(self):
        self.reader.control = runtime.Control(panel_open=True, request=1)
        self.write_control(panel_open=True, request=1)
        self.assertFalse(self.reader.manual_requested)
        self.write_control(panel_open=True, request=2)
        self.assertTrue(self.reader.manual_requested)

    def test_background_scans_cancel_for_every_away_state(self):
        for field in ("sleeping", "locked", "inactive", "lid_closed", "idle"):
            with self.subTest(field=field):
                self.reader.power = Context()
                self.reader.scan_cancel.clear()
                self.reader.refreshing = True
                self.reader.scan_manual = False
                self.reader.on_power(Context(**{field: True}))
                self.assertTrue(self.reader.scan_cancel.is_set())

    def test_native_ui_idle_event_cancels_background_scan(self):
        self.reader.refreshing = True
        self.write_control(idle=True)
        self.assertTrue(self.reader.scan_cancel.is_set())
        self.assertEqual(self.latest_status()["pause_reason"], "idle")

    def test_sleep_lock_and_inactive_cancel_even_manual_scans(self):
        self.reader.control = runtime.Control(panel_open=True)
        for field in ("sleeping", "locked", "inactive"):
            with self.subTest(field=field):
                self.reader.power = Context()
                self.reader.scan_cancel.clear()
                self.reader.refreshing = self.reader.scan_manual = True
                self.reader.on_power(Context(**{field: True}))
                self.assertTrue(self.reader.scan_cancel.is_set())

    def test_manual_panel_may_read_when_docked_with_lid_closed(self):
        self.reader.control = runtime.Control(panel_open=True)
        self.reader.refreshing = self.reader.scan_manual = True
        self.reader.on_power(Context(lid_closed=True, idle=True))
        self.assertFalse(self.reader.scan_cancel.is_set())
        self.assertEqual(self.latest_status()["pause_reason"], "")

    def test_return_from_away_invalidates_old_presence_and_requests_immediate_scan(self):
        for field in ("sleeping", "locked", "inactive", "lid_closed", "idle"):
            with self.subTest(field=field):
                self.reader.schedule.record_result(True, 50, Context())
                self.reader.schedule.misses = 2
                self.reader.power = Context(**{field: True})
                self.clock.return_value = 200
                self.reader.on_power(Context())
                self.assertEqual(self.reader.schedule.next_scan, 200)
                self.assertEqual(self.reader.schedule.expires, 200)
                self.assertFalse(self.reader.schedule.present)
                self.assertEqual(self.reader.schedule.misses, 0)
                self.assertTrue(self.reader.wakeup.is_set())

    def test_return_from_native_ui_idle_invalidates_presence(self):
        self.reader.schedule.record_result(True, 50, Context())
        self.reader.control = runtime.Control(idle=True)
        self.clock.return_value = 200
        self.write_control(idle=False)
        self.assertEqual(self.reader.schedule.next_scan, 200)
        self.assertFalse(self.reader.schedule.present)

    def test_power_changes_update_deadline_without_new_poll_loop(self):
        self.reader.schedule.record_result(True, 100, Context())
        self.clock.return_value = 150
        self.reader.on_power(Context(on_battery=True))
        self.assertEqual(self.reader.schedule.next_scan, 700)
        self.assertEqual(self.reader.schedule.expires, 790)
        self.reader.on_power(Context(on_battery=True, power_saver=True))
        self.assertEqual(self.reader.schedule.next_scan, 1000)
        self.reader.on_power(Context())
        self.assertEqual(self.reader.schedule.next_scan, 400)


class PublishedStatusTests(ReaderFixture, unittest.TestCase):
    def test_status_is_emitted_without_disk_control_or_status_files(self):
        self.reader.schedule.record_result(True, 100, Context())
        self.reader.publish()
        self.assertEqual(self.latest_status()["present"], True)
        self.assertEqual(self.latest_status()["next_scan"], 400)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_identical_status_is_not_emitted_repeatedly(self):
        self.reader.publish()
        self.reader.publish()
        self.assertEqual(len(self.events), 1)


class ScanIntegrationTests(ReaderFixture, unittest.IsolatedAsyncioTestCase):
    def fake_bluetooth(self):
        package = ModuleType("aranet4")
        package.__path__ = []
        client = ModuleType("aranet4.client")
        readings = SimpleNamespace(co2=742, temperature=22.4, humidity=48, pressure=1012,
                                   battery=85, ago=210)
        client.Aranet4Advertisement = lambda device, data: SimpleNamespace(readings=readings)
        bleak = ModuleType("bleak")
        bleak.BleakScanner = lambda **kwargs: SimpleNamespace(callback=kwargs["detection_callback"])
        return patch.dict(sys.modules, {"aranet4": package, "aranet4.client": client, "bleak": bleak})

    async def test_request_during_existing_scan_reuses_result_instead_of_queueing_another(self):
        self.reader.scan_mode = "active"

        async def scan(scanner, cancel, duration, received):
            self.write_control(panel_open=True, request=1)
            self.assertTrue(self.reader.manual_requested)
            scanner.callback(SimpleNamespace(address=ADDRESS, name="Aranet4"), object())
            self.assertTrue(received.is_set())

        with self.fake_bluetooth(), patch.object(runtime, "run_scan", side_effect=scan) as run_scan:
            await self.reader.scan(False)
        self.assertEqual(run_scan.call_count, 1)
        self.assertFalse(self.reader.manual_requested)
        self.assertFalse(self.reader.refreshing)
        self.assertTrue(self.reader.schedule.present)
        self.assertEqual(self.reader.schedule.next_scan, 400)
        self.assertEqual(json.loads(self.reader.path.read_text())["co2"], 742)
        snapshots = [event["snapshot"] for event in self.events if event["type"] == "snapshot"]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["co2"], 742)
        self.assertEqual(sorted(p.name for p in Path(self.directory.name).iterdir()), [self.reader.path.name])

    async def test_closing_manual_scan_does_not_record_device_as_missing(self):
        self.reader.scan_mode = "active"
        self.reader.schedule.record_result(True, 50, Context())
        self.reader.control = runtime.Control(panel_open=True, request=1)

        async def scan(scanner, cancel, duration, received):
            self.write_control(panel_open=False, request=1)
            self.assertTrue(cancel.is_set())

        with self.fake_bluetooth(), patch.object(runtime, "run_scan", side_effect=scan):
            await self.reader.scan(True)
        self.assertTrue(self.reader.schedule.present)
        self.assertEqual(self.reader.schedule.misses, 0)
        self.assertFalse(self.reader.manual_requested)
        self.assertGreater(self.reader.schedule.next_scan, self.clock.return_value)

    async def test_reopening_during_canceled_scan_preserves_new_refresh_request(self):
        self.reader.scan_mode = "active"
        self.reader.schedule.record_result(True, 50, Context())
        self.reader.control = runtime.Control(panel_open=True, request=1)

        async def scan(scanner, cancel, duration, received):
            self.write_control(panel_open=False, request=1)
            self.assertTrue(cancel.is_set())
            self.write_control(panel_open=True, request=2)
            self.assertTrue(self.reader.manual_requested)

        with self.fake_bluetooth(), patch.object(runtime, "run_scan", side_effect=scan):
            await self.reader.scan(True)
        self.assertTrue(self.reader.control.panel_open)
        self.assertEqual(self.reader.control.request, 2)
        self.assertTrue(self.reader.manual_requested)
        self.assertTrue(self.reader.schedule.present)
        self.assertEqual(self.reader.schedule.misses, 0)


if __name__ == "__main__":
    unittest.main()
