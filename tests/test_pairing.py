"""Pairing over the network, both sides (client/pairing.py, services/pairing.py)."""
import argparse
import contextlib
import io
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from audiolink import tls
from audiolink.client import keyring, pairing
from audiolink.client.pairing import PiConn
from audiolink.services import endpoint
from audiolink.services.access import PAIRED
from audiolink.services.endpoint import Endpoint
from audiolink.services.identity import IdentityService
from audiolink.crypto import Spake2
from audiolink.services import pairing as services_pairing
from audiolink.services.pairing import ID_CLIENT, ID_PI, PairingService

from .helpers import FakeClock, patch_time


class Browse(unittest.TestCase):
    """Finding Pis over mDNS: asked on every interface, answers only."""

    def test_answers_count_not_queries(self):
        query = pairing._mdns_query(pairing.SERVICE)          # ours, looped back
        self.assertFalse(pairing.is_answer(query))
        answer = bytes([0, 0, 0x84, 0]) + query[4:]
        self.assertTrue(pairing.is_answer(answer))

    def test_local_ips_leave_out_loopback(self):
        self.assertFalse([ip for ip in pairing.local_ips() if ip.startswith("127.")])

    def test_asked_on_every_interface(self):
        sent = []

        class Sock:
            def __init__(self, *a):
                self.ifaddr = None

            def setsockopt(self, level, opt, value):
                self.ifaddr = value

            def sendto(self, data, addr):
                sent.append((self.ifaddr, addr))

            def close(self):
                pass

        with mock.patch.object(pairing, "local_ips", lambda: ["192.168.1.5", "172.28.144.1"]), \
                mock.patch.object(pairing.socket, "socket", Sock), \
                mock.patch.object(pairing.select, "select", lambda r, w, x, t: ([], [], [])):
            self.assertEqual(pairing.browse(0.01), [])
        ifaces = {ifaddr for ifaddr, _ in sent}
        self.assertEqual(ifaces, {pairing.socket.inet_aton("192.168.1.5"), pairing.socket.inet_aton("172.28.144.1")})

    def test_resolver_only_when_asked_and_nothing_answered(self):
        with mock.patch.object(pairing, "local_ips", lambda: []), \
                mock.patch.object(pairing.select, "select", lambda r, w, x, t: ([], [], [])), \
                mock.patch.object(pairing.socket, "getaddrinfo",
                                  return_value=[(2, 2, 17, "", ("192.168.1.9", 0))]) as gai:
            self.assertEqual(pairing.browse(0.01), [])
            self.assertEqual(pairing.browse(0.01, resolve=True), ["192.168.1.9"])
        self.assertEqual(gai.call_args.args[0], pairing.HOSTNAME)


class SlowAsk:
    """A person who takes their time at every prompt (longer than the Pi waits)."""

    def __init__(self, code=None, wait=0.6):
        self.code, self.wait, self.asked = code, wait, []

    def text(self, prompt):
        self.asked.append(prompt)
        if self.code is None:
            raise AssertionError("asked for a code")
        time.sleep(self.wait)
        return self.code

    def yes(self, question, default=False):
        self.asked.append(question)
        time.sleep(self.wait)
        return True


@unittest.skip("roles-v1: needs the v2 Pi side (client/pairing.py is ported)")
@unittest.skipIf(tls.unsupported(), "this Python's SSL library can't do TLS 1.3 with ed25519")
class AddOverNetwork(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.identity = IdentityService(self.dir)
        self.identity.claim()
        self.ep = Endpoint(self.identity, port=0, wifi_ip=lambda: "127.0.0.1", bind="127.0.0.1")
        self.svc = PairingService(self.identity, self.ep)
        self.ep.register("change_status", lambda conn, req: {"saved": []}, PAIRED)
        port = self.ep.port

        def candidates(at):
            c = PiConn("127.0.0.1", port=port)
            return [(c, c.hello())]
        for patch in (mock.patch.object(keyring, "PATH", Path(self.dir) / "pis.json"),
                      mock.patch.object(endpoint, "PROVE_WITHIN", 0.3),
                      mock.patch.object(pairing, "candidates", candidates),
                      mock.patch.object(pairing, "PiConn",
                                        lambda ip, timeout=3: PiConn(ip, port=port, timeout=timeout))):
            patch.start()
            self.addCleanup(patch.stop)

    def add(self, ask, pairing_code=None):
        a = argparse.Namespace(at=None, pairing_code=pairing_code, name=None)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            entry = pairing.add_over_network(a, ask)
        return entry, out.getvalue()

    def tries_left(self):
        return self.svc.handle("status", {})["tries_left"]

    def test_a_slow_answer_doesnt_lose_the_pi(self):
        code = self.svc.new_code()["code"]
        entry, out = self.add(SlowAsk(code[:3] + " " + code[3:]))
        self.assertEqual(entry["code"], self.identity.code())
        self.assertIn("audiolink pairing code", out)

    def test_a_given_code_needs_no_hint(self):
        code = self.svc.new_code()["code"]
        entry, out = self.add(SlowAsk(wait=0), pairing_code=code)
        self.assertEqual(entry["code"], self.identity.code())
        self.assertIn("(claimed): pairing with the code given", out)
        self.assertNotIn("audiolink pairing code", out)

    def test_claiming_after_a_slow_yes(self):
        self.identity.start_over()
        entry, _ = self.add(SlowAsk())
        self.assertTrue(self.identity.claimed())
        self.assertEqual(entry["code"], self.identity.code())

    def test_a_code_that_isnt_six_digits_costs_no_try(self):
        self.svc.new_code()
        for typed in ("12345", "12345a", "1234567"):
            with self.assertRaisesRegex(SystemExit, "6 digits"):
                self.add(SlowAsk(typed, wait=0))
        self.assertEqual(self.tries_left(), 3)

    def test_an_empty_pairing_code_option_isnt_ignored(self):
        self.svc.new_code()
        with self.assertRaisesRegex(SystemExit, "6 digits"):
            self.add(SlowAsk(None), pairing_code="")

    def pis(self):
        from audiolink.client import pis
        with contextlib.redirect_stdout(io.StringIO()) as out:
            pis.pis_command(argparse.Namespace())
        return out.getvalue().splitlines()[1].split()

    def test_the_pis_network_comes_with_pairing(self):
        rec = {"v": 1, "name": "pi", "ts": 1700000000, "lan": ["10.0.0.5"], "ssid": "HomeWifi", "ports": [2222]}
        self.svc.where = lambda: rec
        self.add(SlowAsk(self.svc.new_code()["code"], wait=0))
        self.assertEqual(self.pis()[-1], "HomeWifi")
        self.assertEqual(keyring.load()[self.identity.info()["pub"]]["last_record"], rec)

    def test_an_older_pi_says_no_network(self):
        self.add(SlowAsk(self.svc.new_code()["code"], wait=0))
        self.assertEqual(self.pis()[-1], "-")
        self.assertNotIn("last_record", keyring.load()[self.identity.info()["pub"]])

    def test_a_rotated_code_says_so(self):
        old = self.identity.code()
        keyring.upsert(old, name="pi")
        self.identity.handle("rotate_secret", {})
        code = self.svc.new_code()["code"]
        entry, out = self.add(SlowAsk(code, wait=0))
        self.assertIn("code has changed; this computer needs a pairing code", out)
        self.assertEqual(entry["code"], self.identity.code())


class FakeConn:
    """The Pi's view of one pairing attempt; plays the client's part with `code`."""

    def __init__(self, ip, code):
        self.addr = (ip, 5000)
        self.spake = Spake2("A", code or "", ID_CLIENT, ID_PI)
        self.sent, self.proven = [], False

    def send(self, obj):
        self.sent.append(obj)

    def recv(self):
        _, ours, _ = self.spake.finish(bytes.fromhex(self.sent[-1]["msg"]))
        return {"confirm": ours.hex()}

    def prove(self):
        self.proven = True


class FakeEndpoint:
    def register(self, *args):
        pass


class Guessing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.identity = IdentityService(self.dir)
        self.identity.claim()
        self.svc = PairingService(self.identity, FakeEndpoint())
        self.clock = FakeClock()
        patch = patch_time(services_pairing, self.clock)
        patch.start()
        self.addCleanup(patch.stop)

    def attempt(self, ip, code):
        conn = FakeConn(ip, code)
        with self.assertRaises(StopIteration):
            self.svc._pair(conn, {"op": "pair", "msg": conn.spake.msg.hex()})
        return conn

    def test_wrong_codes_hold_back_that_address_only(self):
        code = self.svc.new_code()["code"]
        self.assertEqual(self.attempt("10.0.0.9", "000000").sent[-1]["error"], "Wrong code (2 tries left)")
        self.assertIn("Too many wrong codes", self.attempt("10.0.0.9", code).sent[-1]["error"])
        ok = self.attempt("10.0.0.2", code)
        self.assertIn("payload", ok.sent[-1])
        self.assertTrue(ok.proven)

    def test_backoff_grows_then_is_forgotten(self):
        self.svc.new_code()
        for _ in range(3):
            self.attempt("10.0.0.9", "000000")
            self.clock.advance(services_pairing.MAX_BACKOFF)
        self.assertEqual(self.svc.failures["10.0.0.9"]["count"], 3)
        self.clock.advance(services_pairing.DECAY)
        self.attempt("10.0.0.9", "000000")
        self.assertEqual(self.svc.failures["10.0.0.9"]["count"], 1)

    def test_asking_doesnt_tell_whether_a_code_is_active(self):
        none = self.attempt("10.0.0.9", "123456")
        self.clock.advance(services_pairing.MAX_BACKOFF)
        self.svc.new_code()
        some = self.attempt("10.0.0.9", "123456")
        self.assertEqual(set(none.sent[0]), {"msg"})
        self.assertEqual(set(some.sent[0]), {"msg"})
        self.assertIn("expired or been used up; get a new one: audiolink pairing code", none.sent[-1]["error"])

    def test_last_try(self):
        self.svc.new_code()
        for left in (2, 1):
            self.attempt("10.0.0.%d" % left, "000000")
        self.assertEqual(self.svc.code["tries"], 1)
        self.assertIn("last try", self.attempt("10.0.0.3", "000000").sent[-1]["error"])
        self.assertIn("expired or been used up", self.attempt("10.0.0.4", "000000").sent[-1]["error"])


class InvitePi:
    """The Pi's half of redeeming an invite (docs/protocol.md "Invites"), in-process: what
    the client's pair() talks to."""

    def __init__(self, code, role="user", name="green-rowan"):
        import os
        from audiolink.crypto import ed25519_public
        self.pub = ed25519_public(os.urandom(32))
        self.code, self.role, self.name, self.got_key = code, role, name, None

    def ask(self, obj):
        import json
        from audiolink.crypto import open_sealed, seal
        if obj.get("op") == "pair":
            self.spake = Spake2("B", self.code, ID_CLIENT, ID_PI)
            self.ke, self.ours, self.theirs = self.spake.finish(bytes.fromhex(obj["msg"]))
            return {"msg": self.spake.msg.hex()}
        if bytes.fromhex(obj["confirm"]) != self.theirs:
            return {"error": "Wrong code (2 tries left)"}
        self.got_key = json.loads(open_sealed(self.ke, bytes.fromhex(obj["key"])))["key"]
        payload = {"name": self.name, "role": self.role, "pub": self.pub.hex(), "record": {"ts": 1, "lan": []}}
        return {"confirm": self.ours.hex(), "payload": seal(self.ke, json.dumps(payload).encode()).hex()}


class Replies(unittest.TestCase):
    def key(self):
        import os
        from audiolink.client.computer import Key
        from audiolink.crypto import ed25519_public
        k = Key.__new__(Key)
        k.seed = os.urandom(32)
        k.pub = ed25519_public(k.seed)
        return k

    def test_a_reply_without_its_fields(self):
        class Conn:
            def ask(self, obj):
                return {}
        with self.assertRaisesRegex(SystemExit, "bad reply"):
            pairing.pair(Conn(), "123456", self.key())

    def test_redeeming_carries_this_computers_key_in(self):
        pi, key = InvitePi("123456", role="owner"), self.key()
        conn = mock.Mock(ask=pi.ask, pub=pi.pub)        # pub: the certificate's key
        got = pairing.pair(conn, "123456", key)
        self.assertEqual(pi.got_key, key.pub.hex())
        self.assertEqual((got["name"], got["role"], got["pub"]), ("green-rowan", "owner", pi.pub.hex()))

    def test_a_wrong_code(self):
        pi = InvitePi("123456")
        conn = mock.Mock(ask=pi.ask, pub=pi.pub)
        with self.assertRaisesRegex(SystemExit, "Wrong code"):
            pairing.pair(conn, "654321", self.key())
        self.assertIsNone(pi.got_key)

    def test_the_payload_names_the_certificates_key(self):
        pi = InvitePi("123456")
        conn = mock.Mock(ask=pi.ask, pub=b"\x02" * 32)     # some other key's certificate
        with self.assertRaisesRegex(SystemExit, "another key than its certificate"):
            pairing.pair(conn, "123456", self.key())

    def test_an_unclaimed_pi_says_claim_over_usb(self):
        conn = mock.Mock()
        conn.ask.return_value = {"error": "no owner", "code": "claim_usb"}
        with self.assertRaisesRegex(SystemExit, "audiolink setup there to claim it"):
            pairing.pair(conn, "123456", self.key())

    def test_invite_codes(self):
        self.assertEqual(pairing.invite_code("482 177", "pi"), "482177")
        self.assertEqual(pairing.invite_code("482-177", "pi"), "482177")
        for bad in ("", "12345", "1234567", "12345a"):
            with self.assertRaisesRegex(SystemExit, "6 digits"):
                pairing.invite_code(bad, "pi")


class RecordNow(unittest.TestCase):
    def test_where_the_pi_is_once_it_knows_the_time(self):
        from types import SimpleNamespace
        from audiolink.services.discovery import DiscoveryService
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        wifi = SimpleNamespace(connection=lambda: {"ip": "10.0.0.5", "ssid": "HomeWifi", "internet": "offline"})
        svc = DiscoveryService(IdentityService(d), wifi, SimpleNamespace(ports=lambda: [2222]))
        self.assertIsNone(svc.record_now())                     # not attached yet
        now = [5]
        svc.attach(SimpleNamespace(now=lambda: now[0]))
        self.assertIsNone(svc.record_now())                     # the time isn't known
        now[0] = 1800000000
        rec = svc.record_now()
        self.assertEqual((rec["ssid"], rec["lan"], rec["ports"], rec["ts"]), ("HomeWifi", ["10.0.0.5"], [2222], now[0]))


if __name__ == "__main__":
    unittest.main()
