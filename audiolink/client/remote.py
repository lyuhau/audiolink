"""Talking to a Pi's endpoint from any machine, with whatever proof this machine has
(services/access.py): its connect code (paired), a debug key (--key FILE), or none.

Reached like `connect`: the local network (--at, last address, mDNS, the setup
network), then the relay (needs the connect code), then a DHT lookup. PI is a name
from `audiolink pis`, a connect code, or (for a Pi this machine hasn't added) an address.
"""
import re
import socket
import threading

from .. import tls
from ..discovery.record import parse_code
from ..services.endpoint import PORT, key_message, proof_for
from ..ui import detail, done, step
from . import keyring
from .computer import Key
from .pairing import Busy, OldPi, PiConn, busy, impostor, reach, too_old


# ---------------------------------------------------------------- reaching a Pi
def _looks_like_address(s):
    return bool(re.fullmatch(r"[0-9.]+|[0-9a-fA-F:]+:[0-9a-fA-F:]*|[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+", s))


def _resolve(pi):
    try:
        return keyring.resolve(pi)
    except keyring.Ambiguous:
        return keyring.lookup(pi)               # ends the command: which name to use
    except KeyError:
        if _looks_like_address(pi):
            return {"alias": pi, "code": None, "addr": pi}
        raise SystemExit("No Pi named %s (see: audiolink pis; an address works too)" % pi)


def _over_relay(pub, secret):
    """A plain socket carrying the Pi's endpoint through its relay, or None. The relay
    connection is TLS, which only one thread may drive: splice() drives it, and the
    caller gets the other end of a socket pair."""
    from ..relay.client import Denied, connect
    from ..relay.wire import Refused, splice
    try:
        tls = connect(pub, secret, PORT)
    except Denied as e:
        raise SystemExit(str(e))
    except Refused:
        return None
    mine, theirs = socket.socketpair()
    threading.Thread(target=splice, args=(theirs, tls), daemon=True).start()
    return mine


def _through_relay(sock, pub, name):
    """(PiConn, hello, how) over a relayed socket, or None."""
    try:
        conn = PiConn("relay", sock=sock)
    except (OSError, ValueError):                       # e.g. the Pi's audiolink predates TLS
        sock.close()
        return None
    try:
        hello = conn.hello()
    except tls.Mismatch:
        conn.close()
        raise impostor("through the relay", name)
    except (OSError, ValueError):
        conn.close()
        return None
    if hello.get("pub") != pub.hex():                   # the key in the code: proven by TLS
        conn.close()
        raise impostor("through the relay", name)
    return conn, hello, "through the relay"


def _find(entry, at, turned_away):
    """(PiConn, hello, how) or (None, None, None). Addresses that turned this machine
    away (the Pi is busy) go in turned_away."""
    if not entry.get("code"):
        for ip in list(at) + [entry["addr"]]:
            try:
                conn = PiConn(ip, timeout=3)
            except OldPi as e:
                raise too_old(e.hello.get("name") or ip)
            except Busy:
                turned_away.append(ip)
                continue
            except (OSError, ValueError):
                continue
            try:
                return conn, conn.hello(), "at %s" % ip
            except tls.Mismatch:
                conn.close()
                raise impostor("at %s" % ip)
            except (OSError, ValueError):
                conn.close()
        return None, None, None
    conn, hello = reach(entry, at, busy=turned_away)
    if conn:
        return conn, hello, "at %s" % conn.ip
    pub, secret = parse_code(entry["code"])
    sock = _over_relay(pub, secret)
    if sock is not None:
        found = _through_relay(sock, pub, entry.get("alias") or "the Pi")
        if found:
            return found
    conn, hello = reach(entry, at, with_dht=True, busy=turned_away)
    return (conn, hello, "at %s" % conn.ip) if conn else (None, None, None)


class Remote:
    def __init__(self, entry, at, key):
        self.entry, self.at, self.key = entry, at, key
        self.name = entry.get("alias") or entry.get("name") or "the Pi"
        self.secret = parse_code(entry["code"])[1] if entry.get("code") else None
        self.conn = self.hello = None

    def open(self, quiet=False):
        if not quiet:
            step("Finding %s" % self.name)
        turned_away = []
        self.conn, self.hello, how = _find(self.entry, self.at, turned_away)
        if not self.conn:
            if not quiet:
                done("not found")
            if turned_away:
                raise busy(self.name)
            raise SystemExit("%s isn't reachable from here (not on this network, its setup network or its relay)"
                             % self.name)
        if self.entry.get("code"):
            keyring.seen(keyring.pub_of(self.entry["code"]))
        if not quiet:
            done("found")
            if not self.entry.get("code"):
                print("This computer has no code for %s, so it can't tell it's the real one "
                      "(the connection is encrypted, but anyone could answer at that address)" % self.name)
        detail("%s", how)
        return self

    def ask(self, op, **fields):
        """A request with every proof this machine has."""
        nonce, pub = bytes.fromhex(self.hello["nonce"]), self.conn.pub
        req = dict(fields, op=op)
        if self.secret:
            req["proof"] = proof_for(self.secret, op, nonce, pub)
        if self.key:
            req["key"], req["sig"] = self.key.pub.hex(), self.key.sign(key_message(op, nonce, pub)).hex()
        return self.conn.ask(req)

    def call(self, op, args=None):
        """Like host.control.call for an endpoint request: the result, or SystemExit."""
        r = self.ask(op, **(args or {}))
        if "error" in r:
            raise SystemExit(self.refusal(r))
        return r

    def refusal(self, r):
        if r.get("code") == "unknown" or str(r.get("error", "")).startswith("unknown op"):
            return "%s's audiolink is too old for this; update it over USB" % self.name
        return r["error"]

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None


def open_remote(pi, at=(), key_path=None):
    return Remote(_resolve(pi), list(at), Key(key_path) if key_path else None).open()

