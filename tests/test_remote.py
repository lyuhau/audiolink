"""Network logs and updates through the endpoint (client/remote.py, host/update_cli.py),
debug keys from the client, and rotating the read secret."""
import argparse
import contextlib
import hashlib
import io
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

from audiolink.client import keyring, pairing
from audiolink.client.pairing import PiConn
from audiolink.client.remote import Key, Remote
from audiolink.crypto import ed25519_public
from audiolink.host import update_cli
from audiolink.services.access import PAIRED, DebugService
from audiolink.services.endpoint import Endpoint
from audiolink.services.identity import IdentityService
from audiolink.services.logs import LogsService
from audiolink.services.update import UpdateService

from .helpers import FakeClock, needs_tls, patch_time


class FakeUpdate(UpdateService):
    """Installs for real (into a temp dir); "restarting" makes the update the running code."""

    def __init__(self, state_dir, endpoint):
        self.restarted = False
        super().__init__(state_dir, restart=lambda: setattr(self, "restarted", True), endpoint=endpoint)

    def _restart_soon(self):
        self.restart()

    def status(self):
        st = super().status()
        st["running_hash"] = st["update_hash"] if self.restarted else "0" * 64
        return st


def pyz():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("__main__.py", "print('new')")
    return buf.getvalue()


@needs_tls
class Harness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.identity = IdentityService(self.dir)
        self.identity.claim()
        self.debug = DebugService()
        self.ep = Endpoint(self.identity, port=0, debug=self.debug, wifi_ip=lambda: "127.0.0.1", bind="127.0.0.1")
        self.update = FakeUpdate(self.dir, self.ep)
        logs = os.path.join(self.dir, "logs")
        os.mkdir(logs)
        with open(os.path.join(logs, "audiolink.log"), "w") as f:
            f.write("".join("line %d\n" % i for i in range(10)))
        LogsService(log_dir=logs, prev=os.path.join(logs, "prev"), endpoint=self.ep)
        self.ep.register("change_status", lambda conn, req: {"saved": []}, PAIRED)

    def remote(self, code=None, key=None):
        entry = {"alias": "pi", "code": code if code is not None else self.identity.code()}
        r = Remote(entry, [], key)
        r.conn = PiConn("127.0.0.1", port=self.ep.port)
        r.hello = r.conn.ask({"op": "hello"})
        self.addCleanup(r.close)
        return r


class Logs(Harness):
    def test_paired_machines_read_logs(self):
        r = self.remote()
        self.assertEqual(r.call("logs", {"action": "tail", "lines": 2})["result"]["text"], "line 8\nline 9")
        self.assertIn("audiolink.log", r.call("logs", {"action": "list"})["result"]["files"])

    def test_others_dont(self):
        with self.assertRaisesRegex(SystemExit, "Not added"):
            self.remote(code="").call("logs", {"action": "list"})


class Update(Harness):
    def run_update(self, path, **kw):
        a = argparse.Namespace(pi="pi", at=None, key=None, status=False, revert=False, pyz=path,
                               control_port=None, **kw)
        remote = self.remote()
        out = io.StringIO()
        with mock.patch("audiolink.client.remote.open_remote", lambda *args: remote), \
                patch_time(update_cli, FakeClock()), contextlib.redirect_stdout(out):
            update_cli.update_command(a)
        return out.getvalue()

    def pyz_file(self):
        path = os.path.join(self.dir, "new.pyz")
        with open(path, "wb") as f:
            f.write(pyz())
        return path

    def test_needs_debug_access(self):
        with self.assertRaisesRegex(SystemExit, "Debug access is off"):
            self.run_update(self.pyz_file())
        self.assertFalse(os.path.exists(os.path.join(self.dir, "audiolink.pyz")))

    def test_installs_over_the_network(self):
        self.debug.set({"mode": "paired"})
        out = self.run_update(self.pyz_file())
        self.assertIn("reset pins", out)                    # the warning
        self.assertIn("running %s" % hashlib.sha256(pyz()).hexdigest()[:12], out)
        with open(os.path.join(self.dir, "audiolink.pyz"), "rb") as f:
            self.assertEqual(f.read(), pyz())


class Keys(Harness):
    def key(self):
        k = Key.__new__(Key)
        k.path, k.seed = "test", os.urandom(32)
        k.pub = ed25519_public(k.seed)
        return k

    def test_key_mode_from_the_client(self):
        k = self.key()
        self.debug.set({"mode": "key", "keys": [k.pub.hex()]})
        st = self.remote(code="", key=k).call("update", {"action": "status"})["result"]
        self.assertIn("update", st)
        with self.assertRaisesRegex(SystemExit, "debug key"):
            self.remote(key=self.key()).call("update", {"action": "status"})


class WithoutTheCode(Harness):
    """Debug access without the Pi's code (--auth key or none) reaches what it needs:
    the update's status (also after the restart) and the logs."""
    key = Keys.key

    def status(self, remote):
        return update_cli.NetCaller(remote)("update.status")

    def test_key(self):
        k = self.key()
        self.debug.set({"mode": "key", "keys": [k.pub.hex()]})
        self.assertIn("running_hash", self.status(self.remote(code="", key=k)))
        r = self.remote(code="", key=k)
        self.assertEqual(r.call("logs", {"action": "tail", "lines": 1})["result"]["text"], "line 9")
        with self.assertRaisesRegex(SystemExit, "debug key"):
            self.remote(code="", key=self.key()).call("logs", {"action": "list"})

    def test_none(self):
        self.debug.set({"mode": "none"})
        self.assertIn("running_hash", self.status(self.remote(code="")))
        self.remote(code="").call("logs", {"action": "list"})
        self.debug.off("test")
        with self.assertRaisesRegex(SystemExit, "Not added"):     # nothing to go by: the paired refusal
            self.status(self.remote(code=""))


class Rotate(Harness):
    def test_rotate_secret(self):
        old = self.identity.code()
        calls = {"peer": lambda a: {"connected": True, "peer": {"caps": ["rotate_secret"]}}}
        for op in ("info", "rotate_secret", "export"):
            calls["identity." + op] = (lambda op: lambda a: self.identity.handle(op, a))(op)
        from audiolink.client import pis
        with mock.patch.object(keyring, "PATH", keyring.PATH.__class__(self.dir) / "pis.json"), \
                mock.patch("audiolink.host.control.call", lambda op, args=None, port=None: calls[op](args or {})), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            keyring.upsert(old, name="pi")
            pis.rotate_secret_command(argparse.Namespace(control_port=None, yes=True))
            entry = keyring.lookup("pi")
        new = self.identity.code()
        self.assertNotEqual(new, old)
        self.assertEqual(entry["code"], new)                # this computer keeps access
        self.assertEqual(keyring.pub_of(new), keyring.pub_of(old))      # same Pi, same key
        self.assertIn("lost access", out.getvalue())
        with self.assertRaisesRegex(SystemExit, "Not added"):
            self.remote(code=old).call("logs", {"action": "list"})
        self.remote(code=new).call("logs", {"action": "list"})
        stale = self.remote(code=old)
        self.assertTrue(pairing._outdated(stale.conn, stale.hello, {"code": old}))
        fresh = self.remote(code=new)
        self.assertFalse(pairing._outdated(fresh.conn, fresh.hello, {"code": new}))
