"""Pi side: the network endpoint (TCP PORT, mDNS _audiolink._tcp) that machines reach
over Wi-Fi or the setup network. Services register requests on it (pairing, mode).

Protocol: one JSON object per line. Every connection starts with
  {"op":"hello"} -> {"name","tag","pub","claimed","nonce"}
Requests registered with auth=True must carry "proof": HMAC-SHA256(read secret,
op + nonce), i.e. come from a machine that has paired with this Pi; their
sensitive fields travel sealed with the read secret (ctx.seal / ctx.open).
"""
import hashlib
import hmac
import json
import logging
import os
import socket
import threading

from ..crypto import open_sealed, seal

log = logging.getLogger("audiolink")

PORT = 47900


def proof_for(secret, op, nonce):
    return hmac.new(secret, op.encode() + nonce, hashlib.sha256).hexdigest()


class Conn:
    """One client connection, as seen by request handlers."""

    def __init__(self, sock, addr, identity):
        self.addr = addr
        self.identity = identity
        self.nonce = os.urandom(16)
        self.f = sock.makefile("rwb")

    def send(self, obj):
        self.f.write((json.dumps(obj) + "\n").encode())
        self.f.flush()

    def recv(self):
        line = self.f.readline()
        if not line:
            raise ConnectionError("closed")
        return json.loads(line)

    def seal(self, obj):
        return seal(self.identity.keys()[1], json.dumps(obj).encode()).hex()

    def open(self, blob_hex):
        pt = open_sealed(self.identity.keys()[1], bytes.fromhex(blob_hex))
        if pt is None:
            raise ValueError("couldn't decrypt")
        return json.loads(pt)


class Endpoint:
    def __init__(self, identity, port=PORT):
        self.identity = identity
        self.handlers = {}                      # op -> (fn(conn, req) -> reply or None, auth)
        self.slots = threading.Semaphore(4)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(8)
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()
        log.info("endpoint: listening on :%d", port)

    def register(self, op, fn, auth=False):
        """fn(conn, req) returns a reply dict, or None if it replied itself; it raises
        StopIteration to end the connection after replying."""
        self.handlers[op] = (fn, auth)

    def _accept(self, srv):
        while True:
            sock, addr = srv.accept()
            if not self.slots.acquire(blocking=False):
                sock.close()                    # too many at once
                continue
            threading.Thread(target=self._serve, args=(sock, addr), daemon=True).start()

    def _serve(self, sock, addr):
        try:
            sock.settimeout(30)
            conn = Conn(sock, addr, self.identity)
            while True:
                req = conn.recv()
                op = req.get("op")
                if op == "hello":
                    info = self.identity.info()
                    conn.send({"name": info["name"], "tag": info["tag"], "pub": info["pub"],
                               "claimed": info["claimed"], "nonce": conn.nonce.hex()})
                    continue
                if op not in self.handlers:
                    conn.send({"error": "unknown op %r" % op})
                    continue
                fn, auth = self.handlers[op]
                if auth:
                    expected = proof_for(self.identity.keys()[1], op, conn.nonce)
                    if not self.identity.data["claimed"] or \
                            not hmac.compare_digest(expected, req.get("proof", "")):
                        conn.send({"error": "Not added to this Pi (run: audiolink add)"})
                        continue
                try:
                    reply = fn(conn, req)
                except StopIteration:
                    return
                if reply is not None:
                    conn.send(reply)
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            sock.close()
            self.slots.release()
