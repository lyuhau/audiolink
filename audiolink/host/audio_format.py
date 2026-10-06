"""Host side of audio-format negotiation: move the link to a faster format and keep it
only if it works end to end (the Pi's side: services/audio_format.py).

Negotiation runs over the link itself. It always starts in the baseline, so the
two sides can always talk; for each candidate format, best first:
  1. ask the Pi to switch; it re-presents its USB audio in that format
     (the host's audio supervisor reopens the device in whatever format it reports)
  2. train: send known data both ways and time it
  3. if it arrived intact and is clearly faster than the baseline, confirm; otherwise
     revert. If anything goes wrong in between, the Pi reverts by itself.
Results are remembered per Pi (by board serial) in audio_formats.json, so the next
connection goes straight to the format that worked and skips ones that didn't.

`audiolink host --audio-format auto` (default) negotiates when a Pi connects;
`audiolink audio sweep` measures every candidate and keeps the fastest.
"""
import base64
import hashlib
import json
import logging
import os
import threading
import time

from ..paths import config_dir
from ..services import Service
from ..wire.fmt import BASELINE, CANDIDATES, AudioFormat

log = logging.getLogger("audiolink")

STORE = config_dir() / "audio_formats.json"
CONFIRM_WITHIN = 20             # the Pi reverts if we haven't confirmed by then
LINK_WAIT = 12                  # how long a new format gets to carry requests
MIN_GAIN = 1.25                 # a format must beat the baseline by this much to be kept


def parse_format(text):
    """'192000x8' -> AudioFormat; 'baseline' -> BASELINE."""
    if text == "baseline":
        return BASELINE
    rate, _, ch = text.lower().partition("x")
    try:
        return AudioFormat(int(rate), int(ch))
    except ValueError:
        raise ValueError("audio format must look like 192000x8 (rate x channels) or 'baseline'")


def key(fmt):
    return "%dx%d" % (fmt.rate, fmt.channels)


class _Store:
    def __init__(self, path=STORE):
        self.path = path

    def load(self, serial):
        try:
            return json.loads(self.path.read_text()).get(serial, {})
        except (OSError, ValueError):
            return {}

    def save(self, serial, entry):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        data[serial] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=1))


class AudioNegotiator(Service):
    def __init__(self, mode="auto"):
        self.mode = mode                # "auto", "baseline" or an AudioFormat
        self.store = _Store()
        self.lock = threading.Lock()
        self.running = False
        self.state = "idle"
        self.events = []                # [(time, message)] for `audiolink audio`

    def _note(self, msg, *args):
        msg = msg % args
        log.info("audio: %s", msg)
        self.events = self.events[-200:] + [(time.time(), msg)]

    # ---- triggers
    def on_peer_hello(self, info):
        audio = info.get("audio")
        if not audio or not audio.get("switchable") or self.mode == "baseline":
            return
        self.start(sweep=False, target=None if self.mode == "auto" else self.mode)

    def start(self, sweep, target=None):
        with self.lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(target=self._run, args=(sweep, target), daemon=True).start()
        return True

    def _run(self, sweep, target):
        try:
            self._negotiate(sweep, target)
        except Exception as e:
            self._note("negotiation stopped: %s", e)
        finally:
            self.state = "idle"
            self.running = False

    # ---- the Pi's state
    def _serial(self):
        return ((self.session.peer_info or {}).get("identity") or {}).get("serial", "unknown")

    def _status(self, timeout=5):
        st = self.session.request("audio.status", timeout=timeout)
        return AudioFormat.from_json(st["format"]), st["pending"]

    def _wait_for(self, fmt, timeout):
        """Until both ends run fmt and the link carries requests, or timeout."""
        end = time.monotonic() + timeout
        time.sleep(2)                   # the switch takes the device away first
        while time.monotonic() < end:
            if self.session.peer_alive and self.session.link.format == fmt:
                try:
                    now, pending = self._status(timeout=4)
                    if now == fmt:
                        return True
                    if now == BASELINE and not pending:
                        return False        # the Pi couldn't present fmt and is back already
                except (RuntimeError, TimeoutError):
                    pass
            time.sleep(1)
        return False

    def _train(self, fmt, timeout=60):
        """Known data both ways; returns goodput (bytes/s, both directions) or None."""
        n = max(192_000, min(fmt.bytes_per_sec, 3_000_000))     # ~1 s of the format each way
        up = os.urandom(n)
        stats = self.session.link.stats
        retx0, crc0 = stats["retx"], stats["crc_fail"]
        t0 = time.monotonic()
        try:
            r = self.session.request("audio.train", {"up": base64.b64encode(up).decode(), "down": n},
                                     timeout=timeout)
        except (RuntimeError, TimeoutError) as e:
            self._note("%r: training failed: %s", fmt, e)
            return None
        elapsed = time.monotonic() - t0
        down = base64.b64decode(r["down"])
        if r["up_sha"] != hashlib.sha256(up).hexdigest() or len(down) != n \
                or r["down_sha"] != hashlib.sha256(down).hexdigest():
            self._note("%r: training data corrupted", fmt)
            return None
        goodput = 2 * n / elapsed
        self._note("%r: %.0f KB/s goodput (%d KB each way in %.1fs, retx=%d crc_fail=%d)", fmt,
                   goodput / 1e3, n // 1000, elapsed, stats["retx"] - retx0, stats["crc_fail"] - crc0)
        return goodput

    def _switch(self, fmt):
        """Switch both ends to fmt and train; returns goodput or None (the Pi is then
        on its way back to the baseline)."""
        self.state = "trying %r" % fmt
        self._note("trying %r", fmt)
        self.session.request("audio.switch", {"format": fmt.to_json(), "confirm_within": CONFIRM_WITHIN},
                             timeout=10)
        deadline = time.monotonic() + CONFIRM_WITHIN
        if not self._wait_for(fmt, LINK_WAIT):
            self._note("%r: the link didn't come up; the Pi will revert", fmt)
            self._back_to_baseline(asked=False)
            return None
        # Train within the Pi's deadline (leaving time to confirm): too slow counts as failed.
        goodput = self._train(fmt, timeout=max(1, deadline - time.monotonic() - 3))
        if goodput is None:
            self._back_to_baseline()
        return goodput

    def _back_to_baseline(self, asked=True):
        if asked:
            try:
                self.session.request("audio.revert", timeout=5)
            except (RuntimeError, TimeoutError):
                pass                    # it reverts on its own at the deadline
        if not self._wait_for(BASELINE, CONFIRM_WITHIN + 15):
            raise RuntimeError("the Pi didn't come back in the baseline format")

    # ---- the procedure
    def _negotiate(self, sweep, target):
        serial = self._serial()
        mem = self.store.load(serial)
        failed = set(mem.get("failed", []))
        current, pending = self._status()
        if pending or (current != BASELINE and (sweep or target not in (None, current))):
            self.state = "reverting"
            self._note("back to the baseline first")
            self._back_to_baseline()
            current = BASELINE

        if target is not None:
            if target == current:
                self._note("already in %r", current)
                return
            order = [target]
        elif sweep:
            order = list(CANDIDATES)
            failed = set()
        else:
            best = mem.get("best") and AudioFormat.from_json(mem["best"])
            if best and best == current:
                self._note("already in %r", current)
                return
            order = ([best] if best else []) + [f for f in CANDIDATES if f != best and key(f) not in failed]
            if current != BASELINE:     # an earlier host left it in some other format
                self._back_to_baseline()
                current = BASELINE
        if target == BASELINE or not order:
            return

        self.state = "measuring the baseline"
        base = self._train(BASELINE)
        if base is None:
            raise RuntimeError("the baseline link isn't working")
        results = {}
        for fmt in order:
            if fmt == BASELINE:
                continue
            goodput = self._switch(fmt)
            if goodput is None or (target is None and goodput < base * MIN_GAIN):
                if goodput is not None:
                    self._note("%r isn't faster than the baseline (%.0f KB/s)", fmt, base / 1e3)
                    self._back_to_baseline()
                failed.add(key(fmt))
                if mem.get("best") == fmt.to_json():
                    mem["best"] = None
                continue
            results[fmt] = goodput
            if not sweep:
                break
            self._back_to_baseline()
        mem["failed"] = sorted(failed - {key(f) for f in results})
        if not results:
            self._note("staying in %r", BASELINE)
            mem["best"] = None
            self.store.save(serial, mem)
            return
        best = max(results, key=results.get)
        if sweep and self._switch(best) is None:
            self._note("%r stopped working; staying in %r", best, BASELINE)
            self.store.save(serial, mem)
            return
        self.session.request("audio.confirm", timeout=10)
        self._note("using %r: %.1fx the baseline", best, results[best] / base)
        mem.update(best=best.to_json(), goodput=round(results[best]), baseline_goodput=round(base))
        self.store.save(serial, mem)

    # ---- `audiolink audio` (local control ops)
    def local_ops(self):
        def status(args):
            info = (self.session.peer_info or {}).get("audio")
            if info and self.session.peer_alive:
                try:
                    info = self.session.request("audio.status", timeout=5)
                except (RuntimeError, TimeoutError):
                    pass
            return {"mode": self.mode if isinstance(self.mode, str) else key(self.mode),
                    "state": self.state, "running": self.running, "connected": self.session.peer_alive,
                    "pi": info, "link": self.session.link.format.to_json(),
                    "remembered": self.store.load(self._serial()),
                    "events": self.events[-int(args.get("events", 20)):]}

        def run(args):
            target = parse_format(args["format"]) if args.get("format") else None
            if not self.session.peer_alive:
                raise RuntimeError("the Pi is not connected")
            if not self.start(sweep=bool(args.get("sweep")), target=target):
                raise RuntimeError("a negotiation is already running")
            return {"started": True}

        def forget(args):
            self.store.save(self._serial(), {})
            return {}
        return {"audio.status": status, "audio.run": run, "audio.forget": forget}


def audio_command(a):
    from .control import call
    if a.action == "forget":
        call("audio.forget", port=a.control_port)
        print("forgot this Pi's audio formats; the next negotiation starts from scratch")
        return 0
    if a.action in ("sweep", "set"):
        args = {"sweep": a.action == "sweep"}
        if a.action == "set":
            if not a.format:
                raise SystemExit("usage: audiolink audio set RATExCHANNELS|baseline")
            args["format"] = a.format
        since = time.time()
        call("audio.run", args, port=a.control_port)
        while True:
            time.sleep(1)
            st = call("audio.status", {"events": 50}, port=a.control_port)
            for t, msg in st["events"]:
                if t >= since:
                    print(msg)
                    since = t + 1e-6
            if not st["running"]:
                break
    st = call("audio.status", port=a.control_port)
    link = AudioFormat.from_json(st["link"])
    print("link:  %r%s" % (link, "" if st["connected"] else " (Pi not connected)"))
    if st["pi"]:
        print("Pi:    %r%s" % (AudioFormat.from_json(st["pi"]["format"]),
                               ", awaiting confirmation" if st["pi"].get("pending") else ""))
    print("mode:  %s%s" % (st["mode"], ", " + st["state"] if st["running"] else ""))
    mem = st["remembered"]
    if mem.get("best"):
        print("best:  %r, %.0f KB/s goodput (baseline %.0f KB/s)" % (
            AudioFormat.from_json(mem["best"]), mem["goodput"] / 1e3, mem["baseline_goodput"] / 1e3))
    if mem.get("failed"):
        print("skip:  %s" % ", ".join(mem["failed"]))
    return 0
