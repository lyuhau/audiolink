"""`audiolink connect PI PORT`: stdin/stdout <-> a port on the Pi's host, for an ssh
ProxyCommand. The Pi is found as client/remote.py finds it (this network, then its
relay); the connection is TLS to the Pi's endpoint, auth with this computer's key, then
`open` (docs/protocol.md "Streams"): the same on the LAN and through the relay.
PI is a name from `audiolink pis` (alias or the Pi's own name) or a Pi code.
"""
import os
import socket
import sys
import threading

from .. import ui


def connect_command(a):
    """stdout is the connection, so everything said goes to stderr, as "audiolink: ..."."""
    try:
        conn = _connect(a)
    except SystemExit as e:
        if isinstance(e.code, str) and not e.code.startswith("audiolink:"):
            raise SystemExit("audiolink: " + e.code)
        raise
    _pipe(conn.sock)


def _connect(a):
    from . import keyring
    from .remote import Remote
    entry = keyring.lookup(a.pi)
    r = Remote(entry, list(getattr(a, "at", None) or [])).open(quiet=True)
    if ui.VERBOSE and r.conn.ip == "relay":     # stdout is the connection: say it on stderr
        sys.stderr.write("audiolink: through the relay\n")
    reply = r.conn.open_stream(a.port)
    if "error" in reply:
        r.close()
        if reply.get("code") == "not_exposed":
            raise SystemExit("%s isn't offering port %s (on its host: audiolink host --expose %s:22)"
                             % (r.name, a.port, a.port))
        raise SystemExit(r.refusal(reply))
    return r.conn


def _pipe(sock):
    """Bytes between this process's stdin/stdout and the socket, until the socket's side
    ends. The socket is the plain end of PiConn's socket pair (its TLS is driven by
    another thread), so any thread may read or write it."""
    out = sys.stdout.buffer

    def up():
        try:
            while True:
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    break
                sock.sendall(data)
        except OSError:
            pass
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    threading.Thread(target=up, daemon=True).start()
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            out.write(data)
            out.flush()
    except OSError:
        pass
