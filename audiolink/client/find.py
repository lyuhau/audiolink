"""Finding and reaching a Pi from any machine (no daemon needed).

`audiolink find PI`            is it online (asks its relay), and where (its DHT record)
`audiolink connect PI PORT`    stdin/stdout <-> the Pi's port, for an ssh ProxyCommand;
                               tries the last address found, the relay, then a DHT lookup
PI is a name from `audiolink pis` (alias or the Pi's own name) or a connect code.
`audiolink dht-size`           estimate how many DHT nodes answer

Records don't name a relay: every Pi and client uses the default one (relay/__init__.py).
"""
import os
import re
import socket
import sys
import threading
import time

from ..paths import cache_dir, load_json, save_json
from . import keyring
from .. import ui
from ..ui import ago, detail, done, step

OUTDATED = "This computer's code for it is out of date (the Pi has a new one); add it again: audiolink add"


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
    from ..discovery.record import SALT, read_record
    pub, secret = keyring.parse(code)
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


def _relay_state(pub, secret):
    """Is the Pi on its relay? "online", "offline" (the relay says it isn't there), or
    None (no answer from the relay)."""
    from ..relay.client import Denied, connect
    from ..relay.wire import Refused, close
    from ..services.endpoint import PORT
    try:
        close(connect(pub, secret, PORT))
    except Denied as e:
        if "not paired" in str(e):
            raise SystemExit(OUTDATED)
        detail("the Pi said: %s", e)            # it answered, so it's there
    except Refused as e:
        detail("the relay: %s", e)
        return "offline" if str(e) == "offline" else None
    return "online"


def find_command(a):
    from ..services.endpoint import PORT
    entry = keyring.lookup(a.pi)
    pub, secret = keyring.parse(entry["code"])
    name = entry.get("alias") or "The Pi"
    step("Asking its relay")
    state = _relay_state(pub, secret)
    done({"online": "online", "offline": "offline"}.get(state, "no answer"))
    if state == "online":
        keyring.seen(pub.hex())
    step("Looking it up on the DHT")
    rec, took = lookup(entry["code"])
    done("found" if rec else "not found")
    detail("lookup %.1fs", took)
    if not rec:
        if state == "online":
            print("%s: online, through its relay (not published on the DHT: discovery is off)" % name)
            return
        raise SystemExit("%s: %s, and no record on the DHT (it publishes one while online with discovery on; "
                         "ask its owner)" % (name, "offline" if state == "offline" else "not on its relay"))
    name = entry.get("alias") or rec["name"]
    keyring.seen(pub.hex(), rec["ts"])
    near = _try_connect(rec.get("lan", []), PORT, timeout=1)
    if near:
        near.close()
    print("%s: %s" % (name, "online, on this network" if near else {
        "online": "online, through its relay",
        "offline": "offline (not on its relay)"}.get(state, "its relay didn't answer")))
    print("Last record: on %s, %s" % (rec.get("ssid") or "an unknown network", ago(rec["ts"])))
    print("Ports: %s" % (", ".join(map(str, rec["ports"])) or "none"))
    detail("addresses %s", ", ".join(rec["lan"]) or "?")


def _try_connect(ips, port, timeout=3):
    for ip in ips:
        try:
            return socket.create_connection((ip, port), timeout=timeout)
        except OSError:
            continue
    return None


def _via_relay(pub, secret, port, name):
    """(socket through the relay, None), or (None, the relay's reason). Ends the command
    if the Pi itself said no."""
    from ..relay.client import Denied, connect
    from ..relay.wire import Refused
    try:
        return connect(pub, secret, port), None
    except Denied as e:
        closed = re.match(r"port (\d+) isn't open", str(e))
        if closed:
            raise SystemExit("%s isn't offering port %s (on its host: audiolink host --expose %s:22)"
                             % (name, closed.group(1), closed.group(1)))
        raise SystemExit(OUTDATED if "not paired" in str(e) else str(e))
    except Refused as e:
        return None, str(e)


def connect_command(a):
    """stdin/stdout <-> the Pi's port: for `ssh -o ProxyCommand='audiolink connect PI %p'`.
    stdout is the connection, so everything said goes to stderr, as "audiolink: ..."."""
    try:
        sock = _connect(a)
    except SystemExit as e:
        if isinstance(e.code, str) and not e.code.startswith("audiolink:"):
            raise SystemExit("audiolink: " + e.code)
        raise
    sock.settimeout(None)
    _pipe(sock)


def _connect(a):
    """The Pi's last known address, then its relay, then (unless the relay says it's
    offline: an online Pi keeps a connection to its relay) a DHT lookup."""
    entry = keyring.lookup(a.pi)
    pub, secret = keyring.parse(entry["code"])
    name = entry.get("alias") or "the Pi"
    cached = _cached(pub) or entry.get("last_record") or {}
    sock = _try_connect(cached.get("lan", []), a.port, timeout=1.5)
    if sock is None:
        sock, why = _via_relay(pub, secret, a.port, name)
        if sock is None and why == "offline":
            last = "; last record %s on %s" % (ago(cached["ts"]), cached.get("ssid") or "an unknown network") \
                if cached.get("ts") else ""
            raise SystemExit("%s is offline (not on its relay%s)" % (name, last))
        if sock is not None and ui.VERBOSE:     # stdout is the connection: say it on stderr
            sys.stderr.write("audiolink: through the relay\n")
    if sock is None:
        sys.stderr.write("audiolink: looking up %s...\n" % name)
        rec, _ = lookup(entry["code"])
        if not rec:
            raise SystemExit("%s not found (not on its relay, nor on the DHT)" % name)
        sock = _try_connect(rec.get("lan", []), a.port)
        if sock is None:
            raise SystemExit("%s is on %s, unreachable from here (connect to %s too)"
                             % (name, rec.get("ssid") or "another network", rec.get("ssid") or "that network"))
    keyring.seen(pub.hex())
    return sock


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
    step("Sampling the BitTorrent DHT (%d lookups, up to %d s)" % (a.samples, a.samples * 10))
    dht = DHT()
    t0 = time.monotonic()
    n, estimates = dht.estimate_size(samples=a.samples)
    if not n:
        done("no answers")
        raise SystemExit("DHT unreachable (UDP blocked on this network?)")
    done("done")
    print("At least %.1f million nodes" % (n / 1e6))
    detail("median of %d estimates from %d lookups, %.0fs; a lower bound: slow, NAT'd and read-only "
           "nodes don't show up", estimates, a.samples, time.monotonic() - t0)
