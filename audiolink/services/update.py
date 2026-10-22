"""Pi side: new code from the host (`audiolink update --code`, for developers), kept on the SD card.

The image carries a built-in audiolink.pyz (/opt/audiolink/, next to
start-audiolink.sh); an update goes to the card's audiolink/ folder (the state folder) and
pi/start-audiolink.sh runs it instead. An update that keeps dying soon
after starting is set aside (.bad) and the built-in copy runs again, so a bad update
can't take the Pi out of reach. `audiolink update --code --back` removes it.

Owners only, over USB (link requests, from an owner's host: services/access.py) or
over the network (endpoint "update", `audiolink update --code --pi PI`): new code runs as root.
"""
import base64
import hashlib
import logging
import os
import sys
import threading
import time
import zipfile

from ..paths import PI_STATE_DIR
from . import Service
from .access import OWNER, Refused
from .image import slot_status

log = logging.getLogger("audiolink")

MAX_SIZE = 8 * 1024 * 1024
RESTART = 75                    # exit status: "start me again now" (pi/start-audiolink.sh)
IMAGE_CODE = "audiolink/services/image.py"      # in a .pyz: code that knows image slots
# Said by whatever program sent it (an old one shows it as is): its code would run without
# the image service, and the Pi would look like a card from before slots
TOO_OLD = ("That audiolink is too old for this Pi (its card has versions, which that code can't keep): "
           "get the new audiolink on this computer, then audiolink update. Nothing changed")


def pyz_build(path):
    """The build id a .pyz carries (audiolink/_build.py), or None (none, or not a pyz)."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            return z.read("audiolink/_build.py").decode().split('"')[1]
    except (OSError, KeyError, IndexError, zipfile.BadZipFile, TypeError):
        return None


def pyz_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


class UpdateService(Service):
    name = "update"
    caps = ("update",)

    def __init__(self, state_dir=PI_STATE_DIR, restart=None, endpoint=None, slots=slot_status):
        self.path = os.path.join(state_dir, "audiolink.pyz")
        self.new = self.path + ".new"
        self.restart = restart or self._exit        # () -> None, after the reply has gone
        self.slots = slots                          # () -> slot status, or None (a card from before slots)
        self.lock = threading.Lock()
        self.expect = None                          # (size, sha256) of the upload under way
        if endpoint:
            endpoint.register("update", self._remote, OWNER)
            endpoint.register("update_status", lambda conn, req: {"result": self.status()}, OWNER)

    def _remote(self, conn, req):
        if req.get("action") not in ("begin", "chunk", "finish", "status", "revert"):
            return {"error": "update: begin, chunk, finish, status or revert"}
        return {"result": self.handle(req["action"], req)}

    def status(self):
        running = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else None
        return {"running": running, "running_hash": pyz_hash(running) if running else None,
                "running_build": pyz_build(running) if running else None,
                "update": os.path.exists(self.path), "update_hash": pyz_hash(self.path),
                "update_build": pyz_build(self.path),
                "set_aside": os.path.exists(self.path + ".bad")}

    def handle(self, op, args):
        with self.lock:
            if op == "status":
                return self.status()
            if op == "begin":
                size, digest = int(args["size"]), str(args["sha256"])
                if not 0 < size <= MAX_SIZE:
                    raise ValueError("update too big (%d bytes)" % size)
                with open(self.new, "wb"):
                    pass
                self.expect = (size, digest)
                return {"ok": True}
            if op == "chunk":
                if not self.expect:
                    raise ValueError("no update under way")
                data = base64.b64decode(args["data"])
                with open(self.new, "r+b") as f:
                    f.seek(int(args["offset"]))
                    f.write(data)
                return {"ok": True}
            if op == "finish":
                return self._finish()
            if op == "revert":
                found = [p for p in (self.path, self.path + ".bad", self.new) if os.path.exists(p)]
                if not found and not args.get("restart"):
                    return {"ok": True, "already": True}    # nothing to undo: no restart
                for p in found:
                    os.remove(p)
                log.info("update: removed; the built-in copy runs from the next start")
                self._restart_soon()
                return {"ok": True}
        return super().handle(op, args)

    def _finish(self):
        if not self.expect:
            raise ValueError("no update under way")
        size, digest = self.expect
        self.expect = None
        with open(self.new, "rb") as f:
            data = f.read()
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            os.remove(self.new)
            raise ValueError("the update arrived damaged; nothing changed")
        try:
            with zipfile.ZipFile(self.new) as z:
                names = z.namelist()
                if "__main__.py" not in names or z.testzip() is not None:
                    raise zipfile.BadZipFile("no __main__.py, or damaged")
        except zipfile.BadZipFile as e:
            os.remove(self.new)
            raise ValueError("not an audiolink .pyz (%s)" % e)
        if IMAGE_CODE not in names and self.slots() is not None:
            os.remove(self.new)
            raise Refused("too_old", TOO_OLD)
        with open(self.new, "r+b") as f:        # Windows won't fsync a read-only handle
            os.fsync(f.fileno())
        os.replace(self.new, self.path)
        if os.path.exists(self.path + ".bad"):
            os.remove(self.path + ".bad")
        log.info("update: installed %s; restarting", digest[:12])
        self._restart_soon()
        return {"ok": True, "sha256": digest}

    def _restart_soon(self):
        def later():
            time.sleep(1)               # let the reply go out first
            self.restart()
        threading.Thread(target=later, daemon=True).start()

    @staticmethod
    def _exit():
        os._exit(RESTART)               # pi/start-audiolink.sh starts the new copy at once
