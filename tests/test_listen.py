"""Listen before talking (wire/host_audio.hears_the_link): the host plays to a device
only once it has heard the Pi's link on its input."""
import os
import unittest

from audiolink.wire import host_audio, protocol
from audiolink.wire.fmt import BASELINE


class Input:
    """A PortAudio input stand-in: reads hand out `data`, a chunk at a time, then zeros."""

    def __init__(self, data):
        self.data = data

    def available(self):
        return BASELINE.chunk_frames

    def read(self, frames):
        n = frames * BASELINE.frame_bytes
        chunk, self.data = self.data[:n], self.data[n:]
        return chunk + bytes(n - len(chunk)), False


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.01                  # every look at the clock: 10 ms on
        return self.t


def listen(data):
    return host_audio.hears_the_link(Input(data), BASELINE, clock=Clock(), sleep=lambda s: None)


class Listen(unittest.TestCase):
    def test_the_pi(self):
        frame = protocol.build_frame(1234, 0, protocol.T_ACK, 0, 0, 0, b"")
        n = BASELINE.chunk_frames * BASELINE.frame_bytes
        self.assertTrue(listen(bytes(n * 3) + frame))                          # after some silence
        self.assertTrue(listen(bytes(n - 10) + frame))                         # cut across two reads

    def test_not_the_pi(self):
        self.assertFalse(listen(bytes(1 << 20)))                               # silence
        self.assertFalse(listen(os.urandom(1 << 20)))                          # a microphone, noise
        frame = bytearray(protocol.build_frame(1234, 0, protocol.T_ACK, 0, 0, 0, b""))
        frame[-1] ^= 1                                                         # a damaged frame isn't the link
        self.assertFalse(listen(bytes(frame)))


if __name__ == "__main__":
    unittest.main()
