"""`audiolink debug on|off|status`: the plugged-in Pi's debug switch (services/access.py).

Only the host changes it (link requests). Debug access is root on the Pi over the
network: `audiolink shell PI`, `audiolink update --pi PI`.
"""
import os
import time

from .control import call
from ..services.access import DEFAULT_FOR, NONE_FOR, parse_duration
from ..ui import detail

ROUTE_NAMES = {"lan": "LAN", "setup": "setup network", "relay": "relay"}
AUTH_NAMES = {"paired": "computers that added it", "key": "debug keys", "none": "anyone, no proof"}
ON_ONLY = (("auth", "--auth"), ("key", "--key"), ("setup", "--setup"), ("for_", "--for"), ("persist", "--persist"))


def _span(s):
    """Seconds -> "45 min", "2 h", "1 h 30 min", "24 h", "36 h", "3 d 4 h" (hours up to 2 days)."""
    m = max(1, int(s + 30) // 60)                # whole minutes
    if m < 60:
        return "%d min" % m
    h, m = divmod(m, 60)
    if h >= 48:
        d, h = divmod(h + (m >= 30), 24)
        return "%d d %d h" % (d, h) if h else "%d d" % d
    return "%d h %d min" % (h, m) if m else "%d h" % h


def _at(secs, now=None):
    """The local time secs from now: "14:05", "14:05 tomorrow", "Mon 20 Oct 14:05"."""
    now = time.time() if now is None else now
    then = time.localtime(now + secs)
    today, end = time.localtime(now), time.localtime(now + 86400)
    clock = time.strftime("%H:%M", then)
    if then[:3] == today[:3]:
        return clock
    if then[:3] == end[:3]:
        return clock + " tomorrow"
    return time.strftime("%a %d %b ", then) + clock


def _keys(paths):
    """Public keys from files: OpenSSH public keys (one per line) or private key files."""
    from ..client.remote import Key
    from ..services.access import parse_key
    out = []
    for path in paths:
        try:
            with open(os.path.expanduser(path)) as f:
                text = f.read()
        except OSError as e:
            raise SystemExit("Can't read %s (%s)" % (path, e.strerror))
        if "PRIVATE KEY" in text:
            out.append(Key(path).pub.hex())
            continue
        lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
        for line in lines:
            try:
                out.append(parse_key(line).hex())
            except ValueError as e:
                if len(lines) == 1:
                    raise SystemExit("%s: %s" % (path, e))
    if not out:
        raise SystemExit("No ed25519 public keys in %s" % ", ".join(paths))
    return out


def show(st):
    if st["mode"] == "off":
        print("Debug: off")
        return
    who = AUTH_NAMES[st["mode"]]
    if st["mode"] == "key":
        who = "%d debug key%s" % (st["keys"], "s"[:st["keys"] != 1])
    print("Debug: on, %s" % who)
    print("Routes: USB, %s" % ", ".join(ROUTE_NAMES[r] for r in st["routes"]))
    print("Ends in %s (at %s)%s" % (_span(st["expires_in"]), _at(st["expires_in"]),
                                    ", kept across restarts" if st["persist"] else ""))
    print("Network shells open: %d" % st["shells"])


def _check(a):
    """The request for a.action, or SystemExit for options that don't fit (before the Pi is asked)."""
    if a.action != "on":
        given = [flag for attr, flag in ON_ONLY if getattr(a, attr, None)]
        if given:
            raise SystemExit("%s go%s with: audiolink debug on" % (", ".join(given), "es"[:len(given) == 1]))
        return {"mode": "off"} if a.action == "off" else None
    auth = a.auth or ("key" if a.key else "paired")
    if a.key and auth != "key":
        raise SystemExit("--key goes with --auth key")
    if auth == "key" and not a.key:
        raise SystemExit("--auth key needs --key FILE (an ed25519 public key)")
    if a.setup and auth != "none":
        raise SystemExit("--setup goes with --auth none (other modes work on every route)")
    try:
        secs = parse_duration(a.for_) if a.for_ else (NONE_FOR if auth == "none" else DEFAULT_FOR)
    except ValueError as e:
        raise SystemExit("--for: %s" % e)
    args = {"mode": auth, "for": secs, "setup": a.setup, "persist": a.persist}
    if auth == "key":
        args["keys"] = _keys(a.key)
    return args


def debug_command(a):
    port = a.control_port
    args = _check(a)
    peer = call("peer", port=port)
    if not peer.get("connected"):
        raise SystemExit("No Pi plugged in (debug status comes from the plugged-in Pi)" if a.action == "status"
                         else "No Pi plugged in (only the computer it's plugged into can change debug access)")
    if "debug" not in (peer.get("peer") or {}).get("caps", []):
        raise SystemExit("The Pi's audiolink has no debug switch; update it (audiolink update)")
    if a.action == "status":
        st = call("debug.status", port=port)
        show(st)
        detail("%s", st)
        return
    if a.action == "off":
        try:
            shells = call("debug.status", port=port).get("shells", 0)
        except SystemExit:
            shells = 0
        call("debug.set", args, port=port)
        print("Debug: off%s" % (" (closed %d network shell%s)" % (shells, "s"[:shells != 1]) if shells else ""))
        return
    st = call("debug.set", args, port=port)
    show(st)
    if args["mode"] == "none":
        print("Warning: anyone on the Pi's Wi-Fi network%s can get a root shell and install updates until %s"
              % (" or its setup network" if "setup" in st["routes"] else "", _at(st["expires_in"])))
