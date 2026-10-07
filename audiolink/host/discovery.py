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
        raise SystemExit("No Pi is plugged in to this computer right now.")
    if "identity" not in (peer.get("peer") or {}).get("caps", []):
        raise SystemExit("This Pi's audiolink is too old for this; update it.")
    info = call("identity.info", port=port)
    name = info["name"]
    if a.action in ("enable", "disable"):
        info = call("identity.set_discoverable", {"on": a.action == "enable"}, port=port)
        if info["discoverable"]:
            print("%s can now be found from other networks: it posts where it is, encrypted so that\n"
                  "only machines that know it can read it." % name)
        else:
            print("%s stopped posting where it is. Its last post disappears within a few hours." % name)
    elif a.action == "code":
        print(call("identity.code", port=port)["code"])
    elif a.action == "publish":
        call("discovery.publish", port=port)
        print("%s will post where it is within about 30 seconds. To check: audiolink discovery status" % name)
    else:  # status
        st = call("discovery.status", port=port)
        detail("%s", st)
        if not info["discoverable"]:
            print("%s can't be found from other networks (to turn that on: audiolink discovery enable)." % name)
            return
        print("%s can be found from other networks." % name)
        if st.get("published"):
            mins = (st["now"] - st["published"]) / 60
            print("It last posted where it is %s." % ("just now" if mins < 1 else "%.0f minutes ago" % mins))
            detail("%d DHT nodes, %.1fs: %s", st.get("nodes", 0), st.get("took", 0), st.get("record"))
        elif not st.get("clock_known"):
            print("It hasn't posted yet: it's waiting to learn the time from a computer.")
        elif st.get("error"):
            print("It couldn't post where it is yet; it keeps trying. (Is it online?)")
            detail("last error: %s", st["error"])
        else:
            print("It's about to post where it is (it does soon after starting, then every few minutes).")
