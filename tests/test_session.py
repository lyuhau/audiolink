"""Two sessions (host and Pi) over an in-memory wire, in real time (fast: no audio)."""
import itertools
import threading
import time
import unittest

from audiolink.services import Service
from audiolink.session import Session
from audiolink.wire.protocol import T_HELLO

from .helpers import wait


class Echo(Service):
    name = "echo"
    caps = ("echo",)

    def handle(self, op, args):
        if op == "back":
            return args
        if op == "boom":
            raise ValueError("boom")
        return super().handle(op, args)


class Pair(unittest.TestCase):
    def setUp(self):
        self.host = Session("host", [])
        self.pi = Session("pi", [Echo()])
        self.running = True
        threading.Thread(target=self._pump, daemon=True).start()
        self.addCleanup(setattr, self, "running", False)
        self.assertTrue(wait(lambda: self.host.peer_alive and self.host.peer_has("echo")))

    def _pump(self):
        while self.running:
            self.pi.link.feed(self.host.link.pull(4096))
            self.host.link.feed(self.pi.link.pull(4096))
            time.sleep(0.001)

    def test_request_and_reply(self):
        self.assertEqual(self.host.request("echo.back", {"x": 1}, timeout=5), {"x": 1})

    def test_large_request_is_fragmented(self):
        big = {"data": "x" * 20000}
        self.assertEqual(self.host.request("echo.back", big, timeout=10), big)

    def test_errors_come_back_as_errors(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.host.request("echo.boom", timeout=5)
        with self.assertRaisesRegex(RuntimeError, "does not support"):
            self.host.request("nosuch.op", timeout=5)

    def test_a_bad_message_does_not_stop_the_receiver(self):
        self.host.link.send(T_HELLO, 0, b"{not json")
        self.assertEqual(self.host.request("echo.back", {"still": "working"}, timeout=5), {"still": "working"})

    def test_request_ids_stay_16_bit(self):
        self.host.req_ids = itertools.count(0xFFFF)
        for i in range(3):                              # 0xFFFF, then wraps (0 is skipped)
            self.assertEqual(self.host.request("echo.back", {"i": i}, timeout=5), {"i": i})


if __name__ == "__main__":
    unittest.main()
