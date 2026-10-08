"""`audiolink relay`: the relay itself. Runs anywhere with Python 3 and a public port.

  pi      {"v","role":"pi","pub"} -> {"v","challenge"}; {"sig"} -> {"v","ok","addr"}
          then control lines: relay->pi {"op":"open","id"}; pi->relay {"op":"ping"}
          -> {"op":"pong"}. A newer registration for the same key replaces the older.
  client  {"v","role":"client","pub"} -> (relay asks the Pi to dial in) -> {"v","ok"}
  data    {"v","role":"data","id"} -> {"v","ok"}
After the two "ok"s the client and data connections are joined byte for byte. The
relay never dials anywhere and keeps no record of what passes through.
"""
import logging
import os
import socket
import ssl
import threading
import time

from ..crypto import ed25519_verify
from . import parse_addr
from .wire import (MIN_VERSION, REGISTER, VERSION, Control, close, read_line, send_line,
                   server_context, splice, tune)

log = logging.getLogger("audiolink")

HANDSHAKE = 10              # s for TLS and the first lines
OPEN_WAIT = 10              # s for the Pi to dial in after being asked
CONTROL_IDLE = 90           # s without a line before a Pi's control connection is dropped
PER_IP = 32                 # connections at once from one address
PER_PI = 8                  # client connections waiting on one Pi
MAX_PIS = 5000


class Relay:
    def __init__(self, ctx, open_wait=OPEN_WAIT, control_idle=CONTROL_IDLE, per_ip=PER_IP):
        self.ctx = ctx
        self.open_wait = open_wait
        self.control_idle = control_idle
        self.per_ip = per_ip
        self.lock = threading.Lock()
        self.pis = {}               # pub (bytes) -> Control
        self.waiting = {}           # id -> {"pub", "sock", "ready" Event}
        self.from_ip = {}           # ip -> connections open
        self.sessions = 0
        self.started = time.time()

    # ---- listening
    def listen(self, host, port):
        srv = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(128)
        return srv

    def serve(self, srv):
        threading.stack_size(256 * 1024)
        while True:
            try:
                sock, addr = srv.accept()
            except OSError:
                return
            ip = addr[0]
            with self.lock:
                if self.from_ip.get(ip, 0) >= self.per_ip:
                    sock.close()
                    continue
                self.from_ip[ip] = self.from_ip.get(ip, 0) + 1
            threading.Thread(target=self._conn, args=(sock, addr), daemon=True).start()

    def _conn(self, raw, addr):
        sock = raw
        keep = False
        try:
            tune(raw)
            raw.settimeout(HANDSHAKE)
            sock = self.ctx.wrap_socket(raw, server_side=True)
            hello = read_line(sock)
            v = hello.get("v")
            if not isinstance(v, int) or v < MIN_VERSION:
                send_line(sock, {"v": VERSION, "error": "unsupported version", "versions": [MIN_VERSION, VERSION]})
                return
            role = hello.get("role")
            if role == "pi":
                self._pi(sock, addr, hello)
            elif role == "client":
                self._client(sock, hello)
            elif role == "data":
                keep = self._data(sock, hello)
            elif role == "status" and addr[0] in ("127.0.0.1", "::1"):
                send_line(sock, self.status())
            else:
                send_line(sock, {"v": VERSION, "error": "unknown role"})
        except (OSError, ValueError, TypeError, AttributeError) as e:
            log.debug("relay: %s: %s", addr[0], e)
        finally:
            if not keep:
                close(sock)
            with self.lock:
                n = self.from_ip.get(addr[0], 1) - 1
                if n > 0:
                    self.from_ip[addr[0]] = n
                else:
                    self.from_ip.pop(addr[0], None)

    # ---- roles
    def _pi(self, sock, addr, hello):
        pub = bytes.fromhex(hello.get("pub", ""))
        if len(pub) != 32:
            send_line(sock, {"v": VERSION, "error": "bad key"})
            return
        challenge = os.urandom(32)
        send_line(sock, {"v": VERSION, "challenge": challenge.hex()})
        sig = bytes.fromhex(read_line(sock).get("sig", ""))
        if not ed25519_verify(pub, REGISTER + challenge + pub, sig):
            send_line(sock, {"v": VERSION, "error": "bad signature"})
            return
        ctl = Control(sock)
        with self.lock:
            if pub not in self.pis and len(self.pis) >= MAX_PIS:
                send_line(sock, {"v": VERSION, "error": "relay full"})
                return
            old = self.pis.get(pub)
            self.pis[pub] = ctl
        if old:
            old.close()
        send_line(sock, {"v": VERSION, "ok": True, "addr": "%s:%d" % addr[:2]})
        tag = pub[:4].hex()
        log.info("relay: pi %s up", tag)
        sock.settimeout(None)

        def on_line(msg):
            if msg.get("op") == "ping":
                ctl.post({"op": "pong"})

        why = ctl.run(on_line, self.control_idle)
        with self.lock:
            if self.pis.get(pub) is ctl:
                del self.pis[pub]
        log.info("relay: pi %s down (%s)", tag, why)

    def _client(self, sock, hello):
        try:
            pub = bytes.fromhex(hello.get("pub", ""))
        except ValueError:
            pub = b""
        wid = os.urandom(8).hex()
        slot = {"pub": pub, "sock": None, "ready": threading.Event()}
        with self.lock:
            ctl = self.pis.get(pub)
            busy = sum(1 for w in self.waiting.values() if w["pub"] == pub)
            if ctl and busy < PER_PI:
                self.waiting[wid] = slot
        if not ctl:
            send_line(sock, {"v": VERSION, "error": "offline"})
            return
        if busy >= PER_PI:
            send_line(sock, {"v": VERSION, "error": "busy"})
            return
        try:
            ctl.post({"op": "open", "id": wid})
            arrived = slot["ready"].wait(self.open_wait)
        finally:
            with self.lock:
                self.waiting.pop(wid, None)
        data = slot["sock"]
        if not arrived or data is None:
            send_line(sock, {"v": VERSION, "error": "no answer"})
            return
        try:
            send_line(data, {"v": VERSION, "ok": True})
            send_line(sock, {"v": VERSION, "ok": True})
        except OSError:
            close(data)
            raise
        sock.settimeout(None)
        data.settimeout(None)
        with self.lock:
            self.sessions += 1
        t0 = time.monotonic()
        up, down = splice(sock, data)
        with self.lock:
            self.sessions -= 1
        log.info("relay: session to %s ended after %ds (%d up, %d down)",
                 pub[:4].hex(), time.monotonic() - t0, up, down)

    def _data(self, sock, hello):
        """Hand this connection to the client waiting for it. True if handed over."""
        with self.lock:
            slot = self.waiting.pop(str(hello.get("id")), None)
        if not slot:
            send_line(sock, {"v": VERSION, "error": "unknown id"})
            return False
        slot["sock"] = sock
        slot["ready"].set()
        return True

    def status(self):
        with self.lock:
            return {"v": VERSION, "ok": True, "pis": len(self.pis), "sessions": self.sessions,
                    "waiting": len(self.waiting), "up": int(time.time() - self.started)}


def ask_status(port):
    """The relay's counts, asked over loopback (only loopback may ask)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE         # our own relay, over loopback
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with ctx.wrap_socket(raw) as sock:
            send_line(sock, {"v": VERSION, "role": "status"})
            return read_line(sock)


def relay_command(a):
    from ..ui import done
    host, port = parse_addr(a.listen)
    if a.status:
        st = ask_status(port)
        print("%d Pis connected, %d sessions, %d waiting, up %ds"
              % (st["pis"], st["sessions"], st["waiting"], st["up"]))
        return
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    relay = Relay(server_context(a.cert, a.key))
    srv = relay.listen(host, port)
    done("relay on %s:%d" % (host, port))
    relay.serve(srv)
