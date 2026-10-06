"""Finding and reaching a Pi from any machine (no daemon needed).

`audiolink find CODE`          look the Pi's record up on the DHT and decrypt it
`audiolink connect CODE PORT`  stdin/stdout <-> the Pi's port, for an ssh ProxyCommand;
                               tries the last address found before a DHT lookup
`audiolink dht-size`           estimate how many DHT nodes answer
"""
import json
import os
import socket
import sys
import threading
import time

from ..paths import cache_dir


def _cache_file():
    return cache_dir() / "found.json"


def _cached(pub):
    try:
        return json.loads(_cache_file().read_text()).get(pub.hex())
    except (OSError, ValueError):
        return None


def _remember(pub, rec):
    f = _cache_file()
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        data = {}
    data[pub.hex()] = rec
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data))


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
        raise SystemExit("found a record, but this code can't decrypt it (wrong or old code?)")
    _remember(pub, rec)
    return rec, took


def find_command(a):
    try:
        rec, took = lookup(a.code)
    except ValueError as e:
        raise SystemExit(str(e))
    if not rec:
        raise SystemExit("no record found on the DHT (%.0fs). Is discovery enabled and the Pi online?" % took)
    age = time.time() - rec["ts"]
    print("%s: LAN %s on %r, ports %s (published %s, lookup %.1fs)"
          % (rec["name"], ", ".join(rec["lan"]) or "?", rec.get("ssid"),
             ", ".join(map(str, rec["ports"])) or "none",
             "%.0f min ago" % (age / 60) if age >= 0 else "just now", took))


def _try_connect(ips, port, timeout=3):
    for ip in ips:
        try:
            return socket.create_connection((ip, port), timeout=timeout)
        except OSError:
            continue
    return None


def connect_command(a):
    """stdin/stdout <-> the Pi's port: for `ssh -o ProxyCommand='audiolink connect CODE %p'`."""
    from ..discovery.record import parse_code
    try:
        pub, _ = parse_code(a.code)
    except ValueError as e:
        raise SystemExit(str(e))
    sock = None
    cached = _cached(pub)
    if cached:
        sock = _try_connect(cached.get("lan", []), a.port)
    if sock is None:
        sys.stderr.write("audiolink: looking up the Pi on the DHT...\n")
        rec, _ = lookup(a.code)
        if not rec:
            raise SystemExit("audiolink: the Pi's record wasn't found on the DHT")
        sock = _try_connect(rec.get("lan", []), a.port)
        if sock is None:
            raise SystemExit("audiolink: the Pi is at %s on %r, but port %d isn't reachable from here "
                             "(different network, or devices isolated)"
                             % (", ".join(rec.get("lan", [])), rec.get("ssid"), a.port))
    sock.settimeout(None)
    _pipe(sock)


def _pipe(sock):
    """Bytes between this process's stdin/stdout and the socket, until either side ends."""
    out = sys.stdout.buffer

    def up():
        try:
            while True:
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    break
                sock.sendall(data)
        except OSError:
            pass
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    threading.Thread(target=up, daemon=True).start()
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            out.write(data)
            out.flush()
    except OSError:
        pass


def dht_size_command(a):
    from ..discovery.dht import DHT
    print("sampling the Mainline DHT (about a minute)...", flush=True)
    dht = DHT()
    t0 = time.monotonic()
    n, samples = dht.estimate_size(samples=a.samples)
    if not n:
        raise SystemExit("no DHT nodes answered (UDP blocked on this network?)")
    print("~%.1f million nodes that answer a read-only client (median of %d samples, %.0fs)"
          % (n / 1e6, samples, time.monotonic() - t0))
    print("a lower bound: slow, NAT'd, or ro-ignoring nodes don't show up in this method")
