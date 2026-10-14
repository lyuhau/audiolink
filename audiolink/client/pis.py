"""`audiolink pis | add | share | forget | rename | ssh-config`: the Pis this machine knows.

add tries the ways a Pi can be reached, in order:
  1. USB: a Pi plugged into this machine (through the running `audiolink host`);
     physical possession is the trust, so no code is needed
  2. the network: Pis found over mDNS on the local network, or on the Pi's own setup
     network, or at --at ADDRESS; claimed Pis need a fresh pairing code (client/pairing.py)
  3. a connect code someone shared: `audiolink add al1-...`, checked through the relay
     or on the DHT when the Pi can be reached
The first machine to add a Pi claims it.
"""
import collections
import getpass
import os
import shlex
import socket
import subprocess
import sys
import threading
import time

from ..names import NAME_RE
from . import keyring
from ..ui import Prompter, ago, detail, done, step


def _quote(arg, windows=None):
    """One argument of a ProxyCommand: shell quoting, or Windows' (double quotes, backslashes
    kept as they are; shlex would put C:\\... in single quotes, which Windows doesn't know)."""
    if windows if windows is not None else sys.platform == "win32":
        return subprocess.list2cmdline([arg])
    return shlex.quote(arg)


def _launcher():
    """How to run this same audiolink from an ssh ProxyCommand."""
    if getattr(sys, "frozen", False):
        return _quote(sys.executable)
    arg0 = os.path.abspath(sys.argv[0])
    if arg0.endswith(".pyz"):
        return "%s %s" % (_quote(sys.executable), _quote(arg0))
    return "%s -m audiolink" % _quote(sys.executable)


def pis_command(a):
    pis = keyring.load()
    if not pis:
        print("No Pis yet (add one: audiolink add)")
        return
    aliases = {e["alias"] for e in pis.values()}
    called = collections.Counter(e.get("name") for e in pis.values())
    rows = []
    for pub, e in sorted(pis.items(), key=lambda kv: kv[1]["alias"]):
        rec = e.get("last_record") or {}
        own = e.get("name")
        notes = ["calls itself %s" % own] if own and own != e["alias"] else []
        if own and (called[own] > 1 or (own != e["alias"] and own in aliases)):
            notes.append("tag %s" % pub[:4])      # names alone don't tell these apart
        rows.append((e["alias"], ago(e.get("last_seen")), rec.get("ssid") or "-",
                     "  (%s)" % ", ".join(notes) if notes else "", pub, e, rec))
    w0 = max(len("NAME"), max(len(r[0]) for r in rows))
    w1 = max(len("LAST SEEN"), max(len(r[1]) for r in rows))
    print("%-*s  %-*s  %s" % (w0, "NAME", w1, "LAST SEEN", "NETWORK"))
    for alias, when, where, notes, pub, e, rec in rows:
        print("%-*s  %-*s  %s%s" % (w0, alias, w1, when, where, notes))
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
                                    custody=custody, seen=True)
    print("%s %s%s" % ("Added" if created else "Updated", entry["alias"],
                       " (claimed: first computer)" if claim.get("first") else ""))
    if custody:
        print("No SD card: keeping a backup of its identity here")
    return entry


OUTDATED = "That code is out of date (the Pi has a new one); ask for a fresh one: audiolink share NAME"


def _check_code(code, dht=True):
    """Does the Pi behind a code answer to it? -> (name, DHT record or None), or (None, None)
    if it can't be reached now. Ends the command if the Pi turns the code down. The relay
    proves it both ways (each side proves it holds the secret); a DHT record does too
    (signed with the Pi's key, readable only with the secret)."""
    from ..relay.client import Denied, connect
    from ..relay.wire import Refused, splice
    from ..services.endpoint import PORT
    from .remote import _through_relay
    pub, secret = keyring.parse(code)
    try:
        sock = connect(pub, secret, PORT)
    except Denied as e:
        detail("the relay: %s", e)
        raise SystemExit(OUTDATED if "not paired" in str(e) else "audiolink: %s" % e)
    except Refused as e:
        detail("the relay: %s", e)
        sock = None
    if sock is not None:
        mine, theirs = socket.socketpair()
        threading.Thread(target=splice, args=(theirs, sock), daemon=True).start()
        found = _through_relay(mine, pub, "the Pi")
        if found:
            found[0].close()
            return found[1].get("name"), None
    if dht:
        from .find import lookup
        detail("not on its relay; looking on the DHT (--no-lookup skips this)")
        try:
            rec, _ = lookup(code)
        except SystemExit:
            raise SystemExit(OUTDATED)
        if rec:
            return rec.get("name"), rec
    return None, None


def _add_code(a):
    pub = keyring.pub_of(a.code)
    step("Checking it")
    name, rec = _check_code(a.code, dht=not a.no_lookup)
    done("found %s" % name if name else "not reachable now")
    entry, created = keyring.upsert(a.code, name=name, alias=a.name, seen=bool(name and not rec))
    if rec:
        keyring.update(pub, last_record=rec)
        keyring.seen(pub, rec.get("ts"))
    print("%s %s%s" % ("Added" if created else "Updated", entry["alias"],
                       "" if name else "; not checked (it isn't reachable now)"))
    return entry


def add_command(a):
    from ..ui import line_buffered
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


def rotate_secret_command(a):
    """A new read secret for the plugged-in Pi: every other computer loses access."""
    from ..host.control import DEFAULT_PORT, call
    port = getattr(a, "control_port", None) or DEFAULT_PORT
    peer = call("peer", port=port)
    if not peer.get("connected"):
        raise SystemExit("No Pi plugged in")
    caps = (peer.get("peer") or {}).get("caps", [])
    if "rotate_secret" not in caps:
        raise SystemExit("The Pi's audiolink can't do this; update it (audiolink update)")
    info = call("identity.info", port=port)
    known = keyring.load().get(info["pub"])
    if known is None:
        raise SystemExit("%s isn't added here (it would be the only computer left with a code). "
                         "Add it first: audiolink add" % info["name"])
    if not getattr(a, "yes", False) and not Prompter(a, "--yes").yes(
            "New code for %s: every other computer that added it loses access. Go ahead?" % known["alias"]):
        raise SystemExit("Not changed")
    code = call("identity.rotate_secret", port=port)["code"]
    custody = None if info["persistent"] else call("identity.export", port=port)
    entry, _ = keyring.upsert(code, name=info["name"], serial=info["serial"], custody=custody, seen=True)
    print("New code for %s (kept here)" % entry["alias"])
    print("Other computers lost access; to add one again: audiolink pairing code %s, then audiolink add there"
          % entry["alias"])
    if "debug" in caps:
        try:
            mode = call("debug.status", port=port).get("mode")
        except SystemExit:
            mode = None
        if mode in ("key", "none"):
            print("Debug access (%s) doesn't use the code: it stays until audiolink debug off"
                  % ("by key" if mode == "key" else "for anyone"))


def share_command(a):
    e = keyring.lookup(a.name, known=True)
    name = e["alias"]
    sys.stderr.write("Anyone with this code can use and manage %s from anywhere, including over the relay.\n"
                     "To take it back: audiolink rotate-secret (%s plugged in).\n" % (name, name))
    sys.stderr.flush()
    print(e["code"])


def forget_command(a):
    e = keyring.lookup(a.name, known=True)
    if e.get("custody") and not getattr(a, "yes", False):
        print("This computer keeps the backup of %s's identity (it has no SD card): without it, or another\n"
              "computer's, it gets a new identity (and code) when it restarts" % e["alias"])
        if not Prompter(a, "--yes").yes("Forget %s anyway?" % e["alias"]):
            raise SystemExit("Not forgotten")
    keyring.remove(keyring.pub_of(e["code"]))
    print("Forgot %s" % e["alias"])
    print("Its code still works on the Pi; to also cut this computer off: audiolink rotate-secret "
          "(on the computer it's plugged into)")


def rename_command(a):
    e = keyring.lookup(a.name, known=True)
    pub = keyring.pub_of(e["code"])
    new = a.new.strip().lower()
    if not NAME_RE.match(new):
        raise SystemExit("Names: lowercase letters, digits and dashes, up to 32")
    taken = any(p != pub and x.get("alias") == new for p, x in keyring.load().items())
    if not a.on_pi:
        if e["alias"] == new:
            print("It's already called %s here" % new)
            return
        if taken:
            raise SystemExit("%s is taken (another Pi here); pick another name" % new)
        keyring.update(pub, alias=new)
        print("Renamed %s to %s on this computer (--on-pi renames the Pi itself)" % (e["alias"], new))
        return
    from ..host.control import DEFAULT_PORT, call
    port = getattr(a, "control_port", None) or DEFAULT_PORT
    info = call("identity.info", port=port)
    if info["pub"] != pub:
        raise SystemExit("The Pi plugged in here isn't %s" % e["alias"])
    if info["name"] == new and e["alias"] == new:
        print("It's already called %s" % new)
        return
    if info["name"] != new:
        call("identity.rename", {"name": new}, port=port)
    alias = keyring.unique_alias(keyring.load(), new, pub)
    keyring.update(pub, name=new, alias=alias, last_seen=int(time.time()))
    print("Renamed %s to %s: other computers see its new name; their own names for it stay" % (e["alias"], new))
    if alias != new:
        print("%s is taken here (another Pi); called it %s" % (new, alias))


def ssh_config_command(a):
    pis = keyring.load()
    if not pis:
        raise SystemExit("No Pis yet (add one: audiolink add)")
    user = a.user or getpass.getuser()
    if not a.user:
        sys.stderr.write("Logging in as %s, this computer's user (another: --user NAME)\n" % user)
    print("# audiolink: add to ~/.ssh/config once (appending again adds duplicates; ssh uses the first),\n"
          "# then: ssh NAME. Each entry logs in to the computer that Pi is plugged into, as %s." % user)
    for e in sorted(pis.values(), key=lambda e: e["alias"]):
        rec = e.get("last_record")
        ports = (rec or {}).get("ports") or [a.port]
        port = a.port if a.port in ports or not rec else ports[0]
        if rec and not rec.get("ports"):
            sys.stderr.write("%s offered no ports when last found (on its host: audiolink host --expose %d:22)\n"
                             % (e["alias"], a.port))
        print("\nHost %s\n  HostName %s\n  Port %d\n  User %s\n  ProxyCommand %s connect %s %%p"
              % (e["alias"], e["alias"], port, user, _launcher(), e["alias"]))
