"""Pi-side audio: the UAC2 gadget through aplay/arecord."""
import logging
import subprocess
import threading
import time

from .fmt import BASELINE

log = logging.getLogger("audiolink")


RETRY = 0.5                     # s between restarts
IDLE_RETRY = 2.0                # at most, for aplay while the host isn't streaming
WAKE_CHECK = 0.1
# ALSA buffer and period (µs). aplay's buffer is a queue every byte to the host waits
# through: short. arecord's only needs room for a late read (its delay is the period).
PLAY_US = (10000, 2500)
RECORD_US = (40000, 2500)


class AlsaAudio:
    """aplay/arecord on the UAC2 gadget in one format. Both exit when the host isn't
    streaming (no sound card yet, or no USB transfers to move the data: ALSA gives up
    on a write or read that waits too long), so each is restarted in a loop until stop().

    Only a restart after the host was streaming counts in the link's stats
    (aplay_restarts, arecord_restarts): that's a fault; waiting for the host isn't.
    The host streams both ways at once, so arecord receiving is the sign that it is.
    While it isn't, aplay retries less often, and right away once arecord hears it."""

    def __init__(self, link, device="hw:0,0"):
        self.link = link
        self.device = device
        self.format = BASELINE          # aplay's: what the Pi sends (the gadget's p_*)
        self.record_format = None       # arecord's (c_*), if not the same (a device cloned from another)
        self.gen = 0                    # bumped by stop(): loops of older generations exit
        self.procs = set()
        self.lock = threading.Lock()
        self.heard = 0.0                # monotonic time arecord last got data from the host

    def start(self, fmt=None, record_fmt=None):
        """Run in `fmt` both ways, or `fmt` out and `record_fmt` in."""
        if fmt is not None:
            self.format, self.record_format = fmt, record_fmt
        self.link.set_format(self.format)       # what this side sends in
        gen = self.gen
        threading.Thread(target=self._player, args=(gen,), daemon=True).start()
        threading.Thread(target=self._recorder, args=(gen,), daemon=True).start()

    def stop(self):
        with self.lock:
            self.gen += 1
            procs, self.procs = self.procs, set()
        for p in procs:
            p.kill()
        for p in procs:
            p.wait()

    def _args(self, play):
        f = self.format if play else (self.record_format or self.format)
        buffer_us, period_us = PLAY_US if play else RECORD_US
        return ["-D", self.device, "-f", "S16_LE", "-r", str(f.rate), "-c", str(f.channels),
                "-t", "raw", "--buffer-time=%d" % buffer_us, "--period-time=%d" % period_us]

    def _spawn(self, gen, cmd, **kw):
        with self.lock:
            if gen != self.gen:
                return None
            p = subprocess.Popen(cmd, stderr=subprocess.DEVNULL, bufsize=0, **kw)
            self.procs.add(p)
            return p

    def _reap(self, p):
        p.kill()
        p.wait()
        with self.lock:
            self.procs.discard(p)

    def _player(self, gen):
        import fcntl
        f = self.format
        piece = max(f.frame_bytes, f.chunk // 4 // f.frame_bytes * f.frame_bytes)   # whole frames
        idle = 0                        # restarts in a row without the host streaming
        while gen == self.gen:
            p = self._spawn(gen, ["aplay", "-q"] + self._args(True) + ["-"], stdin=subprocess.PIPE)
            if p is None:
                return
            began = time.monotonic()
            try:
                fcntl.fcntl(p.stdin.fileno(), 1031, max(4096, piece))  # F_SETPIPE_SZ: a queue too
            except OSError:
                pass
            try:
                while gen == self.gen:
                    p.stdin.write(self.link.pull(piece))
            except (OSError, ValueError):
                pass
            self._reap(p)
            if gen != self.gen:
                return
            if self.heard >= began:
                self.link.stats["aplay_restarts"] += 1
                idle = 0
                time.sleep(RETRY)
            else:
                idle += 1
                self._wait_for_host(gen, min(IDLE_RETRY, RETRY * 2 ** (idle - 1)))

    def _wait_for_host(self, gen, timeout):
        """Sleep up to timeout, less if arecord starts hearing the host (or stop())."""
        since, end = time.monotonic(), time.monotonic() + timeout
        while gen == self.gen and self.heard < since and time.monotonic() < end:
            time.sleep(WAKE_CHECK)

    def _recorder(self, gen):
        chunk = self.format.chunk
        while gen == self.gen:
            p = self._spawn(gen, ["arecord", "-q"] + self._args(False) + ["-"], stdout=subprocess.PIPE)
            if p is None:
                return
            got = False
            while gen == self.gen:
                data = p.stdout.read(chunk)
                if not data:
                    break
                got = True
                self.heard = time.monotonic()
                try:
                    self.link.feed(data)
                except Exception:       # never let one bad chunk stop the Pi from hearing the host
                    log.exception("audio: receiving failed")
            self._reap(p)
            if gen == self.gen:
                if got:
                    self.link.stats["arecord_restarts"] += 1
                time.sleep(RETRY)
