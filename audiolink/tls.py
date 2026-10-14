"""TLS for the Pi's endpoint, keyed by the Pi's own identity (ed25519).

The Pi presents a self-signed X.509 certificate for its identity key, built here
(hand-written DER, signed with crypto.ed25519_sign: no compiled dependencies beyond
Python's ssl). Clients don't check certificate chains: they compare the certificate's
public key with the one in the Pi's connect code (pin), which is the Pi's identity.
Without a code there is nothing to compare with: the connection is encrypted against
someone listening, but not proven to reach that Pi.

Proofs sent inside TLS are bound to the certificate the client saw (bind()), so a
proof made for an impostor's connection is no good on the real Pi's.
"""
import hashlib
import os
import ssl
import tempfile

from .crypto import ed25519_public, ed25519_sign

_ED25519 = b"\x06\x03\x2b\x65\x70"            # OID 1.3.101.112
_CN = b"\x06\x03\x55\x04\x03"                 # OID 2.5.4.3 (commonName)
SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")
FIRST_BYTE = 0x16                             # a TLS record (handshake); a plain request starts with "{"


# ---------------------------------------------------------------- DER
def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, body):
    return bytes([tag]) + _len(len(body)) + body


def _seq(*parts):
    return _tlv(0x30, b"".join(parts))


def _int(n):
    b = n.to_bytes(n.bit_length() // 8 + 1, "big")      # leading 0 keeps it positive
    return _tlv(0x02, b)


def _name(cn):
    return _seq(_tlv(0x31, _seq(_CN, _tlv(0x0C, cn.encode()))))


def certificate(seed, cn="audiolink"):
    """A self-signed X.509 v3 certificate (DER) for this ed25519 seed."""
    pub = ed25519_public(seed)
    alg = _seq(_ED25519)
    serial = int.from_bytes(hashlib.sha256(pub).digest()[:8], "big") | 1
    validity = _seq(_tlv(0x17, b"000101000000Z"),              # UTCTime 2000-01-01
                    _tlv(0x18, b"99991231235959Z"))            # "no expiry" (RFC 5280)
    tbs = _seq(_tlv(0xA0, _int(2)), _int(serial), alg, _name(cn), validity, _name(cn),
               SPKI_PREFIX + pub)
    return _seq(tbs, alg, _tlv(0x03, b"\0" + ed25519_sign(seed, tbs)))


def private_key(seed):
    """The seed as a PKCS#8 private key (DER, RFC 8410)."""
    return _seq(_int(0), _seq(_ED25519), _tlv(0x04, _tlv(0x04, seed)))


def pem(kind, der):
    import base64
    b = base64.b64encode(der).decode()
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (
        kind, "\n".join(b[i:i + 64] for i in range(0, len(b), 64)), kind)


def _read(der, i):
    """-> (tag, body start, body end) of the element at i."""
    tag, n = der[i], der[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        if not 0 < k <= 4:
            raise ValueError("bad length")
        n = int.from_bytes(der[i:i + k], "big")
        i += k
    if i + n > len(der):
        raise ValueError("truncated")
    return tag, i, i + n


def _children(der, start, end):
    out = []
    while start < end:
        tag, s, e = _read(der, start)
        out.append((tag, s, e))
        start = e
    return out


def cert_public(der):
    """The ed25519 public key a certificate (DER) holds, or None (another key type,
    or not a certificate)."""
    try:
        tag, s, e = _read(der, 0)
        tag, s, e = _children(der, s, e)[0]                     # tbsCertificate
        fields = _children(der, s, e)
        if fields[0][0] == 0xA0:                                # [0] version
            fields = fields[1:]
        _, s, e = fields[5]                                     # subjectPublicKeyInfo
        spki = der[s - 2:e] if der[s - 1] == e - s else None
    except (IndexError, ValueError):
        return None
    if spki and spki.startswith(SPKI_PREFIX) and len(spki) == len(SPKI_PREFIX) + 32:
        return spki[len(SPKI_PREFIX):]
    return None


# ---------------------------------------------------------------- contexts
def half_close(ctx):
    """Let a peer's end of sending (a bare TCP FIN: relay.wire.splice's half-close) end
    reading only, like a close_notify, so this side can still send. Without it OpenSSL 3
    treats it as a fatal error and the next send fails."""
    ctx.options |= getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF",
                           0x80 if ssl.OPENSSL_VERSION_INFO >= (3,) else 0)
    return ctx


_server = {}                                     # seed -> SSLContext (the cert is signed once)


def server_context(seed):
    """A server context presenting this identity (built once per seed)."""
    ctx = _server.get(seed)
    if ctx is None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        half_close(ctx)
        if hasattr(ctx, "num_tickets"):
            ctx.num_tickets = 0
        fd, path = tempfile.mkstemp(prefix="audiolink-tls-")
        try:                                     # only the file's owner can read it, and only now
            with os.fdopen(fd, "w") as f:
                f.write(pem("PRIVATE KEY", private_key(seed)) + pem("CERTIFICATE", certificate(seed)))
            ctx.load_cert_chain(path)
        finally:
            os.remove(path)
        _server.clear()                          # an identity change: the old one is done
        _server[seed] = ctx
    return ctx


def client_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE              # the pin is the check (client_wrap)
    return half_close(ctx)


class Mismatch(ssl.SSLError):
    """The Pi's certificate isn't for the key it claims (claimed: that key, hex)."""

    def __init__(self, claimed=None):
        super().__init__("the certificate isn't for the key it claims")
        self.claimed = claimed


def unsupported():
    """Why this Python can't speak the endpoint's TLS (1.3, ed25519), or None."""
    if not getattr(ssl, "HAS_TLSv1_3", False) or ssl.OPENSSL_VERSION.startswith("LibreSSL"):
        return ("This Python's SSL library (%s) can't reach a Pi over the network (that takes "
                "TLS 1.3 with ed25519); use Python from python.org or Homebrew" % ssl.OPENSSL_VERSION)
    return None


def client_wrap(sock):
    """TLS over a connected socket to a Pi's endpoint. -> (SSL socket, the public key in
    the Pi's certificate). The caller compares that key with the one it expects."""
    tls = client_context().wrap_socket(sock)
    pub = cert_public(tls.getpeercert(binary_form=True) or b"")
    if pub is None:
        tls.close()
        raise Mismatch()
    return tls, pub


def bind(message, pub):
    """A proof's message tied to the Pi's key as seen in TLS."""
    return message + b"\0tls:" + pub
