"""Small pure functions: Wi-Fi helpers, connect codes and records, bencode, formats."""
import unittest

from audiolink.services import wifi
from audiolink.services.setup_ap import channel_of
from audiolink.wire.fmt import BASELINE, CANDIDATES, AudioFormat


class Wifi(unittest.TestCase):
    def test_psk_ieee_802_11i_vector(self):
        self.assertEqual(wifi.derive_psk("IEEE", "password"),
                         "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e")

    def test_password_length(self):
        with self.assertRaises(ValueError):
            wifi.derive_psk("x", "short")

    def test_classify(self):
        for flags, sec in (("[WPA2-PSK-CCMP][ESS]", "wpa2"), ("[WPA2-PSK+SAE-CCMP][ESS]", "wpa2/wpa3"),
                           ("[WPA2-SAE-CCMP][ESS]", "wpa3"), ("[WPA2-EAP-CCMP][ESS]", "enterprise"),
                           ("[RSN--OWE-CCMP][ESS]", "owe"), ("[WEP][ESS]", "wep"), ("[ESS]", "open")):
            self.assertEqual(wifi.classify(flags), sec, flags)

    def test_unescape_ssid(self):
        self.assertEqual(wifi.unescape_ssid("caf\\xc3\\xa9"), "café")
        self.assertEqual(wifi.unescape_ssid("plain"), "plain")

    def test_credentials_keep_no_wpa2_password(self):
        ent = wifi.credentials("Home", "wpa2", "password1")
        self.assertEqual(set(ent), {"security", "psk"})
        self.assertEqual(wifi.credentials("Cafe", "open", None), {"security": "open"})
        with self.assertRaises(ValueError):
            wifi.credentials("Home", "wpa2", "")

    def test_channel_of(self):
        self.assertEqual(channel_of(2412), 1)
        self.assertEqual(channel_of(2462), 11)
        self.assertIsNone(channel_of(5180))
        self.assertIsNone(channel_of(None))


class Formats(unittest.TestCase):
    def test_json_round_trip_and_sizes(self):
        for f in [BASELINE] + list(CANDIDATES):
            self.assertEqual(AudioFormat.from_json(f.to_json()), f)
            payload, window = f.link_params()
            self.assertLessEqual(payload, 4096)
            self.assertGreater(window, 0)


if __name__ == "__main__":
    unittest.main()
