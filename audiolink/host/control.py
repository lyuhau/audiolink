"""Local control port of a running `audiolink host`, and call() for its clients.

The host daemon owns the audio device, so other commands (e.g. `audiolink wifi`)
talk to it over 127.0.0.1 with one JSON request and one JSON reply per connection.
Requests named in local_ops are answered by the daemon itself; anything else
("wifi.scan", ...) is forwarded to the Pi over the session, with the link's role (the
host's key on the Pi's list): the control port adds no authority of its own.

Every request carries a token from <config>/control-token, which only the OS user who
runs `audiolink host` can read: other users on the computer can't drive this host.
"""
import hmac
import json
import logging
import os
import secrets
import socket
import threading
import time

from ..net import reuse_addr
from ..paths import config_dir

log = logging.getLogger("audiolink")

DEFAULT_PORT = int(os.environ.get("AUDIOLINK_CONTROL_PORT", "47800"))
NOT_YOURS = "not this user's audiolink host"


def token_path():
    return os.path.join(str(config_dir()), "control-token")


def make_token():
    """A fresh token for this run of `audiolink host`, readable by this user only."""
    tok = secrets.token_hex(32)
    path = token_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp-%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    os.replace(tmp, path)
    return tok


def read_token():
    try:
        with open(token_path()) as f:
            return f.read().strip()
    except OSError:
        return ""


def listen(port=DEFAULT_PORT):
    """Take the control port: the first thing `audiolink host` does, since holding it is
    also what keeps a second host off the Pi's audio device (two would corrupt the link)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reuse_addr(srv)
    try:
        srv.bind(("127.0.0.1", port))
    except OSError as e:
        srv.close()
        log.debug("control port 127.0.0.1:%d: %s", port, e)
        try:
            call("peer", port=port, timeout=3)
        except SystemExit as e:
            if "another user's" in str(e.code):
                raise SystemExit("audiolink host is already running here, as another login (one host per "
                                 "computer)")
            raise SystemExit("Port %d is taken by another program; give audiolink another: "
                             "--control-port PORT (the same for its other commands)" % port)
        raise SystemExit("audiolink host is already running")
    srv.listen(4)
    return srv


def serve(session, srv, local_ops=None, token=None):
    """srv: from listen(). local_ops: {op: fn(args) -> result} handled in the daemon.
    token: what each request must carry (make_token())."""
    local_ops = local_ops or {}
    token = token if token is not None else make_token()

    def handle(conn):
        with conn:
            try:
                req = json.loads(conn.makefile("r").readline())
                if not hmac.compare_digest(str(req.get("token", "")), token):
                    raise PermissionError(NOT_YOURS)
                if req["op"] in local_ops:
                    result = local_ops[req["op"]](req.get("args") or {})
                else:
                    result = session.request(req["op"], req.get("args"), timeout=90)
                reply = {"ok": True, "result": result}
            except Exception as e:
                reply = {"ok": False, "error": str(e)}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass                    # the caller gave up waiting

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
                threading.Thread(target=handle, args=(conn,), daemon=True).start()
            except (OSError, RuntimeError) as e:     # e.g. out of file descriptors or threads
                log.warning("control port: %s", e)
                time.sleep(1)

    threading.Thread(target=loop, daemon=True).start()


CONNECT = 3         # s: a host is on this computer or not (WSL's mirrored networking drops,
                    # rather than refuses, a connection to a localhost port nobody listens on)


def call(op, args=None, port=DEFAULT_PORT, timeout=120):
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=min(timeout, CONNECT))
    except OSError:
        raise SystemExit("audiolink host isn't running (start it: audiolink host)")
    conn.settimeout(timeout)
    try:
        with conn:
            conn.sendall((json.dumps({"op": op, "args": args or {}, "token": read_token()}) + "\n").encode())
            line = conn.makefile("r").readline()
        reply = json.loads(line) if line else None
    except socket.timeout:
        raise SystemExit("audiolink host didn't answer in time")
    except (OSError, ValueError):
        reply = None
    if not isinstance(reply, dict):
        raise SystemExit("audiolink host stopped before answering")
    if not reply.get("ok"):
        msg = reply.get("error") or "Failed"
        if msg == NOT_YOURS:
            msg = "audiolink host here is another user's (it runs as a different login)"
        raise SystemExit(msg[:1].upper() + msg[1:])
    return reply["result"]
