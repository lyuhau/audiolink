"""Where audiolink keeps things on each kind of machine."""
import os
import sys
from pathlib import Path


def config_dir():
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
PI_STATE_DIR = "/var/lib/audiolink"     # RAM in today's image; on the SD card once there is one


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
