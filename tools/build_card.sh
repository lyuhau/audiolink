#!/bin/sh
# Build the Pi's SD card with pi-gadget-kit: dist/audiolink.pyz, then the
# platform's build_card.py on an audiolink config.
#   tools/build_card.sh [config/pi.toml | config/pi-release.toml] [build_card.py options]
# -> build/card-NAME/ and build/card-NAME.manifest (NAME: the config's, e.g. pi).
# Install it on a running dev Pi with tools/deploy.sh, or on a card in a reader
# with --install /Volumes/CARD.
#   PLATFORM      pi-gadget-kit's checkout (default: platform/, the submodule;
#                 `git submodule update --init` fetches it)
#   PLATFORM_PYTHON  Python for it, 3.11+ or 3.9+ with tomli (default: its .venv,
#                 then python3)
set -e
case "$1" in -h|--help) sed -n '2,10s/^# \{0,1\}//p' "$0"; exit 0 ;; esac
cd "$(dirname "$0")/.."
CONFIG=${1:-config/pi.toml}
[ $# -gt 0 ] && shift
PLATFORM=${PLATFORM:-${USB_PLATFORM:-$PWD/platform}}   # USB_PLATFORM: the old name
PLATFORM_PYTHON=${PLATFORM_PYTHON:-$USB_PYTHON}
if [ -z "$PLATFORM_PYTHON" ]; then
    for p in "$PLATFORM/.venv/bin/python" python3; do
        command -v "$p" >/dev/null && PLATFORM_PYTHON=$p && break
    done
fi
[ -f "$PLATFORM/build_card.py" ] || {
    echo "no pi-gadget-kit at $PLATFORM: git submodule update --init (or set PLATFORM)" >&2; exit 1; }
ls "$PLATFORM"/out/modules/*.ko >/dev/null 2>&1 || {
    echo "the platform has no gadget modules yet: $PLATFORM/alpine-module/fetch.sh (prebuilt), or build.sh (Docker; see its README)" >&2; exit 1; }
[ -f "$CONFIG" ] || { echo "no config $CONFIG" >&2; exit 1; }
CONFIG=$(cd "$(dirname "$CONFIG")" && pwd)/$(basename "$CONFIG")
NAME=$(basename "$CONFIG" .toml)
python3 build_pyz.py
# The platform names its output after the config's hostname and file name; take
# the card's path from the manifest line it prints, "... (out/card-X.manifest)".
LOG=$(mktemp)
trap 'rm -f "$LOG" "$LOG.status"' EXIT
{ PYTHONUNBUFFERED=1 "$PLATFORM_PYTHON" "$PLATFORM/build_card.py" "$CONFIG" "$@"; echo $? > "$LOG.status"; } | tee "$LOG"
[ "$(cat "$LOG.status")" = 0 ] || exit 1
MANIFEST=$(sed -n 's/.*manifest sha256 .*(\(.*\.manifest\))$/\1/p' "$LOG" | tail -n 1)
[ -n "$MANIFEST" ] || { echo "can't find the card's manifest in build_card.py's output" >&2; exit 1; }
SRC=$PLATFORM/${MANIFEST%.manifest}
rm -rf "build/card-$NAME" "build/card-$NAME.manifest"
mkdir -p build
cp -R "$SRC" "build/card-$NAME"
cp "$PLATFORM/$MANIFEST" "build/card-$NAME.manifest"
# A release card must not let anyone in over SSH.
case "$NAME" in *release*)
    if gzip -dc "build/card-$NAME/boot/initrd.img" | cpio -it 2>/dev/null | grep -qx root/.ssh/authorized_keys; then
        rm -rf "build/card-$NAME" "build/card-$NAME.manifest"
        echo "$SRC has root/.ssh/authorized_keys: not a release card (nothing kept in build/)" >&2
        exit 1
    fi ;;
esac
echo "build/card-$NAME/ ($(du -sh "build/card-$NAME" | cut -f1 | tr -d ' '))"
