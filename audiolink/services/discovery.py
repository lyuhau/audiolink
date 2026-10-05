"""Pi side: publish where the Pi is as a signed, encrypted record on the Mainline DHT.

The host owns the identity (the Pi has no persistent storage yet) and pushes it with
"discovery.configure", along with its clock: the Pi has no real-time clock, and BEP 44
sequence numbers (we use the timestamp) must keep increasing across restarts.

Publishes once the Pi is online, again whenever the record's contents change, and
every REPUBLISH seconds (DHT nodes drop items after a few hours).
"""
import logging
import threading
import time

from . import Service

log = logging.getLogger("audiolink")

REPUBLISH = 30 * 60
CHECK_EVERY = 30


class DiscoveryService(Service):
    name = "discovery"
    caps = ("discovery",)

    def __init__(self, hostname="audiopi"):
        self.hostname = hostname
        self.identity = None
        self.clock_offset = 0.0
        self.last = {"published": None}
        self.wake = threading.Event()
        self.dht = None
        threading.Thread(target=self._loop, daemon=True).start()

    def now(self):
        return time.time() + self.clock_offset

    # ---- control requests from the host
    def handle(self, op, args):
        if op == "configure":
            if args.get("seed"):
                self.identity = {"seed": args["seed"], "secret": args["secret"]}
                self.clock_offset = args["time"] - time.time()
                log.info("discovery: configured; publishing when online")
            else:
                self.identity = None
                log.info("discovery: disabled")
            self.last["content"] = None
            self.wake.set()
            return {"ok": True}
        if op == "status":
            return dict(self.last, enabled=bool(self.identity), now=self.now())
        if op == "publish":
            self.last["content"] = None
            self.wake.set()
            return {"ok": True}
        raise ValueError("unknown discovery operation %r" % op)

    # ---- publishing
    def _current(self):
        """(lan ips, ssid, ports) as the record should describe them, or None if offline."""
        wifi = self.session.by_name.get("wifi")
        state = wifi.manager.internet if wifi else None
        if not state or state.get("internet") != "online":
            return None
        ip = wifi.manager._ip()
        streams = self.session.by_name.get("streams")
        ports = sorted(streams.listeners) if streams else []
        return ([ip] if ip else []), state.get("ssid"), ports

    def _loop(self):
        from ..discovery.dht import DHT
        from ..discovery.crypto import ed25519_public
        from ..discovery.record import SALT, make_record
        while True:
            self.wake.wait(CHECK_EVERY)
            self.wake.clear()
            if not self.identity:
                continue
            cur = self._current()
            if cur is None:
                continue
            due = self.last.get("published") is None or self.now() - self.last["published"] > REPUBLISH
            if cur == self.last.get("content") and not due:
                continue
            try:
                seed = bytes.fromhex(self.identity["seed"])
                secret = bytes.fromhex(self.identity["secret"])
                lan, ssid, ports = cur
                rec, blob = make_record(secret, self.hostname, lan, ssid, ports, ts=self.now())
                self.dht = self.dht or DHT()
                t0 = time.monotonic()
                stored = self.dht.put(seed, ed25519_public(seed), blob, seq=rec["ts"], salt=SALT)
                took = time.monotonic() - t0
                if stored:
                    self.last.update(published=self.now(), content=cur, record=rec, nodes=stored,
                                     took=round(took, 1), error=None)
                    log.info("discovery: published %s (%d DHT nodes, %.1fs)", rec, stored, took)
                else:
                    self.last.update(error="no DHT node accepted the record", took=round(took, 1))
                    log.warning("discovery: no DHT node accepted the record")
            except Exception as e:
                self.last.update(error=str(e))
                log.warning("discovery: publish failed: %s", e)
