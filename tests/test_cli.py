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
                self.assertEqual(code, keyring.NOT_A_CODE, (fn.__name__, bad))

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


if __name__ == "__main__":
    unittest.main()
