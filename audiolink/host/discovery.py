"""`audiolink discovery enable|disable|status|publish|code` for the Pi plugged in here.

The Pi owns its identity (services/identity.py) and publishes its own record
(services/discovery.py); these commands just steer it through the running host.
Discovery is only about the DHT record (which network, which address): the relay
reaches the Pi either way, for computers that have its code.
"""
import time

from .control import call
from ..ui import ago, detail, line_buffered

EXPIRED = 2 * 3600      # DHT nodes drop items after about this long (the Pi republishes every 30 min)


def status_line(name, st, discoverable):
    """One line on how the record is doing (st: discovery.status from the Pi)."""
    if not discoverable:
        return "%s: not published (turn on: audiolink discovery enable); the relay works either way" % name
    age = st["now"] - st["published"] if st.get("published") else None
    last = "; last published %s" % ago(time.time() - age) if age is not None else ""
    if age is None and not st.get("clock_known"):
        return "%s: discovery on, not published yet (waiting for the time from a computer)" % name
    if st.get("online") is False:
        return "%s: discovery on, waiting for internet%s" % (name, last)
    if st.get("error"):
        return "%s: discovery on, publishing failed (retrying%s)" % (name, last)
    if age is None:
        return "%s: discovery on, publishing shortly" % name
    if age > EXPIRED:
        return "%s: last published %s: record probably expired (offline?)" % (name, ago(time.time() - age))
    return "%s: published %s" % (name, ago(time.time() - age))


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
            online = call("discovery.status", port=port).get("online")
            print("%s: discovery on: publishes its network and address on the DHT%s (the relay works either way)"
                  % (name, " once it's online" if online is False else ""))
            detail("an encrypted record, readable only with its code")
        else:
            print("%s: no longer publishes where it is (its last record expires within hours); still reachable\n"
                  "through the relay by computers with its code" % name)
    elif a.action == "code":
        print(call("identity.code", port=port)["code"])
    elif a.action == "publish":
        call("discovery.publish", port=port)
        if not info["discoverable"]:
            print("%s: discovery is off (turn on: audiolink discovery enable)" % name)
        elif call("discovery.status", port=port).get("online") is False:
            print("%s: publishes once it's online (check: audiolink discovery status)" % name)
        else:
            print("%s: publishing within 30s (check: audiolink discovery status)" % name)
    else:  # status
        st = call("discovery.status", port=port)
        detail("%s", st)
        print(status_line(name, st, info["discoverable"]))
        if st.get("published"):
            detail("%d DHT nodes, %.1fs: %s", st.get("nodes", 0), st.get("took", 0), st.get("record"))
        if st.get("error"):
            detail("last error: %s", st["error"])
