import unittest

from helpers import frame, handshake, heartbeat, live, report, wifi
from keepalive.tracker import Tracker, iso

T0 = 1_800_000_000.0


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.t = Tracker(idle_timeout_s=15 * 60, clock=lambda: T0)

    def feed(self, sid, frame, t, reply="ack"):
        return self.t.frame(sid, frame, t, reply)

    def test_login_and_accepted_data(self):
        self.t.opened(1)
        self.assertEqual(self.feed(1, handshake(up=34), T0), ["logger logged in"])
        self.feed(1, live(38), T0 + 4)
        self.feed(1, live(376), T0 + 342)       # next reading 5.6 min later: the reply was accepted
        snap = self.t.snapshot(T0 + 400)
        self.assertEqual(snap["totals"]["not_accepted"], 0)
        self.assertEqual(snap["totals"]["live"], 2)
        self.assertEqual(snap["totals"]["logins"], 1)
        self.assertTrue(snap["connected"])

    def test_data_sent_again_is_not_accepted(self):
        self.t.opened(1)
        self.feed(1, live(1000), T0)
        msgs = self.feed(1, live(1034), T0 + 34)
        self.assertTrue(any("not accepted" in m for m in msgs))
        self.assertEqual(self.t.totals["not_accepted"], 1)
        self.assertEqual(self.t.last_hour(T0 + 40)["not_accepted_pct"], 50.0)

    def test_close_after_data_is_not_accepted(self):
        self.t.opened(1)
        self.feed(1, live(1000), T0)
        msgs = self.t.closed(1, T0 + 35, "logger")
        self.assertTrue(any("not accepted" in m for m in msgs))
        self.assertEqual(self.t.totals["not_accepted"], 1)
        self.assertEqual(self.t.totals["closed_by_logger"], 1)

    def test_idle_close_is_not_the_logger(self):
        self.t.opened(1)
        self.feed(1, live(1000), T0)
        self.assertEqual(self.t.closed(1, T0 + 30, "idle"), [])
        self.assertEqual(self.t.totals["not_accepted"], 0)
        self.assertEqual(self.t.totals["idle_closes"], 1)

    def test_other_frame_after_data_means_accepted(self):
        self.t.opened(1)
        self.feed(1, live(1000), T0)
        self.feed(1, live(990, flag=0x81), T0 + 34)     # stored data: the logger moved on
        self.feed(1, live(1340), T0 + 340)
        self.assertEqual(self.t.totals["not_accepted"], 0)

    def test_restart_detection(self):
        self.t.opened(1)
        self.feed(1, live(5000), T0)
        self.feed(1, report(5338 + 5), T0 + 338)        # 5 s of drift: same boot
        self.assertEqual(self.t.totals["restarts"], 0)
        self.t.opened(2)
        msgs = self.feed(2, handshake(up=34), T0 + 400)
        self.assertEqual(self.t.totals["restarts"], 1)
        self.assertIn("logger restarted 34 s ago", msgs)
        self.assertEqual(self.t.snapshot(T0 + 400)["logger_uptime_s"], 34)

    def test_reconnect_and_empty_sessions(self):
        self.t.opened(1)
        self.t.closed(1, T0 + 1, "logger")       # opened and closed at once: not counted
        self.t.opened(2)
        self.assertEqual(self.feed(2, live(100), T0 + 5), ["logger reconnected (no new login)"])
        self.feed(2, wifi(), T0 + 6)
        self.assertEqual(self.t.totals["reconnects"], 1)
        self.assertEqual(self.t.totals["closed_by_logger"], 0)

    def test_unknown_frames_and_acks(self):
        self.t.opened(1)
        self.feed(1, heartbeat(), T0)
        self.feed(1, heartbeat(), T0 + 1, reply="unknown")
        self.assertEqual((self.t.totals["acks"], self.t.totals["unknown"]), (1, 1))

    def test_hour_window_and_connected(self):
        self.t.opened(1)
        self.feed(1, live(10), T0)
        self.assertEqual(self.t.last_hour(T0 + 3599)["live_data"], 1)
        self.assertEqual(self.t.last_hour(T0 + 3601)["live_data"], 0)
        self.assertIsNone(self.t.last_hour(T0 + 3601)["not_accepted_pct"])
        self.assertTrue(self.t.connected(T0 + 60))
        self.assertFalse(self.t.connected(T0 + 16 * 60), "no frame for longer than the idle timeout")
        self.t.closed(1, T0 + 61, "logger")
        self.assertFalse(self.t.connected(T0 + 62))

    def test_second_logger_warns_once(self):
        self.t.opened(1)
        self.feed(1, heartbeat(), T0)
        self.assertEqual(self.t.warnings, [])
        other = frame(0x47, b"\x00", sn=0x11111111)
        self.feed(1, other, T0 + 1)
        self.feed(1, other, T0 + 2)
        self.assertEqual(len(self.t.warnings), 1, "once per new logger")
        self.assertIn("2 different loggers", self.t.warnings[0])
        self.assertNotIn(str(0x11111111), self.t.warnings[0], "no serial numbers above debug")
        self.assertEqual(self.t.snapshot(T0 + 3)["loggers"], 2)

    def test_server_closes_do_not_count_as_the_logger(self):
        for closer in ("idle", "evicted", "stop"):
            self.t.opened(1)
            self.feed(1, live(1000), T0)
            self.assertEqual(self.t.closed(1, T0 + 30, closer), [], closer)
        self.assertEqual(self.t.totals["not_accepted"], 0)
        self.assertEqual((self.t.totals["idle_closes"], self.t.totals["evicted"]), (1, 1))
        self.assertEqual(self.t.totals["closed_by_logger"], 0)

    def test_held_and_rejected(self):
        self.t.opened(1)
        self.feed(1, heartbeat(), T0, reply="held")
        self.t.rejected(T0 + 1)
        self.t.rejected(T0 + 2)
        snap = self.t.snapshot(T0 + 3)
        self.assertEqual((snap["totals"]["held"], snap["totals"]["acks"]), (1, 0))
        self.assertEqual((snap["totals"]["rejected"], snap["last_hour"]["rejected"]), (2, 2))
        self.assertIn("2 connection(s) rejected", self.t.summary(T0 + 3))

    def test_timestamps_follow_the_wall_clock_intervals_do_not(self):
        """Intervals use the monotonic clock: a jump of the system clock changes only how
        the timestamps are shown, not ages, windows or restarts."""
        self.t.opened(1)
        self.feed(1, live(1000), T0)
        wall = 1_790_000_000.0
        snap = self.t.snapshot(T0 + 10, wall)
        self.assertEqual(snap["last_frame"], iso(wall - 10))
        self.assertEqual(snap["logger_boot"], iso(wall - 1010))
        jumped = self.t.snapshot(T0 + 10, wall + 7200)          # the system clock moved 2 h
        self.assertEqual(jumped["last_frame_age_s"], 10)
        self.assertEqual(jumped["logger_uptime_s"], 1010)
        self.assertTrue(jumped["connected"])
        self.assertEqual(jumped["totals"]["restarts"], 0)

    def test_summary(self):
        self.t.opened(1)
        self.feed(1, handshake(), T0)
        s = self.t.summary(T0 + 10)
        self.assertIn("logger connected", s)
        self.assertIn("1 login(s)", s)


if __name__ == "__main__":
    unittest.main()
