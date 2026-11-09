"""Smoke test of the built app image (run by CI against a started container).

The container runs with TZ set, like under the Supervisor, the logger port published on
localhost:--port and the status port on localhost:--status-port. A fake logger logs in and
sends data; the replies must carry the local time of --tz.

Usage: python tests/smoke_container.py --tz Europe/Kyiv [--frame-log PATH]
"""
import argparse
import asyncio
import datetime as dt
import json
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

from helpers import FakeLogger, ack_time, handshake, live, valid_ack


def get(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


async def main(a) -> None:
    base = f"http://127.0.0.1:{a.status_port}"
    deadline = time.time() + 60
    while True:
        try:
            if get(base + "/health")[0] == 200:
                break
        except OSError:
            pass
        if time.time() > deadline:
            sys.exit("the health check never answered")
        await asyncio.sleep(1)

    lg = await FakeLogger().connect(a.port)
    for req, reply_type in ((handshake(), 0x11), (live(40), 0x12)):
        await lg.send(req)
        reply = await lg.reply(5)
        now = time.time()
        expected = int(now) + int(dt.datetime.fromtimestamp(now, ZoneInfo(a.tz)).utcoffset().total_seconds())
        assert valid_ack(reply) and reply[4] == reply_type, reply.hex(" ")
        assert abs(ack_time(reply) - expected) <= 5, f"reply time {ack_time(reply)}, expected {expected}"
    await lg.close()

    status = json.loads(get(base + "/status")[1])
    assert status["time_mode"] == "local" and status["timezone"] == a.tz, status
    assert status["totals"]["logins"] == 1 and status["totals"]["acks"] == 2, status
    if a.frame_log:
        time.sleep(1)
        with open(a.frame_log, encoding="utf-8") as f:
            text = f.read()
        assert "<- 0x41 handshake" in text and "-> 0x12 ack" in text, text[-500:]
    print("smoke test ok:", json.dumps(status["totals"]))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--tz", required=True)
    p.add_argument("--port", type=int, default=10000)
    p.add_argument("--status-port", type=int, default=8080)
    p.add_argument("--frame-log")
    asyncio.run(main(p.parse_args()))
