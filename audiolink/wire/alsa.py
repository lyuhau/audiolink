"""Pi-side audio: the UAC2 gadget through aplay/arecord."""
import logging
import subprocess
import threading
import time

from .fmt import BASELINE

log = logging.getLogger("audiolink")


class AlsaAudio:
    """aplay/arecord on the UAC2 gadget in one format. Both exit when the host stops
    streaming, so each is restarted in a loop until stop()."""

    def __init__(self, link, device="hw:0,0"):
        self.link = link
        self.device = device
        self.format = BASELINE
        self.gen = 0                    # bumped by stop(): loops of older generations exit
        self.procs = set()
        self.lock = threading.Lock()

    def start(self, fmt=None):
        self.format = fmt or self.format
        self.link.set_format(self.format)
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

    def _args(self):
        f = self.format
        return ["-D", self.device, "-f", "S16_LE", "-r", str(f.rate), "-c", str(f.channels),
                "-t", "raw", "--buffer-time=40000", "--period-time=10000"]

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
        chunk = self.format.chunk
        while gen == self.gen:
            p = self._spawn(gen, ["aplay", "-q"] + self._args() + ["-"], stdin=subprocess.PIPE)
            if p is None:
                return
            try:
                fcntl.fcntl(p.stdin.fileno(), 1031, max(4096, chunk))  # F_SETPIPE_SZ: keep latency low
            except OSError:
                pass
            try:
                while gen == self.gen:
                    p.stdin.write(self.link.pull(chunk))
            except (OSError, ValueError):
                pass
            self._reap(p)
            if gen == self.gen:
                self.link.stats["aplay_restarts"] += 1
                time.sleep(0.5)

    def _recorder(self, gen):
        chunk = self.format.chunk
        while gen == self.gen:
            p = self._spawn(gen, ["arecord", "-q"] + self._args() + ["-"], stdout=subprocess.PIPE)
            if p is None:
                return
            while gen == self.gen:
                data = p.stdout.read(chunk)
                if not data:
                    break
                self.link.feed(data)
            self._reap(p)
            if gen == self.gen:
                self.link.stats["arecord_restarts"] += 1
                time.sleep(0.5)
