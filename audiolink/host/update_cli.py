"""`audiolink update [PYZ] | --revert | --status [--pi PI]`: new code for a Pi.

Sends the .pyz over the link (USB networking not needed), or with --pi over the network
(debug access, services/access.py); the Pi keeps it on its SD card and restarts into it
(services/update.py). Default: the .pyz this command runs from.
"""
import base64
import hashlib
import os
import sys
import time

from ..paths import PI_STATE_DIR
from ..ui import detail, done, line_buffered, step

CHUNK = 48 * 1024


def _short(h):
    return h[:12] if h else "none"


class NetCaller:
    """call() for update requests over the network (client/remote.py), reconnecting
    after the Pi restarts."""

    def __init__(self, remote):
        self.remote = remote

    def __call__(self, op, args=None, port=None):
        action = op.split(".", 1)[1]
        for attempt in (0, 1):
            try:
                if self.remote.conn is None:
                    self.remote.open(quiet=True)
                return self.remote.call("update", dict(args or {}, action=action))["result"]
            except (OSError, ValueError, ConnectionError):
                self.remote.close()
                if attempt:
                    raise SystemExit("Lost the connection to %s" % self.remote.name)


def update_command(a):
    line_buffered()
    port = a.control_port
    if a.pi:
        from ..client.remote import open_remote
        call = NetCaller(open_remote(a.pi, a.at or [], a.key))
        if not (a.status or a.revert):
            print("Over the network: an update that breaks networking without crashing leaves "
                  "the Pi reachable only over USB or by the reset pins")
    else:
        from .control import call
        peer = call("peer", port=port)
        if not peer.get("connected"):
            raise SystemExit("No Pi plugged in (or over the network: --pi PI)")
        if "update" not in (peer.get("peer") or {}).get("caps", []):
            raise SystemExit("The Pi's audiolink can't take updates over the link; put a new image on its card")
    if a.status:
        st = call("update.status", port=port)
        on_card = (st["running"] or "").startswith(PI_STATE_DIR + "/")
        where = "an update" if on_card else "the built-in code"
        print("Running %s (%s)" % (where, _short(st["running_hash"])))
        if st["set_aside"]:
            print("The update on the card kept failing and is set aside")
        detail("%s", st)
        return
    if a.revert:
        call("update.revert", port=port)
        print("Back to the built-in code (restarting)")
        return
    path = a.pyz or (sys.argv[0] if sys.argv[0].endswith(".pyz") else None)
    if not path or not os.path.isfile(path):
        raise SystemExit("Which .pyz? (audiolink update PATH)")
    with open(path, "rb") as f:
        data = f.read()
    digest = hashlib.sha256(data).hexdigest()
    if call("update.status", port=port).get("running_hash") == digest:
        print("The Pi already runs this (%s)" % _short(digest))
        return
    step("Sending %s (%d KB)" % (os.path.basename(path), len(data) // 1024))
    call("update.begin", {"size": len(data), "sha256": digest}, port=port)
    for off in range(0, len(data), CHUNK):
        call("update.chunk", {"offset": off, "data": base64.b64encode(data[off:off + CHUNK]).decode()}, port=port)
    call("update.finish", port=port)
    done("sent")
    step("Restarting the Pi's audiolink")
    end = time.monotonic() + 60
    while time.monotonic() < end:
        time.sleep(2)
        try:
            st = call("update.status", port=port)
        except SystemExit:
            continue
        if st.get("running_hash") == digest:
            done("running %s" % _short(digest))
            return
        if st.get("set_aside"):
            break
    done("didn't come up")
    raise SystemExit("The update didn't start; the Pi runs its built-in code (audiolink update --status)")
