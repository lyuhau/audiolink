"""TLS on the Pi's endpoint (audiolink/tls.py): the identity certificate, pinning the
key from the connect code, proofs bound to it, and older clients and Pis."""
import functools
import json
import os
import shutil
import socket
import ssl
import tempfile
import threading
import unittest
from unittest import mock

from audiolink import tls
from audiolink.client import pairing
from audiolink.client.pairing import OldPi, PiConn
from audiolink.crypto import ed25519_public, ed25519_sign
from audiolink.services.access import DEBUG, PAIRED, DebugService
from audiolink.services.endpoint import Endpoint, key_message, proof_for
from audiolink.services.identity import IdentityService


@unittest.skipIf(tls.unsupported(), "this Python's SSL library can't do TLS 1.3 with ed25519")
class Certificate(unittest.TestCase):
    def test_ssl_takes_the_cert_and_key(self):
        seed = os.urandom(32)
        der = tls.certificate(seed)
        self.assertEqual(ssl.PEM_cert_to_DER_cert(tls.pem("CERTIFICATE", der)), der)
        self.assertEqual(tls.cert_public(der), ed25519_public(seed))
        self.assertIsInstance(tls.server_context(seed), ssl.SSLContext)

    def test_not_a_cert(self):
        self.assertIsNone(tls.cert_public(b""))
        self.assertIsNone(tls.cert_public(b"\x30\x03\x02\x01\x00"))

    def test_handshake_shows_the_key(self):
        seed = os.urandom(32)
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)

        def serve():
            s, _ = srv.accept()
            with tls.server_context(seed).wrap_socket(s, server_side=True) as t:
                t.sendall(b"hi")
        threading.Thread(target=serve, daemon=True).start()
        sock, pub = tls.client_wrap(socket.create_connection(srv.getsockname(), timeout=5))
        with sock:
            self.assertEqual(pub, ed25519_public(seed))
            self.assertEqual(sock.version(), "TLSv1.3")
            self.assertEqual(sock.recv(2), b"hi")


class Impostor(IdentityService):
    """A Pi that claims someone else's key in its hello."""
    claims = None

    def info(self):
        return dict(super().info(), pub=self.claims)


@unittest.skipIf(tls.unsupported(), "this Python's SSL library can't do TLS 1.3 with ed25519")
class Endpoints(unittest.TestCase):
    def pi(self, cls=IdentityService):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        identity = cls(d)
        identity.claim()
        debug = DebugService()
        ep = Endpoint(identity, port=0, debug=debug, wifi_ip=lambda: "127.0.0.1", bind="127.0.0.1")
        ep.register("look", lambda conn, req: {"ok": True}, PAIRED)
        ep.register("root", lambda conn, req: {"ok": True}, DEBUG)
        return identity, debug, ep

    def conn(self, ep):
        c = PiConn("127.0.0.1", port=ep.port)
        self.addCleanup(c.close)
        return c

    def reach(self, identity, ep):
        entry = {"alias": "pi", "code": identity.code()}
        with mock.patch.object(pairing, "PiConn", functools.partial(PiConn, port=ep.port)), \
                mock.patch.object(pairing, "browse", lambda timeout: []), \
                mock.patch.object(pairing, "SETUP_ADDR", "127.0.0.1"):
            return pairing.reach(entry, ["127.0.0.1"])

    def test_the_cert_is_the_identity(self):
        identity, _, ep = self.pi()
        c = self.conn(ep)
        self.assertEqual(c.pub, identity.public())
        self.assertEqual(c.hello()["pub"], identity.public().hex())

    def test_a_new_identity_gets_a_new_cert(self):
        identity, _, ep = self.pi()
        identity.start_over()
        self.assertEqual(self.conn(ep).pub, identity.public())

    def test_reach_pins_the_key_from_the_code(self):
        identity, _, ep = self.pi()
        conn, hello = self.reach(identity, ep)
        self.addCleanup(conn.close)
        self.assertEqual(hello["pub"], identity.public().hex())
        other, _, _ = self.pi()                     # another Pi answers there: not this one
        self.assertEqual(self.reach(other, ep), (None, None))

    def test_an_impostor_is_stopped(self):
        real, _, _ = self.pi()
        fake, _, ep = self.pi(Impostor)
        fake.claims = real.public().hex()           # says it's the real Pi; its cert says otherwise
        with self.assertRaises(tls.Mismatch):
            self.conn(ep).hello()
        with self.assertRaisesRegex(SystemExit, "couldn't prove it"):
            self.reach(real, ep)

    def test_proofs_are_bound_to_the_key(self):
        identity, debug, ep = self.pi()
        c = self.conn(ep)
        nonce, secret = bytes.fromhex(c.hello()["nonce"]), identity.keys()[1]
        elsewhere = ed25519_public(os.urandom(32))  # what a man in the middle's client saw
        self.assertEqual(c.ask({"op": "look", "proof": proof_for(secret, "look", nonce, elsewhere)})["code"],
                         "not_paired")
        self.assertTrue(c.ask({"op": "look", "proof": proof_for(secret, "look", nonce, c.pub)})["ok"])
        seed = os.urandom(32)
        debug.set({"mode": "key", "keys": [ed25519_public(seed).hex()]})
        req = {"op": "root", "key": ed25519_public(seed).hex()}
        bad = dict(req, sig=ed25519_sign(seed, key_message("root", nonce, elsewhere)).hex())
        self.assertEqual(c.ask(bad)["code"], "debug_key")
        good = dict(req, sig=ed25519_sign(seed, key_message("root", nonce, c.pub)).hex())
        self.assertTrue(c.ask(good)["ok"])

    def test_older_clients_are_told_to_update(self):
        identity, _, ep = self.pi()
        with socket.create_connection(("127.0.0.1", ep.port), timeout=5) as s:
            f = s.makefile("rwb")

            def ask(obj):
                f.write((json.dumps(obj) + "\n").encode())
                f.flush()
                return json.loads(f.readline())
            hello = ask({"op": "hello"})
            self.assertEqual(hello["pub"], identity.public().hex())
            nonce = bytes.fromhex(hello["nonce"])
            r = ask({"op": "look", "proof": proof_for(identity.keys()[1], "look", nonce, identity.public())})
            self.assertEqual(r["code"], "upgrade")
            self.assertIn("update audiolink", r["error"])


class OldPiServer:
    """An endpoint from before TLS: JSON lines from the first byte."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                s, _ = self.srv.accept()
            except OSError:
                return
            with s:
                try:
                    json.loads(s.makefile("rb").readline())
                except ValueError:
                    continue                        # a TLS hello: an old Pi just hangs up
                s.sendall(b'{"name": "old-pi", "pub": "' + b"ab" * 32 + b'", "nonce": "00"}\n')


@unittest.skipIf(tls.unsupported(), "this Python's SSL library can't do TLS 1.3 with ed25519")
class OlderPis(unittest.TestCase):
    def test_an_older_pi_is_recognized(self):
        old = OldPiServer()
        self.addCleanup(old.srv.close)
        with self.assertRaises(OldPi) as e:
            PiConn("127.0.0.1", port=old.port)
        self.assertEqual(e.exception.hello["name"], "old-pi")


if __name__ == "__main__":
    unittest.main()
