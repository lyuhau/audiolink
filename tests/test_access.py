"""services/access.py (levels x routes x debug modes) and the endpoint's refusals."""
import json
import os
import shutil
import socket
import tempfile
import unittest

from audiolink.crypto import ed25519_public, ed25519_sign
from audiolink.paths import save_state
from audiolink.services import access
from audiolink.services.access import (DEBUG, PAIRED, PUBLIC, DebugService, decide, parse_duration,
                                       parse_key, route_of)
from audiolink.services.endpoint import Endpoint, key_message, proof_for
from audiolink.services.identity import IdentityService

from .helpers import FakeClock, patch_time

NOW = 1000.0
LIVE, EXPIRED = NOW + 60, NOW - 1
NETWORK = ("setup", "lan", "relay")
NO = None                                   # refused whatever is proven

# Debug level: mode -> {route: the proof it takes ("" = none needed), or NO}.
DEBUG_TABLE = {
    "off":        {"setup": NO, "lan": NO, "relay": NO},
    "paired":     {"setup": "paired", "lan": "paired", "relay": "paired"},
    "key":        {"setup": "key", "lan": "key", "relay": "key"},
    "none":       {"setup": NO, "lan": "", "relay": NO},
    "none+setup": {"setup": "", "lan": "", "relay": NO},
}
PROOFS = (frozenset(), frozenset(["paired"]), frozenset(["key"]), frozenset(["paired", "key"]))


def switch(mode, deadline=LIVE):
    m, _, flag = mode.partition("+")
    return {"mode": m, "setup": flag == "setup", "deadline": deadline}


class Decide(unittest.TestCase):
    def test_every_cell(self):
        for mode, routes in DEBUG_TABLE.items():
            for route in NETWORK:
                for proven in PROOFS:
                    for level in (PUBLIC, PAIRED, DEBUG):
                        if level == PUBLIC:
                            want = True
                        elif level == PAIRED:
                            want = "paired" in proven
                        else:
                            need = routes[route]
                            want = need is not NO and (need == "" or need in proven)
                        got = decide(level, route, proven, switch(mode), NOW)
                        self.assertEqual(got is None, want, (level, mode, route, sorted(proven), got))

    def test_usb_is_never_refused(self):
        for mode in DEBUG_TABLE:
            for level in (PUBLIC, PAIRED, DEBUG):
                self.assertIsNone(decide(level, "usb", set(), switch(mode), NOW))

    def test_every_mode_expires(self):
        for mode in DEBUG_TABLE:
            for route in NETWORK:
                got = decide(DEBUG, route, {"paired", "key"}, switch(mode, EXPIRED), NOW)
                self.assertEqual(got[0], "debug_off", (mode, route))

    def test_paired_level_ignores_the_switch(self):
        self.assertIsNone(decide(PAIRED, "relay", {"paired"}, switch("off", EXPIRED), NOW))

    def test_none_is_refused_on_the_relay_and_off_setup_without_the_flag(self):
        self.assertEqual(decide(DEBUG, "relay", set(), switch("none+setup"), NOW)[0], "debug_route")
        self.assertEqual(decide(DEBUG, "setup", set(), switch("none"), NOW)[0], "debug_route")


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


class Parsing(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(parse_duration("30m"), 1800)
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("1d"), 86400)
        for bad in ("", "5", "0m", "30 minutes", "9d"):
            with self.assertRaises(ValueError, msg=bad):
                parse_duration(bad)

    def test_keys(self):
        import base64
        import struct
        pub = ed25519_public(b"\1" * 32)
        blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + pub
        self.assertEqual(parse_key("ssh-ed25519 %s me@there" % base64.b64encode(blob).decode()), pub)
        self.assertEqual(parse_key(pub.hex()), pub)
        for bad in ("ssh-rsa AAAAB3Nza", "abc", "ssh-ed25519 AAAA"):
            with self.assertRaises(ValueError, msg=bad):
                parse_key(bad)


class Switch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.clock = FakeClock()
        p = patch_time(access, self.clock)
        p.start()
        self.addCleanup(p.stop)

    def test_defaults_and_expiry_end_access(self):
        d = DebugService(self.dir)
        ended = []
        d.on_end(ended.append)
        self.assertEqual(d.set({"mode": "paired"})["expires_in"], 24 * 3600)
        self.assertEqual(d.set({"mode": "none"})["expires_in"], 3600)
        self.assertEqual(d.status()["routes"], ["lan"])
        self.clock.advance(3599)
        d.check()
        self.assertEqual(d.status()["mode"], "none")
        self.clock.advance(1)
        d.check()
        self.assertEqual(d.status()["mode"], "off")
        self.assertEqual(ended, ["expired"])

    def test_key_mode_needs_a_key(self):
        with self.assertRaises(ValueError):
            DebugService().set({"mode": "key"})

    def test_ram_unless_persist(self):
        DebugService(self.dir).set({"mode": "paired"})
        self.assertEqual(DebugService(self.dir).status()["mode"], "off")
        DebugService(self.dir).set({"mode": "paired", "for": 7200, "persist": True})
        self.clock.advance(600)
        again = DebugService(self.dir).status()
        self.assertEqual(again["mode"], "paired")
        self.assertLessEqual(again["expires_in"], 7200)
        DebugService(self.dir).set({"mode": "none"})        # not kept: replaces the kept one
        self.assertEqual(DebugService(self.dir).status()["mode"], "off")

    def test_off_clears_a_kept_switch(self):
        DebugService(self.dir).set({"mode": "paired", "persist": True})
        DebugService(self.dir).off("reset pins")
        self.assertEqual(DebugService(self.dir).status()["mode"], "off")

    def test_damaged_file_reads_as_off(self):
        save_state(os.path.join(self.dir, "debug.json"), {"mode": "key", "persist": True, "for": "x"})
        self.assertEqual(DebugService(self.dir).status()["mode"], "off")
        save_state(os.path.join(self.dir, "debug.json"), {"mode": "key", "persist": True, "for": 60,
                                                           "left": 60, "keys": []})
        self.assertEqual(DebugService(self.dir).status()["mode"], "off")


class Client:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.f = self.sock.makefile("rwb")
        self.hello = self.ask({"op": "hello"})
        self.nonce = bytes.fromhex(self.hello["nonce"])

    def ask(self, obj):
        self.f.write((json.dumps(obj) + "\n").encode())
        self.f.flush()
        return json.loads(self.f.readline())

    def close(self):
        self.sock.close()


class EndpointAccess(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.identity = IdentityService(self.dir)
        self.identity.claim()
        self.secret = self.identity.keys()[1]
        self.debug = DebugService()
        self.wifi_ip = "127.0.0.1"                  # loopback stands in for the LAN; None: unplaceable
        self.ep = Endpoint(self.identity, port=0, debug=self.debug, wifi_ip=lambda: self.wifi_ip,
                           bind="127.0.0.1")
        self.ep.register("look", lambda conn, req: {"ok": True, "route": conn.route}, PAIRED)
        self.ep.register("root", lambda conn, req: {"ok": True}, DEBUG)

    def client(self):
        c = Client(self.ep.port)
        self.addCleanup(c.close)
        return c

    def test_paired_needs_the_proof(self):
        c = self.client()
        self.assertEqual(c.ask({"op": "look"})["code"], "not_paired")
        self.assertEqual(c.ask({"op": "look", "proof": proof_for(b"x" * 16, "look", c.nonce)})["code"],
                         "not_paired")
        self.assertEqual(c.ask({"op": "look", "proof": proof_for(self.secret, "look", c.nonce)})["route"], "lan")

    def test_proof_is_per_op(self):
        c = self.client()
        r = c.ask({"op": "root", "proof": proof_for(self.secret, "look", c.nonce)})
        self.assertEqual(r["code"], "debug_off")
        self.debug.set({"mode": "paired"})
        self.assertEqual(c.ask({"op": "root", "proof": proof_for(self.secret, "look", c.nonce)})["code"],
                         "not_paired")
        self.assertTrue(c.ask({"op": "root", "proof": proof_for(self.secret, "root", c.nonce)})["ok"])

    def test_key_mode(self):
        seed = os.urandom(32)
        pub = ed25519_public(seed)
        self.debug.set({"mode": "key", "keys": [pub.hex()]})
        c = self.client()
        self.assertEqual(c.ask({"op": "root", "proof": proof_for(self.secret, "root", c.nonce)})["code"],
                         "debug_key")
        other = os.urandom(32)
        bad = {"op": "root", "key": ed25519_public(other).hex(),
               "sig": ed25519_sign(other, key_message("root", c.nonce)).hex()}
        self.assertEqual(c.ask(bad)["code"], "debug_key")
        good = {"op": "root", "key": pub.hex(), "sig": ed25519_sign(seed, key_message("root", c.nonce)).hex()}
        self.assertTrue(c.ask(good)["ok"])
        self.assertTrue(c.ask(good)["ok"])                  # again on the same connection
        replayed = self.client()
        self.assertEqual(replayed.ask(good)["code"], "debug_key")     # another connection, another nonce

    def test_none_on_the_lan_but_not_unplaceable(self):
        self.debug.set({"mode": "none", "setup": True})
        self.assertTrue(self.client().ask({"op": "root"})["ok"])
        self.wifi_ip = None                                 # can't tell the route: the relay's rules
        self.assertEqual(self.client().ask({"op": "root"})["code"], "debug_route")

    def test_off_after_on(self):
        self.debug.set({"mode": "none"})
        c = self.client()
        self.assertTrue(c.ask({"op": "root"})["ok"])
        self.debug.off("test")
        self.assertEqual(c.ask({"op": "root"})["code"], "debug_off")

    def test_unclaimed_pi_has_no_paired_machines(self):
        self.identity.start_over()
        c = self.client()
        self.assertEqual(c.ask({"op": "look", "proof": proof_for(self.identity.keys()[1], "look", c.nonce)})
                         ["code"], "not_paired")


class DebugCommand(unittest.TestCase):
    """`audiolink debug`, against a DebugService standing in for the plugged-in Pi."""

    def run_cmd(self, *argv):
        import contextlib
        import io
        from unittest import mock
        from audiolink.__main__ import main
        svc = self.svc

        def call(op, args=None, port=None):
            if op == "peer":
                return {"connected": True, "peer": {"caps": ["debug"]}}
            return svc.handle(op.split(".", 1)[1], args or {})
        out = io.StringIO()
        with mock.patch("audiolink.host.control.call", call), mock.patch("audiolink.host.debug_cli.call", call), \
                contextlib.redirect_stdout(out):
            main(["debug"] + list(argv))
        return out.getvalue()

    def setUp(self):
        self.svc = DebugService()

    def test_on_status_off(self):
        out = self.run_cmd("on", "--for", "2h")
        self.assertIn("Debug: on, computers that added it", out)
        self.assertIn("Routes: USB, LAN, setup network, relay", out)
        self.assertIn("Ends in 2 h", out)
        self.assertIn("Routes: USB, LAN\n", self.run_cmd("on", "--auth", "none"))
        self.assertIn("Debug: on", self.run_cmd("status"))
        self.assertEqual(self.run_cmd("off"), "Debug: off\n")
        self.assertEqual(self.svc.status()["mode"], "off")

    def test_key_files(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "k.pub")
        with open(path, "w") as f:
            f.write("# keys\n%s\n%s\n" % (ed25519_public(b"\1" * 32).hex(), ed25519_public(b"\2" * 32).hex()))
        self.assertIn("debug keys (2)", self.run_cmd("on", "--key", path))
        self.assertEqual(len(self.svc.keys()), 2)

    def test_refuses_mismatched_options(self):
        for argv in (["on", "--auth", "key"], ["on", "--setup"], ["on", "--for", "forever"]):
            with self.assertRaises(SystemExit, msg=argv):
                self.run_cmd(*argv)
