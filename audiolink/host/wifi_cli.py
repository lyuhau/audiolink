"""`audiolink wifi ...` and `audiolink host --wifi NAME`: manage the Pi's Wi-Fi from the host.

Joining works out the network's security from the Pi's scan and asks only for what's
missing; it suggests near-misses (5 GHz twins, other-case names) and re-prompts on a
wrong password. Requests go through the running host daemon (control.call).
"""
import os
import time

from .control import call
from .portal import offer_portal, portal_flow
from ..ui import Prompter, line_buffered

SEC_LABEL = {"open": "open", "owe": "open+", "wpa2": "WPA2", "wpa3": "WPA3", "wpa2/wpa3": "WPA2/3",
             "wep": "WEP", "enterprise": "802.1X"}
FIVE_GHZ_SUFFIXES = ("_5g", "-5g", "_5ghz", "-5ghz", " 5g", "_5", "-5", " 5ghz", ".5g")


def wifi_password(ask, ssid, security, retry=False):
    """Returns (password, can_retry): from --password-stdin, $AUDIOLINK_WIFI_PASSWORD
    (first try only), or a prompt that enforces WPA2's 8-63 characters."""
    if ask.stdin_pw is not None:
        return ask.stdin_pw, False
    env = os.environ.get("AUDIOLINK_WIFI_PASSWORD")
    if env and not retry:
        return env, False
    while True:
        pw = ask.secret("%s password for %r: " % (SEC_LABEL.get(security, security), ssid))
        if security in ("wpa2", "wpa2/wpa3") and not 8 <= len(pw) <= 63:
            print("WPA2 passwords are 8-63 characters; try again")
            continue
        if pw:
            return pw, True


def _band(freq):
    return "5GHz" if freq and freq > 4000 else "2.4GHz" if freq else ""


def _suggest(name, nets):
    """Near-misses for a name the Pi can't see: (message, suggested ssid or None)."""
    visible = {n["ssid"] for n in nets}
    low = name.lower()
    for suf in FIVE_GHZ_SUFFIXES:
        if low.endswith(suf):
            base = name[:-len(suf)]
            twin = next((v for v in visible if v.lower() == base.lower()), None)
            if twin:
                return ("%r looks like a 5 GHz network, and the Pi only sees 2.4 GHz. "
                        "Its 2.4 GHz twin %r is visible." % (name, twin), twin)
            return ("%r looks like a 5 GHz network, and the Pi only sees 2.4 GHz." % name, None)
    case = [v for v in visible if v.lower() == low]
    if len(case) == 1:
        return ("no network named %r, but %r is visible." % (name, case[0]), case[0])
    close = [v for v in visible if low in v.lower() or v.lower() in low]
    if close:
        return ("no network named %r. Similar: %s" % (name, ", ".join(repr(c) for c in close[:5])), None)
    return ("no network named %r is visible to the Pi (2.4 GHz only)." % name, None)


def join_flow(name, a, port, known_security=None):
    """Work out what kind of network NAME is, ask only for what's needed, then join."""
    ask = Prompter(a, "--security, --hidden, --password-stdin")
    saved = {n["ssid"]: n for n in call("wifi.list", port=port)}
    security = getattr(a, "security", None) or ("open" if getattr(a, "open", False) else None) \
        or known_security
    hidden = getattr(a, "hidden", False)

    def attempt(**args):
        print("joining %r..." % name, flush=True)
        return call("wifi.join", dict(args, ssid=name), port=port)

    # 1. Saved: just switch to it; if its key is now rejected, ask for the new password.
    if name in saved and not security and not getattr(a, "reenter", False):
        r = attempt(use_saved=True)
        if r["joined"]:
            return _joined(r, a, port)
        if r["code"] != "auth_failed":
            raise SystemExit("couldn't join %r: %s" % (name, r["reason"]))
        print("the saved password for %r was rejected (changed?)" % name)
        security = saved[name]["security"]
        hidden = saved[name]["hidden"]

    # 2. Not saved: look it up in the Pi's scan.
    if not security and not hidden:
        nets = call("wifi.scan", {"max_age": 10}, port=port)
        matches = [n for n in nets if n["ssid"] == name]
        if not matches:
            nets = call("wifi.scan", port=port)           # scans occasionally miss a network
            matches = [n for n in nets if n["ssid"] == name]
        if not matches:
            msg, suggestion = _suggest(name, nets)
            print(msg)
            if suggestion and ask.yes("Use %r instead?" % suggestion, default=True):
                return join_flow(suggestion, a, port)     # start over: it may be saved
            elif ask.yes("Is %r a hidden network (name not broadcast)?" % name):
                hidden = True
                security = ask.choose("Security type:", [("WPA2 (most networks)", "wpa2"),
                                                         ("WPA3 only", "wpa3"), ("open", "open")])
            else:
                raise SystemExit(1)
        if matches:
            kinds = sorted({m["security"] for m in matches})
            if len(kinds) > 1:
                print("there are %d networks named %r:" % (len(kinds), name))
                security = ask.choose("Which one?", [(SEC_LABEL[k], k) for k in kinds])
            else:
                security = kinds[0]

    if hidden and not security:
        # Hidden networks aren't in the scan, so their security can't be detected.
        security = ask.choose("Security type of hidden network %r:" % name,
                              [("WPA2 (most networks)", "wpa2"), ("WPA3 only", "wpa3"), ("open", "open")])

    if security == "wep":
        raise SystemExit("%r uses WEP, which is obsolete and insecure; not supported" % name)
    if security == "enterprise":
        raise SystemExit("%r uses 802.1X/enterprise login (username + password); not supported yet" % name)

    # 3. Join, re-prompting on a wrong password.
    needs_pw = security in ("wpa2", "wpa3", "wpa2/wpa3")
    if security == "wpa3":
        print("note: WPA3 can't use a derived key, so the Pi will save this password itself")
    retry = False
    for _ in range(3):
        pw, can_retry = wifi_password(ask, name, security, retry) if needs_pw else (None, False)
        r = attempt(password=pw, security=security, hidden=hidden)
        if r["joined"]:
            return _joined(r, a, port)
        if r["code"] == "auth_failed" and can_retry:
            print("password incorrect; try again")
            retry = True
            continue
        break
    raise SystemExit("couldn't join %r: %s" % (name, r["reason"]))


def startup_join(session, name, a, port):
    """`audiolink host --wifi NAME`: before the daemon's logs start, wait for the Pi
    and run the join flow. A failed join doesn't stop the daemon: the Pi stays on
    (or falls back to) its saved networks."""
    line_buffered()
    if not session.peer_alive:
        print("waiting for the Pi to connect (plug it in)...", flush=True)
    while not (session.peer_alive and session.peer_info):
        time.sleep(0.25)
    if not session.peer_has("wifi"):
        print("this Pi's audiolink doesn't support Wi-Fi management (update it); ignoring --wifi")
        return
    try:
        st = call("wifi.status", port=port)
        if st["ssid"] == name:
            print("the Pi is already on %r (IP %s)" % (name, st["ip"]))
            if st.get("internet") == "portal":
                offer_portal(a, port)
            return
        join_flow(name, a, port)
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code)
        print("continuing; the Pi stays on its saved networks. Retry with: audiolink wifi join %r" % name)


def _joined(r, a, port):
    print("joined %r (%s), Pi IP %s; saved" % (r["ssid"], SEC_LABEL.get(r.get("security"), "?"), r["ip"]))
    if r.get("internet") == "portal":
        offer_portal(a, port)
    elif r.get("internet") == "offline":
        print("note: the Pi has no internet on this network (%s)" % r.get("detail", "no response"))


def pick_flow(a, port):
    """`audiolink wifi` with no action: numbered list from a fresh scan."""
    print("scanning from the Pi (it only sees 2.4 GHz)...", flush=True)
    nets = call("wifi.scan", port=port)
    st = call("wifi.status", port=port)
    opts = []
    for n in nets:
        tags = [SEC_LABEL[n["security"]]]
        if n["saved"]:
            tags.append("saved")
        if n["ssid"] == st["ssid"]:
            tags.append("connected")
        opts.append(("%-32s %4d dBm  %s" % (n["ssid"][:32], n["signal"], ", ".join(tags)), n))
    opts.append(("(hidden network: type its name)", None))
    choice = Prompter(a, "--security, --hidden, --password-stdin").choose("Join which network?", opts)
    if choice is None:
        name = Prompter._input("Network name: ").strip()
        a.hidden = True
        return join_flow(name, a, port)
    if choice["ssid"] == st["ssid"]:
        print("already connected to %r" % choice["ssid"])
        return
    return join_flow(choice["ssid"], a, port, known_security=choice["security"])


def wifi_command(a):
    line_buffered()
    port = a.control_port
    peer = call("peer", port=port)
    if not peer["connected"]:
        raise SystemExit("the host is running but the Pi isn't connected yet")
    if "wifi" not in (peer["peer"] or {}).get("caps", []):
        raise SystemExit("this Pi's audiolink doesn't support Wi-Fi management (update it)")

    if a.action is None:
        return pick_flow(a, port)
    if a.action == "scan":
        print("scanning from the Pi (it only sees 2.4 GHz)...", flush=True)
        nets = call("wifi.scan", port=port)
        print("%-32s %8s %-7s %-7s %s" % ("NETWORK", "SIGNAL", "BAND", "SEC", ""))
        for n in nets:
            print("%-32s %5d dBm %-7s %-7s %s" % (n["ssid"][:32], n["signal"], _band(n["freq"]),
                                                  SEC_LABEL[n["security"]], "saved" if n["saved"] else ""))
    elif a.action == "status":
        st = call("wifi.status", port=port)
        if st["ssid"]:
            print("connected to %r (%s), IP %s, internet: %s" % (st["ssid"], _band(st["freq"]),
                  st["ip"] or "none yet", st.get("internet", "?")))
            if st.get("internet") == "portal":
                print("this network has a login page (captive portal); run: audiolink wifi portal")
        else:
            print("not connected (state: %s)" % st["state"])
        print("saved networks: %s" % (", ".join(st["saved"]) or "none"))
    elif a.action == "list":
        nets = call("wifi.list", port=port)
        if not nets:
            print("no saved networks")
        for n in nets:
            print("%-32s %-7s%s" % (n["ssid"], SEC_LABEL.get(n["security"], n["security"]),
                                    " hidden" if n["hidden"] else ""))
    elif a.action == "join":
        if not a.name:
            raise SystemExit("usage: audiolink wifi join NAME")
        join_flow(a.name, a, port)
    elif a.action == "portal":
        portal_flow(port)
    elif a.action == "check":
        r = call("wifi.check", port=port)
        print("internet: %s%s" % (r["internet"], " on %r" % r["ssid"] if r.get("ssid") else ""))
        raise SystemExit({"online": 0, "portal": 2}.get(r["internet"], 3))
    elif a.action == "forget":
        if not a.name:
            raise SystemExit("usage: audiolink wifi forget NAME")
        call("wifi.forget", {"ssid": a.name}, port=port)
        print("forgot %r" % a.name)
