"""The Pi's card, written through the Pi itself (`audiolink setup`, with the Pi on USB).

A Pi whose card has no boot files (none in, blank, unreadable) waits in its USB boot mode
("BCM2710 Boot", 0a5c:2764). rpiboot (Raspberry Pi's usbboot, installed separately) sends
it a small firmware that shows its card to this computer as a disk ("msd" mode,
0a5c:0001); setup erases that disk, unzips the card image onto it (the release's
audiolink-pi-VER.zip, checked against its SHA256SUMS), ejects it, and the Pi starts from
it once it's replugged.

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
from .ui import detail

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
    """The Pi as USB shows it: "pi" (running audiolink), "rom" (waiting in its boot mode),
    "msd" (its card shown as a disk) or None."""
    if hints is None:
        from .wire.host_audio import DEVICE_HINTS as hints
    devs = usb_devices(runner, platform)
    if any(any(h in name.lower() for h in hints) for _, _, name in devs):
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


def check_image(path):
    try:
        with zipfile.ZipFile(str(path)) as z:
            names = z.namelist()
    except (OSError, zipfile.BadZipFile) as e:
        raise CardError("%s isn't a card image (%s)" % (path, e))
    if "config.txt" not in names or "settings.txt" not in names:
        raise CardError("%s isn't an audiolink card image (no config.txt, settings.txt)" % path)
    return Path(path)


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
    """Erase disk (FAT32, MBR) and unzip image onto it, then eject it."""
    status, out = runner.run(["diskutil", "eraseDisk", "FAT32", LABEL, "MBRFormat", disk], timeout=300)
    if status:
        raise CardError("Couldn't erase the card: %s" % out.strip())
    mount = Path("/Volumes") / LABEL
    detail("erased %s; unzipping %s onto %s", disk, image, mount)
    with zipfile.ZipFile(str(image)) as z:
        z.extractall(str(mount))
    os.sync()
    for _ in range(12):                 # Spotlight looks at a new disk first: it can refuse a while
        status, out = runner.run(["diskutil", "eject", disk])
        if not status:
            return
        sleep(5)
    raise CardError("Wrote the card, but couldn't eject the card (%s); eject the card, then go on" % out.strip())
