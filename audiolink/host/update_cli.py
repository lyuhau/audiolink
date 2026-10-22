"""`audiolink update [IMAGE] | --back | --status [--pi PI]`: a new version for a Pi.

A version is a whole image (host/image_cli.py: tried in the Pi's spare slot, kept if
it comes back). Developers can send just new code instead, hidden `--code [PYZ]`
(`--code --back` undoes it), which is what this file does itself:

it sends the .pyz over the link (USB networking not needed), or with --pi over the network
(owners of the Pi); the Pi keeps it on its SD card and restarts into it
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


class NoOp(SystemExit):
    """The Pi's audiolink doesn't have that service."""


class NetCaller:
    """call() for update requests over the network (client/remote.py), reconnecting
    after the Pi restarts."""

    def __init__(self, remote):
        self.remote = remote

    def __call__(self, op, args=None, port=None):
        service, action = op.split(".", 1)          # update.* or image.*
        for attempt in (0, 1):
            try:
                if self.remote.conn is None:
                    self.remote.open(quiet=True)
                if action == "status":
                    r = self.remote.ask("%s_status" % service)
                    if "error" in r:
                        unknown = r.get("code") == "unknown" or str(r["error"]).startswith("unknown op")
                        raise (NoOp if unknown else SystemExit)(self.remote.refusal(r))
                    return r["result"]
                return self.remote.call(service, dict(args or {}, action=action))["result"]
            except (OSError, ValueError, ConnectionError):
                self.remote.close()
                if attempt:
                    raise SystemExit("Lost the connection to %s" % self.remote.name)


def to_baseline(call, port):
    """Over USB: the link to the baseline audio format. -> (it's there, it was slowed
    down for this)."""
    from .audio_format import at_home

    def status():
        st = call("audio.status", port=port)
        return st, at_home(st) and not (st.get("pi") or {}).get("pending")
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
    from .audio_format import AudioFormat, at_home, human
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
            if not at_home(st) or not (st.get("remembered") or {}).get("best"):
                break
    except (SystemExit, KeyError, ValueError) as e:
        detail("audio: %s", e)
    link = st and AudioFormat.from_json(st["link"])
    done(human(link) if link and not at_home(st) else "stayed at the starting speed (audiolink audio sweep)")


def update_command(a):
    line_buffered()
    port = a.control_port
    code = getattr(a, "code", None)
    if (a.image is not None) + a.back + a.status > 1:
        raise SystemExit("Pick one: an image to send, --back or --status")
    if code is not None and (a.image is not None or a.status):
        raise SystemExit("--code goes alone (or with --back)")
    if a.at and not a.pi:
        raise SystemExit("--at goes with --pi PI (over the network)")
    data = path = None
    if code is not None and not a.back:
        path = _this_pyz() if code == "this" else code
        if not path:
            raise SystemExit("Which .pyz? (audiolink update --code PATH)")
        data = read_pyz(path)
    if a.pi:
        from ..client.remote import open_remote
        call = NetCaller(open_remote(a.pi, a.at or []))
    else:
        from .control import call
        peer = call("peer", port=port)
        if not peer.get("connected"):
            raise SystemExit("No Pi plugged in (or over the network: --pi PI)")
        caps = (peer.get("peer") or {}).get("caps", [])
        if "update" not in caps:
            raise SystemExit("The Pi's audiolink can't take updates over the link; put a new image on its card")
        if code is None and not a.status and "image" not in caps:
            raise SystemExit(no_images(call, port))
    if a.status:
        try:
            st = call("image.status", port=port)
        except (SystemExit, KeyError, ValueError):
            st = None                           # a Pi from before image updates
        from .image_cli import show_image_status
        show_image_status(st or {"slots": None})
        return show_status(call("update.status", port=port))
    if code is None and a.pi:            # no caps over the network: ask its image service
        try:
            call("image.status", port=port)
        except NoOp:
            raise SystemExit(no_images(call, port))
    if code is None:
        from .image_cli import image_command
        return image_command(a, call, port, bool(a.pi))
    if a.back:
        r = call("update.revert", port=port)
        print("The Pi already runs its image's code" if r.get("already") else "Back to the image's code (restarting)")
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
        "an earlier update" if _on_card(st) else "its image's code"))


def no_images(call, port):
    """Why a Pi can't take versions: a code update from before them running from its
    card, or a card from before slots."""
    try:
        st = call("update.status", port=port)
    except (SystemExit, KeyError, ValueError):
        st = {}
    if _on_card(st):
        return ("The Pi runs a code update from its card that can't take versions: drop it with "
                "audiolink update --code --back, then audiolink update (if it still can't, its card "
                "needs audiolink card write once)")
    return ("The Pi's card is from before versions went over the link: put this one on it "
            "once with audiolink card write (it keeps the Pi's state); after that, audiolink update")


def _on_card(st):
    return (st.get("running") or "").startswith(PI_STATE_DIR + "/")


def show_status(st):
    from .. import BUILD
    from ..services.update import pyz_build
    path = _this_pyz()
    mine, theirs = (path and pyz_build(path)) or BUILD, st.get("running_build")
    if not (mine and theirs):           # a pyz from before build ids: by file hash
        mine, theirs = path and _file_hash(path), st.get("running_hash")
    same = ""
    if mine and theirs:
        same = ", the same as this computer's" if theirs == mine else ", not this computer's"
    print("Code:     %s (%s%s)" % ("an update on the card" if _on_card(st) else "the image's",
                                "build %s" % st["running_build"] if st.get("running_build")
                                else _short(st.get("running_hash")), same))
    if _on_card(st):
        print("          (back to the image's: audiolink update --code --back)")
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
