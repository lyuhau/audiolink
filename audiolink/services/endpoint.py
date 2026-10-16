"""Pi side: the network endpoint (TCP PORT, mDNS _audiolink._tcp) that machines reach
over Wi-Fi, the setup network or the relay. Services register requests on it (pairing,
Wi-Fi changes, logs, updates, shells), each with the access level it needs
(services/access.py decides).

Protocol: TLS 1.3 (audiolink/tls.py: the Pi's certificate is for its identity key, which
clients compare with the one in its connect code), then one JSON object per line.
Every connection starts with
  {"op":"hello"} -> {"name","tag","pub","claimed","nonce","tls"}
Proofs a request can carry (each bound to the Pi's key as the client saw it in TLS, so
one made for an impostor's connection is no good here):
  "proof"        HMAC-SHA256(read secret, op + nonce + key): a machine that paired with
                 this Pi; its sensitive fields travel sealed with the read secret (Conn.seal/open)
  "key", "sig"   an ed25519 signature of KEY_LABEL + op + nonce + key (debug key mode)
A connection that starts without TLS (an older audiolink) gets its hello answered and
every request refused with UPGRADE, so it can say what to do.
A handler can keep the connection for itself (a shell): it raises Takeover, and the
endpoint frees the request slot and drops its timeout first.

Strangers can reach the endpoint (the LAN, the open setup network), so a connection
has PROVE_WITHIN s to make a request that proves something (paired, or a debug
request the switch allows) before it's dropped, one address can hold PER_ADDR
connections that haven't, and only proven ones count as someone connected
(services/presence.py). One turned away gets BUSY in the clear (the client says the
Pi is busy) and the refusal is logged.
"""
import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time

from .. import tls
from ..crypto import ed25519_verify, open_sealed, seal
from ..net import reuse_addr
from .access import DEBUG, KEY_LABEL, PUBLIC, RELAY_SOURCE, decide, route_of

log = logging.getLogger("audiolink")

PORT = 47900
MAX_CONNS = 16              # connections served at once
PER_ADDR = 2                # of them not yet proven, from one address
PROVE_WITHIN = 15           # s a connection has to prove itself before it's dropped
IDLE = 30                   # s between requests on a proven connection
EXEMPT = (RELAY_SOURCE, "127.0.0.1")    # the relay (proven there; services/relay.py caps it), the Pi
BUSY = {"error": "busy", "code": "busy"}
LOG_EVERY = 60              # s between logged refusals of one address
LINGER = 1                  # s a turned-away connection stays open after BUSY
UPGRADE = {"error": "This Pi's audiolink is newer than this computer's: update audiolink here",
           "code": "upgrade"}


def proof_for(secret, op, nonce, pub):
    """pub: the Pi's public key from its TLS certificate."""
    return hmac.new(secret, tls.bind(op.encode() + nonce, pub), hashlib.sha256).hexdigest()


def key_message(op, nonce, pub):
    return tls.bind(KEY_LABEL + op.encode() + nonce, pub)


class Takeover(Exception):
    """Raised by a handler that keeps the connection: run() then has it, on this thread,
    without the endpoint's slot or timeout."""

    def __init__(self, run):
        super().__init__("takeover")
        self.run = run


class Conn:
    """One client connection, as seen by request handlers."""

    def __init__(self, sock, addr, identity, route="relay", secure=True):
        self.sock, self.addr, self.route = sock, addr, route
        self.identity = identity
        self.secure = secure                # TLS (False: an older client, refused)
        self.pub = identity.public()        # what proofs are bound to
        self.nonce = os.urandom(16)
        self.f = sock.makefile("rwb")
        self.verified = set()               # (key, sig) already checked on this connection
        self.logged = set()                 # debug ops logged on this connection
        self.proven = False                 # made a request that proved something
        self.on_proven = lambda: None

    def prove(self):
        """This connection is someone legitimate (a passed check, or a handler's say: a
        machine that just paired)."""
        if not self.proven:
            self.proven = True
            self.on_proven()

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
    def __init__(self, identity, port=PORT, presence=None, debug=None, wifi_ip=None, bind="0.0.0.0",
                 exempt=EXEMPT):
        self.identity = identity
        self.presence = presence                # services/presence.py: requests count as clients
        self.debug = debug                      # services/access.py DebugService (None: debug off)
        self.wifi_ip = wifi_ip or (presence.wifi_ip if presence else (lambda: None))
        self.handlers = {}                      # op -> (fn(conn, req) -> reply or None, level)
        self.exempt = exempt                    # addresses PER_ADDR doesn't apply to
        self.lock = threading.Lock()
        self.active = 0                         # connections being served
        self.unproven = {}                      # address -> its connections not yet proven
        self.turned_away = {}                   # address -> when that was last logged
        self.closing = []                       # [(when, socket)] turned away, closed then
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        reuse_addr(srv)
        srv.bind((bind, port))
        srv.listen(8)
        self.port = srv.getsockname()[1]
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()
        log.info("endpoint: listening on :%d", self.port)

    def register(self, op, fn, level=PUBLIC):
        """fn(conn, req) returns a reply dict, or None if it replied itself; it raises
        StopIteration to end the connection after replying, or Takeover to keep it.
        level: the access level it needs, or a tuple of levels any one of which will do."""
        self.handlers[op] = (fn, level)

    def _proven(self, conn, op, req, need, switch):
        proven = set()
        secret = self.identity.keys()[1]
        if self.identity.claimed() and hmac.compare_digest(proof_for(secret, op, conn.nonce, conn.pub),
                                                           str(req.get("proof", ""))):
            proven.add("paired")
        if need == DEBUG and switch.get("mode") == "key" and req.get("key") and req.get("sig"):
            got = (str(req["key"]), str(req["sig"]))
            try:
                key, sig = bytes.fromhex(got[0]), bytes.fromhex(got[1])
            except ValueError:
                return proven
            if key in self.debug.keys() and (got in conn.verified or       # still one of the keys
                                             ed25519_verify(key, key_message(op, conn.nonce, conn.pub), sig)):
                conn.verified.add(got)
                proven.add("key")
        return proven

    def check(self, conn, op, req, need):
        """None if the request may go ahead, else the refusal reply (logged). need: a
        level, or a tuple of levels any one of which will do."""
        levels = need if isinstance(need, tuple) else (need,)
        if PUBLIC in levels:
            return None
        switch = self.debug.snapshot() if self.debug else {"mode": "off"}
        refusals = []
        for level in levels:
            why = decide(level, conn.route, self._proven(conn, op, req, level, switch), switch, time.monotonic())
            if not why:
                if level == DEBUG and op not in conn.logged:
                    conn.logged.add(op)
                    log.info("access: %s from %s (%s, debug %s)", op, conn.addr[0], conn.route, switch.get("mode"))
                return None
            refusals.append(why)
        why = refusals[-1] if req.get("key") else refusals[0]      # what the asker went by
        log.warning("access: refused %s from %s (%s): %s", op, conn.addr[0], conn.route, why[0])
        return {"error": why[1], "code": why[0]}

    def _accept(self, srv):
        srv.settimeout(LINGER)
        while True:
            self._close_turned_away()
            try:
                sock, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError as e:                # e.g. out of file descriptors
                log.warning("endpoint: %s", e)
                time.sleep(1)
                continue
            why = self._admit(addr[0])
            if why:
                self._turn_away(sock, addr[0], why)
                continue
            try:
                threading.Thread(target=self._serve, args=(sock, addr), daemon=True).start()
            except RuntimeError as e:           # out of threads
                log.warning("endpoint: %s", e)
                sock.close()
                self._leave(addr[0], held=True, proven=False)

    def _admit(self, ip):
        """None (and the connection counts), or why it's turned away."""
        with self.lock:
            if self.active >= MAX_CONNS:
                return "%d connections already" % self.active
            if ip not in self.exempt and self.unproven.get(ip, 0) >= PER_ADDR:
                return "%d connections from it that haven't proven anything" % self.unproven[ip]
            self.active += 1
            self.unproven[ip] = self.unproven.get(ip, 0) + 1
        return None

    def _leave(self, ip, held, proven):
        """A connection stops counting: as served (held), and as unproven (not proven)."""
        with self.lock:
            if held:
                self.active -= 1
            if not proven:
                n = self.unproven.get(ip, 0) - 1
                if n > 0:
                    self.unproven[ip] = n
                else:
                    self.unproven.pop(ip, None)

    def _turn_away(self, sock, ip, why):
        try:
            sock.setblocking(False)
            sock.send((json.dumps(BUSY) + "\n").encode())
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            sock.close()
        else:
            # Closed now, with the client's TLS hello unread, it would get a reset, and
            # Windows drops the BUSY it already has when one comes: closed later instead.
            self.closing.append((time.monotonic() + LINGER, sock))
            while len(self.closing) > MAX_CONNS:
                self.closing.pop(0)[1].close()
        now = time.monotonic()
        with self.lock:
            if now - self.turned_away.get(ip, -LOG_EVERY) < LOG_EVERY:
                return
            self.turned_away = {k: t for k, t in self.turned_away.items() if now - t < LOG_EVERY}
            self.turned_away[ip] = now
        log.warning("endpoint: turned away %s (%s)", ip, why)

    def _close_turned_away(self):
        now = time.monotonic()
        while self.closing and self.closing[0][0] <= now:
            self.closing.pop(0)[1].close()

    def _serve(self, sock, addr):
        held, conn = True, None
        try:
            ends = (sock.getsockname(), addr) if self.presence else None
        except OSError:
            ends = None
        if ends:
            self.presence.hold(ends)            # not someone connected until it proves it

        def proven():
            self._leave(addr[0], held=False, proven=False)
            if ends:
                self.presence.release(ends)
                self.presence.touch(sock)
        try:
            deadline = time.monotonic() + PROVE_WITHIN
            sock.settimeout(PROVE_WITHIN)
            try:
                local = sock.getsockname()[0]
            except OSError:
                local = None
            route = route_of(local, addr[0], self.wifi_ip())
            first = sock.recv(1, socket.MSG_PEEK)
            secure = first[:1] == bytes([tls.FIRST_BYTE])
            if secure:
                sock = tls.server_context(self.identity.keys()[0]).wrap_socket(sock, server_side=True)
            conn = Conn(sock, addr, self.identity, route, secure)
            conn.on_proven = proven
            greeted = False
            while True:
                left = IDLE if conn.proven else min(IDLE, deadline - time.monotonic())
                if left <= 0:
                    return
                sock.settimeout(left)
                req = conn.recv()
                op = req.get("op")
                if op == "hello":
                    if greeted:
                        return                  # one per connection
                    greeted = True
                    info = self.identity.info()
                    conn.send({"name": info["name"], "tag": info["tag"], "pub": info["pub"],
                               "claimed": info["claimed"], "nonce": conn.nonce.hex(), "tls": True})
                    continue
                if not secure:
                    conn.send(UPGRADE)
                    continue
                if op not in self.handlers:
                    conn.send({"error": "unknown op %r" % op})
                    continue
                fn, level = self.handlers[op]
                refused = self.check(conn, op, req, level)
                if refused:
                    conn.send(refused)
                    continue
                if PUBLIC not in (level if isinstance(level, tuple) else (level,)):
                    conn.prove()
                try:
                    reply = fn(conn, req)
                except StopIteration:
                    return
                except Takeover as t:
                    held = False
                    self._leave(addr[0], held=True, proven=True)
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
            was_proven = bool(conn and conn.proven)
            if ends:
                if was_proven:
                    self.presence.touch(sock)
                else:
                    self.presence.release(ends)
            sock.close()
            self._leave(addr[0], held=held, proven=was_proven)
