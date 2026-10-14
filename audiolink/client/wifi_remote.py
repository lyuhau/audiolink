"""Changing a Pi's Wi-Fi over the network, client side (services/wifi_remote.py).

`audiolink wifi join NAME` uses this when the Pi isn't plugged in here: the request
goes over the network (the Pi's setup network or a shared LAN). The Pi checks the
network first when it can (a wrong password is refused without moving), then
switches. If nobody can reach it there, it opens its setup network.
"""
from ..discovery.record import parse_code
from ..ui import detail, done, step
from ..wifi_ui import FAILURE
from . import keyring
from .pairing import reach


def _pick_pi(a):
    if a.pi:
        return keyring.lookup(a.pi, known=True)
    pis = list(keyring.load().values())
    if len(pis) == 1:
        return pis[0]
    raise SystemExit("Which Pi? Use --pi NAME (see: audiolink pis)" if pis else
                     "No Pis yet (add one: audiolink add)")


def change_command(a):
    """`audiolink wifi join NAME` over the network."""
    from ..wifi_ui import wifi_password
    from ..ui import Prompter
    entry = _pick_pi(a)
    pi, net = entry["alias"], a.name
    _, secret = parse_code(entry["code"])
    step("Finding %s" % pi)
    conn, hello = reach(entry, a.at or [])
    if not conn:
        done("not found")
        raise SystemExit("%s isn't on this network or on its setup network (audiopi-%s)" % (pi, pi))
    done("found")
    detail("at %s", conn.ip)
    try:
        saved = conn.ask_auth("change_status", hello, secret).get("saved", [])
        details = {"ssid": net, "tries": a.tries, "hidden": a.hidden}
        typed = False
        if net in saved and not a.reenter:
            details["use_saved"] = True
            detail("%s already has the password for %s", pi, net)
        else:
            security = a.security or ("open" if a.open else "wpa2")
            details["security"] = security
            if security not in ("open", "owe"):
                ask = Prompter(a, "--security, --open, --password-stdin")
                details["password"], _ = wifi_password(ask, net, security)
                typed = True
        step("Checking %s" % net)
        conn.sock.settimeout(90)        # the Pi tries the network before answering
        r = conn.ask_auth("change_wifi", hello, secret, sealed=details)
    finally:
        conn.close()
    if "error" in r:
        _refuse(pi, net, r)
    detail("reply: %s", {k: v for k, v in r.items() if k != "accepted"})
    if r.get("already"):
        done("already on it")
        return
    pre = r.get("precheck") or {}
    proven = r.get("joined") or pre.get("ok") or pre.get("code") == "no_ip"
    saved_note = " (password saved)" if typed and proven else ""
    if r.get("joined"):                 # joined next to its setup network
        done("password ok, " + _internet(r.get("internet")))
        print("%s on %s%s" % (pi, net, saved_note))
        return
    if pre.get("ok") or pre.get("code") == "no_ip":
        done("password ok, " + _internet(pre.get("internet")) if pre.get("ok") else "password ok")
    elif pre:
        done("couldn't check ahead")
        detail("%s", pre.get("reason"))
    else:
        done("skipped")
    print("Switching %s to %s%s" % (pi, net, saved_note))


def _internet(state):
    return {"online": "online", "portal": "sign-in page", "offline": "no internet"}.get(state, "internet unknown")


def _refuse(pi, net, r):
    """A refused change: finish the progress line with why, then where the Pi is."""
    detail("%s said: %s", pi, r["error"])
    done(FAILURE.get(r.get("code"), "refused"))
    if r.get("stayed_on"):
        raise SystemExit("%s staying on %s" % (pi, r["stayed_on"]))
    if "stayed_on" in r:
        raise SystemExit("%s staying on its setup network" % pi)
    raise SystemExit(r["error"])
