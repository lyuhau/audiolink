"""Pi side: a root shell (release images have no sshd).

ShellServer: a pty running SHELL per connection, on 127.0.0.1:PORT only. A connection
starts with one JSON line {"rows", "cols", "term"}, then carries the terminal's bytes
both ways; closing it hangs the shell up, and the shell exiting closes it.

With "v": 2 in that line, both ways carry frames (frame(): a type byte, a 4-byte
length, the payload) instead of bare bytes:
  to the Pi     F_DATA (input), F_EOF (input ended), F_SIZE {"rows", "cols"} (resized)
  from the Pi   F_DATA (output), F_ERR (a command's stderr), F_EXIT {"status"} (last,
                when it ended by itself), F_CLOSED {"why"} (last, when the Pi closed it)
and "cmd": "..." runs that with `SHELL[0] -c` on pipes, not a pty (`audiolink shell -- CMD`).

Reached:
  over USB          a link stream (the host forwards its 127.0.0.1:47802 here; the
                    Pi's stream allow list has PORT), when the host's key is an owner's
                    (services/access.py LinkAccess)
  over the network  the endpoint's "shell" request (owners), which joins the endpoint
                    connection to the server. At most NETWORK_SHELLS at once, apart
                    from the endpoint's request slots; closed when the computer stops
                    being an owner (taken off the list, made a user, the Pi reset).
Nothing else reaches PORT: the relay's connections go to the endpoint (services/relay.py).
"""
import json
import logging
import os
import re
import select
import signal
import socket
import struct
import subprocess
import threading
import time
import warnings

from ..net import reuse_addr
from . import Service
from .access import OWNER
from .endpoint import Takeover
from .streams import shut

log = logging.getLogger("audiolink")

PORT = 47901
SHELL = ["/bin/sh", "-l"]
NETWORK_SHELLS = 2
MAX_SHELLS = 6                      # all routes together
HEADER_MAX = 64 * 1024
MAX_CMD = 16 * 1024
MAX_FRAME = 1 << 20
F_DATA, F_EOF, F_SIZE, F_ERR, F_EXIT, F_CLOSED = range(6)

# The child execs at once; nothing in it needs the parent's other threads.
warnings.filterwarnings("ignore", message=".*multi-threaded.*fork", category=DeprecationWarning)


def header(rows=24, cols=80, term="xterm", v=1, cmd=None):
    h = {"rows": rows, "cols": cols, "term": term}
    if v == 2:
        h["v"] = 2
        if cmd is not None:
            h["cmd"] = cmd
    return (json.dumps(h) + "\n").encode()


def frame(kind, payload=b""):
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode()
    return struct.pack(">BI", kind, len(payload)) + payload


class Frames:
    """Splits a byte stream into (type, payload) frames as it arrives."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= 5:
            kind, n = struct.unpack_from(">BI", self.buf)
            if n > MAX_FRAME:
                raise ValueError("frame too big")
            if len(self.buf) < 5 + n:
                break
            out.append((kind, bytes(self.buf[5:5 + n])))
            del self.buf[:5 + n]
        return out


def mode(h):
    """-> (version, command or None) from a header or a request. Raises ValueError."""
    if h.get("v") != 2:
        return 1, None
    cmd = h.get("cmd")
    if cmd is not None and (not isinstance(cmd, str) or not cmd.strip() or len(cmd) > MAX_CMD):
        raise ValueError("bad command")
    return 2, cmd


def _write_all(fd, data):
    while data:
        data = data[os.write(fd, data):]


def _settled(fd, rows, cols, wait=1.0):
    """Until the child has set the terminal's first size (or wait seconds)."""
    try:
        import fcntl
        import termios
    except ImportError:
        return
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            if struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[:2] == (rows, cols):
                return
        except OSError:
            return
        time.sleep(0.01)


def _resize(fd, payload):
    try:
        size = json.loads(payload)
        rows, cols, _ = _clean(dict(size, term=None))
        import fcntl
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except (ValueError, TypeError, AttributeError, ImportError, OSError):
        pass


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


def join(a, b, last=None):
    """Bytes both ways until either side ends; then last() (if given: it may still
    write to a) and both close (a shell that exited ends the client's connection, and a
    client gone hangs the shell up). Either may be an SSL socket: what it has decrypted
    already doesn't show in select()."""
    peer = {a: b, b: a}
    try:
        while True:
            ready = [s for s in (a, b) if getattr(s, "pending", lambda: 0)()]
            for s in ready or select.select([a, b], [], [])[0]:
                data = s.recv(65536)
                if not data:
                    return
                peer[s].sendall(data)
    except (OSError, ValueError):
        pass
    finally:
        if last:
            last()
        shut(a)
        shut(b)


class ShellServer:
    def __init__(self, port=PORT, argv=SHELL, bind="127.0.0.1"):
        self.argv = list(argv)
        self.slots = threading.BoundedSemaphore(MAX_SHELLS)
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        reuse_addr(self.srv)
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
            v, cmd = mode(h)
            if cmd is not None:
                self._command(sock, cmd, term, rest)
                return
            pid, fd = self._spawn(rows, cols, term)
            log.info("shell: started (pid %d)", pid)
            if v == 2:
                _settled(fd, rows, cols)        # before a resize: the child sets the size first
                self._pump_frames(sock, fd, pid, rest)
            else:
                if rest:
                    _write_all(fd, rest)
                self._pump(sock, fd)
        except (OSError, ValueError) as e:
            log.debug("shell: %s", e)
        finally:
            shut(sock)
            if pid:
                self._hang_up(pid, fd)
                log.info("shell: closed (pid %d)", pid)
            self.slots.release()

    def _env(self, term):
        home = os.path.expanduser("~")
        return home, {"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"), "HOME": home,
                      "TERM": term, "SHELL": self.argv[0]}

    def _spawn(self, rows, cols, term):
        import pty
        home, env = self._env(term)
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

    def _command(self, sock, cmd, term, rest):
        """`audiolink shell -- CMD`: CMD with `sh -c` on pipes; its stdout and stderr as
        frames, its exit status last."""
        home, env = self._env(term)
        proc = subprocess.Popen([self.argv[0], "-c", cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=home, env=env, start_new_session=True)
        log.info("shell: command started (pid %d)", proc.pid)
        send = threading.Lock()

        def put(data):
            with send:
                sock.sendall(data)

        def copy(pipe, kind):
            try:
                for data in iter(lambda: os.read(pipe.fileno(), 65536), b""):
                    put(frame(kind, data))
            except OSError:
                pass

        readers = [threading.Thread(target=copy, args=(p, k), daemon=True)
                   for p, k in ((proc.stdout, F_DATA), (proc.stderr, F_ERR))]
        for t in readers:
            t.start()

        def finish():
            for t in readers:
                t.join()
            code = proc.wait()
            try:
                put(frame(F_EXIT, {"status": 128 - code if code < 0 else code}))
                sock.shutdown(socket.SHUT_RDWR)         # wakes the input loop below
            except OSError:
                pass
        ender = threading.Thread(target=finish, daemon=True)
        ender.start()
        frames, stdin = Frames(), proc.stdin
        try:
            items = frames.feed(rest)
            while True:
                for kind, data in items:
                    if stdin and kind in (F_DATA, F_EOF):
                        try:
                            if kind == F_DATA:
                                stdin.write(data)
                                stdin.flush()
                            else:
                                stdin.close()
                                stdin = None
                        except (OSError, ValueError):   # it closed its input
                            stdin = None
                data = sock.recv(65536)
                if not data:
                    break
                items = frames.feed(data)
        except (OSError, ValueError):
            pass
        finally:
            if proc.poll() is None:                     # the client went away first
                for sig in (signal.SIGHUP, signal.SIGKILL):
                    try:
                        os.killpg(proc.pid, sig)
                        proc.wait(1)
                        break
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            ender.join(5)
            for p in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    p.close()
                except (OSError, ValueError):
                    pass
            log.info("shell: command ended (pid %d, status %s)", proc.pid, proc.returncode)

    @staticmethod
    def _pump_frames(sock, fd, pid, rest):
        frames = Frames()

        def take(items):
            for kind, data in items:
                if kind == F_DATA:
                    _write_all(fd, data)
                elif kind == F_EOF:
                    _write_all(fd, b"\x04")            # the terminal's end of input
                elif kind == F_SIZE:
                    _resize(fd, data)
        sock.setblocking(True)
        take(frames.feed(rest))
        while True:
            ready = select.select([sock, fd], [], [])[0]
            if sock in ready:
                data = sock.recv(65536)
                if not data:
                    return
                take(frames.feed(data))
            if fd in ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:                         # Linux: EIO once the shell has gone
                    data = b""
                if not data:
                    code = ShellServer._reap(pid)
                    if code is not None:
                        sock.sendall(frame(F_EXIT, {"status": code}))
                    return
                sock.sendall(frame(F_DATA, data))

    @staticmethod
    def _reap(pid, wait=2.0):
        """The shell's exit status, once it has exited (None if it doesn't in time)."""
        end = time.monotonic() + wait
        while True:
            try:
                done, st = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return None
            if done:
                return os.WEXITSTATUS(st) if os.WIFEXITED(st) else 128 + os.WTERMSIG(st)
            if time.monotonic() >= end:
                return None
            time.sleep(0.02)

    @staticmethod
    def _pump(sock, fd):
        sock.setblocking(True)
        while True:
            ready = select.select([sock, fd], [], [])[0]
            if sock in ready:
                data = sock.recv(65536)
                if not data:
                    return
                _write_all(fd, data)
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
    """The endpoint's "shell" request: owners only, its own limit."""
    caps = ("shell", "shell2")         # shell2: framed (exit status, resize, -- CMD)

    def __init__(self, endpoint, port=PORT, limit=NETWORK_SHELLS):
        self.port = port
        self.slots = threading.BoundedSemaphore(limit)
        self.limit = limit
        self.lock = threading.Lock()
        self.open = {}                  # endpoint socket -> [shell socket, framed, why closed]
        endpoint.register("shell", self._shell, OWNER)

    def count(self):
        with self.lock:
            return len(self.open)

    def _shell(self, conn, req):
        try:
            v, cmd = mode(req)
        except ValueError:
            return {"error": "Not a command the Pi can run", "code": "bad_request"}
        if not self.slots.acquire(blocking=False):
            return {"error": "%d shells open already" % self.limit, "code": "busy"}
        try:
            target = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            target.sendall(header(*_clean(req), v=v, cmd=cmd))
        except OSError:
            self.slots.release()
            return {"error": "The Pi's shell isn't running", "code": "no_shell"}
        entry = [target, v == 2, None]
        with self.lock:
            self.open[conn.sock] = entry
        try:
            conn.send({"ok": True, "v": v})
        except OSError:
            self._ended(conn, target)
            raise

        def last():
            if entry[1] and entry[2]:
                try:
                    conn.sock.sendall(frame(F_CLOSED, {"why": entry[2]}))
                except (OSError, ValueError):
                    pass

        def revoked(why):               # the endpoint's: this computer is no longer an owner
            entry[2] = why
            shut(target)
        conn.on_revoke = revoked

        def run():
            try:
                pending = conn.leftover()
                if pending:
                    target.sendall(pending)
                join(conn.sock, target, last)
            finally:
                self._ended(conn, target)
                log.info("shell: network shell from %s ended", conn.addr[0])
        raise Takeover(run)

    def _ended(self, conn, target):
        shut(target)
        with self.lock:
            self.open.pop(conn.sock, None)
        self.slots.release()
