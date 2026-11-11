import asyncio
import json
import time
import unittest
from unittest import mock

from helpers import FakeLogger, ack_time, config, frame, handshake, heartbeat, http_get, live, report, valid_ack
from keepalive import __version__
from keepalive.config import TimeSource
from keepalive.server import CLOCK_MIN, KeepaliveServer, log


class ServerTest(unittest.IsolatedAsyncioTestCase):
    async def start(self, clock=time.time, **kw) -> KeepaliveServer:
        srv = KeepaliveServer(config(**kw), TimeSource("utc"), clock=clock)
        await srv.start()
        self.addAsyncCleanup(srv.stop)
        return srv

    async def logger(self, srv) -> FakeLogger:
        lg = await FakeLogger().connect(srv.port)
        self.addAsyncCleanup(lg.close)
        return lg

    async def test_replies_to_each_frame(self):
        srv = await self.start()
        lg = await self.logger(srv)
        for req, reply_type in ((handshake(), 0x11), (live(40), 0x12), (heartbeat(), 0x17), (report(41), 0x18)):
            await lg.send(req)
            a = await lg.reply()
            self.assertTrue(valid_ack(a))
            self.assertEqual(a[4], reply_type)
            self.assertEqual(a[5], (req[5] + 1) & 0xFF)
            self.assertLessEqual(abs(ack_time(a) - time.time()), 2)

    async def test_unknown_frame_gets_no_reply(self):
        srv = await self.start()
        lg = await self.logger(srv)
        await lg.send(frame(0x49, b"\x00"))
        self.assertTrue(await lg.no_reply())
        await lg.send(heartbeat())
        self.assertTrue(valid_ack(await lg.reply()), "the connection still works")

    async def test_sends_only_replies(self):
        """Whatever the logger sends, the server writes nothing but one reply per known frame."""
        srv = await self.start()
        lg = await self.logger(srv)
        stream = (b"junk" + handshake() + frame(0x49, b"\x00") + live(40) + b"\xa5\xff\xff"
                  + heartbeat() + frame(0x47, b"\x00", ctl=0x20) + report(41))
        for i in range(0, len(stream), 7):
            await lg.send(stream[i:i + 7])
        received = b""
        while True:
            try:
                received += await asyncio.wait_for(lg.reader.read(1000), 0.5)
            except asyncio.TimeoutError:
                break
        self.assertEqual(len(received), 4 * 23, "handshake, live, heartbeat, report")
        acks = [received[i:i + 23] for i in range(0, len(received), 23)]
        self.assertTrue(all(valid_ack(a) for a in acks))
        self.assertEqual([a[4] for a in acks], [0x11, 0x12, 0x17, 0x18])

    async def test_allowed_ips(self):
        srv = await self.start(allowed_ips=["192.0.2.10"])
        lg = await self.logger(srv)
        self.assertTrue(await lg.closed_by_server())
        self.assertEqual(srv.tracker.totals["rejected"], 1)

        srv2 = await self.start(allowed_ips=["127.0.0.1"])
        lg2 = await self.logger(srv2)
        await lg2.send(heartbeat())
        self.assertTrue(valid_ack(await lg2.reply()))

    async def test_rejected_logged_once_per_address(self):
        srv = await self.start(allowed_ips=["192.0.2.10"])
        with self.assertLogs(log, "INFO") as logs:
            for _ in range(3):
                lg = await FakeLogger().connect(srv.port)
                self.assertTrue(await lg.closed_by_server())
                await lg.close()
        self.assertEqual(sum("rejected a connection" in line for line in logs.output), 1)
        self.assertEqual(srv.tracker.totals["rejected"], 3)
        self.assertEqual(srv.status()["last_hour"]["rejected"], 3)

    async def test_no_reply_while_the_clock_is_not_set(self):
        now = [CLOCK_MIN - 86400]
        srv = await self.start(clock=lambda: now[0])
        lg = await self.logger(srv)
        with self.assertLogs(log, "INFO") as logs:
            for _ in range(2):
                await lg.send(heartbeat())
                self.assertTrue(await lg.no_reply())
            self.assertFalse(srv.status()["clock_ok"])
            now[0] = time.time()                            # time sync
            await lg.send(heartbeat())
            reply = await lg.reply()
        self.assertTrue(valid_ack(reply))
        self.assertLessEqual(abs(ack_time(reply) - time.time()), 2)
        self.assertEqual(sum("system clock says" in line for line in logs.output), 1, "warned once")
        self.assertTrue(any("answering the logger again" in line for line in logs.output))
        self.assertEqual((srv.tracker.totals["held"], srv.tracker.totals["acks"]), (2, 1))
        self.assertTrue(srv.status()["clock_ok"])

    async def test_full_house_evicts_the_longest_silent(self):
        with mock.patch("keepalive.server.MAX_SESSIONS", 2):
            srv = await self.start()
            silent = await self.logger(srv)                 # sends nothing
            await asyncio.sleep(0.05)
            active = await self.logger(srv)
            await active.send(heartbeat())
            await active.reply()
            new = await self.logger(srv)                    # the house is full
            await new.send(heartbeat())
            self.assertTrue(valid_ack(await new.reply()), "the new connection is served")
            self.assertTrue(await silent.closed_by_server(), "the longest-silent one made room")
            await active.send(heartbeat())
            self.assertTrue(valid_ack(await active.reply()), "the active one stays")
            await asyncio.sleep(0.05)
            self.assertEqual(srv.tracker.totals["evicted"], 1)
            self.assertEqual(srv.tracker.totals["rejected"], 0)

    async def test_status_deadline_and_limit(self):
        with mock.patch("keepalive.status.DEADLINE", 0.5), mock.patch("keepalive.status.MAX_CONNECTIONS", 2):
            srv = await self.start()
            slow = []
            for _ in range(2):
                r, w = await asyncio.open_connection("127.0.0.1", srv.status_port)
                w.write(b"GET /status HTTP/1.1\r\n")
                await w.drain()
                slow.append((r, w))
            await asyncio.sleep(0.05)
            r3, w3 = await asyncio.open_connection("127.0.0.1", srv.status_port)
            self.assertEqual(await asyncio.wait_for(r3.read(), 1), b"", "over the limit: closed at once")
            w3.close()

            async def drip(r, w):
                """A header line every 0.1 s, never the blank line: returns when the server closes."""
                start = time.monotonic()
                while time.monotonic() - start < 3:
                    try:
                        w.write(b"X-Slow: 1\r\n")
                        await w.drain()
                        if await asyncio.wait_for(r.read(100), 0.1) == b"":
                            return time.monotonic() - start
                    except asyncio.TimeoutError:
                        continue
                    except ConnectionError:
                        return time.monotonic() - start
                return None

            for took in await asyncio.gather(*(drip(r, w) for r, w in slow)):
                self.assertIsNotNone(took, "a whole request has a deadline, not each line")
                self.assertLess(took, 1.5)
            for _, w in slow:
                w.close()
            self.assertEqual(await http_get(srv.status_port, "/health"), (200, b"ok\n"), "free again")

    async def test_idle_timeout(self):
        srv = await self.start(idle_timeout=0.01)       # 0.6 s
        lg = await self.logger(srv)
        await lg.send(heartbeat())
        await lg.reply()
        self.assertTrue(await lg.closed_by_server(2))
        await asyncio.sleep(0.1)
        self.assertEqual(srv.tracker.totals["idle_closes"], 1)

    async def test_no_idle_timeout(self):
        srv = await self.start(idle_timeout=0)
        lg = await self.logger(srv)
        await lg.send(heartbeat())
        await lg.reply()
        self.assertTrue(await lg.no_reply(1.0))
        await lg.send(heartbeat())
        self.assertTrue(valid_ack(await lg.reply()), "still open after 1 s")

    async def test_reconnect_while_old_connection_is_open(self):
        srv = await self.start()
        old = await self.logger(srv)
        await old.send(live(5000))
        await old.reply()
        new = await self.logger(srv)                     # the logger rebooted; the old socket lingers
        await new.send(handshake(up=34))
        self.assertTrue(valid_ack(await new.reply()))
        status = srv.status()
        self.assertEqual(status["sessions_open"], 2)
        self.assertEqual(status["totals"]["restarts"], 1)

    async def test_second_logger_answered_and_warned(self):
        srv = await self.start()
        lg = await self.logger(srv)
        with self.assertLogs(log, "WARNING") as logs:
            await lg.send(heartbeat())
            await lg.reply()
            await lg.send(frame(0x47, b"\x00", sn=0x11111111))
            self.assertTrue(valid_ack(await lg.reply()), "the other logger still gets its reply")
        self.assertIn("different loggers", logs.output[0])
        self.assertEqual(srv.status()["loggers"], 2)

    async def test_status_endpoint(self):
        srv = await self.start()
        lg = await self.logger(srv)
        await lg.send(handshake())
        await lg.reply()
        code, body = await http_get(srv.status_port, "/status")
        self.assertEqual(code, 200)
        s = json.loads(body)
        self.assertTrue(s["connected"])
        self.assertEqual(s["version"], __version__)
        self.assertEqual(s["time_mode"], "utc")
        self.assertEqual(s["totals"]["logins"], 1)
        self.assertNotIn("305419896", body.decode(), "no serial number in the status")
        self.assertNotIn("127.0.0.1", body.decode(), "no addresses in the status")
        self.assertEqual(await http_get(srv.status_port, "/health"), (200, b"ok\n"))
        self.assertEqual((await http_get(srv.status_port, "/nope"))[0], 404)
        self.assertEqual((await http_get(srv.status_port, "/status", "POST"))[0], 405)

    async def test_status_survives_garbage(self):
        srv = await self.start()
        r, w = await asyncio.open_connection("127.0.0.1", srv.status_port)
        w.write(b"G" * 10000 + b"\r\n")
        await w.drain()
        try:
            self.assertEqual(await asyncio.wait_for(r.read(), 5), b"")
        except ConnectionError:
            pass                        # closed with unread data: a reset is fine too
        w.close()
        self.assertEqual(await http_get(srv.status_port, "/health"), (200, b"ok\n"))

    async def test_stop_with_open_connection(self):
        srv = KeepaliveServer(config(), TimeSource("utc"))
        await srv.start()
        lg = await FakeLogger().connect(srv.port)
        await lg.send(heartbeat())
        await lg.reply()
        await asyncio.wait_for(srv.stop(), 3)
        self.assertTrue(await lg.closed_by_server())
        await lg.close()


if __name__ == "__main__":
    unittest.main()
