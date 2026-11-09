import unittest
from dataclasses import FrozenInstanceError

from adaptive_policy import Context, Schedule, absent_interval, pause_reason, present_interval


class AdaptivePolicyTests(unittest.TestCase):
    def test_context_is_immutable(self):
        context = Context()
        with self.assertRaises(FrozenInstanceError):
            context.sleeping = True

    def test_present_cadence_respects_power(self):
        self.assertEqual(present_interval(Context()), 300)
        self.assertEqual(present_interval(Context(on_battery=True)), 600)
        self.assertEqual(present_interval(Context(power_saver=True)), 900)
        self.assertEqual(present_interval(Context(on_battery=True, power_saver=True)), 900)

    def test_absence_backs_off_and_caps_at_one_hour(self):
        self.assertEqual([absent_interval(n) for n in range(1, 6)], [900, 1800, 3600, 3600, 3600])
        self.assertEqual(absent_interval(1000), 3600)
        self.assertEqual(absent_interval(0), 900)
        self.assertEqual(absent_interval(-1), 900)

    def test_pause_reasons_and_priority(self):
        for field, reason in (("sleeping", "sleep"), ("locked", "locked"),
                              ("inactive", "inactive"), ("lid_closed", "lid"),
                              ("idle", "idle")):
            with self.subTest(field=field):
                self.assertEqual(pause_reason(Context(**{field: True})), reason)
        self.assertEqual(pause_reason(Context(sleeping=True, locked=True, lid_closed=True)), "sleep")
        self.assertEqual(pause_reason(Context(locked=True, inactive=True, idle=True)), "locked")
        self.assertEqual(pause_reason(Context()), "")

    def test_manual_scan_can_override_idle_or_closed_lid(self):
        self.assertEqual(pause_reason(Context(idle=True), manual=True), "")
        self.assertEqual(pause_reason(Context(lid_closed=True), manual=True), "")
        self.assertEqual(pause_reason(Context(idle=True, lid_closed=True), manual=True), "")

    def test_manual_scan_cannot_override_sleep_lock_or_inactive(self):
        self.assertEqual(pause_reason(Context(sleeping=True), manual=True), "sleep")
        self.assertEqual(pause_reason(Context(locked=True), manual=True), "locked")
        self.assertEqual(pause_reason(Context(inactive=True), manual=True), "inactive")

    def test_startup_is_unknown_and_due_immediately(self):
        schedule = Schedule()
        self.assertFalse(schedule.present)
        self.assertEqual(schedule.next_scan, 0)
        self.assertEqual(schedule.expires, 0)

    def test_success_schedules_normal_refresh_with_presence_grace(self):
        schedule = Schedule()
        schedule.record_result(True, 100, Context())
        self.assertTrue(schedule.present)
        self.assertEqual(schedule.last_attempt, 100)
        self.assertEqual(schedule.last_seen, 100)
        self.assertEqual(schedule.next_scan, 400)
        self.assertEqual(schedule.expires, 490)

    def test_absence_hides_immediately_and_keeps_last_measurement_time(self):
        schedule = Schedule()
        schedule.record_result(True, 100, Context())
        schedule.record_result(False, 410, Context())
        self.assertFalse(schedule.present)
        self.assertEqual(schedule.last_attempt, 410)
        self.assertEqual(schedule.last_seen, 100)
        self.assertEqual(schedule.expires, 410)
        self.assertEqual(schedule.next_scan, 1310)
        schedule.record_result(False, 1320, Context())
        self.assertEqual(schedule.next_scan, 3120)
        schedule.record_result(False, 3130, Context())
        self.assertEqual(schedule.next_scan, 6730)

    def test_success_resets_absence_backoff(self):
        schedule = Schedule()
        for now in (100, 1100, 3100, 7100):
            schedule.record_result(False, now, Context())
        schedule.record_result(True, 10800, Context(on_battery=True))
        self.assertEqual(schedule.misses, 0)
        self.assertTrue(schedule.present)
        self.assertEqual(schedule.next_scan, 11400)
        schedule.record_result(False, 11500, Context())
        self.assertEqual(schedule.misses, 1)
        self.assertEqual(schedule.next_scan, 12400)

    def test_panel_request_keeps_known_presence_and_history(self):
        schedule = Schedule()
        schedule.record_result(True, 100, Context())
        schedule.request_check(150)
        self.assertEqual(schedule.next_scan, 150)
        self.assertTrue(schedule.present)
        self.assertEqual(schedule.last_seen, 100)
        self.assertEqual(schedule.last_attempt, 100)
        self.assertEqual(schedule.expires, 490)

    def test_wake_check_invalidates_presence_and_resets_backoff(self):
        schedule = Schedule()
        schedule.record_result(True, 100, Context())
        schedule.record_result(False, 410, Context())
        schedule.record_result(False, 1400, Context())
        schedule.request_check(2000, invalidate=True)
        self.assertEqual(schedule.next_scan, 2000)
        self.assertFalse(schedule.present)
        self.assertEqual(schedule.expires, 2000)
        self.assertEqual(schedule.misses, 0)
        self.assertEqual(schedule.last_seen, 100)
        schedule.record_result(False, 2045, Context())
        self.assertEqual(schedule.next_scan, 2945)

    def test_power_changes_can_extend_or_shorten_normal_refresh(self):
        schedule = Schedule()
        schedule.record_result(True, 100, Context())
        schedule.adjust_power(150, Context(on_battery=True))
        self.assertEqual(schedule.next_scan, 700)
        self.assertEqual(schedule.expires, 790)
        schedule.adjust_power(160, Context(power_saver=True))
        self.assertEqual(schedule.next_scan, 1000)
        schedule.adjust_power(200, Context())
        self.assertEqual(schedule.next_scan, 400)
        self.assertEqual(schedule.expires, 490)

    def test_overdue_refresh_runs_now_after_power_change(self):
        schedule = Schedule()
        schedule.record_result(True, 100, Context(power_saver=True))
        schedule.adjust_power(600, Context())
        self.assertEqual(schedule.next_scan, 600)
        self.assertEqual(schedule.expires, 690)

    def test_power_change_does_not_reset_absent_device_backoff(self):
        schedule = Schedule()
        schedule.record_result(False, 100, Context())
        expected = (schedule.next_scan, schedule.expires, schedule.misses)
        schedule.adjust_power(150, Context(power_saver=True))
        self.assertEqual((schedule.next_scan, schedule.expires, schedule.misses), expected)


if __name__ == "__main__":
    unittest.main()
