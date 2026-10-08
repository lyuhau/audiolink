"""Finding and reaching a Pi from any machine (no daemon needed).

`audiolink find PI`            look the Pi's record up on the DHT and decrypt it
`audiolink connect PI PORT`    stdin/stdout <-> the Pi's port, for an ssh ProxyCommand;
                               tries the last address found, the relay, then a DHT lookup
PI is a name from `audiolink pis` (alias or the Pi's own name) or a connect code.
`audiolink dht-size`           estimate how many DHT nodes answer
"""
import os
import socket
import sys
import threading
import time

from ..paths import cache_dir, load_json, save_json
from . import keyring
from .. import ui
from ..ui import detail, done, step


def _cache_file():
    return cache_dir() / "found.json"


def _cached(pub):
    return load_json(_cache_file(), {}).get(pub.hex())


def _remember(pub, rec):
    data = load_json(_cache_file(), {})
    data[pub.hex()] = rec
    save_json(_cache_file(), data)
    keyring.update(pub.hex(), last_record=rec, name=rec.get("name"))


def code_for(pi):
    """A connect code for a name from the keyring, or a code given directly."""
    return keyring.lookup(pi)["code"]


def lookup(code):
    """Fetch and decrypt the Pi's current record: (record, seconds taken)."""
    from ..discovery.dht import DHT
    from ..discovery.record import SALT, parse_code, read_record
    pub, secret = parse_code(code)
    t0 = time.monotonic()
    dht = DHT()
    try:
        got = dht.get(pub, SALT)
        if not got:                     # unlucky first walk; the client is warm now
            got = dht.get(pub, SALT)
    finally:
        dht.close()
    took = time.monotonic() - t0
    if not got:
        return None, took
    rec = read_record(secret, got[1])
    if rec is None:
        raise SystemExit("Found its record, but can't read it (wrong or outdated code?)")
    _remember(pub, rec)
    return rec, took


def find_command(a):
    code = code_for(a.pi)
    try:
        rec, took = lookup(code)
    except ValueError as e:
        raise SystemExit(str(e))
    if not rec:
        detail("no record on the DHT after %.0fs", took)
        raise SystemExit("Not found (needs to be online with discovery on; see: audiolink discovery status)")
    age = time.time() - rec["ts"]
    print("%s: on %s%s" % (rec["name"], rec.get("ssid") or "unknown network",
                           " (%.0f min ago)" % (age / 60) if age >= 120 else ""))
    if rec["ports"]:
        print("Ports: %s" % ", ".join(map(str, rec["ports"])))
    detail("addresses %s, lookup %.1fs", ", ".join(rec["lan"]) or "?", took)


def _try_connect(ips, port, timeout=3):
    for ip in ips:
        try:
            return socket.create_connection((ip, port), timeout=timeout)
        except OSError:
            continue
    return None


def _via_relay(pub, secret, port, relays):
    """A socket through a relay, or None. Exits if the Pi itself said no."""
    from ..relay.client import Denied, connect
    from ..relay.wire import Refused
    try:
        return connect(pub, secret, port, relays)
    except Denied as e:
        raise SystemExit("audiolink: %s" % e)
    except Refused:
        return None


def connect_command(a):
    """stdin/stdout <-> the Pi's port: for `ssh -o ProxyCommand='audiolink connect PI %p'`.
    Tries the Pi's last known address, then its relay, then a DHT lookup."""
    from ..discovery.record import parse_code
    code = code_for(a.pi)
    try:
        pub, secret = parse_code(code)
    except ValueError as e:
        raise SystemExit(str(e))
    cached = _cached(pub) or {}
    sock = _try_connect(cached.get("lan", []), a.port, timeout=1.5)
    if sock is None:
        sock = _via_relay(pub, secret, a.port, cached.get("relays", []))
        if sock is not None and ui.VERBOSE:     # stdout is the connection: say it on stderr
            sys.stderr.write("audiolink: through the relay\n")
    if sock is None:
        sys.stderr.write("audiolink: looking up the Pi...\n")
        rec, _ = lookup(code)
        if not rec:
            raise SystemExit("audiolink: Pi not found (not on its relay, nor on the DHT)")
        sock = _try_connect(rec.get("lan", []), a.port)
        if sock is None and set(rec.get("relays", [])) - set(cached.get("relays", [])):
            sock = _via_relay(pub, secret, a.port, rec["relays"])
        if sock is None:
            raise SystemExit("audiolink: Pi is on %s, unreachable from here (connect to %s too)"
                             % (rec.get("ssid") or "another network", rec.get("ssid") or "that network"))
    sock.settimeout(None)
    _pipe(sock)


def _pipe(sock):
    """Bytes between this process's stdin/stdout and the socket, until the socket's side
    ends. The socket may be TLS, which only one thread may drive: stdin/stdout reach it
    through a socket pair, and splice() drives both in one loop."""
    from ..relay.wire import splice
    mine, theirs = socket.socketpair()
    out = sys.stdout.buffer

    def up():
        try:
            while True:
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    break
                mine.sendall(data)
        except OSError:
            pass
        try:
            mine.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    threading.Thread(target=up, daemon=True).start()
    threading.Thread(target=splice, args=(theirs, sock), daemon=True).start()
    try:
        while True:
            data = mine.recv(65536)
            if not data:
                break
            out.write(data)
            out.flush()
    except OSError:
        pass


def dht_size_command(a):
    from ..discovery.dht import DHT
    step("Sampling the BitTorrent DHT (about a minute)")
    dht = DHT()
    t0 = time.monotonic()
    n, samples = dht.estimate_size(samples=a.samples)
    if not n:
        done("no answers")
        raise SystemExit("DHT unreachable (UDP blocked on this network?)")
    done("done")
    print("At least %.1f million nodes" % (n / 1e6))
    detail("median of %d samples, %.0fs; a lower bound: slow, NAT'd and read-only-ignoring nodes "
           "don't show up", samples, time.monotonic() - t0)
