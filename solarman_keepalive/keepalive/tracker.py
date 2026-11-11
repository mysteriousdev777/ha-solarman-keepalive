"""What the logger does, from its frames: counters for the log and the status endpoint.
No I/O here: the server feeds frames and connection events in and logs what comes out."""
from __future__ import annotations

import collections
import datetime as dt
import time

from .protocol import is_live, logger_serial, uptime

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
