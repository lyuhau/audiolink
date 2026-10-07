"""`audiolink wifi ...` and `audiolink host --wifi NAME`: manage the Pi's Wi-Fi from the host.

Joining works out the network's security from the Pi's scan and asks only for what's
missing; it suggests near-misses (5 GHz twins, other-case names) and re-prompts on a
wrong password. Requests go through the running host daemon (control.call).
"""
import time

from .control import call
from .portal import offer_portal, portal_flow
from ..ui import Prompter, detail, line_buffered
from ..wifi_ui import SEC_LABEL, bars, wifi_password

FIVE_GHZ_SUFFIXES = ("_5g", "-5g", "_5ghz", "-5ghz", " 5g", "_5", "-5", " 5ghz", ".5g")


PI = "the Pi"                           # the plugged-in Pi's name, once known (_set_pi)


def _set_pi(peer):
    global PI
    PI = ((peer or {}).get("identity") or {}).get("name") or "the Pi"


def _suggest(name, nets):
    """Near-misses for a name the Pi can't see: (message, suggested ssid or None)."""
    visible = {n["ssid"] for n in nets}
    low = name.lower()
    for suf in FIVE_GHZ_SUFFIXES:
        if low.endswith(suf):
            base = name[:-len(suf)]
            twin = next((v for v in visible if v.lower() == base.lower()), None)
            if twin:
                return ("%s looks like a 5 GHz network, and %s can only use 2.4 GHz.\n"
                        "%s looks like the same network on 2.4 GHz." % (name, PI, twin), twin)
            return ("%s looks like a 5 GHz network, and %s can only use 2.4 GHz. If your router\n"
                    "has a 2.4 GHz network too, use that one." % (name, PI), None)
    case = [v for v in visible if v.lower() == low]
    if len(case) == 1:
        return ("%s can't see %s, but it can see %s." % (PI, name, case[0]), case[0])
    close = [v for v in visible if low in v.lower() or v.lower() in low]
    if close:
        return ("%s can't see %s. Did you mean: %s?" % (PI, name, ", ".join(close[:5])), None)
    return ("%s can't see a network called %s. (It can only use 2.4 GHz networks.)" % (PI, name), None)


def join_flow(name, a, port, known_security=None):
    """Work out what kind of network NAME is, ask only for what's needed, then join."""
    ask = Prompter(a, "--security, --hidden, --password-stdin")
    saved = {n["ssid"]: n for n in call("wifi.list", port=port)}
    security = getattr(a, "security", None) or ("open" if getattr(a, "open", False) else None) \
        or known_security
    hidden = getattr(a, "hidden", False)

    def attempt(**args):
        print("Connecting %s to %s..." % (PI, name), flush=True)
        return call("wifi.join", dict(args, ssid=name), port=port)

    # 1. Saved: just switch to it; if its key is now rejected, ask for the new password.
    if name in saved and not security and not getattr(a, "reenter", False):
        r = attempt(use_saved=True)
        if r["joined"]:
            return _joined(r, a, port)
        if r["code"] != "auth_failed":
            raise SystemExit(_failed(name, r))
        print("The saved password for %s didn't work. Has it changed?" % name)
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
            if suggestion and ask.yes("Use %s instead?" % suggestion, default=True):
                return join_flow(suggestion, a, port)     # start over: it may be saved
            elif ask.yes("Is %s a hidden network (one that doesn't show its name)?" % name):
                hidden = True
                security = ask.choose("What kind of security does it use?", [
                    ("a password (most networks)", "wpa2"), ("a password, WPA3 only", "wpa3"),
                    ("none (open network)", "open")])
            else:
                raise SystemExit(1)
        if matches:
            kinds = sorted({m["security"] for m in matches})
            if len(kinds) > 1:
                print("There are %d networks called %s:" % (len(kinds), name))
                security = ask.choose("Which one?", [(SEC_LABEL[k], k) for k in kinds])
            else:
                security = kinds[0]

    if hidden and not security:
        # Hidden networks aren't in the scan, so their security can't be detected.
        security = ask.choose("What kind of security does %s use?" % name, [
            ("a password (most networks)", "wpa2"), ("a password, WPA3 only", "wpa3"),
            ("none (open network)", "open")])

    if security == "wep":
        raise SystemExit("%s uses WEP, an old and insecure kind of Wi-Fi security that %s doesn't support."
                         % (name, PI))
    if security == "enterprise":
        raise SystemExit("%s needs a username as well as a password (enterprise Wi-Fi), which isn't "
                         "supported yet." % name)

    # 3. Join, re-prompting on a wrong password.
    needs_pw = security in ("wpa2", "wpa3", "wpa2/wpa3")
    if security == "wpa3":
        print("Note: this is a WPA3 network, so %s has to keep the password itself, not just a\n"
              "scrambled form of it." % PI)
    retry = False
    for _ in range(3):
        pw, can_retry = wifi_password(ask, name, security, retry) if needs_pw else (None, False)
        r = attempt(password=pw, security=security, hidden=hidden)
        if r["joined"]:
            return _joined(r, a, port)
        if r["code"] == "auth_failed" and can_retry:
            print("That password didn't work. Try again.")
            retry = True
            continue
        break
    raise SystemExit(_failed(name, r))


def _failed(name, r):
    detail("the Pi said: %s (%s)", r.get("reason"), r.get("code"))
    return {"auth_failed": "The password for %s didn't work." % name,
            "no_ip": "%s connected to %s, but the network didn't give it an address." % (PI, name),
            "not_found": "%s can't see %s. (It can only use 2.4 GHz networks; is it in range?)" % (PI, name),
            "timeout": "%s couldn't connect to %s." % (PI, name)}.get(
                r.get("code"), "%s couldn't join %s: %s" % (PI, name, r.get("reason")))


def startup_join(session, name, a, port):
    """`audiolink host --wifi NAME`: before the daemon's logs start, wait for the Pi
    and run the join flow. A failed join doesn't stop the daemon: the Pi stays on
    (or falls back to) its saved networks."""
    line_buffered()
    if not session.peer_alive:
        print("Waiting for the Pi (plug it in)...", flush=True)
    while not (session.peer_alive and session.peer_info):
        time.sleep(0.25)
    _set_pi(session.peer_info)
    if not session.peer_has("wifi"):
        print("%s's audiolink is too old to manage Wi-Fi; update it. Skipping --wifi." % PI)
        return
    try:
        st = call("wifi.status", port=port)
        if st["ssid"] == name:
            print("%s is already on %s." % (PI, name))
            detail("IP %s", st["ip"])
            if st.get("internet") == "portal":
                offer_portal(a, port)
            return
        join_flow(name, a, port)
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code)
        print("%s stays on the networks it knows. To try again: audiolink wifi join \"%s\"" % (PI, name))


def _joined(r, a, port):
    detail("IP %s, %s", r["ip"], SEC_LABEL.get(r.get("security"), "?"))
    internet = r.get("internet")
    print("%s is on %s%s" % (PI, r["ssid"], {"online": " and online.", "offline": ", but it has no internet there."}
                             .get(internet, ".")))
    if internet == "portal":
        offer_portal(a, port)
    elif internet == "offline":
        detail("internet check: %s", r.get("detail", "no response"))


def pick_flow(a, port):
    """`audiolink wifi` with no action: numbered list from a fresh scan."""
    print("Looking for Wi-Fi networks near %s..." % PI, flush=True)
    nets = call("wifi.scan", port=port)
    st = call("wifi.status", port=port)
    opts = []
    for n in nets:
        tags = [SEC_LABEL[n["security"]]]
        if n["ssid"] == st["ssid"]:
            tags.append("connected")
        elif n["saved"]:
            tags.append("known")
        opts.append(("%-32s %s  %s" % (n["ssid"][:32], bars(n["signal"]), ", ".join(tags)), n))
    opts.append(("(a hidden network: type its name)", None))
    choice = Prompter(a, "--security, --hidden, --password-stdin").choose("Which network should %s join?" % PI, opts)
    if choice is None:
        name = Prompter._input("Network name: ").strip()
        a.hidden = True
        return join_flow(name, a, port)
    if choice["ssid"] == st["ssid"]:
        print("%s is already on %s." % (PI, choice["ssid"]))
        return
    return join_flow(choice["ssid"], a, port, known_security=choice["security"])


def pi_on_usb(port):
    """Is a Pi with Wi-Fi management connected to a running host on this machine?"""
    try:
        peer = call("peer", port=port)
    except SystemExit:
        return False
    return bool(peer.get("connected")) and "wifi" in (peer.get("peer") or {}).get("caps", [])


def wifi_command(a):
    line_buffered()
    port = a.control_port
    peer = call("peer", port=port)
    if not peer["connected"]:
        raise SystemExit("audiolink host is running, but no Pi is plugged in.")
    _set_pi(peer["peer"])
    if "wifi" not in (peer["peer"] or {}).get("caps", []):
        raise SystemExit("%s's audiolink is too old to manage Wi-Fi; update it." % PI)

    if a.action is None:
        return pick_flow(a, port)
    if a.action == "scan":
        print("Looking for Wi-Fi networks near %s..." % PI, flush=True)
        nets = call("wifi.scan", port=port)
        if not nets:
            print("%s can't see any networks. (It can only use 2.4 GHz networks.)" % PI)
        for n in nets:
            print("  %-32s %s  %-16s %s" % (n["ssid"][:32], bars(n["signal"]), SEC_LABEL[n["security"]],
                                            "known" if n["saved"] else ""))
            detail("%d dBm, %d MHz", n["signal"], n["freq"])
    elif a.action == "status":
        st = call("wifi.status", port=port)
        internet = st.get("internet")
        if st["ssid"]:
            print("%s is on %s%s" % (PI, st["ssid"], {
                "online": " and online.", "offline": ", but it has no internet there.",
                "portal": ", but the network wants you to sign in first.\n"
                          "To sign in from this computer: audiolink wifi portal"}.get(internet, ".")))
        else:
            print("%s isn't on any Wi-Fi network." % PI)
        detail("state %s, IP %s, %s MHz", st["state"], st["ip"] or "none", st.get("freq"))
        print("Networks it knows: %s" % (", ".join(st["saved"]) or "none yet"))
    elif a.action == "list":
        nets = call("wifi.list", port=port)
        if not nets:
            print("%s doesn't know any networks yet. To add one: audiolink wifi" % PI)
        else:
            print("Networks %s knows (most recently used first):" % PI)
        for n in nets:
            notes = [SEC_LABEL.get(n["security"], n["security"])] + (["hidden"] if n["hidden"] else []) + \
                (["password saved, not used yet"] if not n.get("last_used") else [])
            print("  %-32s %s" % (n["ssid"], ", ".join(notes)))
    elif a.action == "join":
        if not a.name:
            raise SystemExit("Which network? Usage: audiolink wifi join NAME")
        join_flow(a.name, a, port)
    elif a.action == "ap":
        sub = a.name or "status"
        if sub not in ("on", "off", "status"):
            raise SystemExit("Usage: audiolink wifi ap on|off|status")
        if sub == "on":
            print("Turning on %s's setup network (it leaves its current Wi-Fi meanwhile)..." % PI, flush=True)
        st = call("setup_ap." + sub, port=port)
        detail("%s", st)
        if st["active"]:
            print("%s's setup network, %s, is on. Computers that join it can reach %s." % (PI, st["ssid"], PI))
            if "manual" in st["mode"]:
                print("It stays on until: audiolink wifi ap off")
        else:
            print("%s's setup network is off." % PI)
            print("It turns on by itself when %s has no computer plugged in and no Wi-Fi." % PI)
    elif a.action == "portal":
        portal_flow(port)
    elif a.action == "check":
        r = call("wifi.check", port=port)
        print({"online": "%s is online (on %s)." % (PI, r.get("ssid")),
               "portal": "%s is on %s, but the network wants you to sign in first.\n"
                         "To sign in from this computer: audiolink wifi portal" % (PI, r.get("ssid")),
               }.get(r["internet"], "%s has no internet%s." % (PI, " on %s" % r["ssid"] if r.get("ssid") else "")))
        detail("%s", r)
        raise SystemExit({"online": 0, "portal": 2}.get(r["internet"], 3))
    elif a.action == "forget":
        if not a.name:
            raise SystemExit("Which network? Usage: audiolink wifi forget NAME")
        call("wifi.forget", {"ssid": a.name}, port=port)
        print("%s forgot %s." % (PI, a.name))
