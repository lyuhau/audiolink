#!/bin/sh
# Dev: copy bootdir/ onto a running Pi's SD card over USB ethernet, then reboot it.
# The card's audiolink/ folder (identity, saved networks) is left alone.
set -e
cd "$(dirname "$0")"
PI=${PI:-root@10.55.0.2}
# A release image has no USB ethernet or ssh: after it, this script can't reach the Pi.
if [ "$(cat bootdir/image.txt 2>/dev/null)" != dev ] && [ "$1" != --release ]; then
    echo "bootdir/ isn't a dev image (python3 build_initramfs.py --dev); --release to write it anyway" >&2
    exit 1
fi
[ "$1" = --release ] && shift
SSH="ssh -i keys/id_ed25519 -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR"
DIR=$($SSH $PI 'mkdir -p /mnt/sd && { mountpoint -q /boot && echo /boot || { mount /dev/mmcblk0p1 /mnt/sd && echo /mnt/sd; }; }')
COPYFILE_DISABLE=1 tar -C bootdir -cf - . | $SSH $PI "tar -C $DIR -xf - 2>/dev/null; sync; md5sum $DIR/initrd.img"
md5 -q bootdir/initrd.img 2>/dev/null || md5sum bootdir/initrd.img
[ "$1" = "--no-reboot" ] || $SSH $PI '(sleep 1; echo b > /proc/sysrq-trigger) >/dev/null 2>&1 &'
