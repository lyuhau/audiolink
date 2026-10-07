"""Pairing with a Pi over the network, client side (see services/pairing.py).

`audiolink add` (network part): find Pis on the local network (mDNS) or on the Pi's
own setup network (SETUP_ADDR), then pair: an unclaimed Pi is claimed on the spot,
a claimed one asks for a fresh 6-digit code from its host or a paired machine.
`audiolink pairing code PI`: issue that code (over USB if the Pi is plugged in here,
otherwise over the network, proving this machine already knows the Pi).
"""
import hashlib
import hmac
import json
import socket
import struct
import time

from ..crypto import Spake2, open_sealed
from ..discovery.record import parse_code
from . import keyring
from ..ui import detail

PORT = 47900
SETUP_ADDR = "192.168.4.1"          # where the Pi sits on its own setup network
SERVICE = b"_audiolink._tcp.local"
ID_CLIENT, ID_PI = b"audiolink client", b"audiolink pi"


# ---------------------------------------------------------------- finding Pis
def _mdns_query(name):
    q = b"".join(bytes([len(p)]) + p for p in name.split(b".")) + b"\0"
    # id 0, no flags, 1 question; type PTR, class IN with the "unicast response" bit
    return struct.pack(">6H", 0, 0, 1, 0, 0, 0) + q + struct.pack(">2H", 12, 0x8001)


def browse(timeout=2.0):
    """IPs of hosts answering for _audiolink._tcp over mDNS (multicast; blocked on
    some networks, which is what --at and the setup network are for)."""
    found = set()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.3)
        for _ in range(2):
            s.sendto(_mdns_query(SERVICE), ("224.0.0.251", 5353))
            end = time.monotonic() + timeout / 2
            while time.monotonic() < end:
                try:
                    data, addr = s.recvfrom(9000)
                except socket.timeout:
                    continue
                if b"\x0a_audiolink\x04_tcp" in data:
                    found.add(addr[0])
    except OSError:
        pass
    finally:
        s.close()
    return sorted(found)


class PiConn:
    """One line-delimited JSON conversation with a Pi's pairing endpoint."""

    def __init__(self, ip, port=PORT, timeout=3.0):
        self.ip = ip
        self.sock = socket.create_connection((ip, port), timeout=timeout)
        self.sock.settimeout(30)
        self.f = self.sock.makefile("rwb")

    def ask(self, obj):
        self.f.write((json.dumps(obj) + "\n").encode())
        self.f.flush()
        line = self.f.readline()
        if not line:
            raise ConnectionError("the Pi closed the connection")
        return json.loads(line)

    def ask_auth(self, op, hello, secret, sealed=None, **fields):
        """An authenticated request: proves this machine knows the Pi's read secret;
        `sealed` (a dict) travels encrypted with it."""
        from ..crypto import seal
        proof = hmac.new(secret, op.encode() + bytes.fromhex(hello["nonce"]), hashlib.sha256).hexdigest()
        req = dict(fields, op=op, proof=proof)
        if sealed is not None:
            req["sealed"] = seal(secret, json.dumps(sealed).encode()).hex()
        return self.ask(req)

    def close(self):
        self.sock.close()


def reach(entry, at=(), with_dht=False):
    """(PiConn, hello) for this known Pi over the network, or (None, None). Tries
    --at addresses, its last known LAN address, mDNS, the setup network address, and
    (with_dht) a fresh DHT lookup."""
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
            hello = conn.ask({"op": "hello"})
        except (OSError, ValueError):
            continue
        if hello.get("pub") == pub:
            return conn, hello
        conn.close()
    return None, None


def candidates(at=()):
    """[(PiConn, hello)] for every Pi reachable over the network right now."""
    out, seen = [], set()
    for ip in list(at) + browse() + [SETUP_ADDR]:
        if ip in seen:
            continue
        seen.add(ip)
        try:
            c = PiConn(ip, timeout=1.5)
            out.append((c, c.ask({"op": "hello"})))
        except (OSError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- pairing
def pair(conn, code=None, client_name="audiolink"):
    """Run the exchange; returns {"code", "name"}. Raises SystemExit with a reason."""
    a = Spake2("A", code or "", ID_CLIENT, ID_PI)
    r = conn.ask({"op": "pair", "msg": a.msg.hex(), "client": client_name})
    if "error" in r:
        raise SystemExit(r["error"])
    try:
        key, ours, theirs = a.finish(bytes.fromhex(r["msg"]))
    except ValueError:
        raise SystemExit("Pairing failed: the Pi's answer didn't make sense. Try again.")
    r = conn.ask({"confirm": ours.hex()})
    if "error" in r:
        raise SystemExit(r["error"])
    if not hmac.compare_digest(bytes.fromhex(r.get("confirm", "")), theirs):
        raise SystemExit("Pairing stopped: the device that answered couldn't prove it's the Pi you meant\n"
                         "(it didn't know the code). Make sure you're adding the right Pi, and try again.")
    payload = open_sealed(key, bytes.fromhex(r["payload"]))
    if payload is None:
        raise SystemExit("Pairing failed: the Pi's reply couldn't be read. Try again.")
    return json.loads(payload)


def request_code(conn, hello, code):
    """Ask a Pi for a fresh pairing code, proving we know its read secret."""
    _, secret = parse_code(code)
    r = conn.ask_auth("new_code", hello, secret)
    if "error" in r:
        raise SystemExit("The Pi said no: %s" % r["error"])
    issued = open_sealed(secret, bytes.fromhex(r["sealed"]))
    if issued is None:
        raise SystemExit("Couldn't read the Pi's reply. Its code on this computer may be out of date.")
    return json.loads(issued)


# ---------------------------------------------------------------- commands
def add_over_network(a, ask):
    """The network part of `audiolink add`. Returns the keyring entry or None."""
    known = keyring.load()
    found = [(c, h) for c, h in candidates(a.at or [])]
    new = [(c, h) for c, h in found if h["pub"] not in known]
    for c, h in found:
        if h["pub"] in known:
            c.close()
    if not new:
        return None
    if len(new) == 1:
        conn, hello = new[0]
    else:
        conn, hello = ask.choose("Found these Pis on the network. Which one?", [
            ("%s%s" % (h["name"], "" if h["claimed"] else "  (new: nobody has added it yet)"), (c, h))
            for c, h in new])
    try:
        detail("%s, tag %s, at %s", hello["name"], hello["tag"], conn.ip)
        if hello["claimed"]:
            print("Found %s. It already belongs to someone, so you need a pairing code from a computer" % hello["name"])
            print("that knows it. On that computer, run:  audiolink pairing code %s" % hello["name"])
            code = (a.pairing_code or ask.text("Pairing code: ")).strip().replace(" ", "")
            result = pair(conn, code, client_name=socket.gethostname())
        else:
            if not ask.yes("Found %s, which nobody has added yet. Add it? (This computer becomes its first.)"
                           % hello["name"], default=True):
                raise SystemExit("Not added.")
            result = pair(conn, None, client_name=socket.gethostname())
    finally:
        conn.close()
    entry, created = keyring.upsert(result["code"], name=result["name"], alias=a.name)
    print(("Added %s." if created else "%s was already added; updated its details.") % entry["alias"])
    return entry


def code_command(a):
    """`audiolink pairing code PI`: a fresh 6-digit code for pairing a new machine."""
    from ..host.control import DEFAULT_PORT, call
    try:
        entry = keyring.resolve(a.pi)
    except KeyError:
        raise SystemExit("You don't have a Pi called %s. To see yours: audiolink pis" % a.pi)
    pub = keyring.pub_of(entry["code"])
    port = getattr(a, "control_port", None) or DEFAULT_PORT
    issued = None
    try:                                            # plugged in here? ask over USB
        if call("identity.info", port=port)["pub"] == pub:
            issued = call("pairing.new_code", port=port)
    except SystemExit:
        pass
    if issued is None:                              # otherwise over the network
        conn, hello = reach(entry, a.at or [])
        if conn:
            try:
                issued = request_code(conn, hello, entry["code"])
            finally:
                conn.close()
    if issued is None:
        raise SystemExit("Can't reach %s: it isn't plugged in here or on this network." % entry["alias"])
    print("Pairing code for %s:  %s %s" % (entry["alias"], issued["code"][:3], issued["code"][3:]))
    print("On the new computer, run `audiolink add` and type this code. It works once, within %d minutes."
          % (issued["expires_in"] // 60))
