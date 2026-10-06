"""Pi side: pairing over the network (local Wi-Fi, or the Pi's own setup network).

A machine that can reach the Pi over the network learns its connect code by
pairing, on TCP port PORT (advertised over mDNS as _audiolink._tcp):
  unclaimed Pi  the first machine to pair claims it ("first to claim owns it")
  claimed Pi    needs a fresh 6-digit pairing code, issued on request by the host
                (over USB: "pairing.new_code") or by any already-paired machine
                (over the network, proving it knows the Pi's read secret)
Codes expire after CODE_TTL, work once, and allow MAX_TRIES guesses. Pairing runs
SPAKE2 with the code, so the code never crosses the network and a recording of the
exchange can't be used to guess it offline; failed attempts back off exponentially.

Protocol: one JSON object per line, request/response.
  {"op":"hello"}                     -> {"name","tag","pub","claimed","nonce"}
  {"op":"pair","msg":X,"client":id}  -> {"msg":Y}   then  {"confirm":cA}
                                     -> {"confirm":cB,"payload":sealed {"code","name"}}
  {"op":"new_code","proof":hmac}     -> {"sealed": sealed {"code","expires_in"}}
"""
import hmac
import hashlib
import json
import logging
import os
import secrets
import socket
import threading
import time

from ..crypto import Spake2, seal
from ..discovery.record import code_for
from . import Service

log = logging.getLogger("audiolink")

PORT = 47900
CODE_TTL = 300
MAX_TRIES = 3
MAX_BACKOFF = 300
ID_CLIENT, ID_PI = b"audiolink client", b"audiolink pi"


class PairingService(Service):
    name = "pairing"
    caps = ("pairing",)

    def __init__(self, identity, port=PORT):
        self.identity = identity
        self.lock = threading.Lock()
        self.code = None                # {"code", "expires", "tries"}
        self.failures = 0
        self.blocked_until = 0.0
        self.slots = threading.Semaphore(4)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(8)
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()
        log.info("pairing: listening on :%d", port)

    # ---- codes
    def new_code(self):
        with self.lock:
            self.code = {"code": "%06d" % secrets.randbelow(10 ** 6),
                         "expires": time.monotonic() + CODE_TTL, "tries": MAX_TRIES}
            return {"code": self.code["code"], "expires_in": CODE_TTL}

    def _active_code(self):
        c = self.code
        if c and c["tries"] > 0 and time.monotonic() < c["expires"]:
            return c
        self.code = None
        return None

    # ---- requests from the host over USB (trusted)
    def handle(self, op, args):
        if op == "new_code":
            if not self.identity.data["claimed"]:
                return {"code": None, "unclaimed": True}
            return self.new_code()
        if op == "status":
            with self.lock:
                c = self._active_code()
                return {"active": bool(c), "expires_in": int(c["expires"] - time.monotonic()) if c else 0,
                        "tries_left": c["tries"] if c else 0, "claimed": self.identity.data["claimed"]}
        raise ValueError("unknown pairing operation %r" % op)

    # ---- network side
    def _accept(self, srv):
        while True:
            conn, addr = srv.accept()
            if not self.slots.acquire(blocking=False):
                conn.close()            # too many at once
                continue
            threading.Thread(target=self._serve, args=(conn, addr), daemon=True).start()

    def _serve(self, conn, addr):
        try:
            conn.settimeout(30)
            f = conn.makefile("rwb")

            def send(obj):
                f.write((json.dumps(obj) + "\n").encode())
                f.flush()

            nonce = os.urandom(16)
            for line in f:
                req = json.loads(line)
                op = req.get("op")
                if op == "hello":
                    info = self.identity.info()
                    send({"name": info["name"], "tag": info["tag"], "pub": info["pub"],
                          "claimed": info["claimed"], "nonce": nonce.hex()})
                elif op == "pair":
                    if self._pair(req, f, send, addr):
                        return
                elif op == "new_code":
                    _, secret = self.identity.keys()
                    proof = hmac.new(secret, b"new_code" + nonce, hashlib.sha256).hexdigest()
                    if not self.identity.data["claimed"] or not hmac.compare_digest(proof, req.get("proof", "")):
                        send({"error": "not allowed"})
                        continue
                    issued = self.new_code()
                    log.info("pairing: code issued to a paired machine at %s", addr[0])
                    send({"sealed": seal(secret, json.dumps(issued).encode()).hex()})
                else:
                    send({"error": "unknown op"})
        except (OSError, ValueError):
            pass
        finally:
            conn.close()
            self.slots.release()

    def _pair(self, req, f, send, addr):
        """Returns True when the connection is done."""
        with self.lock:
            wait = self.blocked_until - time.monotonic()
            if wait > 0:
                send({"error": "too many failed attempts; try again in %d s" % wait})
                return True
            claimed = self.identity.data["claimed"]
            c = self._active_code() if claimed else None
            if claimed and not c:
                send({"error": "no pairing code is active; on the host or a paired machine run: "
                               "audiolink pairing code %s" % self.identity.data["name"]})
                return True
            password = c["code"] if c else ""
        b = Spake2("B", password, ID_CLIENT, ID_PI)
        try:
            key, ours, theirs = b.finish(bytes.fromhex(req["msg"]))
        except ValueError:
            send({"error": "bad message"})
            return True
        send({"msg": b.msg.hex()})
        confirm = bytes.fromhex(json.loads(f.readline()).get("confirm", ""))
        with self.lock:
            if not hmac.compare_digest(confirm, theirs):
                self.failures += 1
                self.blocked_until = time.monotonic() + min(MAX_BACKOFF, 2 ** self.failures)
                if c:
                    c["tries"] -= 1
                log.warning("pairing: wrong code from %s (%d tries left)", addr[0], c["tries"] if c else 0)
                send({"error": "wrong code"})
                return True
            self.failures = 0
            if c:
                self.code = None        # single use
        first = not claimed
        if first:
            self.identity.handle("claim", {})
        payload = {"code": code_for(self.identity.data), "name": self.identity.data["name"]}
        send({"confirm": ours.hex(), "payload": seal(key, json.dumps(payload).encode()).hex()})
        log.info("pairing: %s paired from %s%s", req.get("client", "a machine"), addr[0],
                 " (first: claimed)" if first else "")
        return True
