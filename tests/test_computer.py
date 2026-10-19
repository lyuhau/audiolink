"""This computer's audiolink key (client/computer.py)."""
import os
import shutil
import subprocess
import tempfile
import unittest

from audiolink import auth
from audiolink.client import computer
from audiolink.crypto import ed25519_public, ed25519_verify


class ComputerKey(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_made_once_then_the_same(self):
        k1 = computer.key(self.dir)
        k2 = computer.key(self.dir)
        self.assertEqual(k1.pub, k2.pub)
        self.assertEqual(len(k1.seed), 32)
        self.assertEqual(ed25519_public(k1.seed), k1.pub)
        pub_line = open(os.path.join(self.dir, "key.pub")).read()
        self.assertTrue(pub_line.startswith("ssh-ed25519 "))
        self.assertEqual(computer.parse_public(pub_line), k1.pub)

    @unittest.skipIf(os.name == "nt", "mode bits")
    def test_private_file_is_user_only(self):
        computer.key(self.dir)
        self.assertEqual(os.stat(os.path.join(self.dir, "key")).st_mode & 0o777, 0o600)

    def test_signs_the_auth_message(self):
        k = computer.key(self.dir)
        nonce, pi = os.urandom(16), ed25519_public(os.urandom(32))
        self.assertTrue(auth.verify(k.pub, nonce, pi, k.sign(auth.auth_message(nonce, pi))))

    def test_round_trip_through_the_file_format(self):
        seed = os.urandom(32)
        p = os.path.join(self.dir, "k")
        with open(p, "w") as f:
            f.write(computer.openssh_private(seed, "me@here"))
        k = computer.Key(p)
        self.assertEqual((k.seed, k.pub), (seed, ed25519_public(seed)))
        self.assertTrue(ed25519_verify(k.pub, b"x", k.sign(b"x")))

    @unittest.skipUnless(shutil.which("ssh-keygen"), "no ssh-keygen")
    def test_ssh_keygen_reads_it(self):
        # Windows: a plain folder under the profile, as the real config folder is (mkdtemp
        # gives an OWNER RIGHTS ACL, which Windows OpenSSH refuses for a private key)
        if os.name == "nt":
            d = os.path.join(os.environ["APPDATA"], "audiolink-test-%s" % os.urandom(4).hex())
            os.makedirs(d)
        else:
            d = self.dir
        try:
            k = computer.key(os.path.join(d, "cfg"))
            out = subprocess.run(["ssh-keygen", "-y", "-f", os.path.join(d, "cfg", "key")],
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(computer.parse_public(out.stdout), k.pub)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_parse_public(self):
        pub = ed25519_public(os.urandom(32))
        self.assertEqual(computer.parse_public(pub.hex()), pub)
        self.assertEqual(computer.parse_public(computer.openssh_public(pub, "c")), pub)
        for bad in ("", "ssh-rsa AAAA", "ssh-ed25519 !!!", "abc"):
            with self.assertRaises(ValueError):
                computer.parse_public(bad)


if __name__ == "__main__":
    unittest.main()
