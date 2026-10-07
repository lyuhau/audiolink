"""Session: one live conversation with the peer over the link, hosting services.

The session owns what every feature shares:
  HELLO    JSON {"v", "role", "version", "caps", "time", ...service fields}, sent whenever
           the link comes up (new peer, or the peer restarted). The Pi has no real-time
           clock, so it adopts the host's "time" (session.now()).
  liveness the peer is considered gone after PEER_TIMEOUT seconds of silence; the link
           then restarts with a new session id so both sides resync when it returns
  CTRL     JSON request {"op", "args"}, reply {"ok", "result"|"error"} (stream id field =
           request id), or one-way event {"event", "data"} (stream id 0). Fragmented: first
           byte 1 = more follows, 0 = last. Requests "<area>.<op>" go to the service named
           <area>; events go to every service.
Everything else (streams, Wi-Fi, proxy, ...) is a Service (see services/__init__.py).
"""
import itertools
import json
import logging
import threading
import time

from . import PROTOCOL_VERSION, __version__
from .wire.protocol import T_CTRL, T_HELLO, Link

log = logging.getLogger("audiolink")

PEER_TIMEOUT = 5.0      # seconds without frames before the peer is considered gone


class Session:
    def __init__(self, role, services=()):
        self.role = role
        self.services = []
        self.by_name = {}
        self.by_type = {}
        self.peer_info = None
        self.peer_alive = False
        self.clock_offset = 0.0                         # Pi: host's clock minus ours
        self.requests = {}                              # id -> [Event, reply]
        self.req_ids = itertools.count(1)
        self.ctrl_frags = {}                            # id -> bytearray being reassembled
        self.link = Link(self.on_message, self.on_datagram, self.on_up)
        for svc in services:
            self.add(svc)
        threading.Thread(target=self._watchdog, daemon=True).start()

    def add(self, svc):
        svc.attach(self)
        self.services.append(svc)
        if svc.name:
            self.by_name[svc.name] = svc
        for t in svc.msg_types:
            self.by_type[t] = svc
        return svc

    def now(self):
        """Wall-clock time; on the Pi, the host's clock (the Pi has none of its own)."""
        return time.time() + self.clock_offset

    @property
    def caps(self):
        caps = ["events"]
        for svc in self.services:
            caps += [c for c in svc.caps if c not in caps]
        return caps

    def peer_has(self, cap):
        return cap in (self.peer_info or {}).get("caps", [])

    # ---- link callbacks (parser thread; must not block for long)
    def on_up(self):
        self.peer_alive = True
        self._fail_requests("The connection to the Pi restarted; try again.")
        hello = {"v": PROTOCOL_VERSION, "role": self.role, "version": __version__, "time": time.time()}
        for svc in self.services:
            svc.on_up()
            hello.update(svc.hello())
        hello["caps"] = self.caps
        self.link.send(T_HELLO, 0, json.dumps(hello).encode())

    def on_datagram(self, sid, payload):
        pass  # phase 3 (IP packets) will hook in here

    def on_message(self, typ, sid, payload):
        if typ == T_HELLO:
            self.peer_info = info = json.loads(payload.decode())
            if self.role == "pi" and info.get("role") == "host" and info.get("time"):
                self.clock_offset = info["time"] - time.time()
            log.info("peer: %s %s, caps=%s", info.get("role"), info.get("version"),
                     ",".join(info.get("caps", [])))
            for svc in self.services:
                svc.on_peer_hello(info)
        elif typ == T_CTRL:
            buf = self.ctrl_frags.setdefault(sid, bytearray())
            buf += payload[1:]
            if payload[:1] == b"\x00":
                self._on_ctrl(sid, json.loads(bytes(self.ctrl_frags.pop(sid)).decode()))
        elif typ in self.by_type:
            self.by_type[typ].on_message(typ, sid, payload)

    # ---- control channel
    def request(self, op, args=None, timeout=60):
        """Send a control request to the peer and wait for its reply."""
        if not self.peer_alive:
            raise RuntimeError("No Pi is plugged in right now.")
        rid = next(self.req_ids) & 0xFFFF or next(self.req_ids)
        ent = self.requests[rid] = [threading.Event(), None]
        self._send_ctrl(rid, {"op": op, "args": args or {}})
        if not ent[0].wait(timeout):
            self.requests.pop(rid, None)
            raise TimeoutError("The Pi didn't answer (%s)." % op)
        reply = self.requests.pop(rid)[1]
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "request failed"))
        return reply.get("result")

    def send_event(self, name, data=None):
        """One-way notification to the peer (dropped if no peer is connected)."""
        if self.peer_alive:
            self._send_ctrl(0, {"event": name, "data": data})

    def _send_ctrl(self, rid, obj):
        data = json.dumps(obj).encode()
        step = self.link.max_payload - 1
        for i in range(0, max(len(data), 1), step):
            more = i + step < len(data)
            self.link.send(T_CTRL, rid, (b"\x01" if more else b"\x00") + data[i:i + step])

    def _on_ctrl(self, rid, msg):
        if "event" in msg:
            for svc in self.services:
                threading.Thread(target=svc.on_event, args=(msg["event"], msg.get("data")),
                                 daemon=True).start()
        elif "op" in msg:                     # a request from the peer
            threading.Thread(target=self._serve_ctrl, args=(rid, msg), daemon=True).start()
        else:                                 # a reply to one of ours
            ent = self.requests.get(rid)
            if ent:
                ent[1] = msg
                ent[0].set()

    def _serve_ctrl(self, rid, msg):
        area, _, op = msg["op"].partition(".")
        svc = self.by_name.get(area)
        try:
            if svc is None:
                raise ValueError("%s does not support %r" % (self.role, msg["op"]))
            reply = {"ok": True, "result": svc.handle(op, msg.get("args", {}))}
        except Exception as e:
            log.warning("control %s failed: %s", msg["op"], e)
            reply = {"ok": False, "error": str(e)}
        try:
            self._send_ctrl(rid, reply)
        except (TypeError, ValueError) as e:        # unencodable result: still answer
            log.warning("control %s: bad reply: %s", msg["op"], e)
            self._send_ctrl(rid, {"ok": False, "error": "%s returned an unencodable reply" % msg["op"]})

    def _fail_requests(self, why):
        for rid in list(self.requests):
            ent = self.requests.get(rid)
            if ent:
                ent[1] = {"ok": False, "error": why}
                ent[0].set()
        self.ctrl_frags.clear()

    # ---- liveness
    def _watchdog(self):
        while True:
            time.sleep(1)
            if self.peer_alive and time.monotonic() - self.link.last_rx > PEER_TIMEOUT:
                log.warning("peer silent for %.0fs; resetting the link", PEER_TIMEOUT)
                self.peer_alive = False
                self._fail_requests("The Pi disconnected.")
                for svc in self.services:
                    svc.on_down()
                # Start a fresh session so both sides resync when the peer returns.
                self.link.restart()
