#!/bin/sh
# Dev: install a built card (tools/build_card.sh) on a running dev Pi over USB
# ethernet, then reboot it. Each file goes to the card as NAME.new, all are
# checked against the build's manifest on the Pi, then renamed into place. The
# card's audiolink/ (state) and logs/ are left alone.
#   tools/deploy.sh [build/card-pi] [--release] [--no-reboot]
# --release: write an image without SSH (after it, this script can't reach the Pi).
# Works from the older image too (card at /boot).
set -e
cd "$(dirname "$0")/.."
CARD=build/card-pi
RELEASE= REBOOT=1
for a in "$@"; do
    case "$a" in
    --release) RELEASE=1 ;;
    --no-reboot) REBOOT= ;;
    -*) echo "usage: tools/deploy.sh [build/card-NAME] [--release] [--no-reboot]" >&2; exit 2 ;;
    *) CARD=${a%/} ;;
    esac
done
MANIFEST=$CARD.manifest
[ -f "$MANIFEST" ] || { echo "no $MANIFEST: tools/build_card.sh first" >&2; exit 1; }
if ! grep -q root/.ssh/authorized_keys "$CARD/boot/initrd.img" && [ -z "$RELEASE" ]; then
    echo "$CARD has no SSH (a release image); --release to write it anyway" >&2
    exit 1
fi
PI=${PI:-root@10.55.0.2}
SSH="ssh -i keys/id_ed25519 -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR -o ConnectTimeout=5"

# The card's files as NAME.new, plus the manifest (.deploy.manifest), in one tar.
python3 - "$CARD" "$MANIFEST" <<'EOF' | $SSH $PI '
set -e
C=$(awk '\''$1=="/dev/mmcblk0p1" {print $2; exit}'\'' /proc/mounts)
[ -n "$C" ] || { echo "the Pi has no card mounted" >&2; exit 1; }
grep -q " $C vfat rw[, ]" /proc/mounts || mount -o remount,rw "$C"
cd "$C"
tar -xf -
if ! sed "s/\$/.new/" .deploy.manifest | sha256sum -c -s; then
    sed "s/^[0-9a-f]*  //; s/\$/.new/" .deploy.manifest | while read -r f; do rm -f "$f"; done
    rm -f .deploy.manifest
    echo "the copy on the card is damaged; nothing changed" >&2
    exit 1
fi
sed "s/^[0-9a-f]*  //" .deploy.manifest | while read -r f; do mv "$f.new" "$f"; done
rm -f .deploy.manifest
sync
'
import os, sys, tarfile
card, manifest = sys.argv[1], sys.argv[2]
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.USTAR_FORMAT) as t:
    for line in open(manifest):
        path = line.rstrip("\n")[66:]
        t.add(os.path.join(card, path), path + ".new", recursive=False)
    t.add(manifest, ".deploy.manifest")
EOF
echo "$(grep -c . "$MANIFEST") files installed, checked against $MANIFEST"
[ -n "$REBOOT" ] || exit 0

# busybox reboot (the platform's init saves logs and unmounts the card); the older
# image's init ignores it: sysrq after 10 s.
BOOT=$($SSH $PI 'cat /proc/sys/kernel/random/boot_id')
$SSH $PI '(sleep 1; reboot; sleep 10; echo b > /proc/sysrq-trigger) >/dev/null 2>&1 </dev/null &'
echo "rebooting"
[ -n "$RELEASE" ] && exit 0
start=$(date +%s)
for i in $(seq 1 60); do
    sleep 2
    if $SSH -o ConnectTimeout=2 $PI "[ \"\$(cat /proc/sys/kernel/random/boot_id)\" != $BOOT ] &&
            grep -q ' pi up ' /var/log/audiolink.log" 2>/dev/null; then
        echo "pi up ($(( $(date +%s) - start ))s after the reboot)"
        exit 0
    fi
done
echo "the Pi didn't come back up" >&2
exit 1
