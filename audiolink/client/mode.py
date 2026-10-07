"""Changing a Pi's connection mode over the network, client side (services/mode.py).

`audiolink wifi join NAME` uses this when the Pi isn't plugged in here: the request
goes over the network (the Pi's setup network or a shared LAN), and the change only
sticks once someone reaches the Pi through the new network and confirms.

--confirm-by me   (default) this machine confirms: it holds a one-time token, waits
                  for you to switch it to the same network, finds the Pi (mDNS, DHT,
                  last known address) and confirms
--confirm-by any  any paired machine may confirm (`audiolink wifi confirm PI`)
"""
import hashlib
import os
import time

from ..discovery.record import parse_code
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
    _, secret = parse_code(entry["code"])
    print("reaching %s over the network..." % entry["alias"], flush=True)
    conn, hello = reach(entry, a.at or [])
    if not conn:
        raise SystemExit("couldn't reach %s over the network (not on this network or its setup network)"
                         % entry["alias"])
    try:
        saved = conn.ask_auth("change_status", hello, secret).get("saved", [])
        details = {"ssid": a.name, "tries": a.tries, "confirm_within": a.confirm_within,
                   "hidden": a.hidden}
        if a.name in saved and not a.reenter:
            details["use_saved"] = True
        else:
            security = a.security or ("open" if a.open else "wpa2")
            details["security"] = security
            if security not in ("open", "owe"):
                ask = Prompter(a, "--security, --open, --password-stdin")
                details["password"], _ = wifi_password(ask, a.name, security)
        token = None
        if a.confirm_by == "me":
            token = os.urandom(12).hex()
            details["token_hash"] = hashlib.sha256(token.encode()).hexdigest()
        print("asking %s to try %r (this can take up to half a minute)..." % (entry["alias"], a.name), flush=True)
        conn.sock.settimeout(90)        # the Pi tries the network before answering
        r = conn.ask_auth("change_wifi", hello, secret, sealed=details)
    finally:
        conn.close()
    if "error" in r:
        raise SystemExit("%s refused: %s" % (entry["alias"], r["error"]))
    pre = r.get("precheck") or {}
    if r.get("joined"):
        print("%s joined %r (%s, internet %s) and keeps its setup network up for now." % (
            entry["alias"], a.name, r.get("ip"), r.get("internet") or "unknown"))
        print("If nobody reaches it on %r within %d s, it leaves %r again." % (a.name, r["confirm_within"], a.name))
    else:
        if pre.get("ok"):
            print("tried %r without leaving: %s." % (a.name, pre["reason"]))
        elif pre:
            print("couldn't try %r ahead of time (%s); switching anyway." % (a.name, pre["reason"]))
        print("%s accepted: switching to %r in %ds. If it can't be reached there within %d s, it goes back"
              " to the %s." % (entry["alias"], a.name, r["switch_in"], r["confirm_within"], r["fallback"]))
    if a.confirm_by == "any":
        print("any paired machine on %r can confirm it: audiolink wifi confirm %s" % (a.name, entry["alias"]))
        return
    print("now switch this machine to %r; I'll find %s there and confirm." % (a.name, entry["alias"]))
    print("(or confirm from another machine: audiolink wifi confirm %s --token %s)" % (entry["alias"], token))
    # Not a.at: those addresses reach the Pi's *old* mode; confirming must go through the new one.
    _await_and_confirm(entry, secret, token, [], time.monotonic() + r["switch_in"] + r["confirm_within"])


def _await_and_confirm(entry, secret, token, at, deadline):
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
                            raise SystemExit("couldn't confirm: %s" % r["error"])
                        print("reached %s on %r and confirmed: it keeps this network." % (entry["alias"], r["kept"]))
                        return
                if not st.get("pending") and st.get("last"):
                    last = st["last"]
                    if last["ok"]:
                        print(last["result"])
                        return
                    raise SystemExit("the change didn't stick: %s (it's back on its %s)"
                                     % (last["result"], last.get("reverted_to", "previous network")))
            finally:
                conn.close()
        time.sleep(5)
    raise SystemExit("couldn't reach %s in time; it will have gone back to its previous network.\n"
                     "once you can reach it again: audiolink wifi confirm %s  (shows what happened)"
                     % (entry["alias"], entry["alias"]))


def confirm_command(a):
    """`audiolink wifi confirm PI [--token T]`: keep a pending change (or show the last result)."""
    entry = _pick_pi(a)
    _, secret = parse_code(entry["code"])
    conn, hello = reach(entry, a.at or [], with_dht=True)
    if not conn:
        raise SystemExit("couldn't reach %s over the network" % entry["alias"])
    try:
        st = conn.ask_auth("change_status", hello, secret)
        if st.get("pending") and st["pending"]["phase"] == "awaiting confirmation":
            r = conn.ask_auth("confirm", hello, secret, sealed={"token": a.token or ""})
            if "error" in r:
                raise SystemExit(r["error"])
            print("confirmed: %s keeps %r" % (entry["alias"], r["kept"]))
        elif st.get("pending"):
            print("%s is still %s %r" % (entry["alias"], st["pending"]["phase"], st["pending"]["target"]))
        elif st.get("last"):
            print("nothing pending; last change: %s" % st["last"]["result"])
        else:
            print("nothing pending")
    finally:
        conn.close()
