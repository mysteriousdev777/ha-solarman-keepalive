#!/usr/bin/env python3
"""Solarman Keepalive: a local stand-in for the Solarman cloud.

A Solarman Wi-Fi logger (LSW-3 and similar sticks) that cannot reach its cloud keeps
rebooting every 30 minutes, and its local Modbus port (8899) goes away each time.
Pointed at this server (logger web UI, config_hide.html: Server A and the Optional
Server -> this host, port 10000, TCP) it gets the replies it expects from the cloud and
stays online; only its built-in reboot every 3 hours remains.

The server only answers the logger's frames with the acknowledgement the cloud sends
(the reply of Hypfer's dummycloud), and never sends requests or Modbus commands of its
own. Each answer carries the current time, and the logger sets the inverter clock from
it: the one thing the server changes, as the Solarman cloud does. So the 'local' mode
sends the local wall-clock time, like the cloud.

Home Assistant app: options from /data/options.json (--options).
Standalone:  python keepalive.py --timezone Europe/Kyiv --allow <logger-ip>
             (Windows without the tzdata package: --timezone system)

Standard library only, Python 3.11 or newer.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import datetime as dt
import ipaddress
import itertools
import json
import logging
import logging.handlers
import os
import re
import signal
import struct
import sys
import time
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__version__ = "0.1.0"

log = logging.getLogger("keepalive")
frames_log = logging.getLogger("keepalive_frames")

# ------------------------------------------------------------------------------------------
# Solarman V5 frames
#
#   A5 | length u16 LE | control | type | seq | seq | logger serial u32 LE | payload | sum | 15
#
# Logger -> server frames have control 0x10; the reply type is the request type - 0x30.

START, END = 0xA5, 0x15
HEADER, FOOTER = 11, 2
MAX_PAYLOAD = 2048          # the longest frame seen carries 1051 bytes (live data)
ACK_LEN = 23
REQUEST = 0x10
TYPES = {0x41: "handshake", 0x42: "data", 0x43: "wifi", 0x47: "heartbeat", 0x48: "report"}
LIVE = 0x01                 # first payload byte of current data; 0x81 = stored, sent late


def checksum(frame: bytes | bytearray) -> int:
    """Sum of all bytes but the start byte, the checksum and the end byte, mod 256."""
    return sum(frame[1:-2]) & 0xFF


class FrameReader:
    """Splits a TCP byte stream into Solarman V5 frames.

    Bytes before a frame start and frames with a bad length, end byte or checksum are
    dropped, and reading resyncs on the next start byte. The buffer never holds more than
    one incomplete frame, so it stays under HEADER + MAX_PAYLOAD + FOOTER bytes."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.notes: list[str] = []      # what was dropped, for the frame log
        self.dropped = 0                # bytes dropped

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out = []
        while (frame := self._next()) is not None:
            out.append(frame)
        return out

    def _drop(self, n: int, why: str) -> None:
        self.notes.append(f"   {why}: {bytes(self.buf[:16]).hex(' ')}")
        self.dropped += n
        del self.buf[:n]

    def _next(self) -> bytes | None:
        buf = self.buf
        while buf:
            if buf[0] != START:
                i = buf.find(START)
                n = len(buf) if i < 0 else i
                self._drop(n, f"skip {n} byte(s) before a frame start")
                continue
            if len(buf) < 3:
                return None
            size = struct.unpack_from("<H", buf, 1)[0]
            if size > MAX_PAYLOAD:
                self._drop(1, f"bad length {size}, resync")
                continue
            n = HEADER + size + FOOTER
            if len(buf) < n:
                return None
            frame = bytes(buf[:n])
            if frame[-1] != END or frame[-2] != checksum(frame):
                self._drop(1, "bad end or checksum, resync")
                continue
            del buf[:n]
            return frame
        return None


def payload(frame: bytes) -> bytes:
    return frame[HEADER:-FOOTER]


def logger_serial(frame: bytes) -> int:
    return struct.unpack_from("<I", frame, 7)[0]


def is_request(frame: bytes) -> bool:
    """A logger -> server frame of a known type: the only kind the server answers."""
    return frame[3] == REQUEST and frame[4] in TYPES


def is_live(frame: bytes) -> bool:
    """Current data (as opposed to data stored while offline and sent late)."""
    return frame[4] == 0x42 and payload(frame)[:1] == bytes([LIVE])


def uptime(frame: bytes) -> int | None:
    """Seconds since the logger booted, from the frames that carry it."""
    p, typ = payload(frame), frame[4]
    if typ == 0x41:
        offset = 5
    elif typ == 0x42 and p[:1] == bytes([LIVE]):
        offset = 7
    elif typ == 0x48 and p[:1] == bytes([LIVE]):
        offset = 5
    else:
        return None
    if len(p) < offset + 4:
        return None
    return struct.unpack_from("<I", p, offset)[0]


def describe(frame: bytes) -> str:
    """One line per frame for the frame log (tools/stats.py reads this format)."""
    typ, p = frame[4], payload(frame)
    s = (f"<- 0x{typ:02x} {TYPES.get(typ, 'unknown')} ctl={frame[3]:02x} seq={frame[5]:02x}/{frame[6]:02x} "
         f"sn={logger_serial(frame)} len={len(p)} head={p[:32].hex(' ')}")
    if typ in (0x41, 0x43):
        s += " text=" + "|".join(t.decode() for t in re.findall(rb"[\x20-\x7e]{4,}", p))
    return s


def ack(frame: bytes, now: int) -> bytes:
    """The cloud's reply to a logger frame (Hypfer, dummycloud, buildTimeResponse)."""
    r = bytearray(ACK_LEN)
    r[0] = START
    struct.pack_into("<H", r, 1, 10)
    r[3] = frame[3]
    r[4] = (frame[4] - 0x30) & 0xFF
    r[5] = (frame[5] + 1) & 0xFF
    r[6] = frame[6]
    r[7:11] = frame[7:11]
    r[11] = frame[HEADER] if len(frame) > HEADER + FOOTER else 0
    r[12] = 0x01
    struct.pack_into("<II", r, 13, now & 0xFFFFFFFF, 0)
    r[21] = checksum(r)
    r[22] = END
    return bytes(r)


# ------------------------------------------------------------------------------------------
# Time in the replies


class ConfigError(Exception):
    pass


class TimeSource:
    """Time sent in the replies. The logger sets the inverter clock from it, so in the
    'local' mode it is the local wall-clock time written as if it were UTC, which is what
    the Solarman cloud sends. Without a known time zone the 'local' mode refuses to start:
    falling back to UTC would shift the inverter clock."""

    def __init__(self, mode: str, zone: str = "") -> None:
        if mode not in ("local", "utc"):
            raise ConfigError(f"time must be 'local' or 'utc', not {mode!r}")
        self.mode = mode
        self.zone_name: str | None = None
        self.tz: ZoneInfo | None = None
        if mode == "utc":
            return
        name = (zone or os.environ.get("TZ", "")).strip()
        if not name:
            raise ConfigError("time 'local' needs a time zone: set the 'timezone' option "
                              "or the TZ environment variable")
        if name == "system":
            self.zone_name = "system"
            return
        try:
            self.tz = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as e:
            hint = " (on Windows: pip install tzdata, or --timezone system)" if sys.platform == "win32" else ""
            raise ConfigError(f"unknown time zone {name!r}{hint}") from e
        self.zone_name = name

    def offset(self, utc: float) -> int:
        """Seconds to add to UTC for the local time at that moment (follows DST)."""
        if self.mode == "utc":
            return 0
        if self.tz is None:
            local = dt.datetime.fromtimestamp(utc).astimezone()
        else:
            local = dt.datetime.fromtimestamp(utc, self.tz)
        return int(local.utcoffset().total_seconds())

    def now(self, utc: float | None = None) -> int:
        utc = time.time() if utc is None else utc
        return int(utc) + self.offset(utc)

    def utc_offset(self, utc: float | None = None) -> str:
        off = self.offset(time.time() if utc is None else utc)
        h, m = divmod(abs(off) // 60, 60)
        return f"{'+' if off >= 0 else '-'}{h:02d}:{m:02d}"

    def describe(self, utc: float | None = None) -> str:
        if self.mode == "utc":
            return "time in replies: UTC"
        utc = time.time() if utc is None else utc
        shown = dt.datetime.fromtimestamp(self.now(utc), dt.timezone.utc)
        return (f"time in replies: local, {self.zone_name} (UTC{self.utc_offset(utc)}), "
                f"now {shown:%Y-%m-%d %H:%M:%S}")


# ------------------------------------------------------------------------------------------
# Options

APP_OPTIONS = "/data/options.json"
APP_FRAME_LOG = "/config/frames.log"
LOG_LEVELS = ("debug", "info", "warning", "error")


@dataclass
class Config:
    time: str = "local"
    timezone: str = ""
    allowed_ips: list[str] = field(default_factory=list)
    idle_timeout: float = 15            # minutes; 0 = never
    log_level: str = "info"
    frame_log: str | None = None        # path; None = off
    host: str = "0.0.0.0"
    port: int = 10000                   # 0 = any free port (tests)
    status_port: int | None = 8080      # None = off; 0 = any free port (tests)

    def validate(self) -> "Config":
        if self.time not in ("local", "utc"):
            raise ConfigError(f"time must be 'local' or 'utc', not {self.time!r}")
        if self.log_level not in LOG_LEVELS:
            raise ConfigError(f"log_level must be one of {', '.join(LOG_LEVELS)}, not {self.log_level!r}")
        if isinstance(self.idle_timeout, bool) or not isinstance(self.idle_timeout, (int, float)) \
                or not 0 <= self.idle_timeout <= 1440:
            raise ConfigError(f"idle_timeout must be 0..1440 minutes, not {self.idle_timeout!r}")
        ips = []
        for ip in self.allowed_ips:
            try:
                ips.append(str(ipaddress.ip_address(str(ip).strip())))
            except ValueError as e:
                raise ConfigError(f"allowed_ips: {ip!r} is not an IP address") from e
        self.allowed_ips = ips
        for name in ("port", "status_port"):
            value = getattr(self, name)
            if value is not None and not (isinstance(value, int) and 0 <= value <= 65535):
                raise ConfigError(f"{name} must be 0..65535, not {value!r}")
        return self


def load_options(path: str) -> Config:
    """Options of the Home Assistant app (Supervisor writes them to /data/options.json)."""
    try:
        with open(path, encoding="utf-8") as f:
            o = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read options {path}: {e}") from e
    if not isinstance(o, dict):
        raise ConfigError(f"options {path}: expected a JSON object")
    allowed = o.get("allowed_ips") or []
    if not isinstance(allowed, list):
        raise ConfigError("allowed_ips must be a list")
    return Config(
        time=o.get("time", "local"),
        timezone=str(o.get("timezone") or "").strip(),
        allowed_ips=allowed,
        idle_timeout=o.get("idle_timeout", 15),
        log_level=o.get("log_level", "info"),
        frame_log=APP_FRAME_LOG if o.get("frame_log") else None,
    ).validate()


# ------------------------------------------------------------------------------------------
# What the logger does: counters for the log and the status endpoint

RETRY_WINDOW = 60       # live data sent again, or the connection closed, within this = reply not accepted
BOOT_TOLERANCE = 60     # boot time estimates closer than this are the same boot
HOUR = 3600
HOURLY = ("live", "not_accepted", "reconnects", "logins", "restarts", "rejected")


def iso(t: float | None) -> str | None:
    return None if t is None else dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="seconds")


class Tracker:
    """Follows the logger through its frames: logins, reconnects, restarts and live data
    whose reply it did not accept.

    The logger sends current data about every 5.5 minutes. When it does not accept the
    reply (it misses replies while a local client polls it on port 8899), it reads the data
    again and sends it about 34 s later, or closes the connection and reconnects.

    One logger per app: the counters follow a single logger. Frames from another logger
    still get replies, but raise a warning, since they would mix up the counters.

    All times here are time.monotonic() seconds, so a jump of the system clock does not
    distort intervals; snapshot() turns them into wall-clock timestamps."""

    def __init__(self, idle_timeout_s: float, clock=time.monotonic) -> None:
        self.started = clock()
        self.idle_timeout_s = idle_timeout_s
        self.serials: dict[int, int] = {}           # logger serial -> frames
        self.warnings: list[str] = []               # for the log, drained by the server
        self.totals: collections.Counter[str] = collections.Counter()
        self.hour: collections.deque[tuple[float, str]] = collections.deque()
        self.sessions: dict[int, int] = {}          # open session -> frames so far
        self.last_frame: float | None = None
        self.last_login: float | None = None
        self.boot: float | None = None
        self.pending: tuple[float, int] | None = None   # live data whose reply is not settled

    def _count(self, key: str, t: float, n: int = 1) -> None:
        self.totals[key] += n
        if key in HOURLY:
            self.hour.append((t, key))

    def _trim(self, t: float) -> None:
        while self.hour and t - self.hour[0][0] > HOUR:
            self.hour.popleft()

    def opened(self, sid: int) -> None:
        self.sessions[sid] = 0
        self.totals["connections"] += 1

    def rejected(self, t: float) -> None:
        self._count("rejected", t)

    def frame(self, sid: int, frame: bytes, t: float, reply: str = "ack") -> list[str]:
        """Account for one frame; returns events for the log. reply: 'ack' (answered),
        'unknown' (not a frame the server answers) or 'held' (system clock not set)."""
        msgs = []
        sn = logger_serial(frame)
        if sn not in self.serials:
            if self.serials:
                self.warnings.append(
                    f"frames from {len(self.serials) + 1} different loggers: this app follows one logger, "
                    "so its status and log now mix them up (replies are not affected; set allowed_ips "
                    "to your logger's address)")
            self.serials[sn] = 0
        self.serials[sn] += 1
        first = self.sessions.get(sid, 0) == 0
        self.sessions[sid] = self.sessions.get(sid, 0) + 1
        self._count("frames", t)
        self._count("acks" if reply == "ack" else reply, t)
        typ = frame[4]
        if typ == 0x41:
            self._count("logins", t)
            self.last_login = t
            msgs.append("logger logged in")
        elif first:
            self._count("reconnects", t)
            msgs.append("logger reconnected (no new login)")

        live = is_live(frame)
        if self.pending is not None:
            sent, _ = self.pending
            self.pending = None
            if live and t - sent < RETRY_WINDOW:
                self._count("not_accepted", t)
                msgs.append("logger sent its data again: the previous reply was not accepted "
                            "(normal while a local client polls the logger)")
        if live:
            self._count("live", t)
            self.pending = (t, sid)

        up = uptime(frame)
        if up is not None:
            boot = t - up
            if self.boot is not None and abs(boot - self.boot) > BOOT_TOLERANCE:
                self._count("restarts", t)
                msgs.append(f"logger restarted {up} s ago")
            self.boot = boot
        self.last_frame = t
        return msgs

    def closed(self, sid: int, t: float, closer: str = "logger", dropped: int = 0) -> list[str]:
        """closer: 'logger', or the server: 'idle' (timeout), 'evicted' (room for a new
        connection), 'stop' (shutdown). Only the logger closing tells about its replies."""
        msgs = []
        frames = self.sessions.pop(sid, 0)
        if dropped:
            self.totals["dropped_bytes"] += dropped
        if closer == "idle":
            self.totals["idle_closes"] += 1
        elif closer == "evicted":
            self.totals["evicted"] += 1
        if self.pending is not None and self.pending[1] == sid:
            sent, _ = self.pending
            self.pending = None
            if closer == "logger" and t - sent < RETRY_WINDOW:
                self._count("not_accepted", t)
                msgs.append("logger closed the connection right after its data: the reply was not accepted")
        if frames and closer == "logger":
            self.totals["closed_by_logger"] += 1
        return msgs

    def connected(self, t: float) -> bool:
        """An open connection has carried frames, and the last frame is recent."""
        limit = self.idle_timeout_s or 15 * 60
        return (any(n > 0 for n in self.sessions.values())
                and self.last_frame is not None and t - self.last_frame <= limit)

    def last_hour(self, t: float) -> dict:
        self._trim(t)
        c = collections.Counter(k for _, k in self.hour)
        live, failed = c["live"], c["not_accepted"]
        return {
            "live_data": live,
            "not_accepted": failed,
            "not_accepted_pct": round(100 * failed / live, 1) if live else None,
            "reconnects": c["reconnects"],
            "logins": c["logins"],
            "restarts": c["restarts"],
            "rejected": c["rejected"],
        }

    def snapshot(self, t: float, wall: float | None = None) -> dict:
        """t: monotonic now; wall: the system clock now, for the timestamps."""
        wall = time.time() if wall is None else wall

        def at(m: float | None) -> str | None:
            return None if m is None else iso(wall - (t - m))

        return {
            "started": at(self.started),
            "loggers": len(self.serials),
            "connected": self.connected(t),
            "sessions_open": len(self.sessions),
            "last_frame": at(self.last_frame),
            "last_frame_age_s": None if self.last_frame is None else int(t - self.last_frame),
            "last_login": at(self.last_login),
            "logger_boot": at(self.boot),
            "logger_uptime_s": None if self.boot is None else int(t - self.boot),
            "last_hour": self.last_hour(t),
            "totals": {k: self.totals[k] for k in (
                "connections", "frames", "acks", "live", "not_accepted", "logins", "reconnects",
                "restarts", "closed_by_logger", "idle_closes", "evicted", "rejected", "unknown",
                "held", "dropped_bytes")},
        }

    def summary(self, t: float) -> str:
        h = self.last_hour(t)
        state = "connected" if self.connected(t) else "NOT connected"
        return (f"last hour: logger {state}; {h['live_data']} data frame(s), {h['not_accepted']} reply(ies) "
                f"not accepted, {h['reconnects']} reconnect(s), {h['logins']} login(s), "
                f"{h['restarts']} restart(s), {h['rejected']} connection(s) rejected")


# ------------------------------------------------------------------------------------------
# Server

MAX_SESSIONS = 8                # the logger opens one; stale ones linger until the idle timeout
MAX_STATUS_CONNECTIONS = 16
STATUS_LIMIT = 4096             # bytes of an HTTP request line or header
STATUS_DEADLINE = 5             # seconds to read a whole status request
MAX_REJECTED_LOGGED = 64        # addresses whose first rejected connection is logged
# A system clock before this is clearly not set (a host without a battery-backed clock,
# before time sync). Answering would set the inverter clock wrong. Keep it at the release
# month of the version.
CLOCK_MIN = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc).timestamp()


@dataclass
class Session:
    writer: asyncio.StreamWriter
    active: float                   # monotonic time of the last frame, or of the connection
    closer: str | None = None       # set when the server closes it: idle, evicted, stop


def trace(msg: str) -> None:
    """Frame-level detail: the frame log (if on) and the debug log."""
    if frames_log.handlers:
        frames_log.info(msg)
    log.debug(msg)


def _peer_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


class KeepaliveServer:
    def __init__(self, cfg: Config, timesource: TimeSource, clock=time.time) -> None:
        self.cfg = cfg
        self.time = timesource
        self.clock = clock                  # the system clock: the time in the replies
        self.allowed = {ipaddress.ip_address(ip) for ip in cfg.allowed_ips}
        self.tracker = Tracker(cfg.idle_timeout * 60)
        self.port: int | None = None
        self.status_port: int | None = None
        self._ids = itertools.count(1)
        self._server: asyncio.base_events.Server | None = None
        self._status: asyncio.base_events.Server | None = None
        self._sessions: dict[int, Session] = {}
        self._status_active = 0
        self._rejected_seen: set[str] = set()
        self._rejected_overflow = False
        self._clock_bad = False
        self._stopping = False
        self._summary_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._logger_conn, self.cfg.host, self.cfg.port)
        self.port = self._server.sockets[0].getsockname()[1]
        if self.cfg.status_port is not None:
            self._status = await asyncio.start_server(self._status_conn, self.cfg.host, self.cfg.status_port,
                                                      limit=STATUS_LIMIT)
            self.status_port = self._status.sockets[0].getsockname()[1]
        self._summary_task = asyncio.create_task(self._summaries())

    async def stop(self) -> None:
        self._stopping = True
        if self._summary_task:
            self._summary_task.cancel()
        for server in (self._server, self._status):
            if server:
                server.close()
        for s in list(self._sessions.values()):
            s.closer = s.closer or "stop"
            s.writer.close()
        for server in (self._server, self._status):
            if server:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(server.wait_closed(), 5)

    def serving(self) -> bool:
        return self._server is not None and self._server.is_serving()

    def status(self) -> dict:
        wall = self.clock()
        return {"version": __version__, "time_mode": self.time.mode, "timezone": self.time.zone_name,
                "utc_offset": self.time.utc_offset(wall), "clock_ok": wall >= CLOCK_MIN,
                **self.tracker.snapshot(time.monotonic(), wall)}

    async def _summaries(self) -> None:
        while True:
            await asyncio.sleep(HOUR)
            log.info(self.tracker.summary(time.monotonic()))

    def _clock_ok(self, wall: float) -> bool:
        """False while the system clock is clearly not set; logs when that starts and ends."""
        ok = wall >= CLOCK_MIN
        if not ok and not self._clock_bad:
            shown = dt.datetime.fromtimestamp(max(wall, 0), dt.timezone.utc)
            log.warning("the system clock says %s, before this version was released: not answering the "
                        "logger until the clock is set (an answer would set the inverter clock wrong)",
                        f"{shown:%Y-%m-%d %H:%M} UTC")
        elif ok and self._clock_bad:
            log.info("the system clock is set now: answering the logger again")
        self._clock_bad = not ok
        return ok

    def _reject(self, ip, where: str) -> None:
        """Log the first rejected connection of each address; count all of them."""
        self.tracker.rejected(time.monotonic())
        trace(f"   rejected {where}: not in allowed_ips")
        key = str(ip)
        if key in self._rejected_seen:
            return
        if len(self._rejected_seen) < MAX_REJECTED_LOGGED:
            self._rejected_seen.add(key)
            log.info("rejected a connection from an address not in allowed_ips; further ones from it "
                     "are only counted (hourly summary, status)")
        elif not self._rejected_overflow:
            self._rejected_overflow = True
            log.info("rejected connections from many addresses; new addresses are only counted from now on")

    def _make_room(self) -> None:
        """At the limit, close the connection that has been silent longest: stale ones left by a
        rebooted logger go first, and connections that send nothing cannot lock the logger out."""
        live = [(sid, s) for sid, s in self._sessions.items() if s.closer is None]
        if not live:
            return
        _, oldest = min(live, key=lambda item: item[1].active)
        oldest.closer = "evicted"
        log.info("closed the longest-silent connection to make room for a new one (%d open)", len(live))
        oldest.writer.close()

    async def _close(self, writer: asyncio.StreamWriter) -> None:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def _logger_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        where = f"{peer[0]}:{peer[1]}"
        ip = _peer_ip(str(peer[0]))
        if self.allowed and ip not in self.allowed:
            self._reject(ip if ip is not None else peer[0], where)
            await self._close(writer)
            return
        if sum(1 for s in self._sessions.values() if s.closer is None) >= MAX_SESSIONS:
            self._make_room()

        sid = next(self._ids)
        opened = time.monotonic()
        session = Session(writer, opened)
        self._sessions[sid] = session
        self.tracker.opened(sid)
        frames_in = FrameReader()
        frames = 0
        idle = self.cfg.idle_timeout * 60 or None
        trace(f"open  {where}")
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), idle)
                except asyncio.TimeoutError:
                    session.closer = "idle"
                    trace(f"   idle {self.cfg.idle_timeout:g} min, closing {where}")
                    log.info("closed a connection idle for %g min (a rebooted logger leaves its old one open)",
                             self.cfg.idle_timeout)
                    break
                if not chunk:
                    break
                for frame in frames_in.feed(chunk):
                    frames += 1
                    wall, mono = self.clock(), time.monotonic()
                    session.active = mono
                    state, reply = "unknown", None
                    if is_request(frame):
                        if self._clock_ok(wall):
                            sent_time = self.time.now(wall)
                            reply = ack(frame, sent_time)
                            writer.write(reply)      # reply first: the logger waits only a few seconds
                            await writer.drain()
                            state = "ack"
                        else:
                            state = "held"
                    trace(describe(frame))
                    if reply:
                        shown = dt.datetime.fromtimestamp(sent_time, dt.timezone.utc)
                        trace(f"-> 0x{reply[4]:02x} ack time={sent_time} ({shown:%Y-%m-%d %H:%M:%S} as UTC)")
                    elif state == "held":
                        trace("   no reply: the system clock is not set")
                    else:
                        trace("   no reply: unknown frame")
                    for msg in self.tracker.frame(sid, frame, mono, state):
                        log.info(msg)
                    for msg in self.tracker.warnings:
                        log.warning(msg)
                    self.tracker.warnings.clear()
                for note in frames_in.notes:
                    trace(note)
                frames_in.notes.clear()
        except (OSError, asyncio.IncompleteReadError) as e:
            trace(f"   error {where}: {e!r}")
        finally:
            self._sessions.pop(sid, None)
            closer = session.closer or ("stop" if self._stopping else "logger")
            s = int(time.monotonic() - opened)
            by = "" if closer == "logger" else f" (closed by the server: {closer})"
            trace(f"close {where} after {s // 60} min {s % 60} s, {frames} frame(s){by}")
            for msg in self.tracker.closed(sid, time.monotonic(), closer, frames_in.dropped):
                log.info(msg)
            if frames and closer == "logger":
                log.info("logger closed its connection after %d min %d s, %d frame(s)", s // 60, s % 60, frames)
            await self._close(writer)

    @staticmethod
    async def _read_request(reader: asyncio.StreamReader) -> bytes:
        request = await reader.readline()
        for _ in range(64):
            if await reader.readline() in (b"\r\n", b"\n", b""):
                break
        return request

    async def _status_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """GET /status: JSON for Home Assistant REST sensors; GET /health: for the watchdog.
        At most MAX_STATUS_CONNECTIONS at a time, each with STATUS_DEADLINE for its request."""
        if self._status_active >= MAX_STATUS_CONNECTIONS:
            await self._close(writer)
            return
        self._status_active += 1
        try:
            request = await asyncio.wait_for(self._read_request(reader), STATUS_DEADLINE)
            parts = request.decode("latin-1").split()
            method = parts[0] if parts else ""
            path = parts[1].split("?")[0] if len(parts) > 1 else ""
            if method not in ("GET", "HEAD"):
                code, ctype, body = 405, "text/plain", b"method not allowed\n"
            elif path == "/status":
                code, ctype = 200, "application/json"
                body = json.dumps(self.status(), indent=2).encode() + b"\n"
            elif path == "/health":
                code, ctype = (200, "text/plain") if self.serving() else (503, "text/plain")
                body = b"ok\n" if code == 200 else b"not serving\n"
            else:
                code, ctype, body = 404, "text/plain", b"not found\n"
            reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 503: "Service Unavailable"}[code]
            head = (f"HTTP/1.1 {code} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                    f"Cache-Control: no-store\r\nConnection: close\r\n\r\n").encode()
            writer.write(head + (b"" if method == "HEAD" else body))
            await writer.drain()
        except (asyncio.TimeoutError, OSError, ValueError, UnicodeError):
            pass
        finally:
            self._status_active -= 1
            await self._close(writer)


# ------------------------------------------------------------------------------------------
# Command line and logging


def setup_logging(level: str, frame_log: str | None) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in list(log.handlers):
        log.removeHandler(h)
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    log.addHandler(out)
    log.setLevel(getattr(logging, level.upper()))
    log.propagate = False

    for h in list(frames_log.handlers):
        frames_log.removeHandler(h)
        h.close()
    frames_log.propagate = False
    frames_log.setLevel(logging.INFO)
    if frame_log:
        try:
            fh = logging.handlers.RotatingFileHandler(frame_log, maxBytes=5 * 1024 * 1024, backupCount=3,
                                                      encoding="utf-8")
        except OSError as e:
            raise ConfigError(f"cannot open the frame log {frame_log}: {e}") from e
        fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        frames_log.addHandler(fh)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Solarman Keepalive: a local stand-in for the Solarman cloud")
    p.add_argument("--options", metavar="FILE", help=f"Home Assistant app options ({APP_OPTIONS}); "
                                                     "other options are then ignored")
    p.add_argument("--time", choices=["local", "utc"], default="local",
                   help="time in the replies; the logger sets the inverter clock from it (default local)")
    p.add_argument("--timezone", default="", metavar="ZONE",
                   help="time zone for --time local, e.g. Europe/Kyiv; default: TZ; 'system': the OS time zone")
    p.add_argument("--allow", action="append", default=[], metavar="IP",
                   help="accept connections only from this address (repeatable; default: any)")
    p.add_argument("--idle-timeout", type=float, default=15, metavar="MIN",
                   help="close a connection after MIN minutes without data (default 15, 0 = never)")
    p.add_argument("--host", default="0.0.0.0",
                   help="address to listen on (default 0.0.0.0, all; 127.0.0.1 for local tries)")
    p.add_argument("--port", type=int, default=10000, help="port for the logger (default 10000)")
    p.add_argument("--status-port", type=int, default=8080, help="port of the status endpoint (default 8080)")
    p.add_argument("--no-status", action="store_true", help="no status endpoint")
    p.add_argument("--log-level", choices=LOG_LEVELS, default="info")
    p.add_argument("--frame-log", metavar="FILE", help="write every frame to FILE (rotated, 4 x 5 MB)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def config_from_args(a: argparse.Namespace) -> Config:
    if a.options:
        return load_options(a.options)
    return Config(time=a.time, timezone=a.timezone, allowed_ips=a.allow, idle_timeout=a.idle_timeout,
                  log_level=a.log_level, frame_log=a.frame_log, host=a.host, port=a.port,
                  status_port=None if a.no_status else a.status_port).validate()


async def run(cfg: Config, timesource: TimeSource) -> None:
    server = KeepaliveServer(cfg, timesource)
    await server.start()
    status = f"status on port {server.status_port}" if server.status_port is not None else "no status endpoint"
    log.info("Solarman Keepalive %s: logger port %d, %s", __version__, server.port, status)
    log.info(timesource.describe())
    log.info("allowed addresses: %s; idle timeout: %s; frame log: %s",
             f"{len(cfg.allowed_ips)} configured" if cfg.allowed_ips else "any",
             f"{cfg.idle_timeout:g} min" if cfg.idle_timeout else "off", cfg.frame_log or "off")
    log.debug("allowed_ips: %s", ", ".join(cfg.allowed_ips) or "any")
    trace(f"start: Solarman Keepalive {__version__}, {timesource.describe()}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, AttributeError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await server.stop()
        log.info("stopped")
        trace("stopped")


def main(argv: list[str] | None = None) -> int:
    setup_logging("info", None)
    try:
        cfg = config_from_args(parse_args(argv))
        timesource = TimeSource(cfg.time, cfg.timezone)
        setup_logging(cfg.log_level, cfg.frame_log)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    try:
        asyncio.run(run(cfg, timesource))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        log.error("cannot listen: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
