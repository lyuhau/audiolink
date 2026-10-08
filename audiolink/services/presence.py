"""Pi side: is any client connected? (the setup network's lonely rule, services/setup_ap.py)

Asks the kernel rather than keeping count: a client is connected while there's an
established TCP connection to one of the Pi's listening ports on its Wi-Fi address
("wifi") or the setup network's ("setup"), from /proc/net/tcp. Whatever the
connection is for (a forwarded port, the network endpoint, ssh), and however it ended
(closed, reset, timed out by keepalive), the kernel's view is the truth; bookkeeping
in the code that serves connections could miss a close. Short requests that come and
go between two looks (the endpoint's) are reported with touch().

The USB host doesn't count (it can manage the Pi, but says nothing about whether
anyone else can reach it), nor does the Pi itself (loopback). The Pi can't know what
situation its clients are in, so "nobody connected" is all it goes by: a client that
lost its internet and one the Pi lost look the same.
"""
import socket
import struct
import threading
import time

from .setup_ap import AP_ADDR

TCP_TABLE = "/proc/net/tcp"
ESTABLISHED, LISTEN = "01", "0A"


def _addr(hexaddr):
    """'0100007F:0016' -> ('127.0.0.1', 22) (the kernel prints IPv4 in host byte order)."""
    ip, port = hexaddr.split(":")
    return socket.inet_ntoa(struct.pack("<I", int(ip, 16))), int(port, 16)


def connections(table_text):
    """[(local (ip, port), remote (ip, port))] of established connections to a
    listening port, from the text of /proc/net/tcp."""
    listening, established = set(), []
    for line in table_text.splitlines()[1:]:
        f = line.split()
        if len(f) < 4:
            continue
        local, remote = _addr(f[1]), _addr(f[2])
        if f[3] == LISTEN:
            listening.add(local[1])
        elif f[3] == ESTABLISHED:
            established.append((local, remote))
    return [(l, r) for l, r in established if l[1] in listening]


class Presence:
    def __init__(self, wifi_ip, table=TCP_TABLE):
        self.lock = threading.Lock()
        self.wifi_ip = wifi_ip                  # () -> the Pi's Wi-Fi address, or None
        self.table = table
        self.last = time.monotonic()            # last time anyone was seen (start: nobody)

    def counts(self):
        """{"wifi": n, "setup": n}: clients connected right now."""
        try:
            with open(self.table) as f:
                conns = connections(f.read())
        except OSError:
            conns = []
        ip = self.wifi_ip()
        n = {"wifi": 0, "setup": 0}
        for (lip, _), _ in conns:
            if lip == AP_ADDR:
                n["setup"] += 1
            elif ip and lip == ip:
                n["wifi"] += 1
        if any(n.values()):
            with self.lock:
                self.last = time.monotonic()
        return n

    def touch(self, sock):
        """A client reached the Pi just now over `sock` (for requests that may be over
        before the next look at the kernel's table). Only Wi-Fi and the setup network count."""
        try:
            local = sock.getsockname()[0]
        except OSError:
            return
        if local == AP_ADDR or local == self.wifi_ip():
            with self.lock:
                self.last = time.monotonic()

    def connected(self):
        return any(self.counts().values())

    def connected_except(self, path):
        return any(v for k, v in self.counts().items() if k != path)

    def lonely_for(self):
        """Seconds since anyone was connected; 0 while someone is."""
        if self.connected():
            return 0
        with self.lock:
            return time.monotonic() - self.last
