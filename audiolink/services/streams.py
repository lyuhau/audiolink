"""TCP streams multiplexed over the session.

Either side can:
  forward  listen on a local TCP port; each connection asks the peer to connect
           to a target host:port and relays bytes both ways.
  allow    accept the peer's OPEN requests, but only for listed targets.
The host can also:
  expose   ask the Pi to listen on a port and bring connections back to a host
           target; sent in the HELLO, so the Pi re-applies it on every link-up.

Messages (all reliable, in order):
  OPEN   "host:port" for a new stream id
  DATA   bytes for a stream
  CLOSE  sender is done writing (half-close); a stream ends when both sides close
"""
import logging
import queue
import socket
import threading

from ..wire.protocol import T_CLOSE, T_DATA, T_OPEN
from . import Service

log = logging.getLogger("audiolink")


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
    def __init__(self, sid, sock=None, path=None):
        self.sid = sid
        self.sock = sock
        self.path = path            # how the client reached us (services/presence.py), if counted
        self.wq = queue.Queue()     # payloads to write to the socket; None = peer EOF
        self.closed_tx = self.closed_rx = False


class Streams(Service):
    name = "streams"
    caps = ("streams", "expose")
    msg_types = (T_OPEN, T_DATA, T_CLOSE)

    def __init__(self, role, allow=(), expose=(), expose_ports="1024-65535", presence=None):
        self.role = role
        self.presence = presence                        # pi: counts client connections
        self.expose = list(expose)                      # host: [(pi_port, (host, port))]
        self.allow = {parse_target(a) for a in allow} | {t for _, t in self.expose}
        self.expose_range = parse_range(expose_ports)   # pi: ports a host may expose
        self.streams = {}
        self.listeners = {}                             # port -> (socket, target, exposed)
        self.lock = threading.Lock()
        self.llock = threading.RLock()                  # guards listeners
        self.next_sid = 1 if role == "pi" else 2        # odd/even: no id clashes

    def ports(self):
        """Local ports currently listening (static forwards and exposes)."""
        with self.llock:
            return sorted(self.listeners)

    # ---- session hooks
    def hello(self):
        return {"expose": [[p, h, tp] for p, (h, tp) in self.expose]}

    def on_peer_hello(self, info):
        if self.role == "pi":
            self._apply_expose(info.get("expose", []))

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
            return
        if typ == T_DATA:
            st.wq.put(payload)
        elif typ == T_CLOSE:
            st.wq.put(None)

    def _close_all(self):
        with self.lock:
            streams, self.streams = list(self.streams.values()), {}
        for st in streams:
            st.wq.put(None)
            if st.sock:
                try:
                    st.sock.close()
                except OSError:
                    pass

    # ---- pi: mirror the host's expose list as local listeners
    def _apply_expose(self, wanted):
        with self.llock:
            self._apply_expose_locked(wanted)

    def _apply_expose_locked(self, wanted):
        want = {}
        lo, hi = self.expose_range
        for port, host, tport in wanted:
            if not lo <= port <= hi:
                log.warning("host asked to expose port %d; allowed range is %d-%d", port, lo, hi)
            elif port in self.listeners and not self.listeners[port][2]:
                log.warning("host asked to expose port %d; already a static --forward", port)
            else:
                want[port] = (host, tport)
        for port, (srv, target, exposed) in list(self.listeners.items()):
            if exposed and want.get(port) != target:
                self._unlisten(port)
        for port, target in want.items():
            if port not in self.listeners:
                try:
                    self.forward(port, target, exposed=True)
                except OSError as e:
                    log.warning("can't expose port %d: %s", port, e)

    def _unlisten(self, port):
        srv, target, exposed = self.listeners.pop(port)
        srv.close()
        log.info("stopped listening on :%d", port)

    # ---- per-stream pumps
    def _connect_and_pump(self, st, target):
        host, port = target
        link = self.session.link
        if target not in self.allow:
            log.warning("stream %d: %s:%d not allowed (use --allow or --expose)", st.sid, host, port)
            link.send(T_CLOSE, st.sid)
            return
        try:
            st.sock = socket.create_connection(target, timeout=5)
            st.sock.settimeout(None)
            st.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            log.warning("stream %d: connect to %s:%d failed: %s", st.sid, host, port, e)
            link.send(T_CLOSE, st.sid)
            return
        log.info("stream %d: connected to %s:%d", st.sid, host, port)
        threading.Thread(target=self._reader, args=(st,), daemon=True).start()
        self._writer(st)

    def _reader(self, st):
        """socket -> link"""
        link = self.session.link
        try:
            while True:
                data = st.sock.recv(link.max_payload)
                if not data:
                    break
                link.stats["tcp_tx_bytes"] += len(data)
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
            st.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        st.closed_rx = True
        self._maybe_finish(st)

    def _maybe_finish(self, st):
        if st.closed_tx and st.closed_rx:
            with self.lock:
                self.streams.pop(st.sid, None)
                path, st.path = st.path, None   # both pumps can get here: count it once
            if self.presence:
                self.presence.leave(path)
            try:
                st.sock.close()
            except OSError:
                pass
            log.info("stream %d: closed", st.sid)

    # ---- forward: accept local TCP connections and open streams on the peer
    def forward(self, lport, target, bind="0.0.0.0", exposed=False):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((bind, lport))
        srv.listen(16)
        with self.llock:
            self.listeners[lport] = (srv, target, exposed)
        log.info("%s %s:%d -> peer %s:%d", "exposing" if exposed else "forwarding", bind, lport, *target)

        def accept_loop():
            while True:
                try:
                    sock, addr = srv.accept()
                except OSError:
                    return                                  # listener closed
                if not self.session.peer_alive:
                    if self.presence:
                        self.presence.touch(sock)           # a client did reach us
                    sock.close()                            # nobody on the other end
                    continue
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                path = self.presence.enter(sock) if self.presence else None
                with self.lock:
                    sid = self.next_sid
                    self.next_sid = (self.next_sid + 2) & 0xFFFF or (1 if self.role == "pi" else 2)
                    st = Stream(sid, sock, path)
                    self.streams[sid] = st
                log.info("stream %d: %s:%d -> peer %s:%d", sid, addr[0], addr[1], *target)
                self.session.link.send(T_OPEN, sid, ("%s:%d" % target).encode())
                threading.Thread(target=self._reader, args=(st,), daemon=True).start()
                threading.Thread(target=self._writer, args=(st,), daemon=True).start()

        threading.Thread(target=accept_loop, daemon=True).start()
