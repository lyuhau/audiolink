"""Local control port of a running `audiolink host`, and call() for its clients.

The host daemon owns the audio device, so other commands (e.g. `audiolink wifi`)
talk to it over 127.0.0.1 with one JSON request and one JSON reply per connection.
Requests named in local_ops are answered by the daemon itself; anything else
("wifi.scan", ...) is forwarded to the Pi over the session.
"""
import json
import logging
import os
import socket
import threading
import time

from ..net import reuse_addr

log = logging.getLogger("audiolink")

DEFAULT_PORT = int(os.environ.get("AUDIOLINK_CONTROL_PORT", "47800"))


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
        except SystemExit:
            raise SystemExit("Port %d is taken by another program; give audiolink another: "
                             "--control-port PORT (the same for its other commands)" % port)
        raise SystemExit("audiolink host is already running")
    srv.listen(4)
    return srv


def serve(session, srv, local_ops=None):
    """srv: from listen(). local_ops: {op: fn(args) -> result} handled in the daemon."""
    local_ops = local_ops or {}

    def handle(conn):
        with conn:
            try:
                req = json.loads(conn.makefile("r").readline())
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


def call(op, args=None, port=DEFAULT_PORT, timeout=120):
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        raise SystemExit("audiolink host isn't running (start it: audiolink host)")
    try:
        with conn:
            conn.sendall((json.dumps({"op": op, "args": args or {}}) + "\n").encode())
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
        raise SystemExit(msg[:1].upper() + msg[1:])
    return reply["result"]
