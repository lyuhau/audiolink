"""Discovery, host and client side.

Host:   `audiolink discovery enable|code|status|publish|disable`; the identity lives in
        the host's config directory and is pushed to the Pi on every link-up
        (HostDiscovery service).
Client: `audiolink find CODE` and `audiolink connect CODE PORT` (an ssh ProxyCommand);
        no daemon needed. The last record found is cached, so repeat connections try
        the known address before waiting on a DHT lookup.
"""
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

from ..services import Service

log = logging.getLogger("audiolink")


def config_dir():
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "audiolink"


IDENTITY = config_dir() / "discovery.json"


def load_identity():
    try:
        return json.loads(IDENTITY.read_text())
    except (OSError, ValueError):
        return None


def save_identity(ident):
    IDENTITY.parent.mkdir(parents=True, exist_ok=True)
    tmp = IDENTITY.with_suffix(".tmp")
    tmp.write_text(json.dumps(ident))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(IDENTITY)


# ---------------------------------------------------------------- host daemon
class HostDiscovery(Service):
    """Pushes the identity (or "disabled") to the Pi whenever the link comes up."""

    def on_peer_hello(self, info):
        if "discovery" in info.get("caps", []):
            threading.Thread(target=self.push, daemon=True).start()

    def push(self):
        ident = load_identity()
        args = dict(ident, time=time.time()) if ident else {}
        try:
            self.session.request("discovery.configure", args, timeout=30)
        except Exception as e:
            log.warning("discovery: couldn't configure the Pi: %s", e)


# ---------------------------------------------------------------- `audiolink discovery ...`
def _usage(code):
    print("connect code (share it with clients; it lets them find and read the Pi's address):")
    print("  " + code)
    print("clients:")
    print("  audiolink find %s" % code)
    print("  ssh -o ProxyCommand='python3 audiolink.pyz connect %s %%p' -p 2222 USER@audiopi" % code)


def discovery_command(a):
    from ..discovery.record import code_for, new_identity
    from .control import call
    from .ui import line_buffered
    line_buffered()
    ident = load_identity()
    if a.action == "enable":
        if ident:
            print("discovery is already enabled")
        else:
            ident = new_identity()
            save_identity(ident)
            print("discovery enabled; identity saved in %s" % IDENTITY)
        _usage(code_for(ident))
        _poke(call, a.control_port, "configure")
    elif a.action == "code":
        if not ident:
            raise SystemExit("discovery isn't enabled; run: audiolink discovery enable")
        _usage(code_for(ident))
    elif a.action == "disable":
        if not ident:
            print("discovery is already disabled")
            return
        IDENTITY.unlink()
        print("discovery disabled; the old code stops working once the Pi's record expires (hours)")
        _poke(call, a.control_port, "configure")
    elif a.action == "publish":
        call("discovery.publish", port=a.control_port)
        print("the Pi will publish within ~30 s; check with: audiolink discovery status")
    else:  # status
        print("identity: %s" % ("enabled (%s)" % IDENTITY if ident else "disabled"))
        try:
            st = call("discovery.status", port=a.control_port)
        except SystemExit as e:
            raise SystemExit("couldn't ask the Pi: %s" % e.code)
        if st.get("published"):
            age = st["now"] - st["published"]
            print("last published %.0f min ago to %d DHT nodes (%.1fs): %s"
                  % (age / 60, st.get("nodes", 0), st.get("took", 0), st.get("record")))
        else:
            print("not published yet%s" % (" (%s)" % st["error"] if st.get("error") else
                                           " (waits for the Pi to be online)"))


def _poke(call, port, op):
    """Tell a running host daemon to (re)configure the Pi; fine if none is running."""
    ident = load_identity()
    try:
        call("discovery.configure", dict(ident, time=time.time()) if ident else {}, port=port)
        print("the running host updated the Pi")
    except SystemExit:
        print("(no running host to update; the Pi gets it when `audiolink host` next connects)")


# ---------------------------------------------------------------- client side
def _cache_file():
    from ..wire.portaudio import cache_dir
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
