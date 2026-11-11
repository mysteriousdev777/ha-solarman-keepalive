"""Solarman V5 frames: splitting a TCP stream into frames, their fields, and the cloud's reply.

  A5 | length u16 LE | control | type | seq | seq | logger serial u32 LE | payload | sum | 15

Logger -> server frames have control 0x10; the reply type is the request type - 0x30.
No I/O here.
"""
from __future__ import annotations

import re
import struct

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
