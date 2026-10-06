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


class AudioFormatService(Service):
    name = "audio"
    caps = ("audio_format",)

    def __init__(self, audio, gadget):
        self.audio = audio              # wire.alsa.AlsaAudio
        self.gadget = gadget            # wire.gadget.Gadget
        self.lock = threading.Lock()
        self.pending = None             # deadline (monotonic) for confirming the current format
        self.gone_since = None
        if gadget.available() and gadget.current() not in (None, BASELINE):
            # A restarted daemon: the gadget kept the last format. Start over in the baseline.
            log.info("audio: gadget left in %r; back to %r", gadget.current(), BASELINE)
            with self.lock:
                self._switch(BASELINE)

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
        if not ok:
            return
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
