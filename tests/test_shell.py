"""services/shell.py: the pty shell server, and network shells through the endpoint."""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from audiolink.client.pairing import PiConn
from audiolink.crypto import ed25519_public, ed25519_sign
from audiolink.services.access import PAIRED, DebugService
from audiolink.services.endpoint import Endpoint, key_message
from audiolink.services.identity import IdentityService
from audiolink.services.shell import (F_CLOSED, F_DATA, F_EOF, F_ERR, F_EXIT, F_SIZE, MAX_CMD, NetworkShells,
                                      ShellServer, frame, header)

from .helpers import needs_tls, wait


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
        self.nonce = bytes.fromhex(self.hello["nonce"])



@unittest.skipIf(sys.platform == "win32", "no pty")
@needs_tls
@unittest.skip("roles-v1: needs the v2 Pi side")
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
        self.assertTrue(wait(lambda: self.ep.active == 0))     # the shells hold no request slots
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

    def test_narrowing_debug_closes_them(self):
        seed = os.urandom(32)
        k1, k2 = ed25519_public(seed).hex(), ed25519_public(os.urandom(32)).hex()
        for before, after, closes in (
                ({"mode": "none"}, {"mode": "none", "for": 7200}, False),           # longer: stays
                ({"mode": "key", "keys": [k1]}, {"mode": "key", "keys": [k1, k2]}, False),
                ({"mode": "none"}, {"mode": "paired"}, True),
                ({"mode": "none", "setup": True}, {"mode": "none"}, True),
                ({"mode": "key", "keys": [k1, k2]}, {"mode": "key", "keys": [k1]}, True)):
            self.debug.set(before)
            c = self.client()
            req = {"op": "shell"}
            if before["mode"] == "key":
                req.update(key=k1, sig=ed25519_sign(seed, key_message("shell", c.nonce, c.pub)).hex())
            self.assertTrue(c.ask(req)["ok"], before)
            self.assertTrue(wait(lambda: self.shells.count() == 1))
            self.debug.set(after)
            self.assertEqual(wait(lambda: self.shells.count() == 0, 0.5), closes, (before, after))
            self.shells.close_all("next")
            self.assertTrue(wait(lambda: self.server.slots._value == 6, 5))

    def test_a_new_read_secret_closes_them(self):
        self.debug.set({"mode": "none"})
        c, r = self.shell()
        self.assertTrue(wait(lambda: self.shells.count() == 1))
        self.identity.handle("rotate_secret", {})
        self.assertTrue(wait(lambda: self.shells.count() == 0))

    def test_framed_over_the_network(self):
        self.debug.set({"mode": "none"})
        c = self.client()
        r = c.ask({"op": "shell", "v": 2, "cmd": "cat; exit 5"})
        self.assertEqual((r["ok"], r["v"]), (True, 2))
        c.sock.sendall(frame(F_DATA, b"through\n") + frame(F_EOF))
        got, order = frames_until_end(lambda: c.f.read1(4096))
        self.assertEqual(got[F_DATA], b"through\n")
        self.assertEqual(json.loads(got[F_EXIT]), {"status": 5})

    def test_a_framed_shell_hears_why_it_was_closed(self):
        self.debug.set({"mode": "none"})
        c = self.client()
        self.assertEqual(c.ask({"op": "shell", "v": 2})["v"], 2)
        self.assertTrue(wait(lambda: self.shells.count() == 1))
        self.debug.off("expired")
        got, order = frames_until_end(lambda: c.f.read1(4096))
        self.assertEqual(order[-1], F_CLOSED)
        self.assertEqual(json.loads(got[F_CLOSED]), {"why": "debug access ended"})
        self.assertTrue(wait(lambda: self.server.slots._value == 6, 5))

    def test_old_clients_get_the_bare_terminal(self):
        self.debug.set({"mode": "none"})
        c = self.client()
        self.assertEqual(c.ask({"op": "shell"})["v"], 1)
        c.sock.sendall(b"echo hi''ya; exit\n")
        self.assertIn("hiya", read_all(lambda: c.f.read1(4096)))


def frames_until_end(read, timeout=10):
    """{kind: bytes joined} and the order kinds came in, until EOF."""
    from audiolink.services.shell import Frames
    f, got, order = Frames(), {}, []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            data = read()
        except socket.timeout:
            continue
        except OSError:
            break
        if not data:
            break
        for kind, payload in f.feed(data):
            got[kind] = got.get(kind, b"") + payload
            order.append(kind)
    return got, order


@unittest.skipIf(sys.platform == "win32", "no pty")
class Framed(unittest.TestCase):
    """Version 2: frames both ways, the exit status, resizing, and `-- CMD`."""

    def setUp(self):
        self.server = ShellServer(port=0, argv=["/bin/sh"]).run()
        self.addCleanup(self.server.srv.close)

    def connect(self, **kw):
        s = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
        self.addCleanup(s.close)
        s.sendall(header(24, 80, v=2, **kw))
        return s

    def test_shell_ends_with_its_exit_status(self):
        s = self.connect()
        s.sendall(frame(F_SIZE, {"rows": 50, "cols": 132}))
        s.sendall(frame(F_DATA, b"stty size; exit 7\n"))
        got, order = frames_until_end(lambda: s.recv(4096))
        self.assertIn(b"50 132", got[F_DATA])                 # the resize took
        self.assertEqual(order[-1], F_EXIT)
        self.assertEqual(json.loads(got[F_EXIT]), {"status": 7})
        wait(lambda: self.server.slots._value == 6)

    def test_a_command_on_pipes(self):
        s = self.connect(cmd="read x; echo got $x; echo oops >&2; [ -t 0 ] || echo no-tty; exit 3")
        s.sendall(frame(F_DATA, b"hello\n") + frame(F_EOF))
        got, order = frames_until_end(lambda: s.recv(4096))
        self.assertEqual(got[F_DATA], b"got hello\nno-tty\n")
        self.assertEqual(got[F_ERR], b"oops\n")
        self.assertEqual(order[-1], F_EXIT)
        self.assertEqual(json.loads(got[F_EXIT]), {"status": 3})

    def test_a_command_reading_to_the_end_of_input(self):
        s = self.connect(cmd="wc -l")
        s.sendall(frame(F_DATA, b"a\nb\n") + frame(F_DATA, b"c\n") + frame(F_EOF))
        got, _ = frames_until_end(lambda: s.recv(4096))
        self.assertEqual(got[F_DATA].strip(), b"3")
        self.assertEqual(json.loads(got[F_EXIT]), {"status": 0})

    def test_a_command_stops_when_the_client_goes(self):
        s = self.connect(cmd="echo $$; exec sleep 30")
        s.settimeout(5)
        from audiolink.services.shell import Frames
        data = b""
        while b"\n" not in data:
            data += s.recv(4096)
        pid = int([p for k, p in Frames().feed(data) if k == F_DATA][0])
        s.close()
        self.assertTrue(wait(lambda: not alive_any(pid), 5))
        self.assertTrue(wait(lambda: self.server.slots._value == 6, 5))

    def test_bad_commands_are_refused(self):
        for cmd in ("", 5, "x" * (MAX_CMD + 1)):
            s = self.connect(cmd=cmd)
            self.assertEqual(read_all(lambda: s.recv(4096), timeout=5), "")


def alive_any(pid):
    """Is pid running (not necessarily our child)?"""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@unittest.skipIf(sys.platform == "win32", "no pty")
class ClientSide(unittest.TestCase):
    """client/shell.py framed(): output to stdout and stderr, input up, the status."""

    def test_framed(self):
        from audiolink.client import shell as client
        here, there = socket.socketpair()
        self.addCleanup(here.close)
        self.addCleanup(there.close)
        rin, win = os.pipe()
        os.write(win, b"typed\n")
        os.close(win)
        rout, wout = os.pipe()
        rerr, werr = os.pipe()
        stdin, stdout, stderr = (mock.Mock(**{"fileno.return_value": fd}) for fd in (rin, wout, werr))
        result = []
        with mock.patch.object(client.sys, "stdin", stdin), mock.patch.object(client.sys, "stdout", stdout), \
                mock.patch.object(client.sys, "stderr", stderr):
            t = threading.Thread(target=lambda: result.append(client.framed(here, here.makefile("rb"), False)))
            t.start()
            from audiolink.services.shell import Frames
            f, up = Frames(), []
            there.settimeout(5)
            while not up or up[-1][0] != F_EOF:        # what was typed, then the end of input
                up += f.feed(there.recv(4096))
            there.sendall(frame(F_DATA, b"out") + frame(F_ERR, b"err") + frame(F_EXIT, {"status": 4}))
            there.shutdown(socket.SHUT_WR)
            t.join(5)
        self.assertEqual(b"".join(d for k, d in up if k == F_DATA), b"typed\n")
        self.assertEqual(result, [(4, None)])
        os.close(wout)
        os.close(werr)
        self.assertEqual(os.read(rout, 100), b"out")
        self.assertEqual(os.read(rerr, 100), b"err")
        for fd in (rin, rout, rerr):
            os.close(fd)

    def test_closed_by_the_pi(self):
        from audiolink.client import shell as client
        here, there = socket.socketpair()
        self.addCleanup(there.close)
        there.sendall(frame(F_CLOSED, {"why": "debug access ended"}))
        there.shutdown(socket.SHUT_WR)
        rin, win = os.pipe()
        self.addCleanup(os.close, rin)
        self.addCleanup(os.close, win)
        with mock.patch.object(client.sys, "stdin", mock.Mock(**{"fileno.return_value": rin})):
            self.assertEqual(client.framed(here, here.makefile("rb"), interactive=False),
                             (None, "debug access ended"))

    def test_argv_after_dashes_is_the_command(self):
        from audiolink import __main__
        seen = {}
        with mock.patch("audiolink.client.shell.shell_command", lambda a: seen.update(vars(a)) or 0):
            __main__.main(["shell", "mypi", "--", "ls", "-l"])
            self.assertEqual((seen["pi"], seen["command"]), ("mypi", ["ls", "-l"]))
            __main__.main(["shell"])
            self.assertIsNone(seen["command"])
