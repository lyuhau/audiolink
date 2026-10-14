"""`audiolink shell [PI] [-- CMD]`: a root shell on the Pi (services/shell.py), or one
command run there.

No PI: the Pi plugged in here, over the link (the host daemon's shell port).
PI: over the network, reached like `connect` (LAN, setup network, relay); the Pi's
debug switch decides (services/access.py): this machine's pairing, --key FILE, or
nothing. Raw terminal here; Windows isn't supported yet.

With a Pi that frames the shell (cap "shell2"; services/shell.py) the command ends with
the shell's exit status, a resized window follows here, and `-- CMD` runs CMD with
`sh -c` on pipes (stdin, stdout and stderr passed through). An older Pi gets the bare
terminal and no exit status.
"""
import json
import os
import signal
import socket
import sys
import threading

from ..services.shell import F_CLOSED, F_DATA, F_EOF, F_ERR, F_EXIT, F_SIZE, MAX_CMD, Frames, frame, header


def _size():
    try:
        s = os.get_terminal_size(sys.stdout.fileno())
        return s.lines, s.columns
    except OSError:
        return 24, 80


def _header():
    rows, cols = _size()
    return {"rows": rows, "cols": cols, "term": os.environ.get("TERM") or "xterm"}


def _say(msg):
    print(msg, file=sys.stderr, flush=True)


def terminal(sock, reader):
    """This terminal <-> the shell until the shell's side ends (an older Pi: bare bytes).
    reader.read1() gives what the Pi sends (it may hold bytes already buffered), sock
    takes what's typed."""
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd) if os.isatty(fd) else None

    def up():
        try:
            while True:
                data = os.read(fd, 4096)
                if not data:
                    sock.sendall(b"\x04")     # input ended (not a terminal): the shell's EOF
                    break
                sock.sendall(data)
        except OSError:
            pass

    if old is not None:
        tty.setraw(fd)
    try:
        threading.Thread(target=up, daemon=True).start()
        out = sys.stdout.fileno()
        while True:
            try:
                data = reader.read1(65536)
            except (OSError, ValueError):
                break
            if not data:
                break
            os.write(out, data)
    finally:
        if old is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        try:
            sock.close()
        except OSError:
            pass


def framed(sock, reader, interactive):
    """This terminal (or stdin/stdout/stderr, for a command) <-> a framed shell.
    -> (exit status or None, why the Pi closed it or None)."""
    fd = sys.stdin.fileno()
    old = None
    if interactive and os.isatty(fd):
        import termios
        import tty
        old = termios.tcgetattr(fd)
    send = threading.Lock()

    def put(kind, payload=b""):
        with send:
            sock.sendall(frame(kind, payload))

    def up():
        try:
            while True:
                data = os.read(fd, 65536)
                if not data:
                    put(F_EOF)
                    break
                put(F_DATA, data)
        except OSError:
            pass

    def resized(*_):
        rows, cols = _size()
        try:
            put(F_SIZE, {"rows": rows, "cols": cols})
        except OSError:
            pass

    winch = None
    if interactive and hasattr(signal, "SIGWINCH") and threading.current_thread() is threading.main_thread():
        winch = signal.signal(signal.SIGWINCH, resized)
    if old is not None:
        tty.setraw(fd)
    status = why = None
    try:
        threading.Thread(target=up, daemon=True).start()
        frames = Frames()
        outs = {F_DATA: sys.stdout.fileno(), F_ERR: sys.stderr.fileno()}
        while True:
            try:
                data = reader.read1(65536)
            except (OSError, ValueError):
                break
            if not data:
                break
            try:
                items = frames.feed(data)
            except ValueError:
                break
            for kind, payload in items:
                if kind in outs:
                    os.write(outs[kind], payload)
                elif kind in (F_EXIT, F_CLOSED):
                    try:
                        info = json.loads(payload)
                    except ValueError:
                        info = {}
                    if kind == F_EXIT and isinstance(info.get("status"), int):
                        status = info["status"]
                    elif kind == F_CLOSED:
                        why = str(info.get("why") or "closed by the Pi")
    finally:
        if old is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if winch is not None:
            signal.signal(signal.SIGWINCH, winch)
        try:
            sock.close()
        except OSError:
            pass
    return status, why


def _run(sock, reader, name, cmd, v):
    """Runs the shell (or the command) to its end; -> this command's exit status."""
    if v != 2:
        _say("Root shell on %s (exit or Ctrl-D to leave)" % name)
        terminal(sock, reader)
        _say("Shell closed")
        return 0
    if cmd is None:
        _say("Root shell on %s (exit or Ctrl-D to leave)" % name)
    status, why = framed(sock, reader, interactive=cmd is None)
    if cmd is None:
        _say("Shell closed: %s" % why if why else "Shell closed")
    elif why:
        _say("Stopped: %s" % why)
    elif status is None:
        _say("Lost the connection to %s" % name)
    return status if status is not None else 255


def _usb(a, cmd):
    from ..host.control import DEFAULT_PORT, call
    port = getattr(a, "control_port", None) or DEFAULT_PORT
    peer = call("peer", port=port)
    if not peer.get("connected"):
        raise SystemExit("No Pi plugged in (or name one: audiolink shell PI)")
    caps = (peer.get("peer") or {}).get("caps", [])
    if "shell" not in caps:
        raise SystemExit("The Pi's audiolink has no shell; update it (audiolink update)")
    v = 2 if "shell2" in caps else 1
    if cmd is not None and v != 2:
        raise SystemExit("The Pi's audiolink can't run a single command; update it (audiolink update)")
    if not peer.get("shell_port"):
        raise SystemExit("audiolink host runs without a shell port (--shell-port)")
    try:
        sock = socket.create_connection(("127.0.0.1", peer["shell_port"]), timeout=5)
    except OSError:
        raise SystemExit("audiolink host's shell port isn't answering; restart audiolink host")
    sock.settimeout(None)
    h = _header()
    sock.sendall(header(h["rows"], h["cols"], h["term"], v=v, cmd=cmd))
    ident = (peer.get("peer") or {}).get("identity") or {}
    return _run(sock, sock.makefile("rb"), _name(ident), cmd, v)


def _name(ident):
    """What this machine calls the plugged-in Pi."""
    try:
        from . import keyring
        alias = (keyring.load().get(ident.get("pub")) or {}).get("alias")
    except Exception:
        alias = None
    return alias or ident.get("name") or "the Pi"


def shell_command(a):
    if sys.platform == "win32":
        raise SystemExit("audiolink shell doesn't work on Windows yet")
    cmd = getattr(a, "command", None)
    if cmd is not None:
        if not cmd:
            raise SystemExit("Which command? (audiolink shell [PI] -- CMD)")
        cmd = " ".join(cmd)                 # as ssh does: quote it to keep shell syntax for the Pi
        if len(cmd.encode()) > MAX_CMD:
            raise SystemExit("That command is too long (at most %d KB)" % (MAX_CMD // 1024))
    if not a.pi:
        return _usb(a, cmd)
    from .remote import open_remote
    remote = open_remote(a.pi, a.at or [], getattr(a, "key", None))
    try:
        r = remote.ask("shell", v=2, **dict(_header(), **({"cmd": cmd} if cmd is not None else {})))
    except BaseException:
        remote.close()
        raise
    if "error" in r:
        remote.close()
        raise SystemExit(remote.refusal(r))
    v = 2 if r.get("v") == 2 else 1
    if cmd is not None and v != 2:
        remote.close()                      # an older Pi opened a plain shell instead
        raise SystemExit("%s's audiolink can't run a single command; update it" % remote.name)
    remote.conn.sock.settimeout(None)       # a shell can sit idle
    return _run(remote.conn.sock, remote.conn.f, remote.name, cmd, v)
