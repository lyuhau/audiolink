"""A Pi's endpoint from another computer (client/remote.py, host/update_cli.py, logs), against
the real Pi side: auth with this computer's key, then what its role allows."""
import argparse
import contextlib
import hashlib
import io
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from audiolink.client import computer, keyring, remote as remote_mod
from audiolink.client.pairing import PiConn
from audiolink.client.remote import Remote
from audiolink.host import update_cli
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
        z.writestr(zipfile.ZipInfo("__main__.py", (2020, 1, 1, 0, 0, 0)), "print('new')")   # same bytes every time
    return buf.getvalue()


@needs_tls
class Harness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        for p in (mock.patch.object(keyring, "PATH", Path(self.dir) / "pis.json"),
                  mock.patch.dict(os.environ, {"AUDIOLINK_CONFIG_DIR": self.dir})):
            p.start()
            self.addCleanup(p.stop)
        self.key = computer.key()                       # this computer's
        self.identity = IdentityService(os.path.join(self.dir, "pi"))
        self.identity.claim(os.urandom(32).hex(), "macbook")
        self.ep = Endpoint(self.identity, port=0, wifi_ip=lambda: "127.0.0.1", bind="127.0.0.1")
        self.update = FakeUpdate(os.path.join(self.dir, "pi"), self.ep)
        logs = os.path.join(self.dir, "logs")
        os.mkdir(logs)
        with open(os.path.join(logs, "audiolink.log"), "w") as f:
            f.write("".join("line %d\n" % i for i in range(10)))
        LogsService(log_dir=logs, prev=os.path.join(logs, "prev"), endpoint=self.ep)
        self.code = keyring.code_for(self.identity.public())
        keyring.upsert(self.code, name="pi")

    def let_in(self, role):
        self.identity.add(self.key.pub.hex(), "pc", role)

    def remote(self):
        """client/remote.Remote, found at the test's endpoint (as the LAN would)."""
        def find(entry, at, turned_away):
            conn = PiConn("127.0.0.1", port=self.ep.port)
            return conn, conn.hello(), "at 127.0.0.1", None
        with mock.patch.object(remote_mod, "find", find):
            r = Remote(keyring.lookup("pi"), []).open(quiet=True)
        self.addCleanup(r.close)
        return r


class Roles(Harness):
    def test_an_owner_reads_logs(self):
        self.let_in("owner")
        r = self.remote()
        self.assertEqual(r.role, "owner")
        self.assertEqual(r.call("logs", {"action": "tail", "lines": 2})["result"]["text"], "line 8\nline 9")
        self.assertIn("audiolink.log", r.call("logs", {"action": "list"})["result"]["files"])

    def test_a_user_doesnt(self):
        self.let_in("user")
        r = self.remote()
        with self.assertRaisesRegex(SystemExit, "Only an owner of pi can do that"):
            r.call("logs", {"action": "list"})

    def test_not_on_the_list(self):
        with self.assertRaisesRegex(SystemExit, "pi doesn't have this computer on its list"):
            self.remote()

    def test_the_record_and_role_land_in_the_keyring(self):
        self.let_in("user")
        self.remote()
        e = keyring.lookup("pi")
        self.assertEqual(e["role"], "user")
        self.assertIn("last_seen", e)

    def test_removed_mid_connection(self):
        self.let_in("owner")
        r = self.remote()
        self.identity.remove(self.key.pub.hex())
        with self.assertRaises((SystemExit, OSError, ValueError, ConnectionError)):
            r.call("logs", {"action": "list"})          # the Pi closed this computer's connections


class Update(Harness):
    def run_update(self, path):
        a = argparse.Namespace(pi="pi", at=None, status=False, back=False, image=None, code=path, control_port=None)
        r = self.remote()
        out = io.StringIO()
        with mock.patch("audiolink.client.remote.open_remote", lambda *args: r), \
                patch_time(update_cli, FakeClock()), contextlib.redirect_stdout(out):
            update_cli.update_command(a)
        return out.getvalue()

    def pyz_file(self):
        path = os.path.join(self.dir, "new.pyz")
        with open(path, "wb") as f:
            f.write(pyz())
        return path

    def test_an_owner_installs_over_the_network(self):
        self.let_in("owner")
        out = self.run_update(self.pyz_file())
        self.assertIn("running %s" % hashlib.sha256(pyz()).hexdigest()[:12], out)
        with open(os.path.join(self.dir, "pi", "audiolink.pyz"), "rb") as f:
            self.assertEqual(f.read(), pyz())

    def test_a_user_cant(self):
        self.let_in("user")
        with self.assertRaisesRegex(SystemExit, "Only an owner"):
            self.run_update(self.pyz_file())
        self.assertFalse(os.path.exists(os.path.join(self.dir, "pi", "audiolink.pyz")))


if __name__ == "__main__":
    unittest.main()
