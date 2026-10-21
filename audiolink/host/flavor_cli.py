"""`audiolink flavor [use NAME | add FILE [--name NAME] | remove NAME | try FILE]`: what
the Pi presents on USB (services/flavor.py; docs/flavors-plan.md).

`use` and `try` are kept only once a computer's USB link comes back on the new device;
otherwise the Pi goes back by itself. The command waits and says which happened. This
computer learns the flavor's USB ID first (the running host, and host.json once kept or
added), so it keeps finding the Pi. Over the network (--pi) there must be a computer
plugged into the Pi to confirm a switch. Owners only.
"""
import os
import re
import time

from ..ui import detail, done, step
from .pi_call import pi_caller

POLL = 2
SLACK = 150                     # s past the Pi's own deadline: the gap before going back, a boot's worth


def flavor_command(a):
    call, net = pi_caller(a, "flavor")
    action = a.action or "list"
    if action == "list":
        return show(call("flavor.list"))
    if not a.arg:
        raise SystemExit("Which? audiolink flavor %s %s" % (action, "FILE" if action in ("add", "try") else "NAME"))
    port = getattr(a, "control_port", None)
    if action == "add":
        return add(a, call, net, port)
    if action == "remove":
        r = call("flavor.remove", {"name": a.arg})
        print("Removed %s" % r["removed"])
        if r.get("unchosen"):
            print("The Pi starts as %s again" % r["default"])
        return
    info = call("flavor.list")
    if not info.get("switchable"):
        raise SystemExit("This Pi's image has no flavors to switch between: audiolink update first")
    if net and not info.get("usb_peer"):
        raise SystemExit("Nothing can confirm a switch: no computer is plugged into the Pi's USB. Plug one in "
                         "(with audiolink host running), or, without a check: audiolink settings flavor=NAME "
                         "--pi %s --reboot" % a.pi)
    if action == "use":
        entry = next((f for f in info["flavors"] if f["name"] == a.arg), None)
        if entry is None:
            raise SystemExit("No flavor %s on the Pi (audiolink flavor lists them)" % a.arg)
        if not net:
            teach_host([entry["usb"]], port)
        warn_accessory()
        r = call("flavor.use", {"name": a.arg})
        if r.get("unchanged"):
            print("The Pi is %s already" % a.arg)
            return
        return follow(call, r, net, port, keep=entry["usb"])
    if action == "try":
        r = call("flavor.try", {"toml": read_toml(a.arg)})
        for n in r.get("notes") or []:
            print("Note: %s" % n)
        if not net:
            teach_host([r.get("usb")], port)
        warn_accessory()
        return follow(call, r, net, port, keep=None)
    raise SystemExit("audiolink flavor: list, use NAME, add FILE, remove NAME or try FILE")


def read_toml(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise SystemExit("Can't read %s (%s)" % (path, e))


def name_of(path):
    """A flavor's name from its file's: my_GameBuds v2.toml -> my_gamebuds-v2."""
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    return re.sub(r"[^a-z0-9_-]+", "-", stem).strip("-_")[:32] or "flavor"


def add(a, call, net, port):
    name = a.name or name_of(a.arg)
    r = call("flavor.add", {"name": name, "toml": read_toml(a.arg)})
    for n in r.get("notes") or []:
        print("Note: %s" % n)
    print("%s %s (%s, %s)" % ("Replaced" if r.get("replaced") else "Added", r["name"], r.get("product") or "no name",
                              r.get("usb") or "no USB ID"))
    remember([r.get("usb")], port, net)
    print("Switch: audiolink flavor use %s" % r["name"])


def warn_accessory():
    print("The Pi becomes another USB device: this computer may ask to allow it (macOS: Allow accessory)")


def teach_host(ids, port):
    """The running host finds the Pi as these too, from now on (until it restarts)."""
    from . import control
    ids = [i for i in ids if i]
    if not ids:
        return
    try:
        control.call("usb_ids.add", {"ids": ids}, port=port or control.DEFAULT_PORT, timeout=10)
    except SystemExit as e:             # a host from before flavors
        detail("the running host didn't take the USB IDs: %s", e)


def remember(ids, port, net):
    """This computer finds the Pi as these from now on (host.json usb_ids)."""
    from .config import remember_usb_ids
    added = remember_usb_ids([i for i in ids if i])
    if added:
        if not net:
            teach_host(added, port)
        print("This computer finds the Pi as %s from now on" % ", ".join(added))


def follow(call, r, net, port, keep):
    """Wait for the switch r (the Pi's reply) to end; say how. keep: the USB ID to
    remember once kept (None: a try, not kept)."""
    name = "the tried flavor" if r["name"] == "try" else r["name"]
    if r.get("wait"):
        print("Waiting %ds first: the Pi's USB was re-presented just before" % r["wait"])
    step("Switching the Pi to %s; it keeps it once %s link comes back (%ds)"
         % (name, "its computer's" if net else "this computer's", r["within"]))
    end = time.monotonic() + r.get("wait", 0) + r["within"] + SLACK
    seen = None
    while time.monotonic() < end:
        time.sleep(POLL)
        try:
            seen = (call("flavor.list") or {}).get("switch") or {}
        except (SystemExit, OSError, ValueError):       # the link is down while it switches
            continue
        if seen.get("id") == r["id"] and seen.get("state") not in ("switching", "waiting"):
            break
    detail("%s", seen)
    state = (seen or {}).get("state") if (seen or {}).get("id") == r["id"] else None
    if state == "kept":
        done("kept")
        if keep:
            remember([keep], port, net)
            print("The Pi starts as %s from now on" % name)
        else:
            print("Until the Pi restarts (to keep one: audiolink flavor add FILE, then use)")
        return
    if state == "back":
        done("went back")
        raise SystemExit("No computer's link came back on %s, so the Pi went back to %s (audiolink logs)"
                         % (name, seen.get("running") or r["back"]))
    if state == "refused":
        done("refused")
        raise SystemExit("The Pi couldn't present %s (%s); it's %s now" % (name, seen.get("error") or "?",
                                                                          seen.get("running") or r["back"]))
    if state == "cancelled":
        done("cancelled")
        raise SystemExit("The reset pins brought the default back")
    done("no answer")
    raise SystemExit("The Pi hasn't said yet; check: audiolink flavor")


def show(info):
    if not info.get("switchable"):
        print("Flavor: %s (this Pi's image has no others: audiolink update first)"
              % (info.get("running") or info.get("default") or "its own"))
        return
    running, default = info.get("running"), info.get("default")
    chosen = info.get("chosen") or default
    print("Flavor: %s%s" % ("the tried flavor" if running == "try" else running,
                            " (starts as %s)" % chosen if chosen != running else ""))
    flavors = info.get("flavors") or []
    width = max([len(f["name"]) for f in flavors] + [4])
    pwidth = max([len(f.get("product") or "") for f in flavors] + [4])
    for f in flavors:
        where = {"card": "yours", "tried": "tried, not kept"}.get(f["where"], f["where"])
        if f.get("replaces"):
            where += ", instead of the built-in one"
        if f["name"] == default:
            where += ", the default"
        print("%s %-*s  %-*s  %-9s  (%s)" % ("*" if f["name"] == running else " ", width, f["name"], pwidth,
                                             f.get("product") or "-", f.get("usb") or "-", where))
    sw = info.get("switch") or {}
    if sw.get("state") in ("switching", "waiting"):
        print("Switching to %s: kept once a computer's USB link comes back" % sw["name"])
    print("Switch: audiolink flavor use NAME")
