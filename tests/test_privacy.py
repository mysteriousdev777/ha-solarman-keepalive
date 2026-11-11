"""The normal (info) log may be shown to others: no serial number, no addresses, no network
name. They appear only in the debug log and the frame log."""
import io
import logging
import unittest

from helpers import FakeLogger, config, handshake, live, wifi
from keepalive.__main__ import setup_logging
from keepalive.config import TimeSource
from keepalive.server import KeepaliveServer, log

PRIVATE = ("305419896", "127.0.0.1", "192.0.2.77", "TESTNET1")


class PrivacyTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        for h in list(log.handlers):
            log.removeHandler(h)
        log.setLevel(logging.NOTSET)

    async def session_log(self, level: str) -> str:
        setup_logging(level, None)
        for h in list(log.handlers):          # keep the test output quiet
            log.removeHandler(h)
        out = io.StringIO()
        handler = logging.StreamHandler(out)
        log.addHandler(handler)
        self.addCleanup(log.removeHandler, handler)
        cfg = config(allowed_ips=["127.0.0.1"])
        srv = KeepaliveServer(cfg, TimeSource("utc"))
        await srv.start()
        lg = await FakeLogger().connect(srv.port)
        for f in (handshake(), live(40), wifi()):
            await lg.send(f)
            await lg.reply()
        await lg.close()
        await srv.stop()
        log.info(srv.tracker.summary(0))
        return out.getvalue()

    async def test_info_log_has_no_private_data(self):
        text = await self.session_log("info")
        self.assertIn("logger logged in", text)
        for s in PRIVATE:
            self.assertNotIn(s, text)

    async def test_debug_log_has_details(self):
        text = await self.session_log("debug")
        for s in PRIVATE:
            self.assertIn(s, text, "the check above would notice a leak")


if __name__ == "__main__":
    unittest.main()
