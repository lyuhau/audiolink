"""Changing a Pi's connection mode over the network, client side (services/mode.py).

`audiolink wifi join NAME` uses this when the Pi isn't plugged in here: the request
goes over the network (the Pi's setup network or a shared LAN), and the change only
sticks once someone reaches the Pi through the new network and confirms.

--confirm-by me   (default) this machine confirms: it holds a one-time token, waits
                  for you to switch it to the same network, finds the Pi (mDNS, DHT,
                  last known address) and confirms
--confirm-by any  any paired machine may confirm (`audiolink wifi confirm PI`)
--confirm-by none keep the change as soon as the Pi is on the new network (the
                  password is checked first either way, and saved once it works)
"""
import hashlib
import os
import time

from ..discovery.record import parse_code
from ..ui import detail, duration
from . import keyring
from .pairing import reach


def _pick_pi(a):
    if a.pi:
        try:
            return keyring.resolve(a.pi)
        except KeyError:
            raise SystemExit("no Pi called %r here; see: audiolink pis" % a.pi)
    pis = list(keyring.load().values())
    if len(pis) == 1:
        return pis[0]
    raise SystemExit("which Pi? add --pi NAME (see: audiolink pis)" if pis else
                     "no Pis known yet; pair with one first: audiolink add")


def change_command(a):
    """`audiolink wifi join NAME` over the network."""
    from ..wifi_ui import wifi_password
    from ..ui import Prompter
    entry = _pick_pi(a)
    pi, net = entry["alias"], a.name
    _, secret = parse_code(entry["code"])
    print("Looking for %s..." % pi, flush=True)
    conn, hello = reach(entry, a.at or [])
    if not conn:
        raise SystemExit("Can't find %s from here. This computer needs to be on the same network as %s,\n"
                         "or on its setup network (audiopi-%s)." % (pi, pi, pi))
    detail("reached %s at %s", pi, conn.ip)
    try:
        saved = conn.ask_auth("change_status", hello, secret).get("saved", [])
        details = {"ssid": net, "tries": a.tries, "confirm_within": a.confirm_within, "hidden": a.hidden}
        typed = False
        if net in saved and not a.reenter:
            details["use_saved"] = True
            detail("%s already knows the password for %r", pi, net)
        else:
            security = a.security or ("open" if a.open else "wpa2")
            details["security"] = security
            if security not in ("open", "owe"):
                ask = Prompter(a, "--security, --open, --password-stdin")
                details["password"], _ = wifi_password(ask, net, security)
                typed = True
        token = None
        details["confirm"] = a.confirm_by
        if a.confirm_by == "me":
            token = os.urandom(12).hex()
            details["token_hash"] = hashlib.sha256(token.encode()).hexdigest()
        print("Checking %s from %s (this can take half a minute)..." % (net, pi), flush=True)
        conn.sock.settimeout(90)        # the Pi tries the network before answering
        r = conn.ask_auth("change_wifi", hello, secret, sealed=details)
    finally:
        conn.close()
    if "error" in r:
        raise SystemExit(_refusal(pi, net, r))
    detail("reply: %s", {k: v for k, v in r.items() if k not in ("accepted",)})
    pre = r.get("precheck") or {}
    if r.get("joined"):                 # joined next to its setup network
        print("%s joined %s: %s." % (pi, net, _works(r.get("internet"))))
    elif pre.get("ok"):
        print("Checked %s: %s." % (net, _works(pre.get("internet"))))
        print("%s is moving to %s." % (pi, net))
    else:
        if pre:
            detail("couldn't check ahead: %s", pre.get("reason"))
        print("%s is moving to %s." % (pi, net))
    if typed and (r.get("joined") or pre.get("ok")):
        print("%s saved the password, so you won't need to type it again." % pi)

    back = "its previous network, %s" % r["fallback_ssid"] if r.get("fallback_ssid") else "its setup network"
    within = duration(r["confirm_within"])
    if a.confirm_by == "none":
        print("It stays on %s." % net)
        return
    if a.confirm_by == "any":
        print("\nTo keep it there, run this on any computer connected to %s within %s:" % (net, within))
        print("    audiolink wifi confirm %s" % pi)
        print("Otherwise %s goes back to %s." % (pi, back))
        return
    print("\nNow connect this computer to %s. Once I can see %s there, it stays." % (net, pi))
    print("If I can't within %s, %s goes back to %s." % (within, pi, back))
    detail("or confirm from another computer: audiolink wifi confirm %s --token %s", pi, token)
    # Not a.at: those addresses reach the Pi's *old* mode; confirming must go through the new one.
    _await_and_confirm(entry, secret, token, [], time.monotonic() + r["switch_in"] + r["confirm_within"], net, back)


def _works(internet):
    return {"online": "the password works and it's online",
            "portal": "the password works, but it needs a sign-in page before it gets internet",
            "offline": "the password works, but it has no internet there"}.get(
                internet, "the password works")


def _refusal(pi, net, r):
    """A refused change, in words; the Pi's own reason goes to -v."""
    detail("%s said: %s", pi, r["error"])
    where = ("%s is still on %s. Nothing changed." % (pi, r["stayed_on"]) if r.get("stayed_on")
             else "%s is still on its setup network. Nothing changed." % pi if "stayed_on" in r else "")
    why = {"auth_failed": "The password for %s didn't work." % net,
           "no_ip": "%s connected to %s, but the network didn't give it an address." % (pi, net),
           "not_found": "%s can't see %s. (It only sees 2.4 GHz networks; is it in range?)" % (pi, net),
           "timeout": "%s couldn't connect to %s." % (pi, net)}.get(r.get("code"))
    if not why:
        why = "%s didn't switch to %s: %s." % (pi, net, r["error"])
    return why + ("\n" + where if where else "")


def _await_and_confirm(entry, secret, token, at, deadline, net, back):
    pi = entry["alias"]
    time.sleep(5)
    while time.monotonic() < deadline:
        conn, hello = reach(entry, at, with_dht=True)
        if conn:
            try:
                st = conn.ask_auth("change_status", hello, secret)
                if st.get("pending") and st["pending"]["phase"] == "awaiting confirmation":
                    r = conn.ask_auth("confirm", hello, secret, sealed={"token": token or ""})
                    # retry: reached it over its setup network, not the new one; keep looking
                    if not r.get("retry"):
                        if "error" in r:
                            raise SystemExit("Couldn't confirm: %s" % r["error"])
                        detail("confirmed through %s", conn.ip)
                        print("Found %s on %s. It's staying there." % (pi, net))
                        return
                if not st.get("pending") and st.get("last"):
                    last = st["last"]
                    detail("last change: %s", last["result"])
                    if last["ok"]:
                        print("%s is on %s." % (pi, net))
                        return
                    raise SystemExit("%s went back to %s." % (pi, back))
            finally:
                conn.close()
        time.sleep(5)
    raise SystemExit("Couldn't find %s on %s in time, so it went back to %s.\n"
                     "(To check later: audiolink wifi confirm %s)" % (pi, net, back, pi))


def confirm_command(a):
    """`audiolink wifi confirm PI [--token T]`: keep a pending change (or show the last result)."""
    entry = _pick_pi(a)
    pi = entry["alias"]
    _, secret = parse_code(entry["code"])
    conn, hello = reach(entry, a.at or [], with_dht=True)
    if not conn:
        raise SystemExit("Can't find %s from here." % pi)
    detail("reached %s at %s", pi, conn.ip)
    try:
        st = conn.ask_auth("change_status", hello, secret)
        detail("status: %s", st)
        pending, last = st.get("pending"), st.get("last")
        if pending and pending["phase"] == "awaiting confirmation":
            r = conn.ask_auth("confirm", hello, secret, sealed={"token": a.token or ""})
            if r.get("retry"):
                raise SystemExit("You're reaching %s through its setup network. Connect this computer to %s "
                                 "and run this again." % (pi, pending["target"]))
            if "error" in r:
                raise SystemExit("Couldn't confirm: %s" % r["error"])
            print("Done: %s stays on %s." % (pi, r["kept"]))
        elif pending:
            print("%s is still switching to %s; try again in a moment." % (pi, pending["target"]))
        elif last:
            print("Nothing to confirm. Last change: %s" % ("it worked." if last["ok"] else
                  "it didn't stick, and %s went back to %s." % (pi, last.get("reverted_to", "its previous network"))))
        else:
            print("Nothing to confirm.")
    finally:
        conn.close()
