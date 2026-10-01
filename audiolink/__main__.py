"""audiolink command line.

  audiolink host --expose 2222:22            Mac/PC: Pi listens on 2222, connections reach local sshd
  audiolink host --allow 22                  Mac/PC: let the Pi's own --forward streams reach local sshd
  audiolink host --forward 8080:pi-lan:80    Mac/PC: tunnel local :8080 to a host on the Pi's side
  audiolink pi --forward 2222:22             Pi: expose the host's port 22 as Pi port 2222
  audiolink list-devices                     show audio devices (host side)

--forward LPORT:[HOST:]PORT  listen on LPORT here; each connection asks the peer to
                             connect to HOST:PORT (HOST defaults to the peer's 127.0.0.1)
--allow [HOST:]PORT          let the peer open streams to HOST:PORT here (default deny)
--expose PIPORT:[HOST:]PORT  (host) ask the Pi to listen on PIPORT and bring connections to
                             HOST:PORT here (HOST defaults to 127.0.0.1); implies --allow
"""
import argparse
import collections
import logging
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
    roles = {"host": sub.add_parser("host", help="run on the Mac/PC the Pi is plugged into"),
             "pi": sub.add_parser("pi", help="run on the Pi")}
    for name, p in roles.items():
        p.add_argument("--forward", action="append", default=[], metavar="LPORT:[HOST:]PORT")
        p.add_argument("--allow", action="append", default=[], metavar="[HOST:]PORT")
        p.add_argument("--stats", type=float, default=0, help="log stats every N seconds")
        p.add_argument("-v", "--verbose", action="store_true")
    roles["host"].add_argument("--expose", action="append", default=[], metavar="PIPORT:[HOST:]PORT")
    roles["host"].add_argument("--out-device", help="output device name (substring)")
    roles["host"].add_argument("--in-device", help="input device name (substring)")
    roles["pi"].add_argument("--device", default="hw:0,0", help="ALSA device")
    roles["pi"].add_argument("--expose-ports", default="1024-65535", metavar="LO-HI",
                             help="ports the host may ask the Pi to expose")
    a = ap.parse_args(argv)

    if a.cmd == "list-devices":
        return list_devices()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    allow = [x for spec in a.allow for x in spec.split(",") if x]
    if a.cmd == "host":
        node = Node("host", allow=allow, expose=[parse_forward(x) for x in a.expose])
    else:
        node = Node("pi", allow=allow, expose_ports=a.expose_ports)
    for spec in a.forward:
        node.forward(*parse_forward(spec))
    if a.cmd == "pi":
        from .audio import run_alsa
        run_alsa(node.link, a.device)
    else:
        from .audio import run_portaudio
        run_portaudio(node.link, a.out_device, a.in_device)
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
