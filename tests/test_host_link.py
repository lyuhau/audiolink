"""The host's side of the USB link's auth (host/identity.py) and its control port's token
(host/control.py)."""
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from audiolink import auth
from audiolink.client import keyring
from audiolink.client.computer import Key
from audiolink.crypto import ed25519_public
from audiolink.host import control
from audiolink.host.identity import HostIdentity


def a_key():
    k = Key.__new__(Key)
    k.seed = os.urandom(32)
    k.pub = ed25519_public(k.seed)
    return k


class LinkAuth(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.object(keyring, "PATH", Path(self.dir) / "pis.json")
        p.start()
        self.addCleanup(p.stop)
        self.key, self.pi = a_key(), ed25519_public(os.urandom(32))
        self.nonce = os.urandom(16)
        self.requests = []
        self.role = "owner"

        def request(op, args=None, timeout=None):
            self.requests.append((op, args))
            ok = auth.verify(bytes.fromhex(args["key"]), self.nonce, self.pi, bytes.fromhex(args["sig"]))
            return {"role": self.role if ok else None}
        self.svc = HostIdentity(key=self.key)
        self.svc.attach(mock.Mock(request=request))

    def hello(self, name="green-rowan"):
        return {"nonce": self.nonce.hex(), "identity": {"pub": self.pi.hex(), "name": name}}

    def test_proves_the_key_once_per_link_up(self):
        self.svc.on_peer_hello(self.hello())
        self.svc._sync()                        # (on_peer_hello runs it in a thread)
        op, args = self.requests[-1]
        self.assertEqual((op, args["key"]), ("link.auth", self.key.pub.hex()))
        self.assertEqual(self.svc.local_ops(), {"role": "owner"})

    def test_not_on_the_list_is_host_jobs_only(self):
        self.role = None
        self.svc.on_peer_hello(self.hello())
        self.svc._sync()
        self.assertEqual(self.svc.local_ops(), {"role": None})

    def test_a_new_link_starts_without_a_role(self):
        self.svc.on_peer_hello(self.hello())
        self.svc._sync()
        with mock.patch.object(threading, "Thread"):        # the new link's auth hasn't run yet
            self.svc.on_peer_hello(self.hello())
        self.assertEqual(self.svc.local_ops(), {"role": None})

    def test_refreshes_a_known_pi(self):
        keyring.upsert(keyring.code_for(self.pi), name="old-name")
        self.svc.on_event("identity", {"pub": self.pi.hex(), "name": "green-rowan"})   # e.g. renamed
        self.assertFalse(self.requests)         # no nonce yet (before any HELLO): nothing to sign
        self.svc.on_peer_hello(self.hello())
        self.svc._sync()
        e = keyring.load()[self.pi.hex()]
        self.assertEqual((e["name"], e["role"]), ("green-rowan", "owner"))
        self.assertIn("last_seen", e)


class ControlToken(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.object(control, "config_dir", lambda: Path(self.dir))
        p.start()
        self.addCleanup(p.stop)
        self.srv = control.listen(0)
        self.port = self.srv.getsockname()[1]
        self.addCleanup(self.srv.close)
        control.serve(mock.Mock(), self.srv, {"peer": lambda args: {"connected": False}})

    def test_this_user_gets_in(self):
        self.assertEqual(control.call("peer", port=self.port, timeout=5), {"connected": False})

    @unittest.skipIf(os.name == "nt", "mode bits")
    def test_the_token_is_this_users_only(self):
        self.assertEqual(os.stat(control.token_path()).st_mode & 0o777, 0o600)

    def test_another_user_doesnt(self):
        with open(control.token_path(), "w") as f:      # as if another login read its own token
            f.write("not-it")
        with self.assertRaises(SystemExit) as e:
            control.call("peer", port=self.port, timeout=5)
        self.assertIn("another user's", str(e.exception.code))


if __name__ == "__main__":
    unittest.main()
