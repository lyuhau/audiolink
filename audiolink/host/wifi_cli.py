"""`audiolink wifi ...` and `audiolink host --wifi NAME`: manage the Pi's Wi-Fi from the host.

Joining works out the network's security from the Pi's scan and asks only for what's
missing; it suggests near-misses (5 GHz twins, other-case names) and re-prompts on a
wrong password. Requests go through the running host daemon (control.call).
"""
import time

from .control import call
from .portal import offer_portal, portal_flow
from ..ui import Prompter, detail, done, line_buffered, step
from ..wifi_ui import FAILURE, SEC_LABEL, bars, wifi_password

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
                return ("%s is 5 GHz; %s only sees 2.4 GHz. Same network on 2.4 GHz: %s" % (name, PI, twin), twin)
            return ("%s is 5 GHz; %s only sees 2.4 GHz (use the router's 2.4 GHz network)" % (name, PI), None)
    case = [v for v in visible if v.lower() == low]
    if len(case) == 1:
        return ("No network named %s; found %s" % (name, case[0]), case[0])
    close = [v for v in visible if low in v.lower() or v.lower() in low]
    if close:
        return ("No network named %s. Similar: %s" % (name, ", ".join(close[:5])), None)
    return ("No network named %s in range (2.4 GHz only)" % name, None)


def join_flow(name, a, port, known_security=None):
    """Work out what kind of network NAME is, ask only for what's needed, then join."""
    ask = Prompter(a, "--security, --hidden, --password-stdin")
    saved = {n["ssid"]: n for n in call("wifi.list", port=port)}
    security = getattr(a, "security", None) or ("open" if getattr(a, "open", False) else None) \
        or known_security
    hidden = getattr(a, "hidden", False)

    def attempt(**args):
        step("Connecting %s to %s" % (PI, name))
        return call("wifi.join", dict(args, ssid=name), port=port)

    # 1. Saved: just switch to it; if its key is now rejected, ask for the new password.
    if name in saved and not security and not getattr(a, "reenter", False):
        r = attempt(use_saved=True)
        if r["joined"]:
            return _joined(r, a, port)
        if r["code"] != "auth_failed":
            raise SystemExit(_failed(name, r))
        done("saved password rejected (changed?)")
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
            if suggestion and ask.yes("Use %s?" % suggestion, default=True):
                return join_flow(suggestion, a, port)     # start over: it may be saved
            elif ask.yes("Is %s hidden (doesn't broadcast its name)?" % name):
                hidden = True
                security = ask.choose("Security:", [("password (most networks)", "wpa2"),
                                                    ("password, WPA3 only", "wpa3"), ("none (open)", "open")])
            else:
                raise SystemExit(1)
        if matches:
            kinds = sorted({m["security"] for m in matches})
            if len(kinds) > 1:
                print("%d networks named %s:" % (len(kinds), name))
                security = ask.choose("Which one?", [(SEC_LABEL.get(k, k), k) for k in kinds])
            else:
                security = kinds[0]

    if hidden and not security:
        # Hidden networks aren't in the scan, so their security can't be detected.
        security = ask.choose("Security of %s:" % name, [("password (most networks)", "wpa2"),
                                                         ("password, WPA3 only", "wpa3"), ("none (open)", "open")])

    if security == "wep":
        raise SystemExit("%s uses WEP (obsolete, not supported)" % name)
    if security == "enterprise":
        raise SystemExit("%s needs a username (enterprise Wi-Fi, not supported yet)" % name)

    # 3. Join, re-prompting on a wrong password.
    needs_pw = security in ("wpa2", "wpa3", "wpa2/wpa3")
    if security == "wpa3":
        print("Note: WPA3, so %s stores the password itself (not a derived key)" % PI)
    retry = False
    for _ in range(3):
        pw, can_retry = wifi_password(ask, name, security, retry) if needs_pw else (None, False)
        r = attempt(password=pw, security=security, hidden=hidden)
        if r["joined"]:
            return _joined(r, a, port)
        if r["code"] == "auth_failed" and can_retry:
            done("wrong password")
            retry = True
            continue
        break
    raise SystemExit(_failed(name, r))


def _failed(name, r):
    """Finish the "Connecting..." line with why it failed; returns where the Pi is."""
    done(FAILURE.get(r.get("code"), "failed"))
    detail("the Pi said: %s (%s)", r.get("reason"), r.get("code"))
    return "%s staying on its known networks" % PI


def startup_join(session, name, a, port):
    """`audiolink host --wifi NAME`: before the daemon's logs start, wait for the Pi
    and run the join flow. A failed join doesn't stop the daemon: the Pi stays on
    (or falls back to) its saved networks."""
    line_buffered()
    if not session.peer_alive:
        print("Waiting for Pi (plug it in)...", flush=True)
    while not (session.peer_alive and session.peer_info):
        time.sleep(0.25)
    _set_pi(session.peer_info)
    if not session.peer_has("wifi"):
        print("%s: audiolink too old for Wi-Fi management (update it); skipping --wifi" % PI)
        return
    try:
        st = call("wifi.status", port=port)
        if st["ssid"] == name:
            print("%s: already on %s" % (PI, name))
            detail("IP %s", st["ip"])
            if st.get("internet") == "portal":
                offer_portal(a, port)
            return
        join_flow(name, a, port)
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code)
        print("Retry: audiolink wifi join \"%s\"" % name)


def _joined(r, a, port):
    detail("IP %s, %s", r["ip"], SEC_LABEL.get(r.get("security"), "?"))
    internet = r.get("internet")
    done({"online": "online", "offline": "connected, no internet", "portal": "connected, sign-in page"}
         .get(internet, "connected"))
    if internet == "portal":
        offer_portal(a, port)
    elif internet == "offline":
        detail("internet check: %s", r.get("detail", "no response"))


def pick_flow(a, port):
    """`audiolink wifi` with no action: numbered list from a fresh scan."""
    step("Scanning from %s" % PI)
    nets = call("wifi.scan", port=port)
    st = call("wifi.status", port=port)
    done("%d networks" % len(nets))
    opts = []
    for n in nets:
        tags = [SEC_LABEL.get(n["security"], n["security"])]
        if n["ssid"] == st["ssid"]:
            tags.append("connected")
        elif n["saved"]:
            tags.append("known")
        opts.append(("%-32s %s  %s" % (n["ssid"][:32], bars(n["signal"]), ", ".join(tags)), n))
    opts.append(("(a hidden network: type its name)", None))
    choice = Prompter(a, "--security, --hidden, --password-stdin").choose("Join which network?", opts)
    if choice is None:
        name = Prompter._input("Network name: ").strip()
        a.hidden = True
        return join_flow(name, a, port)
    if choice["ssid"] == st["ssid"]:
        print("%s: already on %s" % (PI, choice["ssid"]))
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
        raise SystemExit("No Pi plugged in")
    _set_pi(peer["peer"])
    if "wifi" not in (peer["peer"] or {}).get("caps", []):
        raise SystemExit("%s: audiolink too old for Wi-Fi management (update it)" % PI)

    if a.action is None:
        return pick_flow(a, port)
    if a.action == "scan":
        step("Scanning from %s" % PI)
        nets = call("wifi.scan", port=port)
        done("%d networks" % len(nets) if nets else "none in range (2.4 GHz only)")
        for n in nets:
            print("%-32s %s  %-16s %s" % (n["ssid"][:32], bars(n["signal"]), SEC_LABEL.get(n["security"], n["security"]),
                                            "known" if n["saved"] else ""))
            detail("%d dBm, %d MHz", n["signal"], n["freq"])
    elif a.action == "status":
        st = call("wifi.status", port=port)
        internet = st.get("internet")
        if st["ssid"]:
            print("%s: %s, %s" % (PI, st["ssid"], {
                "online": "online", "offline": "no internet",
                "portal": "sign-in page (run: audiolink wifi portal)"}.get(internet, "connected")))
        else:
            print("%s: not on Wi-Fi" % PI)
        detail("state %s, IP %s, %s MHz", st["state"], st["ip"] or "none", st.get("freq"))
        print("Known networks: %s" % (", ".join(st["saved"]) or "none"))
    elif a.action == "list":
        nets = call("wifi.list", port=port)
        if not nets:
            print("No known networks (add one: audiolink wifi)")
        for n in nets:
            notes = [SEC_LABEL.get(n["security"], n["security"])] + (["hidden"] if n["hidden"] else []) + \
                (["not used yet"] if not n.get("last_used") else [])
            print("%-32s %s" % (n["ssid"], ", ".join(notes)))
    elif a.action == "join":
        if not a.name:
            raise SystemExit("Usage: audiolink wifi join NAME")
        join_flow(a.name, a, port)
    elif a.action == "ap":
        sub = a.name or "status"
        if sub not in ("on", "off", "auto", "status"):
            raise SystemExit("Usage: audiolink wifi ap [on|off|auto]")
        if sub == "on":
            step("Starting %s's setup network" % PI)
        st = call("setup_ap." + sub, port=port)
        if sub == "on":
            done("on" if st["active"] else "failed")
        detail("%s", st)
        if st["active"]:
            who = {0: "", 1: ", 1 device on it"}.get(st["stations"], ", %d devices on it" % st["stations"])
            print("Setup network: %s%s%s" % (st["ssid"], who, {"on": " (on until: audiolink wifi ap auto)",
                                                               "auto": ""}.get(st["mode"], "")))
        elif st["mode"] == "off":
            print("Setup network: off (until: audiolink wifi ap auto)")
        else:
            print("Setup network: off (opens when nobody is connected to %s)" % PI)
    elif a.action == "portal":
        portal_flow(port)
    elif a.action == "check":
        r = call("wifi.check", port=port)
        print({"online": "%s: online (%s)" % (PI, r.get("ssid")),
               "portal": "%s: %s, sign-in page (run: audiolink wifi portal)" % (PI, r.get("ssid")),
               }.get(r["internet"], "%s: no internet" % PI))
        detail("%s", r)
        raise SystemExit({"online": 0, "portal": 2}.get(r["internet"], 3))
    elif a.action == "forget":
        if not a.name:
            raise SystemExit("Usage: audiolink wifi forget NAME")
        call("wifi.forget", {"ssid": a.name}, port=port)
        print("Forgot %s" % a.name)
