import os
import tempfile
import unittest

from helpers import FakeLogger, config, frame, handshake, live, wifi  # first: puts tools/ on sys.path
import stats  # noqa: E402
from keepalive.__main__ import setup_logging
from keepalive.config import TimeSource
from keepalive.protocol import describe
from keepalive.server import KeepaliveServer, log


class FrameLogFormatTest(unittest.IsolatedAsyncioTestCase):
    """tools/stats.py reads the frame log the server writes: both sides change together."""

    async def test_server_log_is_readable(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "frames.log")
            setup_logging("info", path)
            for h in list(log.handlers):
                log.removeHandler(h)
            try:
                srv = KeepaliveServer(config(), TimeSource("utc"))
                await srv.start()
                lg = await FakeLogger().connect(srv.port)
                for f in (handshake(), live(40), wifi()):
                    await lg.send(f)
                    await lg.reply()
                await lg.close()
                await srv.stop()
            finally:
                setup_logging("info", None)
                for h in list(log.handlers):
                    log.removeHandler(h)
            with open(path, encoding="utf-8") as f:
                r = stats.analyze(stats.parse(f))
        self.assertEqual(r["frames"], 3)
        self.assertEqual(r["counts"]["ack"], 3)
        self.assertEqual((r["counts"]["open"], r["counts"]["close"]), (1, 1))
        self.assertEqual((r["logins"], r["reconnects"]), (1, 0))
        self.assertEqual(r["counts"]["odd"], 0)


def line(t: str, msg: str) -> str:
    return f"2026-01-15 {t} {msg}\n"


class AnalyzeTest(unittest.TestCase):
    """A made-up hour: login, two replies not accepted, a reconnect."""

    LOG = [
        line("10:00:00", "open  192.0.2.10:5000"),
        line("10:00:04", describe(handshake(up=34))),
        line("10:00:04", "-> 0x11 ack time=1768471204 (2026-01-15 10:00:04 as UTC)"),
        line("10:00:08", describe(live(38, s6=0x10))),
        line("10:05:46", describe(live(376, s6=0x35))),     # +37 responses in 338 s
        line("10:06:20", describe(live(410, s6=0x36))),     # sent again 34 s later: not accepted
        line("10:11:58", describe(live(748, s6=0x5b))),
        line("10:12:31", "close 192.0.2.10:5000 after 12 min 31 s, 6 frame(s)"),   # closed 33 s later
        line("10:12:42", "open  192.0.2.10:5001"),
        line("10:17:36", describe(live(1086, s6=0x80))),    # reconnect without login
        line("10:23:14", describe(live(1424, s6=0xa5))),
    ]

    def test_metrics(self):
        r = stats.analyze(stats.parse(self.LOG))
        self.assertEqual(r["frames"], 7)
        self.assertEqual((r["logins"], r["reconnects"]), (1, 1))
        self.assertEqual(len(r["boots"]), 1)
        self.assertEqual((r["live"], r["not_accepted"]), (5, 2), "the last one is too close to the end")
        self.assertEqual(r["window"]["start"], 12)
        self.assertEqual((r["window"]["in"], r["window"]["out"]), ((1, 1), (1, 4)))
        self.assertAlmostEqual(r["rates"][0], (37 - 1) / 338 * 60, places=2)

    def test_server_close_is_not_the_logger(self):
        log = [
            line("10:00:00", "open  192.0.2.10:5000"),
            line("10:00:08", describe(live(38))),
            line("10:00:30", "close 192.0.2.10:5000 after 0 min 30 s, 1 frame(s) (closed by the server: evicted)"),
            line("10:06:00", "open  192.0.2.10:5001"),
            line("10:06:08", describe(live(398))),
        ]
        self.assertEqual(stats.analyze(stats.parse(log))["not_accepted"], 0)
        logger_close = [x.replace(" (closed by the server: evicted)", "") for x in log]
        self.assertEqual(stats.analyze(stats.parse(logger_close))["not_accepted"], 1)

    def test_several_loggers_warn(self):
        other = line("10:23:20", describe(frame(0x47, b"\x00", sn=0x11111111)))
        self.assertIn("WARNING: frames from 2 loggers", stats.report(stats.analyze(stats.parse(self.LOG + [other]))))
        self.assertNotIn("WARNING", stats.report(stats.analyze(stats.parse(self.LOG))))

    def test_report_and_range(self):
        text = stats.report(stats.analyze(stats.parse(self.LOG)))
        self.assertIn("live data: 5, not accepted 2 (40%)", text)
        self.assertIn("1 login(s), 1 reconnect(s) without login", text)
        import datetime as dt
        r = stats.analyze(stats.parse(self.LOG, start=dt.datetime(2026, 1, 15, 10, 12, 40)))
        self.assertEqual(r["logins"], 0)
        self.assertEqual(stats.report(stats.analyze(stats.parse([]))), "no logger frames in range")


if __name__ == "__main__":
    unittest.main()
