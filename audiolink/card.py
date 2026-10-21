"""The Pi's card, written through the Pi itself (`audiolink setup`, with the Pi on USB),
and backed up and restored (`audiolink card backup|restore`).

A Pi whose card has no boot files (none in, blank, unreadable) waits in its USB boot mode
("BCM2710 Boot", 0a5c:2764). rpiboot (Raspberry Pi's usbboot, installed separately) sends
it a small firmware that shows its card to this computer as a disk ("msd" mode,
0a5c:0001); setup erases that disk, unzips the card image onto it (the release's
audiolink-pi-VER.zip, checked against its SHA256SUMS), ejects it, and the Pi starts from
it once it's replugged.

`audiolink card write` puts an image on the card: on an audiolink card it replaces the
image's files and keeps the Pi's state (KEEP), with its settings.txt renewed from the image's;
with --wipe, or on a blank card, it erases the card first. Anything else on the card (a
Raspberry Pi OS, another project) is only ever erased, and only when asked (--wipe).
A backup is a zip of everything on the card, so it's also an image: restoring it is
writing it with --wipe.

macOS only for now (diskutil). Elsewhere: write it by hand (README).
"""
import hashlib
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

from . import __version__
from .ui import detail, done, step

REPO = "lyuhau/audiolink"
BOOT_ROM = ("0a5c", ("2763", "2764"))       # the Pi's boot ROM, waiting for rpiboot
MSD = ("0a5c", ("0001",))                   # rpiboot's firmware: its card as a disk
LABEL = "AUDIOPI"
MAX_CARD = 512 * 10 ** 9                    # bigger than any card: not the Pi's
INSTALL_RPIBOOT = {
    "darwin": "brew install rpiboot",
    "linux": "sudo apt install rpiboot (or build from github.com/raspberrypi/usbboot)",
    "win32": "the installer from github.com/raspberrypi/usbboot",
}


class CardError(SystemExit):
    pass


# ---- what's on USB
def usb_devices(runner, platform=sys.platform):
    """[(vendor, product, name)] for USB devices, ids as 4 lowercase hex digits."""
    if platform == "darwin":
        _, out = runner.run(["ioreg", "-p", "IOUSB", "-l", "-w0"])
        found, cur = [], {}
        for line in out.splitlines():
            m = re.search(r'"(idVendor|idProduct|USB Product Name)" = (.*)$', line)
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip()
            cur[key] = val.strip('"') if key == "USB Product Name" else "%04x" % int(val)
            if len(cur) == 3:
                found.append((cur["idVendor"], cur["idProduct"], cur["USB Product Name"]))
                cur = {}
        return found
    if platform.startswith("linux"):
        found = []
        for dev in Path("/sys/bus/usb/devices").glob("*"):
            try:
                vid, pid = (dev / "idVendor").read_text().strip(), (dev / "idProduct").read_text().strip()
            except OSError:
                continue
            try:
                name = (dev / "product").read_text().strip()
            except OSError:
                name = ""
            found.append((vid, pid, name))
        return found
    return []


def usb_state(runner, platform=sys.platform, hints=None):
    """The Pi as USB shows it: "pi" (running audiolink: by its USB ID, or else its
    name), "rom" (waiting in its boot mode), "msd" (its card shown as a disk) or None."""
    from .wire.usb_audio import NAME_HINTS, known_ids
    hints = NAME_HINTS if hints is None else hints
    known = known_ids()
    devs = usb_devices(runner, platform)
    if any((int(v, 16), int(p, 16)) in known or any(h in name.lower() for h in hints) for v, p, name in devs):
        return "pi"
    for state, (vid, pids) in (("rom", BOOT_ROM), ("msd", MSD)):
        if any(v == vid and p in pids for v, p, _ in devs):
            return state
    return None


# ---- rpiboot
def rpiboot_command(platform=sys.platform):
    """How to run rpiboot to show the Pi's card as a disk, or None if it isn't installed."""
    exe = shutil.which("rpiboot")
    if not exe:
        return None
    real = Path(os.path.realpath(exe))
    for d in (real.parent.parent / "share" / "rpiboot" / "msd", Path("/usr/share/rpiboot/msd"),
              Path("/usr/local/share/rpiboot/msd")):
        if (d / "bootcode.bin").exists():
            cmd = [exe, "-d", str(d)]
            break
    else:
        cmd = [exe]                     # its built-in default: mass storage
    if platform.startswith("linux") and os.geteuid() != 0:
        cmd = ["sudo"] + cmd
    return cmd


def install_hint(platform=sys.platform):
    return INSTALL_RPIBOOT.get("linux" if platform.startswith("linux") else platform, INSTALL_RPIBOOT["linux"])


# ---- the card image
def image_name(version=__version__):
    return "audiolink-pi-%s.zip" % version


def release_url(name, version=__version__):
    return "https://github.com/%s/releases/download/v%s/%s" % (REPO, version, name)


def sha256(path):
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_image(runner, folder, version=__version__):
    """The release's card image for this version, downloaded (once) and checked -> path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    name = image_name(version)
    sums, zpath = folder / ("SHA256SUMS-%s" % version), folder / name
    status, out = runner.run(["curl", "-fsSL", "--retry", "2", "-o", str(sums),
                              release_url("SHA256SUMS", version)], timeout=60)
    if status:
        raise CardError("Couldn't get the card image for audiolink %s from the release (%s); give one: "
                        "--card-image ZIP" % (version, (out.strip().splitlines() or ["no answer"])[-1]))
    want = next((ln.split()[0] for ln in sums.read_text().splitlines() if ln.split()[-1:] == [name]), None)
    if not want:
        raise CardError("audiolink %s's release has no %s; give one: --card-image ZIP" % (version, name))
    if not (zpath.exists() and sha256(zpath) == want):
        status, out = runner.run(["curl", "-fsSL", "--retry", "2", "-o", str(zpath) + ".part",
                                  release_url(name, version)], timeout=900)
        if status:
            raise CardError("Couldn't download %s: %s" % (name, out.strip()))
        os.replace(str(zpath) + ".part", str(zpath))
        if sha256(zpath) != want:
            zpath.unlink()
            raise CardError("%s arrived damaged (checksum mismatch); try again" % name)
    return zpath


class Image:
    """A card image: a zip (a release's, a backup) or a folder (a build, build/card-pi)."""

    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.name
        if self.path.is_dir():
            self.names = sorted(os.path.relpath(os.path.join(r, n), str(self.path)).replace(os.sep, "/")
                                for r, ds, ns in os.walk(str(self.path)) for n in ns
                                if not _macos(n) and not any(_macos(p) or p in SKIP for p in
                                                             os.path.relpath(r, str(self.path)).split(os.sep)))
        else:
            try:
                with zipfile.ZipFile(str(self.path)) as z:
                    self.names = sorted(n for n in z.namelist() if not n.endswith("/"))
            except (OSError, zipfile.BadZipFile) as e:
                raise CardError("%s isn't a card image (%s)" % (path, e))
        if "config.txt" not in self.names or "settings.txt" not in self.names:
            raise CardError("%s isn't an audiolink card image (no config.txt, settings.txt)" % path)

    def read(self, name):
        if self.path.is_dir():
            return (self.path / name).read_bytes()
        with zipfile.ZipFile(str(self.path)) as z:
            return z.read(name)

    def files(self):
        """(name, bytes) for every file, reading the zip once."""
        if self.path.is_dir():
            for n in self.names:
                yield n, (self.path / n).read_bytes()
            return
        with zipfile.ZipFile(str(self.path)) as z:
            for n in self.names:
                yield n, z.read(n)


def check_image(path):
    return Image(path)


def _macos(name):
    return name.startswith("._") or name == ".DS_Store"


# ---- disks (macOS)
def external_disks(runner):
    """{"/dev/diskN": bytes} for external physical disks."""
    _, out = runner.run(["diskutil", "list", "external", "physical"])
    disks = {}
    for dev in re.findall(r"^(/dev/disk\d+) \(external, physical\)", out, re.M):
        _, info = runner.run(["diskutil", "info", dev])
        m = re.search(r"Disk Size:.*\((\d+) Bytes\)", info)
        disks[dev] = int(m.group(1)) if m else 0
    return disks


def write(runner, disk, image, sleep=time.sleep):
    """Erase disk (FAT32, MBR), put image on it, check every file, eject it -> files."""
    image = image if isinstance(image, Image) else Image(image)
    status, out = runner.run(["diskutil", "eraseDisk", "FAT32", LABEL, "MBRFormat", disk], timeout=300)
    if status:
        raise CardError("Couldn't erase the card: %s" % out.strip())
    mount = volume_of(runner, disk, sleep)
    if not mount:
        raise CardError("Erased the card, but it didn't come back as a disk; unplug it and try again")
    detail("erased %s; writing %s onto %s", disk, image.path, mount)
    put(mount, image)
    n = check(mount, image)
    eject(runner, disk, sleep)
    return n


# An update `audiolink update` put on the card (services/update.py; its --revert removes
# the same): the Pi runs it instead of its image's built-in code
UPDATE = ("audiolink/audiolink.pyz", "audiolink/audiolink.pyz.bad", "audiolink/audiolink.pyz.new")


def update(runner, disk, mount, image, sleep=time.sleep, keep_update=False):
    """An audiolink card in place: everything but KEEP replaced by image's files; its
    settings.txt renewed from the image's, keeping what its users set; an earlier update
    removed (keep_update: kept), so the Pi runs the image's code; checked; ejected.
    The new files go next to the old ones first (NAME.new) and are checked there, then
    renamed into place: a card pulled out before that keeps its old system whole, and
    running it again picks up from there. -> (files, new settings, the removed
    update's build id, or False)."""
    image = image if isinstance(image, Image) else Image(image)
    kept = {n for n in os.listdir(mount) if n in KEEP}
    skip = {n for n in image.names if n.split("/")[0] in kept}
    want = [n for n in image.names if n not in skip]
    for rel in _system_files(mount, kept):              # leftovers of an interrupted write
        if rel.endswith(NEW):
            os.remove(os.path.join(mount, *rel.split("/")))
    put(mount, image, skip, NEW)
    check(mount, image, skip, NEW)
    # The switch: renames only, so it's quick
    removed = False
    if not keep_update:
        from .services.update import pyz_build
        for rel in UPDATE:
            p = os.path.join(mount, *rel.split("/"))
            if os.path.exists(p):
                removed = removed or pyz_build(p) or "unknown"
                os.remove(p)
    for rel in _system_files(mount, kept):
        if not rel.endswith(NEW) and rel not in want:
            try:
                os.remove(os.path.join(mount, *rel.split("/")))
            except FileNotFoundError:
                pass                    # a "._" file macOS removed with its file
    for rel in want:
        p = os.path.join(mount, *rel.split("/"))
        os.replace(p + NEW, p)
    _drop_empty_dirs(mount, kept)
    os.sync()
    new = []
    if "settings.txt" in kept:
        path = os.path.join(mount, "settings.txt")
        with open(path, encoding="utf-8", errors="replace") as f:
            old = f.read()
        text, new = merge_settings(old, image.read("settings.txt").decode("utf-8", "replace"))
        if text != old:
            _write(path + NEW, text.encode())
            os.replace(path + NEW, path)
    n = check(mount, image, skip | {"settings.txt"} if "settings.txt" in kept else skip)
    eject(runner, disk, sleep)
    return n, new, removed


NEW = ".new"


def _system_files(mount, kept):
    """The card's files outside the Pi's own (KEEP) and macOS's, relative, with /."""
    out = []
    for name in os.listdir(mount):
        if name in kept or name in SKIP or _macos(name):
            continue
        p = os.path.join(mount, name)
        if os.path.isdir(p) and not os.path.islink(p):
            for r, ds, fs in os.walk(p):
                out += [os.path.relpath(os.path.join(r, f), mount).replace(os.sep, "/") for f in fs if not _macos(f)]
        else:
            out.append(name)
    return out


def _drop_empty_dirs(mount, kept):
    for name in os.listdir(mount):
        p = os.path.join(mount, name)
        if name in kept or name in SKIP or not os.path.isdir(p):
            continue
        for r, ds, fs in os.walk(p, topdown=False):
            if not [f for f in os.listdir(r) if not _macos(f)]:
                shutil.rmtree(r, ignore_errors=True)


def put(mount, image, skip=(), suffix=""):
    for name, data in image.files():
        if name not in skip:
            _write(os.path.join(mount, *name.split("/")) + suffix, data)
    os.sync()


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def check(mount, image, skip=(), suffix=""):
    """Every file of image on the card, byte for byte -> how many were checked."""
    bad, n = [], 0
    for name, data in image.files():
        if name in skip:
            continue
        p = os.path.join(mount, *name.split("/")) + suffix
        try:
            with open(p, "rb") as f:
                same = f.read() == data
        except OSError:
            same = False
        if not same:
            bad.append(name)
        n += 1
    if bad:
        raise CardError("The card doesn't match the image (%s%s); write it again"
                        % (", ".join(bad[:3]), ", ..." if len(bad) > 3 else ""))
    return n


SETTING = re.compile(r"^#?\s*([A-Za-z0-9_.-]+)\s*=", re.M)
SET = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=.*$", re.M)          # a line someone set (no #)


def merge_settings(old, new):
    """The image's settings.txt (every setting, commented out with its help and built-in
    value), with the lines the card's users set carried over in place of the commented
    ones; set lines the image doesn't know (a setting since removed, a typo) are kept at
    the end, not lost. -> (text, settings new in this image)."""
    mine = {}
    for m in SET.finditer(old):
        mine.setdefault(m.group(1), m.group(0).strip())
    text, left = new, []
    for key, line in mine.items():
        text, n = re.subn(r"^#\s*%s\s*=.*$" % re.escape(key), lambda _: line, text, count=1, flags=re.M)
        if not n:
            left.append(line)
    if left:
        text = text.rstrip("\n") + ("\n\n# ---- set on this card, not settings of this image (kept as they were)\n"
                                    + "\n".join(left) + "\n")
    added = [k for k in dict.fromkeys(SETTING.findall(new)) if k not in set(SETTING.findall(old))]
    return text, added


def eject(runner, disk, sleep=time.sleep):
    for _ in range(12):                 # Spotlight looks at a new disk first: it can refuse a while
        status, out = runner.run(["diskutil", "eject", disk])
        if not status:
            return
        sleep(5)
    raise CardError("Wrote the card, but couldn't eject it (%s); eject it before unplugging" % out.strip())


# The Pi's own, on its card: never replaced by an update
KEEP = {"audiolink", "logs", "settings.txt", "ssh_authorized_keys"}


def partitions(runner, disk):
    _, out = runner.run(["diskutil", "list", disk])
    return re.findall(r"\b(%ss\d+)\s*$" % re.escape(disk[len("/dev/"):]), out, re.M)


def volume_of(runner, disk, sleep=time.sleep):
    """Where disk's first partition is mounted (waiting a little: macOS mounts a new
    disk a moment after it appears), or None."""
    for _ in range(10):
        for part in partitions(runner, disk)[:1]:
            _, info = runner.run(["diskutil", "info", part])
            m = re.search(r"^\s*Mount Point:\s*(\S.*?)\s*$", info, re.M)
            if m:
                return m.group(1)
        sleep(1)
    return None


def classify(runner, disk, mount):
    """("blank" | "audiolink" | "other", what it looks like, for people)."""
    parts = len(partitions(runner, disk))
    files = [n for n in os.listdir(mount) if not _macos(n) and n not in SKIP] if mount else []
    if parts > 1:
        return "other", "%d partitions%s" % (parts, ", a Raspberry Pi system?" if "config.txt" in files else "")
    if not files:
        return "blank", "blank" if mount else "blank or unreadable"
    settings = os.path.join(mount, "settings.txt")
    ours = os.path.isdir(os.path.join(mount, "audiolink"))
    if not ours and os.path.isfile(settings):
        with open(settings, encoding="utf-8", errors="replace") as f:
            ours = "audiolink." in f.read()
    if ours:
        return "audiolink", "audiolink"
    return "other", "a Raspberry Pi system?" if "config.txt" in files else "%d files, not a Pi card" % len(files)


def mounted_cards(runner):
    """[(disk, bytes, mount point)] for mounted external disks that hold a Pi card
    (config.txt at the top: the Pi boots from that)."""
    _, out = runner.run(["diskutil", "list", "external", "physical"])
    found = []
    for disk, size in sorted(external_disks(runner).items()):
        parts = re.findall(r"\b(%s(?:s\d+)?)\s*$" % re.escape(disk[len("/dev/"):]), out, re.M)
        for part in parts:
            _, info = runner.run(["diskutil", "info", part])
            m = re.search(r"^\s*Mount Point:\s*(\S.*?)\s*$", info, re.M)
            if m and os.path.isfile(os.path.join(m.group(1), "config.txt")):
                found.append((disk, size, m.group(1)))
                break
    return found


# ---- opening the card through the Pi (rpiboot)
def new_disk(runner, before, seconds, sleep=time.sleep, clock=time):
    """(device, bytes) of a disk that wasn't in `before`, waiting up to `seconds`."""
    end = clock.monotonic() + seconds
    while True:
        new = {d: n for d, n in external_disks(runner).items() if d not in before and n}
        if new:
            return sorted(new.items())[0]
        if clock.monotonic() >= end:
            return None
        sleep(1)


def open_through_pi(runner, usb_state, again, platform=sys.platform, sleep=time.sleep, clock=time):
    """The Pi waiting in its USB boot mode: show its card as a disk -> (device, bytes).
    again: how to try again, for the messages ("run audiolink setup again"). clock: what
    tells the time (monotonic()), with sleep."""
    if platform != "darwin":
        raise CardError("Opening the card through the Pi works on macOS only so far")
    cmd = rpiboot_command(platform)
    if not cmd:
        raise CardError("Opening the card through the Pi needs rpiboot: %s; then %s" % (install_hint(platform), again))
    before = external_disks(runner)
    step("Opening the card")
    status, out = runner.run(cmd, timeout=90)
    if status:
        done("failed")
        raise CardError("rpiboot failed: %s" % ((out.strip().splitlines() or ["no output"])[-1]))
    end = clock.monotonic() + 15            # its firmware shows up as a card reader first
    while usb_state() != "msd" and clock.monotonic() < end:
        sleep(0.5)
    disk = new_disk(runner, before, 5, sleep, clock)        # then the card, if there is one
    if not disk:
        done("no card detected")
        step("Waiting for a card")
        disk = new_disk(runner, before, 180, sleep, clock)
        if not disk:
            done("none")
            raise CardError("No card detected; unplug the Pi, insert the card, plug the Pi back in, and %s" % again)
    dev, size = disk
    if size > MAX_CARD:
        done("%s, %d GB" % (dev, size // 10 ** 9))
        raise CardError("%s is too big to be the Pi's card; nothing changed" % dev)
    done("%.1f GB" % (size / 1e9))
    return disk


# ---- backup
SKIP = {".Spotlight-V100", ".fseventsd", ".Trashes", ".TemporaryItems"}
BACKUP_ON_CARD = ".audiolink-backup.zip"      # the Pi's own backup of its card, while it sends it (services/card.py)


def backup(mount, out):
    """Zip everything on the card at `mount` into `out` (private: it holds the Pi's keys
    and saved Wi-Fi passwords), and check the zip. -> number of files."""
    out = Path(out)
    part = out.with_name(out.name + ".part")
    fd = os.open(str(part), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    count = 0
    try:
        # strict_timestamps=False: files the Pi wrote before it knew the time (no clock:
        # 1970) are stored as 1980, the earliest a zip can say
        with os.fdopen(fd, "wb") as f, zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as z:
            for root, dirs, files in os.walk(mount):
                dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith("._"))
                for name in sorted(files):
                    if name.startswith("._") or name == ".DS_Store" or name.startswith(BACKUP_ON_CARD):
                        continue                # macOS's, or a backup being made: not the card's
                    path = os.path.join(root, name)
                    z.write(path, os.path.relpath(path, mount))
                    count += 1
        with zipfile.ZipFile(str(part)) as z:
            bad = z.testzip()
        if bad:
            raise CardError("The backup didn't check out (%s); nothing kept" % bad)
        os.replace(str(part), str(out))
    finally:
        if part.exists():
            part.unlink()
    return count
