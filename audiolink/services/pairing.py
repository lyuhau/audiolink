"""Pi side: invites (docs/protocol.md "Invites"), redeemed over the network (via
services/endpoint.py).

An owner asks for an invite (endpoint "invite", or identity.invite over USB): a
one-use 6-digit code for {name, role}. Codes expire after CODE_TTL, work once, allow
MAX_TRIES guesses, and a new one replaces the last. Redeeming runs SPAKE2 with the
code, so the code never crosses the network and a recording of the exchange can't be
used to guess it offline; the redeemer's key and the Pi's answer travel sealed with
the exchange's key. Wrong codes back off exponentially per address (one stranger can't
lock everyone out), forgotten after DECAY s without one. The exchange runs whether or
not a code is active (with none, against a random one), so asking doesn't tell a
stranger when there's a code to guess. A Pi with no owner refuses: claiming is over
USB only (identity.claim).

  {"op":"pair","msg":X,"client":hostname} -> {"msg":Y}
  {"confirm":cA, "key": seal(ke, {"key"})} -> {"confirm":cB, "payload": seal(ke, {"name","role","pub","record"})}
payload: the Pi's name and key, the role the computer now has, and where the Pi is
(record, as on every auth).
"""
import hmac
import json
import logging
import secrets
import threading
import time

from ..crypto import Spake2, open_sealed, seal
from .access import NONE, ROLES

log = logging.getLogger("audiolink")

CODE_TTL = 300
MAX_TRIES = 3
MAX_BACKOFF = 300
DECAY = 600                     # s without a wrong code before an address starts over
ID_CLIENT, ID_PI = b"audiolink client", b"audiolink pi"


class PairingService:
    """Not a link service: invites over USB are identity.invite (services/identity.py)."""

    def __init__(self, identity, endpoint):
        self.identity = identity
        self.record = endpoint.record   # -> where the Pi is now
        self.lock = threading.Lock()
        self.code = None                # {"code", "name", "role", "expires", "tries"}
        self.failures = {}              # address -> {"count", "until", "last"}
        identity.inviter = self.invite
        endpoint.register("pair", self._pair, NONE)

    # ---- codes
    def invite(self, name, role):
        if role not in ROLES:
            raise ValueError("roles are user and owner")
        with self.lock:
            self.code = {"code": "%06d" % secrets.randbelow(10 ** 6), "name": name, "role": role,
                         "expires": time.monotonic() + CODE_TTL, "tries": MAX_TRIES}
        log.info("pairing: an invite for %s (%s)", name, role)
        return {"code": self.code["code"], "expires_in": CODE_TTL}

    def _active_code(self):
        c = self.code
        if c and c["tries"] > 0 and time.monotonic() < c["expires"]:
            return c
        self.code = None
        return None

    # ---- endpoint requests
    def _pair(self, conn, req):
        ip = conn.addr[0]
        pi = self.identity.pi_name()
        if not self.identity.claimed():
            conn.send({"error": "%s has no owner yet: plug it into a computer and run audiolink setup there"
                                % pi, "code": "claim_usb"})
            raise StopIteration
        with self.lock:
            now = time.monotonic()
            self.failures = {k: f for k, f in self.failures.items() if now - f["last"] < DECAY}
            failed = self.failures.get(ip)
            if failed and failed["until"] > now:
                conn.send({"error": "Too many wrong codes; retry in %ds" % -(-(failed["until"] - now) // 1)})
                raise StopIteration
            c = self._active_code()
            password = c["code"] if c else secrets.token_hex(16)
        b = Spake2("B", password, ID_CLIENT, ID_PI)
        try:
            ke, ours, theirs = b.finish(bytes.fromhex(req["msg"]))
        except (KeyError, TypeError, ValueError):
            conn.send({"error": "Bad request"})
            raise StopIteration
        conn.send({"msg": b.msg.hex()})
        answer = conn.recv()
        try:
            confirm = bytes.fromhex(str(answer.get("confirm", "")))
        except (AttributeError, ValueError):
            confirm = b""
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
                    error = "Wrong code, and that was its last try; ask an owner of %s for a new invite" % pi
                else:
                    error = "That code has expired or been used up; ask an owner of %s for a new invite" % pi
                conn.send({"error": error})
                raise StopIteration
            self.failures.pop(ip, None)
            if self.code is c:
                self.code = None        # single use
        try:
            sealed = open_sealed(ke, bytes.fromhex(str(answer.get("key", ""))))
            key = bytes.fromhex(json.loads(sealed)["key"])
            entry = self.identity.add(key, c["name"], c["role"])
        except (TypeError, ValueError, KeyError):
            conn.send({"error": "Bad request: no key to put on the list (is audiolink up to date here?)"})
            raise StopIteration
        conn.prove()                    # someone legitimate is here (services/presence.py)
        payload = {"name": pi, "role": entry["role"], "as": entry["name"], "pub": self.identity.public().hex(),
                   "record": self.record()}
        conn.send({"confirm": ours.hex(), "payload": seal(ke, json.dumps(payload).encode()).hex()})
        log.info("pairing: %s (%s) added from %s (%s), as %s", req.get("client", "a computer"), entry["role"],
                 conn.addr[0], conn.route, entry["name"])
        raise StopIteration
