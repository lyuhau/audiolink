"""Pi side of the relay (audiolink/relay): stay registered, answer clients through it.

While the Pi is online it keeps one connection to its relay, registered under its
public key. When a client asks for it, the relay passes on an "open"; the Pi dials a
data connection and joins it to its own endpoint (services/endpoint.py), where the
client does what it does on the LAN: TLS pinned to the Pi's key, hello, auth, then its
requests or `open` for a stream. Nothing else on the Pi is reachable this way.

Connections to the endpoint come from RELAY_SOURCE (127.0.0.2), so the kernel's table
shows which ones came through the relay: presence counts them as clients, and the
endpoint knows their route (no Wi-Fi changes through the relay: they'd cut the
connection they came on).

Opens cost the Pi a connection and a TLS handshake before it knows who's asking, so
they're limited: per client address (the relay names it), and all of them together
(OPENS_PER_MIN), besides SESSIONS at once.

Relay state is its own fact ("relay": up/down), never the Pi's internet state: a
relay outage mustn't look like the Pi going offline.
"""
import logging
import random
import socket
import sys
import threading
import time

from ..crypto import ed25519_sign
from ..relay import DEFAULT, parse_relay
from ..relay.wire import VERSION, REGISTER, Control, Refused, answer, close, dial, send_line, splice
from . import Service
from .access import RELAY_SOURCE

log = logging.getLogger("audiolink")

SOURCE = RELAY_SOURCE if sys.platform.startswith("linux") else None   # Linux: all of 127/8 is local
PING = 25                   # s between pings on the control connection
IDLE = 75                   # s without a pong before it counts as dead
BACKOFF = (5, 300)          # s, first and longest wait between tries (randomised)
STEADY = 60                 # s up before the wait resets
SESSIONS = 8                # relayed connections at once
OPENS_PER_MIN = 60          # opens answered per minute, all clients together
PER_CLIENT_PER_MIN = 30     # opens answered per minute from one address
MAX_TRACKED = 1000          # addresses remembered at most
ENDPOINT_PORT = 47900       # services/endpoint.py PORT


class RelayAgent(Service):
    name = "relay"

    def __init__(self, identity, wifi, relays=(DEFAULT,), endpoint_port=ENDPOINT_PORT):
        self.identity, self.wifi = identity, wifi
        self.relays = list(relays)
        self.endpoint_port = endpoint_port
        self.lock = threading.Lock()
        self.state = {"relay": "off" if not self.relays else "connecting", "since": time.time()}
        self.sessions = threading.BoundedSemaphore(SESSIONS)
        self.active = 0
        self.opens = []                 # times of recent opens
        self.by_client = {}             # address -> times of its recent opens
        self.ctl = None

    def handle(self, op, args):
        if op == "status":
            return self.status()
        return super().handle(op, args)

    def status(self):
        with self.lock:
            return dict(self.state, sessions=self.active)

    def run(self):
        if self.relays:
            threading.Thread(target=self._loop, name="relay", daemon=True).start()

    # ---- control connection
    def _set(self, relay, **fields):
        with self.lock:
            changed = self.state.get("relay") != relay
            if changed:
                self.state = {"relay": relay, "since": time.time()}
            self.state.update(fields)
        return changed

    def _online(self):
        conn = self.wifi.connection()
        return conn["internet"] == "online" and not self.wifi.parked, conn["ip"]

    def _loop(self):
        wait = BACKOFF[0]
        while True:
            online, ip = self._online()
            if not online:
                if self._set("waiting for internet"):
                    log.info("relay: waiting for internet")
                time.sleep(5)
                continue
            spec = self.relays[0]
            started = time.monotonic()
            why = self._session(spec, ip)
            if self._set("down", error=why):
                log.info("relay: down (%s), retrying", why)
            if time.monotonic() - started > STEADY:
                wait = BACKOFF[0]
            time.sleep(random.uniform(0, wait))       # spread out Pis after a relay restart
            wait = min(wait * 2, BACKOFF[1])

    def _session(self, spec, ip):
        """One control connection, until it ends. -> why it ended."""
        addr, pin = parse_relay(spec)
        seed = self.identity.seed()
        pub = self.identity.public()
        try:
            sock = dial(addr, pin)
        except OSError as e:
            return "unreachable (%s)" % (getattr(e, "strerror", None) or e)
        try:
            send_line(sock, {"v": VERSION, "role": "pi", "pub": pub.hex()})
            challenge = bytes.fromhex(answer(sock)["challenge"])
            send_line(sock, {"sig": ed25519_sign(seed, REGISTER + challenge + pub).hex()})
            seen = answer(sock).get("addr")
        except (Refused, OSError, ValueError, KeyError) as e:
            close(sock)
            return "refused (%s)" % e if isinstance(e, Refused) else "handshake failed"
        sock.settimeout(None)
        ctl = self.ctl = Control(sock)
        self._set("up", relay_addr="%s:%d" % addr, seen_as=seen)
        log.info("relay: up at %s:%d", *addr)
        last_ping = [time.monotonic()]

        def on_line(msg):
            if msg.get("op") == "open":
                client = msg.get("from") if isinstance(msg.get("from"), str) else None
                threading.Thread(target=self._open, args=(addr, pin, str(msg.get("id")), client),
                                 daemon=True).start()

        def tick():
            now = time.monotonic()
            if now - last_ping[0] >= PING:
                last_ping[0] = now
                ctl.post({"op": "ping"})
                if self._online()[1] != ip:          # new address: this connection is stale
                    ctl.close()

        why = ctl.run(on_line, IDLE, tick)
        self.ctl = None
        return why

    # ---- relayed connections
    def _rate_ok(self, client):
        """May an open from this client address (None: the relay didn't say) go ahead?"""
        now = time.monotonic()
        with self.lock:
            if client:
                self.by_client = {k: v for k, v in self.by_client.items() if now - v[-1] < 60}
                times = [t for t in self.by_client.get(client, []) if now - t < 60]
                if len(times) >= PER_CLIENT_PER_MIN or (not times and len(self.by_client) >= MAX_TRACKED):
                    return False
                self.by_client[client] = times + [now]
            self.opens = [t for t in self.opens if now - t < 60]
            if len(self.opens) >= OPENS_PER_MIN:
                return False
            self.opens.append(now)
            return True

    def _open(self, addr, pin, wid, client=None):
        if not self._rate_ok(client) or not self.sessions.acquire(blocking=False):
            return                      # the client hears "no answer" from the relay
        try:
            data = dial(addr, pin)
            try:
                send_line(data, {"v": VERSION, "role": "data", "id": wid})
                answer(data)
                target = socket.create_connection(("127.0.0.1", self.endpoint_port), timeout=5,
                                                  source_address=(SOURCE, 0) if SOURCE else None)
            except (Refused, OSError, ValueError):
                close(data)
                return
            data.settimeout(None)
            target.settimeout(None)
            with self.lock:
                self.active += 1
            try:
                splice(data, target)
            finally:
                with self.lock:
                    self.active -= 1
        except OSError:
            pass
        finally:
            self.sessions.release()
