import unittest

from audiolink import crypto


class Ed25519(unittest.TestCase):
    def test_rfc8032_test1(self):
        seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        pub = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
                            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertEqual(crypto.ed25519_public(seed), pub)
        self.assertEqual(crypto.ed25519_sign(seed, b""), sig)
        self.assertTrue(crypto.ed25519_verify(pub, b"", sig))
        self.assertFalse(crypto.ed25519_verify(pub, b"x", sig))


class ChaCha20(unittest.TestCase):
    def test_rfc8439_2_4_2(self):
        key = bytes(range(32))
        nonce = bytes.fromhex("000000000000004a00000000")
        pt = (b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for "
              b"the future, sunscreen would be it.")
        ct = crypto.chacha20(key, nonce, pt, counter=1)
        self.assertEqual(ct[:16].hex(), "6e2e359a2568f98041ba0728dd0d6981")
        self.assertEqual(crypto.chacha20(key, nonce, ct, counter=1), pt)


class Seal(unittest.TestCase):
    def test_round_trip_and_tampering(self):
        secret = bytes(16)
        blob = crypto.seal(secret, b"hello")
        self.assertEqual(crypto.open_sealed(secret, blob), b"hello")
        self.assertIsNone(crypto.open_sealed(bytes([1] * 16), blob))
        bad = bytearray(blob)
        bad[-1] ^= 1
        self.assertIsNone(crypto.open_sealed(secret, bytes(bad)))


class Spake2(unittest.TestCase):
    def test_same_password_agrees(self):
        a, b = crypto.Spake2("A", "123456", b"a", b"b"), crypto.Spake2("B", "123456", b"a", b"b")
        ka, conf_a, expect_b = a.finish(b.msg)
        kb, conf_b, expect_a = b.finish(a.msg)
        self.assertEqual(ka, kb)
        self.assertEqual(conf_a, expect_a)
        self.assertEqual(conf_b, expect_b)

    def test_wrong_password_disagrees(self):
        a, b = crypto.Spake2("A", "123456", b"a", b"b"), crypto.Spake2("B", "654321", b"a", b"b")
        ka, conf_a, _ = a.finish(b.msg)
        kb, _, expect_a = b.finish(a.msg)
        self.assertNotEqual(ka, kb)
        self.assertNotEqual(conf_a, expect_a)

    def test_rejects_identity_point(self):
        a = crypto.Spake2("A", "123456")
        with self.assertRaises(ValueError):
            a.finish(bytes([1]) + bytes(31))      # the identity point


if __name__ == "__main__":
    unittest.main()
