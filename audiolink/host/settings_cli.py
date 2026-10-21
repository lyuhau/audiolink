"""`audiolink settings [KEY=VALUE ...] [--unset KEY] [--reboot]`: the Pi's card settings.

The same settings as settings.txt on its card (README: The card's settings), through
the running Pi (services/card.py): checked like the Pi checks them at boot, which is
when they take effect (--reboot: now). Owners only.
"""
from .pi_call import pi_caller


def settings_command(a):
    call, _ = pi_caller(a, "card")
    changes = {}
    for item in a.items or []:
        key, eq, value = item.partition("=")
        if not eq or not key.strip():
            raise SystemExit("%s: KEY=VALUE, e.g. hostname=kitchen (audiolink settings lists them)" % item)
        changes[key.strip()] = value.strip()
    for key in a.unset or []:
        changes[key] = None
    if not changes:
        if a.reboot:
            raise SystemExit("Nothing to change (--reboot goes with a change)")
        return show(call("card.settings")["settings"])
    try:
        r = call("card.set", {"changes": changes, "reboot": a.reboot})
    except SystemExit as e:
        raise SystemExit(str(e).replace("card.set: ", ""))
    for key, value in r["changes"].items():
        print("%s = %s" % (key, value) if value is not None else "%s: back to its built-in value" % key)
    print("Rebooting the Pi" if a.reboot else "Takes effect when the Pi next starts (--reboot: now)")


def show(settings):
    width = max(len(s["key"]) for s in settings)
    for s in settings:
        if s["value"] is not None:
            value, note = s["value"], "set"
        else:
            value, note = s["default"] or "-", "built in" if s["default"] else "the device's"
        print("%-*s  %-24s  %s" % (width, s["key"], value, note))
