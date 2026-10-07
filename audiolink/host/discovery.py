"""`audiolink discovery enable|disable|status|publish|code` for the Pi plugged in here.

The Pi owns its identity (services/identity.py) and publishes its own record
(services/discovery.py); these commands just steer it through the running host.
"""
from .control import call
from ..ui import detail, line_buffered


def discovery_command(a):
    line_buffered()
    port = a.control_port
    peer = call("peer", port=port)
    if not peer.get("connected"):
        raise SystemExit("No Pi plugged in")
    if "identity" not in (peer.get("peer") or {}).get("caps", []):
        raise SystemExit("Pi's audiolink too old for this (update it)")
    info = call("identity.info", port=port)
    name = info["name"]
    if a.action in ("enable", "disable"):
        info = call("identity.set_discoverable", {"on": a.action == "enable"}, port=port)
        if info["discoverable"]:
            print("%s: findable from other networks" % name)
            detail("publishes an encrypted record on the DHT, readable only with its code")
        else:
            print("%s: not findable (its last record expires within hours)" % name)
    elif a.action == "code":
        print(call("identity.code", port=port)["code"])
    elif a.action == "publish":
        call("discovery.publish", port=port)
        print("%s: publishing within 30s (check: audiolink discovery status)" % name)
    else:  # status
        st = call("discovery.status", port=port)
        detail("%s", st)
        if not info["discoverable"]:
            print("%s: not findable (turn on: audiolink discovery enable)" % name)
            return
        if st.get("published"):
            mins = (st["now"] - st["published"]) / 60
            print("%s: findable, last published %s" % (name, "just now" if mins < 1 else "%.0f min ago" % mins))
            detail("%d DHT nodes, %.1fs: %s", st.get("nodes", 0), st.get("took", 0), st.get("record"))
        elif not st.get("clock_known"):
            print("%s: findable, not published yet (waiting for the time from a computer)" % name)
        elif st.get("error"):
            print("%s: findable, publishing failed (retrying; online?)" % name)
            detail("last error: %s", st["error"])
        else:
            print("%s: findable, publishing shortly" % name)
