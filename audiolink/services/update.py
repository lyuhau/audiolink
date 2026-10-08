"""Pi side: new code from the host (`audiolink update`), kept on the SD card.

The image carries a built-in audiolink.pyz; an update goes to the card's audiolink/
folder and pi/start-audiolink.sh runs it instead. An update that keeps dying soon
after starting is set aside (.bad) and the built-in copy runs again, so a bad update
can't take the Pi out of reach. `audiolink update --revert` removes it.

Only the host the Pi is plugged into can send one (these are link requests).
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

log = logging.getLogger("audiolink")

MAX_SIZE = 8 * 1024 * 1024
BUILT_IN = "/root/audiolink.pyz"


def pyz_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


class UpdateService(Service):
    name = "update"
    caps = ("update",)

    def __init__(self, state_dir=PI_STATE_DIR, restart=None):
        self.path = os.path.join(state_dir, "audiolink.pyz")
        self.new = self.path + ".new"
        self.restart = restart or self._exit        # () -> None, after the reply has gone
        self.lock = threading.Lock()
        self.expect = None                          # (size, sha256) of the upload under way

    def status(self):
        running = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else None
        return {"running": running, "running_hash": pyz_hash(running) if running else None,
                "update": os.path.exists(self.path), "update_hash": pyz_hash(self.path),
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
                for p in (self.path, self.path + ".bad", self.new):
                    if os.path.exists(p):
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
                if "__main__.py" not in z.namelist() or z.testzip() is not None:
                    raise zipfile.BadZipFile("no __main__.py, or damaged")
        except zipfile.BadZipFile as e:
            os.remove(self.new)
            raise ValueError("not an audiolink .pyz (%s)" % e)
        with open(self.new, "rb") as f:
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
        os._exit(0)                     # pi/start-audiolink.sh starts the new copy
