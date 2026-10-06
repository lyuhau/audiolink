"""`audiolink discovery enable|disable|status|publish|code` for the Pi plugged in here.

The Pi owns its identity (services/identity.py) and publishes its own record
(services/discovery.py); these commands just steer it through the running host.
"""
from .control import call
from ..ui import line_buffered


def discovery_command(a):
    line_buffered()
    port = a.control_port
    peer = call("peer", port=port)
    if not peer.get("connected"):
        raise SystemExit("no Pi is connected to this host right now")
    if "identity" not in (peer.get("peer") or {}).get("caps", []):
        raise SystemExit("this Pi's audiolink is too old for this (update it)")
    info = call("identity.info", port=port)
    if a.action in ("enable", "disable"):
        info = call("identity.set_discoverable", {"on": a.action == "enable"}, port=port)
        print("%s is %s" % (info["name"], "discoverable: it publishes its record on the DHT" if info["discoverable"]
                            else "no longer discoverable (its last record expires from the DHT in hours)"))
    elif a.action == "code":
        print(call("identity.code", port=port)["code"])
    elif a.action == "publish":
        call("discovery.publish", port=port)
        print("%s will publish within ~30 s; check with: audiolink discovery status" % info["name"])
    else:  # status
        st = call("discovery.status", port=port)
        print("%s: %s" % (info["name"], "discoverable" if info["discoverable"] else "not discoverable"))
        if st.get("published"):
            print("last published %.0f min ago to %d DHT nodes (%.1fs): %s"
                  % ((st["now"] - st["published"]) / 60, st.get("nodes", 0), st.get("took", 0), st.get("record")))
        elif not st.get("clock_known"):
            print("not published yet (waiting to learn the time from a host)")
        else:
            print("not published yet%s" % (" (%s)" % st["error"] if st.get("error") else
                                           " (waits for the Pi to be online)"))
