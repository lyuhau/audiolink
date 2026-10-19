"""`audiolink pis [PI] | invite | remove | forget | rename | ssh-config`: the Pis this
computer knows, and their lists (docs/state.md "Commands").

A Pi's list holds computer keys, each a user (passes through to the Pi's host) or an
owner (everything). This computer gets on a list by claiming a Pi over USB (setup), or
by redeeming an invite from an owner (setup, client/pairing.py).
"""
import collections
import getpass
import os
import shlex
import socket
import subprocess
import sys
import time

from ..names import NAME_RE
from . import keyring
from ..ui import ago, detail


def _quote(arg, windows=None):
    """One argument of a ProxyCommand: shell quoting, or Windows' (double quotes, backslashes
    kept as they are; shlex would put C:\\... in single quotes, which Windows doesn't know)."""
    if windows if windows is not None else sys.platform == "win32":
        return subprocess.list2cmdline([arg])
    return shlex.quote(arg)


def _launcher(argv=None, env=None):
    """How to run this same audiolink (or argv, with env) from an ssh ProxyCommand."""
    if argv:
        prefix = "" if sys.platform == "win32" or not env else "env " + "".join(
            "%s=%s " % (k, shlex.quote(v)) for k, v in sorted(env.items()))
        return prefix + " ".join(_quote(x) for x in argv)
    if getattr(sys, "frozen", False):
        # PyInstaller: sys.executable is the executable in its folder (sys.argv[0] may be a
        # link to it on PATH); resolved, so the entry works whatever the link does later
        return _quote(os.path.realpath(sys.executable))
    arg0 = os.path.abspath(sys.argv[0])
    if arg0.endswith(".pyz"):
        return "%s %s" % (_quote(sys.executable), _quote(arg0))
    checkout = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return "%s %s" % (_quote(sys.executable), _quote(checkout))     # its __main__.py


def _this_computer():
    return socket.gethostname().split(".")[0] or "computer"


# ---------------------------------------------------------------- pis
def pis_command(a):
    if getattr(a, "pi", None):
        return _one(a)
    pis = keyring.load()
    if not pis:
        print("No Pis yet (claim one: plug it in and run audiolink setup; or get an invite: audiolink invite "
              "on an owner computer, then audiolink setup)")
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
        rows.append((e["alias"], e.get("role") or "-", ago(e.get("last_seen")), rec.get("ssid") or "-",
                     "  (%s)" % ", ".join(notes) if notes else "", pub, rec))
    w0 = max(len("NAME"), max(len(r[0]) for r in rows))
    w1 = max(len("ROLE"), max(len(r[1]) for r in rows))
    w2 = max(len("LAST SEEN"), max(len(r[2]) for r in rows))
    print("%-*s  %-*s  %-*s  %s" % (w0, "NAME", w1, "ROLE", w2, "LAST SEEN", "NETWORK"))
    for alias, role, when, where, notes, pub, rec in rows:
        print("%-*s  %-*s  %-*s  %s%s" % (w0, alias, w1, role, w2, when, where, notes))
        detail("tag %s, addresses %s", pub[:4], ", ".join(rec.get("lan", [])) or "-")


def _one(a):
    """`audiolink pis PI`: online (on this network or through its relay) or when it was last
    seen, its code, and for an owner its list."""
    from .remote import Remote
    e = keyring.lookup(a.pi, known=True)
    name = e["alias"]
    try:
        r = Remote(e, list(getattr(a, "at", None) or [])).open(quiet=True)
    except SystemExit as why:
        print("%s: %s" % (name, why.code))
        r = None
    if r is not None:
        e = keyring.lookup(a.pi, known=True)
        print("%s: online, %s (this computer: %s)"
              % (name, "through its relay" if r.conn.ip == "relay" else "on this network", r.role))
    rec = e.get("last_record") or {}
    if rec.get("ts"):
        print("Last record: on %s, %s" % (rec.get("ssid") or "an unknown network", ago(rec["ts"])))
        print("Ports: %s" % (", ".join(map(str, rec.get("ports") or [])) or "none"))
        detail("addresses %s", ", ".join(rec.get("lan") or []) or "?")
    print("Code: %s" % e["code"])
    if r is not None and r.role == "owner":
        members = r.call("members")["members"]
        print("List:")
        mine = r.key.pub.hex()
        for m in sorted(members, key=lambda m: (m["role"] != "owner", m["name"])):
            print("  %-16s %-6s added %s%s" % (m["name"], m["role"], ago(m.get("added")),
                                              "  (this computer)" if m["key"] == mine else ""))
        if sum(m["role"] == "owner" for m in members) == 1:
            print("Only one owner: if that computer is lost, the Pi needs a reset. Another: audiolink invite NAME --owner")
    if r is not None:
        r.close()


# ---------------------------------------------------------------- USB: claim
def add_usb_entry(port, alias=None):
    """Add (or refresh) the Pi plugged in here, claiming it if it has no owner (USB is the
    trust). -> (entry, created, claimed just now, no saved state), or None if no Pi is
    reachable over USB from here."""
    from ..host.control import call
    from .computer import key as computer_key
    try:
        peer = call("peer", port=port)
    except SystemExit:
        return None                     # no host daemon running here
    if not peer.get("connected") or "identity" not in (peer.get("peer") or {}).get("caps", []):
        return None
    info = call("identity.info", port=port)
    first = False
    role = (peer.get("link") or {}).get("role")
    if not info.get("owners"):
        key = computer_key()
        call("identity.claim", {"key": key.pub.hex(), "name": _this_computer()}, port=port)
        first, role = True, "owner"
    entry, created = keyring.upsert(keyring.code_for(info["pub"]), name=info["name"], alias=alias,
                                    serial=info["serial"], seen=True)
    keyring.update(info["pub"], role=role)
    entry["role"] = role
    return entry, created, first, not info.get("persistent", True)


# ---------------------------------------------------------------- invite, remove
def _owner_remote(pi, at=()):
    from .remote import Remote
    e = keyring.lookup(pi, known=True)
    r = Remote(e, list(at)).open(quiet=True)
    if r.role != "owner":
        r.close()
        raise SystemExit("Only an owner of %s can do that (this computer is a %s)" % (e["alias"], r.role))
    return r


def _which_pi(a):
    """--pi, or the one Pi this computer owns."""
    if getattr(a, "pi", None):
        return a.pi
    owned = [e["alias"] for e in keyring.load().values() if e.get("role") == "owner"]
    if len(owned) == 1:
        return owned[0]
    if not owned:
        raise SystemExit("This computer owns no Pi (audiolink pis)")
    raise SystemExit("This computer owns %s: which one? --pi NAME" % ", ".join(sorted(owned)))


def invite_command(a):
    name = a.name.strip().lower()
    if not NAME_RE.match(name):
        raise SystemExit("Names: lowercase letters, digits and dashes, up to 32")
    role = "owner" if a.owner else "user"
    pi = _which_pi(a)
    r = _owner_remote(pi, getattr(a, "at", None) or [])
    try:
        got = r.call("invite", {"name": name, "role": role})
    finally:
        r.close()
    code = got["code"]
    print("Code for %s: %s-%s (one use, %d min)" % (name, code[:3], code[3:], got.get("expires_in", 300) // 60))
    print("%s will %s %s. On %s: audiolink setup, and type the code"
          % (name, "own" if role == "owner" else "pass through", r.name, name))
    if role == "user":
        print("%s logs in to %s's host only with its key in that computer's authorized_keys" % (name, r.name))


def remove_command(a):
    """Take NAME off every Pi this computer owns that lists it."""
    owned = [e for e in keyring.load().values() if e.get("role") == "owner"]
    if getattr(a, "pi", None):
        owned = [keyring.lookup(a.pi, known=True)]
    if not owned:
        raise SystemExit("This computer owns no Pi (audiolink pis)")
    removed, failed = [], []
    for e in sorted(owned, key=lambda e: e["alias"]):
        try:
            r = _owner_remote(e["alias"], getattr(a, "at", None) or [])
        except SystemExit as why:
            failed.append("%s: %s" % (e["alias"], why.code))
            continue
        try:
            members = r.call("members")["members"]
            if any(m["name"] == a.name for m in members):
                r.call("remove", {"name": a.name})
                removed.append(e["alias"])
        except SystemExit as why:
            failed.append("%s: %s" % (e["alias"], why.code))
        finally:
            r.close()
    if removed:
        print("%s: removed from %s" % (a.name, ", ".join(removed)))
    elif not failed:
        print("%s isn't on any Pi this computer owns" % a.name)
    if removed:
        print("Its line in your hosts' authorized_keys, if any, is yours to remove")
    if failed:
        raise SystemExit("Couldn't reach (nothing changed there):\n  " + "\n  ".join(failed))


def forget_command(a):
    """This computer drops the Pi, and takes itself off the Pi's list when it can."""
    from .remote import Remote
    e = keyring.lookup(a.name, known=True)
    left = False
    try:
        r = Remote(e, []).open(quiet=True)
        try:
            r.call("remove", {"key": r.key.pub.hex()})
            left = True
        finally:
            r.close()
    except SystemExit as why:
        detail("%s", why.code)
    keyring.remove(keyring.pub_of(e["code"]))
    print("Forgot %s%s" % (e["alias"], "; off its list too" if left else
                            "; it couldn't be reached, so this computer is still on its list "
                            "(an owner can remove it: audiolink remove %s)" % _this_computer()))


# ---------------------------------------------------------------- rename, ssh-config
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
    if not _rename_over_usb(a, pub, new):
        r = _owner_remote(e["alias"])
        try:
            if (r.hello or {}).get("name") != new:
                r.call("rename", {"name": new})
        finally:
            r.close()
    alias = keyring.unique_alias(keyring.load(), new, pub)
    keyring.update(pub, name=new, alias=alias, last_seen=int(time.time()))
    print("Renamed %s to %s: other computers see its new name; their own names for it stay" % (e["alias"], new))
    if alias != new:
        print("%s is taken here (another Pi); called it %s" % (new, alias))


def _rename_over_usb(a, pub, new):
    """Rename the Pi plugged in here, if that's this one (the link's role must be owner).
    -> whether it was."""
    from ..host.control import DEFAULT_PORT, call
    port = getattr(a, "control_port", None) or DEFAULT_PORT
    try:
        info = call("identity.info", port=port)
    except SystemExit:
        return False
    if info.get("pub") != pub:
        return False
    if info.get("name") != new:
        call("identity.rename", {"name": new}, port=port)
    return True


def ssh_port(entry, default=2222):
    """The Pi's port for ssh: the default if it offers it (or nothing's known), else the first it offers."""
    rec = entry.get("last_record")
    ports = (rec or {}).get("ports") or [default]
    return default if default in ports or not rec else ports[0]


def ssh_config_command(a):
    pis = keyring.load()
    if not pis:
        raise SystemExit("No Pis yet (audiolink setup)")
    user = a.user or getpass.getuser()
    if not a.user:
        sys.stderr.write("Logging in as %s, this computer's user (another: --user NAME)\n" % user)
    print("# audiolink: add to ~/.ssh/config once (appending again adds duplicates; ssh uses the first),\n"
          "# then: ssh NAME. Each entry logs in to the computer that Pi is plugged into, as %s." % user)
    for e in sorted(pis.values(), key=lambda e: e["alias"]):
        rec = e.get("last_record")
        port = ssh_port(e, a.port)
        if rec and not rec.get("ports"):
            sys.stderr.write("%s offered no ports when last found (on its host: audiolink host --expose %d:22)\n"
                             % (e["alias"], a.port))
        print("\nHost %s\n  HostName %s\n  Port %d\n  User %s\n  ProxyCommand %s connect %s %%p"
              % (e["alias"], e["alias"], port, user, _launcher(), e["alias"]))
