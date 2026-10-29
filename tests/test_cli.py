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
from tests.helpers import needs_symlinks

BAD = ["al2-garbage", "al2-!!", "al2-", "al1-short"]


def new_code():
    return keyring.code_for(os.urandom(32))


class KeyringCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(keyring, "PATH", Path(self.tmp.name) / "pis.json")
        p.start()
        self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"AUDIOLINK_CONFIG_DIR": self.tmp.name})   # this computer's key too
        env.start()
        self.addCleanup(env.stop)

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


class FakeRemote:
    """client/remote.Remote, already found and authed: answers from a dict of ops."""
    instances = []

    def __init__(self, entry, at, key=None, role="owner", answers=None, fail=None):
        self.entry, self.name, self.role, self.answers, self.fail = entry, entry.get("alias"), role, answers or {}, fail
        self.calls, self.closed = [], False
        self.key = SimpleNamespace(pub=b"\x01" * 32)
        self.conn = SimpleNamespace(ip="10.0.0.5", role=role, record=None)
        self.hello = {"name": entry.get("name")}
        FakeRemote.instances.append(self)

    def open(self, quiet=False):
        if self.fail:
            raise SystemExit(self.fail)
        return self

    def call(self, op, args=None):
        self.calls.append((op, args))
        r = self.answers.get(op, {})
        if callable(r):
            r = r(args)
        if "error" in r:
            raise SystemExit(r["error"])
        return r

    def close(self):
        self.closed = True


def fake_remotes(test, **kw):
    """Patch client/remote.Remote with FakeRemote(**kw); returns the list of instances."""
    FakeRemote.instances = []
    p = mock.patch("audiolink.client.remote.Remote", lambda entry, at, key=None: FakeRemote(entry, at, key, **kw))
    p.start()
    test.addCleanup(p.stop)
    return FakeRemote.instances


class Codes(KeyringCase):
    def test_code_is_the_public_key(self):
        pub = os.urandom(32)
        code = keyring.code_for(pub)
        self.assertTrue(code.startswith("al2-"))
        self.assertEqual(keyring.parse(code), pub)
        self.assertEqual(keyring.code_for(pub.hex()), code)

    def test_an_old_code_is_read_for_its_key(self):
        import base64
        pub = os.urandom(32)
        old = "al1-" + base64.urlsafe_b64encode(pub + os.urandom(16)).decode().rstrip("=")
        self.assertEqual(keyring.parse(old), pub)
        keyring.save({pub.hex(): {"code": old, "alias": "pi", "custody": {"seed": "00"}}})
        e = keyring.load()[pub.hex()]
        self.assertEqual(e["code"], keyring.code_for(pub))           # no secret kept
        self.assertNotIn("custody", e)                               # nor the Pi's private key

    def test_malformed_codes_say_so(self):
        from audiolink.client import find
        for bad in BAD:
            for fn, args in ((pis.forget_command, {"name": bad}),
                             (pis.rename_command, {"name": bad, "new": "x", "on_pi": False}),
                             (find.connect_command, {"pi": bad, "port": 22})):
                _, _, code = self.run_cmd(fn, **args)
                self.assertTrue(code.endswith(keyring.NOT_A_CODE), (fn.__name__, bad, code))

    def test_unknown_code_isnt_a_pi_here(self):
        code = new_code()
        for fn, args in ((pis.forget_command, {"name": code}),
                         (pis.rename_command, {"name": code, "new": "x", "on_pi": False})):
            _, _, msg = self.run_cmd(fn, **args)
            self.assertIn("No Pi with that code here", msg)
        self.assertEqual(keyring.load(), {})
        self.assertEqual(keyring.resolve(code)["code"], code)      # still usable for connect

    def test_ambiguous_name(self):
        keyring.upsert(new_code(), name="kitchen", alias="a")
        keyring.upsert(new_code(), name="kitchen", alias="b")
        _, _, msg = self.run_cmd(pis.forget_command, name="kitchen")
        self.assertEqual(msg, "2 Pis call themselves kitchen; use the name in the first column (audiolink pis)")
        out, _, _ = self.run_cmd(pis.pis_command, pi=None)
        self.assertIn("tag", out)
        self.assertIn("NETWORK", out)
        self.assertIn("ROLE", out)

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

    def test_forget_takes_itself_off_the_list(self):
        code = new_code()
        keyring.upsert(code, name="pi")
        remotes = fake_remotes(self, role="user")
        out, _, msg = self.run_cmd(pis.forget_command, name="pi")
        self.assertIsNone(msg)
        self.assertIn("off its list too", out)
        self.assertEqual(remotes[0].calls, [("remove", {"key": ("01" * 32)})])
        self.assertEqual(keyring.load(), {})

    def test_forget_when_unreachable(self):
        keyring.upsert(new_code(), name="pi")
        fake_remotes(self, fail="pi isn't reachable from here")
        out, _, msg = self.run_cmd(pis.forget_command, name="pi")
        self.assertIsNone(msg)
        self.assertIn("still on its list", out)
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


class Lists(KeyringCase):
    """invite, remove, pis PI: an owner's requests to a Pi (the Pi faked)."""

    def setUp(self):
        super().setUp()
        self.code = new_code()
        keyring.upsert(self.code, name="green-rowan")
        keyring.update(keyring.pub_of(self.code), role="owner")
        p = mock.patch("audiolink.host.control.call", side_effect=SystemExit("audiolink host isn't running"))
        p.start()                       # not plugged in here (a test patches its own over it)
        self.addCleanup(p.stop)

    def test_invite(self):
        remotes = fake_remotes(self, answers={"invite": {"code": "482177", "expires_in": 300}})
        out, _, msg = self.run_cmd(pis.invite_command, name="mom", owner=False, pi=None, at=None)
        self.assertIsNone(msg, out)
        self.assertEqual(remotes[0].calls, [("invite", {"name": "mom", "role": "user"})])
        self.assertIn("Code for mom: 482-177 (one use, 5 min)", out)
        self.assertIn("authorized_keys", out)

    def test_invite_over_usb_when_plugged_in(self):
        calls = []
        answers = {"peer": {"connected": True, "link": {"role": "owner"}},
                   "identity.info": {"pub": keyring.pub_of(self.code)},
                   "identity.invite": {"code": "482177", "expires_in": 300}}

        def call(op, args=None, port=None):
            calls.append((op, args))
            return answers[op]
        with mock.patch("audiolink.host.control.call", call), \
                mock.patch("audiolink.client.remote.Remote", side_effect=AssertionError("not over the network")):
            out, _, msg = self.run_cmd(pis.invite_command, name="laptop", owner=True, pi=None, at=None,
                                       control_port=None)
        self.assertIsNone(msg, out)
        self.assertIn(("identity.invite", {"name": "laptop", "role": "owner"}), calls)
        answers["peer"]["link"]["role"] = "user"                   # plugged in, but not an owner
        with mock.patch("audiolink.host.control.call", call):
            _, _, msg = self.run_cmd(pis.invite_command, name="laptop", owner=True, pi=None, at=None,
                                     control_port=None)
        self.assertIn("Only an owner of green-rowan", msg)

    def test_invite_needs_an_owner(self):
        fake_remotes(self, role="user")
        _, _, msg = self.run_cmd(pis.invite_command, name="mom", owner=True, pi=None, at=None)
        self.assertIn("Only an owner of green-rowan", msg)

    def test_invite_which_pi(self):
        other = new_code()
        keyring.upsert(other, name="blue-ash")
        keyring.update(keyring.pub_of(other), role="owner")
        _, _, msg = self.run_cmd(pis.invite_command, name="mom", owner=False, pi=None, at=None)
        self.assertIn("which one? --pi NAME", msg)

    def test_remove(self):
        members = [{"name": "mom", "key": "aa" * 32, "role": "user"}]
        remotes = fake_remotes(self, answers={"members": {"members": members}, "remove": {"ok": True}})
        out, _, msg = self.run_cmd(pis.remove_command, name="mom", pi=None, at=None)
        self.assertIsNone(msg)
        self.assertIn("mom: removed from green-rowan", out)
        self.assertIn(("remove", {"name": "mom"}), remotes[0].calls)
        out, _, msg = self.run_cmd(pis.remove_command, name="dad", pi=None, at=None)
        self.assertIn("isn't on any Pi this computer owns", out)

    def test_a_refusal_isnt_unreachable(self):
        members = [{"name": "macbook", "key": "01" * 32, "role": "owner"}]
        fake_remotes(self, answers={"members": {"members": members},
                                    "remove": {"error": "Only one owner left: reset it instead", "code": "last_owner"}})
        _, _, msg = self.run_cmd(pis.remove_command, name="macbook", pi=None, at=None)
        self.assertEqual(msg, "green-rowan: Only one owner left: reset it instead")

    def test_remove_fails_where_it_cant_reach(self):
        fake_remotes(self, fail="green-rowan is offline")
        _, _, msg = self.run_cmd(pis.remove_command, name="mom", pi=None, at=None)
        self.assertIn("Couldn't reach (nothing changed there)", msg)
        self.assertIn("green-rowan: green-rowan is offline", msg)

    def test_pis_one(self):
        members = [{"name": "macbook", "key": "01" * 32, "role": "owner", "added": 1},
                   {"name": "mom", "key": "aa" * 32, "role": "user", "added": 1}]
        keyring.update(keyring.pub_of(self.code), last_record={"ts": 1, "ssid": "HomeWifi", "lan": [], "ports": [2222]})
        fake_remotes(self, answers={"members": {"members": members}})
        out, _, msg = self.run_cmd(pis.pis_command, pi="green-rowan", at=None)
        self.assertIsNone(msg, out)
        self.assertIn("green-rowan: online, on this network (this computer: owner)", out)
        self.assertIn("Last record: on HomeWifi", out)
        self.assertIn("Code: %s" % self.code, out)
        self.assertIn("macbook", out)
        self.assertIn("(this computer)", out)
        self.assertIn("Only one owner", out)

    def test_pis_one_offline(self):
        keyring.update(keyring.pub_of(self.code), last_record={"ts": 1, "ssid": "HomeWifi", "lan": [], "ports": []})
        fake_remotes(self, fail="green-rowan is offline (not on this network, nor on its relay)")
        out, _, msg = self.run_cmd(pis.pis_command, pi="green-rowan", at=None)
        self.assertIsNone(msg)
        self.assertIn("green-rowan: green-rowan is offline", out)
        self.assertIn("Last record: on HomeWifi", out)
        self.assertNotIn("List:", out)


class Lock(KeyringCase):
    def test_processes_dont_lose_each_others_changes(self):
        script = ("import os\nfrom audiolink.client import keyring\n"
                  "for _ in range(15):\n    keyring.upsert(keyring.code_for(os.urandom(32)))\n")
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

    def test_offline_on_the_relay(self):
        from audiolink.client import remote
        with mock.patch.object(remote, "reach", return_value=(None, None)), \
                mock.patch.object(remote, "over_relay", return_value=(None, "offline")):
            _, _, msg = self.run_cmd(self.find.connect_command, pi="kitchen", port=2222)
        self.assertRegex(msg, r"^audiolink: kitchen is offline \(not on this network, nor on its relay\); "
                              r"last seen .* on HomeWifi$")

    def test_port_not_offered(self):
        conn = mock.Mock(ip="10.0.0.5", role="user", record=None)
        conn.open_stream.return_value = {"error": "port 2222 isn't exposed", "code": "not_exposed"}
        with mock.patch("audiolink.client.remote.find", return_value=(conn, {"pub": "x"}, "at 10.0.0.5", None)):
            _, _, msg = self.run_cmd(self.find.connect_command, pi="kitchen", port=2222)
        self.assertEqual(msg, "audiolink: kitchen isn't offering port 2222 (on its host: audiolink host "
                              "--expose 2222:22)")
        conn.auth.assert_called_once()

    def test_not_on_the_list(self):
        from audiolink.client.pairing import NotListed
        conn = mock.Mock(ip="10.0.0.5")
        conn.auth.side_effect = NotListed("not listed")
        with mock.patch("audiolink.client.remote.find", return_value=(conn, {"pub": "x"}, "at 10.0.0.5", None)):
            _, _, msg = self.run_cmd(self.find.connect_command, pi="kitchen", port=2222)
        self.assertIn("kitchen doesn't have this computer on its list", msg)
        self.assertIn("audiolink invite", msg)

    def test_the_record_comes_with_auth(self):
        rec = {"ts": 2, "ssid": "Office", "lan": ["10.0.0.5"], "ports": [2222], "name": "kitchen"}
        conn = mock.Mock(ip="10.0.0.5", role="user", record=rec)
        conn.open_stream.return_value = {"ok": True}
        with mock.patch("audiolink.client.remote.find", return_value=(conn, {"pub": "x"}, "at 10.0.0.5", None)):
            got = self.find._connect(SimpleNamespace(pi="kitchen", port=2222, at=None))
        self.assertIs(got, conn)
        e = keyring.lookup("kitchen")
        self.assertEqual((e["last_record"], e["role"]), (rec, "user"))
        self.assertIn("last_seen", e)

    def test_ssh_config(self):
        out, err, _ = self.run_cmd(pis.ssh_config_command, port=2222)
        self.assertIn("plugged into", out)
        self.assertIn("offered no ports", err)
        self.assertIn("  HostKeyAlias audiolink-%s\n" % keyring.pub_of(self.code), out)
        self.assertNotIn("User ", out)
        self.assertEqual(pis._quote(r"C:\Program Files\audiolink.exe", windows=True),
                         r'"C:\Program Files\audiolink.exe"')

    def test_launcher_from_a_checkout_runs_the_checkout(self):     # no PYTHONPATH needed
        checkout = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with mock.patch.object(sys, "argv", [os.path.join(checkout, "audiolink", "__main__.py")]):
            self.assertEqual(pis._launcher(), "%s %s" % (pis._quote(sys.executable), pis._quote(checkout)))

    @needs_symlinks
    def test_launcher_frozen_is_the_real_executable(self):
        d = Path(self.tmp.name)
        (d / "audiolink folder").mkdir()
        exe = d / "audiolink folder" / "audiolink"
        exe.write_text("")
        (d / "link").symlink_to(exe)
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", str(d / "link")), \
                mock.patch.object(sys, "argv", [str(d / "link")]):
            self.assertEqual(pis._launcher(), pis._quote(os.path.realpath(str(exe))))


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

        ep, radio = Endpoint(), Radio()
        with mock.patch.object(wifi_remote, "cleanup_trial"):
            wifi_remote.WifiRemoteService(Wifi(), None, ep, radio)
        self.assertEqual(ep.ops["change_status"](Conn(), {"ssid": "Cafe"})["security"], "open")
        self.assertEqual(ep.ops["change_wifi"](Conn(), {"ssid": "Cafe"}), {"accepted": True})   # no security given
        args, ent = radio.args[0], radio.args[1]
        self.assertEqual((args["security"], ent["security"]), ("open", "open"))
        Conn.route = "relay"                    # it would cut the connection the request came on
        self.assertEqual(ep.ops["change_wifi"](Conn(), {"ssid": "Cafe"})["code"], "via_relay")


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


class Output(unittest.TestCase):
    def test_a_narrow_encoding_doesnt_end_the_command(self):
        # Windows: output to a pipe or file is cp1252, which has no →
        from audiolink import ui
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="cp1252")
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", out):
            ui.safe_output()
            print("Pi port 2222 → this computer's port 22")
            out.flush()
        self.assertIn(b"2222 ? this", raw.getvalue())

    def test_no_console_is_fine(self):
        from audiolink import ui
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None):
            ui.safe_output()


class ControlMessages(unittest.TestCase):
    def test_not_running_keeps_the_port(self):
        from audiolink.host import control
        self.assertIn("start it: audiolink host;", control.not_running())
        self.assertIn("audiolink host --control-port 47811;", control.not_running(47811))
        self.assertIn("--pi PI", control.not_running())

    def test_refused_token_may_be_an_older_audiolink(self):
        import socket
        from audiolink.host import control
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        control.serve(None, srv, token="theirs")
        with mock.patch.object(control, "read_token", lambda: "mine"):
            with self.assertRaises(SystemExit) as e:
                control.call("peer", port=srv.getsockname()[1], timeout=5)
        self.assertIn("another user's", str(e.exception.code))
        self.assertIn("older than the running host's: update it", str(e.exception.code))
