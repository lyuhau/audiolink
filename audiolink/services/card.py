"""Pi side: its card, for its owners: a backup of the whole card (`audiolink card backup`
with the Pi connected), and the card's settings (`audiolink settings`).

Backup: the Pi zips its card (card.backup: everything, both image slots and its
state) into a hidden file on the card itself (not RAM: two images and their state
are ~85 MB), and the host reads it in pieces; it's removed when the host is done, or
by the next backup. Settings: settings.txt on the card, as anyone with the card could
edit it; the platform reads it at boot, so changes take effect then. Values are
checked like the platform does (pi-gadget-kit image/settings.sh), against the keys
the image has (/etc/usb-gadget/settings.keys).

Owners only, over USB or the network (endpoint "card").
"""
import base64
import hashlib
import logging
import os
import re
import subprocess
import threading
import time

from ..paths import platform_env
from . import Service
from .access import OWNER

log = logging.getLogger("audiolink")

KEYS = "/etc/usb-gadget/settings.keys"
DEFAULTS = "/etc/usb-gadget/settings.defaults"
MAX_READ = 1024 * 1024
LINE = re.compile(r"^(#?)\s*([A-Za-z0-9_.-]+)\s*=\s*(.*?)\s*$")


def check(kind, value):
    """A setting's value as the platform takes it (normalised), or ValueError with a hint."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    bad = any(ord(c) < 32 for c in value)
    if kind == "hostname" and re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?", value):
        return value
    if kind == "hex16" and re.fullmatch(r"(0[xX])?[0-9A-Fa-f]{1,4}", value):
        return "0x%04x" % int(value[2:] if value[:2] in ("0x", "0X") else value, 16)
    if kind == "onoff":
        v = {"on": "on", "yes": "on", "true": "on", "1": "on", "off": "off", "no": "off", "false": "off", "0": "off"}
        if value in v:
            return v[value]
    if kind == "usbstring" and not bad and len(value.encode()) <= 126:
        return value
    if kind == "text" and not bad:
        return value
    if kind == "flavor" and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
        return value
    raise ValueError({"hostname": "letters, digits and -", "hex16": "a hex ID like 0x1d6b", "onoff": "on or off",
                      "usbstring": "at most 126 characters",
                      "flavor": "a flavor's name: lowercase letters, digits, - and _ (audiolink flavor lists them)"
                      }.get(kind, "one line of text"))


class CardService(Service):
    name = "card"
    caps = ("card",)

    def __init__(self, endpoint=None, card=None, keys=KEYS, defaults=DEFAULTS, reboot=None):
        self.card = card or platform_env().get("CARD") or "/media/mmcblk0p1"
        self.keys_path, self.defaults_path = keys, defaults
        self.reboot = reboot or (lambda: subprocess.run(["reboot"]))
        self.lock = threading.Lock()
        self.job = {"state": "none"}                # the backup being made: running, done (its size, sha256) or error
        if endpoint:
            endpoint.register("card", lambda conn, req: {"result": self.handle(req.get("action"), req)}, OWNER)

    @property
    def backup_path(self):
        from ..card import BACKUP_ON_CARD
        return os.path.join(self.card, BACKUP_ON_CARD)

    def handle(self, op, args):
        if op == "backup":                          # starts it; "progress" says when it's ready
            with self.lock:
                if self.job["state"] != "running":
                    self.job = {"state": "running"}
                    threading.Thread(target=self._backup, args=(bool(args.get("all_images")),),
                                     name="card-backup", daemon=True).start()
            return dict(self.job)
        if op == "progress":
            return dict(self.job)
        if op == "read":
            n = min(int(args.get("size", MAX_READ)), MAX_READ)
            with open(self.backup_path, "rb") as f:
                f.seek(int(args["offset"]))
                return {"data": base64.b64encode(f.read(n)).decode()}
        if op == "done":
            if os.path.exists(self.backup_path):
                os.remove(self.backup_path)
            self.job = {"state": "none"}
            return {"ok": True}
        if op == "settings":
            return self.settings()
        if op == "set":
            with self.lock:
                return self._set(args.get("changes") or {}, bool(args.get("reboot")))
        return super().handle(op, args)

    # ---- backup
    def _backup(self, all_images=False):
        from ..card import backup
        try:
            for p in (self.backup_path, self.backup_path + ".part"):
                if os.path.exists(p):
                    os.remove(p)
            start = time.monotonic()
            n = backup(self.card, self.backup_path, all_images)
            h = hashlib.sha256()
            with open(self.backup_path, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            size = os.path.getsize(self.backup_path)
            log.info("card: backed up %d files (%.1f MB) in %.0fs", n, size / 1e6, time.monotonic() - start)
            self.job = {"state": "done", "files": n, "size": size, "sha256": h.hexdigest()}
        except (OSError, SystemExit) as e:
            log.warning("card: backup failed: %s", e)
            self.job = {"state": "error", "error": str(e)}

    # ---- settings
    def _keys(self):
        """[(key, type, variable)] the image has."""
        out = []
        with open(self.keys_path) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 3 and not line.startswith("#"):
                    out.append(tuple(parts))
        return out

    def _defaults(self):
        out = {}
        try:
            with open(self.defaults_path) as f:
                for line in f:
                    var, eq, value = line.strip().partition("=")
                    if eq and not var.startswith("#"):
                        out[var] = value.strip("'\"")
        except OSError:
            pass
        return out

    def _text(self):
        try:
            with open(os.path.join(self.card, "settings.txt"), encoding="utf-8", errors="replace") as f:
                return f.read().replace("\r", "")
        except OSError:
            return ""

    def settings(self):
        """[{key, type, default, value}]: value is what the card sets (None: the default)."""
        text, defaults = self._text(), self._defaults()
        set_ = {}
        for line in text.splitlines():
            m = LINE.match(line)
            if m and not m.group(1) and m.group(3) != "":
                set_[m.group(2)] = m.group(3)
        return {"settings": [{"key": k, "type": t, "default": defaults.get(v, ""), "value": set_.get(k)}
                             for k, t, v in self._keys()]}

    def _set(self, changes, reboot):
        kinds = {k: t for k, t, _ in self._keys()}
        clean = {}
        for key, value in changes.items():
            if key not in kinds:
                raise ValueError("%s isn't one of this image's settings (audiolink settings lists them)" % key)
            try:
                clean[key] = None if value is None else check(kinds[key], str(value))
            except ValueError as e:
                raise ValueError("%s: %s" % (key, e))
        lines = self._text().splitlines()
        for key, value in clean.items():
            mine = [i for i, l in enumerate(lines) if (LINE.match(l) or [None] * 3)[2] == key and not l.startswith("#")]
            stub = [i for i, l in enumerate(lines) if (LINE.match(l) or [None] * 3)[2] == key and l.startswith("#")]
            for i in reversed(mine[1:] if value is not None else mine):     # one line each, or none to unset
                del lines[i]
                stub = [j - (j > i) for j in stub]
            if value is None:
                continue
            new = "%s = %s" % (key, value)
            if mine:
                lines[mine[0]] = new
            elif stub:
                lines.insert(stub[0] + 1, new)
            else:
                lines.append(new)
        path = os.path.join(self.card, "settings.txt")
        with open(path + ".new", "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(path + ".new", path)
        os.sync()
        log.info("card: settings %s", ", ".join("%s=%s" % kv if kv[1] is not None else "%s unset" % kv[0]
                                                for kv in clean.items()))
        if reboot:
            def later():
                time.sleep(1)           # let the reply go out first
                self.reboot()
            threading.Thread(target=later, daemon=True).start()
        return {"ok": True, "changes": clean}
