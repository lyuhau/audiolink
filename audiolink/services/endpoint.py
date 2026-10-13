"""Pi side: the network endpoint (TCP PORT, mDNS _audiolink._tcp) that machines reach
over Wi-Fi, the setup network or the relay. Services register requests on it (pairing,
Wi-Fi changes, logs, updates, shells), each with the access level it needs
(services/access.py decides).

Protocol: one JSON object per line. Every connection starts with
  {"op":"hello"} -> {"name","tag","pub","claimed","nonce"}
Proofs a request can carry:
  "proof"        HMAC-SHA256(read secret, op + nonce): a machine that paired with this Pi;
                 its sensitive fields travel sealed with the read secret (Conn.seal/open)
  "key", "sig"   an ed25519 signature of KEY_LABEL + op + nonce (debug key mode)
A handler can keep the connection for itself (a shell): it raises Takeover, and the
endpoint frees the request slot and drops its timeout first.
"""
import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time

from ..crypto import ed25519_verify, open_sealed, seal
from .access import DEBUG, KEY_LABEL, PUBLIC, decide, route_of

log = logging.getLogger("audiolink")

PORT = 47900


def proof_for(secret, op, nonce):
    return hmac.new(secret, op.encode() + nonce, hashlib.sha256).hexdigest()


def key_message(op, nonce):
    return KEY_LABEL + op.encode() + nonce


class Takeover(Exception):
    """Raised by a handler that keeps the connection: run() then has it, on this thread,
    without the endpoint's slot or timeout."""

    def __init__(self, run):
        super().__init__("takeover")
        self.run = run


class Conn:
    """One client connection, as seen by request handlers."""

    def __init__(self, sock, addr, identity, route="relay"):
        self.sock, self.addr, self.route = sock, addr, route
        self.identity = identity
        self.nonce = os.urandom(16)
        self.f = sock.makefile("rwb")
        self.verified = set()               # (key, sig) already checked on this connection
        self.logged = set()                 # debug ops logged on this connection

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
    def __init__(self, identity, port=PORT, presence=None, debug=None, wifi_ip=None, bind="0.0.0.0"):
        self.identity = identity
        self.presence = presence                # services/presence.py: requests count as clients
        self.debug = debug                      # services/access.py DebugService (None: debug off)
        self.wifi_ip = wifi_ip or (presence.wifi_ip if presence else (lambda: None))
        self.handlers = {}                      # op -> (fn(conn, req) -> reply or None, level)
        self.slots = threading.Semaphore(4)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((bind, port))
        srv.listen(8)
        self.port = srv.getsockname()[1]
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()
        log.info("endpoint: listening on :%d", self.port)

    def register(self, op, fn, level=PUBLIC):
        """fn(conn, req) returns a reply dict, or None if it replied itself; it raises
        StopIteration to end the connection after replying, or Takeover to keep it."""
        self.handlers[op] = (fn, level)

    def _proven(self, conn, op, req, need, switch):
        proven = set()
        secret = self.identity.keys()[1]
        if self.identity.claimed() and hmac.compare_digest(proof_for(secret, op, conn.nonce),
                                                           str(req.get("proof", ""))):
            proven.add("paired")
        if need == DEBUG and switch.get("mode") == "key" and req.get("key") and req.get("sig"):
            got = (str(req["key"]), str(req["sig"]))
            try:
                key, sig = bytes.fromhex(got[0]), bytes.fromhex(got[1])
            except ValueError:
                return proven
            if got in conn.verified or (key in self.debug.keys() and
                                        ed25519_verify(key, key_message(op, conn.nonce), sig)):
                conn.verified.add(got)
                proven.add("key")
        return proven

    def check(self, conn, op, req, need):
        """None if the request may go ahead, else the refusal reply (logged)."""
        if need == PUBLIC:
            return None
        switch = self.debug.snapshot() if self.debug else {"mode": "off"}
        why = decide(need, conn.route, self._proven(conn, op, req, need, switch), switch, time.monotonic())
        if why:
            log.warning("access: refused %s from %s (%s): %s", op, conn.addr[0], conn.route, why[0])
            return {"error": why[1], "code": why[0]}
        if need == DEBUG and op not in conn.logged:
            conn.logged.add(op)
            log.info("access: %s from %s (%s, debug %s)", op, conn.addr[0], conn.route, switch.get("mode"))
        return None

    def _accept(self, srv):
        while True:
            try:
                sock, addr = srv.accept()
            except OSError as e:                # e.g. out of file descriptors
                log.warning("endpoint: %s", e)
                time.sleep(1)
                continue
            if not self.slots.acquire(blocking=False):
                sock.close()                    # too many at once
                continue
            try:
                threading.Thread(target=self._serve, args=(sock, addr), daemon=True).start()
            except RuntimeError as e:           # out of threads
                log.warning("endpoint: %s", e)
                sock.close()
                self.slots.release()

    def _serve(self, sock, addr):
        if self.presence:
            self.presence.touch(sock)
        held = True
        try:
            sock.settimeout(30)
            try:
                local = sock.getsockname()[0]
            except OSError:
                local = None
            conn = Conn(sock, addr, self.identity, route_of(local, addr[0], self.wifi_ip()))
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
                fn, level = self.handlers[op]
                refused = self.check(conn, op, req, level)
                if refused:
                    conn.send(refused)
                    continue
                try:
                    reply = fn(conn, req)
                except StopIteration:
                    return
                except Takeover as t:
                    held = False
                    self.slots.release()
                    sock.settimeout(None)
                    t.run()
                    return
                except (OSError, ConnectionError):
                    raise
                except Exception as e:          # a bug, or a request shaped for another version
                    log.warning("endpoint: %s failed: %s", op, e)
                    reply = {"error": "The Pi couldn't do that (%s)" % (str(e) or type(e).__name__)}
                if reply is not None:
                    conn.send(reply)
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            if self.presence:
                self.presence.touch(sock)
            sock.close()
            if held:
                self.slots.release()
