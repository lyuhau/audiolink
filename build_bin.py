#!/usr/bin/env python3
"""Build a standalone audiolink executable for the current OS with PyInstaller.

  macOS:   dist/audiolink-macos-<arch>   (no Python needed on the target)
  Windows: dist/audiolink.exe

PyInstaller can't cross-compile: run this on each target OS (or in CI).
Requires `pip install pyinstaller`.
"""
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    lib = ROOT / "audiolink" / "_lib"
    if sys.platform == "darwin":
        name = "audiolink-macos-" + platform.machine()
        libs = ["libportaudio.dylib"]
    elif sys.platform == "win32":
        name = "audiolink"
        libs = ["libportaudio64bit.dll", "libportaudioarm64.dll"]
    else:
        sys.exit("hosts are macOS or Windows; the Pi runs audiolink.pyz")
    entry = ROOT / "build" / "entry.py"
    entry.parent.mkdir(exist_ok=True)
    entry.write_text("import sys\nfrom audiolink.__main__ import main\nsys.exit(main())\n")
    sep = ";" if sys.platform == "win32" else ":"
    cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--noconfirm", "--clean",
           "--name", name, "--paths", str(ROOT),
           "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build" / "pyinstaller"),
           "--specpath", str(ROOT / "build")]
    for f in libs + ["PORTAUDIO-LICENSE.txt"]:
        cmd += ["--add-data", "%s%saudiolink/_lib" % (lib / f, sep)]
    subprocess.run(cmd + [str(entry)], check=True)
    out = ROOT / "dist" / (name + (".exe" if sys.platform == "win32" else ""))
    print("%s (%.1f MB)" % (out.relative_to(ROOT), out.stat().st_size / 1e6))


if __name__ == "__main__":
    main()
