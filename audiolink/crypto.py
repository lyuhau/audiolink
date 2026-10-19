"""Small pure-Python crypto shared by the Pi, its pairing and its clients (no compiled dependencies).

ed25519   RFC 8032 signatures; BEP 44 (signed DHT items) requires them. Slow next
          to a C library, but records are signed about twice an hour.
chacha20  RFC 8439 stream cipher, for encrypting record contents.
seal/open_sealed   ChaCha20 + HMAC-SHA256 (encrypt-then-MAC) with a shared secret,
          so only clients holding the secret can read where the Pi is.
"""
import hashlib
import hmac
import os
import struct

# ---------------------------------------------------------------- ed25519 (RFC 8032)
_p = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_d = -121665 * pow(121666, _p - 2, _p) % _p
_I = pow(2, (_p - 1) // 4, _p)


def _recover_x(y, sign):
    x2 = (y * y - 1) * pow(_d * y * y + 1, _p - 2, _p)
    if x2 == 0:
        if sign:
            raise ValueError("invalid point")
        return 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p:
        x = x * _I % _p
    if (x * x - x2) % _p:
        raise ValueError("invalid point")
    if x & 1 != sign:
        x = _p - x
    return x


_Gy = 4 * pow(5, _p - 2, _p) % _p
_Gx = _recover_x(_Gy, 0)
_G = (_Gx, _Gy, 1, _Gx * _Gy % _p)        # extended coordinates (X, Y, Z, T)


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C = 2 * P[3] * Q[3] * _d % _p
    D = 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _encode(P):
    zi = pow(P[2], _p - 2, _p)
    x, y = P[0] * zi % _p, P[1] * zi % _p
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode(b):
    y = int.from_bytes(b, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    if y >= _p:
        raise ValueError("invalid point")
    x = _recover_x(y, sign)
    return (x, y, 1, x * y % _p)


def _equal(P, Q):
    return (P[0] * Q[2] - Q[0] * P[2]) % _p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % _p == 0


def _h(*parts):
    return int.from_bytes(hashlib.sha512(b"".join(parts)).digest(), "little")


def _expand(seed):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def ed25519_public(seed):
    """32-byte public key for a 32-byte secret seed."""
    a, _ = _expand(seed)
    return _encode(_mul(a, _G))


def ed25519_sign(seed, msg):
    a, prefix = _expand(seed)
    A = _encode(_mul(a, _G))
    r = _h(prefix, msg) % _L
    R = _encode(_mul(r, _G))
    s = (r + _h(R, A, msg) * a) % _L
    return R + s.to_bytes(32, "little")


def ed25519_verify(public, msg, sig):
    if len(public) != 32 or len(sig) != 64:
        return False
    try:
        A, R = _decode(public), _decode(sig[:32])
    except ValueError:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _L:
        return False
    k = _h(sig[:32], public, msg) % _L
    return _equal(_mul(s, _G), _add(R, _mul(k, A)))


# ---------------------------------------------------------------- chacha20 (RFC 8439)
def _rotl(v, c):
    return ((v << c) & 0xFFFFFFFF) | (v >> (32 - c))


def _block(key_words, counter, nonce_words):
    state = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574] + key_words + [counter] + nonce_words
    x = list(state)
    for _ in range(10):
        for a, b, c, d in ((0, 4, 8, 12), (1, 5, 9, 13), (2, 6, 10, 14), (3, 7, 11, 15),
                           (0, 5, 10, 15), (1, 6, 11, 12), (2, 7, 8, 13), (3, 4, 9, 14)):
            x[a] = (x[a] + x[b]) & 0xFFFFFFFF; x[d] = _rotl(x[d] ^ x[a], 16)
            x[c] = (x[c] + x[d]) & 0xFFFFFFFF; x[b] = _rotl(x[b] ^ x[c], 12)
            x[a] = (x[a] + x[b]) & 0xFFFFFFFF; x[d] = _rotl(x[d] ^ x[a], 8)
            x[c] = (x[c] + x[d]) & 0xFFFFFFFF; x[b] = _rotl(x[b] ^ x[c], 7)
    return struct.pack("<16I", *((x[i] + state[i]) & 0xFFFFFFFF for i in range(16)))


def chacha20(key, nonce, data, counter=1):
    """Encrypt or decrypt (the same operation). key 32 bytes, nonce 12 bytes."""
    kw, nw = list(struct.unpack("<8I", key)), list(struct.unpack("<3I", nonce))
    out = bytearray()
    for i in range(0, len(data), 64):
        ks = _block(kw, counter + i // 64, nw)
        out += bytes(a ^ b for a, b in zip(data[i:i + 64], ks))
    return bytes(out)


# ---------------------------------------------------------------- sealed records
def _keys(secret):
    return (hmac.new(secret, b"audiolink enc", hashlib.sha256).digest(),
            hmac.new(secret, b"audiolink mac", hashlib.sha256).digest())


def seal(secret, plaintext):
    """nonce(12) || ciphertext || tag(16)."""
    enc, mac = _keys(secret)
    nonce = os.urandom(12)
    ct = chacha20(enc, nonce, plaintext)
    return nonce + ct + hmac.new(mac, nonce + ct, hashlib.sha256).digest()[:16]


def open_sealed(secret, blob):
    """Plaintext, or None if the blob wasn't sealed with this secret (or was altered)."""
    if len(blob) < 28:
        return None
    enc, mac = _keys(secret)
    nonce, ct, tag = blob[:12], blob[12:-16], blob[-16:]
    if not hmac.compare_digest(tag, hmac.new(mac, nonce + ct, hashlib.sha256).digest()[:16]):
        return None
    return chacha20(enc, nonce, ct)


# ---------------------------------------------------------------- SPAKE2 (RFC 9382)
# Password-authenticated key exchange: both sides end up with the same key only if
# they used the same password, an eavesdropper learns nothing that lets them test
# guesses offline, and an active attacker gets one guess per attempt. Built on the
# edwards25519 group above. M and N are RFC 9382's points for this curve, hashed onto
# it from a fixed seed so that nobody knows their discrete logs.
SPAKE2_M = bytes.fromhex("d048032c6ea0b6d697ddc2e86bda85a33adac920f1bf18e1b0c6d166a5cecdaf")
SPAKE2_N = bytes.fromhex("d3bfb518f44f3430f29d0c92af503865a1ed3281dc69b35dd868ba85f886c4ab")
_IDENTITY = (0, 1, 1, 0)
_COFACTOR = 8


def _neg(P):
    return ((-P[0]) % _p, P[1], P[2], (-P[3]) % _p)


def _is_identity(P):
    return _equal(P, _IDENTITY)


def _lp(b):
    """Length-prefixed (8-byte little-endian length), as RFC 9382 builds its transcript."""
    return len(b).to_bytes(8, "little") + b


def _hkdf(key, info, length=32):
    prk = hmac.new(b"\0" * 32, key, hashlib.sha256).digest()
    out, block = b"", b""
    for i in range((length + 31) // 32):
        block = hmac.new(prk, block + info + bytes([i + 1]), hashlib.sha256).digest()
        out += block
    return out[:length]


class Spake2:
    """One side of an exchange. role "A" is the side that speaks first (the new
    machine), "B" the responder (the Pi). Usage:
        a = Spake2("A", pw, ida, idb); b = Spake2("B", pw, ida, idb)
        ka, conf_a, expect_b = a.finish(b.msg);  kb, conf_b, expect_a = b.finish(a.msg)
        each side sends its confirmation and checks the other's (hmac.compare_digest)."""

    def __init__(self, role, password, id_a=b"", id_b=b"", context=b"audiolink pairing"):
        self.role, self.id_a, self.id_b = role, id_a, id_b
        self.w = int.from_bytes(hashlib.pbkdf2_hmac("sha256", password.encode(), context, 20000, 64),
                                "little") % _L
        self.x = int.from_bytes(os.urandom(64), "little") % _L
        mask = _decode(SPAKE2_M if role == "A" else SPAKE2_N)
        self.msg = _encode(_add(_mul(self.x, _G), _mul(self.w, mask)))

    def finish(self, peer_msg):
        """-> (shared key, our confirmation, the peer's expected confirmation)."""
        try:
            P = _decode(peer_msg)
        except ValueError:
            raise ValueError("invalid SPAKE2 message")
        if _is_identity(_mul(_COFACTOR, P)):            # identity or a small-order point
            raise ValueError("invalid SPAKE2 message")
        unmask = _decode(SPAKE2_N if self.role == "A" else SPAKE2_M)
        K = _mul(_COFACTOR * self.x, _add(P, _neg(_mul(self.w, unmask))))
        if _is_identity(K):
            raise ValueError("invalid SPAKE2 message")
        X, Y = (self.msg, peer_msg) if self.role == "A" else (peer_msg, self.msg)
        tt = b"".join(_lp(v) for v in (self.id_a, self.id_b, X, Y, _encode(K),
                                         self.w.to_bytes(32, "little")))
        h = hashlib.sha256(tt).digest()
        ke, ka = h[:16], h[16:]
        kc = _hkdf(ka, b"ConfirmationKeys")
        conf_a = hmac.new(kc[:16], tt, hashlib.sha256).digest()
        conf_b = hmac.new(kc[16:], tt, hashlib.sha256).digest()
        return (ke, conf_a, conf_b) if self.role == "A" else (ke, conf_b, conf_a)
