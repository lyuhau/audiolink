"""`audiolink debug on|off|status`: the plugged-in Pi's debug switch (services/access.py).

Only the host changes it (link requests). Debug access is root on the Pi over the
network: `audiolink shell PI`, `audiolink update --pi PI`.
"""
import os

from .control import call
from ..services.access import DEFAULT_FOR, NONE_FOR, parse_duration
from ..ui import detail

ROUTE_NAMES = {"lan": "LAN", "setup": "setup network", "relay": "relay"}
AUTH_NAMES = {"paired": "computers that added it", "key": "debug keys", "none": "anyone, no proof"}


def _span(s):
    s = (s + 30) // 60 * 60                  # whole minutes
    if s >= 86400 and s % 86400 == 0:
        return "%d d" % (s // 86400)
    if s >= 3600:
        return "%d h %02d min" % (s // 3600, s % 3600 // 60) if s % 3600 else "%d h" % (s // 3600)
    return "%d min" % max(1, s // 60)


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
    print("Debug: on, %s%s" % (AUTH_NAMES[st["mode"]], " (%d)" % st["keys"] if st["mode"] == "key" else ""))
    print("Routes: USB, %s" % ", ".join(ROUTE_NAMES[r] for r in st["routes"]))
    print("Ends in %s%s" % (_span(st["expires_in"]), ", kept across restarts" if st["persist"] else ""))
    print("Open shells: %d" % st["shells"])


def debug_command(a):
    port = a.control_port
    peer = call("peer", port=port)
    if not peer.get("connected"):
        raise SystemExit("No Pi plugged in (only its host can change debug access)")
    if "debug" not in (peer.get("peer") or {}).get("caps", []):
        raise SystemExit("The Pi's audiolink has no debug switch; update it (audiolink update)")
    if a.action == "status":
        st = call("debug.status", port=port)
        show(st)
        detail("%s", st)
        return
    if a.action == "off":
        st = call("debug.set", {"mode": "off"}, port=port)
        print("Debug: off")
        return
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
    st = call("debug.set", args, port=port)
    show(st)
    if auth == "none":
        print("Anyone on %s gets a root shell" % " or ".join(ROUTE_NAMES[r] for r in st["routes"]))
