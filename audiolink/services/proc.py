"""Pi side: small helpers for the daemons audiolink runs itself (hostapd, udhcpd, the
trial's wpa_supplicant), found through their pid files."""
import os
import time
from pathlib import Path


def pid_of(pidfile):
    try:
        return int(Path(pidfile).read_text().strip())
    except (OSError, ValueError):
        return None


def alive(pidfile):
    pid = pid_of(pidfile)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop(pidfile, wait=0.0):
    """SIGTERM the process; with wait, until it's gone (up to that many seconds)."""
    pid = pid_of(pidfile)
    if pid is None:
        return
    try:
        os.kill(pid, 15)
        end = time.monotonic() + wait
        while time.monotonic() < end:
            os.kill(pid, 0)
            time.sleep(0.1)
    except OSError:
        pass                            # gone
