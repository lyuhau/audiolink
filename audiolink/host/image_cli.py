"""`audiolink update [IMAGE] | --back`: a new version (a whole image) for the Pi.

Sends a card image (this version's release image by default, or a zip, or a build
folder like build/card-pi) over the link or the network; the Pi puts it in its spare
slot and boots it once on trial, keeping it only if it gets through to its host or
relay (services/image.py). The command waits and says which happened. Over USB the
link goes to the baseline audio format first, as for code updates (update_cli.py).
"""
import base64
import hashlib
import io
import time
import zipfile

from ..services.image import COMMIT_WITHIN, check_image
from ..ui import detail, done, step

CHUNK = 256 * 1024
IN_FLIGHT = 4                   # chunks sent at once over USB: the link carries several requests at a time
TRIAL_GUARD = 300               # the platform's: an uncommitted trial reboots after this
BOOT = 90                       # a boot, at most, until the link is back


def read_image(path, runner=None):
    """(name, zip bytes) of the image: PATH (a zip, or a build folder), or this version's
    release image (downloaded and checked) when PATH is None."""
    from .. import card
    if path is None:
        from ..paths import cache_dir
        from ..system import Runner
        step("Downloading the card image (audiolink %s)" % card.__version__)
        try:
            path = card.fetch_image(runner or Runner(), cache_dir() / "cards")
        except SystemExit:
            done("failed")
            raise
        done("checked")
    image = card.Image(path)
    if image.path.is_dir():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in image.files():
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, data)
        data = buf.getvalue()
    else:
        with open(str(image.path), "rb") as f:
            data = f.read()
    try:
        check_image(io.BytesIO(data))
    except ValueError as e:
        raise SystemExit("%s: %s" % (image.name, e))
    return image.name, data


def image_command(a, call, port, over_network):
    from .update_cli import restore, to_baseline
    if a.back:
        r = call("image.revert", port=port)
        print("Going back to the version before (%s; rebooting)" % r["slot"].rstrip("/"))
        return
    name, data = read_image(a.image)
    st = call("image.status", port=port)
    if st.get("slots") is None:
        raise SystemExit("The Pi's card has no image slots yet: put this image on it with audiolink card write "
                         "(once; then images go over the link)")
    digest = hashlib.sha256(data).hexdigest()
    running = (st.get("images") or {}).get(st.get("current")) or {}
    if running.get("sha256") == digest:
        print("The Pi already runs this image (%s)" % name)
        return
    if over_network:
        print("Over the network: if the new image doesn't get back online, the Pi returns to "
              "this one by itself (within about %d minutes)" % ((TRIAL_GUARD + BOOT) // 60 + 1))
    # Sent at the link's speed; only the reboot wants the starting format (update_cli.py)
    step("Sending %s (%.1f MB)" % (name, len(data) / 1e6))
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
    r = call("image.finish", port=port)
    if r.get("firmware_differs"):
        print("Its firmware differs from the card's and stays as it is (audiolink card write updates it)")
    old, new = r["from"], r["slot"]
    step("Trying it on the Pi")
    end = time.monotonic() + BOOT + COMMIT_WITHIN + TRIAL_GUARD + BOOT
    seen = None
    while time.monotonic() < end:
        time.sleep(3)
        try:
            seen = call("image.status", port=port)
        except (SystemExit, OSError, ValueError):
            continue
        if seen.get("current") == new and seen.get("committed"):
            done("running it")
            if slowed:
                restore(call, port)
            return
        if seen.get("current") == old and not seen.get("trial"):
            break
    detail("%s", seen)
    if seen and seen.get("current") == old:
        done("didn't work")
        raise SystemExit("The new image didn't get through to this computer or the relay, so the Pi went back "
                         "to the one it had (its trial's logs: audiolink logs prev/boot.log, prev/audiolink.log)")
    done("no answer")
    raise SystemExit("The Pi hasn't come back yet; check: audiolink update --status")


def show_image_status(st):
    if st.get("slots") is None:
        print("Running:  a version from before image slots: the next one goes on with audiolink card write "
              "(once; it keeps the Pi's state)")
        return
    images = st.get("images") or {}
    name = lambda s: "%s (%s)" % ((images.get(s) or {}).get("name") or "as written", s.rstrip("/"))  # noqa: E731
    current = st.get("current")
    print("Running:  %s%s" % (name(current), " on trial: kept once it's back online"
                             if st.get("trial") and not st.get("committed") else ""))
    for s in st["slots"]:
        if s != current:
            print("Previous: %s: audiolink update --back" % name(s))
    if len(st["slots"]) < 2:
        print("Previous: none yet (the next audiolink update keeps this one to go back to)")
