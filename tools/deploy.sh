#!/bin/sh
# Dev: install a built card (tools/build_card.sh) over SSH on a running dev Pi, for
# what `audiolink update build/card-pi` can't do (it sends a build into the spare
# image slot, on trial: use it for a Pi with slots):
#   - a Pi whose card has no image slots yet: the whole build, its image in a/ and
#     the card's config.txt booting a/ (no fallback: it's the only image);
#   - --firmware: just the card's top-level firmware (bootcode.bin, start.elf,
#     fixup.dat), which update leaves alone; slots, config.txt untouched.
# Each file goes to the card as NAME.new, all are checked against the build's
# manifest on the Pi, then renamed into place. The card's audiolink/ (state), logs/
# and settings.txt are left alone (new settings aren't added to it), then it reboots.
#   tools/deploy.sh [build/card-pi] [--firmware] [--release] [--no-reboot]
# --release: write an image without SSH (after it, this script can't reach the Pi).
set -e
cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-python3}
CARD=build/card-pi
RELEASE= REBOOT=1 MODE=all
for a in "$@"; do
    case "$a" in
    --release) RELEASE=1 ;;
    --firmware) MODE=firmware ;;
    --no-reboot) REBOOT= ;;
    -*) echo "usage: tools/deploy.sh [build/card-NAME] [--firmware] [--release] [--no-reboot]" >&2; exit 2 ;;
    *) CARD=${a%/} ;;
    esac
done
MANIFEST=$CARD.manifest
[ -f "$MANIFEST" ] || { echo "no $MANIFEST: make card (tools/build_card.sh) first" >&2; exit 1; }
. tools/pi_code.sh
INITRD=$(card_initrd "$CARD") || exit 1
if [ $MODE = all ] && ! gzip -dc "$INITRD" | cpio -it 2>/dev/null | grep -qx root/.ssh/authorized_keys &&
        [ -z "$RELEASE" ]; then
    echo "$CARD has no SSH (a release image); --release to write it anyway" >&2
    exit 1
fi
PI=${PI:-root@10.55.0.2}
SSH="ssh -i keys/id_ed25519 -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR -o ConnectTimeout=5"
pi_reachable

# A card with slots: its images go through `audiolink update` (spare slot, trial).
CURRENT=$($SSH $PI 'sh /etc/usb-gadget/slot.sh status 2>/dev/null' | sed -n 's/^current=\([^ ]*\).*/\1/p')
if [ $MODE = all ] && [ -n "$CURRENT" ]; then
    echo "the Pi's card has image slots (running $CURRENT): send the build to the spare slot, on trial," >&2
    echo "  audiolink update $CARD" >&2
    echo "(just the code: make hot, or audiolink update --code; the card's firmware: tools/deploy.sh --firmware)" >&2
    exit 1
fi

# The files as NAME.new, plus their manifest (.deploy.manifest), in one tar. A whole
# build also gets its slot's .image.json, as `audiolink update` and `card write` record
# it (card.py InSlot), so `audiolink update --status` knows what a/ holds.
"$PYTHON" - "$CARD" "$MANIFEST" $MODE <<'EOF' | $SSH $PI '
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
sed "s/^[0-9a-f]*  //" .deploy.manifest | while read -r f; do
    if [ "$f" = settings.txt ] && [ -f settings.txt ]; then rm -f settings.txt.new; continue; fi
    mv "$f.new" "$f"
done
echo "$(grep -c . .deploy.manifest) files installed, checked against the build'\''s manifest" >&2
rm -f .deploy.manifest
sync
'
import hashlib, io, os, sys, tarfile
card, manifest, mode = sys.argv[1:4]
FIRMWARE = ("bootcode.bin", "start.elf", "fixup.dat")   # services/image.py FIRMWARE
lines = [l.rstrip("\n") for l in open(manifest) if l.strip()]
if mode == "firmware":
    lines = [l for l in lines if l[66:] in FIRMWARE]
    if not lines:
        sys.exit("%s has no firmware files (%s)" % (card, ", ".join(FIRMWARE)))
extra = {}
if mode == "all" and any(l[66:] == "a/config.txt" for l in lines):
    sys.path.insert(0, os.getcwd())
    from audiolink.card import SLOT_INFO, Image, InSlot
    name = "a/" + SLOT_INFO
    extra[name] = InSlot(Image(card), "a/").read(name)
    lines = [l for l in lines if l[66:] != name] + [hashlib.sha256(extra[name]).hexdigest() + "  " + name]


def add(t, name, data):
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(data), 0o644
    t.addfile(info, io.BytesIO(data))


try:
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.USTAR_FORMAT) as t:
        for line in lines:
            path = line[66:]
            if path in extra:
                add(t, path + ".new", extra[path])
            else:
                t.add(os.path.join(card, path), path + ".new", recursive=False)
        add(t, ".deploy.manifest", ("\n".join(lines) + "\n").encode())
except BrokenPipeError:     # ssh went away and says why
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    sys.exit(1)
EOF
[ -n "$REBOOT" ] || exit 0

# busybox reboot (the platform's init saves logs and unmounts the card); the older
# image's init ignores it: sysrq after 10 s.
BOOT=$($SSH $PI 'cat /proc/sys/kernel/random/boot_id')
$SSH $PI '(sleep 1; reboot; sleep 10; echo b > /proc/sysrq-trigger) >/dev/null 2>&1 </dev/null &'
echo "rebooting"
[ -n "$RELEASE" ] && exit 0
start=$(date +%s)
pi_wait_up 120 "$BOOT" || { echo "the Pi didn't come back up" >&2; exit 1; }
echo "pi up ($(( $(date +%s) - start ))s after the reboot)"
pi_code_check
