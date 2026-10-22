"""The relay server, end to end over loopback TLS: registration, joining, refusals."""
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

from audiolink import tls
from audiolink.crypto import ed25519_public, ed25519_sign
from audiolink.relay import wire
from audiolink.relay.server import Relay, ask_status
from audiolink.relay.wire import REGISTER, VERSION, Control, Refused, answer, read_line, send_line

from tests.helpers import EndpointClient, needs_tls, wait


def make_cert(d):
    cert, key = os.path.join(d, "relay.crt"), os.path.join(d, "relay.key")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                    "-nodes", "-keyout", key, "-out", cert, "-days", "2", "-subj", "/CN=test"],
                   check=True, capture_output=True)
    return cert, key


def pin_of(cert):
    import hashlib
    with open(cert) as f:
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(f.read())).hexdigest()


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a test certificate")
class RelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.cert, cls.key = make_cert(cls.dir)
        cls.pin = pin_of(cls.cert)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.relay = Relay(wire.server_context(self.cert, self.key), open_wait=1, control_idle=3)
        self.srv = self.relay.listen("127.0.0.1", 0)
        self.addr = self.srv.getsockname()
        threading.Thread(target=self.relay.serve, args=(self.srv,), daemon=True).start()
        self.seed = os.urandom(32)
        self.pub = ed25519_public(self.seed)

    def tearDown(self):
        self.srv.close()

    def dial(self):
        sock = wire.dial(self.addr, self.pin, timeout=5)
        self.addCleanup(sock.close)
        return sock

    def register(self, seed=None, sign=None):
        """A Pi's control connection; -> (Control, reply)."""
        seed = seed or self.seed
        pub = ed25519_public(seed)
        sock = self.dial()
        send_line(sock, {"v": VERSION, "role": "pi", "pub": pub.hex()})
        challenge = bytes.fromhex(answer(sock)["challenge"])
        sig = (sign or ed25519_sign)(seed, REGISTER + challenge + pub)
        send_line(sock, {"sig": sig.hex()})
        return sock, answer(sock)

    def run_pi(self, sock, serve=True):
        """Run a Pi's control loop that answers opens with an echoing data connection."""
        ctl = Control(sock)

        def echo(wid):
            d = self.dial()
            send_line(d, {"v": VERSION, "role": "data", "id": wid})
            answer(d)
            d.settimeout(None)
            try:
                while True:
                    data = d.recv(65536)
                    if not data:
                        break
                    d.sendall(data)
            except OSError:
                pass

        def on_line(msg):
            if msg.get("op") == "open" and serve:
                threading.Thread(target=echo, args=(msg["id"],), daemon=True).start()

        result = {}
        t = threading.Thread(target=lambda: result.setdefault("why", ctl.run(on_line, 30)), daemon=True)
        t.start()
        return ctl, t, result

    def client(self, pub=None):
        sock = self.dial()
        send_line(sock, {"v": VERSION, "role": "client", "pub": (pub or self.pub).hex()})
        return sock, answer(sock)

    def test_joins_client_to_pi(self):
        sock, reply = self.register()
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["addr"].startswith("127.0.0.1:"))
        self.run_pi(sock)
        wait(lambda: self.relay.pis)
        c, ok = self.client()
        for _ in range(20):             # one thread per SSL socket: send a piece, read it back
            piece = os.urandom(16384)
            c.sendall(piece)
            got = b""
            while len(got) < len(piece):
                chunk = c.recv(65536)
                self.assertTrue(chunk)
                got += chunk
            self.assertEqual(got, piece)
        c.close()

    def test_bad_signature_refused(self):
        with self.assertRaises(Refused):
            self.register(sign=lambda seed, msg: ed25519_sign(os.urandom(32), msg))
        self.assertFalse(self.relay.pis)

    def test_offline_pi(self):
        with self.assertRaisesRegex(Refused, "offline"):
            self.client(pub=os.urandom(32))

    def test_pi_that_never_dials_back(self):
        sock, _ = self.register()
        self.run_pi(sock, serve=False)
        wait(lambda: self.relay.pis)
        with self.assertRaisesRegex(Refused, "no answer"):
            self.client()
        self.assertFalse(self.relay.waiting)

    def test_newer_registration_replaces_older(self):
        first, _ = self.register()
        ctl, t, result = self.run_pi(first)
        wait(lambda: self.relay.pis)
        second, _ = self.register()
        self.run_pi(second)
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertIs(len(self.relay.pis), 1)
        c, ok = self.client()          # the newer one answers
        self.assertTrue(ok["ok"])
        c.close()

    def test_silent_pi_dropped(self):
        sock, _ = self.register()
        wait(lambda: self.relay.pis)
        wait(lambda: not self.relay.pis, timeout=8)     # control_idle=3, no pings
        sock.close()

    def test_ping_keeps_pi(self):
        sock, _ = self.register()
        ctl, t, result = self.run_pi(sock)
        for _ in range(5):              # past control_idle=3, kept by pings
            ctl.post({"op": "ping"})
            threading.Event().wait(1)
        self.assertTrue(self.relay.pis)

    def test_unknown_data_id(self):
        d = self.dial()
        send_line(d, {"v": VERSION, "role": "data", "id": "nope"})
        with self.assertRaisesRegex(Refused, "unknown id"):
            answer(d)

    def test_old_version_refused(self):
        s = self.dial()
        send_line(s, {"v": 0, "role": "client", "pub": self.pub.hex()})
        msg = read_line(s)
        self.assertEqual(msg["versions"], [1, VERSION])

    def test_garbage_doesnt_hurt(self):
        s = self.dial()
        s.sendall(b'{"v": 1, "role": "pi", "pub": 5}\n')
        self.assertEqual(s.recv(10), b"")
        s = self.dial()
        s.sendall(b"x" * 10000)
        self.assertEqual(s.recv(10), b"")
        self.assertFalse(self.relay.pis)

    def test_wrong_pin(self):
        with self.assertRaises(ssl.SSLError):
            wire.dial(self.addr, "00" * 32, timeout=5)

    def test_status_over_loopback(self):
        sock, _ = self.register()
        self.run_pi(sock)
        wait(lambda: self.relay.pis)
        st = ask_status(self.addr[1])
        self.assertEqual(st["pis"], 1)


class SpliceTest(unittest.TestCase):
    def test_half_close_passes_through(self):
        a1, a2 = socket.socketpair()
        b1, b2 = socket.socketpair()
        for s in (a1, a2, b1, b2):
            self.addCleanup(s.close)
        t = threading.Thread(target=wire.splice, args=(a2, b1), daemon=True)
        t.start()
        a1.sendall(b"request")
        a1.shutdown(socket.SHUT_WR)
        got = b""
        while True:
            chunk = b2.recv(100)
            if not chunk:
                break
            got += chunk
        self.assertEqual(got, b"request")
        b2.sendall(b"reply")
        b2.close()
        self.assertEqual(a1.recv(100), b"reply")
        t.join(5)
        self.assertFalse(t.is_alive())

    def tls_pair(self):
        """(TLS client end, TLS server end) over a socket pair, as the endpoint makes them."""
        c, s = socket.socketpair()
        server = {}

        def accept():
            server["sock"] = tls.server_context(os.urandom(32)).wrap_socket(s, server_side=True)
        t = threading.Thread(target=accept, daemon=True)
        t.start()
        client = tls.client_context().wrap_socket(c)
        t.join(5)
        for sock in (client, server["sock"]):
            sock.settimeout(5)
            self.addCleanup(sock.close)
        return client, server["sock"]

    @staticmethod
    def read_all(sock):
        got = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return got
            got += chunk

    @unittest.skipIf(tls.unsupported(), "this Python's SSL library can't do TLS 1.3 with ed25519")
    def test_half_close_onto_tls_keeps_it_readable(self):
        # The plain side ends: splice half-closes the TLS side, whose reply must still
        # arrive as plaintext (SSLSocket.shutdown would have dropped TLS: raw records).
        a1, a2 = socket.socketpair()
        self.addCleanup(a1.close)
        client, server = self.tls_pair()
        t = threading.Thread(target=wire.splice, args=(a2, client), daemon=True)
        t.start()
        a1.sendall(b"request")
        a1.shutdown(socket.SHUT_WR)
        self.assertEqual(self.read_all(server), b"request")
        reply = os.urandom(200000)

        def answer():
            server.sendall(reply)
            socket.socket.shutdown(server, socket.SHUT_WR)
        threading.Thread(target=answer, daemon=True).start()
        a1.settimeout(5)
        self.assertEqual(self.read_all(a1), reply)
        t.join(5)
        self.assertFalse(t.is_alive())

    @unittest.skipIf(tls.unsupported(), "this Python's SSL library can't do TLS 1.3 with ed25519")
    def test_tls_half_close_keeps_it_writable(self):
        # The TLS side ends first: splice can still send it what the plain side says.
        a1, a2 = socket.socketpair()
        self.addCleanup(a1.close)
        client, server = self.tls_pair()
        t = threading.Thread(target=wire.splice, args=(a2, client), daemon=True)
        t.start()
        server.sendall(b"banner")
        socket.socket.shutdown(server, socket.SHUT_WR)
        a1.settimeout(5)
        self.assertEqual(self.read_all(a1), b"banner")
        a1.sendall(b"after")
        a1.shutdown(socket.SHUT_WR)
        self.assertEqual(self.read_all(server), b"after")
        t.join(5)
        self.assertFalse(t.is_alive())


if __name__ == "__main__":
    unittest.main()


class FakeIdentity:
    def __init__(self):
        self._seed = os.urandom(32)

    def seed(self):
        return self._seed

    def public(self):
        return ed25519_public(self._seed)


class FakeWifi:
    def __init__(self):
        self.internet = "online"

    def connection(self):
        return {"ip": "192.168.1.154", "ssid": "home", "internet": self.internet}

    parked = False              # a property on WifiService


class Echo:
    def __init__(self):
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.peers = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, addr = self.srv.accept()
            except OSError:
                return
            self.peers.append(addr)
            threading.Thread(target=self._echo, args=(c,), daemon=True).start()

    def _echo(self, c):
        with c:
            while True:
                data = c.recv(65536)
                if not data:
                    return
                c.sendall(data)


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a test certificate")
class ThroughTheRelay(unittest.TestCase):
    """Relay and Pi agent in one process, with a bare client: the relay's data
    connection reaches the Pi's endpoint (here an echo server standing in for it)."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.cert, cls.key = make_cert(cls.dir)
        cls.pin = pin_of(cls.cert)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        from audiolink.services.relay import RelayAgent
        self.relay = Relay(wire.server_context(self.cert, self.key), open_wait=2)
        self.srv = self.relay.listen("127.0.0.1", 0)
        self.addCleanup(self.srv.close)
        threading.Thread(target=self.relay.serve, args=(self.srv,), daemon=True).start()
        self.addr = ("127.0.0.1", self.srv.getsockname()[1])
        self.spec = "%s:%d#%s" % (self.addr + (self.pin,))
        self.echo = Echo()
        self.addCleanup(self.echo.srv.close)
        self.id, self.wifi = FakeIdentity(), FakeWifi()
        self.agent = RelayAgent(self.id, self.wifi, [self.spec], endpoint_port=self.echo.port)

    def connect(self):
        """What a client does: ask the relay for the Pi; the socket then reaches the Pi."""
        sock = wire.dial(self.addr, self.pin)
        self.addCleanup(sock.close)
        send_line(sock, {"v": VERSION, "role": "client", "pub": self.id.public().hex()})
        answer(sock)
        return sock

    def up(self):
        self.agent.run()
        self.assertTrue(wait(lambda: self.agent.status()["relay"] == "up"))

    def test_reaches_the_endpoint(self):
        self.up()
        sock = self.connect()
        for size in (1, 5000, 70000):
            piece = os.urandom(size)
            sock.sendall(piece)
            got = b""
            while len(got) < size:
                got += sock.recv(65536)
            self.assertEqual(got, piece)
        self.assertTrue(wait(lambda: self.agent.status()["sessions"] == 1))
        if sys.platform.startswith("linux"):
            self.assertEqual(self.echo.peers[0][0], "127.0.0.2")    # RELAY_SOURCE: the endpoint knows the route

    def test_waits_for_internet(self):
        self.wifi.internet = "portal"
        self.agent.run()
        wait(lambda: self.agent.status()["relay"] == "waiting for internet")
        self.assertFalse(self.relay.pis)

    def test_offline_pi(self):
        with self.assertRaisesRegex(Refused, "offline"):
            self.connect()

    def test_opens_are_capped_per_client_and_overall(self):
        from audiolink.services import relay as agent_relay
        from tests.helpers import FakeClock, patch_time
        clock = FakeClock()
        with patch_time(agent_relay, clock):
            for _ in range(agent_relay.PER_CLIENT_PER_MIN):
                self.assertTrue(self.agent._rate_ok("203.0.113.5"))
            self.assertFalse(self.agent._rate_ok("203.0.113.5"))      # each has its own limit
            for i in range(agent_relay.OPENS_PER_MIN - agent_relay.PER_CLIENT_PER_MIN):
                self.assertTrue(self.agent._rate_ok("198.51.100.%d" % i))
            self.assertFalse(self.agent._rate_ok("198.51.100.200"))   # and all share one
            self.assertFalse(self.agent._rate_ok(None))               # an older relay: the same
            clock.advance(61)
            self.assertTrue(self.agent._rate_ok("198.51.100.200"))

    def test_the_relay_names_the_client(self):
        opened = []
        real = self.agent._open
        self.agent._open = lambda addr, pin, wid, client=None: (opened.append(client), real(addr, pin, wid, client))
        self.up()
        sock = self.connect()
        sock.sendall(b"x")
        self.assertEqual(sock.recv(1), b"x")
        self.assertEqual(opened, ["127.0.0.1"])


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a test certificate")
@needs_tls
class EndpointThroughTheRelay(unittest.TestCase):
    """The real endpoint behind the relay: TLS pinned to the Pi's key, auth, the relay route."""

    def setUp(self):
        from audiolink.services.endpoint import Endpoint
        from audiolink.services.identity import IdentityService
        from audiolink.services.relay import RelayAgent
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        cert, key = make_cert(self.dir)
        pin = pin_of(cert)
        relay = Relay(wire.server_context(cert, key), open_wait=2)
        srv = relay.listen("127.0.0.1", 0)
        self.addCleanup(srv.close)
        threading.Thread(target=relay.serve, args=(srv,), daemon=True).start()
        self.addr, self.pin = ("127.0.0.1", srv.getsockname()[1]), pin
        self.identity = IdentityService(self.dir)
        self.seed = os.urandom(32)
        self.identity.claim(ed25519_public(self.seed), "desktop")
        self.ep = Endpoint(self.identity, port=0, wifi_ip=lambda: "10.9.9.9", bind="127.0.0.1")
        self.ep.register("route", lambda conn, req: {"route": conn.route}, "user")
        self.agent = RelayAgent(self.identity, FakeWifi(), ["%s:%d#%s" % (self.addr + (pin,))],
                                endpoint_port=self.ep.port)
        self.agent.run()
        self.assertTrue(wait(lambda: self.agent.status()["relay"] == "up"))

    def test_auth_through_the_relay(self):
        sock = wire.dial(self.addr, self.pin)
        send_line(sock, {"v": VERSION, "role": "client", "pub": self.identity.public().hex()})
        answer(sock)
        ours, theirs = socket.socketpair()
        threading.Thread(target=wire.splice, args=(theirs, sock), daemon=True).start()
        c = EndpointClient(0, sock=ours)
        self.addCleanup(c.close)
        self.assertEqual(c.pub, self.identity.public())     # the Pi's own certificate, end to end
        self.assertEqual(c.auth(self.seed)["role"], "owner")
        self.assertEqual(c.ask({"op": "route"}), {"route": "relay"})


class Addresses(unittest.TestCase):
    def test_default_off(self):
        """A fork without a relay (DEFAULT = "off"): clients try only the Pi's."""
        from unittest import mock
        from audiolink.relay import client
        with mock.patch.object(client, "DEFAULT", "off"), mock.patch.object(client, "dial", side_effect=AssertionError):
            with self.assertRaisesRegex(Refused, "no relay"):
                client.connect(os.urandom(32))

    def test_parse_addr(self):
        from audiolink.relay import parse_addr
        for spec, want in (("relay.example:8443", ("relay.example", 8443)), ("relay.example", ("relay.example", 443)),
                           ("0.0.0.0:443", ("0.0.0.0", 443)), ("[::1]:8443", ("::1", 8443)),
                           ("[::]", ("::", 443)), ("::1", ("::1", 443))):
            self.assertEqual(parse_addr(spec), want, spec)
