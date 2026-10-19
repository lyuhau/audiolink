"""services/access.py: routes, roles, and the USB link's role (link.auth, the CTRL gate)."""
import os
import shutil
import tempfile
import threading
import time
import unittest

from audiolink.auth import auth_message
from audiolink.crypto import ed25519_public, ed25519_sign
from audiolink.services import Service
from audiolink.services.access import OWNER, USER, LinkAccess, Refused, allows, route_of
from audiolink.services.identity import IdentityService
from audiolink.session import Session

from .helpers import wait


class Routes(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(route_of("192.168.4.1", "192.168.4.20", "10.0.0.5"), "setup")
        self.assertEqual(route_of("10.0.0.5", "10.0.0.9", "10.0.0.5"), "lan")
        self.assertEqual(route_of("127.0.0.1", "127.0.0.2", "10.0.0.5"), "relay")

    def test_a_lan_on_the_setup_subnet_is_the_lan(self):
        self.assertEqual(route_of("192.168.4.77", "192.168.4.20", "192.168.4.77"), "lan")

    def test_fails_closed(self):
        for local, remote, wifi in (("10.55.0.2", "10.55.0.1", "10.0.0.5"), ("127.0.0.1", "127.0.0.1", None),
                                    (None, "10.0.0.9", "10.0.0.5"), ("10.0.0.5", "10.0.0.9", None)):
            self.assertEqual(route_of(local, remote, wifi), "relay", (local, remote, wifi))
        self.assertEqual(route_of("192.168.4.1", "127.0.0.2", None), "relay")


class Roles(unittest.TestCase):
    def test_owner_covers_user(self):
        self.assertTrue(allows(OWNER, USER))
        self.assertTrue(allows(USER, None))
        self.assertFalse(allows(USER, OWNER))
        self.assertFalse(allows(None, USER))


def sign(seed, nonce, pub):
    return {"key": ed25519_public(seed).hex(), "sig": ed25519_sign(seed, auth_message(nonce, pub)).hex()}


class Link(unittest.TestCase):
    """LinkAccess on its own: what the host's key may do over USB."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.identity = IdentityService(self.dir)
        self.link = LinkAccess(self.identity)
        self.link.on_up()
        self.host, self.other = os.urandom(32), os.urandom(32)

    def auth(self, seed, nonce=None):
        return self.link.handle("auth", sign(seed, nonce or self.link.nonce, self.identity.public()))

    def test_host_jobs_need_no_key(self):
        for op in ("audio.status", "link.auth", "identity.info", "identity.claim"):
            self.link.check(op)
        for op in ("wifi.scan", "identity.rename", "update.begin", "identity.export", "relay.status"):
            with self.assertRaises(Refused) as e:
                self.link.check(op)
            self.assertEqual(e.exception.code, "not_owner")
            self.assertIn("has no owner yet", str(e.exception))

    def test_claim_makes_the_link_an_owner_at_once(self):
        self.identity.handle("claim", {"key": ed25519_public(self.host).hex(), "name": "desktop"})
        self.assertEqual(self.link.role(), OWNER)
        self.link.check("wifi.scan")

    def test_auth_gives_the_keys_role(self):
        self.identity.claim(ed25519_public(self.other), "laptop")
        self.link.on_up()                                   # another host, not the claimer
        self.assertEqual(self.auth(self.host), {"role": None})
        with self.assertRaises(Refused) as e:
            self.link.check("wifi.scan")
        self.assertEqual(str(e.exception), "this computer isn't an owner of %s: on an owner computer, "
                                           "audiolink invite NAME --owner" % self.identity.pi_name())
        self.identity.add(ed25519_public(self.host), "desktop", USER)
        self.assertEqual(self.link.role(), USER)            # looked up live
        self.identity.add(ed25519_public(self.host), "desktop", OWNER)
        self.link.check("wifi.scan")
        self.assertTrue(self.link.may_open())
        self.identity.remove(ed25519_public(self.host).hex())
        self.assertFalse(self.link.may_open())

    def test_a_proof_for_another_link_up_or_pi_is_refused(self):
        self.identity.claim(ed25519_public(self.host), "desktop")
        old = self.link.nonce
        self.link.on_up()                                   # the link came up again: a new nonce
        with self.assertRaises(ValueError):
            self.auth(self.host, nonce=old)
        with self.assertRaises(ValueError):
            self.link.handle("auth", sign(self.host, self.link.nonce, ed25519_public(os.urandom(32))))
        self.assertEqual(self.auth(self.host), {"role": OWNER})
        self.link.on_down()
        self.assertIsNone(self.link.role())                 # proved again at the next link-up


class Echo(Service):
    name = "echo"

    def handle(self, op, args):
        return args


class OverTheLink(unittest.TestCase):
    """The gate in a session: refusals come back with their code."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.identity = IdentityService(self.dir)
        self.link = LinkAccess(self.identity)
        self.pi = Session("pi", [self.identity, self.link, Echo()])
        self.pi.gate = self.link.check
        self.host = Session("host", [])
        self.running = True
        threading.Thread(target=self._pump, daemon=True).start()
        self.addCleanup(setattr, self, "running", False)
        self.assertTrue(wait(lambda: self.host.peer_alive and self.host.peer_info))

    def _pump(self):
        while self.running:
            self.pi.link.feed(self.host.link.pull(8192))
            self.host.link.feed(self.pi.link.pull(8192))
            time.sleep(0.0005)

    def test_hello_carries_a_nonce_and_the_owner_count(self):
        info = self.host.peer_info
        self.assertEqual(len(bytes.fromhex(info["nonce"])), 16)
        self.assertEqual(info["identity"]["owners"], 0)
        self.assertNotIn("claimed", info["identity"])

    def test_refused_until_claimed(self):
        with self.assertRaises(RuntimeError) as e:
            self.host.request("echo.x", {"a": 1})
        self.assertEqual(e.exception.code, "not_owner")
        seed = os.urandom(32)
        members = self.host.request("identity.claim", {"key": ed25519_public(seed).hex(), "name": "desktop"})
        self.assertEqual([m["role"] for m in members["members"]], [OWNER])
        self.assertEqual(self.host.request("echo.x", {"a": 1}), {"a": 1})
        with self.assertRaises(RuntimeError) as e:
            self.host.request("identity.claim", {"key": ed25519_public(os.urandom(32)).hex(), "name": "x"})
        self.assertEqual(e.exception.code, "claimed")

    def test_link_auth(self):
        seed = os.urandom(32)
        self.identity.claim(ed25519_public(seed), "desktop")
        nonce = bytes.fromhex(self.host.peer_info["nonce"])
        pub = bytes.fromhex(self.host.peer_info["identity"]["pub"])
        self.assertEqual(self.host.request("link.auth", sign(seed, nonce, pub)), {"role": OWNER})
        self.assertEqual(self.host.request("identity.members")["members"][0]["name"], "desktop")


if __name__ == "__main__":
    unittest.main()
