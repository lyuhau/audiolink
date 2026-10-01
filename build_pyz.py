#!/usr/bin/env python3
"""Build dist/audiolink.pyz: one file, runs with any Python 3.9+ (`python3 audiolink.pyz ...`).

Contains the audiolink package and the bundled PortAudio libraries for macOS and
Windows; the Pi runs the same file (it uses ALSA and ignores PortAudio).
"""
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dist" / "audiolink.pyz"
MAIN = "import sys\nfrom audiolink.__main__ import main\nsys.exit(main())\n"


def main():
    OUT.parent.mkdir(exist_ok=True)
    stage = ROOT / "build" / "pyz"
    if stage.exists():
        import shutil
        shutil.rmtree(stage)
    pkg = stage / "audiolink"
    for src in (ROOT / "audiolink").rglob("*"):
        if src.is_file() and "__pycache__" not in src.parts:
            dest = pkg / src.relative_to(ROOT / "audiolink")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
    (stage / "__main__.py").write_text(MAIN)
    zipapp.create_archive(stage, OUT, interpreter="/usr/bin/env python3", compressed=True)
    OUT.chmod(0o755)
    print("%s (%.0f KB)" % (OUT.relative_to(ROOT), OUT.stat().st_size / 1024))


if __name__ == "__main__":
    main()
