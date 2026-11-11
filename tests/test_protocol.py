import os
import struct
import unittest

from helpers import SN, frame, handshake, heartbeat, live, report, valid_ack, wifi
from keepalive.protocol import FOOTER, HEADER, MAX_PAYLOAD, FrameReader, ack, describe, is_live, is_request, uptime


class AckTest(unittest.TestCase):
    def test_matches_dummycloud(self):
        """Byte for byte the reply of Hypfer's dummycloud (buildTimeResponse)."""
        hb = heartbeat(s5=0xFF, s6=0x34)
        t = 1762300000
        exp = bytearray(b"\xa5\x0a\x00\x10\x17\x00\x34") + struct.pack("<I", SN) + b"\x00\x01"
        exp += struct.pack("<I", t) + b"\x00" * 4 + b"\x00\x15"
        exp[21] = sum(exp[1:21]) & 0xFF
        self.assertEqual(ack(hb, t), bytes(exp))

    def test_each_type(self):
        for req, reply_type in ((handshake(), 0x11), (live(100), 0x12), (wifi(), 0x13),
                                (heartbeat(), 0x17), (report(100), 0x18)):
            a = ack(req, 1762300000)
            self.assertTrue(valid_ack(a))
            self.assertEqual(a[4], reply_type)
            self.assertEqual(a[3], 0x10)
            self.assertEqual(a[5], (req[5] + 1) & 0xFF)
            self.assertEqual(a[6], req[6])
            self.assertEqual(a[7:11], req[7:11])
            self.assertEqual(a[11], req[11], "first payload byte echoed")
            self.assertEqual(a[12], 0x01)

    def test_sequence_wraps(self):
        self.assertEqual(ack(heartbeat(s5=0xFF), 0)[5], 0x00)


class FrameReaderTest(unittest.TestCase):
    def test_garbage_glued_split_and_broken(self):
        hs, data, hb = handshake(), live(100), heartbeat()
        bad = bytearray(heartbeat())
        bad[-2] ^= 0xFF
        r = FrameReader()
        self.assertEqual(r.feed(b"\x00\x01garbage" + hs + data + bytes(bad) + hb[:5]), [hs, data])
        self.assertEqual(r.feed(hb[5:]), [hb])
        self.assertEqual(r.buf, bytearray())
        self.assertTrue(any("skip 9 byte(s)" in n for n in r.notes))
        self.assertTrue(any("bad end or checksum" in n for n in r.notes))

    def test_one_byte_at_a_time(self):
        hs = handshake()
        r = FrameReader()
        got = []
        for b in hs:
            got += r.feed(bytes([b]))
        self.assertEqual(got, [hs])

    def test_huge_length_resyncs(self):
        hb = heartbeat()
        r = FrameReader()
        self.assertEqual(r.feed(b"\xa5\xff\xff\x10\x47" + hb), [hb])
        self.assertTrue(any("bad length 65535" in n for n in r.notes))

    def test_buffer_stays_bounded(self):
        r = FrameReader()
        r.feed(os.urandom(100_000).replace(b"\xa5", b"\x00"))
        self.assertEqual(len(r.buf), 0)
        # a frame start claiming the longest allowed payload, then endless bytes without its end
        r.feed(b"\xa5" + struct.pack("<H", MAX_PAYLOAD) + b"\x10\x42" + b"\x00" * 50_000)
        self.assertLess(len(r.buf), HEADER + MAX_PAYLOAD + FOOTER)


class FieldsTest(unittest.TestCase):
    def test_request_types(self):
        self.assertTrue(is_request(heartbeat()))
        self.assertFalse(is_request(frame(0x49, b"\x00")), "unknown type")
        self.assertFalse(is_request(frame(0x47, b"\x00", ctl=0x20)), "not a logger request")

    def test_uptime(self):
        self.assertEqual(uptime(handshake(up=34)), 34)
        self.assertEqual(uptime(live(5882)), 5882)
        self.assertEqual(uptime(report(77)), 77)
        self.assertIsNone(uptime(live(5882, flag=0x81)), "stored data: no current uptime")
        self.assertIsNone(uptime(heartbeat()))
        self.assertIsNone(uptime(frame(0x42, b"\x01\x07")), "too short")

    def test_live(self):
        self.assertTrue(is_live(live(10)))
        self.assertFalse(is_live(live(10, flag=0x81)))
        self.assertFalse(is_live(report(10)))

    def test_describe(self):
        line = describe(handshake())
        self.assertTrue(line.startswith("<- 0x41 handshake ctl=10 seq=12/34 sn=305419896 len=212 head=02 "))
        self.assertIn("text=", line)


if __name__ == "__main__":
    unittest.main()
