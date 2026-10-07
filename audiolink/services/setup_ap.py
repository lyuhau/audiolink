"""Pi side: the setup network, the Pi's own Wi-Fi network for first contact.

When the Pi has no USB host and hasn't been on any Wi-Fi network for WAIT seconds,
it turns its radio into an access point named "audiopi-<name>" at AP_ADDR, so a
laptop can join it and `audiolink add` it (pairing listens on all interfaces, and
the client tries AP_ADDR). The network is open (pairing itself is protected by
SPAKE2) and offers no internet route, so joined devices know not to use it for
browsing.

It stands down as soon as a host connects over USB (the host can then manage Wi-Fi),
and after MAX_ON seconds it goes back to client mode for WAIT seconds to retry the
saved networks. `audiolink wifi ap on|off|status` overrides the automatic rule, and a
connection-mode change (services/mode.py) pauses it while pending.

The access point runs on its own interface (AP_IFACE) next to wlan0, whose
wpa_supplicant is parked (connects to nothing by itself). The chip allows an access
point plus one client connection on one channel, so the network starts on the channel
of the strongest network around (likely the one it will be told to join): joining that
one keeps the setup network up, and the client sees the result. Joining a network on
another channel would drag the access point there and break its clients, so that
takes it down first (services/mode.py).
"""
import logging
import subprocess
import threading
import time
from pathlib import Path

from . import Service

log = logging.getLogger("audiolink")

AP_ADDR = "192.168.4.1"
AP_IFACE = "uap0"
DEFAULT_CHANNEL = 6                             # allowed everywhere
WAIT = 90
MAX_ON = 15 * 60
RUN = Path("/run")


class SetupApService(Service):
    name = "setup_ap"
    caps = ("setup_ap",)

    def __init__(self, wifi, identity, wait=WAIT, max_on=MAX_ON):
        self.wifi = wifi.manager
        self.identity = identity
        self.wait, self.max_on = wait, max_on
        self.active = False
        self.since = time.monotonic()           # when the current mode started
        self.offline_since = time.monotonic()   # last time we were on a Wi-Fi network
        self.manual = None                      # None: automatic; True: forced on
        self.paused = False                     # a connection-mode change is in progress
        self.channel = None                     # while active
        self.lock = threading.Lock()
        threading.Thread(target=self._policy, daemon=True).start()

    def ssid(self):
        return ("audiopi-" + self.identity.data["name"])[:32]

    # ---- requests from the host over USB
    def handle(self, op, args):
        if op == "status":
            return {"active": self.active, "ssid": self.ssid() if self.active else None,
                    "address": AP_ADDR if self.active else None, "channel": self.channel,
                    "mode": "on (manual)" if self.manual else "automatic"}
        if op == "on":
            self.manual = True
            self.start()
            return self.handle("status", {})
        if op == "off":
            self.manual = None
            self.stop()
            return self.handle("status", {})
        raise ValueError("unknown setup_ap operation %r" % op)

    # ---- for services/mode.py
    def pause_policy(self):
        self.paused = True

    def resume_policy(self):
        self.offline_since = time.monotonic()
        self.paused = False

    # ---- the automatic rule
    def _policy(self):
        while True:
            time.sleep(5)
            if self.manual or self.paused:
                continue
            host = self.session.peer_alive
            now = time.monotonic()
            if not self.active:
                if self.wifi.associated():
                    self.offline_since = now
                elif not host and now - self.offline_since > self.wait:
                    log.info("setup network: no host and no Wi-Fi for %ds", self.wait)
                    self.start()
            elif host:
                log.info("setup network: a host connected; back to client Wi-Fi")
                self.stop()
            elif now - self.since > self.max_on:
                log.info("setup network: retrying saved networks for %ds", self.wait)
                self.stop()

    # ---- switching the radio
    def pick_channel(self):
        """The channel of the strongest 2.4 GHz network in view (DEFAULT_CHANNEL if none)."""
        try:
            nets = self.wifi.handle("scan", {})
        except Exception as e:
            log.warning("setup network: scan failed (%s); using channel %d", e, DEFAULT_CHANNEL)
            return DEFAULT_CHANNEL
        for n in nets:                          # strongest first
            if 2412 <= n["freq"] <= 2472:
                return (n["freq"] - 2407) // 5
        return DEFAULT_CHANNEL

    def start(self):
        with self.lock:
            if self.active:
                return
            channel = self.pick_channel()
            self.wifi.park()
            conf = RUN / "audiolink-hostapd.conf"
            conf.write_text("\n".join([
                # No country_code: without a regulatory database in the image, hostapd would
                # wait forever in COUNTRY_UPDATE.
                "interface=" + AP_IFACE, "driver=nl80211", "ssid=" + self.ssid(), "hw_mode=g",
                "channel=%d" % channel, "ieee80211n=1", "wmm_enabled=1", "auth_algs=1",
                "ignore_broadcast_ssid=0", "ctrl_interface=/run/hostapd", ""]))
            dhcp = RUN / "audiolink-ap-udhcpd.conf"
            dhcp.write_text("\n".join([
                "start 192.168.4.10", "end 192.168.4.60", "interface " + AP_IFACE,
                "pidfile /run/audiolink-ap-udhcpd.pid", "lease_file /run/audiolink-ap.leases",
                "option subnet 255.255.255.0", "option lease 3600", ""]))   # no router: no internet here
            (RUN / "audiolink-ap.leases").touch()
            subprocess.run(["killall", "-q", "hostapd"])
            self._add_iface()
            subprocess.run(["ip", "addr", "add", AP_ADDR + "/24", "dev", AP_IFACE])
            subprocess.run(["ip", "link", "set", AP_IFACE, "up"])
            if not self._launch_hostapd(conf):
                subprocess.run(["killall", "-q", "hostapd"])
                subprocess.run(["iw", "dev", AP_IFACE, "del"])
                self.wifi.unpark()
                return
            subprocess.run(["udhcpd", str(dhcp)])
            self.active, self.since, self.channel = True, time.monotonic(), channel
            log.info("setup network: %r is up at %s, channel %d", self.ssid(), AP_ADDR, channel)

    def _add_iface(self):
        subprocess.run(["iw", "dev", AP_IFACE, "del"], capture_output=True)
        subprocess.run(["iw", "dev", "wlan0", "interface", "add", AP_IFACE, "type", "__ap"])
        try:                                    # its own locally administered address
            b = bytearray.fromhex(Path("/sys/class/net/wlan0/address").read_text().strip().replace(":", ""))
            b[0] = (b[0] | 0x02) & 0xFE
            b[5] ^= 0x01
            subprocess.run(["ip", "link", "set", AP_IFACE, "address", ":".join("%02x" % x for x in b)])
        except (OSError, ValueError):
            pass

    def _launch_hostapd(self, conf, attempts=3):
        """Start hostapd and wait until it reports the access point ENABLED."""
        for attempt in range(1, attempts + 1):
            r = subprocess.run(["hostapd", "-B", "-P", "/run/audiolink-hostapd.pid", str(conf)],
                               capture_output=True, text=True)
            for _ in range(20):
                st = subprocess.run(["hostapd_cli", "-p", "/run/hostapd", "-i", AP_IFACE, "status"],
                                    capture_output=True, text=True).stdout
                if "state=ENABLED" in st:
                    return True
                time.sleep(0.25)
            lines = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()]
            log.warning("setup network: hostapd attempt %d failed: %s", attempt, " | ".join(lines[-3:]) or
                        "no ENABLED state")
            subprocess.run(["killall", "-q", "hostapd"])
            time.sleep(1)
        return False

    def stop(self):
        with self.lock:
            if not self.active:
                return
            for pidfile in ("/run/audiolink-hostapd.pid", "/run/audiolink-ap-udhcpd.pid"):
                try:
                    pid = int(Path(pidfile).read_text().strip())
                    subprocess.run(["kill", str(pid)])
                except (OSError, ValueError):
                    pass
            time.sleep(1)
            subprocess.run(["iw", "dev", AP_IFACE, "del"])
            self.active, self.channel = False, None
            self.since = self.offline_since = time.monotonic()
        self.wifi.unpark()
        log.info("setup network: down")
