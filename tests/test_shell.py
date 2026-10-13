"""services/shell.py: the pty shell server, and network shells through the endpoint."""
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest

from audiolink.client.pairing import PiConn
from audiolink.services.access import PAIRED, DebugService
from audiolink.services.endpoint import Endpoint
from audiolink.services.identity import IdentityService
from audiolink.services.shell import NetworkShells, ShellServer, header

from .helpers import wait


def read_all(read, timeout=10):
    """Everything until EOF (or timeout), as text."""
    out, end = b"", time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            data = read()
        except socket.timeout:
            continue
        except OSError:
            break
        if not data:
            break
        out += data
    return out.decode("utf-8", "replace")


@unittest.skipIf(sys.platform == "win32", "no pty")
class Server(unittest.TestCase):
    def setUp(self):
        self.server = ShellServer(port=0, argv=["/bin/sh"]).run()
        self.addCleanup(self.server.srv.close)

    def connect(self):
        s = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
        self.addCleanup(s.close)
        return s

    def test_runs_a_command_and_ends_with_the_shell(self):
        s = self.connect()
        s.sendall(header(30, 100) + b"echo hel''lo $((6*7)); stty size; exit\n")
        out = read_all(lambda: s.recv(4096))
        self.assertIn("hello 42", out)
        self.assertIn("30 100", out)                    # the window size took
        wait(lambda: self.server.slots._value == 6)     # torn down, slot back

    def test_hangs_up_when_the_connection_closes(self):
        s = self.connect()
        s.sendall(header() + b"echo $$\n")
        time.sleep(0.5)
        pid = int([l for l in read_some(s).split() if l.strip().isdigit()][-1])
        s.close()
        self.assertTrue(wait(lambda: not alive(pid), 5))


def read_some(s):
    s.settimeout(1)
    out = b""
    try:
        while True:
            data = s.recv(4096)
            if not data:
                break
            out += data
    except socket.timeout:
        pass
    return out.decode("utf-8", "replace")


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:                                                # a zombie isn't alive
        return os.waitpid(pid, os.WNOHANG)[0] == 0
    except ChildProcessError:
        return True


class Client(PiConn):
    """A client of the endpoint over TLS (client/pairing.py)."""

    def __init__(self, port):
        super().__init__("127.0.0.1", port=port)
        self.hello = super().hello()



@unittest.skipIf(sys.platform == "win32", "no pty")
class OverTheNetwork(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.server = ShellServer(port=0, argv=["/bin/sh"]).run()
        self.addCleanup(self.server.srv.close)
        self.identity = IdentityService(self.dir)
        self.identity.claim()
        self.debug = DebugService()
        self.ep = Endpoint(self.identity, port=0, debug=self.debug, wifi_ip=lambda: "127.0.0.1", bind="127.0.0.1")
        self.ep.register("look", lambda conn, req: {"ok": True}, PAIRED)
        self.shells = NetworkShells(self.ep, self.debug, self.identity, port=self.server.port)

    def client(self):
        c = Client(self.ep.port)
        self.addCleanup(c.close)
        return c

    def shell(self):
        c = self.client()
        r = c.ask({"op": "shell", "rows": 40, "cols": 120})
        return c, r

    def test_refused_while_debug_is_off(self):
        self.assertEqual(self.shell()[1]["code"], "debug_off")

    def test_runs_and_the_endpoint_timeout_no_longer_applies(self):
        self.debug.set({"mode": "none"})
        c, r = self.shell()
        self.assertTrue(r["ok"])
        self.assertEqual(self.debug.status()["shells"], 1)
        c.sock.sendall(b"echo hel''lo; stty size; exit\n")
        out = read_all(lambda: c.f.read1(4096))
        self.assertIn("hello", out)
        self.assertIn("40 120", out)
        self.assertTrue(wait(lambda: self.shells.count() == 0))

    def test_two_at_most_and_they_dont_hold_request_slots(self):
        self.debug.set({"mode": "none"})
        open_ = [self.shell() for _ in range(2)]
        self.assertTrue(all(r.get("ok") for _, r in open_))
        busy, r = self.shell()
        self.assertEqual(r["code"], "busy")
        busy.close()
        self.assertTrue(wait(lambda: self.ep.slots._value == 4))
        others = [self.client() for _ in range(4)]           # the endpoint's 4 slots are all free
        self.assertTrue(all(o.hello.get("nonce") for o in others))
        for o in others:
            o.close()
        for c, _ in open_:
            c.sock.sendall(b"exit\n")
        self.assertTrue(wait(lambda: self.shells.count() == 0))
        self.assertTrue(self.shell()[1].get("ok"))

    def test_debug_off_and_expiry_close_them(self):
        self.debug.set({"mode": "none"})
        c, r = self.shell()
        self.assertTrue(r["ok"])
        self.assertTrue(wait(lambda: self.shells.count() == 1))
        self.debug.off("test")
        read_all(lambda: c.f.read1(4096), timeout=5)       # ends: EOF
        self.assertTrue(wait(lambda: self.shells.count() == 0))
        self.assertTrue(wait(lambda: self.server.slots._value == 6, 5))

    def test_a_new_read_secret_closes_them(self):
        self.debug.set({"mode": "none"})
        c, r = self.shell()
        self.assertTrue(wait(lambda: self.shells.count() == 1))
        self.identity.handle("rotate_secret", {})
        self.assertTrue(wait(lambda: self.shells.count() == 0))
