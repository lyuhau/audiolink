"""The message a computer key signs to prove itself to a Pi (audiolink/auth.py)."""
import os
import unittest

from audiolink import auth
from audiolink.crypto import ed25519_public, ed25519_sign


class AuthMessage(unittest.TestCase):
    def setUp(self):
        self.seed = os.urandom(32)
        self.key = ed25519_public(self.seed)
        self.nonce = os.urandom(16)
        self.pi = ed25519_public(os.urandom(32))
        self.sig = ed25519_sign(self.seed, auth.auth_message(self.nonce, self.pi))

    def test_layout(self):
        self.assertEqual(auth.auth_message(b"n" * 16, b"p" * 32), b"audiolink-auth-v2\0" + b"n" * 16 + b"p" * 32)

    def test_a_good_signature(self):
        self.assertTrue(auth.verify(self.key, self.nonce, self.pi, self.sig))

    def test_bound_to_the_nonce_the_pi_and_the_key(self):
        self.assertFalse(auth.verify(self.key, os.urandom(16), self.pi, self.sig))       # another connection
        other_pi = ed25519_public(os.urandom(32))
        self.assertFalse(auth.verify(self.key, self.nonce, other_pi, self.sig))          # another Pi
        other_key = ed25519_public(os.urandom(32))
        self.assertFalse(auth.verify(other_key, self.nonce, self.pi, self.sig))          # another computer

    def test_malformed_is_false_not_an_error(self):
        self.assertFalse(auth.verify(self.key, b"short", self.pi, self.sig))
        self.assertFalse(auth.verify(self.key, self.nonce, b"short", self.sig))
        self.assertFalse(auth.verify(b"short", self.nonce, self.pi, self.sig))
        self.assertFalse(auth.verify(self.key, self.nonce, self.pi, b"short"))
        with self.assertRaises(ValueError):
            auth.auth_message(b"short", self.pi)

    def test_signed_through_key(self):
        """client/computer.py's Key signs it, as the client will."""
        from audiolink.client.computer import Key
        k = Key.__new__(Key)
        k.seed, k.pub = self.seed, self.key
        sig = k.sign(auth.auth_message(self.nonce, self.pi))
        self.assertTrue(auth.verify(self.key, self.nonce, self.pi, sig))


if __name__ == "__main__":
    unittest.main()
