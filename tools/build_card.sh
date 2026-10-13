#!/bin/sh
# Build the Pi's SD card with the usb platform: dist/audiolink.pyz, then the
# platform's build_card.py on an audiolink config.
#   tools/build_card.sh [config/pi.toml | config/pi-release.toml] [build_card.py options]
# -> build/card-NAME/ and build/card-NAME.manifest (NAME: the config's, e.g. pi).
# Install it on a running dev Pi with tools/deploy.sh, or on a card in a reader
# with --install /Volumes/CARD.
#   USB_PLATFORM  the platform's checkout (default: DEFAULT_PLATFORM below)
#   USB_PYTHON    Python 3.11+ for it (default: its .venv, ~/Code/usb/.venv, python3)
set -e
# The platform's initramfs branch, until it's merged into ~/Code/usb's main.
DEFAULT_PLATFORM=$HOME/Code/usb-initramfs
cd "$(dirname "$0")/.."
CONFIG=${1:-config/pi.toml}
[ $# -gt 0 ] && shift
USB_PLATFORM=${USB_PLATFORM:-$DEFAULT_PLATFORM}
if [ -z "$USB_PYTHON" ]; then
    for p in "$USB_PLATFORM/.venv/bin/python" "$HOME/Code/usb/.venv/bin/python" python3; do
        command -v "$p" >/dev/null && USB_PYTHON=$p && break
    done
fi
[ -f "$USB_PLATFORM/build_card.py" ] || { echo "no usb platform at $USB_PLATFORM (set USB_PLATFORM)" >&2; exit 1; }
[ -f "$CONFIG" ] || { echo "no config $CONFIG" >&2; exit 1; }
CONFIG=$(cd "$(dirname "$CONFIG")" && pwd)/$(basename "$CONFIG")
python3 build_pyz.py
# From the config's folder: the platform reads [access] ssh_public_key relative
# to the working directory (its other paths are relative to the config).
(cd "$(dirname "$CONFIG")" && "$USB_PYTHON" "$USB_PLATFORM/build_card.py" "$CONFIG" "$@")
# The platform builds into its out/card-HOSTNAME/: keep a copy per config here.
HOST=$(sed -n 's/^hostname *= *"\(.*\)".*/\1/p' "$(dirname "$CONFIG")/pi.toml" "$CONFIG" | tail -n 1)
NAME=$(basename "$CONFIG" .toml)
rm -rf "build/card-$NAME"
mkdir -p build
cp -R "$USB_PLATFORM/out/card-$HOST" "build/card-$NAME"
cp "$USB_PLATFORM/out/card-$HOST.manifest" "build/card-$NAME.manifest"
echo "build/card-$NAME/ ($(du -sh "build/card-$NAME" | cut -f1))"
