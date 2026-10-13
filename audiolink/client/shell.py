"""`audiolink shell [PI]`: a root shell on the Pi (services/shell.py).

No PI: the Pi plugged in here, over the link (the host daemon's shell port).
PI: over the network, reached like `connect` (LAN, setup network, relay); the Pi's
debug switch decides (services/access.py): this machine's pairing, --key FILE, or
nothing. Raw terminal here; Windows isn't supported yet.
"""
import os
import socket
import sys
import threading

from ..services.shell import header


def _size():
    try:
        s = os.get_terminal_size(sys.stdout.fileno())
        return s.lines, s.columns
    except OSError:
        return 24, 80


def _header():
    rows, cols = _size()
    return {"rows": rows, "cols": cols, "term": os.environ.get("TERM") or "xterm"}


def terminal(sock, reader):
    """This terminal <-> the shell until the shell's side ends. reader.read1() gives what
    the Pi sends (it may hold bytes already buffered), sock takes what's typed."""
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


def _usb(a):
    from ..host.control import DEFAULT_PORT, call
    port = getattr(a, "control_port", None) or DEFAULT_PORT
    peer = call("peer", port=port)
    if not peer.get("connected"):
        raise SystemExit("No Pi plugged in (or name one: audiolink shell PI)")
    if "shell" not in (peer.get("peer") or {}).get("caps", []):
        raise SystemExit("The Pi's audiolink has no shell; update it (audiolink update)")
    if not peer.get("shell_port"):
        raise SystemExit("audiolink host runs without a shell port (--shell-port)")
    try:
        sock = socket.create_connection(("127.0.0.1", peer["shell_port"]), timeout=5)
    except OSError:
        raise SystemExit("audiolink host's shell port isn't answering; restart audiolink host")
    sock.settimeout(None)
    h = _header()
    sock.sendall(header(h["rows"], h["cols"], h["term"]))
    terminal(sock, sock.makefile("rb"))


def shell_command(a):
    if sys.platform == "win32":
        raise SystemExit("audiolink shell doesn't work on Windows yet")
    if not a.pi:
        return _usb(a)
    from .remote import open_remote
    remote = open_remote(a.pi, a.at or [], getattr(a, "key", None))
    try:
        r = remote.ask("shell", **_header())
    except BaseException:
        remote.close()
        raise
    if "error" in r:
        remote.close()
        raise SystemExit(remote.refusal(r))
    terminal(remote.conn.sock, remote.conn.f)
