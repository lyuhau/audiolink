#!/usr/bin/env python3
"""Build a standalone audiolink for the current OS with PyInstaller (no Python needed on the target).

  macOS:   dist/audiolink/audiolink      zipped as dist/audiolink-macos-<arch>.zip
  Windows: dist/audiolink/audiolink.exe  zipped as dist/audiolink-windows-x64.zip

A folder (PyInstaller --onedir), not one file: a --onefile binary unpacks itself on
every run (~2.5 s on a Mac), and every `ssh PI` runs audiolink in its ProxyCommand.
The zip holds the folder `audiolink/`: the executable and its `_internal/`.

PyInstaller can't cross-compile: run this on each target OS (or in CI).
Requires `pip install pyinstaller`.
"""
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"


def main():
    lib = ROOT / "audiolink" / "wire" / "_lib"
    if sys.platform == "darwin":
        zipname = "audiolink-macos-%s.zip" % platform.machine()
        libs = ["libportaudio.dylib"]
    elif sys.platform == "win32":
        zipname = "audiolink-windows-x64.zip"
        libs = ["libportaudio64bit.dll", "libportaudioarm64.dll"]
    else:
        sys.exit("hosts are macOS or Windows; the Pi runs audiolink.pyz")
    entry = ROOT / "build" / "entry.py"
    entry.parent.mkdir(exist_ok=True)
    entry.write_text("import sys\nfrom audiolink.__main__ import main\nsys.exit(main())\n")
    sep = ";" if sys.platform == "win32" else ":"
    cmd = [sys.executable, "-m", "PyInstaller", "--onedir", "--noconfirm", "--clean",
           "--name", "audiolink", "--paths", str(ROOT),
           "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "pyinstaller"),
           "--specpath", str(ROOT / "build")]
    for f in libs + ["PORTAUDIO-LICENSE.txt"]:
        cmd += ["--add-data", "%s%saudiolink/wire/_lib" % (lib / f, sep)]
    # The build id and repo, as build_pyz.py stamps them (the same sources: the same id),
    # in the package while PyInstaller collects it
    import build_pyz
    stamp = ROOT / "audiolink" / "_build.py"
    stamp.write_text(build_pyz.stamp(build_pyz.source_build_id()))
    try:
        subprocess.run(cmd + [str(entry)], check=True)
    finally:
        stamp.unlink()
    folder = DIST / "audiolink"
    out = DIST / zipname
    out.unlink(missing_ok=True)
    if sys.platform == "darwin":
        # ditto keeps the symlinks, permissions and signatures PyInstaller made; no ._ files
        # for extended attributes (a plain unzip would leave them as clutter)
        subprocess.run(["ditto", "-c", "-k", "--norsrc", "--noextattr", "--keepParent", str(folder), str(out)], check=True)
    else:
        shutil.make_archive(str(out.with_suffix("")), "zip", DIST, "audiolink")
    print("%s (%.1f MB)" % (out.relative_to(ROOT), out.stat().st_size / 1e6))


if __name__ == "__main__":
    main()
