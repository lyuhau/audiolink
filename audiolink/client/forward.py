"""`audiolink forward PI PORT [--local LPORT]`: listen on 127.0.0.1:LPORT here; each
connection goes to the Pi's port PORT, for ssh clients that can't run a ProxyCommand
(SSHFS-Win's own ssh, ...): `ssh -p LPORT -o HostKeyAlias=audiolink-PUB USER@127.0.0.1`.
Each connection finds the Pi afresh, as `audiolink connect` does (client/find.py), so a
restart of the host or the link ends only the connections open then; the listener stays,
and a new connection keeps trying for RETRY seconds while the Pi's side comes back.
"""
import select
import socket
import sys
import threading
import time
from types import SimpleNamespace

from .. import ui

RETRY = 20          # seconds a new connection keeps trying to reach the Pi's port
PAUSE = 2           # between tries


def forward_command(a):
    from . import keyring
    entry = keyring.lookup(a.pi)                # a name that isn't one ends here, not per connection
    lport = getattr(a, "local", None) or a.port
    at = list(getattr(a, "at", None) or [])
    srv = listen(lport)
    print("Forwarding 127.0.0.1:%d to %s's port %d (Ctrl-C stops)" % (lport, a.pi, a.port))
    if entry.get("code"):
        print("  ssh -p %d -o HostKeyAlias=audiolink-%s USER@127.0.0.1"
              % (lport, keyring.pub_of(entry["code"])))
    sys.stdout.flush()
    try:
        serve(srv, a.pi, a.port, at)
    except KeyboardInterrupt:
        return 0
    finally:
        srv.close()


def listen(lport):
    """127.0.0.1 only. No SO_REUSEADDR on Windows (create_server leaves it off there), so a
    second forward on the same port fails here instead of sharing it."""
    try:
        srv = socket.create_server(("127.0.0.1", lport))
    except OSError as e:
        raise SystemExit("Can't listen on 127.0.0.1:%d here (%s): another program (or forward) "
                         "has it? --local picks another port" % (lport, e.strerror or e))
    srv.settimeout(1)                           # Windows: accept() won't see Ctrl-C otherwise
    return srv


def serve(srv, pi, port, at):
    """Until srv is closed (or Ctrl-C)."""
    while True:
        try:
            client, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            if srv.fileno() == -1:
                return
            time.sleep(0.1)                     # e.g. out of file handles: don't spin
            continue
        client.settimeout(None)
        threading.Thread(target=handle, args=(client, pi, port, at), daemon=True).start()


def say(msg):
    sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


def handle(client, pi, port, at):
    conn = reach(client, pi, port, at)
    if conn is None:
        client.close()
        return
    if ui.VERBOSE:
        say("connected (%s)" % ("through the relay" if getattr(conn, "ip", None) == "relay" else "at %s" % conn.ip))
    try:
        splice(client, conn.sock)
    finally:
        client.close()
        conn.close()
    if ui.VERBOSE:
        say("closed")


def reach(client, pi, port, at):
    """The Pi's stream (find._connect's PiConn), trying again until RETRY runs out or the
    local side hangs up; None then, with why said once."""
    from . import find
    deadline = time.monotonic() + RETRY
    while True:
        try:
            return find._connect(SimpleNamespace(pi=pi, port=port, at=at))
        except SystemExit as e:
            why = str(e.code)
        except (OSError, ValueError) as e:
            why = "%s: %s" % (pi, e)
        if time.monotonic() + PAUSE >= deadline:
            say("%s (gave up after %d s)" % (why, RETRY))
            return None
        if ui.VERBOSE:
            say("%s; trying again" % why)
        time.sleep(PAUSE)
        if hung_up(client):
            return None


def hung_up(sock):
    """Has the other end closed? (Data waiting, e.g. ssh's banner, isn't a hang-up.)"""
    try:
        if not select.select([sock], [], [], 0)[0]:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


def splice(a, b):
    """Bytes both ways until both sides have ended; an end passes on as a half-close.
    b is the plain end of PiConn's socket pair, so any thread may use it."""
    def one(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    t = threading.Thread(target=one, args=(b, a), daemon=True)
    t.start()
    one(a, b)
    t.join()
