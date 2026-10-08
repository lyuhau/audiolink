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
  audiolink wifi ap [on|off|auto]            the Pi's own setup network (normally automatic)
  audiolink wifi join NAME --pi PI           over the network (checks the network first when it can)
  audiolink add [CODE]                       add a Pi: plugged in here, on the network, or by code
  audiolink pairing code PI                  a one-time code for adding PI on another machine
  audiolink pis                              list the Pis this machine knows
  audiolink share|forget PI                  print a Pi's connect code / forget it
  audiolink rename PI NEW [--on-pi]          rename here (or on the Pi itself)
  audiolink ssh-config                       ~/.ssh/config entries: then just `ssh PI`
  audiolink find PI                          (any machine) where is it? (DHT lookup)
  audiolink connect PI PORT                  (any machine) ssh ProxyCommand: find the Pi, connect
  audiolink discovery enable|disable|status  whether the plugged-in Pi publishes on the DHT
  audiolink relay [--listen HOST:PORT]       (a server) the relay Pis and clients meet at
  audiolink dht-size                         estimate how many DHT nodes are out there
  audiolink audio [status|sweep|set F|forget] the link's audio format (faster formats = more throughput)

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
from .paths import PI_STATE_DIR
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
    print("\n* = Pi audio device")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="audiolink", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version="audiolink " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-devices", help="list audio devices")
    w = sub.add_parser("wifi", help="manage the Pi's Wi-Fi (needs a running `audiolink host`)")
    w.add_argument("action", nargs="?", choices=["scan", "status", "list", "join", "forget", "portal", "check", "ap"],
                   help="omit to pick a network from a list")
    w.add_argument("name", nargs="?", help="network name for join/forget; on|off|auto for ap")
    w.add_argument("--security", choices=["open", "owe", "wpa2", "wpa3"],
                   help="join: skip detection (needed for hidden networks)")
    w.add_argument("--open", action="store_true", help="join: same as --security open")
    w.add_argument("--hidden", action="store_true", help="join: network doesn't broadcast its name")
    w.add_argument("--reenter", action="store_true", help="join: ask for the password even if saved")
    w.add_argument("--portal", action="store_true", help="join: open a captive portal login without asking")
    w.add_argument("--no-portal", action="store_true", help="join: never offer a captive portal login")
    w.add_argument("--password-stdin", action="store_true", help="join: read the password from stdin")
    w.add_argument("--pi", help="join over the network: which Pi (default: the only one you know)")
    w.add_argument("--via-network", action="store_true", help="join over the network even if plugged in here")
    w.add_argument("--tries", type=int, default=1, help="over the network: connection attempts before giving up (max 3)")
    w.add_argument("--at", action="append", metavar="ADDRESS", help="the Pi's address, if not found")
    dsc = sub.add_parser("discovery", help="whether the plugged-in Pi publishes on the DHT")
    dsc.add_argument("action", nargs="?", default="status",
                     choices=["enable", "disable", "code", "status", "publish"])
    add = sub.add_parser("add", help="add a Pi: the one plugged in here, or by connect code")
    add.add_argument("code", nargs="?", help="a connect code (al1-...) someone shared")
    add.add_argument("--name", help="what to call it on this machine")
    add.add_argument("--no-lookup", action="store_true", help="don't look its name up on the DHT")
    add.add_argument("--at", action="append", metavar="ADDRESS", help="try a Pi at this address")
    add.add_argument("--pairing-code", metavar="CODE", help="the 6-digit code (instead of being asked)")
    prg = sub.add_parser("pairing", help="pairing codes for adding a Pi on another machine")
    prg.add_argument("action", choices=["code"])
    prg.add_argument("pi")
    prg.add_argument("--at", action="append", metavar="ADDRESS", help="the Pi's address, if not found")
    sub.add_parser("pis", help="list the Pis this machine knows")
    for cmd, helptext in (("share", "print a Pi's connect code"), ("forget", "forget a Pi")):
        sub.add_parser(cmd, help=helptext).add_argument("name")
    ren = sub.add_parser("rename", help="rename a Pi on this machine (or on the Pi: --on-pi)")
    ren.add_argument("name")
    ren.add_argument("new")
    ren.add_argument("--on-pi", action="store_true", help="rename the Pi itself (it must be plugged in here)")
    sshc = sub.add_parser("ssh-config", help="print ~/.ssh/config entries for known Pis")
    sshc.add_argument("--user", help="login user on the host behind the Pi (default: you)")
    sshc.add_argument("--port", type=int, default=2222, help="Pi port to use when unknown")
    fnd = sub.add_parser("find", help="look a Pi up on the DHT")
    fnd.add_argument("pi", help="name (see `audiolink pis`) or connect code")
    con = sub.add_parser("connect", help="pipe stdin/stdout to a Pi port (ssh ProxyCommand)")
    con.add_argument("pi", help="name (see `audiolink pis`) or connect code")
    con.add_argument("port", type=int)
    aud = sub.add_parser("audio", help="the link's audio format (needs a running `audiolink host`)")
    aud.add_argument("action", nargs="?", default="status", choices=["status", "sweep", "set", "forget"],
                     help="sweep: measure every candidate format and keep the fastest")
    aud.add_argument("format", nargs="?", help="set: RATExCHANNELS (e.g. 96000x8) or baseline")
    rly = sub.add_parser("relay", help="run a relay that Pis and clients meet at (on a server)")
    rly.add_argument("--listen", default="0.0.0.0:443", metavar="HOST:PORT")
    rly.add_argument("--cert", default="keys/relay.crt")
    rly.add_argument("--key", default="keys/relay.key")
    rly.add_argument("--status", action="store_true", help="ask the running relay for its counts")
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
    roles["host"].add_argument("--audio-format", default="auto", metavar="auto|baseline|RATExCH",
                               help="negotiate a faster audio format with the Pi (default: auto)")
    roles["host"].add_argument("--out-device", help="output device name (substring)")
    for p in (roles["host"], w, dsc, add, ren, prg, aud):
        p.add_argument("--control-port", type=int, default=None, help="local control port (default 47800)")
    roles["host"].add_argument("--in-device", help="input device name (substring)")
    roles["pi"].add_argument("--device", default="hw:0,0", help="ALSA device")
    roles["pi"].add_argument("--state-dir", default=PI_STATE_DIR, help="where the Pi keeps its identity")
    roles["pi"].add_argument("--wifi-store", default=PI_STATE_DIR + "/networks.json",
                             help="where saved Wi-Fi networks are kept")
    roles["pi"].add_argument("--expose-ports", default="1024-65535", metavar="LO-HI",
                             help="ports the host may ask the Pi to expose")
    for p in sub.choices.values():       # every command: -v shows the technical details
        if not any("--verbose" in act.option_strings for act in p._actions):
            p.add_argument("-v", "--verbose", action="store_true", help="show technical details too")
    a = ap.parse_args(argv)
    from . import ui
    ui.VERBOSE = bool(getattr(a, "verbose", False))

    if a.cmd == "list-devices":
        return list_devices()
    if a.cmd == "pairing":
        from .client.pairing import code_command
        return code_command(a)
    if a.cmd in ("add", "pis", "share", "forget", "rename", "ssh-config"):
        from .client import pis
        return getattr(pis, a.cmd.replace("-", "_") + "_command")(a)
    if a.cmd == "relay":
        from .relay.server import relay_command
        return relay_command(a)
    if a.cmd in ("find", "connect", "dht-size"):
        from .client import find
        return {"find": find.find_command, "connect": find.connect_command,
                "dht-size": find.dht_size_command}[a.cmd](a)
    from .host import control
    port = getattr(a, "control_port", None) or control.DEFAULT_PORT
    if a.cmd == "wifi":
        from .client import wifi_remote
        from .host.wifi_cli import wifi_command, pi_on_usb
        a.control_port = port
        if a.action == "join" and (a.via_network or a.pi or not pi_on_usb(port)):
            return wifi_remote.change_command(a)
        return wifi_command(a)
    if a.cmd == "audio":
        from .host.audio_format import audio_command
        a.control_port = port
        return audio_command(a)
    if a.cmd == "discovery":
        from .host.discovery import discovery_command
        a.control_port = port
        return discovery_command(a)

    from .ui import LogGate
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
    """The Pi: identity, Wi-Fi management, the browse-as-the-Pi proxy, discovery, streams; ALSA audio
    and its format."""
    from .services.discovery import DiscoveryService
    from .services.identity import IdentityService
    from .services.pairing import PairingService
    from .services.endpoint import Endpoint
    from .services.wifi_remote import WifiRemoteService
    from .services.setup_ap import SetupApService
    from .services.presence import Presence
    from .services.radio import Radio
    from .services.proxy import ProxyService
    from .services.wifi import WifiService
    from .services.audio_format import AudioFormatService
    from .wire.alsa import AlsaAudio
    from .wire.gadget import Gadget
    radio = Radio()                     # the one owner of the Wi-Fi chip
    wifi = WifiService(a.wifi_store, radio)
    proxy = ProxyService(on_request=wifi.note_demand)
    presence = Presence(wifi.ip)
    streams = Streams("pi", allow=allow + ["%s:%d" % proxy.target], expose_ports=a.expose_ports)
    identity = IdentityService(a.state_dir)
    endpoint = Endpoint(identity, presence=presence)
    setup_ap = SetupApService(wifi, identity, presence, radio)
    discovery = DiscoveryService(identity, wifi, streams)
    session = Session("pi", [streams, wifi, proxy, identity, discovery, PairingService(identity, endpoint),
                             setup_ap, WifiRemoteService(wifi, setup_ap, endpoint, discovery, radio)])
    audio = AlsaAudio(session.link, a.device)
    audio.start()
    session.add(AudioFormatService(audio, Gadget()))
    for spec in a.forward:
        streams.forward(*parse_forward(spec))
    setup_ap.run()
    return session, streams


def run_host(a, allow, port, gate):
    """The host: streams (+ --expose), the control port, portal handling, identity custody,
    audio-format negotiation; PortAudio."""
    from .host import control
    from .host.audio_format import AudioNegotiator, parse_format
    from .host.identity import HostIdentity
    from .host.portal import PortalWatcher
    from .host.wifi_cli import startup_join
    from .services.proxy import PROXY_PORT
    from .wire.host_audio import run_portaudio
    startup_wifi = bool(a.wifi)
    if startup_wifi:
        gate.pause()             # the Wi-Fi prompts own the terminal until startup is done
    streams = Streams("host", allow=allow, expose=[parse_forward(x) for x in a.expose])
    watcher = PortalWatcher(port, gate)
    negotiator = AudioNegotiator(a.audio_format if a.audio_format in ("auto", "baseline")
                                 else parse_format(a.audio_format))
    session = Session("host", [streams, watcher, HostIdentity(), negotiator])
    for spec in a.forward:
        streams.forward(*parse_forward(spec))
    run_portaudio(session.link, a.out_device, a.in_device)
    local_ops = {"peer": lambda args: {"connected": session.peer_alive, "peer": session.peer_info,
                                       "proxy_port": a.proxy_port}}
    local_ops.update(watcher.local_ops())
    local_ops.update(negotiator.local_ops())
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
