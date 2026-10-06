"""Pi side: pairing a machine with this Pi over the network (via services/endpoint.py).

  unclaimed Pi  the first machine to pair claims it ("first to claim owns it")
  claimed Pi    needs a fresh 6-digit pairing code, issued on request by the host
                (over USB: "pairing.new_code") or by any already-paired machine
                (over the network: the authenticated "new_code" request)
Codes expire after CODE_TTL, work once, and allow MAX_TRIES guesses. Pairing runs
SPAKE2 with the code, so the code never crosses the network and a recording of the
exchange can't be used to guess it offline; failed attempts back off exponentially.

Endpoint requests:
  {"op":"pair","msg":X,"client":id} -> {"msg":Y}; {"confirm":cA} -> {"confirm":cB,"payload":sealed}
  {"op":"new_code","proof":...}     -> {"sealed": {"code","expires_in"}}      (auth)
"""
import hmac
import json
import logging
import secrets
import threading
import time

from ..crypto import Spake2, seal
from ..discovery.record import code_for
from . import Service

log = logging.getLogger("audiolink")

CODE_TTL = 300
MAX_TRIES = 3
MAX_BACKOFF = 300
ID_CLIENT, ID_PI = b"audiolink client", b"audiolink pi"


class PairingService(Service):
    name = "pairing"
    caps = ("pairing",)

    def __init__(self, identity, endpoint):
        self.identity = identity
        self.lock = threading.Lock()
        self.code = None                # {"code", "expires", "tries"}
        self.failures = 0
        self.blocked_until = 0.0
        endpoint.register("pair", self._pair)
        endpoint.register("new_code", self._new_code_remote, auth=True)

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

    # ---- endpoint requests
    def _new_code_remote(self, conn, req):
        issued = self.new_code()
        log.info("pairing: code issued to a paired machine at %s", conn.addr[0])
        return {"sealed": conn.seal(issued)}

    def _pair(self, conn, req):
        with self.lock:
            wait = self.blocked_until - time.monotonic()
            if wait > 0:
                conn.send({"error": "too many failed attempts; try again in %d s" % wait})
                raise StopIteration
            claimed = self.identity.data["claimed"]
            c = self._active_code() if claimed else None
            if claimed and not c:
                conn.send({"error": "no pairing code is active; on the host or a paired machine run: "
                                    "audiolink pairing code %s" % self.identity.data["name"]})
                raise StopIteration
            password = c["code"] if c else ""
        b = Spake2("B", password, ID_CLIENT, ID_PI)
        try:
            key, ours, theirs = b.finish(bytes.fromhex(req["msg"]))
        except ValueError:
            conn.send({"error": "bad message"})
            raise StopIteration
        conn.send({"msg": b.msg.hex()})
        confirm = bytes.fromhex(conn.recv().get("confirm", ""))
        with self.lock:
            if not hmac.compare_digest(confirm, theirs):
                self.failures += 1
                self.blocked_until = time.monotonic() + min(MAX_BACKOFF, 2 ** self.failures)
                if c:
                    c["tries"] -= 1
                log.warning("pairing: wrong code from %s (%d tries left)", conn.addr[0], c["tries"] if c else 0)
                conn.send({"error": "wrong code"})
                raise StopIteration
            self.failures = 0
            if c:
                self.code = None        # single use
        first = not claimed
        if first:
            self.identity.handle("claim", {})
        payload = {"code": code_for(self.identity.data), "name": self.identity.data["name"]}
        conn.send({"confirm": ours.hex(), "payload": seal(key, json.dumps(payload).encode()).hex()})
        log.info("pairing: %s paired from %s%s", req.get("client", "a machine"), conn.addr[0],
                 " (first: claimed)" if first else "")
        raise StopIteration
