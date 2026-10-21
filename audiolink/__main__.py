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
  audiolink update [PYZ] [--revert|--status] new code for the plugged-in Pi, over the link
  audiolink logs [FILE] [-n N] [--list]      the plugged-in Pi's logs, over the link
  audiolink shell [-- CMD]                   root shell on the plugged-in Pi (or run CMD there)

Start here:
  audiolink setup                            set up this computer (and the Pi, if it's plugged in)
  audiolink setup host                       just this computer's host part (exposed ports, autostart)
  audiolink card write [IMAGE] [--wipe]      audiolink onto the Pi's card, keeping its state (macOS)
  audiolink card backup [FILE]               the Pi's whole card to a zip
  audiolink card restore FILE                erase the card and put a backup on it (= write FILE --wipe)
  audiolink settings [KEY=VALUE ...]         the plugged-in Pi's card settings (--pi PI: over the network)

Any computer:
  audiolink pis [PI]                         the Pis this computer knows (PI: online?, its list)
  audiolink invite NAME [--owner]            a one-use code that lets NAME through (or own) your Pi
  audiolink remove NAME                      take NAME off every Pi this computer owns
  audiolink forget PI                        drop a Pi here, and this computer from its list
  audiolink rename PI NEW [--on-pi]          rename here (or on the Pi itself)
  audiolink ssh-config                       ~/.ssh/config entries: then `ssh PI` (or user@PI)
  audiolink connect PI PORT                  ssh ProxyCommand: find the Pi, connect
  audiolink forward PI PORT [--local LPORT]  127.0.0.1:LPORT here -> PI's port (ssh without a ProxyCommand)
  audiolink wifi join NAME --pi PI           over the network (checks the network first when it can)
  audiolink wifi status|check --pi PI        over the network, the relay included
  audiolink logs ... --pi PI                 the Pi's logs over the network (owners)
  audiolink update ... --pi PI               over the network (owners)
  audiolink shell PI [-- CMD]                root shell on PI over the network (owners)

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

from . import __version__, argtypes, version_text
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
    from .wire import portaudio, usb_audio
    devs = usb_audio.annotate(portaudio.devices())
    pis = usb_audio.pi_devices(devs)
    found = False
    for d in devs:
        mark = "*" if d in pis else " "
        found = found or mark == "*"
        usb = "  usb %04x:%04x" % d["usb"] if d.get("usb") else ""
        print("%s %3d  %-40s %-20s in=%d out=%d%s" % (mark, d["index"], d["name"], d["hostapi"],
                                                      d["inputs"], d["outputs"], usb))
    print("\n* = Pi audio device" if found else "\nNo Pi audio device found (plugged in? A Pi with its own "
          "USB ID: audiolink host --usb-id VID:PID)")
    return 0                    # the listing worked; a missing Pi is shown, not an error


def build_parser():
    ap = argparse.ArgumentParser(prog="audiolink", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version="audiolink " + version_text())
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    stp = sub.add_parser("setup", help="start here: set up this computer (and the Pi, if it's plugged in)",
                         formatter_class=argparse.RawDescriptionHelpFormatter, description=(
                             "Set up this computer, and the Pi if it's plugged in here.\n"
                             "The Pi plugged in here: adds it, puts it on Wi-Fi, and offers to make this\n"
                             "computer its host (its ssh reachable through the Pi; --autostart: audiolink host\n"
                             "started at login) and register it as a client (ssh NAME). Not plugged in here: a client: finds the Pi\n"
                             "on this network (or takes its connect code) and sets up ssh NAME.\n"
                             "Run it again any time: it shows what's set. Each question has a flag below."))
    stp.add_argument("part", nargs="?", choices=["host"],
                     help="host: only this computer's host part (what it exposes, starting at login)")
    stp.add_argument("--yes", action="store_true", help="take the suggested answer to every question")
    hst = stp.add_mutually_exclusive_group()
    hst.add_argument("--host", action="store_true", default=None,
                     help="the Pi plugged in here: make this computer its host (don't ask)")
    hst.add_argument("--no-host", dest="host", action="store_false",
                     help="the Pi plugged in here: don't make this computer its host")
    cl = stp.add_mutually_exclusive_group()
    cl.add_argument("--client", action="store_true", default=None,
                    help="the Pi plugged in here: also set up ssh NAME on this computer (don't ask)")
    cl.add_argument("--no-client", dest="client", action="store_false",
                    help="don't set up ssh NAME on this computer (~/.ssh/config)")
    stp.add_argument("--name", help="the Pi's name (renames it when it's plugged in here)")
    wf = stp.add_mutually_exclusive_group()
    wf.add_argument("--wifi", metavar="NETWORK", help="put the Pi on this Wi-Fi network")
    wf.add_argument("--no-wifi", action="store_true", help="leave the Pi's Wi-Fi as it is")
    stp.add_argument("--password-stdin", action="store_true", help="--wifi: read the password from stdin")
    ex = stp.add_mutually_exclusive_group()
    ex.add_argument("--expose", action="append", metavar="PIPORT:[HOST:]PORT", type=argtypes.expose,
                    help="what the Pi exposes for this computer (repeatable; replaces the saved list), "
                         "e.g. 2222:22 for its ssh")
    ex.add_argument("--no-expose", action="store_true", help="expose nothing")
    au = stp.add_mutually_exclusive_group()
    au.add_argument("--autostart", action="store_true", default=None,
                    help="start audiolink host at login, and keep it running")
    au.add_argument("--no-autostart", dest="autostart", action="store_false",
                    help="don't start audiolink host at login (removes it if set up)")
    stp.add_argument("--code", metavar="CODE", help="the Pi's code (al2-...), if it isn't on this network: "
                     "join through its relay")
    stp.add_argument("--invite", metavar="CODE", help="the 6-digit invite code (from audiolink invite on an owner "
                     "computer)")
    stp.add_argument("--at", action="append", metavar="ADDRESS", help="the Pi's address, if not found")
    stp.add_argument("--pi", help="which Pi this computer already knows to set up ssh for (see: audiolink pis)")
    stp.add_argument("--card-image", metavar="ZIP",
                     help="the Pi has no card set up: write this image (default: the release's for this version)")
    stp.add_argument("--no-install", action="store_true",
                     help="use audiolink where it is (don't copy it to a permanent place)")
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
    pis = sub.add_parser("pis", help="the Pis this computer knows (with PI: online?, its code, its list)")
    pis.add_argument("pi", nargs="?", help="one Pi: is it online, where it was last seen, its list (owners)")
    pis.add_argument("--at", action="append", metavar="ADDRESS", help="the Pi's address, if not found")
    inv = sub.add_parser("invite", help="a one-use code that puts NAME on your Pi's list")
    inv.add_argument("name", help="what to call the computer on the list (lowercase, digits, dashes)")
    inv.add_argument("--owner", action="store_true", help="NAME will own the Pi too (manage it, invite others)")
    rmv = sub.add_parser("remove", help="take NAME off every Pi this computer owns")
    rmv.add_argument("name", help="the computer's name on the list (see: audiolink pis PI)")
    for p in (inv, rmv):
        p.add_argument("--pi", help="which Pi (default: the one this computer owns)")
        p.add_argument("--at", action="append", metavar="ADDRESS", help="the Pi's address, if not found")
    fgt = sub.add_parser("forget", help="drop a Pi here, and take this computer off its list")
    fgt.add_argument("name", help="name (see: audiolink pis) or code")
    ren = sub.add_parser("rename", help="rename a Pi on this machine (or on the Pi: --on-pi)")
    ren.add_argument("name", help="its name now (see: audiolink pis)")
    ren.add_argument("new", help="the new name: lowercase letters, digits and dashes")
    ren.add_argument("--on-pi", action="store_true", help="rename the Pi itself (it must be plugged in here)")
    sshc = sub.add_parser("ssh-config", help="print ~/.ssh/config entries for known Pis")
    sshc.add_argument("--port", type=argtypes.port, default=2222,
                      help="the Pi's port for ssh when its exposed ports aren't known (default 2222)")
    con = sub.add_parser("connect", help="pipe stdin/stdout to a Pi port (ssh ProxyCommand)")
    con.add_argument("pi", help="name (see: audiolink pis) or code")
    con.add_argument("port", type=argtypes.port, help="the Pi's port (one its host exposes, e.g. 2222)")
    fwd = sub.add_parser("forward", help="listen on a port here; each connection goes to a Pi's port",
                         formatter_class=argparse.RawDescriptionHelpFormatter, description=(
                             "Listen on 127.0.0.1:LPORT (this computer only); each connection goes to PI's\n"
                             "port PORT, like `audiolink connect`. For ssh clients that can't run a\n"
                             "ProxyCommand (SSHFS-Win, ...): ssh -p LPORT USER@127.0.0.1, with the\n"
                             "HostKeyAlias it prints. Runs until Ctrl-C; a restart of the Pi's host only\n"
                             "ends the connections open then."))
    fwd.add_argument("pi", help="name (see: audiolink pis) or code")
    fwd.add_argument("port", type=argtypes.port, help="the Pi's port (one its host exposes, e.g. 2222)")
    fwd.add_argument("--local", type=argtypes.port, metavar="LPORT", help="the port here (default: PORT)")
    fwd.add_argument("--at", action="append", metavar="ADDRESS", help="the Pi's address, if not found")
    crd = sub.add_parser("card", help="write, back up or restore the Pi's card (macOS)",
                         formatter_class=argparse.RawDescriptionHelpFormatter, description=(
                             "The Pi's card, in a card reader or opened through the Pi (rpiboot). No questions:\n"
                             "a card it shouldn't touch stops it, with the flag to give.\n\n"
                             "  write [IMAGE]   audiolink onto the card: this version's release image, or IMAGE\n"
                             "                  (a zip, or a build folder like build/card-pi). An audiolink card\n"
                             "                  keeps the Pi's state (its keys, Wi-Fi, settings, logs); a blank one\n"
                             "                  is written; anything else needs --wipe\n"
                             "  backup [FILE]   the Pi as it runs to a zip: its keys, Wi-Fi, settings and logs, and\n"
                             "                  the image it runs (--all-images: every one); by default\n"
                             "                  audiolink-card-DATE.zip here (it holds the Pi's keys: keep it private)\n"
                             "  restore FILE    erase the card and put FILE on it: write FILE --wipe"))
    crd.add_argument("action", choices=["write", "backup", "restore"], help="see above")
    crd.add_argument("file", nargs="?", help="the image or backup (a zip, or a folder)")
    crd.add_argument("--wipe", action="store_true", help="write: erase the card first, keeping nothing")
    crd.add_argument("--keep-update", action="store_true",
                     help="write: keep an update on the card (audiolink update), which the Pi runs instead")
    crd.add_argument("--disk", help="the card's disk, e.g. /dev/disk7 (when there's more than one, or it's blank)")
    crd.add_argument("--eject", action="store_true", help="backup: eject the card when done (write and restore do)")
    crd.add_argument("--all-images", action="store_true",
                     help="backup: every image on the card, not just the one it runs (a Pi with several)")
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
    upd = sub.add_parser("update", help="new code (or a whole image) for the plugged-in Pi, over the link")
    upd.add_argument("pyz", nargs="?", help="the .pyz to send (default: this one)")
    upd.add_argument("--image", nargs="?", const="release", metavar="IMAGE",
                     help="a whole new image instead: this version's release image, or IMAGE (a zip, or a "
                          "build folder like build/card-pi); tried once, kept if it comes back online")
    upd.add_argument("--revert", action="store_true",
                     help="back to the code in the Pi's image (with --image: back to its other image)")
    upd.add_argument("--status", action="store_true", help="which code (and image) the Pi runs")
    lg = sub.add_parser("logs", help="the plugged-in Pi's logs (works when USB networking doesn't)")
    lg.add_argument("file", nargs="?", help="which log (default audiolink.log; --list shows them)")
    lg.add_argument("-n", "--lines", type=argtypes.positive_int, default=200,
                    help="how many of the last lines (default 200, max 5000)")
    lg.add_argument("--list", action="store_true", help="list the Pi's logs")
    sh = sub.add_parser("shell", help="a root shell on the Pi (plugged in here, or PI over the network)",
                        usage="audiolink shell [-h] [PI] [--at ADDRESS] [-v] [-- CMD ...]",
                        formatter_class=argparse.RawDescriptionHelpFormatter, description=(
                            "A root shell on the Pi plugged in here, or on PI over the network (owners of the Pi).\n"
                            "Exits with the shell's exit status.\n\n"
                            "  -- CMD ...   run CMD on the Pi with sh -c instead (no terminal; stdin, stdout and\n"
                            "               stderr pass through), e.g. audiolink shell -- 'df -h; uptime'"))
    sh.add_argument("pi", nargs="?", help="over the network: a name, code or address")
    stg = sub.add_parser("settings", help="the Pi's card settings (hostname, relay, USB names...), over the link",
                         formatter_class=argparse.RawDescriptionHelpFormatter, description=(
                             "The Pi's card settings (README: The card's settings), through the running Pi:\n"
                             "  audiolink settings                     list them: their values, set or built in\n"
                             "  audiolink settings hostname=kitchen    set some (checked as the Pi checks them)\n"
                             "  audiolink settings --unset hostname    back to the built-in value\n"
                             "They take effect when the Pi next starts (--reboot: now). Owners only."))
    stg.add_argument("items", nargs="*", metavar="KEY=VALUE", help="settings to change (none: list them)")
    stg.add_argument("--unset", action="append", metavar="KEY", help="back to its built-in value (repeatable)")
    stg.add_argument("--reboot", action="store_true", help="restart the Pi so the changes take effect now")
    crd.add_argument("--pi", help="backup: over the network, from this Pi (name, code or address)")
    for p in (upd, lg, sh, stg, crd):
        if p is not sh and p is not crd:
            p.add_argument("--pi", help="over the network: which Pi (name, code or address)")
        p.add_argument("--at", action="append", metavar="ADDRESS", help="the Pi's address, if not found")
    rly = sub.add_parser("relay", help="run a relay that Pis and clients meet at (on a server)")
    rly.add_argument("--listen", metavar="HOST:PORT",
                     help="where to listen, e.g. 0.0.0.0:443 (every interface) or [::]:443; required")
    rly.add_argument("--cert", metavar="FILE", default="keys/relay.crt", help="its TLS certificate (default: %(default)s)")
    rly.add_argument("--key", metavar="FILE", default="keys/relay.key", help="the certificate's key (default: %(default)s)")
    rly.add_argument("--status", action="store_true",
                     help="ask the relay running here for its counts (port from --listen, default 443)")
    roles = {"host": sub.add_parser("host", help="run on the Mac/PC the Pi is plugged into", description=(
                 "Run on the computer the Pi is plugged into: it carries the Pi's connections to\n"
                 "this computer, and other audiolink commands (wifi, audio, update, ...) go\n"
                 "through it. E.g. `audiolink host --expose 2222:22`: ssh to the Pi's port 2222\n"
                 "reaches this computer's ssh.\n\n"
                 "Settings saved by `audiolink setup host` apply too; a flag given here wins over\n"
                 "its saved value (--expose, --forward, --allow: replace the saved list)."), formatter_class=argparse.RawDescriptionHelpFormatter),
             "pi": sub.add_parser("pi", help="run on the Pi", description=(
                 "The Pi's side (started at boot by start-audiolink.sh; arguments in "
                 "/etc/audiolink.args, then from the card's settings.txt: --relay, --expose-setup)."))}
    for name, p in roles.items():
        peer = "the Pi" if name == "host" else "the host"
        dflt = None if name == "host" else []       # host: None = the saved setting (host/config.py)
        p.add_argument("--forward", action="append", default=dflt, metavar="LPORT:[HOST:]PORT", type=argtypes.forward,
                       help=("listen on LPORT here (this computer only); each connection goes to HOST:PORT as "
                             "seen from the Pi (HOST default: the Pi itself)") if name == "host" else
                            ("computers on the Pi's list can open port LPORT (audiolink connect); each "
                             "connection goes to HOST:PORT as seen from the host (HOST default: the host itself)"))
        p.add_argument("--allow", action="append", default=dflt, metavar="[HOST:]PORT", type=argtypes.allow,
                       help="let %s open connections to HOST:PORT from here (HOST default: 127.0.0.1); "
                            "anything else is refused" % peer)
        p.add_argument("--stats", type=float, default=0, metavar="SECONDS", help="log link stats this often")
        p.add_argument("-v", "--verbose", action="store_true",
                       help="show technical details too (streams, link restarts, audio measurements)")
    roles["host"].add_argument("--expose", action="append", metavar="PIPORT:[HOST:]PORT",
                               type=argtypes.expose,
                               help="the Pi listens on PIPORT; connections reach HOST:PORT from here "
                                    "(HOST default: this computer). E.g. 2222:22")
    roles["host"].add_argument("--proxy-port", type=argtypes.port_or_off, metavar="PORT",
                               help="an HTTP proxy at 127.0.0.1:PORT that browses as the Pi "
                                    "(default 47801; 0 = off)")
    roles["host"].add_argument("--shell-port", type=argtypes.port_or_off, metavar="PORT",
                               help="local port for `audiolink shell` (default 47802; 0 = off)")
    roles["host"].add_argument("--wifi", metavar="NAME",
                               help="first put the Pi on this Wi-Fi network (prompts if needed)")
    roles["host"].add_argument("--audio-format", metavar="auto|baseline|RATExCH",
                               type=argtypes.audio_format,
                               help="negotiate a faster audio format with the Pi (default: auto)")
    roles["host"].add_argument("--out-device", metavar="NAME",
                               help="the Pi's output sound device, if not found (part of its name: "
                                    "audiolink list-devices)")
    for p in (roles["host"], w, ren, aud, upd, lg, sh, stp, stg, crd):
        p.add_argument("--control-port", type=argtypes.port, default=None, metavar="PORT",
                       help="the port other audiolink commands reach this on (default 47800)" if p is roles["host"]
                       else "audiolink host's control port (default 47800), if it runs with another")
    roles["host"].add_argument("--in-device", metavar="NAME", help="likewise, its input sound device")
    roles["host"].add_argument("--usb-id", dest="usb_ids", action="append", type=argtypes.usb_id, metavar="VID:PID",
                               help="a Pi with these USB IDs too (repeatable; replaces the saved usb_ids), "
                                    "e.g. one from a fork: see audiolink list-devices"),
    roles["pi"].add_argument("--device", default="hw:0,0", help="ALSA device")
    roles["pi"].add_argument("--state-dir", default=PI_STATE_DIR, help="where the Pi keeps its identity")
    roles["pi"].add_argument("--wifi-store", default=PI_STATE_DIR + "/networks.json",
                             help="where saved Wi-Fi networks are kept")
    roles["pi"].add_argument("--relay", default=RELAY_DEFAULT, metavar="HOST:PORT[#PIN]",
                             help="relay to stay reachable through from anywhere (off: none)")
    roles["pi"].add_argument("--expose-ports", default="1024-65535", metavar="LO-HI",
                             help="ports the host may ask the Pi to expose")
    roles["pi"].add_argument("--expose-setup", action="store_true",
                             help="ignored: exposed ports are reached through the endpoint, with a key, on "
                                  "every network (older card settings may still pass it)")
    for p in sub.choices.values():       # every command: -v shows the technical details
        if not any("--verbose" in act.option_strings for act in p._actions):
            p.add_argument("-v", "--verbose", action="store_true", help="show technical details too")
    for name, p in sub.choices.items():     # `audiolink X --help` starts with what X does
        if not p.description:
            text = next(c.help for c in sub._choices_actions if c.dest == name)
            p.description = text[:1].upper() + text[1:] + "."
    return ap


def main(argv=None):
    from . import ui
    ui.safe_output()                    # before argparse, whose help and errors print too
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
    ui.VERBOSE = bool(getattr(a, "verbose", False))
    from .host import config as host_config
    if a.cmd == "host":
        host_config.apply(a)
    elif getattr(a, "control_port", "-") is None:
        a.control_port = host_config.control_port()

    if a.cmd == "list-devices":
        return list_devices()
    if a.cmd == "setup":
        from .guide import setup_command
        return setup_command(a)
    if a.cmd == "card":
        if a.action == "restore" and not a.file:
            raise SystemExit("Restore what? audiolink card restore FILE")
        from .card_cli import card_command
        return card_command(a)
    if a.cmd == "shell":
        from .client.shell import shell_command
        return shell_command(a)
    if a.cmd in ("pis", "invite", "remove", "forget", "rename", "ssh-config"):
        from .client import pis
        return getattr(pis, a.cmd.replace("-", "_") + "_command")(a)
    if a.cmd == "relay":
        from .relay.server import relay_command
        return relay_command(a)
    if a.cmd == "connect":
        from .client.find import connect_command
        return connect_command(a)
    if a.cmd == "forward":
        from .client.forward import forward_command
        return forward_command(a)
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
    if a.cmd == "settings":
        from .host.settings_cli import settings_command
        return settings_command(a)
    if a.cmd == "update":
        from .host.update_cli import update_command
        a.control_port = port
        return update_command(a)
    if a.cmd == "logs":
        from .host.logs_cli import logs_command
        a.control_port = port
        return logs_command(a)

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
    """The Pi: identity and its list, Wi-Fi management, the browse-as-the-Pi proxy, the
    endpoint (streams, invites, remote management), the relay; ALSA audio and its format."""
    from .services.access import LinkAccess
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
    from .services.flavor import FlavorService, usb_ids
    from .session import CLOCK_SANE
    from .wire.alsa import AlsaAudio
    from .wire.gadget import Gadget
    radio = Radio()                     # the one owner of the Wi-Fi chip
    wifi = WifiService(a.wifi_store, radio)
    proxy = ProxyService(on_request=wifi.note_demand)
    presence = Presence(wifi.ip)
    from .services.shell import PORT as SHELL_PORT, NetworkShells, ShellServer
    identity = IdentityService(a.state_dir)
    link = LinkAccess(identity)
    own = ["%s:%d" % proxy.target, "127.0.0.1:%d" % SHELL_PORT]        # the host's, with an owner's key
    streams = Streams("pi", allow=allow + own, expose_ports=a.expose_ports, owner_only=own, may_open=link.may_open)

    def record():
        """Where the Pi is now, for a computer that proved its key (and one that just paired)."""
        conn, now = wifi.connection(), session.now()
        return {"name": identity.pi_name(), "ts": int(now) if now > CLOCK_SANE else None,
                "lan": [conn["ip"]] if conn["ip"] else [], "ssid": conn["ssid"], "ports": streams.ports(),
                "usb": usb_ids()}           # its flavor's: computers find it by them (host.json usb_ids)
    endpoint = Endpoint(identity, presence=presence, record=record)
    streams.serve_endpoint(endpoint)
    PairingService(identity, endpoint)
    setup_ap = SetupApService(wifi, identity, presence, radio)
    from .services.button import ResetPins
    from .services.logs import LogsService
    from .services.update import UpdateService
    from .services.relay import RelayAgent
    relay = RelayAgent(identity, wifi, [] if a.relay == "off" else [a.relay])
    update = UpdateService(endpoint=endpoint)
    from .services.card import CardService
    from .services.image import ImageService
    # A new image on trial stays once it gets through: to its host, or to its relay
    image = ImageService(endpoint=endpoint,
                         healthy=lambda: session.peer_alive or relay.status().get("relay") == "up")
    card = CardService(endpoint=endpoint)

    def revert():
        flavors.to_default()                            # the pins: the default flavor too
        update.handle("revert", {"restart": True})     # the pins always restart

    def forget_everything():
        """A new Pi: new key, empty list, no Wi-Fi networks (the pins, an owner's reset)."""
        identity.start_over()
        wifi.forget_all()

    def reset():                                        # an owner's: keeps the code it runs
        forget_everything()
        update.restart()
    identity.serve(endpoint, reset)
    session = Session("pi", [streams, wifi, proxy, identity, link, setup_ap,
                             WifiRemoteService(wifi, setup_ap, endpoint, radio), relay,
                             LogsService(endpoint=endpoint), update, image, card, NetworkShells(endpoint)])
    session.gate = link.check
    audio = AlsaAudio(session.link, a.device)
    gadget = Gadget()
    if gadget.available():
        audio.start(*gadget.home())     # the device's own formats (a clone: each direction its own)
    else:
        audio.start()
    formats = AudioFormatService(audio, gadget)
    session.add(formats)
    flavors = session.add(FlavorService(formats, card, endpoint=endpoint))      # what it presents on USB
    from .services.usbnet import UsbNetWatch
    UsbNetWatch(formats.represent).run()
    for spec in a.forward:
        streams.serve(*parse_forward(spec))
    setup_ap.run()
    relay.run()
    image.run()
    flavors.run()
    try:
        ShellServer().run()
    except OSError as e:
        log.warning("shell: can't listen (%s)", e)

    def start_over():
        forget_everything()
        revert()
    ResetPins(revert, start_over).run()
    return session, streams


def run_host(a, allow, port, gate):
    """The host: streams (+ --expose), the control port, portal handling, the link's auth
    with this computer's key, audio-format negotiation; PortAudio."""
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
    identity = HostIdentity()
    session = Session("host", [streams, watcher, identity, negotiator,
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
        # `audiolink shell`: the Pi's root shell (the Pi allows it when this link is an owner's).
        listen(a.shell_port, ("127.0.0.1", SHELL_PORT), "for audiolink shell", "--shell-port")
    if not a.verbose:
        from .wire.portaudio import quiet_stdout
        quiet_stdout()
    from .wire import usb_audio
    usb_audio.set_extra_ids(a.usb_ids)
    run_portaudio(session.link, a.out_device, a.in_device, expected=lambda: negotiator.running)
    local_ops = {"peer": lambda args: {"connected": session.peer_alive, "peer": session.peer_info,
                                       "link": identity.local_ops(),
                                       "proxy_port": a.proxy_port, "shell_port": a.shell_port,
                                       "expose": list(a.expose)}}
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
