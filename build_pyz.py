#!/usr/bin/env python3
"""Build dist/audiolink.pyz: one file, runs with any Python 3.9+ (`python3 audiolink.pyz ...`).

Contains the audiolink package and the bundled PortAudio libraries for macOS and
Windows; the Pi runs the same file (it uses ALSA and ignores PortAudio).

Reproducible: the same sources give the same bytes (sorted entries, fixed
timestamps and permissions), so the relay and the card's manifest only change
when the code does.
"""
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dist" / "audiolink.pyz"
SHEBANG = b"#!/usr/bin/env python3\n"
MAIN = "import sys\nfrom audiolink.__main__ import main\nsys.exit(main())\n"
EPOCH = (1980, 1, 1, 0, 0, 0)   # the earliest a zip can say


def sources(pkg):
    """{name in the archive: file} for the package, without caches."""
    out = {}
    for src in pkg.rglob("*"):
        if src.is_file() and "__pycache__" not in src.parts and src.suffix != ".pyc" and src.name != ".DS_Store":
            out["audiolink/" + src.relative_to(pkg).as_posix()] = src
    return out


def build(out=OUT, pkg=ROOT / "audiolink"):
    out.parent.mkdir(parents=True, exist_ok=True)
    entries = {name: src.read_bytes() for name, src in sources(pkg).items()}
    entries["__main__.py"] = MAIN.encode()
    tmp = out.with_name(out.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(SHEBANG)
        with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
            for name in sorted(entries):
                info = zipfile.ZipInfo(name, EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3                   # unix, whatever builds it
                info.external_attr = 0o100644 << 16      # a regular file, rw-r--r--
                z.writestr(info, entries[name], compresslevel=9)
    os.chmod(tmp, 0o755)
    os.replace(tmp, out)
    return out


def main():
    out = build()
    print("%s (%.0f KB)" % (out.relative_to(ROOT), out.stat().st_size / 1024))


if __name__ == "__main__":
    main()
