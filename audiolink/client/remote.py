"""Talking to a Pi's endpoint from any machine (docs/protocol.md): found on this network
(--at, its last address, mDNS, the setup network) or through its relay, then this
computer's key proves itself once (auth), and the connection has that key's role.
PI is a name from `audiolink pis`, a Pi code, or (for a Pi this computer hasn't added)
an address.
"""
import re
import socket
import threading

from .. import tls
from ..ui import detail, done, step
from . import keyring
from .computer import Key, key as computer_key, this_computer
from .pairing import Busy, NotListed, OldPi, PiConn, busy, impostor, reach, too_old


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


def over_relay(pub):
    """(a plain socket carrying the Pi's endpoint through its relay, None), or (None, the
    relay's reason, e.g. "offline"). The relay connection is TLS, which only one thread
    may drive: splice() drives it, and the caller gets the other end of a socket pair."""
    from ..relay.client import connect
    from ..relay.wire import Refused, splice
    try:
        relayed = connect(pub)
    except Refused as e:
        return None, str(e)
    mine, theirs = socket.socketpair()
    threading.Thread(target=splice, args=(theirs, relayed), daemon=True).start()
    return mine, None


def through_relay(sock, pub, name):
    """(PiConn, hello, how) over a relayed socket, or None."""
    try:
        conn = PiConn("relay", sock=sock)
    except (OSError, ValueError):
        sock.close()
        return None
    try:
        hello = conn.hello()
    except tls.Mismatch:
        conn.close()
        raise impostor("through the relay", name)
    except OldPi as e:
        conn.close()
        raise too_old(e.hello.get("name") or name)
    except (OSError, ValueError):
        conn.close()
        return None
    if hello.get("pub") != pub.hex():                   # the key in the code: proven by TLS
        conn.close()
        raise impostor("through the relay", name)
    return conn, hello, "through the relay"


def find(entry, at, turned_away):
    """(PiConn, hello, how, why) : how it was reached, or (None, None, None, why the relay
    didn't). Addresses that turned this computer away (the Pi is busy) go in turned_away."""
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
                return conn, conn.hello(), "at %s" % ip, None
            except tls.Mismatch:
                conn.close()
                raise impostor("at %s" % ip)
            except OldPi as e:
                conn.close()
                raise too_old(e.hello.get("name") or ip)
            except (OSError, ValueError):
                conn.close()
        return None, None, None, None
    conn, hello = reach(entry, at, busy=turned_away)
    if conn:
        return conn, hello, "at %s" % conn.ip, None
    pub = keyring.parse(entry["code"])
    sock, why = over_relay(pub)
    if sock is not None:
        found = through_relay(sock, pub, entry.get("alias") or "the Pi")
        if found:
            return found + (None,)
        why = "no answer through the relay"
    return None, None, None, why


def learn_usb_ids(rec):
    """The Pi's record says which USB ID its flavor has (services/flavor.py): this
    computer then finds it plugged in as that, too (host.json usb_ids)."""
    if isinstance(rec, dict) and isinstance(rec.get("usb"), list):
        from ..host.config import remember_usb_ids
        for i in remember_usb_ids(rec["usb"]):
            detail("learned the Pi's USB ID %s (host.json usb_ids)", i)


def not_listed(name):
    return SystemExit("%s doesn't have this computer on its list. On an owner computer: audiolink invite %s; "
                      "then audiolink setup here" % (name, this_computer()))


class Remote:
    def __init__(self, entry, at, key=None):
        self.entry, self.at = entry, at
        self.key = key or computer_key()
        self.name = entry.get("alias") or entry.get("name") or "the Pi"
        self.conn = self.hello = None
        self.relay_why = None

    def open(self, quiet=False):
        if not quiet:
            step("Finding %s" % self.name)
        turned_away = []
        self.conn, self.hello, how, self.relay_why = find(self.entry, self.at, turned_away)
        if not self.conn:
            if not quiet:
                done("not found")
            if turned_away:
                raise busy(self.name)
            if self.relay_why == "offline":
                raise SystemExit("%s is offline (not on this network, nor on its relay)%s"
                                 % (self.name, _last_seen(self.entry)))
            raise SystemExit("%s isn't reachable from here (not on this network, its setup network or its relay)"
                             % self.name)
        try:
            self.conn.auth(self.key)
        except NotListed:
            self.conn.close()
            if not quiet:
                done("not let in")
            raise not_listed(self.name)
        if self.entry.get("code"):
            pub = keyring.pub_of(self.entry["code"])
            rec = self.conn.record
            fields = {"role": self.conn.role}
            if isinstance(rec, dict) and rec.get("ts"):
                fields["last_record"] = rec
                if rec.get("name"):
                    fields["name"] = rec["name"]
            learn_usb_ids(rec)
            keyring.update(pub, **fields)
            keyring.seen(pub)
        if not quiet:
            done("found")
            if not self.entry.get("code"):
                print("This computer has no code for %s, so it can't tell it's the real one "
                      "(the connection is encrypted, but anyone could answer at that address)" % self.name)
        detail("%s, as %s", how, self.conn.role)
        return self

    @property
    def role(self):
        return self.conn.role if self.conn else None

    def ask(self, op, **fields):
        return self.conn.ask(dict(fields, op=op))

    def call(self, op, args=None):
        """Like host.control.call for an endpoint request: the result, or SystemExit."""
        r = self.ask(op, **(args or {}))
        if "error" in r:
            raise SystemExit(self.refusal(r))
        return r

    def refusal(self, r):
        code = r.get("code")
        if code == "unknown" or str(r.get("error", "")).startswith("unknown op"):
            return "%s's audiolink is too old for this; update it over USB" % self.name
        if code == "not_owner":
            return "Only an owner of %s can do that (this computer is a %s)" % (self.name, self.role or "user")
        if code == "not_listed":
            return str(not_listed(self.name).code)
        return r["error"]

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None


def _last_seen(entry):
    from ..ui import ago
    rec = entry.get("last_record") or {}
    if not rec.get("ts"):
        return ""
    return "; last seen %s on %s" % (ago(rec["ts"]), rec.get("ssid") or "an unknown network")


def open_remote(pi, at=(), key_path=None):
    return Remote(_resolve(pi), list(at), Key(key_path) if key_path else None).open()
