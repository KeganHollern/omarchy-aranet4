import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import adaptive_reader as runtime


ADDRESS = "AA:BB:CC:DD:EE:FF"
BOOT = "test-boot"


def control(**changes):
    message = {"type": "control", "schema": 1, "address": ADDRESS, "boot_id": BOOT,
               "idle": False, "panel_open": True, "request": 1}
    message.update(changes)
    return message


def line(message):
    return json.dumps(message).encode() + b"\n"


class WorkerStdioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.events = []
        self.enterContext(patch.object(runtime, "boot_id", return_value=BOOT))
        self.enterContext(patch.object(runtime, "boottime", return_value=100.0))
        self.reader = runtime.AdaptiveReader(ADDRESS, directory.name, emit=self.events.append)

    async def consume(self, payload):
        stream = asyncio.StreamReader()
        stream.feed_data(payload)
        stream.feed_eof()
        await asyncio.wait_for(runtime.consume_commands(self.reader, stream), 1)

    async def test_eof_stops_worker_and_active_scan(self):
        await self.consume(b"")
        self.assertTrue(self.reader.stop.is_set())
        self.assertTrue(self.reader.scan_cancel.is_set())
        self.assertTrue(self.reader.wakeup.is_set())

    async def test_explicit_stop_exits_without_waiting_for_eof(self):
        stream = asyncio.StreamReader()
        stream.feed_data(line({"type": "stop"}))
        await asyncio.wait_for(runtime.consume_commands(self.reader, stream), 1)
        self.assertTrue(self.reader.stop.is_set())

    async def test_control_message_requests_fresh_reading(self):
        self.reader.schedule.next_scan = 10000
        await self.consume(line(control()))
        self.assertTrue(self.reader.manual_requested)
        self.assertTrue(self.reader.control.panel_open)
        self.assertEqual(self.reader.control.request, 1)
        self.assertFalse(any(self.directory.iterdir()))

    async def test_bad_json_unknown_messages_and_blank_lines_do_not_discard_next_control(self):
        await self.consume(b"not-json\n\n" + line({"type": "unknown"}) + b"[]\n"
                           + line(control(request=42)))
        self.assertEqual(self.reader.control.request, 42)
        self.assertTrue(self.reader.manual_requested)

    async def test_foreign_or_invalid_control_is_ignored(self):
        await self.consume(line(control(address="11:22:33:44:55:66"))
                           + line(control(boot_id="old-boot"))
                           + line(control(request="bad")))
        self.assertEqual(self.reader.control.request, 0)
        self.assertFalse(self.reader.control.panel_open)

    async def test_many_small_frames_are_not_treated_as_one_oversized_message(self):
        messages = b"".join(line(control(request=index)) for index in range(1, 151))
        self.assertGreater(len(messages), 16 * 1024)
        await self.consume(messages)
        self.assertEqual(self.reader.control.request, 150)

    async def test_fragmented_json_lines_are_reassembled(self):
        stream = asyncio.StreamReader()
        task = asyncio.create_task(runtime.consume_commands(self.reader, stream))
        payload = line(control(request=7))
        stream.feed_data(payload[:11])
        await asyncio.sleep(0)
        self.assertFalse(self.reader.manual_requested)
        stream.feed_data(payload[11:])
        stream.feed_eof()
        await asyncio.wait_for(task, 1)
        self.assertEqual(self.reader.control.request, 7)

    async def test_oversized_unterminated_frame_stops_without_waiting_for_newline(self):
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * (16 * 1024 + 1))
        await asyncio.wait_for(runtime.consume_commands(self.reader, stream), 1)
        self.assertTrue(self.reader.stop.is_set())
        self.assertEqual(self.reader.control.request, 0)

    async def test_oversized_frame_does_not_execute_following_commands(self):
        await self.consume(b"x" * (16 * 1024 + 1) + b"\n" + line(control(request=99)))
        self.assertTrue(self.reader.stop.is_set())
        self.assertEqual(self.reader.control.request, 0)

    async def test_invalid_utf8_frame_does_not_crash_worker(self):
        await self.consume(b"\xff\xfe\n" + line(control(request=8)))
        self.assertEqual(self.reader.control.request, 8)

    async def test_excessively_nested_json_does_not_crash_worker(self):
        deeply_nested = b"[" * 2000 + b"0" + b"]" * 2000 + b"\n"
        await self.consume(deeply_nested + line(control(request=9)))
        self.assertEqual(self.reader.control.request, 9)


class WorkerOutputTests(unittest.TestCase):
    def test_output_is_utf8_json_line(self):
        output = io.BytesIO()
        event = {"type": "status", "status": {"text": "CO₂"}}
        with patch.object(runtime.sys, "stdout", SimpleNamespace(buffer=output)):
            runtime.emit_message(event)
        raw = output.getvalue()
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertEqual(json.loads(raw), event)
        self.assertIn("CO₂".encode(), raw)

    def test_oversized_output_is_rejected_before_writing(self):
        output = io.BytesIO()
        with patch.object(runtime.sys, "stdout", SimpleNamespace(buffer=output)):
            with self.assertRaises(ValueError):
                runtime.emit_message({"type": "status", "text": "x" * (16 * 1024)})
        self.assertEqual(output.getvalue(), b"")

    def test_lost_owner_stops_reader(self):
        def disconnected(event):
            raise BrokenPipeError()
        with tempfile.TemporaryDirectory() as directory:
            reader = runtime.AdaptiveReader(ADDRESS, directory, emit=disconnected)
            reader.send({"type": "status", "status": {}})
            self.assertTrue(reader.stop.is_set())
            self.assertTrue(reader.scan_cancel.is_set())


if __name__ == "__main__":
    unittest.main()
