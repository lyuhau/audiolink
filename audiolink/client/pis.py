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
from ..ui import detail, done, step


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
        print("No Pis yet (add one: audiolink add)")
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
    print("%s %s%s" % ("Added" if created else "Updated", entry["alias"],
                       " (claimed: first computer)" if claim.get("first") else ""))
    if custody:
        print("No SD card: keeping a backup of its identity here")
    return entry


def _add_code(a):
    try:
        pub, _ = parse_code(a.code)
    except ValueError as e:
        raise SystemExit(str(e))
    name = None
    if not a.no_lookup:
        from .find import lookup
        step("Looking up its name")
        detail("on the DHT; --no-lookup skips this")
        rec, _ = lookup(a.code)
        name = rec and rec.get("name")
        done(name or "not found")
    entry, created = keyring.upsert(a.code, name=name, alias=a.name)
    print("%s %s" % ("Added" if created else "Updated", entry["alias"]))
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
            print("Searching this network...", flush=True)
            entry = add_over_network(a, Prompter(a, "--pairing-code, --at"))
    if entry is None:
        raise SystemExit("No new Pi found. To add one:\n"
                         "  plug it in here (with audiolink host running)\n"
                         "  or join its network or setup network (audiopi-...)  [--at ADDRESS if not found]\n"
                         "  or paste its code: audiolink add al1-...  (from: audiolink share NAME)")
    print("Connect: audiolink ssh-config >> ~/.ssh/config, then ssh %s" % entry["alias"])


def _resolve(name):
    try:
        return keyring.resolve(name)
    except KeyError:
        raise SystemExit("No Pi named %s (see: audiolink pis)" % name)


def share_command(a):
    e = _resolve(a.name)
    print(e["code"])


def forget_command(a):
    e = _resolve(a.name)
    keyring.remove(keyring.pub_of(e["code"]))
    print("Forgot %s" % e["alias"])
    if e.get("custody"):
        print("Note: dropped its identity backup; without an SD card or another computer's backup,\n"
              "it gets a new identity (and code) when it restarts")


def rename_command(a):
    e = _resolve(a.name)
    pub = keyring.pub_of(e["code"])
    new = a.new.strip().lower()
    if not NAME_RE.match(new):
        raise SystemExit("Names: lowercase letters, digits and dashes, up to 32")
    if a.on_pi:
        from ..host.control import DEFAULT_PORT, call
        port = getattr(a, "control_port", None) or DEFAULT_PORT
        info = call("identity.info", port=port)
        if info["pub"] != pub:
            raise SystemExit("The Pi plugged in here isn't %s" % e["alias"])
        call("identity.rename", {"name": new}, port=port)
        keyring.update(pub, name=new)
        print("Renamed %s to %s (other computers update on their next lookup)" % (e["alias"], new))
    alias = keyring.unique_alias(keyring.load(), new, pub)
    keyring.update(pub, alias=alias)
    if not a.on_pi:
        print("Renamed %s to %s on this computer (--on-pi renames the Pi itself)" % (e["alias"], alias))
    elif alias != new:
        print("Here it's %s (another Pi is already named %s)" % (alias, new))


def ssh_config_command(a):
    pis = keyring.load()
    if not pis:
        raise SystemExit("No Pis yet (add one: audiolink add)")
    user = a.user or getpass.getuser()
    print("# audiolink: add to ~/.ssh/config, then: ssh NAME")
    for e in sorted(pis.values(), key=lambda e: e["alias"]):
        ports = (e.get("last_record") or {}).get("ports") or [a.port]
        port = a.port if a.port in ports or not e.get("last_record") else ports[0]
        print("\nHost %s\n  HostName %s\n  Port %d\n  User %s\n  ProxyCommand %s connect %s %%p"
              % (e["alias"], e["alias"], port, user, _launcher(), e["alias"]))
