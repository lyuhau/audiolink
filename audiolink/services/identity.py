"""Pi side: the Pi's own identity, created the first time audiolink runs on it.

identity.json (in the Pi's state directory):
  seed, secret   signing key and read secret (the "connect code" is pub + secret)
  name           advertised name; default adjective-noun from the board serial
  claimed        False until the first machine adds it ("first to claim owns it")
  discoverable   publish the discovery record on the DHT

Requests arrive over the audio link, i.e. from the machine the Pi is physically
plugged into, so they're trusted. "rotate_secret" is revocation: a new read secret
(same key, still claimed), so every machine holding the old code has to pair again. The host that added the Pi keeps a copy ("custody")
and restores it whenever the Pi comes up with a fresh identity (host/identity.py):
after every reboot if the state directory is in RAM (no SD card), and once when a
Pi first gets a card. A restore never replaces an established identity on a card.
"""
import logging
import os
import threading
from pathlib import Path

from ..crypto import ed25519_public
from ..discovery.record import code_for, new_identity
from ..names import NAME_RE, name_for
from ..paths import is_persistent, load_state, save_state
from . import Service

log = logging.getLogger("audiolink")



def board_serial():
    """The SoC serial (unique per board), else the Wi-Fi MAC, else None."""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Serial"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    try:
        return Path("/sys/class/net/wlan0/address").read_text().strip()
    except OSError:
        return None


class IdentityService(Service):
    name = "identity"
    caps = ("identity", "rotate_secret")

    def __init__(self, state_dir):
        self.path = Path(state_dir) / "identity.json"
        self.persistent = is_persistent(state_dir)
        self.serial = board_serial() or os.urandom(8).hex()
        self.lock = threading.Lock()
        self.listeners = []
        self.data = self._load()
        self.fresh = not self.data          # created this run, nothing has happened to it yet
        if not self.data:
            self.data = dict(new_identity(), name=name_for(self.serial), claimed=False, discoverable=True)
            self._save()
            log.info("identity: new identity, name %s%s", self.data["name"],
                     "" if self.persistent else " (in RAM: lost at reboot unless a host keeps custody)")

    # ---- storage
    def _load(self):
        return load_state(self.path)

    def _save(self):
        save_state(self.path, self.data)

    def _changed(self):
        self.fresh = False
        self._save()
        for fn in self.listeners:
            fn()
        if getattr(self, "session", None):
            self.session.send_event("identity", self.info())    # host refreshes its custody copy

    # ---- for other services
    def subscribe(self, fn):
        """fn() runs whenever the identity, name or flags change."""
        self.listeners.append(fn)

    def keys(self):
        return bytes.fromhex(self.data["seed"]), bytes.fromhex(self.data["secret"])

    def pi_name(self):
        return self.data["name"]

    def claimed(self):
        return self.data["claimed"]

    def discoverable(self):
        return self.data["discoverable"]

    def code(self):
        """The connect code (public key + read secret) that paired machines keep."""
        return code_for(self.data)

    def claim(self):
        """Mark the Pi as claimed (its first pairing). Returns whether it was unclaimed."""
        return self.handle("claim", {})["first"]

    def start_over(self):
        """A new identity under the board's default name, unclaimed (the reset pins):
        codes given out stop working, and a host's custody copy isn't put back."""
        with self.lock:
            self.data = dict(new_identity(), name=name_for(self.serial), claimed=False, discoverable=True)
            self._changed()
        log.info("identity: started over as %s", self.data["name"])

    def public(self):
        seed = self.data["seed"]
        cached = getattr(self, "_public", None)
        if not cached or cached[0] != seed:             # every connection asks: compute once per key
            cached = self._public = (seed, ed25519_public(bytes.fromhex(seed)))
        return cached[1]

    def info(self):
        pub = self.public()
        return {"name": self.data["name"], "serial": self.serial, "pub": pub.hex(), "tag": pub.hex()[:4],
                "claimed": self.data["claimed"], "discoverable": self.data["discoverable"],
                "persistent": self.persistent, "fresh": self.fresh}

    # ---- session hooks
    def hello(self):
        return {"identity": self.info()}

    def handle(self, op, args):
        with self.lock:
            if op == "info":
                return self.info()
            if op == "code":
                return {"code": code_for(self.data)}
            if op == "claim":
                first = not self.data["claimed"]
                self.data["claimed"] = True
                self._changed()
                return {"first": first}
            if op == "rename":
                name = args.get("name", "").strip().lower()
                if not NAME_RE.match(name):
                    raise ValueError("names are 1-32 lowercase letters, digits and dashes")
                self.data["name"] = name
                self._changed()
                return self.info()
            if op == "rotate_secret":
                self.data["secret"] = new_identity()["secret"]
                self._changed()
                log.info("identity: new read secret; the old code stops working")
                return {"code": code_for(self.data)}
            if op == "set_discoverable":
                self.data["discoverable"] = bool(args.get("on"))
                self._changed()
                return self.info()
            if op == "export":
                return dict(self.data)
            if op == "restore":
                if self.persistent and not self.fresh and args.get("seed") != self.data["seed"]:
                    raise ValueError("this Pi keeps its own identity on its SD card; not replacing it")
                self.data = {k: args[k] for k in ("seed", "secret", "name", "claimed", "discoverable")}
                self._changed()
                log.info("identity: restored %s from the host's custody copy", self.data["name"])
                return self.info()
        raise ValueError("unknown identity operation %r" % op)
