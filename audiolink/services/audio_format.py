"""The Pi's side of audio-format negotiation (the host drives it: host/audio_format.py).

Every link starts in the BASELINE format. Over the link that's up, the host asks:
  audio.switch {format, confirm_within}  reply first, then re-present the USB gadget
                                         in that format; unless confirmed in time,
                                         go back to the baseline by ourselves (early,
                                         if nothing arrives from the host meanwhile)
  audio.confirm                          keep the current format
  audio.revert                           back to the baseline now
  audio.train {up, down}                 known data both ways, so the host can check
                                         the new format end to end and time it
The HELLO carries {"audio": {"format", "pending"}}. A confirmed format also falls
back to the baseline once no host has been seen for HOST_GONE seconds, so the next
host (perhaps one that can't run it) always meets the Pi in the baseline.

A restarted daemon (after a crash, or a dev redeploy) finds the gadget still in the
last format, with the host most likely still using it. It keeps that format, pending
like a switch (the host checks it and confirms), instead of re-presenting the gadget
at once: rebinding the USB controller several times in a row can wedge it. Not if it
did that less than ADOPT_AGAIN seconds ago: the format may be what keeps crashing it.
"""
import base64
import hashlib
import logging
import os
import threading
import time

from . import Service
from ..wire.fmt import BASELINE, CANDIDATES, AudioFormat

log = logging.getLogger("audiolink")

HOST_GONE = 15.0
MAX_CONFIRM = 120
SILENT_REVERT = 6.0             # after a switch, revert early if the host stays silent this long
ADOPT_CONFIRM = 30.0            # a restarted daemon's kept format: the host has this long to confirm
ADOPT_AGAIN = 120               # ...and it keeps one at most once in this long (seconds of uptime)
ADOPTED = "/run/audiolink-adopted"


def _uptime():
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def _may_adopt():
    """Once per ADOPT_AGAIN: a daemon that crashed right after keeping a format starts
    over in the baseline."""
    try:
        now = _uptime()
        try:
            with open(ADOPTED) as f:
                if now - float(f.read()) < ADOPT_AGAIN:
                    return False
        except (OSError, ValueError):
            pass
        with open(ADOPTED, "w") as f:
            f.write(str(now))
        return True
    except OSError:
        return False


class AudioFormatService(Service):
    name = "audio"
    caps = ("audio_format",)

    def __init__(self, audio, gadget):
        self.audio = audio              # wire.alsa.AlsaAudio
        self.gadget = gadget            # wire.gadget.Gadget
        self.lock = threading.Lock()
        self.pending = None             # deadline (monotonic) for confirming the current format
        self.gone_since = None
        self.adopted = None             # (format, deadline): kept from the last run, see attach()
        current = gadget.current() if gadget.available() else None
        if current not in (None, BASELINE):
            if _may_adopt():
                log.info("audio: gadget still in %r; keeping it unless the host confirms within %.0fs",
                         current, ADOPT_CONFIRM)
                with self.lock:
                    self.audio.stop()
                    self.audio.start(current)
                    self.adopted = (current, time.monotonic() + ADOPT_CONFIRM)
                    self.pending = self.adopted[1]
            else:
                log.info("audio: gadget left in %r again; back to %r", current, BASELINE)
                with self.lock:
                    self._switch(BASELINE)

    def attach(self, session):
        super().attach(session)
        if self.adopted:
            threading.Thread(target=self._await_confirm, args=self.adopted, daemon=True).start()

    @property
    def format(self):
        return self.audio.format

    def hello(self):
        return {"audio": {"format": self.format.to_json(), "pending": self.pending is not None,
                          "switchable": self.gadget.available()}}

    def on_up(self):
        self.gone_since = None

    def on_down(self):
        self.gone_since = since = time.monotonic()

        def fallback():
            time.sleep(HOST_GONE)
            with self.lock:
                if self.gone_since == since and self.pending is None and self.format != BASELINE:
                    log.info("audio: no host for %.0fs; back to %r", HOST_GONE, BASELINE)
                    self._switch(BASELINE)
        threading.Thread(target=fallback, daemon=True).start()

    def handle(self, op, args):
        if op == "status":
            return dict(self.hello()["audio"], candidates=[f.to_json() for f in CANDIDATES])
        if op == "train":
            up = base64.b64decode(args.get("up", ""))
            down = os.urandom(min(int(args.get("down", 0)), 8 << 20))
            return {"up_sha": hashlib.sha256(up).hexdigest(), "down": base64.b64encode(down).decode(),
                    "down_sha": hashlib.sha256(down).hexdigest()}
        if op == "switch":
            if not self.gadget.available():
                raise ValueError("this Pi can't change its audio format (no gadget configfs)")
            fmt = AudioFormat.from_json(args["format"])
            within = min(float(args.get("confirm_within", 20)), MAX_CONFIRM)
            threading.Thread(target=self._switch_pending, args=(fmt, within), daemon=True).start()
            return {"switching": fmt.to_json(), "confirm_within": within}
        if op == "confirm":
            with self.lock:
                self.pending = None
            log.info("audio: %r confirmed", self.format)
            return {"format": self.format.to_json()}
        if op == "revert":
            threading.Thread(target=self._revert, daemon=True).start()
            return {"format": BASELINE.to_json()}
        return super().handle(op, args)

    def _switch_pending(self, fmt, within):
        time.sleep(0.5)                 # let the reply reach the host first
        with self.lock:
            deadline = self.pending = time.monotonic() + within
            log.info("audio: switching to %r; confirm within %.0fs", fmt, within)
            ok = self._switch(fmt)
        if ok:
            self._await_confirm(fmt, deadline)

    def _await_confirm(self, fmt, deadline):
        """Back to the baseline unless confirmed by the deadline (or early, if the host
        goes silent)."""
        since = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if self.pending != deadline:
                return                  # confirmed or reverted
            heard = max(since, self.session.link.last_rx)
            if time.monotonic() - heard > SILENT_REVERT:
                break                   # the host can't use this format: don't wait it out
        with self.lock:
            if self.pending == deadline:
                log.warning("audio: %r not confirmed; back to %r", fmt, BASELINE)
                self._switch(BASELINE)

    def _revert(self):
        time.sleep(0.5)
        with self.lock:
            log.info("audio: reverting to %r", BASELINE)
            self._switch(BASELINE)

    def _switch(self, want):
        """Call with self.lock held. Returns whether we're now in `want`; if the
        kernel refuses it, we end up in the baseline."""
        fmt = want
        self.audio.stop()
        try:
            self.gadget.set_format(fmt)
        except OSError as e:
            log.warning("audio: the gadget refused %r (%s); back to %r", fmt, e, BASELINE)
            fmt = BASELINE
            try:
                self.gadget.set_format(fmt)
            except OSError as e:
                log.error("audio: can't restore %r: %s", fmt, e)
        if fmt == BASELINE:
            self.pending = None
        if not self.gadget.wait_card():
            log.warning("audio: the gadget's sound card didn't come back")
        self.audio.start(fmt)
        return fmt == want
