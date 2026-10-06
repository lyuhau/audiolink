#!/bin/sh
# Fetch what build_initramfs.py needs from Raspberry Pi's repositories, pinned to the
# versions audiolink is tested with. Sparse, blob-less clones: only the files used.
#   firmware 1.20250915: newer releases break USB boot (rpiboot) on the Zero 2 W
#   (raspberrypi/firmware#2024); kernel 6.12.25, where dwc2 is a loadable module
set -e
cd "$(dirname "$0")"
FW_TAG=1.20250915
KVER=6.12.25-v8+
NONFREE=3bab0f823f5b53150b76aab77093adef6655b920

if [ ! -d firmware ]; then
    git -c advice.detachedHead=false clone -q --depth 1 --filter=blob:none --sparse --branch "$FW_TAG" \
        https://github.com/raspberrypi/firmware.git firmware
fi
git -C firmware sparse-checkout set --no-cone \
    /boot/bootcode.bin /boot/start.elf /boot/fixup.dat /boot/kernel8.img \
    /boot/bcm2710-rpi-zero-2-w.dtb /boot/overlays/dwc2.dtbo /boot/overlays/overlay_map.dtb \
    "/modules/$KVER/"

if [ ! -d firmware-nonfree ]; then
    git clone -q --filter=blob:none --no-checkout https://github.com/RPi-Distro/firmware-nonfree.git firmware-nonfree
fi
git -C firmware-nonfree sparse-checkout set --no-cone \
    '/debian/added-firmware/brcm/brcmfmac4343*' '/debian/added-firmware/cypress/cyfmac43430-sdio.*'
git -C firmware-nonfree -c advice.detachedHead=false checkout -q "$NONFREE"
echo "firmware ready: $FW_TAG (kernel $KVER), Wi-Fi firmware ${NONFREE%${NONFREE#????????}}"
