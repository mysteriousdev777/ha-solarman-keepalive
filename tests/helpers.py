"""Synthetic logger frames and a fake logger for the tests. No real device data here:
the serial number, addresses and texts are made up."""
from __future__ import annotations

import asyncio
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "solarman_keepalive"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from keepalive.config import Config  # noqa: E402

SN = 0x12345678                 # 305419896
TOTAL_WORKING = 0x032AD0D6


def frame(typ: int, payload: bytes, s5: int = 0x12, s6: int = 0x34, ctl: int = 0x10, sn: int = SN) -> bytes:
    f = bytearray([0xA5]) + struct.pack("<H", len(payload)) + bytes([ctl, typ, s5, s6]) + struct.pack("<I", sn)
    f += payload + b"\x00\x15"
    f[-2] = sum(f[1:-2]) & 0xFF
    return bytes(f)


def handshake(up: int = 34, s5: int = 0x12, text: bytes = b"LSW3_TEST_0.01|n192.0.2.77|TESTNET1") -> bytes:
    p = b"\x02" + struct.pack("<I", TOTAL_WORKING) + struct.pack("<I", up) + b"\x00" * 4 + b"\x05\x3c" + text
    return frame(0x41, p.ljust(212, b"\x00"), s5=s5)


def live(up: int, flag: int = 0x01, s5: int = 0x20, s6: int = 0x34) -> bytes:
    p = bytes([flag, 0x07, 0x54]) + struct.pack("<I", TOTAL_WORKING + up) + struct.pack("<I", up)
    p += struct.pack("<I", 0x67C2ECE2) + b"\x01" + b"\x00" * 4
    return frame(0x42, p.ljust(300, b"\x00"), s5=s5, s6=s6)


def wifi() -> bytes:
    return frame(0x43, (b"\x81" + b"\x00" * 14 + b"TESTNET1").ljust(47, b"\x00"))


def heartbeat(s5: int = 0xFF, s6: int = 0x34) -> bytes:
    return frame(0x47, b"\x00", s5=s5, s6=s6)


def report(up: int) -> bytes:
    return frame(0x48, (b"\x01" + struct.pack("<I", TOTAL_WORKING) + struct.pack("<I", up)).ljust(60, b"\xff"))


def valid_ack(b: bytes) -> bool:
    return (len(b) == 23 and b[0] == 0xA5 and b[-1] == 0x15 and b[-2] == sum(b[1:-2]) & 0xFF
            and b[1:3] == b"\x0a\x00" and b[4] in (0x11, 0x12, 0x13, 0x17, 0x18))


def ack_time(b: bytes) -> int:
    return struct.unpack_from("<I", b, 13)[0]


class FakeLogger:
    """Connects like a logger, sends frames and reads 23-byte replies."""

    async def connect(self, port: int, host: str = "127.0.0.1") -> "FakeLogger":
        self.reader, self.writer = await asyncio.open_connection(host, port)
        return self

    async def send(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def reply(self, timeout: float = 2.0) -> bytes:
        return await asyncio.wait_for(self.reader.readexactly(23), timeout)

    async def no_reply(self, wait: float = 0.3) -> bool:
        try:
            data = await asyncio.wait_for(self.reader.read(100), wait)
        except asyncio.TimeoutError:
            return True
        return data == b""

    async def closed_by_server(self, timeout: float = 2.0) -> bool:
        try:
            return await asyncio.wait_for(self.reader.read(100), timeout) == b""
        except ConnectionError:
            return True                 # closed with a reset: still closed by the server
        except asyncio.TimeoutError:
            return False

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except OSError:
            pass


def config(**kw) -> Config:
    base = dict(time="utc", host="127.0.0.1", port=0, status_port=0, idle_timeout=15)
    base.update(kw)
    return Config(**base).validate()


async def http_get(port: int, path: str, method: str = "GET") -> tuple[int, bytes]:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"{method} {path} HTTP/1.1\r\nHost: test\r\n\r\n".encode())
    await w.drain()
    data = await asyncio.wait_for(r.read(), 5)
    w.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), body
