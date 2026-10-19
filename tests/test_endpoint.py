"""services/endpoint.py (protocol v2): hello, auth, roles, refusals, strangers; and
`open`, the streams to the host through it (services/streams.py)."""
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from audiolink.auth import ENDPOINT_VERSION
from audiolink.crypto import ed25519_public
from audiolink.services import endpoint
from audiolink.services.access import NONE, OWNER, USER
from audiolink.services.endpoint import Endpoint
from audiolink.services.identity import IdentityService
from audiolink.services.streams import Streams
from audiolink.session import Session

from .helpers import EndpointClient, needs_tls, wait

RECORD = {"name": "pi", "ts": 1800000000, "lan": ["10.0.0.5"], "ssid": "home", "ports": [2222]}


class Pi(unittest.TestCase):
    """An endpoint with an owner (desktop) and a user (laptop) on its list."""
    exempt = endpoint.EXEMPT

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.identity = IdentityService(self.dir)
        self.desktop, self.laptop, self.stranger = os.urandom(32), os.urandom(32), os.urandom(32)
        self.identity.claim(ed25519_public(self.desktop), "desktop")
        self.identity.add(ed25519_public(self.laptop), "laptop", USER)
        self.wifi_ip = "127.0.0.1"                  # loopback stands in for the LAN; None: unplaceable
        self.presence = FakePresence()
        self.ep = Endpoint(self.identity, port=0, presence=self.presence, wifi_ip=lambda: self.wifi_ip,
                           bind="127.0.0.1", exempt=self.exempt, record=lambda: RECORD)
        self.ep.register("look", lambda conn, req: {"ok": True, "route": conn.route, "name": conn.name}, USER)
        self.ep.register("manage", lambda conn, req: {"ok": True}, OWNER)
        self.ep.register("public", lambda conn, req: {"ok": True}, NONE)

    def client(self, seed=None):
        c = EndpointClient(self.ep.port)
        self.addCleanup(c.close)
        if seed:
            self.assertTrue(c.auth(seed).get("ok"))
        return c


@needs_tls
class Auth(Pi):
    def test_hello(self):
        hi = self.client().hello()
        self.assertEqual(hi["v"], ENDPOINT_VERSION)
        self.assertEqual(len(bytes.fromhex(hi["nonce"])), 16)
        self.assertEqual((hi["pub"], hi["claimed"]), (self.identity.public().hex(), True))

    def test_a_listed_key(self):
        reply = self.client().auth(self.laptop)
        self.assertEqual(reply, {"ok": True, "name": "laptop", "role": USER, "record": RECORD})

    def test_not_listed(self):
        reply = self.client().auth(self.stranger)
        self.assertEqual(reply["code"], "not_listed")
        self.assertIn("isn't on %s's list" % self.identity.pi_name(), reply["error"])

    def test_a_signature_for_another_connection(self):
        a, b = self.client(), self.client()
        a.hello()
        b.hello()
        b.hi = a.hi                                 # signed over a's nonce
        self.assertEqual(b.auth(self.laptop)["code"], "not_listed")

    def test_once_per_connection(self):
        c = self.client(self.laptop)
        self.assertNotIn("ok", c.auth(self.desktop))
        self.assertEqual(c.ask({"op": "look"})["name"], "laptop")

    def test_older_clients_are_told_to_update(self):
        c = self.client()
        c.hello()
        self.assertEqual(c.ask({"op": "look", "proof": "00"})["code"], "upgrade")    # the read secret's
        self.assertEqual(c.ask({"op": "manage"})["code"], "upgrade")                # anything before auth
        self.assertEqual(c.ask({"op": "nonsense"})["code"], "upgrade")
        self.assertTrue(c.ask({"op": "public"})["ok"])
        plain = socket.create_connection(("127.0.0.1", self.ep.port), timeout=5)
        self.addCleanup(plain.close)
        f = plain.makefile("rwb")
        for req in ({"op": "hello"}, {"op": "look"}):
            f.write((json.dumps(req) + "\n").encode())
            f.flush()
        self.assertTrue(json.loads(f.readline())["tls"])
        self.assertEqual(json.loads(f.readline())["code"], "upgrade")

    def test_roles(self):
        user, owner = self.client(self.laptop), self.client(self.desktop)
        self.assertEqual(user.ask({"op": "look"})["route"], "lan")
        refused = user.ask({"op": "manage"})
        self.assertEqual(refused["code"], "not_owner")
        self.assertEqual(refused["error"], "Only an owner of %s can do that (laptop isn't one): on an owner computer, "
                                           "audiolink invite laptop --owner" % self.identity.pi_name())
        self.assertTrue(owner.ask({"op": "manage"})["ok"])
        self.assertTrue(owner.ask({"op": "look"})["ok"])

    def test_the_relay_route(self):
        self.wifi_ip = None                         # can't tell the route: the relay's rules
        self.assertEqual(self.client(self.laptop).ask({"op": "look"})["route"], "relay")


@needs_tls
class Revoking(Pi):
    def test_off_the_list_is_off_at_once(self):
        c = self.client(self.laptop)
        self.identity.remove(ed25519_public(self.laptop).hex())
        with self.assertRaises((ConnectionError, OSError)):
            c.ask({"op": "look"})

    def test_removing_itself_gets_its_reply_first(self):
        self.identity.serve(self.ep)
        c = self.client(self.laptop)
        self.assertEqual(c.ask({"op": "remove", "key": ed25519_public(self.laptop).hex()}), {"ok": True})
        with self.assertRaises((ConnectionError, OSError)):
            c.ask({"op": "look"})

    def test_a_pi_that_starts_over_drops_everyone(self):
        c = self.client(self.desktop)
        self.identity.start_over()
        with self.assertRaises((ConnectionError, OSError)):
            c.ask({"op": "look"})

    def test_made_a_user(self):
        held = threading.Event()

        def keep(conn, req):
            def run():
                held.set()
                try:
                    conn.sock.recv(1)
                except (OSError, ValueError):
                    pass
            conn.send({"ok": True})
            raise endpoint.Takeover(run)
        self.ep.register("keep", keep, OWNER)
        self.identity.add(ed25519_public(self.laptop), "laptop", OWNER)
        c = self.client(self.laptop)
        self.assertTrue(c.ask({"op": "keep"})["ok"])
        self.assertTrue(held.wait(5))
        self.identity.data["members"][1]["role"] = USER     # demoted (as a restore could)
        self.identity._changed()
        c.sock.settimeout(5)
        self.assertEqual(c.sock.recv(1), b"")               # closed


@needs_tls
class Managing(Pi):
    def setUp(self):
        super().setUp()
        self.reset = threading.Event()
        self.identity.serve(self.ep, reset=self.reset.set)
        self.identity.inviter = lambda name, role: {"code": "123456", "expires_in": 300, "for": [name, role]}

    def test_owner_ops(self):
        c = self.client(self.desktop)
        self.assertEqual([m["name"] for m in c.ask({"op": "members"})["members"]], ["desktop", "laptop"])
        self.assertEqual(c.ask({"op": "invite", "name": "mom", "role": "user"})["for"], ["mom", "user"])
        self.assertEqual(c.ask({"op": "remove", "name": "desktop"})["code"], "last_owner")
        self.assertEqual(c.ask({"op": "remove", "name": "laptop"}), {"ok": True})
        self.assertEqual(c.ask({"op": "rename", "name": "kitchen"}), {"ok": True})
        self.assertEqual(self.identity.pi_name(), "kitchen")
        self.assertEqual(c.ask({"op": "reset"}), {"ok": True})
        self.assertTrue(self.reset.wait(5))

    def test_users_dont_manage(self):
        c = self.client(self.laptop)
        for op in ("members", "invite", "rename", "reset"):
            self.assertEqual(c.ask({"op": op, "name": "x"})["code"], "not_owner", op)
        self.assertEqual(c.ask({"op": "remove", "name": "desktop"})["code"], "not_owner")


class FakePresence:
    def __init__(self):
        self.lock = threading.Lock()
        self.held, self.touched = set(), 0

    def hold(self, ends):
        with self.lock:
            self.held.add(ends)

    def release(self, ends):
        with self.lock:
            self.held.discard(ends)

    def touch(self, sock):
        with self.lock:
            self.touched += 1


@needs_tls
class Strangers(Pi):
    """Connections that prove nothing can't lock others out or keep the Pi 'in use'."""
    exempt = ()

    def test_one_address_holds_two_unproven(self):
        a, b = self.client(), self.client()
        raw = socket.create_connection(("127.0.0.1", self.ep.port), timeout=5)
        self.addCleanup(raw.close)
        self.assertEqual(json.loads(raw.makefile("rb").readline())["code"], "busy")
        self.assertTrue(a.auth(self.laptop)["ok"])           # proven: no longer counts
        self.assertTrue(self.client().auth(self.laptop)["ok"])
        b.close()
        self.assertTrue(wait(lambda: self.ep.unproven == {}))
        self.assertEqual(self.ep.active, 2)

    def test_unproven_connections_are_dropped(self):
        with mock.patch.object(endpoint, "PROVE_WITHIN", 0.5):
            idle, proven = self.client(), self.client(self.laptop)
            idle.sock.settimeout(5)
            self.assertEqual(idle.sock.recv(10), b"")       # dropped
            self.assertTrue(proven.ask({"op": "look"})["ok"])  # proven: only the idle limit applies

    def test_one_hello_per_connection(self):
        c = self.client()
        c.hello()
        with self.assertRaises((ConnectionError, OSError)):
            c.hello()

    def test_only_proven_connections_count_as_present(self):
        c = self.client()
        self.assertTrue(wait(lambda: len(self.presence.held) == 1))
        self.assertEqual(self.presence.touched, 0)
        c.auth(self.laptop)
        self.assertEqual(self.presence.held, set())
        self.assertEqual(self.presence.touched, 1)
        stranger = self.client()
        stranger.close()
        self.assertTrue(wait(lambda: self.ep.active == 1))
        self.assertEqual((self.presence.held, self.presence.touched), (set(), 1))


class Echo:
    def __init__(self):
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(c,), daemon=True).start()

    def _echo(self, c):
        with c:
            while True:
                data = c.recv(65536)
                if not data:
                    return
                c.sendall(data)


@needs_tls
class Open(Pi):
    """`open`: the connection becomes a stream to what the host exposes."""

    def setUp(self):
        super().setUp()
        self.echo = Echo()
        self.addCleanup(self.echo.srv.close)
        self.host_streams = Streams("host", expose=[(2222, ("127.0.0.1", self.echo.port))])
        self.pi_streams = Streams("pi")
        self.pi_streams.serve_endpoint(self.ep)
        self.host = Session("host", [self.host_streams])
        self.pi = Session("pi", [self.pi_streams])
        self.running = True
        threading.Thread(target=self._pump, daemon=True).start()
        self.addCleanup(setattr, self, "running", False)
        self.assertTrue(wait(lambda: self.pi_streams.ports() == [2222]))

    def _pump(self):
        while self.running:
            self.pi.link.feed(self.host.link.pull(8192))
            self.host.link.feed(self.pi.link.pull(8192))
            time.sleep(0.0005)

    def echoes(self, c, data):
        c.sock.sendall(data)
        return c.recv_exactly(len(data)) == data

    def test_a_stream_to_an_exposed_port(self):
        c = self.client(self.laptop)
        self.assertEqual(c.ask({"op": "open", "port": 2222}), {"ok": True})
        for size in (1, 5000, 200000):
            self.assertTrue(self.echoes(c, os.urandom(size)))
        self.assertEqual(self.ep.active, 0)                 # left the request loop

    def test_bytes_right_behind_the_request(self):
        c = self.client(self.laptop)
        c.sock.sendall(b'{"op": "open", "port": 2222}\nSSH-2.0-test\r\n')
        self.assertEqual(c.line(), {"ok": True})
        self.assertEqual(c.recv_exactly(14), b"SSH-2.0-test\r\n")

    def test_not_exposed(self):
        c = self.client(self.laptop)
        self.assertEqual(c.ask({"op": "open", "port": 2223})["code"], "not_exposed")
        self.assertEqual(c.ask({"op": "open", "port": "2222"})["code"], "not_exposed")
        self.assertEqual(self.client().ask({"op": "open", "port": 2222})["code"], "upgrade")   # no auth

    def test_no_idle_limit(self):
        with mock.patch.object(endpoint, "IDLE", 0.3):
            c = self.client(self.laptop)
            self.assertTrue(c.ask({"op": "open", "port": 2222})["ok"])
            time.sleep(1)
            self.assertTrue(self.echoes(c, b"still here"))

    def test_streams_have_their_own_cap(self):
        from audiolink.services import streams
        self.pi_streams.net_slots = threading.BoundedSemaphore(2)
        with mock.patch.object(streams, "NET_STREAMS", 2):
            opened = [self.client(self.laptop) for _ in range(3)]
            self.assertTrue(all(c.ask({"op": "open", "port": 2222}).get("ok") for c in opened[:2]))
            self.assertEqual(opened[2].ask({"op": "open", "port": 2222})["code"], "busy")
            opened[0].close()
            self.assertTrue(wait(lambda: opened[2].ask({"op": "open", "port": 2222}).get("ok")))

    def test_taken_off_the_list_ends_the_stream(self):
        c = self.client(self.laptop)
        self.assertTrue(c.ask({"op": "open", "port": 2222})["ok"])
        self.assertTrue(self.echoes(c, b"x"))
        self.identity.remove(ed25519_public(self.laptop).hex())
        c.sock.settimeout(5)
        self.assertEqual(c.recv_exactly(1), b"")

    def test_the_host_going_ends_the_offer(self):
        self.running = False
        self.assertTrue(wait(lambda: not self.pi.peer_alive, timeout=10))
        self.assertEqual(self.pi_streams.ports(), [])
        self.assertEqual(self.client(self.laptop).ask({"op": "open", "port": 2222})["code"], "not_exposed")


@needs_tls
class OwnerServices(Pi):
    """Logs and updates over the network: owners only."""

    def setUp(self):
        super().setUp()
        from audiolink.services.logs import LogsService
        from audiolink.services.update import UpdateService
        logs = os.path.join(self.dir, "logs")
        os.mkdir(logs)
        with open(os.path.join(logs, "audiolink.log"), "w") as f:
            f.write("one\ntwo\n")
        LogsService(log_dir=logs, prev=os.path.join(logs, "prev"), endpoint=self.ep)
        UpdateService(self.dir, restart=lambda: None, endpoint=self.ep)

    def test_owners_only(self):
        owner, user = self.client(self.desktop), self.client(self.laptop)
        self.assertEqual(owner.ask({"op": "logs", "action": "tail", "lines": 1})["result"]["text"], "two")
        self.assertFalse(owner.ask({"op": "update_status"})["result"]["update"])
        for req in ({"op": "logs", "action": "list"}, {"op": "update_status"}, {"op": "update", "action": "status"}):
            self.assertEqual(user.ask(req)["code"], "not_owner", req)


class Static(unittest.TestCase):
    def test_a_static_forward_is_offered_not_listened_on(self):
        s = Streams("pi")
        s.serve(2222, ("127.0.0.1", 22))
        self.assertEqual((s.ports(), s.target_for(2222), s.listeners), ([2222], ("127.0.0.1", 22), {}))
        s.session = mock.Mock()
        r = s._apply_expose([[2222, "127.0.0.1", 80], [2223, "127.0.0.1", 80]])
        self.assertEqual(r["refused"], [[2222, "it's already a static --forward there"]])
        self.assertEqual(s.ports(), [2222, 2223])


if __name__ == "__main__":
    unittest.main()
