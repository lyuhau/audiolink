"""Pi side: the network endpoint (TCP PORT, mDNS _audiolink._tcp) that computers reach
over Wi-Fi, the setup network or the relay (docs/protocol.md). Services register
requests on it (streams, invites, Wi-Fi changes, logs, updates, shells), each with the
role it needs (services/access.py).

Protocol: TLS 1.3 (audiolink/tls.py: the Pi's certificate is for its identity key, which
clients pin to the Pi code they hold), then one JSON object per line:
  {"op":"hello"} -> {"v", "name", "tag", "pub", "claimed", "nonce", "tls"}
  {"op":"auth", "key", "sig"} -> {"ok", "name", "role", "record"} | {"error", "code": "not_listed"}
The signature is over auth.auth_message(nonce, the Pi's key), so it's good on this
connection only. Once per connection; the connection then has that key's role. Without
it only hello and pair work: anything else is an older client's (the read secret's
`proof`), told UPGRADE, as is a connection that starts without TLS.

A handler can keep the connection for itself (a shell, an opened stream): it raises
Takeover, and the endpoint frees the request slot and drops its timeout first. A
computer taken off the list (or a Pi that started over) loses its connections at once,
held ones too; an owner made a user loses those that needed an owner.

Strangers can reach the endpoint (the LAN, the open setup network, the relay), so a
connection has PROVE_WITHIN s to prove a key (auth, or pairing) before it's dropped,
one address can hold PER_ADDR connections that haven't, and only proven ones count as
someone connected (services/presence.py). One turned away gets BUSY in the clear (the
client says the Pi is busy) and the refusal is logged.
"""
import json
import logging
import os
import socket
import threading
import time

from .. import auth, tls
from ..net import reuse_addr
from .access import NONE, OWNER, RELAY_SOURCE, Refused, allows, not_owner, route_of

log = logging.getLogger("audiolink")

PORT = 47900
MAX_CONNS = 16              # connections served at once (held ones don't count)
PER_ADDR = 2                # of them not yet proven, from one address
PROVE_WITHIN = 15           # s a connection has to prove itself before it's dropped
IDLE = 30                   # s between requests on a proven connection
MAX_LINE = 1 << 20          # bytes in one request (an update chunk is 64 KiB)
EXEMPT = (RELAY_SOURCE, "127.0.0.1")    # the relay (services/relay.py caps it), the Pi
BUSY = {"error": "busy", "code": "busy"}
LOG_EVERY = 60              # s between logged refusals of one address
LINGER = 1                  # s a turned-away connection stays open after BUSY
UPGRADE = {"error": "This Pi's audiolink is newer than this computer's: update audiolink here",
           "code": "upgrade"}


class Takeover(Exception):
    """Raised by a handler that keeps the connection: run() then has it, on this thread,
    without the endpoint's slot or timeout."""

    def __init__(self, run):
        super().__init__("takeover")
        self.run = run


class Conn:
    """One client connection, as seen by request handlers. key, name, role: the
    computer it proved itself to be (None before auth)."""

    def __init__(self, sock, addr, identity, route="relay", secure=True):
        self.sock, self.addr, self.route = sock, addr, route
        self.identity = identity
        self.secure = secure                # TLS (False: an older client, refused)
        self.nonce = os.urandom(auth.NONCE_BYTES)
        self.buf = b""                      # read past the last request (a stream's first bytes)
        self.key = self.name = self.role = None
        self.proven = False                 # proved a key, or paired
        self.on_proven = lambda: None
        self.lock = threading.Lock()
        self.busy = False                   # serving a request: a revoke waits for its reply
        self.revoked = None                 # why it's being closed
        self.held = None                    # the role a takeover needs while it has the connection
        self.on_revoke = None               # a takeover's own way to close (a shell says why)

    def prove(self):
        """This connection is someone legitimate (a key on the list, or a computer that
        just paired)."""
        if not self.proven:
            self.proven = True
            self.on_proven()

    def send(self, obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def recv(self):
        while b"\n" not in self.buf:
            if len(self.buf) > MAX_LINE:
                raise ValueError("request too long")
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("closed")
            self.buf += data
        line, _, self.buf = self.buf.partition(b"\n")
        return json.loads(line)

    def leftover(self):
        """What the client sent after its last request (a takeover's first bytes)."""
        data, self.buf = self.buf, b""
        return data

    def revoke(self, why):
        """Close it because its key lost its place (see Endpoint._recheck)."""
        with self.lock:
            if self.revoked:
                return
            self.revoked = why
            if self.busy:
                return                      # the request's reply goes out first
            hook = self.on_revoke
        if hook:
            hook(why)
        else:
            _shut(self.sock)


def _shut(sock):
    """Wakes whatever thread is reading it. A bare TCP shutdown, even under TLS: the
    thread using the TLS state finds the end itself."""
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass


class Endpoint:
    def __init__(self, identity, port=PORT, presence=None, wifi_ip=None, bind="0.0.0.0", exempt=EXEMPT,
                 record=None):
        self.identity = identity
        self.presence = presence                # services/presence.py: requests count as clients
        self.wifi_ip = wifi_ip or (presence.wifi_ip if presence else (lambda: None))
        self.record = record or (lambda: None)  # -> {"name", "ts", "lan", "ssid", "ports"}: where the Pi is
        self.handlers = {}                      # op -> (fn(conn, req) -> reply or None, role)
        self.exempt = exempt                    # addresses PER_ADDR doesn't apply to
        self.lock = threading.Lock()
        self.active = 0                         # connections being served
        self.unproven = {}                      # address -> its connections not yet proven
        self.turned_away = {}                   # address -> when that was last logged
        self.closing = []                       # [(when, socket)] turned away, closed then
        self.authed = set()                     # connections with a key, held ones too
        self.pub = identity.public()
        identity.subscribe(self._recheck)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        reuse_addr(srv)
        srv.bind((bind, port))
        srv.listen(8)
        self.port = srv.getsockname()[1]
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()
        log.info("endpoint: listening on :%d", self.port)

    def register(self, op, fn, role=OWNER):
        """fn(conn, req) returns a reply dict, or None if it replied itself; it raises
        StopIteration to end the connection after replying, Takeover to keep it, or
        access.Refused to refuse. role: what the connection needs (NONE: anyone)."""
        self.handlers[op] = (fn, role)

    # ---- keys
    def _auth(self, conn, req):
        if conn.key is not None:
            return {"error": "This connection has proved a key already"}
        name = self.identity.pi_name()
        try:
            key, sig = bytes.fromhex(str(req.get("key"))), bytes.fromhex(str(req.get("sig")))
        except ValueError:
            key = sig = b""
        if len(key) != 32 or not auth.verify(key, conn.nonce, conn.identity.public(), sig):
            log.warning("endpoint: a key proof from %s (%s) that doesn't check out", conn.addr[0], conn.route)
            return {"error": "This computer's key proof didn't check out (is audiolink up to date here?)",
                    "code": "not_listed"}
        member = self.identity.member(key)
        if member is None:
            log.warning("endpoint: %s (%s) isn't on the list (key %s...)", conn.addr[0], conn.route, key.hex()[:8])
            if not self.identity.claimed():
                return {"error": "No owner yet on %s: claim it with audiolink setup on the computer it's "
                                 "plugged into" % name, "code": "not_listed"}
            return {"error": "This computer isn't on %s's list: ask an owner of %s for an invite (audiolink "
                             "invite NAME, on their computer)" % (name, name), "code": "not_listed"}
        conn.key, conn.name, conn.role = key, member["name"], member["role"]
        with self.lock:
            self.authed.add(conn)
        conn.prove()
        log.info("endpoint: %s (%s) from %s (%s)", conn.name, conn.role, conn.addr[0], conn.route)
        return {"ok": True, "name": conn.name, "role": conn.role, "record": self.record()}

    def _recheck(self):
        """The list or the key changed: connections whose key lost its place go."""
        started_over = self.identity.public() != self.pub
        self.pub = self.identity.public()
        with self.lock:
            conns = list(self.authed)
        for conn in conns:
            role = NONE if started_over else self.identity.role_of(conn.key)
            if role is NONE:
                conn.revoke("%s started over" % self.identity.pi_name() if started_over
                            else "taken off %s's list" % self.identity.pi_name())
            elif role != conn.role:
                conn.role = role
                if conn.held and not allows(role, conn.held):
                    conn.revoke("no longer an owner of %s" % self.identity.pi_name())

    def check(self, conn, op, req, need):
        """None if the request may go ahead, else the refusal reply (logged)."""
        if need is NONE:
            return None
        if conn.key is None or "proof" in req:
            return UPGRADE                      # an older client: no auth, or the read secret's proof
        if allows(conn.role, need):
            return None
        log.warning("access: refused %s to %s (%s) from %s (%s)", op, conn.name, conn.role, conn.addr[0], conn.route)
        e = not_owner(self.identity, conn.name) if need == OWNER else Refused("not_listed", "Not on the list")
        return {"error": str(e), "code": e.code}

    # ---- connections
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

    def _hello(self, conn):
        info = self.identity.info()
        conn.send({"v": auth.ENDPOINT_VERSION, "name": info["name"], "tag": info["tag"], "pub": info["pub"],
                   "claimed": self.identity.claimed(), "nonce": conn.nonce.hex(), "tls": True})

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
                sock = tls.server_context(self.identity.seed()).wrap_socket(sock, server_side=True)
            conn = Conn(sock, addr, self.identity, route, secure)
            conn.on_proven = proven
            greeted = False
            while True:
                left = IDLE if conn.proven else min(IDLE, deadline - time.monotonic())
                if left <= 0:
                    return
                sock.settimeout(left)
                req = conn.recv()
                op = req.get("op") if isinstance(req, dict) else None
                if op == "hello":
                    if greeted:
                        return                  # one per connection
                    greeted = True
                    self._hello(conn)
                    continue
                if not secure:
                    conn.send(UPGRADE)
                    continue
                if op == "auth":
                    conn.send(self._auth(conn, req))
                    continue
                if op not in self.handlers:
                    conn.send({"error": "unknown op %r" % op} if conn.key else UPGRADE)
                    continue
                fn, need = self.handlers[op]
                refused = self.check(conn, op, req, need)
                if refused:
                    conn.send(refused)
                    continue
                with conn.lock:
                    conn.busy = True
                try:
                    reply = fn(conn, req)
                except StopIteration:
                    return
                except Takeover as t:
                    held = False
                    self._leave(addr[0], held=True, proven=True)
                    sock.settimeout(None)
                    with conn.lock:
                        conn.busy, conn.held = False, need
                        revoked = conn.revoked
                    if not revoked:
                        t.run()
                    return
                except Refused as e:
                    reply = {"error": str(e), "code": e.code}
                except (OSError, ConnectionError):
                    raise
                except Exception as e:          # a bug, or a request shaped for another version
                    log.warning("endpoint: %s failed: %s", op, e)
                    reply = {"error": "The Pi couldn't do that (%s)" % (str(e) or type(e).__name__)}
                if reply is not None:
                    conn.send(reply)
                with conn.lock:
                    conn.busy = False
                    if conn.revoked:
                        return
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            if conn:
                with self.lock:
                    self.authed.discard(conn)
            was_proven = bool(conn and conn.proven)
            if ends:
                if was_proven:
                    self.presence.touch(sock)
                else:
                    self.presence.release(ends)
            sock.close()
            self._leave(addr[0], held=held, proven=was_proven)
