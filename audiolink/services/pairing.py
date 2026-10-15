"""Pi side: pairing a machine with this Pi over the network (via services/endpoint.py).

  unclaimed Pi  the first machine to pair claims it ("first to claim owns it")
  claimed Pi    needs a fresh 6-digit pairing code, issued on request by the host
                (over USB: "pairing.new_code") or by any already-paired machine
                (over the network: the authenticated "new_code" request)
Codes expire after CODE_TTL, work once, and allow MAX_TRIES guesses. Pairing runs
SPAKE2 with the code, so the code never crosses the network and a recording of the
exchange can't be used to guess it offline. Wrong codes back off exponentially per
address (one stranger can't lock everyone out), forgotten after DECAY s without one.
A claimed Pi runs the exchange whether or not a code is active (with none, against a
random one), so asking doesn't tell a stranger when there's a code to guess.

Endpoint requests:
  {"op":"pair","msg":X,"client":id} -> {"msg":Y}; {"confirm":cA} -> {"confirm":cB,"payload":sealed}
      payload: {"code","name","record"}; record: where the Pi is now, as its DHT record would
      say (services/discovery.py), absent while it can't tell the time
  {"op":"new_code","proof":...}     -> {"sealed": {"code","expires_in"}}      (auth)
"""
import hmac
import json
import logging
import secrets
import threading
import time

from ..crypto import Spake2, seal
from . import Service
from .access import PAIRED

log = logging.getLogger("audiolink")

CODE_TTL = 300
MAX_TRIES = 3
MAX_BACKOFF = 300
DECAY = 600                     # s without a wrong code before an address starts over
ID_CLIENT, ID_PI = b"audiolink client", b"audiolink pi"


class PairingService(Service):
    name = "pairing"
    caps = ("pairing",)

    def __init__(self, identity, endpoint, where=None):
        self.identity = identity
        self.where = where              # -> the Pi's record as of now, or None
        self.lock = threading.Lock()
        self.code = None                # {"code", "expires", "tries"}
        self.failures = {}              # address -> {"count", "until", "last"}
        endpoint.register("pair", self._pair)
        endpoint.register("new_code", self._new_code_remote, PAIRED)

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
            if not self.identity.claimed():
                return {"code": None, "unclaimed": True}
            return self.new_code()
        if op == "status":
            with self.lock:
                c = self._active_code()
                return {"active": bool(c), "expires_in": int(c["expires"] - time.monotonic()) if c else 0,
                        "tries_left": c["tries"] if c else 0, "claimed": self.identity.claimed()}
        raise ValueError("unknown pairing operation %r" % op)

    # ---- endpoint requests
    def _new_code_remote(self, conn, req):
        issued = self.new_code()
        log.info("pairing: code issued to a paired machine at %s", conn.addr[0])
        return {"sealed": conn.seal(issued)}

    def _pair(self, conn, req):
        ip = conn.addr[0]
        with self.lock:
            now = time.monotonic()
            self.failures = {k: f for k, f in self.failures.items() if now - f["last"] < DECAY}
            failed = self.failures.get(ip)
            if failed and failed["until"] > now:
                conn.send({"error": "Too many wrong codes; retry in %ds" % -(-(failed["until"] - now) // 1)})
                raise StopIteration
            claimed = self.identity.claimed()
            c = self._active_code() if claimed else None
            password = c["code"] if c else (secrets.token_hex(16) if claimed else "")
        b = Spake2("B", password, ID_CLIENT, ID_PI)
        try:
            key, ours, theirs = b.finish(bytes.fromhex(req["msg"]))
        except (KeyError, TypeError, ValueError):
            conn.send({"error": "Bad request"})
            raise StopIteration
        conn.send({"msg": b.msg.hex()})
        try:
            confirm = bytes.fromhex(str(conn.recv().get("confirm", "")))
        except AttributeError:
            confirm = b""
        name = self.identity.pi_name()
        with self.lock:
            if not hmac.compare_digest(confirm, theirs):
                now = time.monotonic()
                count = (self.failures.get(ip) or {}).get("count", 0) + 1
                self.failures[ip] = {"count": count, "until": now + min(MAX_BACKOFF, 2 ** count), "last": now}
                if c:
                    c["tries"] -= 1
                log.warning("pairing: wrong code from %s (%s)", conn.addr[0],
                            "%d tries left" % c["tries"] if c else "no code active")
                if c and c["tries"] > 0:
                    error = "Wrong code (%d %s left)" % (c["tries"], "try" if c["tries"] == 1 else "tries")
                elif c:
                    error = "Wrong code, and that was its last try; get a new one: audiolink pairing code %s" % name
                else:
                    error = "That code has expired or been used up; get a new one: audiolink pairing code %s" % name
                conn.send({"error": error})
                raise StopIteration
            self.failures.pop(ip, None)
            if c:
                self.code = None        # single use
        conn.prove()                    # someone legitimate is here (services/presence.py)
        first = not claimed
        if first:
            self.identity.claim()
        payload = {"code": self.identity.code(), "name": name}
        rec = self.where() if self.where else None
        if rec:
            payload["record"] = rec     # the new machine knows its network and ports at once
        conn.send({"confirm": ours.hex(), "payload": seal(key, json.dumps(payload).encode()).hex()})
        log.info("pairing: %s paired from %s%s", req.get("client", "a machine"), conn.addr[0],
                 " (first: claimed)" if first else "")
        raise StopIteration
