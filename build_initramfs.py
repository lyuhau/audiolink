#!/usr/bin/env python3
"""Build the audiopi boot folder: Pi firmware + kernel + an Alpine-based initramfs.

Everything is assembled in memory and written as a newc cpio, so file ownership,
modes, symlinks and case-sensitive names survive without root or a Linux VM.
"""
import gzip
import io
import json
import lzma
import os
import re
import shutil
import stat
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache" / "apk"
OUT = ROOT / "bootdir"
FW = ROOT / "firmware"
KVER = "6.12.25-v8+"
ALPINE = "https://dl-cdn.alpinelinux.org/alpine/v3.24"
REPOS = ["main", "community"]
ARCH = "aarch64"
WIFI_COUNTRY = "US"
PACKAGES = [
    "alpine-baselayout", "busybox", "busybox-binsh", "musl", "dropbear",
    "wpa_supplicant", "alsa-utils", "python3", "busybox-extras", "iw", "avahi", "hostapd",
    "dosfstools",   # fsck.fat: the SD card is never cleanly unmounted (power just goes)
]
KMODS = [
    "dwc2", "libcomposite", "usb_f_ecm", "usb_f_uac2", "brcmfmac",
    "brcmfmac-cyw", "brcmfmac-wcc", "brcmfmac-bca",
]


# ---------------------------------------------------------------- apk index
def fetch(url, dest):
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.rename(dest)
    return dest


def load_index():
    pkgs, provides = {}, {}
    for repo in REPOS:
        idx = fetch(f"{ALPINE}/{repo}/{ARCH}/APKINDEX.tar.gz", CACHE / f"APKINDEX-{repo}.tar.gz")
        with tarfile.open(idx) as t:
            text = t.extractfile("APKINDEX").read().decode()
        for block in text.strip().split("\n\n"):
            e = {}
            for line in block.splitlines():
                if len(line) > 2 and line[1] == ":":
                    e[line[0]] = line[2:]
            e["repo"] = repo
            name = e["P"]
            pkgs.setdefault(name, e)
            for p in e.get("p", "").split():
                provides.setdefault(re.split(r"[<>=~]", p)[0], name)
    return pkgs, provides


def resolve(pkgs, provides, wanted):
    seen, stack = [], list(wanted)
    while stack:
        dep = stack.pop()
        if dep.startswith("!"):
            continue
        dep = re.split(r"[<>=~]", dep)[0]
        name = dep if dep in pkgs else provides.get(dep)
        if name is None:
            raise SystemExit(f"cannot resolve dependency: {dep}")
        if name in seen:
            continue
        seen.append(name)
        stack.extend(pkgs[name].get("D", "").split())
    return seen


# ---------------------------------------------------------------- rootfs model
class Tree:
    """path -> (mode, data_bytes_or_linktarget)"""

    def __init__(self):
        self.entries = {}

    def _parents(self, path):
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i])
            if d and d not in self.entries:
                self.entries[d] = (stat.S_IFDIR | 0o755, b"")

    def dir(self, path, mode=0o755):
        path = path.strip("/")
        self._parents(path)
        self.entries[path] = (stat.S_IFDIR | mode, b"")

    def file(self, path, data, mode=0o644):
        path = path.strip("/")
        self._parents(path)
        self.entries[path] = (stat.S_IFREG | mode, data)

    def symlink(self, path, target):
        path = path.strip("/")
        self._parents(path)
        self.entries[path] = (stat.S_IFLNK | 0o777, target.encode())

    def add_apk(self, apk_path):
        with gzip.open(apk_path) as gz:
            raw = gz.read()
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:", ignore_zeros=True) as t:
            hardlinks = []
            for m in t:
                name = m.name.lstrip("./") if m.name.startswith("./") else m.name
                if not name or name.startswith("."):
                    continue  # .PKGINFO, .SIGN.*, install scripts
                perm = m.mode & 0o7777
                if m.isdir():
                    self.dir(name, perm)
                elif m.issym():
                    self.symlink(name, m.linkname)
                elif m.islnk():
                    hardlinks.append((name, m.linkname))
                elif m.isfile():
                    self.file(name, t.extractfile(m).read(), perm)
            for name, target in hardlinks:
                self.entries[name] = self.entries[target.strip("/")]

    def write_cpio(self, out):
        buf = io.BytesIO()
        ino = 1
        # Sorting puts every directory before its contents.
        for path in sorted(self.entries):
            mode, data = self.entries[path]
            if stat.S_ISDIR(mode):
                data = b""
            name = path.encode() + b"\0"
            hdr = "070701" + "".join(f"{v:08X}" for v in (
                ino, mode, 0, 0, 2 if stat.S_ISDIR(mode) else 1, 0, len(data),
                0, 0, 0, 0, len(name), 0))
            buf.write(hdr.encode() + name)
            buf.write(b"\0" * (-(110 + len(name)) % 4))
            buf.write(data)
            buf.write(b"\0" * (-len(data) % 4))
            ino += 1
        trailer = b"TRAILER!!!\0"
        buf.write(("070701" + "".join(f"{v:08X}" for v in (
            0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, len(trailer), 0))).encode() + trailer)
        buf.write(b"\0" * (-buf.tell() % 512))
        with gzip.open(out, "wb", compresslevel=9) as f:
            f.write(buf.getvalue())


# ---------------------------------------------------------------- kernel modules
def add_kmods(tree):
    mdir = FW / "modules" / KVER
    deps = {}
    for line in (mdir / "modules.dep").read_text().splitlines():
        mod, _, rest = line.partition(":")
        deps[mod] = rest.split()
    by_name = {Path(m).name.split(".ko")[0].replace("-", "_"): m for m in deps}

    need, stack = set(), [by_name[k.replace("-", "_")] for k in KMODS
                          if k.replace("-", "_") in by_name]
    while stack:
        m = stack.pop()
        if m not in need:
            need.add(m)
            stack.extend(deps[m])

    base = f"lib/modules/{KVER}"
    dep_lines = []
    for m in sorted(need):
        plain = m.removesuffix(".xz")
        tree.file(f"{base}/{plain}", lzma.decompress((mdir / m).read_bytes()))
        dep_lines.append(f"{plain}: " + " ".join(d.removesuffix(".xz") for d in deps[m]))
    tree.file(f"{base}/modules.dep", ("\n".join(dep_lines) + "\n").encode())
    for extra in ("modules.builtin", "modules.order", "modules.alias", "modules.softdep"):
        tree.file(f"{base}/{extra}", (mdir / extra).read_bytes().replace(b".ko.xz", b".ko"))
    print(f"kernel modules: {len(need)}")


def add_wifi_fw(tree):
    src = ROOT / "firmware-nonfree/debian/added-firmware/brcm"
    for p in sorted(src.glob("brcmfmac4343*")):
        if p.is_symlink():
            tree.symlink(f"lib/firmware/brcm/{p.name}", os.readlink(p))
        else:
            tree.file(f"lib/firmware/brcm/{p.name}", p.read_bytes())
    # Regulatory data for the 43430/1 (43436s) radio; same link Raspberry Pi OS ships.
    clm = ROOT / "firmware-nonfree/debian/added-firmware/cypress/cyfmac43430-sdio.clm_blob"
    tree.file(f"lib/firmware/cypress/{clm.name}", clm.read_bytes())
    tree.symlink("lib/firmware/brcm/brcmfmac43430-sdio.clm_blob", f"../cypress/{clm.name}")


# ---------------------------------------------------------------- wifi seed
def seed_networks(conf):
    """Turn wifi.conf's network blocks into audiolink's saved-network store, keeping
    only the derived WPA key (never the password) in the image."""
    import hashlib
    if not conf.exists():
        return {}
    seed = {}
    for block in re.findall(r"network\s*=\s*\{(.*?)\}", conf.read_text(), re.S):
        ssid = re.search(r'^\s*ssid="(.*)"\s*$', block, re.M)
        psk = re.search(r'^\s*psk=(".*"|[0-9a-fA-F]{64})\s*$', block, re.M)
        if not ssid or ssid.group(1) == "YOUR_WIFI_NAME":
            continue
        key = None
        if psk:
            raw = psk.group(1)
            key = hashlib.pbkdf2_hmac("sha1", raw[1:-1].encode(), ssid.group(1).encode(), 4096, 32).hex() \
                if raw.startswith('"') else raw.lower()
        seed[ssid.group(1)] = {"psk": key, "hidden": "scan_ssid=1" in block, "last_used": 0}
    return seed


# ---------------------------------------------------------------- main
DEV = False


def main():
    global DEV
    import argparse
    ap = argparse.ArgumentParser(description="Build the Pi boot folder (bootdir/).")
    ap.add_argument("--dev", action="store_true",
                    help="development image: USB ethernet + SSH with keys/id_ed25519.pub, and wifi.conf "
                         "pre-saved. Release images (the default) contain none of these.")
    DEV = ap.parse_args().dev
    print("image: %s" % ("DEV (USB ethernet, SSH key, wifi.conf)" if DEV else "release"))
    pkgs, provides = load_index()
    names = resolve(pkgs, provides, PACKAGES)
    print(f"alpine packages: {len(names)}")

    tree = Tree()
    for n in names:
        e = pkgs[n]
        apk = fetch(f"{ALPINE}/{e['repo']}/{ARCH}/{n}-{e['V']}.apk", CACHE / f"{n}-{e['V']}.apk")
        tree.add_apk(apk)

    for d in ("proc", "sys", "dev", "tmp", "run", "root", "var/log", "var/lib/misc", "etc/dropbear"):
        tree.dir(d)
    tree.dir("tmp", 0o1777)
    tree.dir("root", 0o700)
    tree.file("init", (ROOT / "pi/init").read_bytes(), 0o755)
    tree.dir("root/.ssh", 0o700)
    if DEV:                             # USB-ethernet + SSH for development, with our key
        tree.file("etc/audiolink-dev", b"")
        tree.file("root/.ssh/authorized_keys", (ROOT / "keys/id_ed25519.pub").read_bytes(), 0o600)
    tree.file("etc/hostname", b"audiopi\n")
    tree.file("etc/shadow", b"root:*:0:0:::::\n", 0o640)  # key-only login
    # audiolink: the same .pyz the hosts run, plus its start script and arguments.
    import build_pyz
    build_pyz.main()
    tree.file("root/audiolink.pyz", (ROOT / "dist/audiolink.pyz").read_bytes(), 0o755)
    tree.file("root/start-audiolink.sh", (ROOT / "pi/start-audiolink.sh").read_bytes(), 0o755)
    tree.file("etc/audiolink.args", (ROOT / "pi/audiolink.args").read_bytes())
    tree.file("etc/avahi/avahi-daemon.conf", (ROOT / "pi/avahi/avahi-daemon.conf").read_bytes())
    tree.file("etc/avahi/services/audiolink.service", (ROOT / "pi/avahi/audiolink.service").read_bytes())
    # Users that package install scripts would normally create (we don't run those).
    for path, line in (("etc/passwd", b"avahi:x:86:86:Avahi:/run/avahi-daemon:/sbin/nologin\n"),
                       ("etc/group", b"avahi:x:86:\n")):
        mode, data = tree.entries[path]
        if line not in data:
            tree.entries[path] = (mode, data + line)

    # wpa_supplicant runs with no networks of its own; audiolink loads the saved ones.
    tree.file("etc/wpa_supplicant/wpa_supplicant.conf",
              f"ctrl_interface=/var/run/wpa_supplicant\ncountry={WIFI_COUNTRY}\nupdate_config=0\n".encode(), 0o600)
    seed = seed_networks(ROOT / "wifi.conf") if DEV else {}
    if seed:
        tree.file("var/lib/audiolink/networks.json", json.dumps(seed, indent=1).encode(), 0o600)
    print(f"wifi: {len(seed)} pre-saved network(s) from wifi.conf")

    add_kmods(tree)
    add_wifi_fw(tree)

    OUT.mkdir(exist_ok=True)
    (OUT / "overlays").mkdir(exist_ok=True)
    for f in ("bootcode.bin", "start.elf", "fixup.dat", "kernel8.img", "bcm2710-rpi-zero-2-w.dtb"):
        shutil.copy(FW / "boot" / f, OUT / f)
    for f in ("dwc2.dtbo", "overlay_map.dtb"):
        shutil.copy(FW / "boot/overlays" / f, OUT / "overlays" / f)
    (OUT / "config.txt").write_text(
        "arm_64bit=1\n"
        "kernel=kernel8.img\n"
        "initramfs initrd.img followkernel\n"
        "dtoverlay=dwc2,dr_mode=peripheral\n"
        "disable_splash=1\n"
    )
    (OUT / "cmdline.txt").write_text("console=tty1 rdinit=/init loglevel=6\n")
    tree.write_cpio(OUT / "initrd.img")
    print(f"initrd.img: {(OUT / 'initrd.img').stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
