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
  audiolink wifi portal                      log the Pi in to a captive portal from a browser here
  audiolink wifi check                       exit 0 = Pi online, 2 = captive portal, 3 = offline
  audiolink discovery enable|code|status      publish where the Pi is on the BitTorrent DHT
  audiolink find CODE                        (any machine) look the Pi up by its connect code
  audiolink connect CODE PORT                (any machine) ssh ProxyCommand: find the Pi, connect
  audiolink dht-size                         estimate how many DHT nodes are out there

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
from .services.streams import Streams, parse_forward
from .session import Session

log = logging.getLogger("audiolink")


def report(session, streams, interval):
    last = collections.Counter()
    link = session.link
    while True:
        time.sleep(interval)
        s = link.stats.copy()
        rate = {k: (s[k] - last[k]) / interval for k in ("tcp_tx_bytes", "tcp_rx_bytes")}
        last = s
        with link.cv:
            inflight = len(link.unacked)
        log.info("streams=%d inflight=%d tcp_out=%.1fKB/s tcp_in=%.1fKB/s | %s",
                 len(streams.streams), inflight, rate["tcp_tx_bytes"] / 1e3, rate["tcp_rx_bytes"] / 1e3,
                 " ".join("%s=%s" % kv for kv in sorted(s.items()) if not kv[0].startswith("tcp_")))


def list_devices():
    from .wire import portaudio
    from .wire.host_audio import DEVICE_HINTS
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
    w.add_argument("action", nargs="?", choices=["scan", "status", "list", "join", "forget", "portal", "check"],
                   help="omit to pick a network from a list")
    w.add_argument("name", nargs="?", help="network name for join/forget")
    w.add_argument("--security", choices=["open", "owe", "wpa2", "wpa3"],
                   help="join: skip detection (needed for hidden networks)")
    w.add_argument("--open", action="store_true", help="join: same as --security open")
    w.add_argument("--hidden", action="store_true", help="join: network doesn't broadcast its name")
    w.add_argument("--reenter", action="store_true", help="join: ask for the password even if saved")
    w.add_argument("--portal", action="store_true", help="join: open a captive portal login without asking")
    w.add_argument("--no-portal", action="store_true", help="join: never offer a captive portal login")
    w.add_argument("--password-stdin", action="store_true", help="join: read the password from stdin")
    dsc = sub.add_parser("discovery", help="find the Pi from anywhere via the BitTorrent DHT")
    dsc.add_argument("action", nargs="?", default="status",
                     choices=["enable", "disable", "code", "status", "publish"])
    fnd = sub.add_parser("find", help="look the Pi up by its connect code")
    fnd.add_argument("code")
    con = sub.add_parser("connect", help="pipe stdin/stdout to a Pi port (ssh ProxyCommand)")
    con.add_argument("code")
    con.add_argument("port", type=int)
    size = sub.add_parser("dht-size", help="estimate the size of the BitTorrent DHT")
    size.add_argument("--samples", type=int, default=8)
    roles = {"host": sub.add_parser("host", help="run on the Mac/PC the Pi is plugged into"),
             "pi": sub.add_parser("pi", help="run on the Pi")}
    for name, p in roles.items():
        p.add_argument("--forward", action="append", default=[], metavar="LPORT:[HOST:]PORT")
        p.add_argument("--allow", action="append", default=[], metavar="[HOST:]PORT")
        p.add_argument("--stats", type=float, default=0, help="log stats every N seconds")
        p.add_argument("-v", "--verbose", action="store_true")
    roles["host"].add_argument("--expose", action="append", default=[], metavar="PIPORT:[HOST:]PORT")
    roles["host"].add_argument("--proxy-port", type=int, default=47801,
                               help="local HTTP proxy that browses as the Pi (0 = off)")
    roles["host"].add_argument("--wifi", metavar="NAME",
                               help="first put the Pi on this Wi-Fi network (prompts if needed)")
    roles["host"].add_argument("--out-device", help="output device name (substring)")
    for p in (roles["host"], w, dsc):
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
    if a.cmd in ("find", "connect", "dht-size"):
        from .client import find
        return {"find": find.find_command, "connect": find.connect_command,
                "dht-size": find.dht_size_command}[a.cmd](a)
    from .host import control
    port = getattr(a, "control_port", None) or control.DEFAULT_PORT
    if a.cmd == "wifi":
        from .host.wifi_cli import wifi_command
        a.control_port = port
        return wifi_command(a)
    if a.cmd == "discovery":
        from .host.discovery import discovery_command
        a.control_port = port
        return discovery_command(a)

    from .host.ui import LogGate
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if a.verbose else logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    gate = LogGate(console)
    root.addHandler(gate)
    allow = [x for spec in a.allow for x in spec.split(",") if x]
    run = run_host if a.cmd == "host" else run_pi
    session, streams = run(a, allow, port, gate)
    if session is None:
        return 130
    log.info("audiolink %s %s up (session %08x); allow=%s%s", __version__, a.cmd, session.link.session,
             ",".join("%s:%d" % t for t in sorted(streams.allow)) or "none",
             "; expose=" + ",".join("pi:%d->%s:%d" % (p, h, tp) for p, (h, tp) in streams.expose)
             if streams.expose else "")
    if a.stats:
        threading.Thread(target=report, args=(session, streams, a.stats), daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def run_pi(a, allow, port, gate):
    """The Pi: Wi-Fi management, the browse-as-the-Pi proxy, discovery, streams; ALSA audio."""
    from .services.discovery import DiscoveryService
    from .services.proxy import ProxyService
    from .services.wifi import WifiService
    from .wire.alsa import run_alsa
    wifi = WifiService(a.wifi_store)
    proxy = ProxyService(on_request=wifi.manager.note_demand)
    streams = Streams("pi", allow=allow + ["%s:%d" % proxy.target], expose_ports=a.expose_ports)
    session = Session("pi", [streams, wifi, proxy, DiscoveryService()])
    for spec in a.forward:
        streams.forward(*parse_forward(spec))
    run_alsa(session.link, a.device)
    return session, streams


def run_host(a, allow, port, gate):
    """The host: streams (+ --expose), the control port, portal handling; PortAudio."""
    from .host import control
    from .host.discovery import HostDiscovery
    from .host.portal import PortalWatcher
    from .host.wifi_cli import startup_join
    from .services.proxy import PROXY_PORT
    from .wire.host_audio import run_portaudio
    startup_wifi = bool(a.wifi)
    if startup_wifi:
        gate.pause()             # the Wi-Fi prompts own the terminal until startup is done
    streams = Streams("host", allow=allow, expose=[parse_forward(x) for x in a.expose])
    watcher = PortalWatcher(port, gate)
    session = Session("host", [streams, watcher, HostDiscovery()])
    for spec in a.forward:
        streams.forward(*parse_forward(spec))
    run_portaudio(session.link, a.out_device, a.in_device)
    local_ops = {"peer": lambda args: {"connected": session.peer_alive, "peer": session.peer_info,
                                       "proxy_port": a.proxy_port}}
    local_ops.update(watcher.local_ops())
    control.serve(session, port, local_ops)
    if a.proxy_port:
        # Browse as the Pi (captive portals): local HTTP proxy -> Pi's proxy.
        streams.forward(a.proxy_port, ("127.0.0.1", PROXY_PORT), bind="127.0.0.1")
    if startup_wifi:
        try:
            startup_join(session, a.wifi, a, port)
        except KeyboardInterrupt:
            print()
            return None, None
        finally:
            gate.resume()
    watcher.start(skip_initial=startup_wifi)
    return session, streams


if __name__ == "__main__":
    sys.exit(main())
