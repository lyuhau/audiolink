"""Pi-side Wi-Fi management through wpa_supplicant (wpa_cli), driven by the host.

Like a phone: a network is saved only after a successful join, saved networks
auto-join when in range, most recently used first. The store keeps the WPA
pre-shared key derived from (password, SSID), never the password itself.
"""
import binascii
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time

log = logging.getLogger("audiolink")

IFACE = "wlan0"
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


def connectivity(timeout=6):
    """Captive-portal check, the way Apple devices do it: fetch a page that says
    "Success" on the open internet. Anything else (a redirect or another page)
    means a portal is in the way. Redirects are not followed."""
    import http.client
    try:
        conn = http.client.HTTPConnection(CHECK_HOST, 80, timeout=timeout)
        conn.request("GET", CHECK_PATH, headers={"User-Agent": "CaptiveNetworkSupport wispr"})
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


class WifiManager:
    def __init__(self, store_path):
        self.store_path = store_path
        self.lock = threading.Lock()          # one operation at a time
        self.saved = self._load()
        self._scan_cache = (0.0, [])
        threading.Thread(target=self._load_into_supplicant, daemon=True).start()

    # ---- store
    def _load(self):
        try:
            with open(self.store_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
        tmp = self.store_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.saved, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.store_path)

    # ---- wpa_cli
    def _wpa(self, *args):
        r = subprocess.run(["wpa_cli", "-i", IFACE] + [str(a) for a in args],
                           capture_output=True, text=True, timeout=10)
        out = r.stdout.strip()
        if r.returncode != 0 or out.startswith("FAIL"):
            raise RuntimeError("wpa_cli %s: %s" % (args[0], out or r.stderr.strip() or "failed"))
        return out

    def _status(self):
        st = {}
        for line in self._wpa("status").splitlines():
            k, _, v = line.partition("=")
            st[k] = v
        return st

    def _networks(self):
        """{ssid: network id} for networks configured in wpa_supplicant."""
        nets = {}
        for line in self._wpa("list_networks").splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) >= 2:
                nets[parts[1]] = int(parts[0])
        return nets

    def _flags(self, nid):
        for line in self._wpa("list_networks").splitlines()[1:]:
            parts = line.split("\t")
            if parts[0] == str(nid):
                return parts[3] if len(parts) > 3 else ""
        return ""

    def _add(self, ssid, ent, priority):
        nid = int(self._wpa("add_network").splitlines()[-1])
        self._wpa("set_network", nid, "ssid", binascii.hexlify(ssid.encode()).decode())
        sec = security_of(ent)
        if sec == "open":
            self._wpa("set_network", nid, "key_mgmt", "NONE")
        elif sec == "owe":
            self._wpa("set_network", nid, "key_mgmt", "OWE")
            self._wpa("set_network", nid, "ieee80211w", 2)
        elif sec in ("wpa2", "wpa2/wpa3"):
            self._wpa("set_network", nid, "psk", ent["psk"])
        elif sec == "wpa3":
            self._wpa("set_network", nid, "key_mgmt", "SAE")
            self._wpa("set_network", nid, "ieee80211w", 2)
            # Hex form: any bytes work, including quotes, newlines and non-ASCII.
            self._wpa("set_network", nid, "sae_password",
                      binascii.hexlify(ent["sae_password"].encode()).decode())
        if ent.get("hidden"):
            self._wpa("set_network", nid, "scan_ssid", 1)
        self._wpa("set_network", nid, "priority", priority)
        return nid

    def _ip(self):
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", IFACE], capture_output=True,
                             text=True).stdout
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None

    def _restart_dhcp(self):
        subprocess.run(["killall", "-q", "udhcpc"])
        subprocess.run(["ip", "addr", "flush", "dev", IFACE])
        subprocess.run(DHCP, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _reprioritize(self):
        """Most recently used saved network gets the highest priority."""
        order = sorted(self.saved, key=lambda s: self.saved[s].get("last_used", 0))
        nets = self._networks()
        for prio, ssid in enumerate(order, 1):
            if ssid in nets:
                self._wpa("set_network", nets[ssid], "priority", prio)

    def _load_into_supplicant(self):
        for _ in range(60):                   # wpa_supplicant starts after us at boot
            try:
                self._wpa("ping")
                break
            except Exception:
                time.sleep(1)
        else:
            log.warning("wifi: wpa_supplicant not running; Wi-Fi management unavailable")
            return
        with self.lock:
            existing = self._networks()
            for ssid, ent in self.saved.items():
                if ssid not in existing:
                    self._add(ssid, ent, 1)
            self._reprioritize()
            self._wpa("enable_network", "all")
        log.info("wifi: %d saved network(s) loaded", len(self.saved))

    # ---- operations (called from control requests)
    def handle(self, op, args):
        fn = {"scan": self.scan, "status": self.status, "list": self.list,
              "join": self.join, "forget": self.forget, "check": self.check}.get(op)
        if fn is None:
            raise ValueError("unknown wifi operation %r" % op)
        with self.lock:
            return fn(**args)

    def scan(self, max_age=0):
        """Visible networks, strongest first; one entry per (name, security).
        max_age > 0 reuses results that are at most that many seconds old."""
        at, results = self._scan_cache
        if not (max_age and time.monotonic() - at <= max_age):
            self._wpa("scan")
            time.sleep(4)
            best = {}
            for line in self._wpa("scan_results").splitlines()[1:]:
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

    def check(self):
        if not self._ip():
            return {"internet": "offline", "detail": "no IP address on Wi-Fi"}
        return connectivity()

    def status(self):
        st = self._status()
        connected = st.get("wpa_state") == "COMPLETED"
        ip = self._ip()
        return dict({"state": st.get("wpa_state"), "ssid": st.get("ssid") if connected else None,
                     "freq": int(st["freq"]) if connected and st.get("freq") else None,
                     "ip": ip, "saved": sorted(self.saved)},
                    **(connectivity() if connected and ip else {"internet": "offline"}))

    def list(self):
        return [{"ssid": s, "last_used": e.get("last_used"), "security": security_of(e),
                 "hidden": bool(e.get("hidden"))}
                for s, e in sorted(self.saved.items(), key=lambda kv: -kv[1].get("last_used", 0))]

    def join(self, ssid, password=None, security=None, hidden=False, use_saved=False, timeout=30):
        """Try a network; save it only if association and DHCP succeed.
        use_saved=True reconnects with the stored credentials. Failures carry a code:
        not_found | auth_failed | timeout | no_ip."""
        if use_saved:
            if ssid not in self.saved:
                raise ValueError("%r is not a saved network" % ssid)
            ent = dict(self.saved[ssid])
        else:
            if not security:
                raise ValueError("security type required")
            ent = credentials(ssid, security, password)
            ent["hidden"] = bool(hidden)
        prev = self._networks().get(ssid)
        if prev is not None:
            self._wpa("remove_network", prev)
        nid = self._add(ssid, ent, 1000)
        self._wpa("select_network", nid)      # try only this one for now
        seen, deadline = set(), time.monotonic() + timeout
        ok = bad_key = False
        while time.monotonic() < deadline:
            st = self._status()
            seen.add(st.get("wpa_state"))
            if st.get("wpa_state") == "COMPLETED" and st.get("ssid") == ssid:
                ok = True
                break
            # After failed handshakes wpa_supplicant temporarily disables the network.
            if "TEMP-DISABLED" in self._flags(nid):
                bad_key = True
                break
            time.sleep(0.5)
        ip = None
        if ok:
            self._restart_dhcp()
            for _ in range(30):
                ip = self._ip()
                if ip:
                    break
                time.sleep(0.5)
        if ok and ip:
            ent["last_used"] = int(time.time())
            self.saved[ssid] = ent
            self._save()
            self._reprioritize()
            self._wpa("enable_network", "all")
            log.info("wifi: joined and saved %r (%s)", ssid, ip)
            return dict({"joined": True, "ssid": ssid, "ip": ip, "security": security_of(ent)},
                        **connectivity())

        # Failed: drop the attempt and go back to the saved networks.
        self._wpa("remove_network", nid)
        if ssid in self.saved:
            self._add(ssid, self.saved[ssid], 1)
            self._reprioritize()
        self._wpa("enable_network", "all")
        self._wpa("reassociate")
        self._restart_dhcp()
        sec = security_of(ent)
        if ok:
            code, reason = "no_ip", "associated but got no IP address (DHCP)"
        elif not ent.get("hidden") and not any(n["ssid"] == ssid for n in self.scan()):
            code, reason = "not_found", "network not found (the Pi only sees 2.4 GHz networks)"
        elif bad_key or "4WAY_HANDSHAKE" in seen:
            code, reason = "auth_failed", "authentication failed (wrong password?)"
            if sec == "wpa3":
                reason += "; WPA3 on this Pi's Wi-Fi chip is unverified"
        else:
            code, reason = "timeout", "timed out associating"
        log.warning("wifi: join %r failed: %s", ssid, reason)
        return {"joined": False, "ssid": ssid, "code": code, "reason": reason}

    def forget(self, ssid):
        if ssid not in self.saved:
            raise ValueError("%r is not a saved network" % ssid)
        del self.saved[ssid]
        self._save()
        nid = self._networks().get(ssid)
        if nid is not None:
            self._wpa("remove_network", nid)
        return {"forgotten": ssid}
