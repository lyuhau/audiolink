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

    def close(self):
        self.sock.close()


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
        raise SystemExit("the Pi sent a malformed pairing message")
    r = conn.ask({"confirm": ours.hex()})
    if "error" in r:
        raise SystemExit(r["error"])
    if not hmac.compare_digest(bytes.fromhex(r.get("confirm", "")), theirs):
        raise SystemExit("the Pi didn't prove it knows the code (someone in the middle?)")
    payload = open_sealed(key, bytes.fromhex(r["payload"]))
    if payload is None:
        raise SystemExit("pairing payload didn't decrypt")
    return json.loads(payload)


def request_code(conn, hello, code):
    """Ask a Pi for a fresh pairing code, proving we know its read secret."""
    _, secret = parse_code(code)
    proof = hmac.new(secret, b"new_code" + bytes.fromhex(hello["nonce"]), hashlib.sha256).hexdigest()
    r = conn.ask({"op": "new_code", "proof": proof})
    if "error" in r:
        raise SystemExit("the Pi refused: %s" % r["error"])
    issued = open_sealed(secret, bytes.fromhex(r["sealed"]))
    if issued is None:
        raise SystemExit("couldn't read the Pi's reply (stale code for it?)")
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
        conn, hello = ask.choose("Pis found on the network:", [
            ("%s [%s] at %s%s" % (h["name"], h["tag"], c.ip, "" if h["claimed"] else "  (unclaimed)"), (c, h))
            for c, h in new])
    try:
        if hello["claimed"]:
            print("%s [%s] at %s is claimed; get a pairing code on its host or any machine that knows it:"
                  % (hello["name"], hello["tag"], conn.ip))
            print("  audiolink pairing code %s" % hello["name"])
            code = (a.pairing_code or ask.text("pairing code: ")).strip().replace(" ", "")
            result = pair(conn, code, client_name=socket.gethostname())
        else:
            if not ask.yes("%s [%s] at %s is unclaimed. Claim it (you'll be its first machine)?"
                           % (hello["name"], hello["tag"], conn.ip), default=True):
                raise SystemExit("not added")
            result = pair(conn, None, client_name=socket.gethostname())
    finally:
        conn.close()
    entry, created = keyring.upsert(result["code"], name=result["name"], alias=a.name)
    print("%s %s over the network" % ("added" if created else "refreshed", entry["alias"]))
    return entry


def code_command(a):
    """`audiolink pairing code PI`: a fresh 6-digit code for pairing a new machine."""
    from ..host.control import DEFAULT_PORT, call
    try:
        entry = keyring.resolve(a.pi)
    except KeyError:
        raise SystemExit("no Pi called %r here; see: audiolink pis" % a.pi)
    pub = keyring.pub_of(entry["code"])
    port = getattr(a, "control_port", None) or DEFAULT_PORT
    issued = None
    try:                                            # plugged in here? ask over USB
        if call("identity.info", port=port)["pub"] == pub:
            issued = call("pairing.new_code", port=port)
    except SystemExit:
        pass
    if issued is None:                              # otherwise over the network
        ips = list(a.at or []) + (entry.get("last_record") or {}).get("lan", []) + browse()
        for ip in dict.fromkeys(ips):
            try:
                conn = PiConn(ip, timeout=1.5)
                hello = conn.ask({"op": "hello"})
            except (OSError, ValueError):
                continue
            try:
                if hello["pub"] == pub:
                    issued = request_code(conn, hello, entry["code"])
                    break
            finally:
                conn.close()
    if issued is None:
        raise SystemExit("couldn't reach %s (not plugged in here, and not found on this network)"
                         % entry["alias"])
    print("pairing code for %s: %s %s  (valid %d min, one use)"
          % (entry["alias"], issued["code"][:3], issued["code"][3:], issued["expires_in"] // 60))
    print("on the new machine: audiolink add")
