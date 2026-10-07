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
setup network's clients). Every RETRY seconds while nobody is on the setup network
(every RETRY_BUSY seconds even if someone is: a phone that joined it by itself and
stays mustn't keep the Pi off its Wi-Fi), the Pi looks for its saved networks and lets
wlan0 join one if it's in range.

While up, it checks that hostapd and its DHCP server are still running, and restarts
them if not. `audiolink wifi ap on` keeps it up; `off` closes it for now, but the
lonely rule still opens it again (it's the Pi's safety net: nothing overrides that).
A Wi-Fi change from the network (services/wifi_remote.py) pauses the rule while in progress.
The USB host doesn't count as a client: it can manage the Pi, but can't stand in for
anyone reaching it.
"""
import logging
import subprocess
import threading
import time
from pathlib import Path

from . import Service, proc

log = logging.getLogger("audiolink")

AP_ADDR = "192.168.4.1"
AP_IFACE = "uap0"
DEFAULT_CHANNEL = 6                             # allowed everywhere
LONELY = 45                                     # nobody connected for this long: open up
GRACE = 30                                      # nobody on the setup network for this long: may close
OFFLINE = 30                                    # off Wi-Fi this long: park wlan0, pick our own channel
RETRY = 120                                     # off Wi-Fi: look for saved networks this often
RETRY_BUSY = 600                                #  ... even with someone on the setup network
HEALTH_BACKOFF = (0, 30, 120, 600)              # waits before restarting a broken setup network again
PIDS = {"hostapd": "/run/audiolink-hostapd.pid", "udhcpd": "/run/audiolink-ap-udhcpd.pid"}
RUN = Path("/run")


def channel_of(freq):
    return (freq - 2407) // 5 if freq and 2412 <= freq <= 2472 else None


class SetupApService(Service):
    name = "setup_ap"
    caps = ("setup_ap",)

    def __init__(self, wifi, identity, presence):
        self.wifi = wifi                        # services/wifi.py WifiService
        self.identity = identity
        self.presence = presence
        self.active = False
        self.manual = False                     # True: kept up (`wifi ap on`)
        self.closed_at = 0.0                    # `wifi ap off`: lonely counts from then
        self.paused = False                     # a connection-mode change is in progress
        self.channel = None                     # while active
        self.on_wifi_at = time.monotonic()      # last time wlan0 was on a network (or was let loose)
        self.busy_at = 0.0                      # last time someone was on the setup network
        self.retry_at = time.monotonic()
        self.repairs, self.repair_at = 0, 0.0   # health-check restarts in a row
        self.lock = threading.RLock()

    def run(self):
        """Start applying the rule (in the background). First, a restarted daemon takes
        down a setup network the last run left up."""
        for pidfile in PIDS.values():
            proc.stop(pidfile)
        subprocess.run(["iw", "dev", AP_IFACE, "del"], capture_output=True)
        threading.Thread(target=self._policy, daemon=True).start()

    def ssid(self):
        return ("audiopi-" + self.identity.pi_name())[:32]

    # ---- requests from the host over USB
    def handle(self, op, args):
        if op == "status":
            return {"active": self.active, "ssid": self.ssid(), "address": AP_ADDR if self.active else None,
                    "channel": self.channel, "stations": len(self.stations()) if self.active else 0,
                    "mode": "on" if self.manual else "auto"}
        if op in ("on", "off", "auto"):
            self.manual = op == "on"
            if op == "on":
                self.start()
            elif op == "off":
                self.closed_at = time.monotonic()
                self.stop()
            return self.handle("status", {})
        raise ValueError("unknown setup_ap operation %r" % op)

    # ---- for services/wifi_remote.py
    def pause_policy(self):
        with self.lock:                         # let a start/stop in progress finish first
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
        with self.lock:                         # pause_policy() waits for a tick in progress
            if not self.paused:
                self._apply_rule()

    def _apply_rule(self):
        now = time.monotonic()
        channel = channel_of(self.wifi.freq())
        if channel:
            self.on_wifi_at = now
        if not self.active:
            # Open up once lonely; off Wi-Fi, give wlan0 a while to (re)connect first.
            lonely = min(self.presence.lonely_for(), now - self.closed_at)
            if self.manual or (lonely >= LONELY and (channel or now - self.on_wifi_at > OFFLINE)):
                log.info("setup network: nobody connected for %ds; opening", lonely)
                self.start()
            return
        dead = self.dead_parts()
        if dead:
            wait = HEALTH_BACKOFF[min(self.repairs, len(HEALTH_BACKOFF) - 1)]
            if now - self.repair_at >= wait:
                log.warning("setup network: %s stopped; restarting it", " and ".join(dead))
                self.repairs, self.repair_at = self.repairs + 1, now
                self.restart()
            return
        if now - self.repair_at > 600:
            self.repairs = 0                    # healthy for a while
        if self.stations():
            self.busy_at = now
        if not self.manual and self.presence.connected_except("setup") and now - self.busy_at > GRACE:
            log.info("setup network: a client is connected over Wi-Fi; closing")
            self.stop()
        elif channel and channel != self.channel:
            log.info("setup network: Wi-Fi moved to channel %d; following", channel)
            self.restart(channel)
        elif not channel and not self.wifi.parked and now - self.on_wifi_at > OFFLINE:
            log.info("setup network: off Wi-Fi for %ds; parking it", OFFLINE)
            self.wifi.park()
        elif self.wifi.parked and now - self.retry_at > (RETRY if now - self.busy_at > GRACE else RETRY_BUSY):
            self.retry_at = now
            self._retry_saved()

    def _retry_saved(self):
        """Off Wi-Fi with nobody on the setup network: if a saved network is in range,
        let wlan0 join it (the setup network follows it to its channel)."""
        try:
            nets = self.wifi.scan()
        except Exception as e:
            log.debug("setup network: scan: %s", e)
            return
        found = [n["ssid"] for n in nets if n.get("saved")]
        if found:
            log.info("setup network: saved network %r in range; trying it", found[0])
            self.on_wifi_at = time.monotonic()
            self.wifi.unpark()

    def dead_parts(self):
        """Parts of a running setup network that have stopped (hostapd, udhcpd)."""
        return [name for name, pidfile in PIDS.items() if not proc.alive(pidfile)]

    def stations(self):
        """Devices currently on the setup network."""
        r = subprocess.run(["hostapd_cli", "-p", "/run/hostapd", "-i", AP_IFACE, "list_sta"],
                           capture_output=True, text=True)
        return [l for l in r.stdout.split() if l.count(":") == 5] if r.returncode == 0 else []

    # ---- switching the radio
    def pick_channel(self):
        """The channel of the strongest 2.4 GHz network in view (DEFAULT_CHANNEL if none)."""
        try:
            nets = self.wifi.scan()
        except Exception as e:
            log.warning("setup network: scan failed (%s); using channel %d", e, DEFAULT_CHANNEL)
            return DEFAULT_CHANNEL
        for n in nets:                          # strongest first
            if channel_of(n["freq"]):
                return channel_of(n["freq"])
        return DEFAULT_CHANNEL

    def start(self):
        with self.lock:
            if self.active:
                return
            channel = self._wifi_channel()
            if not channel:
                if time.monotonic() - self.on_wifi_at < OFFLINE:
                    # Just left a network, or (re)joining one (it reads as not connected for
                    # a moment around changes): don't park it; the rule tries again later.
                    log.info("setup network: waiting for Wi-Fi to settle")
                    return
                channel = self.pick_channel()   # off Wi-Fi: our own channel, wlan0 parked
                if not self.wifi.park():
                    return                      # it joined a network meanwhile: next time
            if not self._bring_up(channel):
                if self.wifi.parked:
                    self.wifi.unpark()
                return
            self.active, self.channel, self.busy_at, self.retry_at = True, channel, 0.0, time.monotonic()
            log.info("setup network: %r is up at %s, channel %d", self.ssid(), AP_ADDR, channel)

    # ---- the radio
    def _bring_up(self, channel):
        """hostapd and a DHCP server on AP_IFACE; False if the access point didn't start."""
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
            return False
        if subprocess.run(["udhcpd", str(dhcp)]).returncode != 0:
            log.warning("setup network: its DHCP server didn't start")   # the health check retries
        return True

    def _wifi_channel(self, wait=5):
        for _ in range(wait * 2):
            channel = channel_of(self.wifi.freq())
            if channel:
                self.on_wifi_at = time.monotonic()
                return channel
            time.sleep(0.5)
        return None

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
            self._take_down()
            self.active, self.channel = False, None
            self.on_wifi_at = time.monotonic()
            if self.wifi.parked:
                self.wifi.unpark()
        log.info("setup network: down")

    def _take_down(self):
        for pidfile in PIDS.values():
            proc.stop(pidfile)
        time.sleep(1)
        subprocess.run(["iw", "dev", AP_IFACE, "del"])

    def restart(self, channel=None):
        """Bring the setup network up again (on `channel`, or the one it's on), leaving
        wlan0 as it is (parked or not)."""
        with self.lock:
            if not self.active:
                return self.start()
            self._take_down()
            channel = channel or self.channel
            if self._bring_up(channel):
                self.channel = channel
                log.info("setup network: %r is up again, channel %d", self.ssid(), channel)
            else:
                self.active, self.channel = False, None
                if self.wifi.parked:
                    self.wifi.unpark()
                log.warning("setup network: couldn't bring it back up")
