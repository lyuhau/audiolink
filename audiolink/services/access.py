"""Pi side: who may do what over the network, decided in one place.

Levels, low to high (each endpoint op names the one it needs):
  public   anyone (hello, pair)
  paired   proof with the read secret: using and managing the Pi, reading its logs
  debug    root: a shell, updates; only while the debug switch allows it
  host     the USB link (control requests and link streams): everything, never checked

Routes, from the connection's addresses (route_of; anything it can't place counts as
the relay, the most restricted):
  usb      the host, over the audio link
  setup    the setup network (open Wi-Fi)
  lan      the Wi-Fi network the Pi is on
  relay    through the relay (from RELAY_SOURCE)

The debug switch (DebugService, "debug.set|status" from the host only) says how debug
access is proven:
  off      nobody (the default)
  paired   the read secret, on any route
  key      a signature by one of the keys the host gave (KEY_LABEL + op + nonce)
  none     nothing; LAN only, the setup network only if the host said so, never the relay
Every mode expires (DEFAULT_FOR, NONE_FOR). The state is in RAM unless the host asks
for it to be kept (state dir / debug.json); a damaged file reads as off.
"""
import logging
import re
import threading
import time
from pathlib import Path

from ..paths import load_state, save_state
from ..session import CLOCK_SANE
from . import Service
from .setup_ap import AP_ADDR

log = logging.getLogger("audiolink")

PUBLIC, PAIRED, DEBUG = "public", "paired", "debug"
ROUTES = ("usb", "setup", "lan", "relay")
MODES = ("off", "paired", "key", "none")
RELAY_SOURCE = "127.0.0.2"          # where relayed connections to local services come from
KEY_LABEL = b"audiolink-debug-v1\0"
DEFAULT_FOR = 24 * 3600
NONE_FOR = 3600
MAX_FOR = 7 * 24 * 3600
CHECK_EVERY = 5
RESAVE = 600                        # s between saves of a kept switch's time left

NOT_PAIRED = ("not_paired", "Not added to this Pi (run: audiolink add)")


def route_of(local, remote, wifi_ip=None):
    """The route of a connection to the Pi, from its local and remote addresses."""
    if remote == RELAY_SOURCE:
        return "relay"
    if local == AP_ADDR:
        return "setup"
    if wifi_ip and local == wifi_ip:
        return "lan"
    return "relay"                  # fail closed


def decide(need, route, proven, switch, now):
    """None if allowed, else (code, message).
    need: PUBLIC | PAIRED | DEBUG; route: one of ROUTES; proven: what the request proved
    ({"paired", "key"}); switch: {"mode", "setup", "deadline"} (monotonic deadline)."""
    if route == "usb" or need == PUBLIC:
        return None
    if need == PAIRED:
        return None if "paired" in proven else NOT_PAIRED
    if need != DEBUG:
        return ("refused", "Refused")
    mode = switch.get("mode", "off")
    if mode == "off" or now >= switch.get("deadline", 0):
        return ("debug_off", "Debug access is off (on its host: audiolink debug on)")
    if mode == "paired":
        return None if "paired" in proven else NOT_PAIRED
    if mode == "key":
        return None if "key" in proven else ("debug_key", "Needs a debug key this Pi accepts (--key FILE)")
    if mode == "none":
        if route == "lan" or (route == "setup" and switch.get("setup")):
            return None
        return ("debug_route", "Debug access without proof isn't open %s" %
                ("through the relay" if route == "relay" else "on the setup network"))
    return ("debug_off", "Debug access is off")


def routes_for(switch):
    """The network routes debug access is open on (for status)."""
    mode = switch.get("mode", "off")
    if mode in ("paired", "key"):
        return ["lan", "setup", "relay"]
    if mode == "none":
        return ["lan", "setup"] if switch.get("setup") else ["lan"]
    return []


def parse_duration(spec):
    """'90s', '30m', '2h', '1d' -> seconds. Raises ValueError."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd])\s*", str(spec).lower())
    if not m:
        raise ValueError("durations look like 30m, 2h or 1d")
    s = float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    if not 0 < s <= MAX_FOR:
        raise ValueError("between a second and %d days" % (MAX_FOR // 86400))
    return int(s)


def parse_key(text):
    """An ed25519 public key: 'ssh-ed25519 AAAA... [comment]' or 64 hex digits -> 32 bytes."""
    import base64
    import struct
    text = text.strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return bytes.fromhex(text)
    parts = text.split()
    if len(parts) >= 2 and parts[0] == "ssh-ed25519":
        try:
            blob = base64.b64decode(parts[1])
            n = struct.unpack(">I", blob[:4])[0]
            kind = blob[4:4 + n]
            m = struct.unpack(">I", blob[4 + n:8 + n])[0]
            key = blob[8 + n:8 + n + m]
        except (ValueError, struct.error):
            raise ValueError("damaged ssh-ed25519 key")
        if kind == b"ssh-ed25519" and len(key) == 32:
            return key
    raise ValueError("not an ed25519 public key (ssh-ed25519 AAAA... or 64 hex digits)")


class DebugService(Service):
    """The debug switch. Only the host changes it (link requests)."""
    name = "debug"
    caps = ("debug",)

    def __init__(self, state_dir=None):
        self.path = Path(state_dir) / "debug.json" if state_dir else None
        self.lock = threading.Lock()
        self.listeners = []             # fn(reason) when debug access ends (off, expiry)
        self.state = {"mode": "off"}
        self.shells = lambda: 0         # () -> open network shells (services/shell.py)
        self.saved = time.monotonic()
        self._load()

    # ---- for the endpoint and the shell
    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def on_end(self, fn):
        self.listeners.append(fn)

    def keys(self):
        return [bytes.fromhex(k) for k in self.snapshot().get("keys", [])]

    # ---- requests from the host (the link: trusted)
    def handle(self, op, args):
        if op == "status":
            return self.status()
        if op == "set":
            return self.set(args)
        return super().handle(op, args)

    def status(self):
        s = self.snapshot()
        mode = s.get("mode", "off")
        left = max(0, int(s.get("deadline", 0) - time.monotonic())) if mode != "off" else 0
        return {"mode": mode, "routes": routes_for(s), "expires_in": left, "keys": len(s.get("keys", [])),
                "setup": bool(s.get("setup")), "persist": bool(s.get("persist")), "shells": self.shells()}

    def set(self, args):
        mode = args.get("mode", "off")
        if mode not in MODES:
            raise ValueError("debug modes: %s" % ", ".join(MODES))
        if mode == "off":
            self.off("turned off by the host")
            return self.status()
        keys = [parse_key(k).hex() for k in args.get("keys") or []]
        if mode == "key" and not keys:
            raise ValueError("key mode needs at least one public key")
        secs = int(args.get("for") or (NONE_FOR if mode == "none" else DEFAULT_FOR))
        if not 0 < secs <= MAX_FOR:
            raise ValueError("between a second and %d days" % (MAX_FOR // 86400))
        state = {"mode": mode, "keys": keys if mode == "key" else [], "setup": bool(args.get("setup")),
                 "for": secs, "deadline": time.monotonic() + secs, "persist": bool(args.get("persist"))}
        with self.lock:
            self.state = state
        self._save(state)
        log.info("debug: on (%s%s) for %ds%s", mode, ", setup network too" if state["setup"] else "",
                 secs, ", kept across restarts" if state["persist"] else "")
        return self.status()

    def off(self, why):
        with self.lock:
            was = self.state.get("mode", "off")
            self.state = {"mode": "off"}
        self._save(self.state)
        if was != "off":
            log.info("debug: off (%s)", why)
        for fn in self.listeners:
            fn(why)

    # ---- expiry
    def run(self):
        threading.Thread(target=self._loop, name="debug", daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(CHECK_EVERY)
            self.check()

    def check(self):
        s = self.snapshot()
        if s.get("mode", "off") == "off":
            return
        now = time.monotonic()
        if now >= s.get("deadline", 0):
            self.off("expired")
        elif s.get("persist") and now - self.saved >= RESAVE:
            self._save(s)               # so a restart without a clock can't start the time over

    # ---- storage (only with persist; the wall clock may not be known at boot)
    def _now(self):
        session = getattr(self, "session", None)
        return session.now() if session else time.time()

    def _save(self, state):
        if not self.path:
            return
        if not state.get("persist"):
            if not self._stored():
                return
            data = {"mode": "off"}
        else:
            now = self._now()
            left = state["deadline"] - time.monotonic()
            data = {k: state[k] for k in ("mode", "keys", "setup", "for")}
            data.update(persist=True, left=int(left), until=int(now + left) if now > CLOCK_SANE else None)
        try:
            save_state(self.path, data)
            self.saved = time.monotonic()
        except (OSError, ValueError) as e:
            log.warning("debug: couldn't save the switch: %s", e)

    def _stored(self):
        try:
            return (load_state(self.path) or {}).get("mode", "off") != "off"
        except Exception:
            return True

    def _load(self):
        if not self.path:
            return
        try:
            d = load_state(self.path) or {}
            mode = d.get("mode", "off")
            if mode == "off" or mode not in MODES or not d.get("persist"):
                return
            secs = int(d["for"])
            now = time.time()
            # Clock known: what's left by it; unknown (no NTP yet): at most what was left.
            left = min(secs, int(d["until"] - now) if d.get("until") and now > CLOCK_SANE else int(d["left"]))
            keys = [parse_key(k).hex() for k in d.get("keys") or []]
            if left <= 0 or not 0 < secs <= MAX_FOR or (mode == "key" and not keys):
                return
            self.state = {"mode": mode, "keys": keys if mode == "key" else [], "setup": bool(d.get("setup")),
                          "for": secs, "deadline": time.monotonic() + left, "persist": True}
            log.info("debug: on (%s) from the saved switch, %ds left", mode, left)
        except Exception as e:          # damaged: off
            log.warning("debug: saved switch unreadable (%s); off", e)
            self.state = {"mode": "off"}
