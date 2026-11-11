"""HTTP status endpoint on the internal network: GET /status, JSON for Home Assistant REST
sensors, and GET /health for the Supervisor watchdog. It reads the server's state through
two callables and knows nothing else about it."""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Callable

MAX_CONNECTIONS = 16
REQUEST_LIMIT = 4096            # bytes of an HTTP request line or header
DEADLINE = 5                    # seconds to read a whole request
REASONS = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 503: "Service Unavailable"}


async def _read_request(reader: asyncio.StreamReader) -> bytes:
    """The request line; the headers are read and ignored."""
    request = await reader.readline()
    for _ in range(64):
        if await reader.readline() in (b"\r\n", b"\n", b""):
            break
    return request


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


class StatusServer:
    """At most MAX_CONNECTIONS at a time, each with DEADLINE seconds for its request.
    status() gives the JSON document, healthy() the answer of /health."""

    def __init__(self, status: Callable[[], dict], healthy: Callable[[], bool]) -> None:
        self._status = status
        self._healthy = healthy
        self._active = 0
        self._server: asyncio.base_events.Server | None = None
        self.port: int | None = None

    async def start(self, host: str, port: int) -> None:
        self._server = await asyncio.start_server(self._conn, host, port, limit=REQUEST_LIMIT)
        self.port = self._server.sockets[0].getsockname()[1]

    def close(self) -> None:
        if self._server:
            self._server.close()

    async def wait_closed(self) -> None:
        if self._server:
            await self._server.wait_closed()

    def _response(self, request: bytes) -> bytes:
        parts = request.decode("latin-1").split()
        method = parts[0] if parts else ""
        path = parts[1].split("?")[0] if len(parts) > 1 else ""
        if method not in ("GET", "HEAD"):
            code, ctype, body = 405, "text/plain", b"method not allowed\n"
        elif path == "/status":
            code, ctype = 200, "application/json"
            body = json.dumps(self._status(), indent=2).encode() + b"\n"
        elif path == "/health":
            code, ctype = (200, "text/plain") if self._healthy() else (503, "text/plain")
            body = b"ok\n" if code == 200 else b"not serving\n"
        else:
            code, ctype, body = 404, "text/plain", b"not found\n"
        head = (f"HTTP/1.1 {code} {REASONS[code]}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                f"Cache-Control: no-store\r\nConnection: close\r\n\r\n").encode()
        return head + (b"" if method == "HEAD" else body)

    async def _conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._active >= MAX_CONNECTIONS:
            await _close(writer)
            return
        self._active += 1
        try:
            request = await asyncio.wait_for(_read_request(reader), DEADLINE)
            writer.write(self._response(request))
            await writer.drain()
        except (asyncio.TimeoutError, OSError, ValueError, UnicodeError):
            pass
        finally:
            self._active -= 1
            await _close(writer)
