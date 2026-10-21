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


class Directions(unittest.TestCase):
    """Each direction in its own format: a device cloned from headphones with a mono mic."""

    def test_device_formats(self):
        from audiolink.wire.fmt import AudioFormat
        out = {"rate": 48000.0, "outputs": 2}
        inp = {"rate": 48000.0, "inputs": 1}
        self.assertEqual(host_audio.device_formats(out, inp), (AudioFormat(48000, 2), AudioFormat(48000, 1)))

    def test_a_mono_microphone_is_picked(self):
        from unittest import mock
        devs = [{"name": "Arctis GameBuds", "outputs": 2, "inputs": 0, "hostapi_type": 5, "rate": 48000.0},
                {"name": "Arctis GameBuds", "outputs": 0, "inputs": 1, "hostapi_type": 5, "rate": 48000.0}]
        with mock.patch("audiolink.wire.portaudio.devices", lambda: [dict(d) for d in devs]), \
                mock.patch("audiolink.wire.usb_audio._lookup", lambda: (lambda d: (0x1038, 0x230a))), \
                mock.patch("audiolink.wire.usb_audio.known_ids", lambda: {(0x1038, 0x230a)}):
            out, inp = host_audio.find_devices()
        self.assertEqual((out["outputs"], inp["inputs"]), (2, 1))

    def test_pi_plays_and_records_in_its_own(self):
        from audiolink.wire import alsa
        from audiolink.wire.fmt import AudioFormat

        class Link:
            def set_format(self, fmt):
                self.fmt = fmt
        a = alsa.AlsaAudio(Link())
        a.start = alsa.AlsaAudio.start.__get__(a)
        orig = alsa.threading.Thread
        alsa.threading.Thread = lambda **kw: type("T", (), {"start": lambda self: None})()
        try:
            a.start(AudioFormat(48000, 1), AudioFormat(48000, 2))      # sends mono, receives stereo
        finally:
            alsa.threading.Thread = orig
        self.assertEqual(a.link.fmt, AudioFormat(48000, 1))            # the link: what the Pi sends
        self.assertIn("1", a._args(True)[a._args(True).index("-c") + 1])
        self.assertEqual(a._args(False)[a._args(False).index("-c") + 1], "2")
        alsa.threading.Thread = lambda **kw: type("T", (), {"start": lambda self: None})()
        try:
            a.start(AudioFormat(96000, 4))                              # a switch: both the same again
        finally:
            alsa.threading.Thread = orig
        self.assertEqual(a._args(False)[a._args(False).index("-r") + 1], "96000")


if __name__ == "__main__":
    unittest.main()
