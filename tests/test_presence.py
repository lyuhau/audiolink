"""services/presence.py: who's connected, from the kernel's TCP table."""
import os
import tempfile
import unittest

from audiolink.services import presence
from audiolink.services.presence import Presence, connections

from .helpers import FakeClock, patch_time

# The Pi listens on :22 (ssh), :2222 (a forwarded port) and :47900 (the endpoint).
# 192.168.1.154 is its Wi-Fi address, 192.168.4.1 the setup network's, 10.55.0.2 USB.
HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def row(local, remote, state):
    def enc(addr):
        ip, port = addr.split(":")
        a, b, c, d = (int(x) for x in ip.split("."))
        return "%02X%02X%02X%02X:%04X" % (d, c, b, a, int(port))
    return "   0: %s %s %s 00000000:00000000 00:00000000 00000000     0        0 1\n" % (enc(local), enc(remote), state)


LISTENING = row("0.0.0.0:22", "0.0.0.0:0", "0A") + row("0.0.0.0:2222", "0.0.0.0:0", "0A") + \
    row("0.0.0.0:47900", "0.0.0.0:0", "0A")


class Table(unittest.TestCase):
    def test_parses_and_keeps_only_inbound(self):
        text = HEADER + LISTENING + \
            row("192.168.1.154:2222", "192.168.1.20:50000", "01") + \
            row("192.168.1.154:41000", "17.253.1.1:80", "01")       # the Pi's own outbound check
        self.assertEqual(connections(text), [(("192.168.1.154", 2222), ("192.168.1.20", 50000))])


class Counting(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        p = patch_time(presence, self.clock)
        p.start()
        self.addCleanup(p.stop)
        fd, self.path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(os.remove, self.path)
        self.p = Presence(lambda: "192.168.1.154", table=self.path)

    def table(self, *rows):
        with open(self.path, "w") as f:
            f.write(HEADER + LISTENING + "".join(rows))

    def test_counts_wifi_and_setup_but_not_usb_or_closed(self):
        self.table(row("192.168.1.154:22", "192.168.1.20:5000", "01"),
                   row("192.168.4.1:47900", "192.168.4.10:5001", "01"),
                   row("10.55.0.2:22", "10.55.0.1:5002", "01"),             # USB: doesn't count
                   row("192.168.1.154:22", "192.168.1.21:5003", "06"))      # TIME_WAIT: over
        self.assertEqual(self.p.counts(), {"wifi": 1, "setup": 1, "relay": 0})
        self.assertTrue(self.p.connected_except("setup"))

    def test_relayed_connections_count_but_the_usb_hosts_loopback_doesnt(self):
        self.table(row("127.0.0.1:22", "127.0.0.2:5000", "01"),          # through the relay
                   row("127.0.0.1:2222", "127.0.0.1:5001", "01"))        # USB host's stream
        self.assertEqual(self.p.counts(), {"wifi": 0, "setup": 0, "relay": 1})
        self.assertTrue(self.p.connected_except("setup"))

    def test_lonely_counts_from_the_last_connection(self):
        self.table(row("192.168.1.154:2222", "192.168.1.20:5000", "01"))
        self.assertEqual(self.p.lonely_for(), 0)
        self.table()
        self.clock.advance(30)
        self.assertEqual(self.p.lonely_for(), 30)


if __name__ == "__main__":
    unittest.main()
