"""Command-line behaviour that needs no Pi: the keyring commands, codes, messages."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from audiolink.client import keyring, pis
from audiolink.discovery.record import code_for, new_identity

BAD = ["al1-garbage", "al1-!!", "al1-"]


def new_code():
    return code_for(new_identity())


class KeyringCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(keyring, "PATH", Path(self.tmp.name) / "pis.json")
        p.start()
        self.addCleanup(p.stop)

    def run_cmd(self, fn, **args):
        """-> (stdout, stderr, exit message or None)"""
        out, err = io.StringIO(), io.StringIO()
        code = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                fn(SimpleNamespace(**args))
            except SystemExit as e:
                code = e.code
        return out.getvalue(), err.getvalue(), code


class Codes(KeyringCase):
    def test_malformed_codes_say_so(self):
        from audiolink.client import find
        for bad in BAD:
            for fn, args in ((pis.forget_command, {"name": bad}), (pis.share_command, {"name": bad}),
                             (pis.rename_command, {"name": bad, "new": "x", "on_pi": False}),
                             (find.find_command, {"pi": bad}), (find.connect_command, {"pi": bad, "port": 22})):
                _, _, code = self.run_cmd(fn, **args)
                self.assertTrue(code.endswith(keyring.NOT_A_CODE), (fn.__name__, bad, code))

    def test_unknown_code_isnt_a_pi_here(self):
        code = new_code()
        for fn, args in ((pis.forget_command, {"name": code}),
                         (pis.rename_command, {"name": code, "new": "x", "on_pi": False})):
            _, _, msg = self.run_cmd(fn, **args)
            self.assertIn("No Pi with that code here", msg)
        self.assertEqual(keyring.load(), {})
        self.assertEqual(keyring.resolve(code)["code"], code)      # still usable for connect/find

    def test_ambiguous_name(self):
        keyring.upsert(new_code(), name="kitchen", alias="a")
        keyring.upsert(new_code(), name="kitchen", alias="b")
        _, _, msg = self.run_cmd(pis.share_command, name="kitchen")
        self.assertEqual(msg, "2 Pis call themselves kitchen; use the name in the first column (audiolink pis)")
        out, _, _ = self.run_cmd(pis.pis_command)
        self.assertIn("tag", out)
        self.assertIn("NETWORK", out)

    def test_last_seen_only_on_contact(self):
        code = new_code()
        e, _ = keyring.upsert(code, name="pi")
        self.assertNotIn("last_seen", e)
        keyring.seen(keyring.pub_of(code), at=1000)
        self.assertEqual(keyring.load()[keyring.pub_of(code)]["last_seen"], 1000)
        keyring.seen(keyring.pub_of(code), at=500)                  # older: kept
        self.assertEqual(keyring.load()[keyring.pub_of(code)]["last_seen"], 1000)
        e, _ = keyring.upsert(code, seen=True)
        self.assertGreater(e["last_seen"], 1000)

    def test_add_code_checks_it(self):
        code = new_code()
        with mock.patch.object(pis, "_check_code", return_value=(None, None)):
            out, _, _ = self.run_cmd(pis._add_code, code=code, name=None, no_lookup=False)
        self.assertIn("not checked", out)
        self.assertNotIn("last_seen", keyring.load()[keyring.pub_of(code)])
        with mock.patch.object(pis, "_check_code", return_value=("green-rowan", None)):
            out, _, _ = self.run_cmd(pis._add_code, code=new_code(), name=None, no_lookup=False)
        self.assertIn("Added green-rowan", out)
        self.assertNotIn("not checked", out)

    def test_share_warns_on_stderr(self):
        code = new_code()
        keyring.upsert(code, name="pi")
        out, err, _ = self.run_cmd(pis.share_command, name="pi")
        self.assertEqual(out.strip(), code)
        self.assertIn("rotate-secret", err)

    def test_forget(self):
        code = new_code()
        keyring.upsert(code, name="pi", custody={"seed": "00"})
        with mock.patch("sys.stdin.isatty", return_value=False):
            _, _, msg = self.run_cmd(pis.forget_command, name="pi", yes=False)
        self.assertIn("--yes", msg)                                 # asks first
        self.assertIn(keyring.pub_of(code), keyring.load())
        out, _, msg = self.run_cmd(pis.forget_command, name="pi", yes=True)
        self.assertIsNone(msg)
        self.assertIn("rotate-secret", out)
        self.assertEqual(keyring.load(), {})

    def test_rename(self):
        keyring.upsert(new_code(), name="kitchen")
        keyring.upsert(new_code(), name="den")
        _, _, msg = self.run_cmd(pis.rename_command, name="den", new="kitchen", on_pi=False)
        self.assertIn("kitchen is taken", msg)
        out, _, msg = self.run_cmd(pis.rename_command, name="den", new="den", on_pi=False)
        self.assertIn("already called den", out)
        out, _, msg = self.run_cmd(pis.rename_command, name="den", new="study", on_pi=False)
        self.assertIsNone(msg)
        self.assertEqual(keyring.resolve("study")["name"], "den")

    def test_rotate_secret_needs_a_known_pi(self):
        from audiolink.host import control
        answers = {"peer": {"connected": True, "peer": {"caps": ["rotate_secret"]}},
                   "identity.info": {"pub": "ab" * 32, "name": "pi", "persistent": True, "serial": "1"}}
        with mock.patch.object(control, "call", side_effect=lambda op, args=None, port=None: answers[op]):
            _, _, msg = self.run_cmd(pis.rotate_secret_command, control_port=None, yes=True)
        self.assertIn("Add it first: audiolink add", msg)
        self.assertEqual(keyring.load(), {})


class Lock(KeyringCase):
    def test_processes_dont_lose_each_others_changes(self):
        script = ("import sys\nfrom audiolink.client import keyring\nfrom audiolink.discovery.record import "
                  "code_for, new_identity\nfor _ in range(15):\n    keyring.upsert(code_for(new_identity()))\n")
        env = dict(os.environ, AUDIOLINK_CONFIG_DIR=self.tmp.name)
        root = str(Path(__file__).resolve().parent.parent)
        procs = [subprocess.Popen([sys.executable, "-c", script], env=env, cwd=root) for _ in range(3)]
        for p in procs:
            self.assertEqual(p.wait(60), 0)
        self.assertEqual(len(keyring.load()), 45)

    def test_reentrant(self):
        with keyring.changing():
            keyring.upsert(new_code(), name="pi")                   # nests changing()
        self.assertEqual(len(keyring.load()), 1)


class Reaching(KeyringCase):
    def setUp(self):
        super().setUp()
        from audiolink.client import find
        self.find = find
        self.code = new_code()
        keyring.upsert(self.code, name="kitchen")
        keyring.update(keyring.pub_of(self.code), last_record={"ts": 1, "ssid": "HomeWifi", "lan": [], "ports": []})
        for target, value in (("_cached", None), ("_try_connect", None)):
            p = mock.patch.object(find, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_offline_on_the_relay_skips_the_dht(self):
        from audiolink.relay.wire import Refused
        with mock.patch("audiolink.relay.client.connect", side_effect=Refused("offline")), \
                mock.patch.object(self.find, "lookup") as lookup:
            _, _, msg = self.run_cmd(self.find.connect_command, pi="kitchen", port=2222)
        lookup.assert_not_called()
        self.assertRegex(msg, r"^audiolink: kitchen is offline \(not on its relay; last record .* on HomeWifi\)$")

    def test_port_not_offered(self):
        from audiolink.relay.client import Denied
        with mock.patch("audiolink.relay.client.connect", side_effect=Denied("port 2222 isn't open over the relay")):
            _, _, msg = self.run_cmd(self.find.connect_command, pi="kitchen", port=2222)
        self.assertEqual(msg, "audiolink: kitchen isn't offering port 2222 (on its host: audiolink host "
                              "--expose 2222:22)")

    def test_find(self):
        rec = {"ts": 1, "ssid": "HomeWifi", "lan": [], "ports": [], "name": "kitchen"}
        with mock.patch.object(self.find, "_relay_state", return_value="offline"), \
                mock.patch.object(self.find, "lookup", return_value=(rec, 1.0)):
            out, _, msg = self.run_cmd(self.find.find_command, pi="kitchen")
        self.assertIn("kitchen: offline (not on its relay)", out)
        self.assertIn("Ports: none", out)
        with mock.patch.object(self.find, "_relay_state", return_value="offline"), \
                mock.patch.object(self.find, "lookup", return_value=(None, 1.0)):
            _, _, msg = self.run_cmd(self.find.find_command, pi="kitchen")
        self.assertIn("ask its owner", msg)

    def test_ssh_config(self):
        out, err, _ = self.run_cmd(pis.ssh_config_command, user=None, port=2222)
        self.assertIn("plugged into", out)
        self.assertIn("offered no ports", err)
        self.assertIn("--user", err)
        self.assertEqual(pis._quote(r"C:\Program Files\audiolink.exe", windows=True),
                         r'"C:\Program Files\audiolink.exe"')


class Discovery(unittest.TestCase):
    def test_status_lines(self):
        from audiolink.host.discovery import status_line
        now = 1_800_000_000
        base = {"now": now, "clock_known": True, "online": True, "published": None, "error": None}
        for st, on, want in (
                ({}, False, "not published (turn on: audiolink discovery enable); the relay works either way"),
                ({"clock_known": False}, True, "waiting for the time"),
                ({"online": False, "published": now - 600}, True, "waiting for internet; last published 10 min ago"),
                ({"error": "boom", "published": now - 60}, True, "publishing failed (retrying; last published 1 min"),
                ({}, True, "publishing shortly"),
                ({"published": now - 5 * 3600}, True, "last published 5 h ago: record probably expired (offline?)"),
                ({"published": now - 20}, True, "published just now")):
            self.assertIn(want, status_line("pi", dict(base, **st), on))


def wifi_args(**kw):
    base = dict(action=None, name=None, security=None, open=False, hidden=False, reenter=False, portal=False,
                no_portal=True, password_stdin=False, pi=None, via_network=False, tries=None, at=None,
                control_port=1)
    return SimpleNamespace(**dict(base, **kw))


class WifiHost(unittest.TestCase):
    """`audiolink wifi` through the host daemon (control.call faked)."""

    def run_wifi(self, fn, answers, *args):
        from audiolink.host import wifi_cli
        calls = []

        def call(op, args=None, port=None):
            calls.append((op, args))
            a = answers[op]
            return a(args) if callable(a) else a
        out = io.StringIO()
        code = None
        with mock.patch.object(wifi_cli, "call", call), contextlib.redirect_stdout(out):
            try:
                fn(*args)
            except SystemExit as e:
                code = e.code
        return out.getvalue(), code, calls

    def test_three_wrong_passwords_say_so_three_times(self):
        from audiolink.host import wifi_cli
        answers = {"wifi.list": [], "wifi.scan": [{"ssid": "Net", "security": "wpa2", "signal": -50, "freq": 2412,
                                                   "saved": False}],
                   "wifi.join": {"joined": False, "code": "auth_failed", "reason": "wrong"}}
        with mock.patch.object(wifi_cli, "wifi_password", return_value=("password", True)):
            out, code, _ = self.run_wifi(wifi_cli.join_flow, answers, "Net", wifi_args(), 1)
        self.assertEqual(out.count("wrong password"), 3)
        self.assertIn("staying on its known networks", code)

    def test_picking_a_known_network_uses_its_saved_password(self):
        from audiolink.host import wifi_cli
        nets = [{"ssid": "Home", "security": "wpa2", "signal": -50, "freq": 2412, "saved": True},
                {"ssid": "Cafe", "security": "open", "signal": -60, "freq": 2412, "saved": False}]
        answers = {"wifi.scan": nets, "wifi.status": {"ssid": None}}
        for pick, known in ((nets[0], None), (nets[1], "open")):
            with mock.patch.object(wifi_cli, "join_flow") as join, \
                    mock.patch("audiolink.ui.Prompter.choose", return_value=pick), \
                    mock.patch("sys.stdin.isatty", return_value=True):
                self.run_wifi(wifi_cli.pick_flow, answers, wifi_args(), 1)
            self.assertEqual(join.call_args[1]["known_security"], known)

    def test_five_ghz_looks_like(self):
        from audiolink.host import wifi_cli
        for name in ("Home5G", "Home-5GHz", "Home 5G", "Home_5"):
            msg, twin = wifi_cli._suggest(name, [{"ssid": "Home"}])
            self.assertIn("looks like a 5 GHz network", msg)
            self.assertEqual(twin, "Home")
        self.assertNotIn("5 GHz", wifi_cli._suggest("Room 5", [])[0])

    def test_forget(self):
        from audiolink.host import wifi_cli
        peer = {"connected": True, "peer": {"caps": ["wifi"]}}
        answers = {"peer": peer, "wifi.list": [{"ssid": "Home"}], "wifi.status": {"ssid": "Home"},
                   "wifi.forget": {}}
        out, code, calls = self.run_wifi(wifi_cli.wifi_command, answers, wifi_args(action="forget", name="Foo"))
        self.assertEqual(code, "Foo isn't a known network (see: audiolink wifi list)")
        self.assertNotIn("wifi.forget", [c[0] for c in calls])
        out, code, _ = self.run_wifi(wifi_cli.wifi_command, answers, wifi_args(action="forget", name="Home"))
        self.assertIn("was on it", out)

    def test_check_exit_codes(self):
        from audiolink.host import wifi_cli
        peer = {"connected": True, "peer": {"caps": ["wifi"]}}
        for r, want, text in (({"internet": "online", "ssid": "N"}, 0, "online"),
                              ({"internet": "portal", "ssid": "N"}, 3, "sign-in page"),
                              ({"internet": "offline", "ssid": "N"}, 4, "no internet"),
                              ({"internet": "offline", "ssid": None}, 4, "not on Wi-Fi")):
            out, code, _ = self.run_wifi(wifi_cli.wifi_command, {"peer": peer, "wifi.check": r},
                                         wifi_args(action="check"))
            self.assertEqual((code, text in out), (want, True))

    def test_extra_name_is_an_error(self):
        from audiolink.host import wifi_cli
        _, code, calls = self.run_wifi(wifi_cli.wifi_command, {}, wifi_args(action="status", name="extra"))
        self.assertEqual(code, "audiolink wifi status takes no network name")


class WifiNetwork(unittest.TestCase):
    def test_flags_only_where_they_work(self):
        from audiolink.client import wifi_remote
        for action in (None, "scan", "list", "forget", "portal", "ap"):
            for kw in ({"pi": "x"}, {"via_network": True}, {"at": ["1.2.3.4"]}, {"tries": 2}):
                with self.assertRaisesRegex(SystemExit, "work only with join and status/check"):
                    wifi_remote.over_network(wifi_args(action=action, **kw))
        self.assertTrue(wifi_remote.over_network(wifi_args(action="status", pi="x")))
        self.assertFalse(wifi_remote.over_network(wifi_args(action="status")))

    def test_join_needs_a_name_before_searching(self):
        from audiolink.client import wifi_remote
        with mock.patch.object(wifi_remote, "reach") as reach:
            with self.assertRaisesRegex(SystemExit, "Usage"):
                wifi_remote.change_command(wifi_args(action="join"))
        reach.assert_not_called()

    def test_pi_uses_the_scanned_security(self):
        from audiolink.services import wifi_remote

        class Wifi:
            def last_scan(self):
                return 5, [{"ssid": "Cafe", "security": "open", "freq": 2412}]

            def saved_entry(self, ssid):
                return None

            def saved_names(self):
                return []

            def current_ssid(self):
                return "Home"

        class Endpoint:
            def __init__(self):
                self.ops = {}

            def register(self, op, fn, level=None):
                self.ops[op] = fn

        class Radio:
            def run(self, fn, *args):
                self.args = args
                return {"accepted": True}

            def submit(self, *args):
                pass

        class Conn:
            route = "lan"

            def open(self, blob):
                return {"ssid": "Cafe"}             # no security given

        ep, radio = Endpoint(), Radio()
        with mock.patch.object(wifi_remote, "cleanup_trial"):
            wifi_remote.WifiRemoteService(Wifi(), None, ep, None, radio)
        self.assertEqual(ep.ops["change_status"](Conn(), {"ssid": "Cafe"})["security"], "open")
        self.assertEqual(ep.ops["change_wifi"](Conn(), {"sealed": ""}), {"accepted": True})
        args, ent = radio.args[0], radio.args[1]
        self.assertEqual((args["security"], ent["security"]), ("open", "open"))


if __name__ == "__main__":
    unittest.main()


class Help(unittest.TestCase):
    def test_every_command_and_argument_says_what_it_is(self):
        import argparse
        from audiolink.__main__ import build_parser
        sub = [a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)][0]
        for name, p in sub.choices.items():
            self.assertTrue(p.description, name)
            for act in p._actions:
                self.assertTrue(act.help, (name, act.option_strings or act.dest))
