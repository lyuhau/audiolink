"""Proving a computer key to a Pi (docs/protocol.md): the one message every proof signs,
on the endpoint (`auth`) and on the USB link (`link.auth`) alike.

The nonce is fresh from the Pi per connection or link-up, and the Pi's key is in the
message, so a signature can't be replayed on another connection, channel or Pi. Signing
is client/remote.py's Key.sign (a key file, or ssh-agent).
"""
from .crypto import ed25519_verify

ENDPOINT_VERSION = 2            # the endpoint's hello carries "v"; the USB link's is PROTOCOL_VERSION
LABEL = b"audiolink-auth-v2\0"
NONCE_BYTES = 16


def auth_message(nonce, pi_pub):
    """What a computer key signs: LABEL + nonce (16 bytes) + the Pi's public key (32)."""
    if len(nonce) != NONCE_BYTES or len(pi_pub) != 32:
        raise ValueError("auth_message: a %d-byte nonce and a 32-byte Pi key" % NONCE_BYTES)
    return LABEL + nonce + pi_pub


def verify(key, nonce, pi_pub, sig):
    """Did `key` (32 bytes) sign auth_message(nonce, pi_pub)? False for anything malformed."""
    try:
        return ed25519_verify(key, auth_message(nonce, pi_pub), sig)
    except ValueError:
        return False
