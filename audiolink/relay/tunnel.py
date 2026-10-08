"""End to end over a joined relay connection: the client proves it's paired with the
Pi, the Pi proves it's the Pi, then the connection carries one port's bytes.

  Pi      {"v", "nonce"}
  client  {"port", "cnonce", "proof": HMAC(secret, OPEN + pub + port + nonce + cnonce)}
  Pi      {"ok", "proof": HMAC(secret, PI + pub + port + nonce + cnonce)} | {"error"}

The Pi makes a fresh nonce for each connection and the proofs name the Pi and the
port, so the relay (or anyone on the way) can't replay one or point it elsewhere.
"""
import hashlib
import hmac
import os
import socket

from .wire import VERSION, Refused, answer, read_line, send_line

OPEN = b"audiolink-relay-open-v1\0"
PI = b"audiolink-relay-pi-v1\0"


def _mac(secret, label, pub, port, nonce, cnonce):
    msg = label + pub + port.to_bytes(2, "big") + nonce + cnonce
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def serve(sock, pub, secret, allowed, source=None, timeout=5):
    """Pi side, on a data connection the relay has joined. -> (port, socket to the local
    service) once the client has proven itself, or None (the client was told why).
    allowed(port) says which ports may be reached; source is the local address
    connections to the service come from (so they can be told apart)."""
    nonce = os.urandom(16)
    send_line(sock, {"v": VERSION, "nonce": nonce.hex()})
    req = read_line(sock)
    port, cnonce = req.get("port"), bytes.fromhex(str(req.get("cnonce", "")))
    if not isinstance(port, int) or not 0 < port < 65536 or len(cnonce) != 16:
        send_line(sock, {"error": "bad request"})
        return None
    if not hmac.compare_digest(str(req.get("proof", "")), _mac(secret, OPEN, pub, port, nonce, cnonce)):
        send_line(sock, {"error": "not paired with this Pi"})
        return None
    if not allowed(port):
        send_line(sock, {"error": "port %d isn't open over the relay" % port})
        return None
    try:
        target = socket.create_connection(("127.0.0.1", port), timeout=timeout,
                                          source_address=(source, 0) if source else None)
    except OSError:
        send_line(sock, {"error": "nothing on port %d" % port})
        return None
    target.settimeout(None)
    send_line(sock, {"ok": True, "proof": _mac(secret, PI, pub, port, nonce, cnonce)})
    return port, target


def open_port(sock, pub, secret, port):
    """Client side, on a connection the relay has joined to the Pi. Returns when the
    connection carries `port`'s bytes. Raises Refused."""
    nonce = bytes.fromhex(str(read_line(sock).get("nonce", "")))
    if len(nonce) != 16:
        raise Refused("bad reply from the Pi")
    cnonce = os.urandom(16)
    send_line(sock, {"port": port, "cnonce": cnonce.hex(),
                     "proof": _mac(secret, OPEN, pub, port, nonce, cnonce)})
    reply = answer(sock)
    if not hmac.compare_digest(str(reply.get("proof", "")), _mac(secret, PI, pub, port, nonce, cnonce)):
        raise Refused("the other end isn't the Pi")
