"""Options (the Home Assistant app's options.json or the command line) and the time in the
replies."""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
import sys
import time
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP_OPTIONS = "/data/options.json"
APP_FRAME_LOG = "/config/frames.log"
LOG_LEVELS = ("debug", "info", "warning", "error")


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
