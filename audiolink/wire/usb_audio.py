"""Host: which audio devices are the Pi's, by the USB device they belong to.

Device names are up to each OS: macOS names the gadget's devices after its
alternate-setting strings, Windows and Linux after the product string. Matching
on names ties detection to whatever the gadget is called. Instead, each OS is
asked which USB device (vendor:product ID) a sound device belongs to, and the
Pi's are the ones with an ID in USB_IDS. Where that can't be told (Windows, for
now), or no device has one of these IDs (a card's settings.txt can change them),
detection falls back to the name hints.
"""
import glob
import logging
import os
import re
import sys

log = logging.getLogger("audiolink")

# The gadget's vendor:product IDs: config/device.toml and device-release.toml (a
# test keeps them in step). Add the old ID here when changing it, so hosts
# still find Pis with older cards.
USB_IDS = {(0x1d6b, 0x0104)}

# Name fallback. macOS: the alternate-setting strings stock f_uac2 fixes; Windows
# and Linux: the product string.
NAME_HINTS = ("audiopi", "playback inactive", "capture inactive", "uac2")


def by_name(dev):
    return any(h in dev["name"].lower() for h in NAME_HINTS)


def annotate(devs):
    """Add "usb": (vendor, product) or None to each PortAudio device, where this OS
    says which USB device it belongs to; None also for non-USB devices."""
    try:
        ids = _lookup()
    except Exception as e:                  # detection then falls back to names
        log.debug("usb audio ids unavailable: %s", e)
        ids = lambda dev: None              # noqa: E731
    for d in devs:
        d["usb"] = ids(d)
    return devs


def pi_devices(devs):
    """The Pi's devices among annotated ones: by USB ID, or else by name."""
    mine = [d for d in devs if d.get("usb") in USB_IDS]
    return mine or [d for d in devs if by_name(d)]


def _lookup():
    if sys.platform.startswith("linux"):
        return linux_ids()
    if sys.platform == "darwin":
        return mac_ids()
    return lambda dev: None


# ---- Linux: ALSA cards know their USB ID

HW = re.compile(r"\(hw:(\d+),\d+\)")


def linux_ids(proc="/proc/asound"):
    """PortAudio's ALSA devices are named "...(hw:CARD,DEV)"; a USB card has
    /proc/asound/cardN/usbid, "1d6b:0104"."""
    ids = {}
    for path in glob.glob(os.path.join(proc, "card*", "usbid")):
        card = os.path.basename(os.path.dirname(path))[4:]
        try:
            with open(path) as f:
                vid, pid = f.read().strip().split(":")
            ids[card] = (int(vid, 16), int(pid, 16))
        except (OSError, ValueError):
            continue

    def lookup(dev):
        m = HW.search(dev["name"])
        return ids.get(m.group(1)) if m else None
    return lookup


# ---- macOS

def mac_ids():
    from . import coreaudio
    return coreaudio.usb_ids()
