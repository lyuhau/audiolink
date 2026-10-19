"""Pi side: the Pi's own identity and its list, created the first time audiolink runs on it.

identity.json (in the Pi's state directory):
  seed      the Pi's ed25519 key (relay registration, the TLS certificate)
  name      advertised name; default adjective-noun from the board serial
  members   the Pi's list: [{"key": hex32, "name", "role": "user"|"owner", "added"}]
"Claimed" means an owner is on the list. The first owner is the host that claims the
Pi over USB (identity.claim, only while there's no owner: physical presence); the rest
come by invites (services/pairing.py). The last owner can't be removed: the reset pins
(or `reset`) start over instead.

An identity.json from before the list (a read secret, "claimed", "discoverable") keeps
its key and name and comes up unclaimed, to be claimed over USB again.

Nobody else keeps a copy of the key (hosts used to: "custody"). A Pi without an SD card
keeps its identity in RAM: a new key and an empty list at every boot ("persistent" in
the info says which, so a host can warn before claiming).
"""
import logging
import os
import threading
import time
from pathlib import Path

from ..crypto import ed25519_public
from ..names import NAME_RE, name_for
from ..paths import is_persistent, load_state, save_state
from . import Service
from .access import NONE, OWNER, ROLES, USER, Refused, not_owner, parse_key

log = logging.getLogger("audiolink")

OLD_FIELDS = ("secret", "claimed", "discoverable")
MAX_NAME = 64


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


def member_name(text):
    """A computer's name on the list (a hostname, or what an owner typed): trimmed, no
    control characters. Raises ValueError."""
    name = " ".join(str(text or "").split())
    if not name or len(name) > MAX_NAME or not name.isprintable():
        raise ValueError("a computer's name is 1-%d printable characters" % MAX_NAME)
    return name


def clean_members(members):
    """Entries as stored, dropping any that aren't well formed (a damaged or foreign copy)."""
    out, seen = [], set()
    for m in members if isinstance(members, list) else []:
        try:
            key = parse_key(m["key"]).hex()
            entry = {"key": key, "name": member_name(m["name"]), "role": m["role"], "added": int(m.get("added") or 0)}
        except (KeyError, TypeError, ValueError):
            continue
        if entry["role"] in ROLES and key not in seen:
            seen.add(key)
            out.append(entry)
    return out


class IdentityService(Service):
    name = "identity"
    caps = ("identity", "members")

    def __init__(self, state_dir):
        self.path = Path(state_dir) / "identity.json"
        self.persistent = is_persistent(state_dir)
        self.serial = board_serial() or os.urandom(8).hex()
        self.lock = threading.RLock()       # listeners may look things up
        self.listeners = []
        self.on_claim = []                  # fn(key) after a claim (services/access.py LinkAccess)
        self.inviter = None                 # (name, role) -> {"code", "expires_in"} (services/pairing.py)
        self.data = self._load()
        if not self.data:
            self.data = self._new()
            self._save()
            log.info("identity: new identity, name %s%s", self.data["name"],
                     "" if self.persistent else " (in RAM: lost at reboot)")

    def _new(self):
        return {"seed": os.urandom(32).hex(), "name": name_for(self.serial), "members": []}

    # ---- storage
    def _load(self):
        data = load_state(self.path)
        if data and any(k in data for k in OLD_FIELDS):
            data = {"seed": data["seed"], "name": data["name"], "members": clean_members(data.get("members"))}
            save_state(self.path, data)
            log.info("identity: from an older audiolink: kept the key and name %s; no owner until a host "
                     "claims it over USB", data["name"])
        elif data:
            data["members"] = clean_members(data.get("members"))
        return data

    def _save(self):
        save_state(self.path, self.data)

    def _changed(self):
        self._save()
        for fn in self.listeners:
            fn()
        if getattr(self, "session", None):
            self.session.send_event("identity", self.info())    # the host re-runs link.auth

    # ---- for other services
    def subscribe(self, fn):
        """fn() runs whenever the key, the name or the list change."""
        self.listeners.append(fn)

    def seed(self):
        return bytes.fromhex(self.data["seed"])

    def pi_name(self):
        return self.data["name"]

    def members(self):
        with self.lock:
            return [dict(m) for m in self.data["members"]]

    def member(self, key):
        """The list's entry for this key (32 bytes), or None."""
        hexkey = key.hex()
        with self.lock:
            return next((dict(m) for m in self.data["members"] if m["key"] == hexkey), None)

    def role_of(self, key):
        m = self.member(key) if key else None
        return m["role"] if m else NONE

    def owners(self):
        with self.lock:
            return sum(1 for m in self.data["members"] if m["role"] == OWNER)

    def claimed(self):
        return self.owners() > 0

    def public(self):
        seed = self.data["seed"]
        cached = getattr(self, "_public", None)
        if not cached or cached[0] != seed:             # every connection asks: compute once per key
            cached = self._public = (seed, ed25519_public(bytes.fromhex(seed)))
        return cached[1]

    def info(self):
        pub = self.public()
        return {"name": self.data["name"], "serial": self.serial, "pub": pub.hex(), "tag": pub.hex()[:4],
                "owners": self.owners(), "persistent": self.persistent}

    # ---- the list
    def claim(self, key, name):
        """The first owner (the host on USB, while there's none). -> the list."""
        key, name = parse_key(key), member_name(name)
        with self.lock:
            if self.claimed():
                raise Refused("claimed", "%s has an owner already: on an owner computer, audiolink invite NAME "
                                         "--owner" % self.data["name"])
            self.data["members"] = [{"key": key.hex(), "name": name, "role": OWNER, "added": int(time.time())}]
            self._changed()
        for fn in self.on_claim:
            fn(key)
        log.info("identity: claimed by %s", name)
        return self.members()

    def add(self, key, name, role):
        """A computer that redeemed an invite. Already on the list: it takes the invite's
        name, and the higher of the two roles. A name another computer has gets a number
        (laptop-2), so names stay unique. -> its entry."""
        if role not in ROLES:
            raise ValueError("roles are user and owner")
        hexkey, name = parse_key(key).hex(), member_name(name)
        with self.lock:
            members = self.data["members"]
            old = next((m for m in members if m["key"] == hexkey), None)
            taken = {m["name"].lower() for m in members if m["key"] != hexkey}
            unique, n = name, 1
            while unique.lower() in taken:
                n += 1
                unique = "%s-%d" % (name, n)
            if old:
                old.update(name=unique, role=OWNER if OWNER in (old["role"], role) else USER)
                entry = old
            else:
                entry = {"key": hexkey, "name": unique, "role": role, "added": int(time.time())}
                members.append(entry)
            self._changed()
            return dict(entry)

    def find(self, key=None, name=None):
        """The entry named by key (hex) or name, or None."""
        with self.lock:
            for m in self.data["members"]:
                if (key and m["key"] == str(key).lower()) or (name and m["name"].lower() == str(name).lower()):
                    return dict(m)
        return None

    def remove(self, hexkey):
        """Off the list: takes effect at once (the endpoint closes its connections)."""
        with self.lock:
            m = self.find(key=hexkey)
            if m is None:
                return None
            if m["role"] == OWNER and self.owners() == 1:
                raise Refused("last_owner", "%s is the last owner of %s: add another owner first (audiolink "
                                            "invite NAME --owner), or reset the Pi" % (m["name"], self.data["name"]))
            self.data["members"] = [e for e in self.data["members"] if e["key"] != m["key"]]
            self._changed()
        log.info("identity: %s (%s) taken off the list", m["name"], m["role"])
        return m

    def remove_for(self, key, role, args):
        """remove, by key or name, for a requester with this key and role: an owner
        removes anyone, anyone else only itself."""
        target = self.find(key=args.get("key"), name=args.get("name"))
        if role != OWNER and (target is None or key is None or target["key"] != key.hex()):
            me = self.member(key) if key else None
            raise not_owner(self, me["name"] if me else "this computer")
        if target is None:
            what = args.get("name") or args.get("key") or "nothing named"
            raise Refused("not_found", "%s isn't on %s's list" % (what, self.data["name"]))
        self.remove(target["key"])
        return {"ok": True}

    def start_over(self):
        """A new key under the board's default name, and an empty list (the reset pins,
        `reset`): every computer on the list is off it, and a host's custody copy isn't
        put back."""
        with self.lock:
            self.data = self._new()
            self._changed()
        log.info("identity: started over as %s", self.data["name"])

    def rename(self, name):
        name = str(name or "").strip().lower()
        if not NAME_RE.match(name):
            raise ValueError("names are 1-32 lowercase letters, digits and dashes")
        with self.lock:
            self.data["name"] = name
            self._changed()

    # ---- session hooks
    def hello(self):
        return {"identity": self.info()}

    def handle(self, op, args):
        with self.lock:
            if op == "info":
                return self.info()
            if op == "claim":
                return {"members": self.claim(args.get("key"), args.get("name"))}
            if op == "members":
                return {"members": self.members()}
            if op == "invite":
                return self.invite(args)
            if op == "remove":
                return self.remove_for(None, OWNER, args)       # the link: gated to owners
            if op == "rename":
                self.rename(args.get("name"))
                return self.info()
        raise ValueError("unknown identity operation %r" % op)

    def invite(self, args):
        role = args.get("role") or USER
        if role not in ROLES:
            raise ValueError("roles are user and owner")
        if self.inviter is None:
            raise ValueError("this Pi can't make invites")
        return self.inviter(member_name(args.get("name")), role)

    # ---- endpoint requests (services/endpoint.py)
    def serve(self, endpoint, reset=None):
        """members, invite, remove, rename (and reset, if given: () -> None, after the
        reply has gone)."""
        endpoint.register("members", lambda conn, req: {"members": self.members()}, OWNER)
        endpoint.register("invite", lambda conn, req: self.invite(req), OWNER)
        endpoint.register("remove", lambda conn, req: self.remove_for(conn.key, conn.role, req), USER)

        def rename(conn, req):
            self.rename(req.get("name"))
            return {"ok": True}
        endpoint.register("rename", rename, OWNER)
        if reset:
            def do_reset(conn, req):
                log.warning("identity: reset by %s from %s (%s)", conn.name, conn.addr[0], conn.route)
                threading.Thread(target=lambda: (time.sleep(1), reset()), daemon=True).start()   # reply first
                return {"ok": True}
            endpoint.register("reset", do_reset, OWNER)
