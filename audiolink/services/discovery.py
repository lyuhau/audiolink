"""Pi side: publish where the Pi is as a signed, encrypted record on the Mainline DHT.

The record is signed with the Pi's own identity (services/identity.py) and sealed
with its read secret. BEP 44 sequence numbers must keep increasing across restarts,
and we use the timestamp for them; the Pi has no real-time clock, so it waits until
NTP or a host has told it the time (session.now()).

Publishes once the Pi is online and discoverable, again whenever the record's
contents change, and every REPUBLISH seconds (DHT nodes drop items after a few hours).
"""
import logging
import threading
import time

from ..session import CLOCK_SANE
from . import Service

log = logging.getLogger("audiolink")

REPUBLISH = 30 * 60
CHECK_EVERY = 30


class DiscoveryService(Service):
    name = "discovery"
    caps = ("discovery",)

    def __init__(self, identity, wifi, streams):
        self.identity, self.wifi, self.streams = identity, wifi, streams
        self.last = {"published": None}
        self.wake = threading.Event()
        self.dht = None
        identity.subscribe(self.republish)
        threading.Thread(target=self._loop, daemon=True).start()

    def republish(self):
        self.last["content"] = None
        self.wake.set()

    def on_peer_hello(self, info):
        self.wake.set()                 # the host may have just given us the time

    # ---- control requests from the host
    def handle(self, op, args):
        if op == "status":
            public = {k: v for k, v in self.last.items() if k != "content"}     # content is internal
            return dict(public, enabled=self.identity.discoverable(), now=self.session.now(),
                        clock_known=self.session.now() > CLOCK_SANE,
                        online=self.wifi.connection()["internet"] == "online")
        if op == "publish":
            self.republish()
            return {"ok": True}
        raise ValueError("unknown discovery operation %r" % op)

    def record_now(self):
        """Where the Pi is now, as its record would say (for a machine pairing over the
        network, which may read the record anyway), or None while the time is unknown."""
        from ..discovery.record import make_record
        session = getattr(self, "session", None)
        if session is None or session.now() < CLOCK_SANE:
            return None
        conn = self.wifi.connection()
        return make_record(self.identity.keys()[1], self.identity.pi_name(), [conn["ip"]] if conn["ip"] else [],
                           conn["ssid"], self.streams.ports(), ts=session.now())[0]

    # ---- publishing
    def _current(self):
        """What the record should say, or None if there's nothing to publish yet."""
        if not self.identity.discoverable() or self.session.now() < CLOCK_SANE:
            return None
        conn = self.wifi.connection()
        if conn["internet"] != "online":
            return None
        return (self.identity.public(), self.identity.pi_name(),
                [conn["ip"]] if conn["ip"] else [], conn["ssid"], self.streams.ports())

    def _loop(self):
        from ..discovery.dht import DHT
        from ..discovery.record import SALT, make_record
        while True:
            self.wake.wait(CHECK_EVERY)
            self.wake.clear()
            try:
                cur = self._current()
                if cur is None:
                    continue
                due = self.last.get("published") is None or self.session.now() - self.last["published"] > REPUBLISH
                if cur == self.last.get("content") and not due:
                    continue
                seed, secret = self.identity.keys()
                pub, name, lan, ssid, ports = cur
                rec, blob = make_record(secret, name, lan, ssid, ports, ts=self.session.now())
                self.dht = self.dht or DHT()
                t0 = time.monotonic()
                stored = self.dht.put(seed, pub, blob, seq=rec["ts"], salt=SALT)
                took = time.monotonic() - t0
                if stored:
                    self.last.update(published=self.session.now(), content=cur, record=rec, nodes=stored,
                                     took=round(took, 1), error=None)
                    log.info("discovery: published %s (%d DHT nodes, %.1fs)", rec, stored, took)
                else:
                    self.last.update(error="no DHT node accepted the record", took=round(took, 1))
                    log.warning("discovery: no DHT node accepted the record")
            except Exception as e:
                self.last.update(error=str(e))
                log.warning("discovery: publish failed: %s", e)
