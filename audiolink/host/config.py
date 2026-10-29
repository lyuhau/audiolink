"""The host's saved settings (config dir / host.json), written by `audiolink setup host` and
read by `audiolink host`: what it exposes, forwards and allows, the audio format, its local
ports and the Pi's sound devices. A flag given on the command line wins over the saved
value (a list flag such as --expose replaces the saved list; --usb-id adds to it: the
saved usb_ids are read live, wire/usb_audio.py).

  {"expose": ["2222:22"], "forward": [], "allow": [], "audio_format": "auto",
   "proxy_port": 47801, "shell_port": 47802, "control_port": 47800,
   "out_device": null, "in_device": null, "usb_ids": []}

usb_ids: more USB IDs (VID:PID) whose sound devices are the Pi's, next to the built-in
ones (wire/usb_audio.py): a Pi from a fork that presents its own, or in a flavor someone
added (`audiolink flavor`, the Pi's network record, and the Pi itself before it becomes
another device, and whenever its link comes up, add them: remember_usb_ids; the MAX_USB_IDS
told most recently stay). A running host reads them again when this file changes: one
removed from it here is gone there too.
"""
import argparse
import json
import os

from .. import argtypes
from ..paths import config_dir, save_json

# setting -> (built-in value, argparse type of one value, is a list)
FIELDS = {
    "expose": ([], argtypes.expose, True),
    "forward": ([], argtypes.forward, True),
    "allow": ([], argtypes.allow, True),
    "audio_format": ("auto", argtypes.audio_format, False),
    "proxy_port": (47801, argtypes.port_or_off, False),
    "shell_port": (47802, argtypes.port_or_off, False),
    "control_port": (None, argtypes.port, False),
    "out_device": (None, str, False),
    "in_device": (None, str, False),
    "usb_ids": ([], argtypes.usb_id, True),
}
ADDS = {"usb_ids"}              # list flags that add to the saved list (read live) instead of replacing it
MAX_USB_IDS = 32                # remember_usb_ids keeps the most recently told


def path():
    return config_dir() / "host.json"


def load(strict=True):
    """The saved settings ({} if none). strict: a damaged file or a bad value ends the
    command with what's wrong; otherwise it's ignored (commands that only want the port)."""
    p = path()
    try:
        with open(str(p)) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return check(data)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        if strict:
            raise SystemExit("Can't read %s (%s); fix it, or run audiolink setup host again" % (p, e))
        return {}


def check(data):
    """Saved settings with every value checked like its flag; unknown keys are dropped."""
    out = {}
    for key, value in data.items():
        if key not in FIELDS or value is None:
            continue
        _, kind, many = FIELDS[key]
        values = value if many else [value]
        if many and not isinstance(value, list):
            raise ValueError("%s: wants a list" % key)
        try:
            checked = [kind(str(v)) for v in values]
        except argparse.ArgumentTypeError as e:
            raise ValueError("%s: %s" % (key, e))
        out[key] = checked if many else checked[0]
    return out


def save(data):
    """Replace the saved settings (checked first); returns the file's path."""
    data = check(data)
    p = path()
    save_json(p, {k: data[k] for k in FIELDS if k in data}, indent=1)
    return p


def update(**changes):
    """Change some settings, keeping the rest; None removes one (back to its built-in value)."""
    data = load()
    for k, v in changes.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    return save(data)


def remember_usb_ids(ids):
    """Add Pis' USB IDs ("vvvv:pppp") to usb_ids, those that aren't built in; ones saved
    already move to the end (the most recent). Only the last MAX_USB_IDS stay, so a Pi
    can't grow host.json without end. -> the ones added. Never fails: a damaged
    host.json stays as it is."""
    from ..wire.usb_audio import USB_IDS, parse_id
    try:
        data = load()
        saved = list(data.get("usb_ids", []))
        ids = [argtypes.usb_id(str(i)) for i in ids or []]
        ids = [i for n, i in enumerate(ids) if parse_id(i) not in USB_IDS and i not in ids[:n]]
        new = [i for i in ids if i not in saved]
        keep = ([i for i in saved if i not in ids] + ids)[-MAX_USB_IDS:]
        if keep != saved:
            update(usb_ids=keep)
        return [i for i in new if i in keep]
    except (SystemExit, OSError, ValueError, argparse.ArgumentTypeError):
        return []


def apply(a, saved=None):
    """Fill in `audiolink host`'s arguments: the command line's value, else the saved one,
    else the built-in one (the host parser's defaults are None for these). ADDS: only the
    command line's; the saved ones are read live (wire/usb_audio.py saved_ids), so one
    removed from host.json goes without a restart."""
    saved = load() if saved is None else saved
    for key, (builtin, _, many) in FIELDS.items():
        if key in ADDS:
            setattr(a, key, list(getattr(a, key, None) or []))
        elif getattr(a, key, None) is None:
            value = saved.get(key, builtin)
            setattr(a, key, list(value) if many else value)
    return a


def control_port():
    """The saved control port, for commands not given --control-port (None: the default).
    $AUDIOLINK_CONTROL_PORT wins over it."""
    if os.environ.get("AUDIOLINK_CONTROL_PORT"):
        return None
    return load(strict=False).get("control_port")
