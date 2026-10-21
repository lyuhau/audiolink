"""Host: which audio devices are the Pi's, by the USB device they belong to.

Device names are up to each OS: macOS names the gadget's devices after its
alternate-setting strings, Windows and Linux after the product string. Matching
on names ties detection to whatever the gadget is called. Instead, each OS is
asked which USB device (vendor:product ID) a sound device belongs to, and the
Pi's are the ones with a known ID: USB_IDS, plus the host's usb_ids (host.json, or
`audiolink host --usb-id`) for Pis that present others, such as a fork's. Where that
can't be told (Windows, for now), or no device has a known ID (a card's settings.txt
can change them), detection falls back to the name hints.
"""
import glob
import logging
import os
import re
import sys

log = logging.getLogger("audiolink")

# The gadget's vendor:product IDs: the built-in flavors' (config/flavors/*.toml; a
# test keeps them in step). Added flavors' IDs: host.json's usb_ids. Add the old ID here when changing it, so hosts
# still find Pis with older cards.
USB_IDS = {(0x1d6b, 0x0104)}

_extra = None                       # `audiolink host --usb-id`: wins over host.json


def set_extra_ids(ids):
    """The host's usb_ids, as `audiolink host` resolved them (flag, else host.json)."""
    global _extra
    _extra = None if ids is None else {parse_id(i) for i in ids}


def add_ids(ids):
    """More of the host's usb_ids while it runs (`audiolink flavor`: the Pi as another
    device). -> every ID known now, as text."""
    global _extra
    _extra = (_extra if _extra is not None else known_ids() - USB_IDS) | {parse_id(i) for i in ids}
    return sorted("%04x:%04x" % i for i in known_ids())


def parse_id(text):
    vid, pid = text.split(":")
    return int(vid, 16), int(pid, 16)


def known_ids():
    """USB_IDS, plus the host's usb_ids: set by `audiolink host`, else host.json's."""
    if _extra is not None:
        return USB_IDS | _extra
    from ..host import config
    return USB_IDS | {parse_id(i) for i in config.load(strict=False).get("usb_ids", [])}


# Name fallback. macOS: the alternate-setting strings stock f_uac2 fixes; Windows
# and Linux: the product string.
NAME_HINTS = ("audiopi", "playback inactive", "capture inactive", "uac2")


def by_name(dev):
    return any(h in dev["name"].lower() for h in NAME_HINTS)


def annotate(devs):
    """Add to each PortAudio device "usb": (vendor, product) or None, where this OS says
    which USB device it belongs to (None also for non-USB devices), and "usb_dev": which
    USB device it's part of (its input and output share it), or None if unknown."""
    try:
        ids = _lookup()
    except Exception as e:                  # detection then falls back to names
        log.debug("usb audio ids unavailable: %s", e)
        ids = lambda dev, pos=None: (None, None)        # noqa: E731
    pos = 0
    for d in devs:
        here = None
        if d.get("hostapi") == "Core Audio":
            here, pos = pos, pos + 1
        d["usb"], d["usb_dev"] = ids(d, here)
    return devs


def pi_devices(devs):
    """The Pi's devices among annotated ones: by USB ID, or else by name."""
    known = known_ids()
    mine = [d for d in devs if d.get("usb") in known]
    return mine or [d for d in devs if by_name(d)]


def _lookup():
    if sys.platform.startswith("linux"):
        return linux_ids()
    if sys.platform == "darwin":
        return mac_ids()
    return lambda dev, pos=None: (None, None)


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

    def lookup(dev, pos=None):
        m = HW.search(dev["name"])
        return (ids.get(m.group(1)), "card" + m.group(1)) if m and m.group(1) in ids else (None, None)
    return lookup


# ---- macOS

def mac_ids():
    from . import coreaudio
    return coreaudio.usb_ids()
