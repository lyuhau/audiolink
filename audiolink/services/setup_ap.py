"""Pi side: the setup network, the Pi's own Wi-Fi network ("audiopi-<name>" at AP_ADDR).

The lonely rule: the Pi can't know what situation its clients are in, so when nobody
has been connected for LONELY seconds (services/presence.py), it opens up as much as
it can, and the setup network comes up. Once a client is connected some other way
(over Wi-Fi) and nobody has been on the setup network for GRACE seconds, it closes
again. A laptop or phone in range can always join it to `audiolink add` the Pi or
change its Wi-Fi (pairing and the network endpoint listen on all interfaces). The
network is open (pairing itself is protected by SPAKE2) and offers no internet route,
so joined devices know not to use it for browsing.

The chip allows an access point plus one client connection, on one channel. On Wi-Fi,
the setup network runs on that network's channel, next to it, and follows it: if the
Wi-Fi connection changes channel, the firmware drags the access point along and its
clients go silent, so it is restarted on the new channel. Off Wi-Fi, it runs on the
channel of the strongest network around (likely the one the Pi will be told to join),
with wlan0 parked (connecting to nothing by itself: its scans would disturb the
setup network's clients). Every RETRY seconds, while nobody is on the setup network,
the Pi looks for its saved networks and lets wlan0 join one if it's in range.

`audiolink wifi ap on|off|auto` overrides the rule, and a connection-mode change
(services/mode.py) pauses it while in progress. The USB host doesn't count as a
client: it can manage the Pi, but can't stand in for anyone reaching it.
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
LONELY = 45                                     # nobody connected for this long: open up
GRACE = 30                                      # nobody on the setup network for this long: may close
OFFLINE = 30                                    # off Wi-Fi this long: park wlan0, pick our own channel
RETRY = 120                                     # off Wi-Fi: look for saved networks this often
RUN = Path("/run")


def channel_of(freq):
    return (freq - 2407) // 5 if freq and 2412 <= freq <= 2472 else None


class SetupApService(Service):
    name = "setup_ap"
    caps = ("setup_ap",)

    def __init__(self, wifi, identity, presence, lonely=LONELY):
        self.wifi = wifi.manager
        self.identity = identity
        self.presence = presence
        self.lonely = lonely
        self.active = False
        self.manual = None                      # None: automatic; True/False: forced on/off
        self.paused = False                     # a connection-mode change is in progress
        self.channel = None                     # while active
        self.on_wifi_at = time.monotonic()      # last time wlan0 was on a network (or was let loose)
        self.busy_at = 0.0                      # last time someone was on the setup network
        self.retry_at = time.monotonic()
        self.lock = threading.RLock()
        threading.Thread(target=self._policy, daemon=True).start()

    def ssid(self):
        return ("audiopi-" + self.identity.data["name"])[:32]

    # ---- requests from the host over USB
    def handle(self, op, args):
        if op == "status":
            return {"active": self.active, "ssid": self.ssid(), "address": AP_ADDR if self.active else None,
                    "channel": self.channel, "stations": len(self.stations()) if self.active else 0,
                    "mode": {True: "on", False: "off", None: "auto"}[self.manual]}
        if op in ("on", "off", "auto"):
            self.manual = {"on": True, "off": False, "auto": None}[op]
            if op == "on":
                self.start()
            elif op == "off":
                self.stop()
            return self.handle("status", {})
        raise ValueError("unknown setup_ap operation %r" % op)

    # ---- for services/mode.py
    def pause_policy(self):
        self.paused = True

    def resume_policy(self):
        self.on_wifi_at = time.monotonic()
        self.paused = False

    # ---- the lonely rule
    def _policy(self):
        while True:
            time.sleep(5)
            try:
                self._tick()
            except Exception as e:
                log.warning("setup network: %s", e)

    def _tick(self):
        if self.paused:
            return
        now = time.monotonic()
        channel = channel_of(self.wifi.freq())
        if channel:
            self.on_wifi_at = now
        if self.manual is False:
            return
        if not self.active:
            # Open up once lonely; off Wi-Fi, give wlan0 a while to (re)connect first.
            if self.manual or (self.presence.lonely_for() >= self.lonely and
                               (channel or now - self.on_wifi_at > OFFLINE)):
                log.info("setup network: nobody connected for %ds; opening", self.presence.lonely_for())
                self.start()
            return
        if self.stations():
            self.busy_at = now
        if not self.manual and self.presence.connected_except("setup") and now - self.busy_at > GRACE:
            log.info("setup network: a client is connected over Wi-Fi; closing")
            self.stop()
        elif channel and channel != self.channel:
            log.info("setup network: Wi-Fi moved to channel %d; following", channel)
            self.restart()
        elif not channel and not self.wifi.parked and now - self.on_wifi_at > OFFLINE:
            log.info("setup network: off Wi-Fi for %ds; parking it", OFFLINE)
            self.wifi.park()
        elif self.wifi.parked and now - self.busy_at > GRACE and now - self.retry_at > RETRY:
            self.retry_at = now
            self._retry_saved()

    def _retry_saved(self):
        """Off Wi-Fi with nobody on the setup network: if a saved network is in range,
        let wlan0 join it (the setup network follows it to its channel)."""
        try:
            nets = self.wifi.handle("scan", {})
        except Exception as e:
            log.debug("setup network: scan: %s", e)
            return
        found = [n["ssid"] for n in nets if n.get("saved")]
        if found:
            log.info("setup network: saved network %r in range; trying it", found[0])
            self.on_wifi_at = time.monotonic()
            self.wifi.unpark()

    def stations(self):
        """Devices currently on the setup network."""
        r = subprocess.run(["hostapd_cli", "-p", "/run/hostapd", "-i", AP_IFACE, "list_sta"],
                           capture_output=True, text=True)
        return [l for l in r.stdout.split() if l.count(":") == 5] if r.returncode == 0 else []

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
            channel = channel_of(self.wifi.freq())
            if not channel:                     # off Wi-Fi: our own channel, wlan0 parked
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
                if self.wifi.parked:
                    self.wifi.unpark()
                return
            subprocess.run(["udhcpd", str(dhcp)])
            self.active, self.channel, self.busy_at, self.retry_at = True, channel, 0.0, time.monotonic()
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
            self.on_wifi_at = time.monotonic()
            if self.wifi.parked:
                self.wifi.unpark()
        log.info("setup network: down")

    def restart(self):
        with self.lock:
            self.stop()
            self.start()
