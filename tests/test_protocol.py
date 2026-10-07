"""The link layer: framing, the reliable link over a lossy wire, wraparound, versions."""
import unittest

from audiolink.wire import protocol
from audiolink.wire.protocol import SYNC, T_DATA, Link, Parser, build_frame, seq_before

from .helpers import FakeClock, Wire, patch_time


class Framing(unittest.TestCase):
    def setUp(self):
        self.got = []
        self.stats = {"crc_fail": 0, "rx_frames": 0}
        self.p = Parser(lambda *f: self.got.append(f), self.stats)

    def frame(self, payload=b"hi", seq=0):
        return build_frame(1, 2, T_DATA, 3, seq, 0, payload)

    def test_finds_frames_in_noise_and_across_chunks(self):
        data = b"\x00\x13" * 50 + self.frame(b"one") + b"junk" + self.frame(b"two", 1)
        for i in range(0, len(data), 7):                 # SYNC and frames split anywhere
            self.p.feed(data[i:i + 7])
        self.assertEqual([f[-1] for f in self.got], [b"one", b"two"])

    def test_drops_corrupted_frame_and_resyncs(self):
        bad = bytearray(self.frame(b"one"))
        bad[-6] ^= 1
        self.p.feed(bytes(bad) + self.frame(b"two"))
        self.assertEqual([f[-1] for f in self.got], [b"two"])
        self.assertEqual(self.stats["crc_fail"], 1)

    def test_oversized_length_is_noise(self):
        hdr = bytearray(self.frame(b""))
        hdr[25:27] = (protocol.MAX_PAYLOAD + 1).to_bytes(2, "big")
        self.p.feed(bytes(hdr) + self.frame(b"ok"))
        self.assertEqual([f[-1] for f in self.got], [b"ok"])


class Sequence(unittest.TestCase):
    def test_wraps(self):
        self.assertTrue(seq_before(0xFFFFFFFF, 0))
        self.assertTrue(seq_before(5, 6))
        self.assertFalse(seq_before(6, 5))
        self.assertFalse(seq_before(7, 7))


class LinkPair(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.patch = patch_time(protocol, self.clock)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.rx = {"a": [], "b": []}
        self.a = Link(lambda *m: self.rx["a"].append(m), lambda *d: None, lambda: None)
        self.b = Link(lambda *m: self.rx["b"].append(m), lambda *d: None, lambda: None)

    def meet(self, **wire_opts):
        """Run the wire until both ends know each other (messages queued before that are
        dropped: meeting a new peer resets the link)."""
        wire = Wire(self.a, self.b, self.clock, **wire_opts)
        self.assertTrue(wire.run(5, until=lambda: self.a.peer == self.b.session and self.b.peer == self.a.session))
        return wire

    def payloads(self, side):
        return [m[2] for m in self.rx[side] if m[0] == T_DATA]

    def test_delivers_in_order_over_a_lossy_wire(self):
        sent = [bytes([i]) * 300 for i in range(200)]
        wire = self.meet(corrupt=0.05, drop=0.05)
        for p in sent:
            self.a.send(T_DATA, 7, p)
            wire.run(0.01)
        self.assertTrue(wire.run(30, until=lambda: len(self.payloads("b")) == len(sent)))
        self.assertEqual(self.payloads("b"), sent)

    def test_sequence_numbers_wrap(self):
        wire = self.meet()
        self.a.next_seq = self.b.expected = 0xFFFFFFFF - 3
        for i in range(10):
            self.a.send(T_DATA, 7, bytes([i]))
        self.assertTrue(wire.run(5, until=lambda: len(self.payloads("b")) == 10))
        self.assertEqual(self.payloads("b"), [bytes([i]) for i in range(10)])
        self.assertTrue(wire.run(2, until=lambda: not self.a.unacked))

    def test_unknown_message_type_does_not_jam_the_link(self):
        wire = self.meet()
        self.a.send(42, 0, b"from the future")          # a type this version doesn't know
        self.a.send(T_DATA, 7, b"after it")
        self.assertTrue(wire.run(5, until=lambda: self.payloads("b") == [b"after it"]))

    def test_refuses_oversized_messages(self):
        with self.assertRaises(ValueError):
            self.a.send(T_DATA, 7, bytes(protocol.MAX_PAYLOAD + 1))

    def test_notices_a_peer_on_another_protocol_version(self):
        other = bytearray(build_frame(9, 0, T_DATA, 0, 0, 0, b"x"))
        other[3] = SYNC[3] + 1
        self.clock.advance(10)
        self.a.feed(bytes(other) * 3)
        self.assertIsNone(self.a.other_version)         # once could be noise
        self.clock.advance(10)
        self.a.feed(bytes(other) * 3)
        self.assertEqual(self.a.other_version, SYNC[3] + 1)
        self.assertEqual(self.rx["a"], [])


if __name__ == "__main__":
    unittest.main()
