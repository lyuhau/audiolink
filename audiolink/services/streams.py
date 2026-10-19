"""TCP streams multiplexed over the session.

Either side can:
  forward  listen on a local TCP port; each connection asks the peer to connect
           to a target host:port and relays bytes both ways (the host, on 127.0.0.1).
  allow    accept the peer's OPEN requests, but only for listed targets.
The host can also:
  expose   ask the Pi to offer a port that brings connections back to a host target;
           sent in the HELLO, so the Pi re-applies it on every link-up, and answers
           with an "exposed" event: {"ok": [[pi_port, host, port]], "refused":
           [[pi_port, why]]} (cap "expose_report").
The Pi listens on no port for them (docs/protocol.md "Streams"): a computer on its list
asks its endpoint to `open` an exposed port (or a static `audiolink pi --forward`),
over TLS on the LAN, the setup network or the relay alike, and that connection becomes
the stream (Streams.serve_endpoint). At most NET_STREAMS at once, apart from the
endpoint's request slots, with no idle limit (an ssh session idles for minutes). The
host's own streams to the Pi's proxy and shell take an owner's key (owner_only).

Messages (all reliable, in order):
  OPEN    "host:port" for a new stream id
  DATA    bytes for a stream
  CREDIT  4-byte count: the receiver has written that many of the stream's bytes to
          its socket (flow control, when both sides have the "stream_credit" cap)
  CLOSE   sender is done writing (half-close); a stream ends when both sides close

Flow control: a side sends at most WINDOW bytes of a stream ahead of the peer's
credits, so a slow socket at the far end (a client on weak Wi-Fi, or one that went to
sleep) holds up only its own stream, and buffers stay bounded (the Pi has 512 MB).
DATA for a stream the receiver has already finished is credited and dropped.
Relayed sockets use TCP keepalive, so a client that vanished without closing (a laptop
lid shut mid-ssh) is dropped, and stops counting as connected (services/presence.py).

Whatever is behind an exposed port sees every connection come from the host itself
(127.0.0.1), so its own address rules can't tell clients apart: its own login has to.
Each listener carries PER_LISTENER connections at once.
"""
import logging
import queue
import socket
import struct
import threading
import time

from ..net import reuse_addr
from ..wire.protocol import T_CLOSE, T_CREDIT, T_DATA, T_OPEN
from . import Service
from .access import USER

log = logging.getLogger("audiolink")

WINDOW = 512 * 1024             # bytes in flight per stream (about the link's bandwidth x latency)
CREDIT_EVERY = WINDOW // 4
CREDIT = struct.Struct(">I")
PER_LISTENER = 32               # connections at once through one listening port
NET_STREAMS = 16                # pi: streams opened through the endpoint at once


def tune(sock):
    """Low latency, and TCP keepalive: a peer gone silently is dropped in ~2 min."""
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPALIVE", 60), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 6)):
        if hasattr(socket, name):           # TCP_KEEPALIVE is macOS's name for the idle time
            try:
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
            except OSError:
                pass


def shut(sock):
    """Close a socket so that threads blocked on it wake up (close() alone doesn't)."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def parse_target(spec, default_host="127.0.0.1"):
    """'22' -> ('127.0.0.1', 22); 'host:22' -> ('host', 22)."""
    host, sep, port = spec.rpartition(":")
    return (host if sep else default_host), int(port)


def parse_forward(spec):
    """'2222:22' -> (2222, ('127.0.0.1', 22)); '2222:host:22' -> (2222, ('host', 22))."""
    lport, _, target = spec.partition(":")
    return int(lport), parse_target(target)


def parse_range(spec):
    lo, _, hi = spec.partition("-")
    return int(lo), int(hi or lo)


class Stream:
    def __init__(self, sid, sock=None):
        self.sid = sid
        self.sock = sock
        self.wq = queue.Queue()     # payloads to write to the socket; None = peer EOF
        self.closed_tx = self.closed_rx = False
        self.dead = False           # torn down: stop waiting for credit
        self.cv = threading.Condition()
        self.in_flight = 0          # bytes sent to the peer, not yet credited back
        self.consumed = 0           # bytes written to our socket, not yet credited to the peer


class Streams(Service):
    name = "streams"
    caps = ("streams", "expose", "stream_credit", "expose_report")
    msg_types = (T_OPEN, T_DATA, T_CLOSE, T_CREDIT)

    def __init__(self, role, allow=(), expose=(), expose_ports="1024-65535", owner_only=(), may_open=None):
        self.role = role
        # Every stream opening and closing: the Pi's log keeps them; on the host they're -v detail.
        self.chatty = log.info if role == "pi" else log.debug
        self.expose = list(expose)                      # host: [(pi_port, (host, port))]
        self.allow = {parse_target(a) for a in allow} | {t for _, t in self.expose}
        self.owner_only = {parse_target(a) for a in owner_only}     # pi: allowed to an owner's host only
        self.may_open = may_open or (lambda: True)      # pi: () -> is the host's key an owner's?
        self.expose_range = parse_range(expose_ports)   # pi: ports a host may expose
        self.streams = {}
        self.listeners = {}                             # port -> (socket, target): forward()
        self.static = {}                                # pi: port -> target (audiolink pi --forward)
        self.exposed = {}                               # pi: port -> target (the host's expose)
        self.lock = threading.Lock()
        self.llock = threading.RLock()                  # guards listeners, static, exposed
        self.next_sid = 1 if role == "pi" else 2        # odd/even: no id clashes
        self.net_slots = threading.BoundedSemaphore(NET_STREAMS)

    def ports(self):
        """The Pi's ports a client can open (static forwards and exposes); the host's
        listening ones."""
        with self.llock:
            return sorted(set(self.listeners) | set(self.static) | set(self.exposed))

    def target_for(self, port):
        """pi: the host target behind this port, or None."""
        with self.llock:
            return self.static.get(port) or self.exposed.get(port)

    def serve(self, port, target):
        """pi: a static forward (audiolink pi --forward): `open` port reaches the host's target."""
        with self.llock:
            self.static[port] = target
        log.info("forwarding :%d -> host %s:%d (through the endpoint)", port, *target)

    # ---- session hooks
    def hello(self):
        return {"expose": [[p, h, tp] for p, (h, tp) in self.expose]}

    def on_peer_hello(self, info):
        if self.role == "pi":
            report = self._apply_expose(info.get("expose", []))
            self.session.send_event("exposed", report)

    def on_up(self):
        self._close_all()

    def on_down(self):
        self._close_all()
        if self.role == "pi":
            self._apply_expose([])

    def on_message(self, typ, sid, payload):
        if typ == T_OPEN:
            st = Stream(sid)
            with self.lock:
                self.streams[sid] = st
            target = parse_target(payload.decode())
            threading.Thread(target=self._connect_and_pump, args=(st, target), daemon=True).start()
            return
        with self.lock:
            st = self.streams.get(sid)
        if st is None:
            if typ == T_DATA and self._flow_controlled():
                # A stream we've already finished (our end closed first): the sender
                # still counts these bytes against its window. Credit them, or it would
                # wait forever and never notice its own client closing.
                self.session.link.send(T_CREDIT, sid, CREDIT.pack(len(payload)))
            return
        if typ == T_DATA:
            st.wq.put(payload)
        elif typ == T_CREDIT:
            with st.cv:
                st.in_flight -= CREDIT.unpack(payload)[0]
                st.cv.notify_all()
        elif typ == T_CLOSE:
            st.wq.put(None)

    def _flow_controlled(self):
        return self.session.peer_has("stream_credit")

    def _kill(self, st):
        with st.cv:
            st.dead = True
            st.cv.notify_all()
        st.wq.put(None)
        if st.sock:
            shut(st.sock)

    def _close_all(self):
        with self.lock:
            streams, self.streams = list(self.streams.values()), {}
        for st in streams:
            self._kill(st)

    # ---- pi: mirror the host's expose list as local listeners
    def _apply_expose(self, wanted):
        """Returns {"ok": [[port, host, tport]], "refused": [[port, why]]} for the host."""
        with self.llock:
            return self._apply_expose_locked(wanted)

    def _apply_expose_locked(self, wanted):
        want, refused = {}, []
        lo, hi = self.expose_range
        for port, host, tport in wanted:
            if not lo <= port <= hi:
                refused.append([port, "outside the ports it allows (%d-%d)" % (lo, hi)])
            elif port in self.static:
                refused.append([port, "it's already a static --forward there"])
            else:
                want[port] = (host, tport)
        for port in set(self.exposed) | set(want):
            if self.exposed.get(port) != want.get(port):
                self.chatty("%s :%d%s", "exposing" if port in want else "stopped exposing", port,
                            " -> host %s:%d" % want[port] if port in want else "")
        self.exposed = want
        for port, why in refused:
            log.warning("host asked to expose port %d: refused, %s", port, why)
        return {"ok": [[p, h, tp] for p, (h, tp) in sorted(want.items())], "refused": refused}

    def _unlisten(self, port):
        srv, target = self.listeners.pop(port)
        srv.close()
        log.info("stopped listening on :%d", port)

    # ---- per-stream pumps
    def _connect_and_pump(self, st, target):
        host, port = target
        link = self.session.link
        if target not in self.allow:
            log.warning("stream %d: %s:%d not allowed (use --allow or --expose)", st.sid, host, port)
            self._forget(st)
            link.send(T_CLOSE, st.sid)
            return
        if target in self.owner_only and not self.may_open():
            log.warning("stream %d: the host asked for %s:%d, which takes an owner's key: refused "
                        "(this computer isn't an owner of the Pi)", st.sid, host, port)
            self._forget(st)
            link.send(T_CLOSE, st.sid)
            return
        try:
            st.sock = socket.create_connection(target, timeout=5)
            st.sock.settimeout(None)
            tune(st.sock)
        except OSError as e:
            log.warning("stream %d: connect to %s:%d failed: %s", st.sid, host, port, e)
            self._forget(st)
            link.send(T_CLOSE, st.sid)
            return
        self.chatty("stream %d: connected to %s:%d", st.sid, host, port)
        threading.Thread(target=self._reader, args=(st,), daemon=True).start()
        self._writer(st)

    def _reader(self, st):
        """socket -> link"""
        link = self.session.link
        flow = self._flow_controlled()
        try:
            while True:
                if flow:
                    with st.cv:
                        while st.in_flight >= WINDOW and not st.dead:
                            st.cv.wait(1)
                    if st.dead:
                        break
                data = st.sock.recv(link.max_payload)
                if not data:
                    break
                link.stats["tcp_tx_bytes"] += len(data)
                with st.cv:
                    st.in_flight += len(data)
                link.send(T_DATA, st.sid, data)
        except OSError:
            pass
        link.send(T_CLOSE, st.sid)
        st.closed_tx = True
        self._maybe_finish(st)

    def _writer(self, st):
        """link -> socket"""
        try:
            while True:
                data = st.wq.get()
                if data is None:
                    break
                st.sock.sendall(data)
                self.session.link.stats["tcp_rx_bytes"] += len(data)
                st.consumed += len(data)
                if st.consumed >= CREDIT_EVERY and self._flow_controlled():
                    self.session.link.send(T_CREDIT, st.sid, CREDIT.pack(st.consumed))
                    st.consumed = 0
            st.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        st.closed_rx = True
        self._maybe_finish(st)

    @staticmethod
    def _over(st):
        return st.dead or (st.closed_tx and st.closed_rx)

    def _maybe_finish(self, st):
        if st.closed_tx and st.closed_rx:
            self._forget(st)
            shut(st.sock)
            self.chatty("stream %d: closed", st.sid)

    def _forget(self, st):
        with self.lock:
            if self.streams.get(st.sid) is st:  # not a newer stream that reused the id
                del self.streams[st.sid]

    def _new_sid(self):
        """Call with self.lock held: the next free stream id (odd on the Pi, even on the host)."""
        while True:
            sid = self.next_sid
            self.next_sid = (sid + 2) & 0xFFFF or (1 if self.role == "pi" else 2)
            if sid not in self.streams:
                return sid

    # ---- forward: accept local TCP connections and open streams on the peer
    def _refuse(self, sock, mine):
        """Why a listener turns this connection away, or None. mine: its streams."""
        mine[:] = [st for st in mine if not self._over(st)]
        if len(mine) >= PER_LISTENER:
            return "%d connections already" % len(mine)
        return None

    def forward(self, lport, target, bind="0.0.0.0"):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        reuse_addr(srv)
        try:
            srv.bind((bind, lport))
            srv.listen(16)
        except OSError:
            srv.close()
            raise
        with self.llock:
            self.listeners[lport] = (srv, target)
        self.chatty("forwarding %s:%d -> peer %s:%d", bind, lport, *target)

        def accept_loop():
            mine = []                                       # this listener's streams
            warned = set()
            while True:
                try:
                    sock, addr = srv.accept()
                except OSError as e:
                    if srv.fileno() < 0:
                        return                              # listener closed
                    log.warning("accept on :%d: %s", lport, e)  # e.g. out of file descriptors
                    time.sleep(1)
                    continue
                if not self.session.peer_alive:
                    sock.close()                            # nobody on the other end
                    continue
                why = self._refuse(sock, mine)
                if why:
                    sock.close()
                    if why not in warned:                   # once per listener and reason
                        warned.add(why)
                        log.warning("stream on :%d from %s refused: %s", lport, addr[0], why)
                    continue
                tune(sock)
                with self.lock:
                    sid = self._new_sid()
                    st = Stream(sid, sock)
                    self.streams[sid] = st
                self.chatty("stream %d: %s:%d -> peer %s:%d", sid, addr[0], addr[1], *target)
                self.session.link.send(T_OPEN, sid, ("%s:%d" % target).encode())
                try:
                    threading.Thread(target=self._reader, args=(st,), daemon=True).start()
                    threading.Thread(target=self._writer, args=(st,), daemon=True).start()
                except RuntimeError as e:                   # out of threads: drop this one, keep listening
                    log.warning("stream %d: %s", sid, e)
                    self._forget(st)
                    self._kill(st)
                    self.session.link.send(T_CLOSE, sid)
                    continue
                mine.append(st)

        threading.Thread(target=accept_loop, daemon=True).start()

    # ---- pi: streams through the endpoint (services/endpoint.py "open")
    def serve_endpoint(self, endpoint):
        endpoint.register("open", self._open, USER)

    def _open(self, conn, req):
        from .endpoint import Takeover
        port = req.get("port")
        pi = conn.identity.pi_name()
        target = self.target_for(port) if isinstance(port, int) else None
        if target is None:
            return {"error": "Port %s isn't exposed on %s" % (port, pi), "code": "not_exposed"}
        if not self.session.peer_alive:
            return {"error": "%s isn't plugged into a computer right now (port %d)" % (pi, port),
                    "code": "not_exposed"}
        if not self.net_slots.acquire(blocking=False):
            return {"error": "%s has %d streams open already; retry soon" % (pi, NET_STREAMS), "code": "busy"}
        try:
            conn.send({"ok": True})
        except OSError:
            self.net_slots.release()
            raise

        def run():
            try:
                self._through(conn, port, target)
            finally:
                self.net_slots.release()
        raise Takeover(run)

    def _through(self, conn, port, target):
        """The connection, now a stream: its TLS is driven by one thread (splice), the
        link's pumps work on a plain socket pair."""
        from ..relay.wire import splice
        ours, theirs = socket.socketpair()
        try:
            tune(conn.sock)
        except OSError:
            pass
        with self.lock:
            sid = self._new_sid()
            st = Stream(sid, ours)
            self.streams[sid] = st
        self.chatty("stream %d: %s (%s, %s) :%d -> host %s:%d", sid, conn.name, conn.route, conn.addr[0], port, *target)
        self.session.link.send(T_OPEN, sid, ("%s:%d" % target).encode())
        try:
            threading.Thread(target=self._reader, args=(st,), daemon=True).start()
            threading.Thread(target=self._writer, args=(st,), daemon=True).start()
        except RuntimeError as e:                   # out of threads
            log.warning("stream %d: %s", sid, e)
            self._forget(st)
            self._kill(st)
            self.session.link.send(T_CLOSE, sid)
            shut(theirs)
            return
        pending = conn.leftover()
        try:
            if pending:
                theirs.sendall(pending)
        except OSError:
            pass
        splice(conn.sock, theirs)
