"""Talking to a Pi's endpoint over the network, client side (docs/protocol.md).

PiConn: TLS pinned to the Pi's key, hello, then auth with this computer's key (once per
connection: the reply has its role and the Pi's record), then requests, or `open` to turn
the connection into a stream to a port on the Pi's host.
Finding Pis: mDNS on the local network, the Pi's setup network (SETUP_ADDR).
Invites: setup without a Pi on USB finds one here and redeems a 6-digit invite code
(from `audiolink invite` on an owner computer), carrying this computer's key in.
"""
import hmac
import json
import re
import select
import socket
import ssl
import struct
import threading
import time

from .. import tls
from ..auth import ENDPOINT_VERSION, auth_message
from ..crypto import Spake2, open_sealed, seal
from . import keyring
from ..ui import detail

# The Pi's side of these (same wire protocol, so the same constants).
from ..services.endpoint import PORT
from ..services.pairing import ID_CLIENT, ID_PI
from ..services.setup_ap import AP_ADDR as SETUP_ADDR      # where the Pi sits on its setup network

SERVICE = b"_audiolink._tcp.local"
HOSTNAME = "audiopi.local"          # the card's hostname setting, by default


# ---------------------------------------------------------------- finding Pis
def _mdns_query(name):
    q = b"".join(bytes([len(p)]) + p for p in name.split(b".")) + b"\0"
    # id 0, no flags, 1 question; type PTR, class IN with the "unicast response" bit
    return struct.pack(">6H", 0, 0, 1, 0, 0, 0) + q + struct.pack(">2H", 12, 0x8001)


def local_ips():
    """This computer's IPv4 addresses, loopback aside: the default route's first."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))         # picks the route; sends nothing
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        ips += [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    return [ip for ip in dict.fromkeys(ips) if not ip.startswith(("127.", "0."))]


def is_answer(data):
    """An mDNS response (not a query, ours included) that names _audiolink._tcp."""
    return len(data) > 2 and data[2] & 0x80 and b"\x0a_audiolink\x04_tcp" in data


def browse(timeout=2.0, resolve=False):
    """IPs of hosts answering for _audiolink._tcp over mDNS (multicast; blocked on
    some networks, which is what --at and the setup network are for). Asked on every
    interface: Windows sends multicast out one only, often a virtual one (Hyper-V, WSL).
    resolve: none answered: try the OS resolver for audiopi.local (finds one Pi at most;
    slow to fail on some systems, so not on every connect)."""
    found = set()
    socks = []
    for ip in local_ips() or [None]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if ip:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            socks.append(s)
        except OSError:
            s.close()
    try:
        for _ in range(2):
            for s in socks:
                try:
                    s.sendto(_mdns_query(SERVICE), ("224.0.0.251", 5353))
                except OSError:                 # an interface without multicast
                    pass
            end = time.monotonic() + timeout / 2
            while socks and time.monotonic() < end:
                ready, _, _ = select.select(socks, [], [], 0.3)
                for s in ready:
                    try:
                        data, addr = s.recvfrom(9000)
                    except OSError:
                        continue
                    if is_answer(data):
                        found.add(addr[0])
    finally:
        for s in socks:
            s.close()
    if resolve and not found:
        try:
            found.update(a[4][0] for a in socket.getaddrinfo(HOSTNAME, None, socket.AF_INET))
        except OSError:
            pass
    return sorted(found)


class Busy(ConnectionError):
    """The Pi turned the connection away: it's serving as many as it will."""

    def __init__(self):
        super().__init__("busy")


def busy(name):
    return SystemExit("%s didn't answer (busy?); retry, or plug it in" % name)


class OldPi(ConnectionError):
    """A Pi whose audiolink predates this protocol (hello: what it said)."""

    def __init__(self, hello):
        super().__init__("too old")
        self.hello = hello


class NotListed(Exception):
    """The Pi doesn't have this computer's key on its list (or refused it)."""


def _plain_hello(ip, port):
    """The hello of an endpoint that didn't take TLS, or None. Raises Busy if it turned
    the connection away."""
    try:
        with socket.create_connection((ip, port), timeout=3) as s:
            s.settimeout(3)
            f = s.makefile("rwb")
            f.write(b'{"op": "hello"}\n')
            f.flush()
            hello = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    if isinstance(hello, dict) and hello.get("code") == "busy":
        raise Busy()
    return hello if isinstance(hello, dict) and hello.get("pub") else None


class PiConn:
    """One line-delimited JSON conversation with a Pi's endpoint, over TLS. pub: the Pi's
    public key from its certificate (hello() checks the Pi claims that one; callers
    compare it with the code they hold). The TLS socket is driven by one thread
    (relay.wire.splice); self.sock is the plain end of a socket pair, which any thread
    may read or write."""

    def __init__(self, ip, port=PORT, timeout=3.0, sock=None):
        from ..relay.wire import splice
        why = tls.unsupported()
        if why:
            raise SystemExit(why)
        self.ip = ip
        raw = sock or socket.create_connection((ip, port), timeout=timeout)
        raw.settimeout(max(timeout, 10))
        try:
            secure, self.pub = tls.client_wrap(raw)
        except tls.Mismatch:
            raise
        except ssl.SSLError:
            raw.close()
            hello = _plain_hello(ip, port) if sock is None else None
            if hello and hello.get("tls"):              # it speaks TLS: the problem is here
                raise SystemExit("Couldn't set up encryption with the Pi (this Python: %s)"
                                 % ssl.OPENSSL_VERSION)
            if hello:
                raise OldPi(hello)
            raise
        self.sock, theirs = socket.socketpair()
        threading.Thread(target=splice, args=(theirs, secure), daemon=True).start()
        self.sock.settimeout(30)
        self.f = self.sock.makefile("rwb")
        self.role = self.record = None

    def ask(self, obj):
        self.f.write((json.dumps(obj) + "\n").encode())
        self.f.flush()
        line = self.f.readline()
        if not line:
            raise ConnectionError("the Pi closed the connection")
        return json.loads(line)

    def hello(self):
        """The Pi's hello, which must name the key its certificate is for. A Pi on an older
        protocol raises OldPi."""
        h = self.ask({"op": "hello"})
        if h.get("pub") != self.pub.hex():
            raise tls.Mismatch(h.get("pub"))
        if not isinstance(h.get("v"), int) or h["v"] < ENDPOINT_VERSION:
            raise OldPi(h)
        self.nonce = bytes.fromhex(h["nonce"])
        return h

    def auth(self, key):
        """Prove this computer's key (once per connection): the reply has its role and
        the Pi's record. Raises NotListed if the Pi refuses it."""
        sig = key.sign(auth_message(self.nonce, self.pub))
        r = self.ask({"op": "auth", "key": key.pub.hex(), "sig": sig.hex()})
        if "error" in r:
            raise NotListed(r["error"])
        self.role, self.record = r.get("role"), r.get("record")
        return r

    def open_stream(self, port):
        """Ask for a stream to `port` on the Pi's host; on success this connection is that
        stream (self.sock), so the reply is read a byte at a time: nothing after it."""
        self.f.flush()
        self.sock.sendall((json.dumps({"op": "open", "port": port}) + "\n").encode())
        line = b""
        while not line.endswith(b"\n"):
            b = self.sock.recv(1)
            if not b:
                raise ConnectionError("the Pi closed the connection")
            line += b
        r = json.loads(line)
        if "error" in r:
            return r
        self.sock.settimeout(None)
        return r

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)        # close() alone waits for self.f
        except OSError:
            pass
        self.sock.close()


def impostor(where, name="the Pi"):
    return SystemExit("Something %s claims to be %s but couldn't prove it (someone may be in between); "
                      "stopped" % (where, name))


def too_old(name):
    return SystemExit("%s's audiolink is too old for this computer's; update it: plug it in here and "
                      "run audiolink update" % name)


def reach(entry, at=(), busy=None):
    """(PiConn, hello) for this known Pi on the network here, or (None, None). Tries --at
    addresses, its last known LAN address, mDNS, and the setup network address.
    Addresses that turned it away go in `busy` (a list)."""
    pub = keyring.pub_of(entry["code"])
    ips = list(at) + (entry.get("last_record") or {}).get("lan", []) + browse(1.0) + [SETUP_ADDR]
    for ip in dict.fromkeys(ips):
        try:
            conn = PiConn(ip, timeout=1.5)
        except OldPi as e:
            if e.hello.get("pub") == pub:
                raise too_old(e.hello.get("name") or "The Pi")
            continue
        except Busy:
            if busy is not None:
                busy.append(ip)
            continue
        except (OSError, ValueError):
            continue
        try:
            hello = conn.hello()
        except tls.Mismatch as e:
            conn.close()
            if e.claimed == pub:
                raise impostor("at %s" % ip, entry.get("alias") or "this Pi")
            continue
        except OldPi as e:
            conn.close()
            if e.hello.get("pub") == pub:
                raise too_old(e.hello.get("name") or "The Pi")
            continue
        except (OSError, ValueError):
            conn.close()
            continue
        if hello.get("pub") == pub:                 # the key in the code: proven by TLS
            return conn, hello
        conn.close()
    return None, None


def candidates(at=()):
    """[(PiConn, hello)] for every Pi reachable over the network right now."""
    out, seen = [], set()
    for ip in list(at) + browse(resolve=True) + [SETUP_ADDR]:
        if ip in seen:
            continue
        seen.add(ip)
        try:
            c = PiConn(ip, timeout=1.5)
        except OldPi as e:
            print("%s (at %s) runs an older audiolink; update it: plug it in and run audiolink update"
                  % (e.hello.get("name", "A Pi"), ip))
            continue
        except Busy:
            print("A Pi at %s didn't answer (busy?); retry, or plug it in" % ip)
            continue
        except (OSError, ValueError):
            continue
        try:
            out.append((c, c.hello()))
        except OldPi as e:
            c.close()
            print("%s (at %s) runs an older audiolink; update it: plug it in and run audiolink update"
                  % (e.hello.get("name", "A Pi"), ip))
        except (OSError, ValueError):               # including a certificate that doesn't match
            c.close()
    return out


# ---------------------------------------------------------------- invites
def pair(conn, code, key, client_name="audiolink"):
    """Redeem an invite: the SPAKE2 exchange with the 6-digit code, carrying this
    computer's key in. Returns {"name", "role", "pub", "record"}; SystemExit with a reason."""
    a = Spake2("A", code, ID_CLIENT, ID_PI)
    r = conn.ask({"op": "pair", "msg": a.msg.hex(), "client": client_name})
    if "error" in r:
        raise SystemExit(_pair_refusal(r))
    try:
        ke, ours, theirs = a.finish(bytes.fromhex(str(r["msg"])))
    except (KeyError, ValueError):
        raise SystemExit("Pairing failed (bad reply from the Pi); retry")
    sealed_key = seal(ke, json.dumps({"key": key.pub.hex()}).encode())
    r = conn.ask({"confirm": ours.hex(), "key": sealed_key.hex()})
    if "error" in r:
        raise SystemExit(_pair_refusal(r))
    try:
        confirm, sealed = bytes.fromhex(str(r.get("confirm", ""))), bytes.fromhex(str(r["payload"]))
    except (KeyError, ValueError):
        confirm, sealed = b"", b""
    if not hmac.compare_digest(confirm, theirs):
        raise SystemExit("Pairing stopped: the other side didn't know the code (wrong Pi, or someone in between)")
    payload = open_sealed(ke, sealed)
    if payload is None:
        raise SystemExit("Pairing failed (unreadable reply); retry")
    result = json.loads(payload)
    if result.get("pub") != conn.pub.hex():
        raise SystemExit("Pairing stopped: the Pi named another key than its certificate's")
    return result


def _pair_refusal(r):
    if r.get("code") == "claim_usb":
        return "That Pi has no owner yet: plug it into a computer and run audiolink setup there to claim it"
    return r["error"]


def invite_code(typed, name):
    """A typed invite code, checked before it costs one of the code's tries."""
    code = re.sub(r"\s|-", "", typed or "")
    if not re.fullmatch(r"\d{6}", code):
        raise SystemExit("An invite code is 6 digits (on an owner computer of %s: audiolink invite NAME)" % name)
    return code


def _again(ip, pub, name):
    """(PiConn, hello) to the Pi with key pub at ip again (after a prompt: the Pi drops
    connections that wait)."""
    lost = SystemExit("Lost %s (at %s); retry" % (name, ip))
    try:
        conn = PiConn(ip, timeout=3)
    except Busy:
        raise busy(name)
    except (OSError, ValueError):
        raise lost
    try:
        hello = conn.hello()
    except tls.Mismatch:
        conn.close()
        raise impostor("at %s" % ip, name)
    except OldPi as e:
        conn.close()
        raise too_old(e.hello.get("name") or name)
    except (OSError, ValueError):
        conn.close()
        raise lost
    if hello.get("pub") != pub:
        conn.close()
        raise impostor("at %s" % ip, name)
    return conn, hello


def _listed(conn, key):
    """Is this computer's key on the Pi's list?"""
    try:
        conn.auth(key)
        return True
    except NotListed:
        return False
    except (OSError, ValueError):
        return False


def add_over_network(a, ask, key=None):
    """Redeem an invite to a Pi found on this network (setup without a Pi on USB).
    Returns the keyring entry or None (no Pi here that doesn't have this computer already)."""
    from .computer import key as computer_key, this_computer
    key = key or computer_key()
    known = keyring.load()
    found = candidates(a.at or [])
    new, seen = [], set()
    given = getattr(a, "invite", None) is not None     # an invite for a Pi already joined: e.g. to owner
    try:
        for c, h in found:
            if h["pub"] not in seen and (given or h["pub"] not in known or not _listed(c, key)):
                new.append((c.ip, h))
            seen.add(h["pub"])
    finally:
        for c, _ in found:              # none kept open across a prompt
            c.close()
    if not new:
        return None
    if len(new) == 1:
        ip, hello = new[0]
    else:
        ip, hello = ask.choose("Which Pi?", [
            ("%s%s" % (h["name"], "" if h["claimed"] else "  (no owner yet)"), (ip, h)) for ip, h in new])
    name = hello["name"]
    detail("%s, tag %s, at %s", name, hello["tag"], ip)
    if not hello["claimed"]:
        raise SystemExit("Found %s, which has no owner yet: plug it into a computer and run audiolink setup "
                         "there to claim it" % name)
    typed = getattr(a, "invite", None)
    if typed is not None:
        print("Found %s: joining with the invite code given" % name)
    else:
        print("Found %s. Get an invite code on an owner computer: audiolink invite %s"
              % (name, this_computer()))
    code = invite_code(typed if typed is not None else ask.text("Invite code: "), name)
    conn, hello = _again(ip, hello["pub"], name)
    try:
        result = pair(conn, code, key, client_name=socket.gethostname())
    except (OSError, ValueError):
        raise SystemExit("Lost %s (at %s) while joining; retry" % (name, ip))
    finally:
        conn.close()
    return _joined(result, a)


def add_through_relay(pi_code, a, ask, key=None):
    """Redeem an invite to a Pi that isn't on this network, through its relay: the Pi's
    code (al2-...) says which; the invite code says this computer may join."""
    from .computer import key as computer_key
    from .remote import over_relay, through_relay
    key = key or computer_key()
    pub = keyring.parse(pi_code)
    typed = getattr(a, "invite", None)
    code = invite_code(typed if typed is not None else ask.text("Invite code: "), "the Pi")
    sock, why = over_relay(pub)                 # after the prompt: the Pi drops connections that wait
    found = through_relay(sock, pub, "the Pi") if sock is not None else None
    if not found:
        raise SystemExit("The Pi isn't reachable through its relay (%s)" % (why or "no answer"))
    conn = found[0]
    try:
        result = pair(conn, code, key, client_name=socket.gethostname())
    except (OSError, ValueError):
        raise SystemExit("Lost the Pi while joining; retry")
    finally:
        conn.close()
    return _joined(result, a)


def _joined(result, a):
    entry, created = keyring.upsert(keyring.code_for(result["pub"]), name=result["name"], alias=a.name,
                                    seen=True)
    fields = {"role": result.get("role")}
    rec = result.get("record")
    if isinstance(rec, dict) and rec.get("ts"):
        fields["last_record"] = rec
    from .remote import learn_usb_ids
    learn_usb_ids(rec)
    keyring.update(result["pub"], **fields)
    entry.update(fields)
    on_list = result.get("as")          # this computer's name on the Pi's list (maybe numbered: laptop-2)
    print("%s %s (%s%s)" % ("Added" if created else "Updated", entry["alias"], result.get("role") or "user",
                            ", on its list as %s" % on_list if on_list else ""))
    return entry
