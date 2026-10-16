"""Pairing with a Pi over the network, client side (see services/pairing.py).

`audiolink add` (network part): find Pis on the local network (mDNS) or on the Pi's
own setup network (SETUP_ADDR), then pair: an unclaimed Pi is claimed on the spot,
a claimed one asks for a fresh 6-digit code from its host or a paired machine.
`audiolink pairing code PI`: issue that code (over USB if the Pi is plugged in here,
otherwise over the network, proving this machine already knows the Pi).
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
from ..crypto import Spake2, open_sealed
from ..discovery.record import parse_code
from . import keyring
from ..ui import detail

# The Pi's side of these (same wire protocol, so the same constants).
from ..services.endpoint import PORT, proof_for
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
    """A Pi whose audiolink predates TLS on its endpoint (hello: what it said)."""

    def __init__(self, hello):
        super().__init__("too old")
        self.hello = hello


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

    def ask(self, obj):
        self.f.write((json.dumps(obj) + "\n").encode())
        self.f.flush()
        line = self.f.readline()
        if not line:
            raise ConnectionError("the Pi closed the connection")
        return json.loads(line)

    def hello(self):
        """The Pi's hello, which must name the key its certificate is for."""
        h = self.ask({"op": "hello"})
        if h.get("pub") != self.pub.hex():
            raise tls.Mismatch(h.get("pub"))
        return h

    def ask_auth(self, op, hello, secret, sealed=None, **fields):
        """An authenticated request: proves this machine knows the Pi's read secret;
        `sealed` (a dict) travels encrypted with it."""
        from ..crypto import seal
        proof = proof_for(secret, op, bytes.fromhex(hello["nonce"]), self.pub)
        req = dict(fields, op=op, proof=proof)
        if sealed is not None:
            req["sealed"] = seal(secret, json.dumps(sealed).encode()).hex()
        return self.ask(req)

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
    return SystemExit("%s's audiolink is too old to reach over the network; update it over USB "
                      "(audiolink update)" % name)


def reach(entry, at=(), with_dht=False, busy=None):
    """(PiConn, hello) for this known Pi over the network, or (None, None). Tries
    --at addresses, its last known LAN address, mDNS, the setup network address, and
    (with_dht) a fresh DHT lookup. Addresses that turned it away go in `busy` (a list)."""
    pub = keyring.pub_of(entry["code"])
    ips = list(at) + (entry.get("last_record") or {}).get("lan", []) + browse(1.0) + [SETUP_ADDR]
    if with_dht:
        from .find import lookup
        try:
            rec, _ = lookup(entry["code"])
            ips = (rec or {}).get("lan", []) + ips
        except SystemExit:
            pass
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
            print("%s (at %s) runs an audiolink too old to pair over the network; update it over USB"
                  % (e.hello.get("name", "A Pi"), ip))
            continue
        except Busy:
            print("A Pi at %s didn't answer (busy?); retry, or plug it in" % ip)
            continue
        except (OSError, ValueError):
            continue
        try:
            out.append((c, c.hello()))
        except (OSError, ValueError):               # including a certificate that doesn't match
            c.close()
    return out


# ---------------------------------------------------------------- pairing
def pair(conn, code=None, client_name="audiolink"):
    """Run the exchange; returns {"code", "name"}. Raises SystemExit with a reason."""
    a = Spake2("A", code or "", ID_CLIENT, ID_PI)
    r = conn.ask({"op": "pair", "msg": a.msg.hex(), "client": client_name})
    if "error" in r:
        raise SystemExit(r["error"])
    try:
        key, ours, theirs = a.finish(bytes.fromhex(str(r["msg"])))
    except (KeyError, ValueError):
        raise SystemExit("Pairing failed (bad reply from the Pi); retry")
    r = conn.ask({"confirm": ours.hex()})
    if "error" in r:
        raise SystemExit(r["error"])
    try:
        confirm, sealed = bytes.fromhex(str(r.get("confirm", ""))), bytes.fromhex(str(r["payload"]))
    except (KeyError, ValueError):
        confirm, sealed = b"", b""
    if not hmac.compare_digest(confirm, theirs):
        raise SystemExit("Pairing stopped: the other side didn't know the code (wrong Pi, or someone in between)")
    payload = open_sealed(key, sealed)
    if payload is None:
        raise SystemExit("Pairing failed (unreadable reply); retry")
    return json.loads(payload)


def request_code(conn, hello, code):
    """Ask a Pi for a fresh pairing code, proving we know its read secret."""
    _, secret = parse_code(code)
    r = conn.ask_auth("new_code", hello, secret)
    if "error" in r:
        raise SystemExit("Refused: %s" % r["error"])
    issued = open_sealed(secret, bytes.fromhex(r["sealed"]))
    if issued is None:
        raise SystemExit("Unreadable reply (this computer's code for it may be outdated)")
    return json.loads(issued)


def _outdated(conn, hello, entry):
    """Does the Pi refuse this machine's code for it (its secret was rotated)?"""
    if not hello.get("claimed"):
        return True
    try:
        r = conn.ask_auth("change_status", hello, parse_code(entry["code"])[1])
    except (OSError, ValueError):
        return False
    return r.get("code") == "not_paired" or str(r.get("error", "")).startswith("Not added")


# ---------------------------------------------------------------- commands
def pairing_code(typed, name):
    """A typed pairing code, checked before it costs one of the code's tries."""
    code = re.sub(r"\s", "", typed or "")
    if not re.fullmatch(r"\d{6}", code):
        raise SystemExit("A pairing code is 6 digits (on a computer that has %s: audiolink pairing code %s)"
                         % (name, name))
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
    except (OSError, ValueError):
        conn.close()
        raise lost
    if hello.get("pub") != pub:
        conn.close()
        raise impostor("at %s" % ip, name)
    return conn, hello


def add_over_network(a, ask):
    """The network part of `audiolink add`. Returns the keyring entry or None."""
    known = keyring.load()
    found = candidates(a.at or [])
    new, seen = [], set()
    try:
        for c, h in found:
            if h["pub"] not in seen and (h["pub"] not in known or _outdated(c, h, known[h["pub"]])):
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
        ip, hello = ask.choose("Add which Pi?", [
            ("%s%s" % (h["name"], "" if h["claimed"] else "  (unclaimed)"), (ip, h)) for ip, h in new])
    name = hello["name"]
    detail("%s, tag %s, at %s", name, hello["tag"], ip)
    code = None
    if hello["claimed"]:
        if hello["pub"] in known:
            print("%s's code has changed; this computer needs a pairing code. Get one on a computer "
                  "that has it: audiolink pairing code %s" % (name, name))
        else:
            print("Found %s (claimed). Get a code on a computer that knows it: audiolink pairing code %s"
                  % (name, name))
        code = pairing_code(a.pairing_code if a.pairing_code is not None else ask.text("Pairing code: "), name)
    elif not ask.yes("Found %s (unclaimed). Claim it?" % name, default=True):
        raise SystemExit("Not added")
    conn, hello = _again(ip, hello["pub"], name)
    try:
        if hello["claimed"] and code is None:
            raise SystemExit("%s was just claimed by another computer; get a pairing code from it: "
                             "audiolink pairing code %s" % (name, name))
        result = pair(conn, code if hello["claimed"] else None, client_name=socket.gethostname())
    except (OSError, ValueError):
        raise SystemExit("Lost %s (at %s) while pairing; retry" % (name, ip))
    finally:
        conn.close()
    entry, created = keyring.upsert(result["code"], name=result["name"], alias=a.name, seen=True)
    rec = result.get("record")
    if isinstance(rec, dict) and rec.get("ts"):     # where it is now (older Pis don't say)
        keyring.update(keyring.pub_of(result["code"]), last_record=rec)
        entry["last_record"] = rec
    print("%s %s" % ("Added" if created else "Updated", entry["alias"]))
    return entry


def code_command(a):
    """`audiolink pairing code PI`: a fresh 6-digit code for pairing a new machine."""
    from ..host.control import DEFAULT_PORT, call
    entry = keyring.lookup(a.pi, known=True)
    pub = keyring.pub_of(entry["code"])
    port = getattr(a, "control_port", None) or DEFAULT_PORT
    issued = None
    try:                                            # plugged in here? ask over USB
        if call("identity.info", port=port)["pub"] == pub:
            issued = call("pairing.new_code", port=port)
    except SystemExit:
        pass
    turned_away = []
    if issued is None:                              # otherwise over the network
        conn, hello = reach(entry, a.at or [], busy=turned_away)
        if conn:
            try:
                issued = request_code(conn, hello, entry["code"])
            finally:
                conn.close()
    if issued is None and turned_away:
        raise busy(entry["alias"])
    if issued is None:
        raise SystemExit("%s not found (not plugged in here or on this network)" % entry["alias"])
    if not issued.get("code"):                      # over USB, from a Pi nobody has claimed
        print("%s is unclaimed: no code needed. On the new computer: audiolink add" % entry["alias"])
        return
    print("Pairing code for %s: %s %s  (one use, %d min)" % (entry["alias"], issued["code"][:3], issued["code"][3:],
                                                               issued["expires_in"] // 60))
    print("On the new computer: audiolink add")
