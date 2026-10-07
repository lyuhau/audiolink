"""Pi side: changing the connection mode from the network, confirm-or-revert.

Like changing a game's resolution: a change that could cut off the channel being
used is provisional until someone reaches the Pi through the new mode. Today the
modes are "on Wi-Fi network X" and "the setup network" (services/setup_ap.py).

  1. a paired machine sends the new mode (endpoint "change_wifi", authenticated,
     details sealed with the read secret); the Pi validates what it can. If it's on a
     Wi-Fi network, it first tries the new one on a second interface without leaving
     (services/wifi_trial.py): a wrong password, a network without DHCP and the like
     are refused there and then, and the Pi never moves. Otherwise it replies before
     switching (SWITCH_DELAY), naming the fallback
  2. it switches: TRIES attempts (default 1; no retry on a wrong password or an
     unknown network), then publishes its DHT record so it can be found.
     From the setup network, a network on the setup network's channel is joined
     right away with the setup network still up (the chip allows that on one
     channel), and the reply carries the result; confirming then has to come from
     the new network, and the setup network closes once it does
  3. it waits up to confirm_within seconds for an authenticated "confirm" through
     the new mode: from the requester (it holds a one-time token) or, if asked,
     from any paired machine
  4. confirmed: the network is saved. Otherwise: discard it, go back to the
     fallback mode, and keep the reason for "change_status".
Changes over USB (`audiolink wifi join` on the host) don't need this: the USB
link is itself the safety net.
"""
import hashlib
import hmac
import logging
import threading
import time

from . import Service
from .setup_ap import AP_ADDR
from .wifi import credentials
from .wifi_trial import trial

log = logging.getLogger("audiolink")

SWITCH_DELAY = 3
MAX_TRIES = 3
CONFIRM_RANGE = (30, 900)


class ModeService(Service):
    name = "mode"
    caps = ("mode",)

    def __init__(self, wifi, setup_ap, endpoint):
        self.wifi = wifi.manager
        self.setup_ap = setup_ap
        self.lock = threading.Lock()
        self.pending = None
        self.confirmed = threading.Event()
        self.last = None
        endpoint.register("change_wifi", self._change_wifi, auth=True)
        endpoint.register("confirm", self._confirm, auth=True)
        endpoint.register("change_status", lambda conn, req: self.status(), auth=True)

    def status(self):
        p = self.pending
        return {"saved": sorted(self.wifi.saved),
                "pending": {k: p[k] for k in ("target", "phase", "fallback")} if p else None,
                "seconds_left": max(0, int(p["deadline"] - time.monotonic())) if p and p.get("deadline") else None,
                "last": self.last}

    def handle(self, op, args):         # host over USB
        if op == "status":
            return self.status()
        raise ValueError("unknown mode operation %r" % op)

    # ---- 1. accept
    def _change_wifi(self, conn, req):
        p = conn.open(req["sealed"])
        ssid = (p.get("ssid") or "").strip()
        if not ssid:
            return {"error": "no network name given"}
        use_saved = bool(p.get("use_saved")) or (ssid in self.wifi.saved and not p.get("password"))
        if use_saved and ssid not in self.wifi.saved:
            return {"error": "%r isn't a saved network on this Pi; send its password" % ssid}
        try:
            ent = dict(self.wifi.saved[ssid]) if use_saved else dict(
                credentials(ssid, p.get("security") or "wpa2", p.get("password")), hidden=bool(p.get("hidden")))
        except ValueError as e:
            return {"error": str(e)}
        age, nets = self.wifi.last_scan()
        seen = next((n for n in nets if n["ssid"] == ssid), None) if age < 600 else None
        if not p.get("hidden") and nets and age < 600 and not seen:
            return {"error": "%r wasn't in the Pi's last scan (%d min ago; the Pi only sees 2.4 GHz)"
                             % (ssid, age // 60)}
        with self.lock:
            if self.pending:
                return {"error": "a change to %r is already in progress" % self.pending["target"]}
            current = self.wifi.current_ssid()
            fallback = "setup network" if self.setup_ap.active or not current else "Wi-Fi %r" % current
            window = max(CONFIRM_RANGE[0], min(CONFIRM_RANGE[1], int(p.get("confirm_within") or 180)))
            self.pending = {"target": ssid, "phase": "checking", "fallback": fallback,
                            "fallback_ssid": None if fallback == "setup network" else current,
                            "fallback_manual": self.setup_ap.manual, "window": window,
                            "token_hash": p.get("token_hash"), "deadline": None}
            self.confirmed.clear()
        args = {"ssid": ssid, "password": p.get("password"), "security": p.get("security") or "wpa2",
                "hidden": bool(p.get("hidden")), "use_saved": use_saved}
        try:
            ap_channel = self.setup_ap.channel if self.setup_ap.active else None
            if ap_channel and seen and (seen["freq"] - 2407) // 5 == ap_channel:
                return self._join_alongside(args)
            precheck = None
            if current and current != ssid and not ap_channel:
                precheck = trial(ssid, ent)
                if precheck["ok"] is False:
                    reason = "%s; stayed on %r" % (precheck["reason"], current)
                    self.last = {"ok": False, "result": "tried %r: %s" % (ssid, reason), "at": time.time()}
                    self._clear()
                    return {"error": "%r: %s" % (ssid, reason), "code": precheck["code"]}
        except Exception:
            self._clear()
            raise
        tries = max(1, min(MAX_TRIES, int(p.get("tries") or 1)))
        self.pending["phase"] = "switching"
        threading.Thread(target=self._run, args=(args, tries), daemon=True).start()
        log.info("mode: switching to %r in %ds (fallback: %s, confirm within %ds)",
                 ssid, SWITCH_DELAY, fallback, window)
        return {"accepted": True, "switch_in": SWITCH_DELAY, "confirm_within": window,
                "fallback": fallback, "tries": tries, "precheck": precheck}

    def _clear(self):
        with self.lock:
            self.pending = None

    def _join_alongside(self, args):
        """From the setup network, to a network on its channel: join now, keeping the
        setup network up, and answer with the result."""
        p = self.pending
        self.setup_ap.pause_policy()
        r = self.wifi.handle("join", dict(args, save=False, timeout=20))
        if not r["joined"]:
            self.setup_ap.resume_policy()
            self.last = {"ok": False, "result": "couldn't join %r: %s" % (args["ssid"], r["reason"]),
                         "at": time.time()}
            self._clear()
            return {"error": "couldn't join %r: %s (still on the setup network)" % (args["ssid"], r["reason"]),
                    "code": r.get("code")}
        p.update(phase="awaiting confirmation", deadline=time.monotonic() + p["window"], alongside=True)
        self._republish()
        log.info("mode: on %r (%s) next to the setup network; waiting %ds for a confirmation from there",
                 args["ssid"], r["ip"], p["window"])
        threading.Thread(target=self._await, args=(args["ssid"],), daemon=True).start()
        return {"accepted": True, "joined": True, "ip": r["ip"], "internet": r.get("internet"),
                "switch_in": 0, "confirm_within": p["window"], "fallback": p["fallback"], "tries": 1}

    def _republish(self):
        discovery = self.session.by_name.get("discovery")
        if discovery:
            discovery.republish()               # so the requester can find us on the DHT

    # ---- 2.-4. switch, await confirmation, keep or revert
    def _run(self, args, tries):
        time.sleep(SWITCH_DELAY)                # let the reply reach the requester first
        p = self.pending
        self.setup_ap.pause_policy()
        if self.setup_ap.active:
            self.setup_ap.stop()
        r = None
        for attempt in range(tries):
            r = self.wifi.handle("join", dict(args, save=False, timeout=25))
            if r["joined"] or r.get("code") in ("auth_failed", "not_found"):
                break                           # success, or a failure retrying can't fix
        if not r["joined"]:
            return self._revert("couldn't join %r: %s" % (args["ssid"], r["reason"]))
        p.update(phase="awaiting confirmation", deadline=time.monotonic() + p["window"])
        self._republish()
        log.info("mode: on %r (%s); waiting %ds for a confirmation", args["ssid"], r["ip"], p["window"])
        self._await(args["ssid"])

    def _await(self, ssid):
        p = self.pending
        if self.confirmed.wait(p["window"]):
            self.wifi.commit_provisional()
            self.setup_ap.manual = None
            if self.setup_ap.active:
                time.sleep(2)                   # let the confirmation's reply go out first
                self.setup_ap.stop()
            self.setup_ap.resume_policy()
            self.last = {"ok": True, "result": "on %r, confirmed and saved" % ssid, "at": time.time()}
            self._clear()
            log.info("mode: %r confirmed", ssid)
        elif p.get("alongside"):
            log.warning("mode: joined %r, but nobody confirmed from there within %ds; leaving it",
                        ssid, p["window"])
            self.wifi.discard_provisional()     # parked: the setup network stays as it was
            self.setup_ap.resume_policy()
            self.last = {"ok": False, "result": "joined %r, but nobody confirmed from there within %ds"
                         % (ssid, p["window"]), "reverted_to": "setup network", "at": time.time()}
            self._clear()
        else:
            self._revert("joined %r, but nobody confirmed within %ds" % (ssid, p["window"]))

    def _revert(self, reason):
        p = self.pending
        log.warning("mode: %s; back to the %s", reason, p["fallback"])
        self.wifi.discard_provisional()
        if p["fallback_ssid"]:
            self.wifi.handle("join", {"ssid": p["fallback_ssid"], "use_saved": True})
        else:
            self.setup_ap.manual = p["fallback_manual"]
            self.setup_ap.start()
        self.setup_ap.resume_policy()
        self.last = {"ok": False, "result": reason, "reverted_to": p["fallback"], "at": time.time()}
        self._clear()

    def _confirm(self, conn, req):
        p = self.pending
        if not p or p["phase"] != "awaiting confirmation":
            return {"error": "nothing to confirm"}
        if p["token_hash"]:
            token = conn.open(req["sealed"]).get("token", "") if req.get("sealed") else ""
            if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), p["token_hash"]):
                return {"error": "this change waits for its requester's confirmation (wrong token)"}
        if p.get("alongside") and conn.addr[0].rsplit(".", 1)[0] == AP_ADDR.rsplit(".", 1)[0]:
            # Joined next to the setup network: the point is reaching the Pi on the new one.
            return {"error": "confirm from %r, not over the setup network" % p["target"], "retry": True}
        self.confirmed.set()
        log.info("mode: confirmed from %s", conn.addr[0])
        return {"kept": p["target"]}
