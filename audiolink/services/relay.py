"""Pi side of the relay (audiolink/relay): stay registered, answer clients through it.

While the Pi is online it keeps one connection to its relay, registered under its
public key. When a client asks for it, the relay passes on an "open"; the Pi dials a
data connection, checks the client's proof (relay/tunnel.py), and joins it to the
local port asked for. Only the endpoint, ssh and the ports forwarded from the host
are reachable; never the proxy (it would put the Pi's network in reach).

Connections to local services come from RELAY_SOURCE (127.0.0.2), so the kernel's table
shows which ones came through the relay: presence counts them as clients, and the
endpoint treats them like Wi-Fi clients (they can use the Pi but not move it to
another network).

Relay state is its own fact ("relay": up/down), never the Pi's internet state: a
relay outage mustn't look like the Pi going offline.
"""
import logging
import random
import sys
import threading
import time

from ..crypto import ed25519_sign
from ..relay import DEFAULT, parse_relay
from ..relay.tunnel import serve
from ..relay.wire import VERSION, REGISTER, Control, Refused, answer, close, dial, send_line, splice
from . import Service
from .presence import RELAY_SOURCE

log = logging.getLogger("audiolink")

SOURCE = RELAY_SOURCE if sys.platform.startswith("linux") else None   # Linux: all of 127/8 is local
PING = 25                   # s between pings on the control connection
IDLE = 75                   # s without a pong before it counts as dead
BACKOFF = (5, 300)          # s, first and longest wait between tries (randomised)
STEADY = 60                 # s up before the wait resets
SESSIONS = 8                # relayed connections at once
OPENS_PER_MIN = 20          # opens answered per minute (proven or not)
ENDPOINT_PORT, SSH_PORT, PROXY_PORT = 47900, 22, 3128


class RelayAgent(Service):
    name = "relay"

    def __init__(self, identity, wifi, streams, relays=(DEFAULT,)):
        self.identity, self.wifi, self.streams = identity, wifi, streams
        self.relays = list(relays)
        self.lock = threading.Lock()
        self.state = {"relay": "off" if not self.relays else "connecting", "since": time.time()}
        self.sessions = threading.BoundedSemaphore(SESSIONS)
        self.active = 0
        self.opens = []                 # times of recent opens
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
        seed, _ = self.identity.keys()
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
                threading.Thread(target=self._open, args=(addr, pin, str(msg.get("id"))),
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
    def allowed(self, port):
        if port == PROXY_PORT:
            return False
        return port in (ENDPOINT_PORT, SSH_PORT) or port in self.streams.ports()

    def _rate_ok(self):
        now = time.monotonic()
        with self.lock:
            self.opens = [t for t in self.opens if now - t < 60]
            if len(self.opens) >= OPENS_PER_MIN:
                return False
            self.opens.append(now)
            return True

    def _open(self, addr, pin, wid):
        if not self._rate_ok() or not self.sessions.acquire(blocking=False):
            return                      # the client hears "no answer" from the relay
        try:
            data = dial(addr, pin)
            try:
                send_line(data, {"v": VERSION, "role": "data", "id": wid})
                answer(data)
                _, secret = self.identity.keys()
                got = serve(data, self.identity.public(), secret, self.allowed, SOURCE)
            except (Refused, OSError, ValueError):
                close(data)
                return
            if not got:
                close(data)
                return
            port, target = got
            data.settimeout(None)
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
