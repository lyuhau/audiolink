"""Pi side: a whole new image from the host (`audiolink update`), into a slot.

The card holds images in slot folders (a/, b/; pi-gadget-kit's image/slot.sh): its
config.txt says which one boots. A new image arrives as a card image zip (a release's
audiolink-pi-VER.zip, or a build), goes into the slot that isn't running, and boots
once on trial. It stays only if this daemon, running from it, gets through to its host
or its relay within the platform's trial window (TRIAL_SECONDS from boot; it commits
then); otherwise the platform's guard reboots into the slot that was booting. The Pi's state (audiolink/, settings.txt,
logs/) is shared by the slots; settings.txt is renewed from the new image's, keeping
what was set, and an earlier code update (audiolink update --code) goes, so the new image
runs its own code. The firmware files at the top of the card stay: a new firmware needs
`audiolink card write`.

Owners only, over USB or the network (endpoint "image"), like code updates.
"""
import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import zipfile

from ..paths import PI_STATE_DIR, platform_env
from . import Service
from .access import OWNER

log = logging.getLogger("audiolink")

MAX_SIZE = 256 * 1024 * 1024
SLOT_SH = "/etc/usb-gadget/slot.sh"
SLOTS = ("a/", "b/")
IMAGE_SLOT = "a/"               # where a built image has its slot (pi-gadget-kit image_initramfs.SLOT)
FIRMWARE = ("bootcode.bin", "start.elf", "fixup.dat")
TRIAL_SECONDS = 300             # the platform's guard: app.env TRIAL_SECONDS ([app] trial_seconds), its default
COMMIT_MARGIN = 10              # stop trying this long before the guard reboots
INFO = ".image.json"            # in a slot: which image it holds


def slot_status(run=subprocess.run):
    """slot.sh status -> {"current": "a/", "trial": bool, "committed": bool, "slots": [...]}, or None
    off the platform (or an image without slots)."""
    if not os.path.exists(SLOT_SH):
        return None
    p = run(["sh", SLOT_SH, "status"], capture_output=True, text=True)
    if p.returncode:
        return None
    kv = dict(part.split("=", 1) for part in p.stdout.replace('"', "").split(" slots=")[0].split())
    slots = p.stdout.split('slots="', 1)[1].split('"', 1)[0].split() if 'slots="' in p.stdout else []
    return {"current": kv.get("current"), "trial": kv.get("trial") == "1",
            "committed": kv.get("committed") == "1", "slots": slots}


def check_image(path):
    """A card image zip with its image in a slot -> its names; ValueError saying what's wrong."""
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            bad = z.testzip()
    except (OSError, zipfile.BadZipFile) as e:
        raise ValueError("not a card image (%s)" % e)
    if bad:
        raise ValueError("the image is damaged (%s)" % bad)
    if any(n.startswith(("audiolink/", "logs/")) for n in names):
        raise ValueError("that's a card backup (a Pi's keys, its images), not an image: put it back with "
                         "audiolink card restore")
    if "config.txt" not in names or IMAGE_SLOT + "config.txt" not in names:
        raise ValueError("not an image with slots (no %sconfig.txt): put it on the card with "
                         "audiolink card write" % IMAGE_SLOT)
    return names


def trial_left(env=None, uptime="/proc/uptime"):
    """Seconds this boot's trial has left to commit: the guard's TRIAL_SECONDS counted
    from boot, less COMMIT_MARGIN."""
    env = platform_env() if env is None else env
    try:
        window = int(env.get("TRIAL_SECONDS") or TRIAL_SECONDS)
    except ValueError:
        window = TRIAL_SECONDS
    try:
        with open(uptime) as f:
            up = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        up = 0.0
    return window - up - COMMIT_MARGIN


class ImageService(Service):
    name = "image"
    caps = ("image",)

    def __init__(self, state_dir=PI_STATE_DIR, endpoint=None, healthy=lambda: False, card=None,
                 run=subprocess.run, reboot=None):
        self.state_dir = state_dir
        self.zip = os.path.join(state_dir, "image.zip")
        self.new = self.zip + ".new"
        self.card = card or platform_env().get("CARD") or "/media/mmcblk0p1"
        self.healthy = healthy                      # () -> reached the host or the relay
        self.run_cmd = run
        self.reboot = reboot or (lambda: run(["reboot"]))
        self.lock = threading.Lock()
        self.expect = None
        if endpoint:
            endpoint.register("image", self._remote, OWNER)
            endpoint.register("image_status", lambda conn, req: {"result": self.status()}, OWNER)

    def _remote(self, conn, req):
        if req.get("action") not in ("begin", "chunk", "finish", "status", "revert"):
            return {"error": "image: begin, chunk, finish, status or revert"}
        return {"result": self.handle(req["action"], req)}

    # ---- the trial: commit once this image gets through
    def run(self, sleep=time.sleep, clock=time.monotonic, left=trial_left):
        st = slot_status(self.run_cmd)
        if not (st and st["trial"] and not st["committed"]):
            return
        log.info("image: %s is on trial: committing once it reaches its host or relay", st["current"])

        def watch():
            end = clock() + left()
            while clock() < end:
                if self.healthy():
                    p = self.run_cmd(["sh", SLOT_SH, "commit"], capture_output=True, text=True)
                    log.info("image: %s", (p.stdout or p.stderr).strip())
                    return
                sleep(2)
            log.warning("image: %s didn't get through in time; the platform reboots into the old slot",
                        st["current"])
        threading.Thread(target=watch, name="image-trial", daemon=True).start()

    def status(self):
        st = slot_status(self.run_cmd)
        if st is None:
            return {"slots": None}
        info = {}
        for s in st["slots"]:
            try:
                with open(os.path.join(self.card, s, INFO)) as f:
                    info[s] = json.load(f)
            except (OSError, ValueError):
                info[s] = {}
        return dict(st, images=info)

    def handle(self, op, args):
        with self.lock:
            if op == "status":
                return self.status()
            if op == "begin":
                size, digest = int(args["size"]), str(args["sha256"])
                if not 0 < size <= MAX_SIZE:
                    raise ValueError("image too big (%d bytes)" % size)
                if slot_status(self.run_cmd) is None:
                    raise ValueError("this Pi's card has no image slots: put the image on it with audiolink card write")
                with open(self.new, "wb"):
                    pass
                self.expect = (size, digest, str(args.get("name") or "image"))
                return {"ok": True}
            if op == "chunk":
                if not self.expect:
                    raise ValueError("no image under way")
                with open(self.new, "r+b") as f:
                    f.seek(int(args["offset"]))
                    f.write(base64.b64decode(args["data"]))
                return {"ok": True}
            if op == "finish":
                return self._finish()
            if op == "revert":
                return self._revert()
        return super().handle(op, args)

    def _finish(self):
        if not self.expect:
            raise ValueError("no image under way")
        size, digest, name = self.expect
        self.expect = None
        h = hashlib.sha256()
        with open(self.new, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if os.path.getsize(self.new) != size or h.hexdigest() != digest:
            os.remove(self.new)
            raise ValueError("the image arrived damaged; nothing changed")
        os.replace(self.new, self.zip)
        try:
            names = check_image(self.zip)
            st = slot_status(self.run_cmd)
            target = next(s for s in SLOTS if s != st["current"])
            firmware = self._install(names, target, digest, name)
        except Exception:
            os.remove(self.zip)
            raise
        os.remove(self.zip)
        # An earlier code update (audiolink update --code) would run instead of the new image's code
        for p in ("audiolink.pyz", "audiolink.pyz.bad", "audiolink.pyz.new"):
            p = os.path.join(self.state_dir, p)
            if os.path.exists(p):
                os.remove(p)
        log.info("image: %s in %s; trying it", name, target)
        self._later(lambda: self.run_cmd(["sh", SLOT_SH, "try", target]))
        return {"ok": True, "slot": target, "from": st["current"], "firmware_differs": firmware}

    def _install(self, names, target, digest, name):
        """The image's slot into `target` (not running), checked; settings.txt renewed.
        -> whether the image's firmware differs from the card's (kept)."""
        from ..card import merge_settings
        dest = os.path.join(self.card, target)
        shutil.rmtree(dest, ignore_errors=True)
        with zipfile.ZipFile(self.zip) as z:
            for n in names:
                if not n.startswith(IMAGE_SLOT):
                    continue
                p = os.path.join(dest, *n[len(IMAGE_SLOT):].split("/"))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                data = z.read(n)
                with open(p, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
            for n in names:                     # read back: the card is what boots
                if n.startswith(IMAGE_SLOT):
                    with open(os.path.join(dest, *n[len(IMAGE_SLOT):].split("/")), "rb") as f:
                        if f.read() != z.read(n):
                            raise ValueError("%s didn't write correctly to the card" % n)
            with open(os.path.join(dest, INFO), "w") as f:
                json.dump({"name": name, "sha256": digest}, f)
            if "settings.txt" in names:
                path = os.path.join(self.card, "settings.txt")
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        old = f.read()
                except OSError:
                    old = ""
                text, _ = merge_settings(old, z.read("settings.txt").decode("utf-8", "replace"))
                if text != old:
                    with open(path + ".new", "w", encoding="utf-8") as f:
                        f.write(text)
                    os.replace(path + ".new", path)
            firmware = False
            for n in FIRMWARE:
                try:
                    with open(os.path.join(self.card, n), "rb") as f:
                        firmware = firmware or (n in names and f.read() != z.read(n))
                except OSError:
                    pass
        os.sync()
        return firmware

    def _revert(self):
        st = slot_status(self.run_cmd)
        if st is None:
            raise ValueError("this Pi's card has no image slots")
        other = [s for s in st["slots"] if s != st["current"]]
        if not other:
            raise ValueError("no other image on the card to go back to")
        p = self.run_cmd(["sh", SLOT_SH, "switch", other[0]], capture_output=True, text=True)
        if p.returncode:
            raise ValueError((p.stderr or p.stdout).strip())
        log.info("image: back to %s; rebooting", other[0])
        self._later(self.reboot)
        return {"ok": True, "slot": other[0], "from": st["current"]}

    def _later(self, fn):
        def go():
            time.sleep(1)               # let the reply go out first
            fn()
        threading.Thread(target=go, daemon=True).start()
