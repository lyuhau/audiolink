"""`audiolink pis | add | share | forget | rename | ssh-config`: the Pis this machine knows.

add tries the ways a Pi can be reached, in order:
  1. USB: a Pi plugged into this machine (through the running `audiolink host`);
     physical possession is the trust, so no code is needed
  2. the network: Pis found over mDNS on the local network, or on the Pi's own setup
     network, or at --at ADDRESS; claimed Pis need a fresh pairing code (client/pairing.py)
  3. a connect code someone shared: `audiolink add al1-...`
The first machine to add a Pi claims it.
"""
import getpass
import os
import shlex
import sys
import time

from ..discovery.record import parse_code
from ..names import NAME_RE
from . import keyring
from ..ui import detail


def _launcher():
    """How to run this same audiolink from an ssh ProxyCommand."""
    if getattr(sys, "frozen", False):
        return shlex.quote(sys.executable)
    arg0 = os.path.abspath(sys.argv[0])
    if arg0.endswith(".pyz"):
        return "%s %s" % (shlex.quote(sys.executable), shlex.quote(arg0))
    return "%s -m audiolink" % shlex.quote(sys.executable)


def _ago(ts):
    if not ts:
        return "never"
    s = time.time() - ts
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= n:
            return "%d %s ago" % (s // n, unit)
    return "just now"


def pis_command(a):
    pis = keyring.load()
    if not pis:
        print("You haven't added any Pis yet. Plug one in and run: audiolink add")
        return
    print("%-20s %-12s %s" % ("NAME", "LAST SEEN", "LAST SEEN ON"))
    for pub, e in sorted(pis.items(), key=lambda kv: kv[1]["alias"]):
        rec = e.get("last_record") or {}
        where = rec.get("ssid") or "-"
        own = e.get("name")
        print("%-20s %-12s %s%s" % (e["alias"], _ago(e.get("last_seen")), where,
                                    "  (calls itself %s)" % own if own and own != e["alias"] else ""))
        detail("tag %s, addresses %s%s", pub[:4], ", ".join(rec.get("lan", [])) or "-",
               ", identity backup kept here" if e.get("custody") else "")


def _add_usb(a):
    """Returns the entry, or None if no Pi is reachable over USB from here."""
    from ..host.control import DEFAULT_PORT, call
    port = getattr(a, "control_port", None) or DEFAULT_PORT
    try:
        peer = call("peer", port=port)
    except SystemExit:
        return None                     # no host daemon running here
    if not peer.get("connected") or "identity" not in (peer.get("peer") or {}).get("caps", []):
        return None
    info = call("identity.info", port=port)
    claim = call("identity.claim", port=port)
    code = call("identity.code", port=port)["code"]
    custody = None if info["persistent"] else call("identity.export", port=port)
    entry, created = keyring.upsert(code, name=info["name"], alias=a.name, serial=info["serial"],
                                    custody=custody)
    print(("Added %s, the Pi plugged in here." if created else "%s was already added; updated its details.")
          % entry["alias"])
    if claim.get("first"):
        print("This is the first computer to add it, so it's yours now.")
    if custody:
        print("It has no SD card, so this computer keeps a backup of its identity for it.")
    return entry


def _add_code(a):
    try:
        pub, _ = parse_code(a.code)
    except ValueError as e:
        raise SystemExit(str(e))
    name = None
    if not a.no_lookup:
        from .find import lookup
        print("Looking up its name...", flush=True)
        detail("on the DHT; --no-lookup skips this")
        rec, _ = lookup(a.code)
        name = rec and rec.get("name")
    entry, created = keyring.upsert(a.code, name=name, alias=a.name)
    print(("Added %s." if created else "Updated %s.") % entry["alias"])
    return entry


def add_command(a):
    from ..ui import Prompter, line_buffered
    from .pairing import add_over_network
    line_buffered()
    if a.code:
        entry = _add_code(a)
    else:
        entry = None if a.at else _add_usb(a)
        if entry is None:
            print("Looking for Pis on this network...", flush=True)
            entry = add_over_network(a, Prompter(a, "--pairing-code, --at"))
    if entry is None:
        raise SystemExit("Couldn't find a new Pi. Ways to add one:\n"
                         "  - plug it in here, with `audiolink host` running, and run audiolink add again\n"
                         "  - be on the same network as it (if it still isn't found: --at ITS-ADDRESS)\n"
                         "  - join its setup network (named audiopi-...) and run audiolink add\n"
                         "  - on a computer that knows it, run `audiolink share NAME`, then here:\n"
                         "    audiolink add al1-...")
    print("To use it with ssh: audiolink ssh-config >> ~/.ssh/config, then: ssh %s" % entry["alias"])


def _resolve(name):
    try:
        return keyring.resolve(name)
    except KeyError:
        raise SystemExit("You don't have a Pi called %s. To see yours: audiolink pis" % name)


def share_command(a):
    e = _resolve(a.name)
    print(e["code"])


def forget_command(a):
    e = _resolve(a.name)
    keyring.remove(keyring.pub_of(e["code"]))
    print("Forgot %s." % e["alias"])
    if e.get("custody"):
        print("This computer was keeping a backup of its identity. If it has no SD card and no other\n"
              "computer has added it, it gets a new identity (and a new code) when it restarts.")


def rename_command(a):
    e = _resolve(a.name)
    pub = keyring.pub_of(e["code"])
    new = a.new.strip().lower()
    if not NAME_RE.match(new):
        raise SystemExit("Names can only use lowercase letters, digits and dashes (up to 32 characters).")
    if a.on_pi:
        from ..host.control import DEFAULT_PORT, call
        port = getattr(a, "control_port", None) or DEFAULT_PORT
        info = call("identity.info", port=port)
        if info["pub"] != pub:
            raise SystemExit("The Pi plugged in here isn't %s. Plug %s in to rename it." % (e["alias"], e["alias"]))
        call("identity.rename", {"name": new}, port=port)
        keyring.update(pub, name=new)
        print("Renamed the Pi itself to %s. Other computers pick up the new name the next time they\n"
              "look it up." % new)
    alias = keyring.unique_alias(keyring.load(), new, pub)
    keyring.update(pub, alias=alias)
    if not a.on_pi:
        print("On this computer it's now called %s. (To rename the Pi itself, so every computer\n"
              "sees the new name: add --on-pi with it plugged in here.)" % alias)
    elif alias != new:
        print("On this computer it's called %s, since you already have a Pi called %s." % (alias, new))


def ssh_config_command(a):
    pis = keyring.load()
    if not pis:
        raise SystemExit("You haven't added any Pis yet. Plug one in and run: audiolink add")
    user = a.user or getpass.getuser()
    print("# audiolink: add this to ~/.ssh/config, then connect with: ssh NAME")
    for e in sorted(pis.values(), key=lambda e: e["alias"]):
        ports = (e.get("last_record") or {}).get("ports") or [a.port]
        port = a.port if a.port in ports or not e.get("last_record") else ports[0]
        print("\nHost %s\n  HostName %s\n  Port %d\n  User %s\n  ProxyCommand %s connect %s %%p"
              % (e["alias"], e["alias"], port, user, _launcher(), e["alias"]))
