"""Relay plumbing shared by the relay, the Pi and clients: TLS with a pinned certificate,
JSON lines, a control connection, and joining two sockets.

Every relay connection starts with one JSON line from the dialer:
  {"v": 1, "role": "pi" | "client" | "data", ...}
and one reply from the relay carrying its "v" (and "error" when it refuses). Unknown
fields and unknown control ops are ignored, so either side can add to them.

An SSL socket must not be read and written from two threads at once, so everything
here drives each socket from one thread: Control queues writes for its own thread,
and splice() moves both directions in a single loop.
"""
import collections
import hashlib
import json
import select
import socket
import ssl
import time

from ..tls import half_close

VERSION = 1
MIN_VERSION = 1
LINE_MAX = 4096
STALL = 60                  # a peer that takes no data for this long is gone

# Labels keep signatures and proofs made with the same keys apart from each other and
# from DHT records.
REGISTER = b"audiolink-relay-register-v1\0"


class Refused(Exception):
    """The other end said no (its "error" field)."""


def tune(sock):
    """Low latency, keepalive, and (Linux) give up on data unacknowledged for STALL s."""
    from ..services.streams import tune as base
    base(sock)
    if hasattr(socket, "TCP_USER_TIMEOUT"):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, STALL * 1000)
        except OSError:
            pass


def server_context(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    half_close(ctx)
    if hasattr(ctx, "num_tickets"):
        ctx.num_tickets = 0         # no resumption, and nothing unasked-for to read
    return ctx


def fingerprint(sock):
    return hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()


def dial(addr, pin, timeout=10):
    """A TLS connection to the relay at (host, port), checked against the pinned cert."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE         # the pin below is the check
    half_close(ctx)
    raw = socket.create_connection(addr, timeout=timeout)
    try:
        tune(raw)
        sock = ctx.wrap_socket(raw, server_hostname=addr[0])
    except (OSError, ssl.SSLError):
        raw.close()
        raise
    if fingerprint(sock) != pin:
        sock.close()
        raise ssl.SSLError("relay certificate doesn't match")
    return sock


def send_line(sock, obj):
    send_all(sock, (json.dumps(obj) + "\n").encode())


def read_line(sock):
    """One JSON object. Reads a byte at a time so nothing after the line is consumed."""
    buf = bytearray()
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("closed")
        if b == b"\n":
            break
        buf += b
        if len(buf) > LINE_MAX:
            raise ValueError("line too long")
    obj = json.loads(buf)
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    return obj


def answer(sock):
    """Read a reply line; raise Refused if it carries an error."""
    msg = read_line(sock)
    if msg.get("error"):
        raise Refused(msg["error"])
    return msg


def send_all(sock, data):
    """sendall() that also works on non-blocking (SSL) sockets; gives up after STALL s."""
    view = memoryview(data)
    while view:
        try:
            n = sock.send(view)
        except (ssl.SSLWantWriteError, ssl.SSLWantReadError, BlockingIOError, InterruptedError):
            n = 0
        except socket.timeout:
            raise OSError("peer stalled")
        if n:
            view = view[n:]
        elif not select.select([], [sock], [], STALL)[1]:
            raise OSError("peer stalled")


def _recv(sock):
    """Bytes available now; b"" at the end; None if nothing yet."""
    try:
        return sock.recv(65536)
    except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError, InterruptedError):
        return None


def _pending(sock):
    return isinstance(sock, ssl.SSLSocket) and sock.pending() > 0


def splice(a, b, idle=None):
    """Join two sockets until both directions end (or idle s pass with nothing moving).
    A direction that ends is passed on as a half-close: a bare TCP FIN even on TLS, since
    SSLSocket.shutdown() drops the TLS state and what arrives afterwards would be read
    as raw records (the far side takes the FIN as the end of reading: tls.half_close).
    Closes both.
    -> (bytes a->b, bytes b->a)."""
    peer = {a: b, b: a}
    reading = {a: True, b: True}
    moved = {a: 0, b: 0}
    last = time.monotonic()
    try:
        for s in (a, b):
            s.setblocking(False)
        while reading[a] or reading[b]:
            socks = [s for s in (a, b) if reading[s]]
            ready = [s for s in socks if _pending(s)] or select.select(socks, [], [], 5)[0]
            if not ready:
                if idle and time.monotonic() - last > idle:
                    break
                continue
            for s in ready:
                data = _recv(s)
                if data is None:
                    continue
                if not data:
                    reading[s] = False
                    try:
                        socket.socket.shutdown(peer[s], socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                send_all(peer[s], data)
                moved[s] += len(data)
                last = time.monotonic()
    except (OSError, ValueError):
        pass
    finally:
        for s in (a, b):
            close(s)
    return moved[a], moved[b]


def close(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class Control:
    """A long-lived JSON-lines connection driven by one thread (run()). Other threads
    post() lines; the loop sends them, delivers incoming lines to on_line, calls tick()
    on every pass (at least once a second), and ends when the peer closes, nothing arrives for `idle` s,
    or close() is called."""

    def __init__(self, sock):
        self.sock = sock
        self.out = collections.deque()
        self._wake_r, self._wake_w = socket.socketpair()
        self.closed = False
        self.heard = time.monotonic()

    def post(self, obj):
        self.out.append((json.dumps(obj) + "\n").encode())
        self._wake()

    def close(self):
        self.closed = True
        self._wake()

    def _wake(self):
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass

    def run(self, on_line, idle, tick=None):
        buf = b""
        self.sock.setblocking(False)
        self._wake_r.setblocking(False)
        try:
            while not self.closed:
                while self.out:
                    send_all(self.sock, self.out.popleft())
                ready = [self.sock] if _pending(self.sock) else \
                    select.select([self.sock, self._wake_r], [], [], 1)[0]
                if self._wake_r in ready:
                    try:
                        self._wake_r.recv(4096)
                    except OSError:
                        pass
                if self.sock in ready:
                    data = _recv(self.sock)
                    if data == b"":
                        return "closed"
                    if data:
                        self.heard = time.monotonic()
                        buf += data
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            msg = json.loads(line)
                            if isinstance(msg, dict):
                                on_line(msg)
                        if len(buf) > LINE_MAX:
                            return "line too long"
                if time.monotonic() - self.heard > idle:
                    return "silent"
                if tick:
                    tick()
            return "closed here"
        except (OSError, ValueError) as e:
            return str(e) or type(e).__name__
        finally:
            close(self.sock)
            self._wake_r.close()
            self._wake_w.close()
