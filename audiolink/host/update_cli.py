"""`audiolink update [PYZ] | --revert | --status [--pi PI]`: new code for a Pi.

Sends the .pyz over the link (USB networking not needed), or with --pi over the network
(debug access, services/access.py); the Pi keeps it on its SD card and restarts into it
(services/update.py). Default: the .pyz this command runs from.

Over USB the link goes to the baseline audio format first: a daemon restarting in a
faster format re-presents the gadget, which it does at most once per RESET_GAP
(services/audio_format.py), so the link could stay down for minutes. Once the new
code runs, the host goes back to its format (auto: the fastest it knows; restore()).
"""
import base64
import hashlib
import io
import os
import sys
import time
import zipfile

from ..paths import PI_STATE_DIR
from ..services.audio_format import RESET_GAP
from ..services.update import MAX_SIZE
from ..ui import detail, done, line_buffered, step

CHUNK = 48 * 1024
COME_BACK = 60                  # seconds a restart at the baseline format takes, at most


def _short(h):
    return h[:12] if h else "none"


def _this_pyz():
    return sys.argv[0] if sys.argv and sys.argv[0].endswith(".pyz") else None


def read_pyz(path):
    """The .pyz's bytes, or SystemExit saying what's wrong with it (before any Pi is asked)."""
    if not os.path.isfile(path):
        raise SystemExit("No file %s" % path)
    with open(path, "rb") as f:
        data = f.read()
    if not data:
        raise SystemExit("%s is empty" % path)
    if len(data) > MAX_SIZE:
        raise SystemExit("%s is too big for the Pi (%d MB; at most %d)" % (path, len(data) >> 20, MAX_SIZE >> 20))
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            ok = "__main__.py" in z.namelist()
    except zipfile.BadZipFile:
        ok = False
    if not ok:
        raise SystemExit("%s isn't an audiolink .pyz" % path)
    return data


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
                if action == "status":              # allowed with debug off (after a restart)
                    r = self.remote.ask("update_status")
                    if r.get("code") == "not_paired" and self.remote.key:   # a Pi before keys could
                        r = self.remote.ask("update", action="status")
                    if "error" in r:
                        raise SystemExit(self.remote.refusal(r))
                    return r["result"]
                return self.remote.call("update", dict(args or {}, action=action))["result"]
            except (OSError, ValueError, ConnectionError):
                self.remote.close()
                if attempt:
                    raise SystemExit("Lost the connection to %s" % self.remote.name)


def to_baseline(call, port):
    """Over USB: the link to the baseline audio format. -> (it's there, it was slowed
    down for this)."""
    from .audio_format import BASELINE, AudioFormat

    def status():
        st = call("audio.status", port=port)
        return st, AudioFormat.from_json(st["link"]) == BASELINE and not (st.get("pi") or {}).get("pending")
    try:
        st, ok = status()
        if ok:
            return True, False
        step("Slowing the link to its starting speed for the restart")
        end = time.monotonic() + 120
        while st["running"] and time.monotonic() < end:     # a negotiation under way: let it end
            time.sleep(1)
            st, ok = status()
        if not ok:
            call("audio.run", {"format": "baseline"}, port=port)
            while time.monotonic() < end:
                time.sleep(1)
                st, ok = status()
                if not st["running"]:
                    break
    except (SystemExit, KeyError, ValueError) as e:
        detail("audio: %s", e)
        ok = False
    done("done" if ok else "couldn't")
    return ok, True


def restore(call, port, tries=2, wait=180):
    """After the restart: the host's own format again (a negotiation the reconnect
    started may be under way, or may have given up while the Pi was coming up)."""
    from .audio_format import BASELINE, AudioFormat, human
    step("Speeding the link back up")
    st = None
    try:
        for _ in range(tries):
            r = call("audio.resume", port=port)
            if r.get("mode") == "baseline":
                break
            end = time.monotonic() + wait
            while time.monotonic() < end:
                st = call("audio.status", port=port)
                if not st["running"]:
                    break
                time.sleep(1)
            st = call("audio.status", port=port)
            if AudioFormat.from_json(st["link"]) != BASELINE or not (st.get("remembered") or {}).get("best"):
                break
    except (SystemExit, KeyError, ValueError) as e:
        detail("audio: %s", e)
    link = st and AudioFormat.from_json(st["link"])
    done(human(link) if link and link != BASELINE else "stayed at the starting speed (audiolink audio sweep)")


def update_command(a):
    line_buffered()
    port = a.control_port
    if (a.pyz is not None) + a.revert + a.status > 1:
        raise SystemExit("Pick one: a .pyz to send, --revert or --status")
    if (a.key or a.at) and not a.pi:
        raise SystemExit("--key and --at go with --pi PI (over the network)")
    data = path = None
    if not (a.status or a.revert):
        path = a.pyz or _this_pyz()
        if not path:
            raise SystemExit("Which .pyz? (audiolink update PATH)")
        data = read_pyz(path)
    if a.pi:
        from ..client.remote import open_remote
        call = NetCaller(open_remote(a.pi, a.at or [], a.key))
    else:
        from .control import call
        peer = call("peer", port=port)
        if not peer.get("connected"):
            raise SystemExit("No Pi plugged in (or over the network: --pi PI)")
        if "update" not in (peer.get("peer") or {}).get("caps", []):
            raise SystemExit("The Pi's audiolink can't take updates over the link; put a new image on its card")
    if a.status:
        return show_status(call("update.status", port=port))
    if a.revert:
        r = call("update.revert", port=port)
        print("The Pi already runs its built-in code" if r.get("already") else "Back to the built-in code (restarting)")
        return
    digest = hashlib.sha256(data).hexdigest()
    if call("update.status", port=port).get("running_hash") == digest:
        print("The Pi already runs this (%s)" % _short(digest))
        return
    wait, slowed = COME_BACK, False
    if a.pi:
        print("Over the network: an update that breaks networking without crashing leaves "
              "the Pi reachable only over USB or by the reset pins")
    else:
        ok, slowed = to_baseline(call, port)
        if not ok:
            wait = RESET_GAP + 30       # the restart may wait for its gadget reset
    step("Sending %s (%d KB)" % (os.path.basename(path), len(data) // 1024))
    call("update.begin", {"size": len(data), "sha256": digest}, port=port)
    for off in range(0, len(data), CHUNK):
        call("update.chunk", {"offset": off, "data": base64.b64encode(data[off:off + CHUNK]).decode()}, port=port)
    call("update.finish", port=port)
    done("sent")
    step("Restarting the Pi's audiolink")
    end = time.monotonic() + wait
    st = None
    while time.monotonic() < end:
        time.sleep(2)
        try:
            st = call("update.status", port=port)
        except SystemExit:
            continue
        if st.get("running_hash") == digest:
            done("running %s" % _short(digest))
            if slowed:
                restore(call, port)
            return
        if st.get("set_aside"):
            break
    if st is None:
        done("not back yet")
        raise SystemExit("The Pi hasn't come back yet (it can take a couple of minutes after a fast audio "
                         "format); check: audiolink update --status")
    done("didn't come up")
    detail("%s", st)
    raise SystemExit("The update didn't start%s; the Pi runs %s (audiolink update --status)" % (
        " (it kept failing and is set aside)" if st.get("set_aside") else "",
        "an earlier update" if _on_card(st) else "its built-in code"))


def _on_card(st):
    return (st.get("running") or "").startswith(PI_STATE_DIR + "/")


def show_status(st):
    mine = _this_pyz()
    mine = mine and _file_hash(mine)
    same = ""
    if mine and st.get("running_hash"):
        same = ", the same as this computer's" if st["running_hash"] == mine else ", not this computer's"
    print("Running %s (%s%s)" % ("an update" if _on_card(st) else "the built-in code",
                                _short(st.get("running_hash")), same))
    if st.get("set_aside"):
        print("The update on the card kept failing and is set aside")
    elif st.get("update") and not _on_card(st):
        print("An update is on the card but isn't running (the reset pins may be shorted)")
    detail("%s", st)


def _file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None
