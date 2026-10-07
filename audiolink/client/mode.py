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
from ..ui import detail, done, duration, step
from . import keyring
from .pairing import reach


def _pick_pi(a):
    if a.pi:
        try:
            return keyring.resolve(a.pi)
        except KeyError:
            raise SystemExit("No Pi named %s (see: audiolink pis)" % a.pi)
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
        details = {"ssid": net, "tries": a.tries, "confirm_within": a.confirm_within, "hidden": a.hidden}
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
        token = None
        details["confirm"] = a.confirm_by
        if a.confirm_by == "me":
            token = os.urandom(12).hex()
            details["token_hash"] = hashlib.sha256(token.encode()).hexdigest()
        step("Checking %s" % net)
        conn.sock.settimeout(90)        # the Pi tries the network before answering
        r = conn.ask_auth("change_wifi", hello, secret, sealed=details)
    finally:
        conn.close()
    if "error" in r:
        _refuse(pi, net, r)
    detail("reply: %s", {k: v for k, v in r.items() if k != "accepted"})
    pre = r.get("precheck") or {}
    saved_note = " (password saved)" if typed else ""
    if r.get("joined"):                 # joined next to its setup network
        done("password ok, " + _internet(r.get("internet")))
        print("Joined %s; setup network staying up until confirmed%s" % (net, saved_note))
    else:
        if pre.get("ok") or pre.get("code") == "no_ip":
            done("password ok, " + _internet(pre.get("internet")) if pre.get("ok") else "password ok")
        else:
            done("couldn't check ahead")
            detail("%s", pre.get("reason"))
        print("Switching %s to %s%s" % (pi, net, saved_note))

    back = r["fallback_ssid"] if r.get("fallback_ssid") else "its setup network"
    within = duration(r["confirm_within"])
    if a.confirm_by == "none":
        return
    if a.confirm_by == "any":
        print("Reverting to %s in %s unless confirmed from %s:" % (back, within, net))
        print("  audiolink wifi confirm %s" % pi)
        return
    print("Reverting to %s in %s unless this computer finds it on %s." % (back, within, net))
    print("Connect this computer to %s." % net)
    detail("or confirm from another computer: audiolink wifi confirm %s --token %s", pi, token)
    # Not a.at: those addresses reach the Pi's *old* mode; confirming must go through the new one.
    _await_and_confirm(entry, secret, token, [], time.monotonic() + r["switch_in"] + r["confirm_within"], net, back)


def _internet(state):
    return {"online": "online", "portal": "sign-in page", "offline": "no internet"}.get(state, "internet unknown")


def _refuse(pi, net, r):
    """A refused change: finish the progress line with why, then where the Pi is."""
    detail("%s said: %s", pi, r["error"])
    done({"auth_failed": "wrong password", "no_ip": "no address", "timeout": "couldn't connect",
          "not_found": "not in range (2.4 GHz only)"}.get(r.get("code"), "refused"))
    if r.get("stayed_on"):
        raise SystemExit("%s staying on %s" % (pi, r["stayed_on"]))
    if "stayed_on" in r:
        raise SystemExit("%s staying on its setup network" % pi)
    raise SystemExit(r["error"])


def _await_and_confirm(entry, secret, token, at, deadline, net, back):
    pi = entry["alias"]
    step("Waiting for %s on %s" % (pi, net))
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
                            done("failed")
                            raise SystemExit("Couldn't confirm: %s" % r["error"])
                        done("found")
                        detail("confirmed through %s", conn.ip)
                        print("%s staying on %s" % (pi, net))
                        return
                if not st.get("pending") and st.get("last"):
                    last = st["last"]
                    detail("last change: %s", last["result"])
                    if last["ok"]:
                        done("found")
                        print("%s staying on %s" % (pi, net))
                        return
                    done("reverted")
                    raise SystemExit("%s back on %s" % (pi, back))
            finally:
                conn.close()
        time.sleep(5)
    done("not found")
    raise SystemExit("%s reverting to %s (check later: audiolink wifi confirm %s)" % (pi, back, pi))


def confirm_command(a):
    """`audiolink wifi confirm PI [--token T]`: keep a pending change (or show the last result)."""
    entry = _pick_pi(a)
    pi = entry["alias"]
    _, secret = parse_code(entry["code"])
    conn, hello = reach(entry, a.at or [], with_dht=True)
    if not conn:
        raise SystemExit("%s not found from here" % pi)
    detail("at %s", conn.ip)
    try:
        st = conn.ask_auth("change_status", hello, secret)
        detail("status: %s", st)
        pending, last = st.get("pending"), st.get("last")
        if pending and pending["phase"] == "awaiting confirmation":
            r = conn.ask_auth("confirm", hello, secret, sealed={"token": a.token or ""})
            if r.get("retry"):
                raise SystemExit("Reached %s through its setup network; connect to %s and retry"
                                 % (pi, pending["target"]))
            if "error" in r:
                raise SystemExit("Couldn't confirm: %s" % r["error"])
            print("%s staying on %s" % (pi, r["kept"]))
        elif pending:
            print("%s still switching to %s; retry in a moment" % (pi, pending["target"]))
        elif last:
            print("Nothing to confirm (last change: %s)" % ("kept" if last["ok"] else
                  "reverted to %s" % last.get("reverted_to", "previous network")))
        else:
            print("Nothing to confirm")
    finally:
        conn.close()
