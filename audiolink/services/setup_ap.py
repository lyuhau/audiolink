"""Pi side: the setup network, the Pi's own Wi-Fi network for first contact.

When the Pi has no USB host and hasn't been on any Wi-Fi network for WAIT seconds,
it turns its radio into an access point named "audiopi-<name>" at AP_ADDR, so a
laptop can join it and `audiolink add` it (pairing listens on all interfaces, and
the client tries AP_ADDR). The network is open (pairing itself is protected by
SPAKE2) and offers no internet route, so joined devices know not to use it for
browsing.

It stands down as soon as a host connects over USB (the host can then manage Wi-Fi),
and after MAX_ON seconds it goes back to client mode for WAIT seconds to retry the
saved networks. `audiolink wifi ap on|off|status` overrides the automatic rule.
"""
import logging
import subprocess
import threading
import time
from pathlib import Path

from . import Service

log = logging.getLogger("audiolink")

AP_ADDR = "192.168.4.1"
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
        self.lock = threading.Lock()
        threading.Thread(target=self._policy, daemon=True).start()

    def ssid(self):
        return ("audiopi-" + self.identity.data["name"])[:32]

    # ---- requests from the host over USB
    def handle(self, op, args):
        if op == "status":
            return {"active": self.active, "ssid": self.ssid() if self.active else None,
                    "address": AP_ADDR if self.active else None,
                    "mode": "on (manual)" if self.manual else "automatic"}
        if op == "on":
            self.manual = True
            self._start()
            return self.handle("status", {})
        if op == "off":
            self.manual = None
            self._stop()
            return self.handle("status", {})
        raise ValueError("unknown setup_ap operation %r" % op)

    # ---- the automatic rule
    def _policy(self):
        while True:
            time.sleep(5)
            if self.manual:
                continue
            host = self.session.peer_alive
            now = time.monotonic()
            if not self.active:
                if self.wifi.associated():
                    self.offline_since = now
                elif not host and now - self.offline_since > self.wait:
                    log.info("setup network: no host and no Wi-Fi for %ds", self.wait)
                    self._start()
            elif host:
                log.info("setup network: a host connected; back to client Wi-Fi")
                self._stop()
            elif now - self.since > self.max_on:
                log.info("setup network: retrying saved networks for %ds", self.wait)
                self._stop()

    # ---- switching the radio
    def _start(self):
        with self.lock:
            if self.active:
                return
            self.wifi.suspend()
            conf = RUN / "audiolink-hostapd.conf"
            conf.write_text("\n".join([
                # No country_code: without a regulatory database in the image, hostapd would
                # wait forever in COUNTRY_UPDATE. Channel 6 is allowed everywhere.
                "interface=wlan0", "driver=nl80211", "ssid=" + self.ssid(), "hw_mode=g", "channel=6",
                "ieee80211n=1", "wmm_enabled=1", "auth_algs=1",
                "ignore_broadcast_ssid=0", "ctrl_interface=/run/hostapd", ""]))
            dhcp = RUN / "audiolink-ap-udhcpd.conf"
            dhcp.write_text("\n".join([
                "start 192.168.4.10", "end 192.168.4.60", "interface wlan0",
                "pidfile /run/audiolink-ap-udhcpd.pid", "lease_file /run/audiolink-ap.leases",
                "option subnet 255.255.255.0", "option lease 3600", ""]))   # no router: no internet here
            (RUN / "audiolink-ap.leases").touch()
            subprocess.run(["killall", "-q", "hostapd"])
            subprocess.run(["ip", "addr", "add", AP_ADDR + "/24", "dev", "wlan0"])
            subprocess.run(["ip", "link", "set", "wlan0", "up"])
            if not self._launch_hostapd(conf):
                subprocess.run(["killall", "-q", "hostapd"])
                subprocess.run(["ip", "addr", "flush", "dev", "wlan0"])
                self.wifi.resume()
                return
            subprocess.run(["udhcpd", str(dhcp)])
            self.active, self.since = True, time.monotonic()
            log.info("setup network: %r is up at %s", self.ssid(), AP_ADDR)

    def _launch_hostapd(self, conf, attempts=3):
        """Start hostapd and wait until it reports the access point ENABLED."""
        for attempt in range(1, attempts + 1):
            r = subprocess.run(["hostapd", "-B", "-P", "/run/audiolink-hostapd.pid", str(conf)],
                               capture_output=True, text=True)
            for _ in range(20):
                st = subprocess.run(["hostapd_cli", "-p", "/run/hostapd", "status"],
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

    def _stop(self):
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
            subprocess.run(["ip", "addr", "flush", "dev", "wlan0"])
            self.active = False
            self.since = self.offline_since = time.monotonic()
        self.wifi.resume()
        log.info("setup network: down")
