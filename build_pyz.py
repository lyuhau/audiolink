#!/usr/bin/env python3
"""Build dist/audiolink.pyz: one file, runs with any Python 3.9+ (`python3 audiolink.pyz ...`).

Contains the audiolink package and the bundled PortAudio libraries for macOS and
Windows; the Pi runs the same file (it uses ALSA and ignores PortAudio).

Reproducible: the same sources give the same bytes (sorted entries, fixed
timestamps and permissions), so the relay and the card's manifest only change
when the code does. Its build id (audiolink/_build.py, audiolink.BUILD) is a hash of
those sources: two builds of one version say which they are. _build.py also names the
GitHub repo it's built from (a fork's own), whose releases it downloads (card.repo()).
"""
import hashlib
import os
import sys
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


def build_id(entries):
    h = hashlib.sha256()
    for name in sorted(entries):
        h.update(name.encode() + b"\0" + entries[name] + b"\0")
    return h.hexdigest()[:12]


def stamp(bid):
    """audiolink/_build.py's text: the build id, and the repo (GITHUB_REPOSITORY, else origin's)."""
    from audiolink.card import checkout_repo
    repo = checkout_repo(ROOT)
    if not repo:
        from audiolink.card import UPSTREAM
        print("warning: no GitHub repo for this build (no GITHUB_REPOSITORY, origin isn't a GitHub URL):"
              " it will download %s's release images" % UPSTREAM, file=sys.stderr)
    return 'BUILD = "%s"\n' % bid + ('REPO = "%s"\n' % repo if repo else "")


def source_build_id(pkg=ROOT / "audiolink"):
    """The build id of the package as it is now (what build() would stamp)."""
    entries = {name: src.read_bytes() for name, src in sources(pkg).items() if name != "audiolink/_build.py"}
    entries["__main__.py"] = MAIN.encode()
    return build_id(entries)


def build(out=OUT, pkg=ROOT / "audiolink"):
    out.parent.mkdir(parents=True, exist_ok=True)
    entries = {name: src.read_bytes() for name, src in sources(pkg).items() if name != "audiolink/_build.py"}
    entries["__main__.py"] = MAIN.encode()
    entries["audiolink/_build.py"] = stamp(build_id(entries)).encode()
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
    from zipfile import ZipFile
    with ZipFile(str(out)) as z:
        bid = z.read("audiolink/_build.py").decode().split('"')[1]
    print("%s (%.0f KB, build %s)" % (out.relative_to(ROOT), out.stat().st_size / 1024, bid))


if __name__ == "__main__":
    main()
