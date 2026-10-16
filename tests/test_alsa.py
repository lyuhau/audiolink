"""The Pi's aplay/arecord loops (wire/alsa.py), with stand-ins for the processes."""
import collections
import time
import unittest
from unittest import mock

from audiolink.wire import alsa


class FakeLink:
    def __init__(self):
        self.stats = collections.Counter()
        self.fed = 0

    def set_format(self, fmt):
        pass

    def pull(self, n):
        return bytes(n)

    def feed(self, data):
        self.fed += 1


class Host:
    """The other end of the gadget. While it streams, aplay's writes and arecord's reads
    go through (a chunk every few ms); while it doesn't, both processes exit."""

    def __init__(self):
        self.streaming = False
        self.spawned = collections.Counter()

    def write(self, b):
        if not self.streaming:
            raise BrokenPipeError()
        time.sleep(0.003)
        return len(b)

    def read(self, n):
        if not self.streaming:
            return b""
        time.sleep(0.003)
        return b"x" * n

    def popen(self, cmd, **kw):
        self.spawned[cmd[0]] += 1
        return mock.Mock(stdin=mock.Mock(write=self.write, fileno=lambda: -1),
                         stdout=mock.Mock(read=self.read))


def until(cond, timeout=2):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.01)
    return cond()


class Loops(unittest.TestCase):
    def setUp(self):
        self.host = Host()
        for patch in (mock.patch.object(alsa.subprocess, "Popen", self.host.popen),
                      mock.patch.object(alsa, "RETRY", 0.05), mock.patch.object(alsa, "IDLE_RETRY", 0.4),
                      mock.patch.object(alsa, "WAKE_CHECK", 0.01),
                      mock.patch("fcntl.fcntl", lambda *a: None)):
            patch.start()
            self.addCleanup(patch.stop)
        self.link = FakeLink()
        self.audio = alsa.AlsaAudio(self.link)
        self.audio.format = mock.Mock(chunk=64, rate=48000, channels=2)
        self.addCleanup(self.audio.stop)

    def test_waiting_for_the_host_isnt_a_fault(self):
        stats = self.link.stats
        self.audio.start(self.audio.format)
        time.sleep(1.0)
        self.assertEqual(stats["aplay_restarts"] + stats["arecord_restarts"], 0)
        self.assertLess(self.host.spawned["aplay"], self.host.spawned["arecord"] / 2)   # backed off
        self.host.streaming = True
        spawned = self.host.spawned["aplay"]
        self.assertTrue(until(lambda: self.host.spawned["aplay"] > spawned, timeout=0.15),
                        "aplay is back soon after the host streams")
        self.assertTrue(until(lambda: self.link.fed > 10))
        self.assertEqual(stats["aplay_restarts"] + stats["arecord_restarts"], 0)
        self.host.streaming = False                 # the host goes away: a fault, once each
        self.assertTrue(until(lambda: stats["aplay_restarts"] and stats["arecord_restarts"]))
        time.sleep(0.5)
        self.assertEqual((stats["aplay_restarts"], stats["arecord_restarts"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
