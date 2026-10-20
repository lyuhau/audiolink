"""The host's saved settings (config dir / host.json), written by `audiolink setup host` and
read by `audiolink host`: what it exposes, forwards and allows, the audio format, its local
ports and the Pi's sound devices. A flag given on the command line wins over the saved
value (a list flag such as --expose replaces the saved list).

  {"expose": ["2222:22"], "forward": [], "allow": [], "audio_format": "auto",
   "proxy_port": 47801, "shell_port": 47802, "control_port": 47800,
   "out_device": null, "in_device": null, "usb_ids": []}

usb_ids: more USB IDs (VID:PID) whose sound devices are the Pi's, next to the built-in
ones (wire/usb_audio.py): a Pi from a fork that presents its own.
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


def apply(a, saved=None):
    """Fill in `audiolink host`'s arguments: the command line's value, else the saved one,
    else the built-in one (the host parser's defaults are None for these)."""
    saved = load() if saved is None else saved
    for key, (builtin, _, many) in FIELDS.items():
        if getattr(a, key, None) is None:
            value = saved.get(key, builtin)
            setattr(a, key, list(value) if many else value)
    return a


def control_port():
    """The saved control port, for commands not given --control-port (None: the default).
    $AUDIOLINK_CONTROL_PORT wins over it."""
    if os.environ.get("AUDIOLINK_CONTROL_PORT"):
        return None
    return load(strict=False).get("control_port")
