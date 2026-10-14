"""Talking to a Pi's endpoint from any machine, with whatever proof this machine has
(services/access.py): its connect code (paired), a debug key (--key FILE), or none.

Reached like `connect`: the local network (--at, last address, mDNS, the setup
network), then the relay (needs the connect code), then a DHT lookup. PI is a name
from `audiolink pis`, a connect code, or (for a Pi this machine hasn't added) an address.
"""
import base64
import os
import re
import socket
import struct
import threading

from .. import tls
from ..discovery.record import parse_code
from ..services.endpoint import PORT, key_message, proof_for
from ..ui import detail, done, step
from . import keyring
from .pairing import Busy, OldPi, PiConn, busy, impostor, reach, too_old


# ---------------------------------------------------------------- debug keys
def _strings(blob, n):
    out, i = [], 0
    for _ in range(n):
        size = struct.unpack(">I", blob[i:i + 4])[0]
        out.append(blob[i + 4:i + 4 + size])
        i += 4 + size
    return out, blob[i:]


def _agent_sign(pub, msg):
    """Sign with ssh-agent (for passphrase-protected keys). -> signature or None."""
    path = os.environ.get("SSH_AUTH_SOCK")
    if not path or not hasattr(socket, "AF_UNIX"):
        return None
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + pub
    body = b"\x0d" + struct.pack(">I", len(blob)) + blob + struct.pack(">I", len(msg)) + msg + b"\0\0\0\0"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(path)
            s.sendall(struct.pack(">I", len(body)) + body)
            head = s.recv(4)
            size = struct.unpack(">I", head)[0]
            reply = b""
            while len(reply) < size:
                data = s.recv(size - len(reply))
                if not data:
                    return None
                reply += data
    except (OSError, struct.error):
        return None
    if reply[:1] != b"\x0e":
        return None
    (sig_blob,), _ = _strings(reply[1:], 1)
    (kind, sig), _ = _strings(sig_blob, 2)
    return sig if kind == b"ssh-ed25519" and len(sig) == 64 else None


class Key:
    """An ed25519 key from an OpenSSH private key file (unencrypted: signed here;
    passphrase-protected, or a .pub file: signed by ssh-agent)."""

    def __init__(self, path):
        try:
            with open(os.path.expanduser(path)) as f:
                text = f.read()
        except OSError as e:
            raise SystemExit("Can't read %s (%s)" % (path, e.strerror))
        self.path, self.seed = path, None
        if "BEGIN OPENSSH PRIVATE KEY" in text:
            self._private(text)
        else:
            from ..services.access import parse_key
            try:
                self.pub = parse_key(text.splitlines()[0] if text.strip() else "")
            except ValueError as e:
                raise SystemExit("%s: %s" % (path, e))

    def _private(self, text):
        body = "".join(l for l in text.splitlines() if l and not l.startswith("-----"))
        try:
            blob = base64.b64decode(body)
            if not blob.startswith(b"openssh-key-v1\0"):
                raise ValueError
            (cipher, _, _), rest = _strings(blob[15:], 3)
            if struct.unpack(">I", rest[:4])[0] != 1:
                raise ValueError("one key per file")
            (pub_blob, priv), _ = _strings(rest[4:], 2)
            (kind, pub), _ = _strings(pub_blob, 2)
        except (ValueError, TypeError, struct.error):
            raise SystemExit("%s: not an OpenSSH private key" % self.path)
        if kind != b"ssh-ed25519":
            raise SystemExit("%s: not an ed25519 key (make one: ssh-keygen -t ed25519)" % self.path)
        self.pub = pub
        if cipher == b"none":
            try:
                (kind2, pub2, secret), _ = _strings(priv[8:], 3)
            except struct.error:
                raise SystemExit("%s: damaged private key" % self.path)
            if kind2 == b"ssh-ed25519" and pub2 == pub and secret and len(secret) == 64:
                self.seed = secret[:32]

    def sign(self, msg):
        if self.seed:
            from ..crypto import ed25519_sign
            return ed25519_sign(self.seed, msg)
        sig = _agent_sign(self.pub, msg)
        if sig is None:
            raise SystemExit("%s is passphrase-protected: add it to ssh-agent (ssh-add %s)"
                             % (self.path, re.sub(r"\.pub$", "", self.path)))
        return sig


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

