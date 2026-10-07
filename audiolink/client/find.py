"""Finding and reaching a Pi from any machine (no daemon needed).

`audiolink find PI`            look the Pi's record up on the DHT and decrypt it
`audiolink connect PI PORT`    stdin/stdout <-> the Pi's port, for an ssh ProxyCommand;
                               tries the last address found before a DHT lookup
PI is a name from `audiolink pis` (alias or the Pi's own name) or a connect code.
`audiolink dht-size`           estimate how many DHT nodes answer
"""
import json
import os
import socket
import sys
import threading
import time

from ..paths import cache_dir
from . import keyring
from ..ui import detail


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
    keyring.update(pub.hex(), last_record=rec, name=rec.get("name"))


def code_for(pi):
    """A connect code for a name from the keyring, or a code given directly."""
    try:
        return keyring.resolve(pi)["code"]
    except KeyError:
        raise SystemExit("You don't have a Pi called %s, and that isn't a connect code either.\n"
                         "To see your Pis: audiolink pis" % pi)


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
        raise SystemExit("Found the Pi's note, but couldn't read it. The code here may be wrong or out of date.")
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
        raise SystemExit("Couldn't find where it is. It needs to be online, with discovery on\n"
                         "(on its host: audiolink discovery status).")
    age = time.time() - rec["ts"]
    print("%s is on %s%s." % (rec["name"], rec.get("ssid") or "a network",
                              " (as of %.0f minutes ago)" % (age / 60) if age >= 120 else ""))
    if rec["ports"]:
        print("Its open ports: %s" % ", ".join(map(str, rec["ports"])))
    detail("addresses %s, lookup %.1fs", ", ".join(rec["lan"]) or "?", took)


def _try_connect(ips, port, timeout=3):
    for ip in ips:
        try:
            return socket.create_connection((ip, port), timeout=timeout)
        except OSError:
            continue
    return None


def connect_command(a):
    """stdin/stdout <-> the Pi's port: for `ssh -o ProxyCommand='audiolink connect PI %p'`."""
    from ..discovery.record import parse_code
    code = code_for(a.pi)
    try:
        pub, _ = parse_code(code)
    except ValueError as e:
        raise SystemExit(str(e))
    sock = None
    cached = _cached(pub)
    if cached:
        sock = _try_connect(cached.get("lan", []), a.port)
    if sock is None:
        sys.stderr.write("audiolink: looking for the Pi...\n")
        rec, _ = lookup(code)
        if not rec:
            raise SystemExit("audiolink: couldn't find where the Pi is (is it online, with discovery on?)")
        sock = _try_connect(rec.get("lan", []), a.port)
        if sock is None:
            raise SystemExit("audiolink: the Pi is on %s, but this computer can't reach it there.\n"
                             "audiolink: connect this computer to %s too (some networks also keep devices apart)."
                             % (rec.get("ssid") or "another network", rec.get("ssid") or "that network"))
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
    print("Counting the BitTorrent DHT, the network audiolink uses to find Pis (about a minute)...",
          flush=True)
    dht = DHT()
    t0 = time.monotonic()
    n, samples = dht.estimate_size(samples=a.samples)
    if not n:
        raise SystemExit("Nobody on the DHT answered. This network may block it (UDP).")
    print("At least %.1f million computers are taking part right now." % (n / 1e6))
    detail("median of %d samples, %.0fs; a lower bound: slow, NAT'd and read-only-ignoring nodes "
           "don't show up", samples, time.monotonic() - t0)
