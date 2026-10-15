#!/usr/bin/env python3
"""Zip a built card for a release: its files as they go onto the card, checked
against its manifest. Deterministic: the same card gives the same bytes.
  tools/card_zip.py build/card-pi-release OUT.zip    # reads build/card-pi-release.manifest
Users unzip it onto a FAT32-formatted microSD card (README: Where card images come from)."""
import hashlib
import os
import sys
import zipfile

EPOCH = (1980, 1, 1, 0, 0, 0)   # the earliest a zip can say


def main():
    if len(sys.argv) != 3 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__.strip())
    card, out = sys.argv[1].rstrip("/"), sys.argv[2]
    with open(card + ".manifest") as f:
        manifest = dict(reversed(line.rstrip("\n").split("  ", 1)) for line in f if line.strip())
    files = sorted(os.path.relpath(os.path.join(root, n), card).replace(os.sep, "/")
                   for root, _, names in os.walk(card) for n in names)
    if set(files) != set(manifest):
        sys.exit(f"{card} doesn't match its manifest: "
                 f"{sorted(set(files) ^ set(manifest))}")
    with zipfile.ZipFile(out + ".part", "w", zipfile.ZIP_DEFLATED) as z:
        for name in files:
            with open(os.path.join(card, name), "rb") as f:
                data = f.read()
            if hashlib.sha256(data).hexdigest() != manifest[name]:
                sys.exit(f"{card}/{name} doesn't match its manifest")
            info = zipfile.ZipInfo(name, EPOCH)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data, compresslevel=9)
    os.replace(out + ".part", out)
    print(f"{out}: {len(files)} files, {os.path.getsize(out) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
