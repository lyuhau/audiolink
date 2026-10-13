"""Pi side: its logs, over the link (`audiolink logs`). The audio link keeps working
when USB networking doesn't, so this is the way in when ssh isn't. "prev/NAME" is the
boot before's copy on the SD card (the platform's keep_logs; /boot/logs on the older,
pre-platform image)."""
import os

from ..paths import platform_env
from . import Service

LOG_DIR = "/var/log"
LEGACY_PREV_DIR = "/boot/logs/prev"     # the older image's
FILES = ("audiolink.log", "audiolink.log.1", "audiolink.log.2", "audiolink.log.3",
         "app.log", "boot.log", "wifi-up.log", "wifi.log", "ntp.log", "fsck.log", "messages",
         "dropbear.log")


def prev_dir(env=None):
    logs = (platform_env() if env is None else env).get("APP_LOGS")
    return os.path.join(logs, "prev") if logs else LEGACY_PREV_DIR
MAX_LINES = 5000


class LogsService(Service):
    name = "logs"

    def __init__(self, log_dir=LOG_DIR, prev=None):
        self.dir, self.prev = log_dir, prev or prev_dir()

    def _path(self, name):
        if name.startswith("prev/") and name[5:] in FILES:
            return os.path.join(self.prev, name[5:])
        if name in FILES:
            return os.path.join(self.dir, name)
        raise ValueError("no log called %r" % name)

    def handle(self, op, args):
        if op == "list":
            names = list(FILES) + ["prev/" + f for f in FILES]
            return {"files": [f for f in names if os.path.exists(self._path(f))]}
        if op == "tail":
            name = args.get("file") or FILES[0]
            path = self._path(name)
            lines = max(1, min(MAX_LINES, int(args.get("lines") or 200)))
            try:
                with open(path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - lines * 200))
                    text = f.read().decode("utf-8", "replace")
            except FileNotFoundError:
                raise ValueError("%s isn't there (yet)" % name)
            return {"file": name, "text": "\n".join(text.splitlines()[-lines:])}
        return super().handle(op, args)
