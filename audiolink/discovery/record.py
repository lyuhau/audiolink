"""The discovery record and the connect code.

identity  32-byte ed25519 seed (signs the record; only the Pi/host hold it) and a
          16-byte secret (encrypts it). Kept by the host, pushed to the Pi.
code      "al1-" + base64url(public key + secret): what a client needs to *find and
          read* the record, but not to forge it.
record    JSON {"v", "name", "ts", "lan", "ssid", "ports"}, sealed with the secret and
          stored on the DHT under SHA1(public key + SALT), seq = ts.
"""
import base64
import json
import os
import time

from .crypto import ed25519_public, open_sealed, seal

SALT = b"audiolink-v1"
CODE_PREFIX = "al1-"
RECORD_VERSION = 1


def new_identity():
    return {"seed": os.urandom(32).hex(), "secret": os.urandom(16).hex()}


def code_for(identity):
    pub = ed25519_public(bytes.fromhex(identity["seed"]))
    raw = pub + bytes.fromhex(identity["secret"])
    return CODE_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_code(code):
    """-> (public key, secret). Raises ValueError for anything malformed."""
    code = code.strip()
    if not code.startswith(CODE_PREFIX):
        raise ValueError("not an audiolink code (should start with %r)" % CODE_PREFIX)
    body = code[len(CODE_PREFIX):]
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    if len(raw) != 48:
        raise ValueError("audiolink code has the wrong length")
    return raw[:32], raw[32:]


def make_record(secret, name, lan, ssid, ports, ts=None):
    rec = {"v": RECORD_VERSION, "name": name, "ts": int(ts if ts is not None else time.time()),
           "lan": lan, "ssid": ssid, "ports": sorted(ports)}
    return rec, seal(secret, json.dumps(rec, separators=(",", ":")).encode())


def read_record(secret, blob):
    """The record dict, or None if it can't be decrypted with this secret."""
    pt = open_sealed(secret, blob)
    if pt is None:
        return None
    try:
        rec = json.loads(pt.decode())
    except ValueError:
        return None
    return rec if isinstance(rec, dict) and rec.get("v") == RECORD_VERSION else None
