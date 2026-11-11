import datetime as dt
import os
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import helpers  # noqa: F401  (puts the app on sys.path)
from keepalive.config import ConfigError, TimeSource


def utc(*args) -> float:
    return dt.datetime(*args, tzinfo=dt.timezone.utc).timestamp()


def has_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


class TimeSourceTest(unittest.TestCase):
    def test_utc(self):
        ts = TimeSource("utc")
        self.assertEqual(ts.now(1762300000.7), 1762300000)
        self.assertEqual(ts.utc_offset(), "+00:00")

    def test_local_needs_a_zone(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigError) as e:
                TimeSource("local")
        self.assertIn("time zone", str(e.exception))

    def test_unknown_zone(self):
        with self.assertRaises(ConfigError):
            TimeSource("local", "Mars/Olympus_Mons")

    def test_bad_mode(self):
        with self.assertRaises(ConfigError):
            TimeSource("cloud")

    def test_system_zone(self):
        ts = TimeSource("local", "system")
        t = 1762300000
        expected = int(dt.datetime.fromtimestamp(t).astimezone().utcoffset().total_seconds())
        self.assertEqual(ts.now(t), t + expected)

    @unittest.skipUnless(has_zone("Europe/Kyiv"), "no time zone database (Windows without tzdata)")
    def test_zone_from_tz_variable(self):
        with mock.patch.dict(os.environ, {"TZ": "Europe/Kyiv"}):
            ts = TimeSource("local")
        self.assertEqual(ts.zone_name, "Europe/Kyiv")

    @unittest.skipUnless(has_zone("Europe/Kyiv"), "no time zone database (Windows without tzdata)")
    def test_offsets_and_dst(self):
        ts = TimeSource("local", "Europe/Kyiv")
        cases = [
            (utc(2026, 1, 15, 12), 2 * 3600),
            (utc(2026, 7, 15, 12), 3 * 3600),
            (utc(2026, 3, 29, 0, 59, 59), 2 * 3600),    # summer time starts 01:00 UTC
            (utc(2026, 3, 29, 1, 0, 0), 3 * 3600),
            (utc(2026, 10, 25, 0, 59, 59), 3 * 3600),   # and ends 01:00 UTC
            (utc(2026, 10, 25, 1, 0, 0), 2 * 3600),
        ]
        for t, offset in cases:
            self.assertEqual(ts.now(t), int(t) + offset, dt.datetime.fromtimestamp(t, dt.timezone.utc))
        self.assertIn("UTC+02:00", ts.describe(utc(2026, 1, 15, 12)))
        self.assertIn("now 2026-01-15 14:00:00", ts.describe(utc(2026, 1, 15, 12)))


if __name__ == "__main__":
    unittest.main()
