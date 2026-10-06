"""Discovery, host side: `audiolink discovery enable|code|status|publish|disable`.

The identity lives in the host's config directory (custody, while the Pi has no
storage of its own) and is pushed to the Pi on every link-up (HostDiscovery).
"""
import json
import logging
import os
import threading
import time

from ..paths import config_dir
from ..services import Service

log = logging.getLogger("audiolink")


IDENTITY = config_dir() / "discovery.json"


def load_identity():
    try:
        return json.loads(IDENTITY.read_text())
    except (OSError, ValueError):
        return None


def save_identity(ident):
    IDENTITY.parent.mkdir(parents=True, exist_ok=True)
    tmp = IDENTITY.with_suffix(".tmp")
    tmp.write_text(json.dumps(ident))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(IDENTITY)


# ---------------------------------------------------------------- host daemon
class HostDiscovery(Service):
    """Pushes the identity (or "disabled") to the Pi whenever the link comes up."""

    def on_peer_hello(self, info):
        if "discovery" in info.get("caps", []):
            threading.Thread(target=self.push, daemon=True).start()

    def push(self):
        ident = load_identity()
        args = dict(ident, time=time.time()) if ident else {}
        try:
            self.session.request("discovery.configure", args, timeout=30)
        except Exception as e:
            log.warning("discovery: couldn't configure the Pi: %s", e)


# ---------------------------------------------------------------- `audiolink discovery ...`
def _usage(code):
    print("connect code (share it with clients; it lets them find and read the Pi's address):")
    print("  " + code)
    print("clients:")
    print("  audiolink find %s" % code)
    print("  ssh -o ProxyCommand='python3 audiolink.pyz connect %s %%p' -p 2222 USER@audiopi" % code)


def discovery_command(a):
    from ..discovery.record import code_for, new_identity
    from .control import call
    from .ui import line_buffered
    line_buffered()
    ident = load_identity()
    if a.action == "enable":
        if ident:
            print("discovery is already enabled")
        else:
            ident = new_identity()
            save_identity(ident)
            print("discovery enabled; identity saved in %s" % IDENTITY)
        _usage(code_for(ident))
        _poke(call, a.control_port, "configure")
    elif a.action == "code":
        if not ident:
            raise SystemExit("discovery isn't enabled; run: audiolink discovery enable")
        _usage(code_for(ident))
    elif a.action == "disable":
        if not ident:
            print("discovery is already disabled")
            return
        IDENTITY.unlink()
        print("discovery disabled; the old code stops working once the Pi's record expires (hours)")
        _poke(call, a.control_port, "configure")
    elif a.action == "publish":
        call("discovery.publish", port=a.control_port)
        print("the Pi will publish within ~30 s; check with: audiolink discovery status")
    else:  # status
        print("identity: %s" % ("enabled (%s)" % IDENTITY if ident else "disabled"))
        try:
            st = call("discovery.status", port=a.control_port)
        except SystemExit as e:
            raise SystemExit("couldn't ask the Pi: %s" % e.code)
        if st.get("published"):
            age = st["now"] - st["published"]
            print("last published %.0f min ago to %d DHT nodes (%.1fs): %s"
                  % (age / 60, st.get("nodes", 0), st.get("took", 0), st.get("record")))
        else:
            print("not published yet%s" % (" (%s)" % st["error"] if st.get("error") else
                                           " (waits for the Pi to be online)"))


def _poke(call, port, op):
    """Tell a running host daemon to (re)configure the Pi; fine if none is running."""
    ident = load_identity()
    try:
        call("discovery.configure", dict(ident, time=time.time()) if ident else {}, port=port)
        print("the running host updated the Pi")
    except SystemExit:
        print("(no running host to update; the Pi gets it when `audiolink host` next connects)")
