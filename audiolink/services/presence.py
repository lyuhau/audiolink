"""Pi side: is any client connected? (the setup network's lonely rule, services/setup_ap.py)

Counts connections from clients that reach the Pi over a network, by the Pi address
they connected to: its Wi-Fi address ("wifi") or the setup network's ("setup"). The
USB host doesn't count (it can manage the Pi, but says nothing about whether anyone
else can reach it), nor does the Pi itself (loopback).

The Pi can't know what situation its clients are in, so "nobody connected" is all
it goes by: a client that lost its internet and one the Pi lost look the same.
"""
import collections
import threading
import time

from .setup_ap import AP_ADDR


class Presence:
    def __init__(self, wifi_ip):
        self.lock = threading.Lock()
        self.open = collections.Counter()       # path -> open connections
        self.last = time.monotonic()            # last time anyone was connected (start: nobody)
        self.wifi_ip = wifi_ip                  # () -> the Pi's Wi-Fi address, or None

    def path_of(self, sock):
        try:
            local = sock.getsockname()[0]
        except OSError:
            return None
        if local == AP_ADDR:
            return "setup"
        return "wifi" if local == self.wifi_ip() else None

    def enter(self, sock):
        """A connection arrived; returns its path (pass it to leave()), or None if it
        doesn't count."""
        path = self.path_of(sock)
        if path:
            with self.lock:
                self.open[path] += 1
                self.last = time.monotonic()
        return path

    def leave(self, path):
        if path:
            with self.lock:
                self.open[path] -= 1
                self.last = time.monotonic()

    def touch(self, sock):
        """A connection that came and went at once (e.g. turned away)."""
        self.leave(self.enter(sock))

    def connected(self, path=None):
        """Anyone connected (through `path`, or any path)?"""
        with self.lock:
            return any(n > 0 for p, n in self.open.items() if path in (None, p))

    def connected_except(self, path):
        with self.lock:
            return any(n > 0 for p, n in self.open.items() if p != path)

    def lonely_for(self):
        """Seconds since anyone was connected; 0 while someone is."""
        with self.lock:
            return 0 if any(n > 0 for n in self.open.values()) else time.monotonic() - self.last
