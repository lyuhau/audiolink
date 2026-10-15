"""audiolink command line.

On the computer the Pi is plugged into (the host):
  audiolink host --expose 2222:22            the Pi listens on 2222; connections reach this computer's sshd
  audiolink host --wifi NAME --expose ...    same, after first putting the Pi on Wi-Fi NAME
  audiolink host --allow 22                  let the Pi's own --forward streams reach local sshd
  audiolink host --forward 8080:pi-lan:80    tunnel local :8080 to a host on the Pi's side
  audiolink list-devices                     show audio devices (* = the Pi's)
  audiolink wifi                             pick a network for the Pi from a list
  audiolink wifi join NAME                   join (detects the security type, asks only if needed)
  audiolink wifi scan|status|list|forget     the Pi's Wi-Fi, through the running host
  audiolink wifi portal                      sign the Pi in to a sign-in page, from a browser here
  audiolink wifi check                       exit 0 = Pi online, 3 = sign-in page, 4 = no internet
  audiolink wifi ap [on|off|auto]            the Pi's own setup network (normally automatic)
  audiolink audio [status|sweep|set F|forget] the link's audio format (faster formats = more throughput)
  audiolink discovery enable|disable|status  whether the plugged-in Pi publishes on the DHT
  audiolink update [PYZ] [--revert|--status] new code for the plugged-in Pi, over the link
  audiolink logs [FILE] [-n N] [--list]      the plugged-in Pi's logs, over the link
  audiolink shell [-- CMD]                   root shell on the plugged-in Pi (or run CMD there)
  audiolink debug on|off|status              the plugged-in Pi's debug switch (network root access)
  audiolink rotate-secret                    new code for the plugged-in Pi: other computers lose access

Any computer:
  audiolink add [CODE]                       add a Pi: plugged in here, on the network, or by code
  audiolink pairing code PI                  a one-time code for adding PI on another machine
  audiolink pis                              list the Pis this machine knows
  audiolink share|forget PI                  print a Pi's connect code / forget it
  audiolink rename PI NEW [--on-pi]          rename here (or on the Pi itself)
  audiolink ssh-config [--user NAME]         ~/.ssh/config entries: then just `ssh PI`
  audiolink find PI                          online or offline (its relay, this network), and where (DHT)
  audiolink connect PI PORT                  ssh ProxyCommand: find the Pi, connect
  audiolink wifi join NAME --pi PI           over the network (checks the network first when it can)
  audiolink wifi status|check --pi PI        over the network, the relay included
  audiolink logs ... --pi PI [--key FILE]    the Pi's logs over the network
  audiolink update ... --pi PI [--key FILE]  over the network (debug access)
  audiolink shell PI [--key FILE] [-- CMD]   root shell on PI over the network (debug access)
  audiolink dht-size                         estimate how many DHT nodes are out there

Elsewhere:
  audiolink pi --forward 2222:22             (the Pi, started at boot) expose the host's port 22 as Pi port 2222
  audiolink relay --listen HOST:PORT         (a server) the relay Pis and clients meet at

--forward LPORT:[HOST:]PORT  listen on LPORT here (the host: on 127.0.0.1 only); each connection
                             asks the peer to connect to HOST:PORT (HOST defaults to the peer's 127.0.0.1)
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

from . import __version__, argtypes
from .services.streams import Streams, parse_forward
from .paths import PI_STATE_DIR
from .relay import DEFAULT as RELAY_DEFAULT
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
    found = False
    for d in portaudio.devices():
        mark = "*" if any(h in d["name"].lower() for h in DEVICE_HINTS) else " "
        found = found or mark == "*"
        print("%s %3d  %-40s %-20s in=%d out=%d" % (mark, d["index"], d["name"], d["hostapi"],
                                                    d["inputs"], d["outputs"]))
    print("\n* = Pi audio device" if found else "\nNo Pi audio device found (plugged in?)")
    return 0                    # the listing worked; a missing Pi is shown, not an error


def build_parser():
    ap = argparse.ArgumentParser(prog="audiolink", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version="audiolink " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    sub.add_parser("list-devices", help="list audio devices")
    w = sub.add_parser("wifi", help="manage the Pi's Wi-Fi (needs a running `audiolink host`)",
                       formatter_class=argparse.RawDescriptionHelpFormatter, description=(
                           "The Pi's Wi-Fi, through `audiolink host` on this computer; join, status and check\n"
                           "also over the network (--pi PI). The Pi uses 2.4 GHz networks only.\n\n"
                           "  (none)        pick a network from a list\n"
                           "  scan          the networks the Pi can see\n"
                           "  status        which network it's on, its address, whether it's online\n"
                           "  list          the networks it has saved\n"
                           "  join NAME     join NAME (asks for a password only if it needs one)\n"
                           "  forget NAME   drop a saved network\n"
                           "  portal        sign the Pi in to a network's sign-in page, from a browser here\n"
                           "  check         exit status 0: online, 3: a sign-in page is in the way, 4: no internet\n"
                           "  ap on|off|auto  the Pi's setup network, audiopi-NAME (auto: whenever nobody\n"
                           "                has been connected to the Pi for a while)"))
    w.add_argument("action", nargs="?", choices=["scan", "status", "list", "join", "forget", "portal", "check", "ap"],
                   help="see above; omit to pick a network from a list")
    w.add_argument("name", nargs="?", help="network name for join/forget; on|off|auto for ap")
    w.add_argument("--security", choices=["open", "owe", "wpa2", "wpa3"],
                   help="join: skip detection (needed for hidden networks)")
    w.add_argument("--open", action="store_true", help="join: same as --security open")
    w.add_argument("--hidden", action="store_true", help="join: network doesn't broadcast its name")
    w.add_argument("--reenter", action="store_true", help="join: ask for the password even if saved")
    w.add_argument("--portal", action="store_true", help="join: open a captive portal login without asking")
    w.add_argument("--no-portal", action="store_true", help="join: never offer a captive portal login")
    w.add_argument("--password-stdin", action="store_true", help="join: read the password from stdin")
    w.add_argument("--pi", help="join, status, check over the network: which Pi (default: the only one you know)")
    w.add_argument("--via-network", action="store_true", help="over the network even if plugged in here")
    w.add_argument("--tries", type=argtypes.positive_int, metavar="N", help="join over the network: connection attempts before giving up (max 3)")
    w.add_argument("--at", action="append", metavar="ADDRESS", help="the Pi's address, if not found")
    dsc = sub.add_parser("discovery", help="whether the plugged-in Pi publishes on the DHT",
                         formatter_class=argparse.RawDescriptionHelpFormatter, epilog="""\
  status    is it published, and when (default)
  enable    publish its network and address on the DHT, for `audiolink find`/`connect`
  disable   stop publishing (the relay works either way)
  publish   publish again now
  code      print its connect code (as: audiolink share)""")
    dsc.add_argument("action", nargs="?", default="status",
                     choices=["enable", "disable", "code", "status", "publish"], help="see below")
    add = sub.add_parser("add", help="add a Pi: the one plugged in here, or by connect code")
    add.add_argument("code", nargs="?", help="a connect code (al1-...) someone shared")
    add.add_argument("--name", help="what to call it on this machine")
    add.add_argument("--no-lookup", action="store_true", help="with a code: don't check it on the DHT")
    add.add_argument("--at", action="append", metavar="ADDRESS", help="try a Pi at this address")
    add.add_argument("--pairing-code", metavar="CODE", help="the 6-digit code (instead of being asked)")
    prg = sub.add_parser("pairing", help="pairing codes for adding a Pi on another machine")
    prg.add_argument("action", choices=["code"], help="code: a one-time 6-digit code for `audiolink add` "
                                                        "on the other machine")
    prg.add_argument("pi", help="name (see: audiolink pis)")
    prg.add_argument("--at", action="append", metavar="ADDRESS", help="the Pi's address, if not found")
    sub.add_parser("pis", help="list the Pis this machine knows")
    for cmd, helptext in (("share", "print a Pi's connect code"), ("forget", "forget a Pi")):
        sub.add_parser(cmd, help=helptext).add_argument("name", help="name (see: audiolink pis) or connect code")
    sub.choices["forget"].add_argument("--yes", action="store_true", help="don't ask, even to drop an identity backup")
    ren = sub.add_parser("rename", help="rename a Pi on this machine (or on the Pi: --on-pi)")
    ren.add_argument("name", help="its name now (see: audiolink pis)")
    ren.add_argument("new", help="the new name: lowercase letters, digits and dashes")
    ren.add_argument("--on-pi", action="store_true", help="rename the Pi itself (it must be plugged in here)")
    sshc = sub.add_parser("ssh-config", help="print ~/.ssh/config entries for known Pis")
    sshc.add_argument("--user", help="login user on the computer the Pi is plugged into (default: yours here)")
    sshc.add_argument("--port", type=argtypes.port, default=2222,
                      help="the Pi's port for ssh when its exposed ports aren't known (default 2222)")
    fnd = sub.add_parser("find", help="is a Pi online (on its relay or this network), and where (the DHT)")
    fnd.add_argument("pi", help="name (see: audiolink pis) or connect code")
    con = sub.add_parser("connect", help="pipe stdin/stdout to a Pi port (ssh ProxyCommand)")
    con.add_argument("pi", help="name (see: audiolink pis) or connect code")
    con.add_argument("port", type=argtypes.port, help="the Pi's port (one its host exposes, e.g. 2222)")
    aud = sub.add_parser("audio", help="the link's audio format (needs a running `audiolink host`)",
                         formatter_class=argparse.RawDescriptionHelpFormatter, description=(
                             "The link's audio format: faster formats carry more data. The host picks the\n"
                             "fastest that works when the Pi connects.\n\n"
                             "  status      which format the link runs (default)\n"
                             "  sweep       measure every format and keep the fastest\n"
                             "  set F       switch to F: baseline, 192000x8, 96000x8, 192000x4, 96000x4,\n"
                             "              192000x2, 96000x2 or 48000x8 (rate x channels)\n"
                             "  forget      forget this Pi's measurements (measured again on next connect)"))
    aud.add_argument("action", nargs="?", default="status", choices=["status", "sweep", "set", "forget"],
                     help="see above")
    aud.add_argument("format", nargs="?", help="set: RATExCHANNELS (e.g. 96000x8) or baseline")
    upd = sub.add_parser("update", help="new code for the plugged-in Pi, over the link")
    upd.add_argument("pyz", nargs="?", help="the .pyz to send (default: this one)")
    upd.add_argument("--revert", action="store_true", help="back to the code in the Pi's image")
    upd.add_argument("--status", action="store_true", help="which code the Pi runs")
    lg = sub.add_parser("logs", help="the plugged-in Pi's logs (works when USB networking doesn't)")
    lg.add_argument("file", nargs="?", help="which log (default audiolink.log; --list shows them)")
    lg.add_argument("-n", "--lines", type=argtypes.positive_int, default=200,
                    help="how many of the last lines (default 200, max 5000)")
    lg.add_argument("--list", action="store_true", help="list the Pi's logs")
    sh = sub.add_parser("shell", help="a root shell on the Pi (plugged in here, or PI over the network)",
                        usage="audiolink shell [-h] [PI] [--key FILE] [--at ADDRESS] [-v] [-- CMD ...]",
                        formatter_class=argparse.RawDescriptionHelpFormatter, description=(
                            "A root shell on the Pi plugged in here, or on PI over the network (that needs the\n"
                            "Pi's debug switch on: audiolink debug on). Exits with the shell's exit status.\n\n"
                            "  -- CMD ...   run CMD on the Pi with sh -c instead (no terminal; stdin, stdout and\n"
                            "               stderr pass through), e.g. audiolink shell -- 'df -h; uptime'"))
    sh.add_argument("pi", nargs="?", help="over the network: a name, connect code or address")
    for p in (upd, lg, sh):
        if p is not sh:
            p.add_argument("--pi", help="over the network: which Pi (name, connect code or address)")
        p.add_argument("--at", action="append", metavar="ADDRESS", help="the Pi's address, if not found")
    for p in (upd, lg, sh):
        p.add_argument("--key", metavar="FILE", help="debug key (OpenSSH ed25519 private key, or .pub with ssh-agent)")
    dbg = sub.add_parser("debug", help="the plugged-in Pi's debug switch: root access over the network",
                         formatter_class=argparse.RawDescriptionHelpFormatter, description=(
                             "The plugged-in Pi's debug switch: root access over the network (`audiolink shell PI`,\n"
                             "`audiolink update --pi PI`). Only the computer the Pi is plugged into changes it.\n\n"
                             "  status    is it on, for whom, until when (default)\n"
                             "  on        turn it on (24 h; --auth none: 1 h)\n"
                             "  off       turn it off and close open network shells"))
    dbg.add_argument("action", nargs="?", default="status", choices=["on", "off", "status"], help="see above")
    dbg.add_argument("--auth", choices=["paired", "key", "none"],
                     help="on: who gets in (default paired: computers that added the Pi)")
    dbg.add_argument("--key", action="append", metavar="FILE", help="on: an ed25519 public key file (repeatable)")
    dbg.add_argument("--setup", action="store_true", help="on, --auth none: also on the setup network (open Wi-Fi)")
    dbg.add_argument("--for", dest="for_", metavar="DURATION",
                     help="on: how long (30m, 2h, 1d; default 24h, --auth none 1h)")
    dbg.add_argument("--persist", action="store_true", help="on: keep it across the Pi's restarts")
    rot = sub.add_parser("rotate-secret", help="new code for the plugged-in Pi: other computers lose access")
    rot.add_argument("--yes", action="store_true", help="don't ask")
    rly = sub.add_parser("relay", help="run a relay that Pis and clients meet at (on a server)")
    rly.add_argument("--listen", metavar="HOST:PORT",
                     help="where to listen, e.g. 0.0.0.0:443 (every interface) or [::]:443; required")
    rly.add_argument("--cert", metavar="FILE", default="keys/relay.crt", help="its TLS certificate (default: %(default)s)")
    rly.add_argument("--key", metavar="FILE", default="keys/relay.key", help="the certificate's key (default: %(default)s)")
    rly.add_argument("--status", action="store_true",
                     help="ask the relay running here for its counts (port from --listen, default 443)")
    size = sub.add_parser("dht-size", help="estimate the size of the BitTorrent DHT")
    size.add_argument("--samples", type=argtypes.positive_int, default=8, metavar="N",
                      help="how many random lookups to average (default 8)")
    roles = {"host": sub.add_parser("host", help="run on the Mac/PC the Pi is plugged into", description=(
                 "Run on the computer the Pi is plugged into: it carries the Pi's connections to\n"
                 "this computer, and other audiolink commands (wifi, audio, update, ...) go\n"
                 "through it. E.g. `audiolink host --expose 2222:22`: ssh to the Pi's port 2222\n"
                 "reaches this computer's ssh."), formatter_class=argparse.RawDescriptionHelpFormatter),
             "pi": sub.add_parser("pi", help="run on the Pi", description=(
                 "The Pi's side (started at boot by start-audiolink.sh; arguments in "
                 "/etc/audiolink.args, then from the card's settings.txt: --relay, --expose-setup)."))}
    for name, p in roles.items():
        peer = "the Pi" if name == "host" else "the host"
        p.add_argument("--forward", action="append", default=[], metavar="LPORT:[HOST:]PORT", type=argtypes.forward,
                       help="listen on LPORT here%s; each connection goes to HOST:PORT as seen from %s "
                            "(HOST default: %s itself)" % (" (this computer only)" if name == "host" else "",
                                                           peer, peer))
        p.add_argument("--allow", action="append", default=[], metavar="[HOST:]PORT", type=argtypes.allow,
                       help="let %s open connections to HOST:PORT from here (HOST default: 127.0.0.1); "
                            "anything else is refused" % peer)
        p.add_argument("--stats", type=float, default=0, metavar="SECONDS", help="log link stats this often")
        p.add_argument("-v", "--verbose", action="store_true",
                       help="show technical details too (streams, link restarts, audio measurements)")
    roles["host"].add_argument("--expose", action="append", default=[], metavar="PIPORT:[HOST:]PORT",
                               type=argtypes.expose,
                               help="the Pi listens on PIPORT; connections reach HOST:PORT from here "
                                    "(HOST default: this computer). E.g. 2222:22")
    roles["host"].add_argument("--proxy-port", type=argtypes.port_or_off, default=47801, metavar="PORT",
                               help="an HTTP proxy at 127.0.0.1:PORT that browses as the Pi "
                                    "(default 47801; 0 = off)")
    roles["host"].add_argument("--shell-port", type=argtypes.port_or_off, default=47802, metavar="PORT",
                               help="local port for `audiolink shell` (default 47802; 0 = off)")
    roles["host"].add_argument("--wifi", metavar="NAME",
                               help="first put the Pi on this Wi-Fi network (prompts if needed)")
    roles["host"].add_argument("--audio-format", default="auto", metavar="auto|baseline|RATExCH",
                               type=argtypes.audio_format,
                               help="negotiate a faster audio format with the Pi (default: auto)")
    roles["host"].add_argument("--out-device", metavar="NAME",
                               help="the Pi's output sound device, if not found (part of its name: "
                                    "audiolink list-devices)")
    for p in (roles["host"], w, dsc, add, ren, prg, aud, upd, lg, sh, dbg, sub.choices["rotate-secret"]):
        p.add_argument("--control-port", type=argtypes.port, default=None, metavar="PORT",
                       help="the port other audiolink commands reach this on (default 47800)" if p is roles["host"]
                       else "audiolink host's control port (default 47800), if it runs with another")
    roles["host"].add_argument("--in-device", metavar="NAME", help="likewise, its input sound device")
    roles["pi"].add_argument("--device", default="hw:0,0", help="ALSA device")
    roles["pi"].add_argument("--state-dir", default=PI_STATE_DIR, help="where the Pi keeps its identity")
    roles["pi"].add_argument("--wifi-store", default=PI_STATE_DIR + "/networks.json",
                             help="where saved Wi-Fi networks are kept")
    roles["pi"].add_argument("--relay", default=RELAY_DEFAULT, metavar="HOST:PORT[#PIN]",
                             help="relay to stay reachable through from anywhere (off: none)")
    roles["pi"].add_argument("--expose-ports", default="1024-65535", metavar="LO-HI",
                             help="ports the host may ask the Pi to expose")
    roles["pi"].add_argument("--expose-setup", action="store_true",
                             help="exposed and forwarded ports take connections from the setup network too")
    for p in sub.choices.values():       # every command: -v shows the technical details
        if not any("--verbose" in act.option_strings for act in p._actions):
            p.add_argument("-v", "--verbose", action="store_true", help="show technical details too")
    for name, p in sub.choices.items():     # `audiolink X --help` starts with what X does
        if not p.description:
            text = next(c.help for c in sub._choices_actions if c.dest == name)
            p.description = text[:1].upper() + text[1:] + "."
    return ap


def main(argv=None):
    ap = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    command = None
    first = next((x for x in argv if not x.startswith("-")), None)
    if first == "shell" and "--" in argv:                         # audiolink shell [PI] -- CMD ...
        cut = argv.index("--")
        argv, command = argv[:cut], argv[cut + 1:]
    a = ap.parse_args(argv)
    if a.cmd == "shell":
        a.command = command
    from . import ui
    ui.VERBOSE = bool(getattr(a, "verbose", False))

    if a.cmd == "list-devices":
        return list_devices()
    if a.cmd == "pairing":
        from .client.pairing import code_command
        return code_command(a)
    if a.cmd == "shell":
        from .client.shell import shell_command
        return shell_command(a)
    if a.cmd in ("add", "pis", "share", "forget", "rename", "ssh-config", "rotate-secret"):
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
        remote = wifi_remote.over_network(a)
        if a.action in ("status", "check") and remote:
            return wifi_remote.status_command(a)
        if a.action == "join" and (remote or not pi_on_usb(port)):
            return wifi_remote.change_command(a)
        return wifi_command(a)
    if a.cmd == "audio":
        from .host.audio_format import audio_command
        a.control_port = port
        return audio_command(a)
    if a.cmd == "update":
        from .host.update_cli import update_command
        a.control_port = port
        return update_command(a)
    if a.cmd == "logs":
        from .host.logs_cli import logs_command
        a.control_port = port
        return logs_command(a)
    if a.cmd == "debug":
        from .host.debug_cli import debug_command
        a.control_port = port
        return debug_command(a)
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
    (log.info if a.cmd == "pi" else log.debug)(      # tools/hot.sh waits for the Pi's
        "audiolink %s %s up (session %08x); allow=%s%s", __version__, a.cmd, session.link.session,
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
    from .services.shell import PORT as SHELL_PORT, NetworkShells, ShellServer
    streams = Streams("pi", allow=allow + ["%s:%d" % proxy.target, "127.0.0.1:%d" % SHELL_PORT],
                      expose_ports=a.expose_ports, setup=a.expose_setup)
    identity = IdentityService(a.state_dir)
    from .services.access import RUN_DIR, DebugService
    debug = DebugService(a.state_dir, run_dir=RUN_DIR)
    endpoint = Endpoint(identity, presence=presence, debug=debug)
    setup_ap = SetupApService(wifi, identity, presence, radio)
    discovery = DiscoveryService(identity, wifi, streams)
    from .services.button import ResetPins
    from .services.logs import LogsService
    from .services.update import UpdateService
    from .services.relay import RelayAgent
    relay = RelayAgent(identity, wifi, streams, [] if a.relay == "off" else [a.relay])
    update = UpdateService(endpoint=endpoint)
    session = Session("pi", [streams, wifi, proxy, identity, discovery, PairingService(identity, endpoint),
                             setup_ap, WifiRemoteService(wifi, setup_ap, endpoint, discovery, radio), relay,
                             LogsService(endpoint=endpoint), update, debug, NetworkShells(endpoint, debug, identity)])
    audio = AlsaAudio(session.link, a.device)
    audio.start()
    formats = AudioFormatService(audio, Gadget())
    session.add(formats)
    from .services.usbnet import UsbNetWatch
    UsbNetWatch(formats.represent).run()
    for spec in a.forward:
        streams.forward(*parse_forward(spec))
    setup_ap.run()
    relay.run()
    debug.run()
    try:
        ShellServer().run()
    except OSError as e:
        log.warning("shell: can't listen (%s)", e)

    def revert():
        debug.off("reset pins")
        update.handle("revert", {"restart": True})     # the pins always restart

    def start_over():
        identity.start_over()
        wifi.forget_all()
        revert()
    ResetPins(revert, start_over).run()
    return session, streams


def run_host(a, allow, port, gate):
    """The host: streams (+ --expose), the control port, portal handling, identity custody,
    audio-format negotiation; PortAudio."""
    from .host import control
    from .host.audio_format import AudioNegotiator, parse_format
    from .host.console import HostConsole
    from .host.identity import HostIdentity
    from .host.portal import PortalWatcher
    from .host.wifi_cli import startup_join
    from .services.proxy import PROXY_PORT
    from .services.shell import PORT as SHELL_PORT
    from .wire.host_audio import run_portaudio
    # Every port before the Pi's audio device: a second host, or a port in use, must stop
    # here, not after it has opened the device (two hosts on it corrupt the link).
    ctl = control.listen(port)
    startup_wifi = bool(a.wifi)
    if startup_wifi:
        gate.pause()             # the Wi-Fi prompts own the terminal until startup is done
    expose = [parse_forward(x) for x in a.expose]
    streams = Streams("host", allow=allow, expose=expose)
    watcher = PortalWatcher(port, gate)
    negotiator = AudioNegotiator(a.audio_format if a.audio_format in ("auto", "baseline")
                                 else parse_format(a.audio_format))
    session = Session("host", [streams, watcher, HostIdentity(), negotiator,
                               HostConsole(expose, busy=lambda: negotiator.running)])

    def listen(lport, target, what, flag=None):
        try:
            streams.forward(lport, target, bind="127.0.0.1")
        except OSError as e:
            log.debug("listen on %d: %s", lport, e)
            raise SystemExit("Port %d (%s) is in use; pick another%s" % (
                lport, what, ": %s PORT, or turn it off: %s 0" % (flag, flag) if flag else ""))
    for spec in a.forward:
        listen(*parse_forward(spec), "--forward " + spec)
    if a.proxy_port:
        # Browse as the Pi (captive portals): local HTTP proxy -> Pi's proxy.
        listen(a.proxy_port, ("127.0.0.1", PROXY_PORT), "the browse-as-the-Pi proxy", "--proxy-port")
    if a.shell_port:
        # `audiolink shell`: the Pi's root shell, for anyone on this machine (USB = root).
        listen(a.shell_port, ("127.0.0.1", SHELL_PORT), "for audiolink shell", "--shell-port")
    if not a.verbose:
        from .wire.portaudio import quiet_stdout
        quiet_stdout()
    run_portaudio(session.link, a.out_device, a.in_device, expected=lambda: negotiator.running)
    local_ops = {"peer": lambda args: {"connected": session.peer_alive, "peer": session.peer_info,
                                       "proxy_port": a.proxy_port, "shell_port": a.shell_port}}
    local_ops.update(watcher.local_ops())
    local_ops.update(negotiator.local_ops())
    control.serve(session, ctl, local_ops)
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
