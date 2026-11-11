"""Command line, logging and the main loop: python -m keepalive."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import signal
import sys

from . import __version__
from .config import APP_OPTIONS, LOG_LEVELS, Config, ConfigError, TimeSource, load_options
from .server import KeepaliveServer, frames_log, log, trace


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
    p = argparse.ArgumentParser(prog="python -m keepalive",
                                description="Solarman Keepalive: a local stand-in for the Solarman cloud")
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
