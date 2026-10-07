"""Where audiolink keeps things on each kind of machine."""
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path


def config_dir():
    if os.environ.get("AUDIOLINK_CONFIG_DIR"):         # e.g. to act as a second machine
        return Path(os.environ["AUDIOLINK_CONFIG_DIR"])
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "audiolink"


def cache_dir():
    """Per-user cache directory for audiolink (extracted libraries, browser profile)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "audiolink"


# ---- the Pi
PI_STATE_DIR = "/var/lib/audiolink"     # the SD card's audiolink/ folder (pi/init), or RAM without a card


def is_persistent(path):
    """False if `path` lives on a RAM filesystem (the RAM-only boot image), so
    anything written there is lost at reboot."""
    try:
        with open("/proc/mounts") as f:
            mounts = [line.split()[:3] for line in f]
    except OSError:
        return True                     # not Linux: assume a normal disk
    real = os.path.realpath(path)
    best_mnt, best_type = "", ""
    for _, mnt, fstype in mounts:
        inside = real == mnt or real.startswith(mnt.rstrip("/") + "/")
        if inside and len(mnt) > len(best_mnt):
            best_mnt, best_type = mnt, fstype
    return best_type not in ("rootfs", "tmpfs", "ramfs")


def load_json(path, default=None):
    """Read a JSON file written by save_json. Missing: `default`. Unreadable or damaged
    (a failing card, a write cut short): retried, then moved aside to PATH.unreadable
    and `default` returned, so that saving afresh can't destroy what might be recovered."""
    path = str(path)
    err = None
    for attempt in range(3):
        try:
            with open(path) as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except (OSError, ValueError) as e:
            err = e
            time.sleep(0.5)
    logging.getLogger("audiolink").error("can't read %s (%s); moved it aside to %s.unreadable", path, err, path)
    try:
        os.replace(path, path + ".unreadable")
    except OSError:
        pass
    return default


def _clean_stale_tmp(path):
    """Temp files a power cut left behind (other writers' recent ones are in use)."""
    d, base = os.path.split(path)
    try:
        for name in os.listdir(d or "."):
            if name.startswith(base + ".tmp-"):
                f = os.path.join(d, name)
                if time.time() - os.path.getmtime(f) > 3600:
                    os.remove(f)
    except OSError:
        pass


def save_json(path, data, indent=None, sort_keys=False):
    """Replace a JSON file so that power loss leaves either the old or the new one.
    The Pi's state lives on a FAT card and the Pi can lose power (or be reset) at any
    moment: the data and the rename both have to reach the card before we return."""
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())    # writers mustn't share one
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent, sort_keys=sort_keys)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    _clean_stale_tmp(path)
    if hasattr(os, "O_DIRECTORY"):
        try:
            fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)            # the rename lives in the directory
            finally:
                os.close(fd)
        except OSError:
            pass
    if sys.platform.startswith("linux"):
        os.sync()                       # FAT: make sure the directory entry and FAT are written
