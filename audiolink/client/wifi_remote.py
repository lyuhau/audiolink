"""A Pi's Wi-Fi over the network, client side (services/wifi_remote.py).

`audiolink wifi join NAME` uses this when the Pi isn't plugged in here (or with --pi):
the request goes over the network (the Pi's setup network or a shared LAN). The Pi
checks the network first when it can (a wrong password is refused without moving),
then switches. If nobody can reach it there, it opens its setup network.
`audiolink wifi status|check --pi PI` asks over any route, the relay included.
"""
from ..ui import detail, done, step
from ..wifi_ui import FAILURE
from . import keyring
from .pairing import reach

REMOTE_ACTIONS = ("join", "status", "check")
PORTAL_THERE = "sign in on the computer it's plugged into: audiolink wifi portal"


def _pick_pi(a):
    if a.pi:
        return keyring.lookup(a.pi, known=True)
    pis = list(keyring.load().values())
    if len(pis) == 1:
        return pis[0]
    raise SystemExit("Which Pi? Use --pi NAME (see: audiolink pis)" if pis else
                     "No Pis yet (add one: audiolink add)")


def over_network(a):
    """Does this `audiolink wifi` command ask for the network (--pi, --via-network, --at,
    --tries)? Ends it if the action can't go over the network."""
    asked = bool(a.pi or a.via_network or a.at or a.tries is not None)
    if asked and a.action not in REMOTE_ACTIONS:
        raise SystemExit("--pi, --via-network, --at and --tries work only with join and status/check")
    return asked


def status_command(a):
    """`audiolink wifi status|check --pi PI`."""
    from ..host.wifi_cli import show_check, show_status
    from .remote import Remote
    if a.name:
        raise SystemExit("audiolink wifi %s takes no network name" % a.action)
    remote = Remote(_pick_pi(a), a.at or [], None).open()
    try:
        r = remote.call("wifi_status", {"check": a.action == "check"})["result"]
    finally:
        remote.close()
    if a.action == "check":
        raise SystemExit(show_check(remote.name, r, PORTAL_THERE))
    show_status(remote.name, r, PORTAL_THERE)


def change_command(a):
    """`audiolink wifi join NAME` over the network."""
    from ..wifi_ui import wifi_password
    from ..ui import Prompter
    if not a.name:
        raise SystemExit("Usage: audiolink wifi join NAME")
    entry = _pick_pi(a)
    pi, net = entry["alias"], a.name
    pub, secret = keyring.parse(entry["code"])
    setup_net = "audiopi-%s" % (entry.get("name") or pi)
    step("Finding %s" % pi)
    conn, hello = reach(entry, a.at or [])
    if not conn:
        done("not found")
        raise SystemExit("%s isn't on this network or on its setup network (%s)" % (pi, setup_net))
    done("found")
    detail("at %s", conn.ip)
    keyring.seen(pub.hex())
    try:
        st = conn.ask_auth("change_status", hello, secret, ssid=net)
        if "error" in st:
            raise SystemExit(st["error"])
        saved = st.get("saved", [])
        details = {"ssid": net, "tries": a.tries or 1, "hidden": a.hidden}
        typed = False
        if net in saved and not a.reenter:
            details["use_saved"] = True
            detail("%s already has the password for %s", pi, net)
        else:
            # Not given: as the Pi's scan saw it (None: not seen lately, or an older Pi;
            # then it's most likely a network with a password).
            security = a.security or ("open" if a.open else None) or st.get("security")
            if security == "wep":
                raise SystemExit("%s uses WEP (obsolete, not supported)" % net)
            if security == "enterprise":
                raise SystemExit("%s needs a username (enterprise Wi-Fi, not supported yet)" % net)
            if security or "security" not in st:
                details["security"] = security or "wpa2"
            if security not in ("open", "owe"):
                ask = Prompter(a, "--security, --open, --password-stdin")
                details["password"], _ = wifi_password(ask, net, security or "wpa2")
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
        _portal_note(r.get("internet"))
        return
    if pre.get("ok") or pre.get("code") == "no_ip":
        done("password ok, " + _internet(pre.get("internet")) if pre.get("ok") else "password ok")
    elif pre:
        done("couldn't check ahead")
        detail("%s", pre.get("reason"))
    else:
        done("skipped")
    print("Switching %s to %s%s" % (pi, net, saved_note))
    print("If it can't join, it goes back to its known networks; if nobody can reach it there, "
          "it opens its setup network (%s)" % setup_net)
    _portal_note(pre.get("internet"))


def _portal_note(internet):
    if internet == "portal":
        print("The network has a sign-in page: %s" % PORTAL_THERE)


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
