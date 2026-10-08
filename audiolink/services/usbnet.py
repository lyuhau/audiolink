"""Pi side: notice when USB networking is stuck, and re-present the gadget.

Seen in the field: after a boot, the network half of the gadget stayed up on both
sides but the Pi never sent a packet (tx 0, rx thousands) while audio worked; only
re-presenting the gadget brought it back. The Pi checks itself: when it hasn't sent
anything on usb0 for a while, it sends one datagram to the host and looks whether the
packet counter moved (an ARP request counts too). PROBES failures in a row, with the
host connected, and it re-presents the gadget (as an audio format change does). Not
more than once per GAP: rapid rebinds have wedged the USB controller before.
"""
import logging
import socket
import threading
import time

log = logging.getLogger("audiolink")

IFACE = "usb0"
HOST = "10.55.0.1"
EVERY = 30                  # s between looks
PROBES = 3                  # failed probes in a row before acting
SETTLE = 120                # s after start before acting (the host may still be enumerating)
GAP = 600                   # s between re-presents


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


class UsbNetWatch:
    def __init__(self, represent, iface=IFACE, host=HOST, sys_net="/sys/class/net",
                 sys_udc="/sys/class/udc"):
        self.represent = represent      # () -> None: re-present the gadget
        self.iface, self.host = iface, host
        self.sys_net, self.sys_udc = sys_net, sys_udc
        self.failures = 0
        self.started = time.monotonic()
        self.last_fix = None
        self.last_tx = None

    def run(self):
        threading.Thread(target=self._loop, name="usbnet", daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(EVERY)
            try:
                self.check()
            except Exception:
                log.exception("usbnet: check failed")

    def tx(self):
        v = _read("%s/%s/statistics/tx_packets" % (self.sys_net, self.iface))
        return int(v) if v and v.isdigit() else None

    def host_attached(self):
        import glob
        states = [_read(p) for p in glob.glob("%s/*/state" % self.sys_udc)]
        return "configured" in states and _read("%s/%s/carrier" % (self.sys_net, self.iface)) == "1"

    def probe(self):
        """Send one datagram to the host; True if the interface sent something."""
        before = self.tx()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(b"audiolink usbnet probe", (self.host, 9))
        except OSError:
            pass
        time.sleep(2)
        after = self.tx()
        return before is not None and after is not None and after > before

    def check(self):
        tx = self.tx()
        moved, self.last_tx = tx != self.last_tx, tx
        if tx is None or not self.host_attached() or moved:
            self.failures = 0
            return
        if self.probe():
            self.failures = 0
            return
        self.failures += 1
        log.info("usbnet: nothing goes out on %s (%d/%d)", self.iface, self.failures, PROBES)
        now = time.monotonic()
        if self.failures < PROBES or now - self.started < SETTLE:
            return
        if self.last_fix is not None and now - self.last_fix < GAP:
            return
        log.warning("usbnet: %s stuck; re-presenting the gadget", self.iface)
        self.last_fix = now
        self.failures = 0
        self.represent()
