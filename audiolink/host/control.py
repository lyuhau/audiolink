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

log = logging.getLogger("audiolink")

DEFAULT_PORT = int(os.environ.get("AUDIOLINK_CONTROL_PORT", "47800"))


def serve(session, port=DEFAULT_PORT, local_ops=None):
    """local_ops: {op: fn(args) -> result} handled in the daemon."""
    local_ops = local_ops or {}
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", port))
    except OSError as e:
        log.warning("control port 127.0.0.1:%d unavailable (%s); `audiolink wifi` won't work", port, e)
        return
    srv.listen(4)

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
            conn, _ = srv.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()


def call(op, args=None, port=DEFAULT_PORT):
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=120)
    except OSError:
        raise SystemExit("audiolink host isn't running (start it: audiolink host)")
    with conn:
        conn.sendall((json.dumps({"op": op, "args": args or {}}) + "\n").encode())
        line = conn.makefile("r").readline()
    if not line:
        raise SystemExit("audiolink host stopped before answering")
    reply = json.loads(line)
    if not reply.get("ok"):
        msg = reply.get("error") or "Failed"
        raise SystemExit(msg[:1].upper() + msg[1:])
    return reply["result"]
