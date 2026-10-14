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


class Replies(unittest.TestCase):
    def test_a_reply_without_its_fields(self):
        class Conn:
            def ask(self, obj):
                return {}
        with self.assertRaisesRegex(SystemExit, "bad reply"):
            pairing.pair(Conn(), "123456")

    def test_code_for_an_unclaimed_pi_over_usb(self):
        from audiolink.crypto import ed25519_public
        import os
        pub = ed25519_public(os.urandom(32))
        entry = {"alias": "pi", "code": "x"}
        calls = {"identity.info": {"pub": pub.hex()}, "pairing.new_code": {"code": None, "unclaimed": True}}
        with mock.patch.object(keyring, "lookup", lambda name: entry), \
                mock.patch.object(keyring, "pub_of", lambda code: pub.hex()), \
                mock.patch("audiolink.host.control.call", lambda op, args=None, port=None: calls[op]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            pairing.code_command(argparse.Namespace(pi="pi", at=None, control_port=None))
        self.assertIn("pi is unclaimed: no code needed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
