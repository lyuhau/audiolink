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
`audiolink card write`. Going back (audiolink update --back) is a trial too: the other
slot, unless its own trial failed or it's half-written (its INFO's state).

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
INFO = ".image.json"            # in a slot: which image it holds, and its state (below)
# A slot's state: "writing" (half-written until it's done), "ready" (written), "trying"
# (a trial of it began), "kept" (committed), "failed" (its trial fell back). None: from
# before states (an older Pi, a card write): fine.
NOT_OFFERED = ("writing", "trying", "failed")
TRYING = ("The Pi is trying a version: it keeps it or goes back within a few minutes; "
          "check: audiolink update --status")


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


def offered(info, slot, current):
    """Whether `slot` (not running) is a version to go back to."""
    return slot != current and (info or {}).get("state") not in NOT_OFFERED


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
                 run=subprocess.run):
        self.state_dir = state_dir
        self.zip = os.path.join(state_dir, "image.zip")
        self.new = self.zip + ".new"
        self.card = card or platform_env().get("CARD") or "/media/mmcblk0p1"
        self.healthy = healthy                      # () -> reached the host or the relay
        self.run_cmd = run
        self.lock = threading.Lock()
        self.expect = None
        self.trying = False             # a try is scheduled (slot.sh status says trial only once it's booted)
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
        if st:
            self._settle(st)
        if not (st and st["trial"] and not st["committed"]):
            return
        log.info("image: %s is on trial: committing once it reaches its host or relay", st["current"])

        def watch():
            end = clock() + left()
            while clock() < end:
                if self.healthy():
                    p = self.run_cmd(["sh", SLOT_SH, "commit"], capture_output=True, text=True)
                    log.info("image: %s", (p.stdout or p.stderr).strip())
                    if not p.returncode:
                        self._mark(st["current"], "kept")
                    return
                sleep(2)
            log.warning("image: %s didn't get through in time; the platform reboots into the old slot",
                        st["current"])
        threading.Thread(target=watch, name="image-trial", daemon=True).start()

    def _settle(self, st):
        """At start: a trial that ended. The slot running committed was kept; one that was
        "trying" and isn't running fell back: failed."""
        for s in st["slots"]:
            state = self._info(s).get("state")
            if s == st["current"] and not (st["trial"] and not st["committed"]):
                if state in ("trying", "failed"):
                    self._mark(s, "kept")
            elif s != st["current"] and state == "trying":
                log.warning("image: the trial of %s fell back; not offered to go back to", s)
                self._mark(s, "failed", failed_at=int(time.time()))

    def _info(self, slot):
        try:
            with open(os.path.join(self.card, slot, INFO)) as f:
                info = json.load(f)
            return info if isinstance(info, dict) else {}
        except (OSError, ValueError):
            return {}

    def _mark(self, slot, state, **more):
        """Record a slot's state in its INFO (renamed into place)."""
        path = os.path.join(self.card, slot, INFO)
        info = dict(self._info(slot), state=state, **more)
        try:
            with open(path + ".new", "w") as f:
                json.dump(info, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(path + ".new", path)
        except OSError as e:
            log.warning("image: can't mark %s %s: %s", slot, state, e)

    def _try(self, slot):
        """Boot `slot` once, on trial (slot.sh reboots); "trying" until it's kept."""
        before = self._info(slot).get("state")
        self._mark(slot, "trying")
        p = self.run_cmd(["sh", SLOT_SH, "try", slot], capture_output=True, text=True)
        if p.returncode:                        # still here: no trial after all
            log.warning("image: can't try %s: %s", slot, (p.stderr or p.stdout).strip())
            self._mark(slot, before or "ready")
            self.trying = False

    def status(self):
        st = slot_status(self.run_cmd)
        if st is None:
            return {"slots": None}
        return dict(st, images={s: self._info(s) for s in st["slots"]})

    def handle(self, op, args):
        with self.lock:
            if op == "status":
                return self.status()
            if op == "begin":
                size, digest = int(args["size"]), str(args["sha256"])
                if not 0 < size <= MAX_SIZE:
                    raise ValueError("image too big (%d bytes)" % size)
                st = slot_status(self.run_cmd)
                if st is None:
                    raise ValueError("this Pi's card has no image slots: put the image on it with audiolink card write")
                self._not_trying(st)
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
                self._not_trying(slot_status(self.run_cmd))
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
        self.trying = True
        self._later(lambda: self._try(target))
        return {"ok": True, "slot": target, "from": st["current"], "firmware_differs": firmware}

    def _not_trying(self, st):
        """While a trial is open the other slot is the version to go back to: it stays."""
        if self.trying or (st and st["trial"] and not st["committed"]):
            raise ValueError(TRYING)

    def _install(self, names, target, digest, name):
        """The image's slot into `target` (not running), checked; settings.txt renewed.
        -> whether the image's firmware differs from the card's (kept)."""
        from ..card import merge_settings
        dest = os.path.join(self.card, target)
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, INFO), "w") as f:      # never offered until it's all there
            json.dump({"name": name, "sha256": digest, "state": "writing"}, f)
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
            self._mark(target, "ready")
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
        info = self._info(other[0])
        if not offered(info, other[0], st["current"]):
            raise ValueError("the version in %s %s: send a new one instead (audiolink update)" % (
                other[0], "was only partly written" if info.get("state") == "writing" else "didn't work"))
        # On trial, like a new version: kept once it gets through, else back to this one
        log.info("image: back to %s; trying it", other[0])
        self.trying = True
        self._later(lambda: self._try(other[0]))
        return {"ok": True, "slot": other[0], "from": st["current"]}

    def _later(self, fn):
        def go():
            time.sleep(1)               # let the reply go out first
            fn()
        threading.Thread(target=go, daemon=True).start()
