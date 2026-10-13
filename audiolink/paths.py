"""Where audiolink keeps things on each kind of machine."""
import json
import logging
import os
import shlex
import sys
import threading
import time
import zlib
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
PI_STATE_DIR = "/var/lib/audiolink"     # the SD card's audiolink/ folder, or RAM without a card

# The usb platform (config/pi.toml) says where things are in its app.env, which it
# also puts in [app] run's environment. Not there: the older image (pi/init).
APP_ENV = "/etc/usb-gadget/app.env"
PLATFORM_KEYS = ("CARD", "APP_STATE", "APP_LOGS", "GADGET", "GADGET_SH", "GADGET_FUNCTIONS")


def platform_env(path=APP_ENV, environ=None):
    """The platform's app.env as a dict, the environment's values winning (e.g. a
    daemon restarted over ssh has only the file). {} off the platform."""
    env = {}
    try:
        with open(path) as f:
            for line in f:
                key, eq, value = line.strip().partition("=")
                if eq and not key.startswith("#"):
                    try:
                        env[key] = " ".join(shlex.split(value))
                    except ValueError:
                        pass
    except OSError:
        pass
    environ = os.environ if environ is None else environ
    env.update({k: environ[k] for k in PLATFORM_KEYS if environ.get(k)})
    return env


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


# ---- the Pi's state on its FAT card: two slots, overwritten in place
SLOT_SIZE = 64 * 1024
SLOT_MAGIC = b"AL1"


def _slot_paths(path):
    return [str(path) + ".a", str(path) + ".b"]


def _read_slot(slot):
    """(seq, data) of a valid slot, or None."""
    try:
        with open(slot, "rb") as f:
            raw = f.read(SLOT_SIZE)
        head, _, rest = raw.partition(b"\n")
        magic, seq, length, crc = head.split(b" ")
        body = rest[:int(length)]
        if magic != SLOT_MAGIC or len(body) != int(length) or zlib.crc32(body) != int(crc):
            return None
        return int(seq), json.loads(body.decode())
    except (OSError, ValueError):
        return None


def load_state(path, default=None):
    """The newest valid copy of state saved with save_state; falls back to a plain
    JSON file at `path` (written before slots existed), then to `default`."""
    found = [r for r in (_read_slot(s) for s in _slot_paths(path)) if r]
    if found:
        return max(found, key=lambda r: r[0])[1]
    return load_json(path, default)


def save_state(path, data):
    """Save state so that losing power at any moment leaves the previous or the new
    copy. FAT has no journal, and a rename (save_json) changes its directory and
    allocation table, which a power cut can leave half done. Instead there are two
    fixed-size slot files, created once; each save overwrites the older slot in place
    (same size, same clusters: only file data changes) with a sequence number and a
    checksum, and load_state takes the newest slot that checks out."""
    body = json.dumps(data, sort_keys=True).encode()
    slots = _slot_paths(path)
    current = [_read_slot(s) for s in slots]
    seqs = [r[0] if r else -1 for r in current]
    target = slots[0] if seqs[0] <= seqs[1] else slots[1]
    record = b"%s %d %d %d\n" % (SLOT_MAGIC, max(seqs) + 1, len(body), zlib.crc32(body)) + body
    if len(record) > SLOT_SIZE:
        raise ValueError("state too large for a slot (%d bytes)" % len(record))
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    for slot in slots:
        if not os.path.exists(slot) or os.path.getsize(slot) != SLOT_SIZE:
            with open(slot, "wb") as f:     # created once, full size
                f.write(bytes(SLOT_SIZE))
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(slot, 0o600)
            except OSError:
                pass
    with open(target, "r+b") as f:
        f.write(record + bytes(SLOT_SIZE - len(record)))
        f.flush()
        os.fsync(f.fileno())
    if sys.platform.startswith("linux"):
        os.sync()
