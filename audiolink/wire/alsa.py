"""Pi-side audio: the UAC2 gadget through aplay/arecord."""
import logging
import subprocess
import threading
import time

from .protocol import CHANNELS, CHUNK, RATE

log = logging.getLogger("audiolink")


def run_alsa(link, device="hw:0,0"):
    """Pi: aplay/arecord on the UAC2 gadget. Both exit when the host stops
    streaming, so each is restarted in a loop."""
    import fcntl
    fmt = ["-D", device, "-f", "S16_LE", "-r", str(RATE), "-c", str(CHANNELS), "-t", "raw",
           "--buffer-time=40000", "--period-time=10000"]

    def player():
        while True:
            p = subprocess.Popen(["aplay", "-q"] + fmt + ["-"], stdin=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, bufsize=0)
            try:
                fcntl.fcntl(p.stdin.fileno(), 1031, 4096)  # F_SETPIPE_SZ: keep latency low
            except OSError:
                pass
            try:
                while True:
                    p.stdin.write(link.pull(CHUNK))
            except OSError:
                pass
            p.kill()
            p.wait()
            link.stats["aplay_restarts"] += 1
            time.sleep(0.5)

    def recorder():
        while True:
            p = subprocess.Popen(["arecord", "-q"] + fmt + ["-"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, bufsize=0)
            while True:
                data = p.stdout.read(CHUNK)
                if not data:
                    break
                link.feed(data)
            p.wait()
            link.stats["arecord_restarts"] += 1
            time.sleep(0.5)

    threading.Thread(target=player, daemon=True).start()
    threading.Thread(target=recorder, daemon=True).start()
