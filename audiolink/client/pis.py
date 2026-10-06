"""`audiolink pis | add | share | forget | rename | ssh-config`: the Pis this machine knows.

add tries the ways a Pi can be reached, in order:
  1. USB: a Pi plugged into this machine (through the running `audiolink host`);
     physical possession is the trust, so no code is needed
  2. (next) the local network, then the Pi's own setup network, with a pairing code
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
        print("no Pis known yet; plug one in and run: audiolink add")
        return
    print("%-20s %-20s %-5s %-11s %s" % ("ALIAS", "CALLS ITSELF", "TAG", "LAST SEEN", "LAST KNOWN AT"))
    for pub, e in sorted(pis.items(), key=lambda kv: kv[1]["alias"]):
        rec = e.get("last_record") or {}
        where = ("%s on %r" % (", ".join(rec.get("lan", [])), rec.get("ssid"))) if rec else "-"
        print("%-20s %-20s %-5s %-11s %s%s" % (e["alias"], e.get("name") or "?", pub[:4],
                                               _ago(e.get("last_seen")), where,
                                               "  (custody here)" if e.get("custody") else ""))


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
    print("%s %s over USB%s" % ("added" if created else "refreshed", entry["alias"],
                                " (you're its first: claimed it)" if claim.get("first") else ""))
    if custody:
        print("  it has no SD card yet, so this machine keeps a custody copy of its identity")
    return entry


def _add_code(a):
    try:
        pub, _ = parse_code(a.code)
    except ValueError as e:
        raise SystemExit(str(e))
    name = None
    if not a.no_lookup:
        from .find import lookup
        print("looking it up on the DHT for its name (skip with --no-lookup)...", flush=True)
        rec, _ = lookup(a.code)
        name = rec and rec.get("name")
    entry, created = keyring.upsert(a.code, name=name, alias=a.name)
    print("%s %s" % ("added" if created else "updated", entry["alias"]))
    return entry


def add_command(a):
    entry = _add_code(a) if a.code else _add_usb(a)
    if entry is None:
        raise SystemExit("no Pi is plugged into this machine (or `audiolink host` isn't running).\n"
                         "pairing over the local network / the Pi's setup network is coming next;\n"
                         "meanwhile, on a machine that knows the Pi: audiolink share NAME, then here:\n"
                         "  audiolink add al1-...")
    print("connect with:  %s connect %s PORT   (or: audiolink ssh-config)" % (_launcher(), entry["alias"]))


def _resolve(name):
    try:
        return keyring.resolve(name)
    except KeyError:
        raise SystemExit("no Pi called %r here; see: audiolink pis" % name)


def share_command(a):
    e = _resolve(a.name)
    print(e["code"])


def forget_command(a):
    e = _resolve(a.name)
    keyring.remove(keyring.pub_of(e["code"]))
    print("forgot %s" % e["alias"])
    if e.get("custody"):
        print("note: this machine held its identity in custody (no SD card); unless another machine\n"
              "added it too, it gets a new identity, and a new code, at its next reboot")


def rename_command(a):
    e = _resolve(a.name)
    pub = keyring.pub_of(e["code"])
    new = a.new.strip().lower()
    if not NAME_RE.match(new):
        raise SystemExit("names are 1-32 lowercase letters, digits and dashes")
    if a.on_pi:
        from ..host.control import DEFAULT_PORT, call
        port = getattr(a, "control_port", None) or DEFAULT_PORT
        info = call("identity.info", port=port)
        if info["pub"] != pub:
            raise SystemExit("the Pi plugged in here isn't %s; plug it in to rename it" % e["alias"])
        call("identity.rename", {"name": new}, port=port)
        keyring.update(pub, name=new)
        print("%s now calls itself %s (other machines see it on their next lookup)" % (e["alias"], new))
    alias = keyring.unique_alias(keyring.load(), new, pub)
    keyring.update(pub, alias=alias)
    print("this machine now calls it %s%s" % (alias, "" if a.on_pi else
                                             "  (only here; add --on-pi to rename the Pi itself)"))


def ssh_config_command(a):
    pis = keyring.load()
    if not pis:
        raise SystemExit("no Pis known yet; see: audiolink add")
    user = a.user or getpass.getuser()
    print("# audiolink: paste into ~/.ssh/config (then: ssh ALIAS)")
    for e in sorted(pis.values(), key=lambda e: e["alias"]):
        ports = (e.get("last_record") or {}).get("ports") or [a.port]
        port = a.port if a.port in ports or not e.get("last_record") else ports[0]
        print("\nHost %s\n  HostName %s\n  Port %d\n  User %s\n  ProxyCommand %s connect %s %%p"
              % (e["alias"], e["alias"], port, user, _launcher(), e["alias"]))
