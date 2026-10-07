"""Pi side: changing the Pi's Wi-Fi from the network.

A paired machine asks the Pi to join a network (endpoint "change_wifi", authenticated,
details sealed with the read secret). This is about the network only: the Pi saves a
network as soon as its password is proven, and switching to it makes it the preferred
one. Whether clients can still reach the Pi afterwards isn't something the Pi can
check, and it doesn't wait to find out: if nobody connects, it opens its setup network
(services/setup_ap.py, the lonely rule).

How the change is made depends on how the requester reached the Pi:
  - over Wi-Fi: the Pi first tries the network on a second interface without leaving
    (services/wifi_trial.py), so a wrong password, a network without DHCP and the like
    are refused on the spot. The chip can't do that next to an access point, so the
    setup network is closed for it, unless someone is on it (then there's no trial).
    Then it replies and switches.
  - over the setup network, to a network on the setup network's channel: it joins with
    the setup network still up, and replies with the result.
  - over the setup network, to a network on another channel: it replies, then switches;
    the setup network follows it to the new channel.
A switch that fails puts the Pi back on its saved networks. Changes over USB
(`audiolink wifi join` on the host) go to services/wifi.py directly.
"""
import logging
import threading
import time

from . import Service
from .setup_ap import AP_ADDR, channel_of
from .wifi import credentials
from .wifi_trial import trial

log = logging.getLogger("audiolink")

SWITCH_DELAY = 3
MAX_TRIES = 3


class ModeService(Service):
    name = "mode"
    caps = ("mode",)

    def __init__(self, wifi, setup_ap, endpoint):
        self.wifi = wifi.manager
        self.setup_ap = setup_ap
        self.lock = threading.Lock()
        self.switching = None               # target network while a change is in progress
        self.last = None
        endpoint.register("change_wifi", self._change_wifi, auth=True)
        endpoint.register("change_status", lambda conn, req: self.status(), auth=True)

    def status(self):
        return {"saved": sorted(self.wifi.saved), "switching": self.switching, "last": self.last}

    def handle(self, op, args):         # host over USB
        if op == "status":
            return self.status()
        raise ValueError("unknown mode operation %r" % op)

    def _change_wifi(self, conn, req):
        p = conn.open(req["sealed"])
        ssid = (p.get("ssid") or "").strip()
        if not ssid:
            return {"error": "No network name"}
        use_saved = bool(p.get("use_saved")) or (ssid in self.wifi.saved and not p.get("password"))
        if use_saved and ssid not in self.wifi.saved:
            return {"error": "No saved password for %s" % ssid}
        try:
            ent = dict(self.wifi.saved[ssid]) if use_saved else dict(
                credentials(ssid, p.get("security") or "wpa2", p.get("password")), hidden=bool(p.get("hidden")))
        except ValueError as e:
            return {"error": str(e)}
        current = self.wifi.current_ssid()
        if current == ssid and use_saved:
            return {"accepted": True, "already": True}
        age, nets = self.wifi.last_scan()
        seen = next((n for n in nets if n["ssid"] == ssid), None) if age < 600 else None
        if not p.get("hidden") and nets and age < 600 and not seen:
            return {"error": "%r wasn't in the Pi's last scan (%d min ago; the Pi only sees 2.4 GHz)"
                             % (ssid, age // 60), "code": "not_found", "stayed_on": current}
        with self.lock:
            if self.switching:
                return {"error": "Already switching to %s; retry in a minute" % self.switching}
            self.switching = ssid
        args = {"ssid": ssid, "password": p.get("password"), "security": p.get("security") or "wpa2",
                "hidden": bool(p.get("hidden")), "use_saved": use_saved}
        via_setup = conn.addr[0].rsplit(".", 1)[0] == AP_ADDR.rsplit(".", 1)[0]
        ap = self.setup_ap
        self.setup_ap.pause_policy()
        try:
            if via_setup and ap.active and seen and channel_of(seen["freq"]) == ap.channel:
                return self._join_now(args)
            precheck = None
            if current and current != ssid and not via_setup and not (ap.active and ap.stations()):
                ap.stop()                   # the trial needs the radio's second slot
                precheck = self._trial(ssid, ent, use_saved)
                if precheck["ok"] is False:
                    reason = "%s; stayed on %r" % (precheck["reason"], current)
                    self.last = {"ok": False, "result": "tried %r: %s" % (ssid, reason)}
                    self._done()
                    return {"error": "%r: %s" % (ssid, reason), "code": precheck["code"], "stayed_on": current}
        except Exception:
            self._done()
            raise
        tries = max(1, min(MAX_TRIES, int(p.get("tries") or 1)))
        threading.Thread(target=self._switch, args=(args, tries), daemon=True).start()
        log.info("mode: switching to %r in %ds", ssid, SWITCH_DELAY)
        return {"accepted": True, "switch_in": SWITCH_DELAY, "tries": tries, "precheck": precheck}

    def _trial(self, ssid, ent, use_saved):
        r = trial(ssid, ent)
        if r["code"] == "auth_failed" and use_saved and ent.get("verified"):
            # A password that has worked before: right after a failed login an access
            # point can turn the next one away too. Once more before calling it wrong.
            time.sleep(5)
            r = trial(ssid, ent)
        if r["ok"] or r["code"] == "no_ip":
            self.wifi.remember(ssid, ent)   # the password works: never ask for it again
        return r

    def _join_now(self, args):
        """From the setup network, to a network on its channel: join with the setup
        network still up, and answer with the result."""
        r = self.wifi.handle("join", dict(args, timeout=20))
        self._done(r)
        if not r["joined"]:
            return {"error": "couldn't join %r: %s (still on the setup network)" % (args["ssid"], r["reason"]),
                    "code": r.get("code"), "stayed_on": None}
        return {"accepted": True, "joined": True, "ip": r["ip"], "internet": r.get("internet")}

    def _switch(self, args, tries):
        time.sleep(SWITCH_DELAY)                # let the reply reach the requester first
        r = None
        try:
            for attempt in range(tries):
                r = self.wifi.handle("join", dict(args, timeout=25))
                if r["joined"] or r.get("code") in ("auth_failed", "not_found"):
                    break                       # success, or a failure retrying can't fix
        finally:
            self._done(r)

    def _done(self, r=None):
        """End of a change: record it, let the setup network's rule run again."""
        if r is not None:
            ssid = r.get("ssid") or self.switching
            self.last = {"ok": True, "result": "on %r" % ssid} if r["joined"] else \
                {"ok": False, "result": "couldn't join %r: %s" % (ssid, r["reason"])}
            log.info("mode: %s", self.last["result"])
            if r["joined"]:
                discovery = self.session.by_name.get("discovery")
                if discovery:
                    discovery.republish()       # so clients can find it on the DHT
        with self.lock:
            self.switching = None
        self.setup_ap.resume_policy()
