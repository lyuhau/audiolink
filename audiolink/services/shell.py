"""Pi side: a root shell (release images have no sshd).

ShellServer: a pty running SHELL per connection, on 127.0.0.1:PORT only. A connection
starts with one JSON line {"rows", "cols", "term"}, then carries the terminal's bytes
both ways; closing it hangs the shell up, and the shell exiting closes it.

Reached:
  over USB          a link stream (the host forwards its 127.0.0.1:47802 here; the
                    Pi's stream allow list has PORT): the host is root anyway
  over the network  the endpoint's "shell" request (debug level, services/access.py),
                    which joins the endpoint connection to the server. At most
                    NETWORK_SHELLS at once, apart from the endpoint's request slots;
                    closed when debug access ends (off, expiry) or the read secret changes.
The relay never reaches PORT directly (services/relay.py).
"""
import json
import logging
import os
import re
import select
import signal
import socket
import threading
import time
import warnings

from . import Service
from .access import DEBUG
from .endpoint import Takeover
from .streams import shut

log = logging.getLogger("audiolink")

PORT = 47901
SHELL = ["/bin/sh", "-l"]
NETWORK_SHELLS = 2
MAX_SHELLS = 6                      # all routes together
HEADER_MAX = 1024

# The child execs at once; nothing in it needs the parent's other threads.
warnings.filterwarnings("ignore", message=".*multi-threaded.*fork", category=DeprecationWarning)


def header(rows=24, cols=80, term="xterm"):
    return (json.dumps({"rows": rows, "cols": cols, "term": term}) + "\n").encode()


def _read_header(sock):
    """-> (dict, bytes after the header line). Raises ValueError."""
    buf = b""
    sock.settimeout(10)
    while b"\n" not in buf:
        data = sock.recv(256)
        if not data or len(buf) > HEADER_MAX:
            raise ValueError("no header")
        buf += data
    sock.settimeout(None)
    line, _, rest = buf.partition(b"\n")
    h = json.loads(line)
    if not isinstance(h, dict):
        raise ValueError("bad header")
    return h, rest


def _clean(h):
    rows, cols = h.get("rows"), h.get("cols")
    rows = rows if isinstance(rows, int) and 0 < rows < 1000 else 24
    cols = cols if isinstance(cols, int) and 0 < cols < 1000 else 80
    term = str(h.get("term") or "xterm")
    return rows, cols, term if re.fullmatch(r"[A-Za-z0-9._+-]{1,32}", term) else "xterm"


def join(a, b):
    """Bytes both ways until either side ends; then both close (a shell that exited
    ends the client's connection, and a client gone hangs the shell up)."""
    peer = {a: b, b: a}
    try:
        while True:
            for s in select.select([a, b], [], [])[0]:
                data = s.recv(65536)
                if not data:
                    return
                peer[s].sendall(data)
    except (OSError, ValueError):
        pass
    finally:
        shut(a)
        shut(b)


class ShellServer:
    def __init__(self, port=PORT, argv=SHELL, bind="127.0.0.1"):
        self.argv = list(argv)
        self.slots = threading.BoundedSemaphore(MAX_SHELLS)
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((bind, port))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]

    def run(self):
        threading.Thread(target=self._accept, name="shell", daemon=True).start()
        log.info("shell: on 127.0.0.1:%d", self.port)
        return self

    def _accept(self):
        while True:
            try:
                sock, _ = self.srv.accept()
            except OSError as e:
                if self.srv.fileno() < 0:
                    return
                log.warning("shell: %s", e)
                time.sleep(1)
                continue
            if not self.slots.acquire(blocking=False):
                sock.close()
                continue
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock):
        pid = fd = None
        try:
            h, rest = _read_header(sock)
            rows, cols, term = _clean(h)
            pid, fd = self._spawn(rows, cols, term)
            log.info("shell: started (pid %d)", pid)
            if rest:
                os.write(fd, rest)
            self._pump(sock, fd)
        except (OSError, ValueError) as e:
            log.debug("shell: %s", e)
        finally:
            shut(sock)
            if pid:
                self._hang_up(pid, fd)
                log.info("shell: closed (pid %d)", pid)
            self.slots.release()

    def _spawn(self, rows, cols, term):
        import pty
        home = os.path.expanduser("~")
        env = {"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"), "HOME": home,
               "TERM": term, "SHELL": self.argv[0]}
        pid, fd = pty.fork()
        if pid == 0:                                    # the child: exec, whatever happens
            try:
                import fcntl
                import struct
                import termios
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except (ImportError, OSError):
                pass
            try:
                os.chdir(home)
                os.execve(self.argv[0], self.argv, env)
            finally:
                os._exit(127)
        return pid, fd

    @staticmethod
    def _pump(sock, fd):
        sock.setblocking(True)
        while True:
            ready = select.select([sock, fd], [], [])[0]
            if sock in ready:
                data = sock.recv(65536)
                if not data:
                    return
                os.write(fd, data)
            if fd in ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:                         # Linux: EIO once the shell has gone
                    return
                if not data:
                    return
                sock.sendall(data)

    @staticmethod
    def _hang_up(pid, fd):
        try:
            os.close(fd)                                # the terminal hangs up
        except OSError:
            pass
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except OSError:
                pass
            end = time.monotonic() + 1
            while time.monotonic() < end:
                try:
                    if os.waitpid(pid, os.WNOHANG)[0]:
                        return
                except ChildProcessError:
                    return
                time.sleep(0.05)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


class NetworkShells(Service):
    """The endpoint's "shell" request: debug level, its own limit, closed when debug ends."""
    caps = ("shell",)

    def __init__(self, endpoint, debug, identity, port=PORT, limit=NETWORK_SHELLS):
        self.debug, self.identity, self.port = debug, identity, port
        self.slots = threading.BoundedSemaphore(limit)
        self.limit = limit
        self.lock = threading.Lock()
        self.open = set()
        self.secret = identity.keys()[1]
        endpoint.register("shell", self._shell, DEBUG)
        debug.on_end(self.close_all)
        debug.shells = self.count
        identity.subscribe(self._identity_changed)

    def count(self):
        with self.lock:
            return len(self.open)

    def close_all(self, why):
        with self.lock:
            socks, self.open = list(self.open), set()
        for s in socks:
            shut(s)
        if socks:
            log.info("shell: closed %d network shell%s (%s)", len(socks), "s"[:len(socks) != 1], why)

    def _identity_changed(self):
        secret = self.identity.keys()[1]
        if secret != self.secret:
            self.secret = secret
            self.close_all("read secret changed")

    def _shell(self, conn, req):
        if not self.slots.acquire(blocking=False):
            return {"error": "%d shells open already" % self.limit, "code": "busy"}
        try:
            target = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            target.sendall(header(*_clean(req)))
        except OSError:
            self.slots.release()
            return {"error": "The Pi's shell isn't running", "code": "no_shell"}
        conn.send({"ok": True})

        def run():
            with self.lock:
                self.open.add(conn.sock)
            try:
                if self.debug.snapshot().get("mode", "off") == "off":
                    shut(conn.sock)             # debug ended just now
                join(conn.sock, target)
            finally:
                with self.lock:
                    self.open.discard(conn.sock)
                self.slots.release()
                log.info("shell: network shell from %s ended", conn.addr[0])
        raise Takeover(run)
