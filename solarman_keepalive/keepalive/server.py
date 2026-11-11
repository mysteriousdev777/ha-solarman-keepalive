"""The logger connections: a reply to each logger frame, allowed_ips, the clock guard, the
idle timeout and the connection limit. Runs the status endpoint next to them."""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import ipaddress
import itertools
import logging
import time
from dataclasses import dataclass

from . import __version__
from .config import Config, TimeSource
from .protocol import FrameReader, ack, describe, is_request
from .status import StatusServer
from .tracker import HOUR, Tracker

log = logging.getLogger("keepalive")
frames_log = logging.getLogger("keepalive_frames")

MAX_SESSIONS = 8                # the logger opens one; stale ones linger until the idle timeout
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
        self._status: StatusServer | None = None
        self._sessions: dict[int, Session] = {}
        self._rejected_seen: set[str] = set()
        self._rejected_overflow = False
        self._clock_bad = False
        self._stopping = False
        self._summary_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._logger_conn, self.cfg.host, self.cfg.port)
        self.port = self._server.sockets[0].getsockname()[1]
        if self.cfg.status_port is not None:
            self._status = StatusServer(self.status, self.serving)
            await self._status.start(self.cfg.host, self.cfg.status_port)
            self.status_port = self._status.port
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
