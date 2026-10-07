"""Pi-side Wi-Fi management through wpa_supplicant (wpa_cli), driven by the host.

Like a phone: a network is saved once its password is proven to work (a join or a
trial, services/wifi_trial.py), and saved networks auto-join when in range, most
recently used first. A network whose password worked in a trial but that the Pi
never got onto (services/wifi_remote.py: the switch failed) stays saved, so nobody
retypes its password, but ranks last. Saved entries: credentials and "last_used" (a
counter: higher = joined more recently; 0 = never). A counter, not a time: the Pi
has no clock of its own, so after a reboot it may think it's 1970.
The store keeps the WPA pre-shared key derived from (password, SSID), never the
password itself.
"""
import binascii
import hashlib
import inspect
import logging
import os
import re
import subprocess
import threading
import time

from . import Service, proc
from ..paths import load_json, save_json

log = logging.getLogger("audiolink")

IFACE = "wlan0"
SUPPLICANT_PID = "/run/wpa_supplicant.pid"         # wlan0's (pi/init); the trial runs its own
SUPPLICANT = ["wpa_supplicant", "-B", "-P", SUPPLICANT_PID, "-i", IFACE, "-c", "/etc/wpa_supplicant/wpa_supplicant.conf"]
DHCP = ["udhcpc", "-i", IFACE, "-b", "-t", "10", "-s", "/usr/share/udhcpc/default.script",
        "-x", "hostname:audiopi"]


def unescape_ssid(s):
    """wpa_cli prints non-ASCII SSID bytes as \\xNN escapes; turn them back into text."""
    if "\\x" not in s:
        return s
    raw = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s).encode("latin-1")
    return raw.decode("utf-8", errors="replace")


def derive_psk(ssid, password):
    """WPA2-PSK: PBKDF2-HMAC-SHA1(password, ssid, 4096 rounds, 32 bytes)."""
    if not 8 <= len(password) <= 63:
        raise ValueError("WPA passwords are 8-63 characters")
    return hashlib.pbkdf2_hmac("sha1", password.encode(), ssid.encode(), 4096, 32).hex()


CHECK_HOST, CHECK_PATH = "captive.apple.com", "/hotspot-detect.html"


def connectivity(timeout=6, source=None, resolve=None):
    """Captive-portal check, the way Apple devices do it: fetch a page that says
    "Success" on the open internet. Anything else (a redirect or another page)
    means a portal is in the way. Redirects are not followed.
    source/resolve: check through a particular network (wifi_trial.py): connect from
    that source address, to the address resolve(CHECK_HOST) returns."""
    import http.client
    try:
        target = resolve(CHECK_HOST) if resolve else CHECK_HOST
        conn = http.client.HTTPConnection(target, 80, timeout=timeout,
                                          source_address=(source, 0) if source else None)
        conn.request("GET", CHECK_PATH, headers={"Host": CHECK_HOST,
                                                 "User-Agent": "CaptiveNetworkSupport wispr"})
        r = conn.getresponse()
        body = r.read(65536).decode("latin-1", "replace")
        conn.close()
    except (OSError, http.client.HTTPException) as e:
        return {"internet": "offline", "detail": str(e)}
    if r.status == 200 and "<title>success</title>" in body.lower():
        return {"internet": "online"}
    url = r.getheader("Location") if 300 <= r.status < 400 else None
    if not url:
        m = re.search(r"""url=['"]?([^'" >]+)""", body, re.I) or \
            re.search(r"""(?:href|action)=['"](https?://[^'"]+)""", body, re.I)
        url = m.group(1) if m else None
    return {"internet": "portal", "portal_url": url or "http://%s%s" % (CHECK_HOST, CHECK_PATH)}


def classify(flags):
    """wpa_supplicant scan flags -> open | owe | wpa2 | wpa3 | wpa2/wpa3 | wep | enterprise."""
    if "EAP" in flags:
        return "enterprise"
    if "OWE" in flags:
        return "owe"
    sae, psk = "SAE" in flags, "PSK" in flags
    if sae and psk:
        return "wpa2/wpa3"
    if sae:
        return "wpa3"
    if psk:
        return "wpa2"
    if "WEP" in flags:
        return "wep"
    return "open"


def credentials(ssid, security, password):
    """Saved-entry fields for a network. WPA2 keeps only the derived key; WPA3 (SAE)
    can't work from a derived key, so it has to keep the password itself."""
    if security in ("open", "owe"):
        return {"security": security}
    if not password:
        raise ValueError("%s networks need a password" % security.upper())
    if security in ("wpa2", "wpa2/wpa3"):
        return {"security": security, "psk": derive_psk(ssid, password)}
    if security == "wpa3":
        return {"security": "wpa3", "sae_password": password}
    raise ValueError("%s networks aren't supported" % security)


def security_of(ent):
    """Saved entries from before security was recorded: psk -> wpa2, none -> open."""
    return ent.get("security") or ("wpa2" if ent.get("psk") else "open")


class Supplicant:
    """One wpa_supplicant instance, through wpa_cli: the Pi's Wi-Fi (wlan0), or a
    short-lived trial connection on a second interface (services/wifi_trial.py)."""

    def __init__(self, iface=IFACE, ctrl=None):
        self.iface, self.ctrl = iface, ctrl

    def wpa(self, *args):
        cmd = ["wpa_cli"] + (["-p", self.ctrl] if self.ctrl else []) + ["-i", self.iface]
        r = subprocess.run(cmd + [str(a) for a in args], capture_output=True, text=True, timeout=10)
        out = r.stdout.strip()
        if r.returncode != 0 or out.startswith("FAIL"):
            raise RuntimeError("wpa_cli %s: %s" % (args[0], out or r.stderr.strip() or "failed"))
        return out

    def status(self):
        st = {}
        for line in self.wpa("status").splitlines():
            k, _, v = line.partition("=")
            st[k] = v
        return st

    def networks(self):
        """{ssid: network id} for networks configured in wpa_supplicant."""
        nets = {}
        for line in self.wpa("list_networks").splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) >= 2:
                nets[parts[1]] = int(parts[0])
        return nets

    def flags(self, nid):
        for line in self.wpa("list_networks").splitlines()[1:]:
            parts = line.split("\t")
            if parts[0] == str(nid):
                return parts[3] if len(parts) > 3 else ""
        return ""

    def add(self, ssid, ent, priority):
        nid = int(self.wpa("add_network").splitlines()[-1])
        self.wpa("set_network", nid, "ssid", binascii.hexlify(ssid.encode()).decode())
        sec = security_of(ent)
        if sec == "open":
            self.wpa("set_network", nid, "key_mgmt", "NONE")
        elif sec == "owe":
            self.wpa("set_network", nid, "key_mgmt", "OWE")
            self.wpa("set_network", nid, "ieee80211w", 2)
        elif sec in ("wpa2", "wpa2/wpa3"):
            self.wpa("set_network", nid, "psk", ent["psk"])
        elif sec == "wpa3":
            self.wpa("set_network", nid, "key_mgmt", "SAE")
            self.wpa("set_network", nid, "ieee80211w", 2)
            # Hex form: any bytes work, including quotes, newlines and non-ASCII.
            self.wpa("set_network", nid, "sae_password",
                     binascii.hexlify(ent["sae_password"].encode()).decode())
        if ent.get("hidden"):
            self.wpa("set_network", nid, "scan_ssid", 1)
        self.wpa("set_network", nid, "priority", priority)
        return nid

    def associate(self, nid, ssid, keyed, timeout):
        """Wait for network nid to connect. Returns (outcome, states seen), outcome
        "ok", "bad_key" (a handshake happened and failed), "refused" (wpa_supplicant
        gave up without ever associating) or "timeout"."""
        seen, deadline, prev = set(), time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            st = self.status()
            state = st.get("wpa_state")
            seen.add(state)
            if state == "COMPLETED" and st.get("ssid") == ssid:
                return "ok", seen
            # This chip checks the password in firmware: a wrong one shows up only as an
            # association that falls back to scanning (~5 s). Call it on the first one
            # rather than waiting ~20 s for wpa_supplicant's TEMP-DISABLED; a rare
            # transient failure just means re-submitting.
            if keyed and prev == "ASSOCIATING" and state in ("SCANNING", "DISCONNECTED"):
                return "bad_key", seen
            prev = state
            # wpa_supplicant temporarily disables a network after repeated failures: failed
            # handshakes (a wrong key) if it ever got as far as associating, else refusals.
            if "TEMP-DISABLED" in self.flags(nid):
                tried = seen & {"ASSOCIATING", "ASSOCIATED", "4WAY_HANDSHAKE", "GROUP_HANDSHAKE"}
                return ("bad_key" if keyed and tried else "refused"), seen
            time.sleep(0.5)
        return "timeout", seen

    def ip(self):
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", self.iface], capture_output=True,
                             text=True).stdout
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None


def _next_use(saved):
    """The next "last_used" value: above every one so far."""
    return max([e.get("last_used", 0) for e in saved.values()] + [0]) + 1


class WifiManager:
    def __init__(self, store_path):
        self.store_path = store_path
        self.sup = Supplicant()
        self.lock = threading.Lock()          # one operation at a time
        self.saved = self._load()
        self._scan_cache = (0.0, [])
        self.emit = lambda name, data=None: None    # set by the caller: sends events to the host
        self.internet = None                        # last connectivity state, with "ssid"
        self._last_demand = 0.0
        self.parked = False                         # off Wi-Fi with the setup network up (park/unpark)
        self.silent_checks = 0                      # monitor checks in a row wpa_supplicant didn't answer
        threading.Thread(target=self._load_into_supplicant, daemon=True).start()
        threading.Thread(target=self._monitor, daemon=True).start()

    # ---- store
    def _load(self):
        return load_json(self.store_path, {})

    def _save(self):
        save_json(self.store_path, self.saved, indent=1)

    def _restart_dhcp(self):
        subprocess.run(["killall", "-q", "udhcpc"])
        subprocess.run(["ip", "addr", "flush", "dev", IFACE])
        subprocess.run(DHCP, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _reprioritize(self):
        """Most recently used saved network gets the highest priority."""
        order = sorted(self.saved, key=lambda s: self.saved[s].get("last_used", 0))
        nets = self.sup.networks()
        for prio, ssid in enumerate(order, 1):
            if ssid in nets:
                self.sup.wpa("set_network", nets[ssid], "priority", prio)

    def _load_into_supplicant(self):
        for _ in range(60):                   # wpa_supplicant starts after us at boot
            try:
                self.sup.wpa("ping")
                break
            except Exception:
                time.sleep(1)
        else:
            log.warning("wifi: wpa_supplicant not running yet")
            return                            # the monitor keeps checking (_check_supplicant)
        with self.lock:
            self._sync_supplicant()
        log.info("wifi: %d saved network(s) loaded", len(self.saved))

    def _sync_supplicant(self):
        """Call with self.lock held: make sure wpa_supplicant knows every saved network
        (it may have started late, or been restarted)."""
        existing = self.sup.networks()
        missing = [s for s in self.saved if s not in existing]
        for ssid in missing:
            nid = self.sup.add(ssid, self.saved[ssid], 1)
            if self.parked:
                self.sup.wpa("disable_network", nid)
        if missing:
            self._reprioritize()
            if not self.parked:
                self.sup.wpa("enable_network", "all")
        return missing

    def _check_supplicant(self):
        """Call with self.lock held (monitor): wpa_supplicant running and up to date?"""
        try:
            self.sup.wpa("ping")
            self.silent_checks = 0
        except Exception:
            if not os.path.exists("/sys/class/net/" + IFACE):
                return                        # no Wi-Fi hardware (yet)
            self.silent_checks += 1
            if self.silent_checks < 3:        # it can be busy for a moment
                return
            log.warning("wifi: wpa_supplicant hasn't answered %d checks in a row; restarting it", self.silent_checks)
            self.silent_checks = 0
            proc.stop(SUPPLICANT_PID, wait=3)
            subprocess.run(SUPPLICANT, capture_output=True)
            time.sleep(2)
        missing = self._sync_supplicant()
        if missing:
            log.info("wifi: gave wpa_supplicant back %d saved network(s)", len(missing))

    # ---- connectivity monitor (portal logins expire; networks come and go)
    def _probe(self):
        st = self.sup.status()
        ip = self.sup.ip()
        if st.get("wpa_state") != "COMPLETED" or not ip:
            return {"internet": "offline", "ssid": None, "detail": "not connected"}
        return dict(connectivity(), ssid=st.get("ssid"))

    def _set_internet(self, state):
        prev, self.internet = self.internet, state
        if prev is None or (prev["internet"], prev.get("ssid")) != (state["internet"], state.get("ssid")):
            log.info("wifi: internet %s%s", state["internet"],
                     " on %r" % state["ssid"] if state.get("ssid") else "")
            self.emit("internet", state)

    def _monitor(self):
        time.sleep(5)
        while True:
            state = None
            if self.lock.acquire(blocking=False):   # skip while a join is in progress
                try:
                    self._check_supplicant()
                    state = self._probe()
                    self._set_internet(state)
                except Exception as e:
                    log.debug("wifi monitor: %s", e)
                finally:
                    self.lock.release()
            # Not online yet (e.g. DHCP still running at boot) or behind a portal: check sooner.
            time.sleep(60 if state and state["internet"] == "online" else 15)

    def announce(self):
        """Link-up hook: tell a (new) host the current state."""
        if self.internet:
            self.emit("internet", self.internet)

    def note_demand(self):
        """Something tried to use the Pi's internet (proxy request). If a portal is
        in the way, tell the host, at most once a minute."""
        now = time.monotonic()
        if self.internet and self.internet["internet"] == "portal" and now - self._last_demand > 60:
            self._last_demand = now
            self.emit("portal_needed", self.internet)

    # ---- saving without switching (services/wifi_remote.py)
    def remember(self, ssid, ent):
        """Save a network whose password was just proven, without preferring it."""
        with self.lock:
            self._remember(ssid, ent)

    def _remember(self, ssid, ent):
        prev = self.saved.get(ssid, {})
        self.saved[ssid] = dict(ent, last_used=prev.get("last_used", 0))
        self._save()
        if ssid not in self.sup.networks():
            nid = self.sup.add(ssid, self.saved[ssid], 1)
            if self.parked:
                self.sup.wpa("disable_network", nid)
        self._reprioritize()

    def last_scan(self):
        """(age in seconds, results) of the last scan; age is huge if there wasn't one."""
        at, results = self._scan_cache
        return (time.monotonic() - at if at else 1e9), [dict(n) for n in results]

    def current_ssid(self):
        try:
            st = self.sup.status()
        except Exception:
            return None
        return st.get("ssid") if st.get("wpa_state") == "COMPLETED" else None

    # ---- while the setup network is up (services/setup_ap.py)
    def associated(self):
        """True while connected to a Wi-Fi network as a client."""
        try:
            return self.sup.status().get("wpa_state") == "COMPLETED"
        except Exception:
            return False

    def ip(self):
        return self.sup.ip()

    def freq(self):
        """The current network's frequency (MHz), or None when not on one."""
        try:
            st = self.sup.status()
        except Exception:
            return None
        return int(st["freq"]) if st.get("wpa_state") == "COMPLETED" and st.get("freq") else None

    def park(self):
        """Off Wi-Fi with the setup network up: keep wpa_supplicant running but connect
        to nothing on our own (scans would disturb the setup network's clients, and
        joining a network on another channel would drag it along). join() still works
        while parked. Returns False, doing nothing, if a network was joined meanwhile."""
        with self.lock:
            if self.associated():               # decided on stale state (a join just finished)
                return False
            self.parked = True
            try:
                self.sup.wpa("disable_network", "all")
                self.sup.wpa("disconnect")
            except Exception as e:
                log.warning("wifi: park: %s", e)
            subprocess.run(["killall", "-q", "udhcpc"])
            subprocess.run(["ip", "addr", "flush", "dev", IFACE])
        self._set_internet({"internet": "offline", "ssid": None, "detail": "setup network up"})
        log.info("wifi: parked (setup network up)")
        return True

    def unpark(self):
        """Back to normal: saved networks auto-join again."""
        with self.lock:
            self.parked = False
            self._settle()
        log.info("wifi: back to the saved networks")

    def _settle(self):
        """After an operation: let saved networks auto-join, unless parked."""
        if self.parked:
            if not self.associated():
                self.sup.wpa("disable_network", "all")
            return
        self.sup.wpa("enable_network", "all")
        if not self.associated():
            self.sup.wpa("reassociate")
            self._restart_dhcp()

    # ---- operations (called from control requests)
    def handle(self, op, args):
        fn = {"scan": self.scan, "status": self.status, "list": self.list,
              "join": self.join, "forget": self.forget, "check": self.check}.get(op)
        if fn is None:
            raise ValueError("unknown wifi operation %r" % op)
        known = inspect.signature(fn).parameters
        extra = set(args) - set(known)
        if extra:                       # from a newer host: do what we can
            log.warning("wifi.%s: ignoring %s (from a newer host?)", op, ", ".join(sorted(extra)))
        with self.lock:
            return fn(**{k: v for k, v in args.items() if k in known})

    def scan(self, max_age=0):
        """Visible networks, strongest first; one entry per (name, security).
        max_age > 0 reuses results that are at most that many seconds old."""
        at, results = self._scan_cache
        if not (max_age and time.monotonic() - at <= max_age):
            for attempt in range(5):
                try:
                    self.sup.wpa("scan")
                    break
                except RuntimeError as e:       # FAIL-BUSY: a scan or connection attempt is running
                    if "BUSY" not in str(e) or attempt == 4:
                        raise
                    time.sleep(1)
            time.sleep(4)
            best = {}
            for line in self.sup.wpa("scan_results").splitlines()[1:]:
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                freq, signal, flags = int(parts[1]), int(parts[2]), parts[3]
                ssid = unescape_ssid(parts[4])
                if not ssid.strip("\x00"):
                    continue                  # hidden network (name not broadcast)
                key = (ssid, classify(flags))
                if key not in best or signal > best[key]["signal"]:
                    best[key] = {"ssid": ssid, "signal": signal, "freq": freq, "security": key[1]}
            results = sorted(best.values(), key=lambda n: -n["signal"])
            self._scan_cache = (time.monotonic(), results)
        return [dict(n, saved=n["ssid"] in self.saved) for n in results]

    def _scan_or_cached(self):
        try:
            return self.scan()
        except RuntimeError:
            return self._scan_cache[1]

    def check(self):
        state = self._probe()
        self._set_internet(state)
        return state

    def status(self):
        st = self.sup.status()
        connected = st.get("wpa_state") == "COMPLETED"
        ip = self.sup.ip()
        return dict({"state": st.get("wpa_state"), "ssid": st.get("ssid") if connected else None,
                     "freq": int(st["freq"]) if connected and st.get("freq") else None,
                     "ip": ip, "saved": sorted(self.saved)},
                    **(connectivity() if connected and ip else {"internet": "offline"}))

    def list(self):
        return [{"ssid": s, "last_used": e.get("last_used"), "security": security_of(e),
                 "hidden": bool(e.get("hidden"))}
                for s, e in sorted(self.saved.items(), key=lambda kv: -kv[1].get("last_used", 0))]

    def join(self, ssid, password=None, security=None, hidden=False, use_saved=False, timeout=30):
        """Try a network; save it (as the preferred one) only if association and DHCP
        succeed. use_saved=True reconnects with the stored credentials. Failures carry
        a code: not_found | auth_failed | timeout | no_ip."""
        if use_saved:
            if ssid not in self.saved:
                raise ValueError("%r is not a saved network" % ssid)
            ent = dict(self.saved[ssid])
        else:
            if not security:
                raise ValueError("security type required")
            ent = credentials(ssid, security, password)
            ent["hidden"] = bool(hidden)
        prev = self.sup.networks().get(ssid)
        if prev is not None:
            self.sup.wpa("remove_network", prev)
        nid = self.sup.add(ssid, ent, 1000)
        try:
            self.sup.wpa("select_network", nid)      # try only this one for now
            keyed = security_of(ent) not in ("open", "owe")
            outcome, seen = self.sup.associate(nid, ssid, keyed, timeout)
            ok, bad_key = outcome == "ok", outcome == "bad_key"
            ip = None
            if ok:
                self._restart_dhcp()
                for _ in range(30):
                    ip = self.sup.ip()
                    if ip:
                        break
                    time.sleep(0.5)
            if ok and ip:
                self.saved[ssid] = dict(ent, last_used=_next_use(self.saved))
                self._save()
                self._reprioritize()
                self.parked = False                 # on a network: saved ones may auto-join again
                self.sup.wpa("enable_network", "all")
                log.info("wifi: joined and saved %r (%s)", ssid, ip)
                conn = dict(connectivity(), ssid=ssid)
                self._set_internet(conn)
                return dict(conn, joined=True, ip=ip, security=security_of(ent))
        except Exception:
            self._undo_attempt(ssid, nid)           # never leave only the untried entry enabled
            raise
        self._undo_attempt(ssid, nid)
        sec = security_of(ent)
        if ok:
            code, reason = "no_ip", "associated but got no IP address (DHCP)"
        elif not ent.get("hidden") and not any(n["ssid"] == ssid for n in self._scan_or_cached()):
            code, reason = "not_found", "network not found (the Pi only sees 2.4 GHz networks)"
        elif bad_key or "4WAY_HANDSHAKE" in seen:
            code, reason = "auth_failed", "authentication failed (wrong password?)"
            if sec == "wpa3":
                reason += "; WPA3 on this Pi's Wi-Fi chip is unverified"
        else:
            code, reason = "timeout", "timed out associating"
        log.warning("wifi: join %r failed: %s (%s; states %s)", ssid, reason, outcome, ",".join(sorted(seen)))
        return {"joined": False, "ssid": ssid, "code": code, "reason": reason}

    def _undo_attempt(self, ssid, nid):
        """A join that didn't work: drop its entry and go back to the saved networks."""
        for step in (lambda: self.sup.wpa("remove_network", nid),
                     lambda: ssid in self.saved and self.sup.add(ssid, self.saved[ssid], 1),
                     self._reprioritize, self._settle):
            try:
                step()
            except Exception as e:
                log.warning("wifi: undoing a join of %r: %s", ssid, e)

    def forget(self, ssid):
        if ssid not in self.saved:
            raise ValueError("%r is not a saved network" % ssid)
        del self.saved[ssid]
        self._save()
        nid = self.sup.networks().get(ssid)
        if nid is not None:
            self.sup.wpa("remove_network", nid)
        return {"forgotten": ssid}


class WifiService(Service):
    """Pi side: Wi-Fi management requests ("wifi.<op>") from the host, and
    connectivity events ("internet", "portal_needed") pushed to it."""
    name = "wifi"
    caps = ("wifi",)

    def __init__(self, store_path):
        self.manager = WifiManager(store_path)

    def attach(self, session):
        super().attach(session)
        self.manager.emit = session.send_event

    def on_up(self):
        threading.Thread(target=self.manager.announce, daemon=True).start()

    def handle(self, op, args):
        return self.manager.handle(op, args)

    # ---- for other services (setup_ap, wifi_remote, discovery, presence, proxy)
    def ip(self):
        """The Pi's address on its Wi-Fi network, or None."""
        return self.manager.ip()

    def freq(self):
        return self.manager.freq()

    def current_ssid(self):
        return self.manager.current_ssid()

    def scan(self):
        return self.manager.handle("scan", {})

    def last_scan(self):
        return self.manager.last_scan()

    def join(self, **args):
        return self.manager.handle("join", args)

    def saved_entry(self, ssid):
        """A copy of a saved network's entry, or None."""
        ent = self.manager.saved.get(ssid)
        return dict(ent) if ent is not None else None

    def saved_names(self):
        return sorted(self.manager.saved)

    def remember(self, ssid, ent):
        self.manager.remember(ssid, ent)

    @property
    def parked(self):
        return self.manager.parked

    def park(self):
        return self.manager.park()

    def unpark(self):
        self.manager.unpark()

    def note_demand(self):
        self.manager.note_demand()

    def connection(self):
        """{"ip", "ssid", "internet"} as last seen (no network calls)."""
        state = self.manager.internet or {}
        return {"ip": self.manager.ip(), "ssid": state.get("ssid"),
                "internet": state.get("internet", "offline")}
