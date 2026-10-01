"""audiolink command line.

  audiolink host --expose 2222:22            Mac/PC: Pi listens on 2222, connections reach local sshd
  audiolink host --wifi NAME --expose ...    same, after first putting the Pi on Wi-Fi NAME
  audiolink host --allow 22                  Mac/PC: let the Pi's own --forward streams reach local sshd
  audiolink host --forward 8080:pi-lan:80    Mac/PC: tunnel local :8080 to a host on the Pi's side
  audiolink pi --forward 2222:22             Pi: expose the host's port 22 as Pi port 2222
  audiolink list-devices                     show audio devices (host side)
  audiolink wifi                             pick a network for the Pi from a list
  audiolink wifi join NAME                   join (detects the security type, asks only if needed)
  audiolink wifi scan|status|list|forget     manage the Pi's Wi-Fi through a running host

--forward LPORT:[HOST:]PORT  listen on LPORT here; each connection asks the peer to
                             connect to HOST:PORT (HOST defaults to the peer's 127.0.0.1)
--allow [HOST:]PORT          let the peer open streams to HOST:PORT here (default deny)
--expose PIPORT:[HOST:]PORT  (host) ask the Pi to listen on PIPORT and bring connections to
                             HOST:PORT here (HOST defaults to 127.0.0.1); implies --allow
"""
import argparse
import collections
import logging
import logging.handlers
import sys
import threading
import time

from . import __version__
from .node import Node, parse_forward

log = logging.getLogger("audiolink")


def report(node, interval):
    last = collections.Counter()
    while True:
        time.sleep(interval)
        s = node.link.stats.copy()
        rate = {k: (s[k] - last[k]) / interval for k in ("tcp_tx_bytes", "tcp_rx_bytes")}
        last = s
        with node.link.cv:
            inflight = len(node.link.unacked)
        log.info("streams=%d inflight=%d tcp_out=%.1fKB/s tcp_in=%.1fKB/s | %s",
                 len(node.streams), inflight, rate["tcp_tx_bytes"] / 1e3, rate["tcp_rx_bytes"] / 1e3,
                 " ".join("%s=%s" % kv for kv in sorted(s.items()) if not kv[0].startswith("tcp_")))


def list_devices():
    from . import portaudio
    from .audio import DEVICE_HINTS
    for d in portaudio.devices():
        mark = "*" if any(h in d["name"].lower() for h in DEVICE_HINTS) else " "
        print("%s %3d  %-40s %-20s in=%d out=%d" % (mark, d["index"], d["name"], d["hostapi"],
                                                    d["inputs"], d["outputs"]))
    print("\n* = looks like the AudioPi gadget")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="audiolink", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version="audiolink " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-devices", help="list audio devices")
    w = sub.add_parser("wifi", help="manage the Pi's Wi-Fi (needs a running `audiolink host`)")
    w.add_argument("action", nargs="?", choices=["scan", "status", "list", "join", "forget"],
                   help="omit to pick a network from a list")
    w.add_argument("name", nargs="?", help="network name for join/forget")
    w.add_argument("--security", choices=["open", "owe", "wpa2", "wpa3"],
                   help="join: skip detection (needed for hidden networks)")
    w.add_argument("--open", action="store_true", help="join: same as --security open")
    w.add_argument("--hidden", action="store_true", help="join: network doesn't broadcast its name")
    w.add_argument("--reenter", action="store_true", help="join: ask for the password even if saved")
    w.add_argument("--password-stdin", action="store_true", help="join: read the password from stdin")
    roles = {"host": sub.add_parser("host", help="run on the Mac/PC the Pi is plugged into"),
             "pi": sub.add_parser("pi", help="run on the Pi")}
    for name, p in roles.items():
        p.add_argument("--forward", action="append", default=[], metavar="LPORT:[HOST:]PORT")
        p.add_argument("--allow", action="append", default=[], metavar="[HOST:]PORT")
        p.add_argument("--stats", type=float, default=0, help="log stats every N seconds")
        p.add_argument("-v", "--verbose", action="store_true")
    roles["host"].add_argument("--expose", action="append", default=[], metavar="PIPORT:[HOST:]PORT")
    roles["host"].add_argument("--wifi", metavar="NAME",
                               help="first put the Pi on this Wi-Fi network (prompts if needed)")
    roles["host"].add_argument("--out-device", help="output device name (substring)")
    for p in (roles["host"], w):
        p.add_argument("--control-port", type=int, default=None, help="local control port (default 47800)")
    roles["host"].add_argument("--in-device", help="input device name (substring)")
    roles["pi"].add_argument("--device", default="hw:0,0", help="ALSA device")
    roles["pi"].add_argument("--wifi-store", default="/var/lib/audiolink/networks.json",
                             help="where saved Wi-Fi networks are kept")
    roles["pi"].add_argument("--expose-ports", default="1024-65535", metavar="LO-HI",
                             help="ports the host may ask the Pi to expose")
    a = ap.parse_args(argv)

    if a.cmd == "list-devices":
        return list_devices()
    from . import control
    port = getattr(a, "control_port", None) or control.DEFAULT_PORT
    if a.cmd == "wifi":
        a.control_port = port
        return control.wifi_command(a)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if a.verbose else logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    startup_wifi = a.cmd == "host" and a.wifi
    if startup_wifi:
        # Hold log output back while the Wi-Fi prompts own the terminal.
        held = logging.handlers.MemoryHandler(100000, flushLevel=logging.CRITICAL + 1, target=console)
        root.addHandler(held)
    else:
        root.addHandler(console)
    allow = [x for spec in a.allow for x in spec.split(",") if x]
    if a.cmd == "host":
        node = Node("host", allow=allow, expose=[parse_forward(x) for x in a.expose])
    else:
        from .wifi import WifiManager
        wifi = WifiManager(a.wifi_store)
        node = Node("pi", allow=allow, expose_ports=a.expose_ports, ctrl_handlers={"wifi": wifi.handle})
    for spec in a.forward:
        node.forward(*parse_forward(spec))
    if a.cmd == "pi":
        from .audio import run_alsa
        run_alsa(node.link, a.device)
    else:
        from .audio import run_portaudio
        run_portaudio(node.link, a.out_device, a.in_device)
        control.serve(node, port)
        if startup_wifi:
            try:
                control.startup_join(node, a.wifi, a, port)
            except KeyboardInterrupt:
                print()
                return 130
            finally:
                held.flush()
                root.removeHandler(held)
                root.addHandler(console)
    log.info("audiolink %s %s up (session %08x); allow=%s%s", __version__, a.cmd, node.link.session,
             ",".join("%s:%d" % t for t in sorted(node.allow)) or "none",
             "; expose=" + ",".join("pi:%d->%s:%d" % (p, h, tp) for p, (h, tp) in node.expose)
             if node.expose else "")
    if a.stats:
        threading.Thread(target=report, args=(node, a.stats), daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
