"""The setup network's lonely rule (services/setup_ap.py), against a fake radio and clock.
Each test states one rule from the module docstring."""
import unittest

from audiolink.services import setup_ap
from audiolink.services.setup_ap import GRACE, LONELY, OFFLINE, RETRY, RETRY_BUSY, SetupApService

from .helpers import FakeClock, patch_time


class FakeWifi:
    """WifiService as setup_ap sees it."""

    def __init__(self):
        self.channel = 1                # None: not on a network
        self.parked = False
        self.scans = 0
        self.in_range = []              # saved networks a scan finds

    def freq(self):
        return 2407 + 5 * self.channel if self.channel else None

    def park(self):
        if self.channel:
            return False
        self.parked = True
        return True

    def unpark(self):
        self.parked = False
        if self.in_range:
            self.channel = 3            # wpa_supplicant joins the saved network in range

    def scan(self):
        self.scans += 1
        return [{"ssid": s, "freq": 2412, "saved": True} for s in self.in_range]


class FakePresence:
    def __init__(self, clock):
        self.clock, self.wifi_clients, self.since = clock, 0, clock.t

    def connect(self):
        self.wifi_clients += 1

    def disconnect(self):
        self.wifi_clients -= 1
        self.since = self.clock.t

    def lonely_for(self):
        return 0 if self.wifi_clients else self.clock.t - self.since

    def connected_except(self, path):
        return self.wifi_clients > 0


class Identity:
    def pi_name(self):
        return "test-pi"


class Radio(SetupApService):
    """The real rule, with the radio mechanics faked."""

    def __init__(self, wifi, presence):
        super().__init__(wifi, Identity(), presence)
        self.ups, self.on_it, self.dead = [], [], []

    def _bring_up(self, channel):
        self.ups.append(channel)
        return True

    def _take_down(self):
        pass

    def pick_channel(self):
        return 6

    def stations(self):
        return self.on_it

    def dead_parts(self):
        return self.dead


class LonelyRule(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        p = patch_time(setup_ap, self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.wifi = FakeWifi()
        self.presence = FakePresence(self.clock)
        self.ap = Radio(self.wifi, self.presence)

    def run_for(self, seconds):
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.ap._tick()
            self.clock.advance(5)

    def test_opens_when_lonely_next_to_wifi(self):
        self.presence.connect()
        self.run_for(LONELY * 3)
        self.assertFalse(self.ap.active)
        self.presence.disconnect()
        self.run_for(LONELY - 10)
        self.assertFalse(self.ap.active)
        self.run_for(15)
        self.assertTrue(self.ap.active)
        self.assertEqual(self.ap.channel, 1)            # the Wi-Fi network's channel
        self.assertFalse(self.wifi.parked)

    def test_closes_once_a_client_is_on_wifi_and_nobody_on_it(self):
        self.run_for(LONELY + 5)
        self.ap.on_it = ["aa:bb:cc:dd:ee:ff"]
        self.presence.connect()
        self.run_for(GRACE * 2)
        self.assertTrue(self.ap.active)                 # someone's on the setup network
        self.ap.on_it = []
        self.run_for(GRACE + 10)
        self.assertFalse(self.ap.active)

    def test_follows_the_wifi_channel(self):
        self.run_for(LONELY + 5)
        self.wifi.channel = 11
        self.run_for(10)
        self.assertTrue(self.ap.active)
        self.assertEqual(self.ap.channel, 11)

    def test_off_wifi_parks_and_uses_its_own_channel(self):
        self.wifi.channel = None
        self.run_for(LONELY + 5)
        self.assertTrue(self.ap.active)
        self.assertEqual(self.ap.channel, 6)
        self.assertTrue(self.wifi.parked)

    def test_does_not_park_wifi_that_is_only_changing_networks(self):
        self.run_for(LONELY + 5)
        self.wifi.channel = None                        # between networks for a moment
        self.run_for(10)
        self.wifi.channel = 11
        self.run_for(10)
        self.assertFalse(self.wifi.parked)
        self.assertEqual(self.ap.channel, 11)

    def test_retries_saved_networks_while_parked(self):
        self.wifi.channel = None
        self.run_for(LONELY + OFFLINE + 10)
        self.assertTrue(self.wifi.parked)
        self.wifi.in_range = ["Home"]
        self.run_for(RETRY + 10)
        self.assertFalse(self.wifi.parked)              # let loose: it joined
        self.assertEqual(self.ap.channel, 3)            # and the setup network followed

    def test_retries_even_with_someone_on_it_but_less_often(self):
        self.wifi.channel = None
        self.ap.on_it = ["aa:bb:cc:dd:ee:ff"]
        self.run_for(LONELY + OFFLINE + 10)
        self.wifi.in_range = ["Home"]
        self.run_for(RETRY + 10)
        self.assertTrue(self.wifi.parked)
        self.run_for(RETRY_BUSY)
        self.assertFalse(self.wifi.parked)

    def test_restarts_a_setup_network_whose_hostapd_died(self):
        self.run_for(LONELY + 5)
        self.ap.dead = ["hostapd"]
        self.run_for(5)
        self.ap.dead = []
        self.assertEqual(len(self.ap.ups), 2)
        self.assertTrue(self.ap.active)

    def test_off_wifi_restart_stays_parked_on_its_channel(self):
        self.wifi.channel = None
        self.run_for(LONELY + OFFLINE + 10)
        self.ap.dead = ["hostapd"]
        self.run_for(5)
        self.ap.dead = []
        self.assertTrue(self.ap.active)
        self.assertTrue(self.wifi.parked)
        self.assertEqual(self.ap.ups, [6, 6])

    def test_backs_off_when_a_part_keeps_failing(self):
        self.run_for(LONELY + 5)
        self.ap.dead = ["udhcpd"]                       # never recovers
        self.run_for(300)
        self.assertLessEqual(len(self.ap.ups), 1 + 3)   # not every 5 s: 0, 30, 120 s, then every 600 s

    def test_off_does_not_override_the_rule(self):
        self.run_for(LONELY + 5)
        self.ap.handle("off", {})
        self.assertFalse(self.ap.active)
        self.run_for(LONELY - 10)
        self.assertFalse(self.ap.active)                # closed for now...
        self.run_for(20)
        self.assertTrue(self.ap.active)                 # ...but still the safety net

    def test_paused_does_nothing(self):
        self.ap.pause_policy()
        self.run_for(LONELY * 3)
        self.assertFalse(self.ap.active)


if __name__ == "__main__":
    unittest.main()
