#!/usr/bin/env python3
"""Metrics of a Solarman Keepalive frame log (the app's frames.log, or --frame-log).

Usage: python tools/stats.py FRAMES.LOG [--from "YYYY-MM-DD HH:MM:SS"] [--to "..."] [--window 5]

Prints frames by type, sessions and reconnects, logger boots (from the uptime field of
handshake, live data and report frames), live data the logger did not accept (it sends the
data again or closes the connection within 60 s), their phase in a 30 s cycle with the
worst --window seconds (a local client polling every 30 s shows up as a window where most
replies are lost), and how many responses the logger sent per minute: byte 6 of its frames
counts every response it sends, to this server and to local Modbus clients.

Reads the log line format written by solarman_keepalive/keepalive.py; keep them in step.
The metrics assume one logger per log: events are matched by their order, not by connection.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import statistics

LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)$")
FRAME = re.compile(r"^<- 0x([0-9a-f]{2}) \S+ ctl=([0-9a-f]{2}) seq=([0-9a-f]{2})/([0-9a-f]{2}) sn=(\d+) "
                   r"len=(\d+) head=((?:[0-9a-f]{2} ?)*)")
RETRY_S = 60


def u32(b: bytes, i: int) -> int | None:
    return int.from_bytes(bytes(b[i:i + 4]), "little") if len(b) >= i + 4 else None


def parse(lines, start=None, end=None) -> list[dict]:
    events: list[dict] = []
    for line in lines:
        m = LINE.match(line.rstrip("\n"))
        if not m:
            continue
        t = dt.datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S")
        if (start and t < start) or (end and t > end):
            continue
        msg = m[2]
        e: dict = {"t": t}
        if fm := FRAME.match(msg):
            head = bytes.fromhex(fm[7])
            e.update(k="frame", typ=int(fm[1], 16), b5=int(fm[3], 16), b6=int(fm[4], 16),
                     sn=int(fm[5]), head=head, p0=head[0] if head else None)
            up = None
            if e["typ"] == 0x41:
                up = u32(head, 5)
            elif e["typ"] == 0x42 and e["p0"] == 0x01:
                up = u32(head, 7)
            elif e["typ"] == 0x48 and e["p0"] == 0x01:
                up = u32(head, 5)
            e["up"] = up
        elif msg.startswith("open "):
            e["k"] = "open"
        elif msg.startswith("close "):
            e["k"] = "close"
            # the server closing a connection (idle timeout, room for a new one, shutdown),
            # not the logger reacting to a reply; older logs only have the idle line before it
            e["by_server"] = "(closed by the server" in msg or (bool(events) and events[-1]["k"] == "idle")
        elif msg.startswith("-> ") and " ack repeat " in msg:
            e["k"] = "repeat"           # test builds of 2026-11 only
        elif msg.startswith("-> "):
            e["k"] = "ack"
        elif msg.strip().startswith("idle "):
            e["k"] = "idle"
        elif msg.strip().startswith("rejected "):
            e["k"] = "rejected"
        elif re.search(r"skip |bad |error|no reply", msg):
            e["k"] = "odd"
            e["msg"] = msg.strip()
        else:
            continue
        events.append(e)
    return events


def analyze(ev: list[dict], window: int = 5) -> dict | None:
    frames = [e for e in ev if e["k"] == "frame"]
    if not frames:
        return None
    t0, t1 = ev[0]["t"], ev[-1]["t"]
    # the metrics follow one logger: sessions and replies are matched by the order of events
    r: dict = {"start": t0, "end": t1, "loggers": len({e["sn"] for e in frames})}

    kinds: dict[str, int] = {}
    for e in frames:
        key = f"0x{e['typ']:02x}" + (f"/{e['p0']:02x}" if e["typ"] in (0x42, 0x43, 0x48) and e["p0"] is not None else "")
        kinds[key] = kinds.get(key, 0) + 1
    r["frames"] = len(frames)
    r["kinds"] = dict(sorted(kinds.items()))
    r["counts"] = {k: sum(1 for e in ev if e["k"] == k)
                   for k in ("ack", "repeat", "idle", "odd", "open", "close", "rejected")}
    r["odd"] = [e for e in ev if e["k"] == "odd"][:3]

    # sessions: an open whose first frame is a handshake is a login, otherwise a reconnect
    logins = reconnects = 0
    for i, e in enumerate(ev):
        if e["k"] != "open":
            continue
        nxt = next((x for x in ev[i + 1:] if x["k"] in ("frame", "open")), None)
        if nxt and nxt["k"] == "frame" and nxt["typ"] == 0x41:
            logins += 1
        elif nxt and nxt["k"] == "frame":
            reconnects += 1
    r["logins"], r["reconnects"] = logins, reconnects

    boots: list[dt.datetime] = []
    for e in frames:
        if e["up"] is None:
            continue
        b = e["t"] - dt.timedelta(seconds=e["up"])
        if not boots or abs((b - boots[-1]).total_seconds()) > 60:
            boots.append(b)
    r["boots"] = boots

    # live data and whether the logger accepted the reply
    rows = []
    for i, e in enumerate(ev):
        if e["k"] != "frame" or e["typ"] != 0x42 or e["p0"] != 0x01:
            continue
        nxt = next((x for x in ev[i + 1:] if x["k"] in ("frame", "close", "open") and not x.get("by_server")), None)
        if nxt is None and (t1 - e["t"]).total_seconds() < RETRY_S:
            continue  # too close to the end to tell
        soon = nxt is not None and (nxt["t"] - e["t"]).total_seconds() < RETRY_S
        fail = soon and (nxt["k"] == "close" or (nxt["k"] == "frame" and nxt["typ"] == 0x42 and nxt["p0"] == 0x01))
        rows.append((e["t"], e["t"].second % 30, fail))
    r["live"] = len(rows)
    r["not_accepted"] = sum(1 for x in rows if x[2])
    r["phases"] = {ph: "".join("F" if x[2] else "." for x in rows if x[1] == ph) for ph in range(30)}
    r["window"] = None
    if rows:
        w = max(1, min(window, 29))
        best = max(range(30), key=lambda s: sum(1 for x in rows if x[2] and (x[1] - s) % 30 < w))
        inw = [x for x in rows if (x[1] - best) % 30 < w]
        out = [x for x in rows if (x[1] - best) % 30 >= w]
        r["window"] = {"start": best, "end": (best + w - 1) % 30, "width": w,
                       "in": (sum(x[2] for x in inw), len(inw)), "out": (sum(x[2] for x in out), len(out))}

    # responses the logger sent per minute, from byte 6 between consecutive live frames of one boot
    rates = []
    live_idx = [i for i, e in enumerate(frames) if e["typ"] == 0x42 and e["p0"] == 0x01]
    for i, j in zip(live_idx, live_idx[1:]):
        x, y = frames[i], frames[j]
        secs = (y["t"] - x["t"]).total_seconds()
        if not 200 <= secs <= 400:
            continue
        if x["up"] is not None and y["up"] is not None and y["up"] < x["up"]:
            continue  # reboot in between
        sent = (y["b6"] - x["b6"]) % 256
        to_server = j - i  # frames sent to the server after x, up to and including y
        rates.append(max(sent - to_server, 0) / secs * 60)
    r["rates"] = rates
    return r


def report(r: dict | None) -> str:
    if r is None:
        return "no logger frames in range"
    out = []
    t0, t1 = r["start"], r["end"]
    out.append(f"period {t0:%Y-%m-%d %H:%M:%S} .. {t1:%H:%M:%S} ({(t1 - t0).total_seconds() / 3600:.1f} h)")
    if r["loggers"] > 1:
        out.append(f"WARNING: frames from {r['loggers']} loggers; these metrics assume one logger per log")
    out.append(f"frames {r['frames']}: " + ", ".join(f"{k} x{v}" for k, v in r["kinds"].items()))
    c = r["counts"]
    out.append(f"acks {c['ack']}, idle closes {c['idle']}, rejected {c['rejected']}, odd lines {c['odd']}"
               + (f", repeats {c['repeat']}" if c["repeat"] else ""))
    for e in r["odd"]:
        out.append(f"  odd {e['t']:%H:%M:%S} {e['msg'][:100]}")
    out.append(f"sessions: {c['open']} open(s), {c['close']} close(s); {r['logins']} login(s), "
               f"{r['reconnects']} reconnect(s) without login")
    boots = r["boots"]
    parts = [f"{b:%H:%M:%S}" + (f" (+{(b - boots[i - 1]).total_seconds() / 60:.1f} min)" if i else "")
             for i, b in enumerate(boots)]
    out.append("boots: " + (", ".join(parts) if parts else "-"))
    pct = f" ({100 * r['not_accepted'] / r['live']:.0f}%)" if r["live"] else ""
    out.append(f"live data: {r['live']}, not accepted {r['not_accepted']}{pct}")
    if r["window"]:
        out.append("  phase s mod 30: " + " ".join(f"{ph}:{m}" for ph, m in r["phases"].items() if m))
        w = r["window"]
        out.append(f"  worst {w['width']} s window {w['start']}..{w['end']}: {w['in'][0]}/{w['in'][1]} not accepted; "
                   f"outside {w['out'][0]}/{w['out'][1]}")
    if r["rates"]:
        rates = r["rates"]
        out.append(f"local responses by the logger: ~{statistics.median(rates):.1f}/min "
                   f"(median of {len(rates)} intervals; min {min(rates):.1f}, max {max(rates):.1f})")
    return "\n".join(out)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("log")
    p.add_argument("--from", dest="start", type=dt.datetime.fromisoformat)
    p.add_argument("--to", dest="end", type=dt.datetime.fromisoformat)
    p.add_argument("--window", type=int, default=5, help="seconds of the worst phase window (default 5)")
    a = p.parse_args(argv)
    with open(a.log, encoding="utf-8") as f:
        print(report(analyze(parse(f, a.start, a.end), a.window)))


if __name__ == "__main__":
    main()
