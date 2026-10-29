"""`audiolink update [IMAGE] | --back`: a new version (a whole image) for the Pi.

Sends a card image (this version's release image by default, or a zip, or a build
folder like build/card-pi) over the link or the network; the Pi puts it in its spare
slot and boots it once on trial, keeping it only if it gets through to its host or
relay (services/image.py). The command waits and says which happened. --back tries
the version before the same way. Over USB the
link goes to the baseline audio format first, as for code updates (update_cli.py).

A Pi whose status doesn't say "back": "trial" (v0.14.0) goes back permanently, no
trial, to whatever its other slot holds, even a version whose trial failed: --status
doesn't offer it, and --back wants --permanent.
"""
import base64
import hashlib
import io
import sys
import time

from .. import ui
from ..services.image import (IMAGE_SLOT, TRIAL_SECONDS, TRYING, VERSION_FILE, check_image, offered,
                              version_info, version_name)
from ..ui import detail, done, step

CHUNK = 256 * 1024
IN_FLIGHT = 4                   # chunks sent at once over USB: the link carries several requests at a time
BOOT = 90                       # a boot, at most, until the link is back
OLD_BACK = "this Pi's audiolink goes back without a trial: update it first"
LATER = ("To send an older release instead: audiolink update audiolink-pi-VER.zip "
         "(from the project's Releases)")


def window(st):
    """The Pi's trial window (seconds from its boot), from its image status."""
    w = st.get("trial_seconds")
    return w if isinstance(w, int) and w > 0 else TRIAL_SECONDS


def minutes(secs):
    return max(1, round(secs / 60))


def earlier(info):
    """The version in a slot not running, for people: its version, else "an earlier dev
    build" (a build folder's name says little; -v shows it), else None."""
    info = info or {}
    v = version_name(info)
    return "an earlier dev build" if v and v == info.get("name") else v


def accessory(over_network):
    if not over_network and sys.platform == "darwin":
        from .flavor_cli import warn_accessory
        warn_accessory()


def fell_back(what, over_network):
    """Why a trial went back: the likeliest first."""
    pc = "the computer plugged into the Pi" if over_network else "this computer"
    return ("%s didn't get through to %s or the relay, so the Pi went back to the one it had. Likely:\n"
            "  - %s asked to allow the Pi as a new device and nobody did (macOS: Allow accessory)\n"
            "  - it didn't start right (its trial's logs: audiolink logs prev/boot.log, prev/audiolink.log)"
            % (what, pc, pc))


def read_image(path, runner=None):
    """(name, zip bytes) of the image: PATH (a zip, or a build folder), or this version's
    release image (downloaded and checked) when PATH is None."""
    from .. import card
    if path is None:
        from ..paths import cache_dir
        from ..system import Runner
        step("Downloading the card image (audiolink %s)" % card.__version__)
        try:
            path = card.fetch_image(runner or Runner(), cache_dir() / "cards", give="audiolink update FILE")
        except SystemExit:
            done("failed")
            raise
        done("checked")
    image = card.Image(path)
    data = image.zipped()
    try:
        check_image(io.BytesIO(data))
    except ValueError as e:
        raise SystemExit("%s: %s" % (image.name, e))
    return image.name, data


def image_version(name, data):
    """The version an image (its zip) carries, for people (version_name)."""
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = version_info(z.read(IMAGE_SLOT + VERSION_FILE))
    except (KeyError, zipfile.BadZipFile):
        info = {}
    return version_name(dict(info, name=name)) or name


def _called(label):
    """"version 0.14.0", or a build's name as it is."""
    return "version %s" % label if label[:1].isdigit() else label


def image_command(a, call, port, over_network):
    from .update_cli import restore, to_baseline
    if a.back:
        return back_command(call, port, over_network, getattr(a, "permanent", False))
    st = call("image.status", port=port)
    if st.get("slots") is None:
        raise SystemExit("The Pi's card has no image slots yet: put this image on it with audiolink card write "
                         "(once; then images go over the link)")
    if st.get("trial") and not st.get("committed"):     # the other slot is the one to go back to
        raise SystemExit(TRYING)
    name, data = read_image(a.image)
    digest = hashlib.sha256(data).hexdigest()
    running = (st.get("images") or {}).get(st.get("current")) or {}
    version = image_version(name, data)
    if running.get("sha256") == digest:
        print("The Pi already runs this version (%s)" % version)
        return
    if over_network:
        print("Over the network: if the new image doesn't get back online within %d minutes, the Pi "
              "returns to this one by itself" % minutes(window(st)))
    # Sent at the link's speed; only the reboot wants the starting format (update_cli.py)
    detail("%s", name)
    step("Sending %s (%.1f MB)" % (_called(version), len(data) / 1e6))
    start = time.monotonic()
    call("image.begin", {"size": len(data), "sha256": digest, "name": name}, port=port)

    def send(off):
        call("image.chunk", {"offset": off, "data": base64.b64encode(data[off:off + CHUNK]).decode()}, port=port)
    offsets = range(0, len(data), CHUNK)
    if over_network:                    # one connection: one request at a time
        for off in offsets:
            send(off)
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(IN_FLIGHT) as pool:
            list(pool.map(send, offsets))
    secs = time.monotonic() - start
    done("sent in %ds (%.1f MB/s)" % (secs, len(data) / 1e6 / max(secs, 0.1)))
    slowed = False
    if not over_network:
        _, slowed = to_baseline(call, port)
    try:
        r = call("image.finish", port=port)
    except SystemExit:
        if slowed:                      # refused after all: its old speed again
            restore(call, port)
        raise
    if r.get("firmware_differs"):
        print("Its firmware differs from the card's and stays as it is (audiolink card write updates it)")
    if r.get("usb_settings"):
        from ..card import USB_GONE
        print(USB_GONE)
    if r.get("code_update_gone"):
        print("The code update on the card (audiolink update --code) is gone: the new version runs its own")
    accessory(over_network)
    wait_trial(call, port, r["from"], r["slot"], slowed, fell_back("The new version", over_network),
               window(st), over_network)


def back_command(call, port, over_network, permanent=False):
    """--back: the version before, on trial like a new one. A Pi from before that
    (OLD_BACK) switches for good: only with permanent (--permanent)."""
    from .update_cli import restore, to_baseline
    st = call("image.status", port=port)
    if st.get("slots") is None:
        raise SystemExit("The Pi's card has no image slots: there's no version before to go back to")
    if st.get("trial") and not st.get("committed"):
        raise SystemExit(TRYING)
    images = st.get("images") or {}
    other = [s for s in st["slots"] if s != st.get("current")]
    if not other:
        raise SystemExit("No version before on the Pi to go back to")
    before = earlier(images.get(other[0]))
    detail("%s: %s", other[0], images.get(other[0]))
    if st.get("back") != "trial":
        if not permanent:
            raise SystemExit("This Pi's audiolink (0.14.0 or earlier) goes back for good, with no trial, even to a "
                             "version whose trial failed or that was half-written: if %s doesn't start, nothing "
                             "brings this one back. Better, update the Pi first (audiolink update). %s. Anyway: "
                             "audiolink update --back --permanent" % (before or "the version before", LATER))
        print("Going back to %s for good (no trial)" % (_called(before) if before else "the version before"))
    elif not offered(images.get(other[0]), other[0], st.get("current")):
        raise SystemExit("The version before%s %s: send a new one instead (audiolink update). %s" % (
            " (%s)" % before if before else "",
            "was only partly written" if (images.get(other[0]) or {}).get("state") == "writing" else "didn't work",
            LATER))
    else:
        print("Going back to %s" % (_called(before) if before else "the version before"))
        if over_network:
            print("Over the network: if that version doesn't get back online within %d minutes, the Pi "
                  "returns to this one by itself" % minutes(window(st)))
    accessory(over_network)
    slowed = False
    if not over_network:
        _, slowed = to_baseline(call, port)
    try:
        r = call("image.revert", port=port)
    except SystemExit:
        if slowed:                      # refused after all: its old speed again
            restore(call, port)
        raise
    wait_trial(call, port, r["from"], r["slot"], slowed, fell_back("The version before", over_network),
               window(st), over_network)


def wait_trial(call, port, old, new, slowed, fell_back, trial=TRIAL_SECONDS, over_network=False):
    """Until the trial of `new` is kept or the Pi is back on `old`; says which. The
    Pi on `old` with no trial counts only once its try has begun: it says it's no
    longer "trying" (a try scheduled), or, a Pi that doesn't say, it has dropped off
    once (rebooted)."""
    from .update_cli import restore
    total = BOOT + trial + BOOT                 # its boot, its window, the boot back
    step("Trying it on the Pi (up to %d minutes)" % minutes(total))
    end = time.monotonic() + total
    seen = None
    dropped = False
    while time.monotonic() < end:
        time.sleep(3)
        try:
            seen = call("image.status", port=port)
        except (SystemExit, OSError, ValueError):
            dropped = True
            continue
        if seen.get("current") == new and seen.get("committed"):
            done("running it")
            if slowed:
                restore(call, port)
            return
        if seen.get("current") == old and not seen.get("trial") and (
                seen.get("trying") is False or dropped):
            break
    detail("%s", seen)
    if seen and seen.get("current") == old and (seen.get("trying") is False or dropped):
        done("didn't work")
        if slowed:                      # back and answering: its old speed again
            restore(call, port)
        raise SystemExit(fell_back)
    done("no answer")
    raise SystemExit("The Pi hasn't come back yet. Give it a few minutes more; a version that never started "
                     "goes back when the Pi is unplugged and plugged in again. Then: audiolink update --status "
                     "(to go back: audiolink update --back), or over the network: audiolink update --status "
                     "--pi NAME (--back --pi NAME)")


def show_image_status(st, code=None, peer_version=None):
    """Which version runs and which one --back goes to. code: the Pi's update.status (a
    code update on top: its image's version isn't what runs); peer_version: what the Pi
    said it runs, for a slot whose INFO is from before versions."""
    if st.get("slots") is None:
        print("Running:  a version from before image slots: the next one goes on with audiolink card write "
              "(once; it keeps the Pi's state)")
        return
    from .update_cli import _on_card
    images = st.get("images") or {}
    current = st.get("current")
    running = None if _on_card(code or {}) or not peer_version else {"version": peer_version,
                                                        "build": (code or {}).get("running_build")}

    def more(s):                        # -v: the slot, its image, its build
        info = images.get(s) or {}
        bits = [s] + [str(info[k]) for k in ("name", "state") if info.get(k)]
        if info.get("build"):
            bits.append("build %s" % info["build"])
        return "  [%s]" % ", ".join(bits) if ui.VERBOSE else ""
    print("Running:  %s%s%s" % (version_name(images.get(current), running) or "the version written to its card",
                                " on trial: kept once it's back online"
                                if st.get("trial") and not st.get("committed") else "", more(current)))
    for s in st["slots"]:
        if s == current:
            continue
        state = (images.get(s) or {}).get("state")
        label = earlier(images.get(s)) or "an earlier version"
        print("Previous: %s%s" % ("%s (%s)" % (label, OLD_BACK) if st.get("back") != "trial"
                                  else "%s (audiolink update --back)" % label if offered(images.get(s), s, current)
                                  else "%s: partly written, not offered" % label if state == "writing"
                                  else "%s: didn't work, not offered" % label, more(s)))
    if len(st["slots"]) < 2:
        print("Previous: none yet (the next audiolink update keeps this one to go back to)")
