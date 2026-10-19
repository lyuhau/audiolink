"""TCP streams through two sessions over an in-memory wire: data arrives intact, flow
control keeps buffers bounded when the far end reads slowly, clients are counted."""
import hashlib
import os
import socket
import threading
import time
import unittest

from unittest import mock

from audiolink.services import streams
from audiolink.services.streams import WINDOW, Streams
from audiolink.session import Session

from .helpers import wait


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """A TCP server on the host's side. mode: "echo"; "sink" (reads only once
    reading is set); "early" (reads a little, then hangs up)."""

    def __init__(self, mode):
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.mode, self.reading, self.received = mode, threading.Event(), 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        c, _ = self.srv.accept()
        self.echo = self.mode == "echo"
        with c:
            if self.mode == "sink":
                self.reading.wait()
            if self.mode == "early":
                c.recv(10)
                return
            while True:
                data = c.recv(65536)
                if not data:
                    return
                self.received += len(data)
                if self.echo:
                    c.sendall(data)


class Through:
    """Setup shared by the test cases below (not a test case itself)."""
    server_mode = "echo"

    def setUp(self):
        self.server = Server(self.server_mode)
        self.addCleanup(self.server.srv.close)
        target = "127.0.0.1:%d" % self.server.port
        self.host_streams = Streams("host", allow=[target])
        self.pi_streams = Streams("pi")
        self.host = Session("host", [self.host_streams])
        self.pi = Session("pi", [self.pi_streams])
        self.running = True
        threading.Thread(target=self._pump, daemon=True).start()
        self.addCleanup(setattr, self, "running", False)
        self.assertTrue(wait(lambda: self.pi.peer_has("stream_credit") and self.host.peer_has("stream_credit")))
        self.port = free_port()
        self.pi_streams.forward(self.port, ("127.0.0.1", self.server.port), bind="127.0.0.1")
        self.addCleanup(self.pi_streams._unlisten, self.port)

    def _pump(self):
        while self.running:
            self.pi.link.feed(self.host.link.pull(8192))
            self.host.link.feed(self.pi.link.pull(8192))
            time.sleep(0.0005)

class Echoing(Through, unittest.TestCase):
    def test_data_arrives_intact(self):
        payload = os.urandom(300_000)
        with socket.create_connection(("127.0.0.1", self.port)) as c:
            threading.Thread(target=c.sendall, args=(payload,), daemon=True).start()
            got = bytearray()
            c.settimeout(20)
            while len(got) < len(payload):
                got += c.recv(65536)
        self.assertEqual(hashlib.sha256(got).digest(), hashlib.sha256(payload).digest())

class SlowReader(Through, unittest.TestCase):
    server_mode = "sink"

    def test_slow_reader_keeps_buffers_bounded(self):
        with socket.create_connection(("127.0.0.1", self.port)) as c:
            c.setblocking(False)
            sent = 0
            end = time.monotonic() + 3
            while time.monotonic() < end:           # push far more than a window
                try:
                    sent += c.send(bytes(65536))
                except BlockingIOError:
                    time.sleep(0.01)
            self.assertGreater(sent, WINDOW)
            queued = sum(sum(len(p) for p in list(st.wq.queue) if p) for st in self.host_streams.streams.values())
            self.assertLessEqual(queued, WINDOW + 8192)   # the host holds at most a window
            self.server.reading.set()                     # the far end starts reading: all of it arrives
            self.assertTrue(wait(lambda: self.server.received >= sent, timeout=20))


class FarEndClosesEarly(Through, unittest.TestCase):
    server_mode = "early"

    def test_stream_ends_when_the_client_closes(self):
        with socket.create_connection(("127.0.0.1", self.port)) as c:
            c.settimeout(10)
            try:
                c.sendall(bytes(2 * WINDOW))           # far more than the far end ever reads
            except OSError:
                pass                                    # the Pi may hang up on us first
        self.assertTrue(wait(lambda: not self.pi_streams.streams, timeout=10))



class Listening(Through, unittest.TestCase):
    def echoes(self):
        with socket.create_connection(("127.0.0.1", self.port)) as c:
            c.settimeout(5)
            c.sendall(b"ping")
            return c.recv(10) == b"ping"

    def refused(self):
        with socket.create_connection(("127.0.0.1", self.port)) as c:
            c.settimeout(5)
            try:
                return c.recv(10) == b""
            except ConnectionResetError:
                return True

    def test_connections_per_listener_are_capped(self):
        with mock.patch.object(streams, "PER_LISTENER", 1):
            c = socket.create_connection(("127.0.0.1", self.port))
            self.addCleanup(c.close)
            c.settimeout(5)
            c.sendall(b"one")
            self.assertEqual(c.recv(10), b"one")
            self.assertTrue(self.refused())

    def test_out_of_threads_keeps_listening(self):
        real, fails, reader = threading.Thread, [1], self.pi_streams._reader

        class Thread(real):
            def start(self):
                if fails and self._target == reader:
                    fails.pop()
                    raise RuntimeError("can't start new thread")
                super().start()
        with mock.patch.object(streams.threading, "Thread", Thread):
            self.assertTrue(self.refused())
            c = socket.create_connection(("127.0.0.1", self.port))
            self.addCleanup(c.close)
            self.assertTrue(wait(lambda: self.pi_streams.streams))     # still listening


if __name__ == "__main__":
    unittest.main()
