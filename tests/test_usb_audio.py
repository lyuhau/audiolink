"""Which audio devices are the Pi's (wire/usb_audio.py): by USB ID, else by name."""
import os
import re
import tempfile
import unittest

from audiolink.wire import usb_audio

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def dev(name, usb=None):
    return {"name": name, "usb": usb}


class PiDevices(unittest.TestCase):
    PI = (0x1d6b, 0x0104)

    def test_by_usb_id_whatever_the_name(self):
        renamed = dev("Speakers (My Link)", self.PI)
        other = dev("Speakers (AudioPi clone)", (0x1234, 0x0001))
        self.assertEqual(usb_audio.pi_devices([renamed, other, dev("MacBook Air Speakers")]), [renamed])

    def test_names_when_no_id_matches(self):
        # Windows (no IDs yet), or a card whose settings.txt changed the IDs
        named = dev("Speakers (AudioPi)")
        moved = dev("Playback Inactive", (0x1209, 0x0001))
        self.assertEqual(usb_audio.pi_devices([named, moved, dev("Speakers (USB DAC)")]), [named, moved])

    def test_annotate_survives_a_failing_lookup(self):
        def broken():
            raise OSError("no Core Audio")
        orig = usb_audio._lookup
        usb_audio._lookup = broken
        try:
            self.assertEqual(usb_audio.annotate([dev("AudioPi: USB Audio (hw:2,0)")])[0]["usb"], None)
        finally:
            usb_audio._lookup = orig

    def test_device_files_use_known_ids(self):
        """Every built-in flavor (config/flavors/*.toml; not local/, people's own)."""
        names = sorted(n for n in os.listdir(os.path.join(REPO, "config", "flavors")) if n.endswith(".toml"))
        self.assertIn("audiolink.toml", names)
        for name in names:
            with open(os.path.join(REPO, "config", "flavors", name)) as f:
                text = f.read()
            vid = int(re.search(r"^idVendor = (0x[0-9a-fA-F]+)", text, re.M).group(1), 16)
            pid = int(re.search(r"^idProduct = (0x[0-9a-fA-F]+)", text, re.M).group(1), 16)
            self.assertIn((vid, pid), usb_audio.USB_IDS, "config/flavors/%s: add its ID to USB_IDS" % name)


class ExtraIds(unittest.TestCase):
    """A fork's Pi with its own USB ID: host.json's usb_ids, or audiolink host --usb-id."""
    FORK = (0x1209, 0xabcd)

    def tearDown(self):
        usb_audio.set_extra_ids(None)

    def test_saved_ids(self):
        from unittest import mock
        from audiolink.host import config
        fork = dev("Speakers (My Link)", self.FORK)
        with mock.patch.object(config, "load", lambda strict=True: {"usb_ids": ["1209:abcd"]}):
            self.assertIn(self.FORK, usb_audio.known_ids())
            self.assertEqual(usb_audio.pi_devices([fork, dev("Speakers (USB DAC)", (0x1234, 1))]), [fork])
        self.assertEqual(usb_audio.known_ids(), usb_audio.USB_IDS | set())    # nothing saved: the built-in ones

    def test_flag_wins_over_saved(self):
        from unittest import mock
        from audiolink.host import config
        usb_audio.set_extra_ids(["1209:abcd"])
        with mock.patch.object(config, "load", lambda strict=True: {"usb_ids": ["1209:0001"]}):
            self.assertEqual(usb_audio.known_ids(), usb_audio.USB_IDS | {self.FORK})

    def test_added_while_running(self):
        """audiolink flavor: the running host finds the Pi as its new flavor too."""
        from unittest import mock
        from audiolink.host import config
        usb_audio.set_extra_ids(["1209:0001"])
        self.assertEqual(usb_audio.add_ids(["1209:abcd"]), ["1209:0001", "1209:abcd", "1d6b:0104"])
        usb_audio.set_extra_ids(None)
        with mock.patch.object(config, "load", lambda strict=True: {"usb_ids": ["1209:0001"]}):
            usb_audio.add_ids(["1209:abcd"])                     # on top of host.json's
        self.assertEqual(usb_audio.known_ids(), usb_audio.USB_IDS | {self.FORK, (0x1209, 1)})

    def test_flag_and_config_values(self):
        import argparse
        from audiolink import argtypes
        from audiolink.host import config
        self.assertEqual(argtypes.usb_id("1209:ABCD"), "1209:abcd")
        for bad in ("1209", "1209:abcde", "xyz:0001"):
            with self.assertRaises(argparse.ArgumentTypeError):
                argtypes.usb_id(bad)
        self.assertEqual(config.check({"usb_ids": ["1209:ABCD"]}), {"usb_ids": ["1209:abcd"]})
        with self.assertRaises(ValueError):
            config.check({"usb_ids": "1209:abcd"})                         # a list


class Mac(unittest.TestCase):
    def lookup(self, names, ids, uids):
        from unittest import mock
        from audiolink.wire import coreaudio
        strings = {**{(d, coreaudio.NAME): n for d, n in names.items()},
                   **{(d, coreaudio.UID): u for d, u in uids.items()}}
        with mock.patch.object(coreaudio, "devices", lambda: list(names)), \
                mock.patch.object(coreaudio, "_string", lambda d, sel: strings.get((d, sel))), \
                mock.patch.object(coreaudio, "usb_id", lambda d: ids.get(d)):
            return coreaudio.usb_ids()

    def test_by_position_then_name(self):
        names = {93: "Playback Inactive", 91: "Capture Inactive", 76: "MacBook Air Speakers",
                 10: "USB Audio", 11: "USB Audio"}
        ids = {93: (0x1d6b, 0x0104), 91: (0x1d6b, 0x0104), 10: (0x1234, 1), 11: (0x5678, 2)}
        uids = {93: "AppleUSBAudioEngine:audiolink:AudioPi:audiopi0001:1",
                91: "AppleUSBAudioEngine:audiolink:AudioPi:audiopi0001:2", 76: "BuiltInSpeakerDevice"}
        lookup = self.lookup(names, ids, uids)
        self.assertEqual(lookup(dev("Capture Inactive"), 1), ((0x1d6b, 0x0104), "AppleUSBAudioEngine:audiolink:AudioPi:audiopi0001"))
        self.assertEqual(lookup(dev("Capture Inactive")), ((0x1d6b, 0x0104), None))        # by name
        self.assertEqual(lookup(dev("MacBook Air Speakers"), 2), (None, None))
        self.assertEqual(lookup(dev("USB Audio")), (None, None))      # by name, two IDs: can't tell
        self.assertEqual(lookup(dev("USB Audio"), 4), ((0x5678, 2), None))  # by position: can
        self.assertEqual(lookup(dev("Not There"), 9), (None, None))

    def test_model_uid(self):
        from audiolink.wire import coreaudio
        m = coreaudio.MODEL_IDS.search("Topology Control:1D6B:0104")
        self.assertEqual((m.group(1), m.group(2)), ("1D6B", "0104"))
        self.assertIsNone(coreaudio.MODEL_IDS.search("Digital Mic"))


class Pairs(unittest.TestCase):
    """Real GameBuds and a Pi cloned from them, plugged in together: same USB ID, same
    names. Each USB device's output goes with its own input, never another's."""

    def test_pairs_by_usb_device(self):
        from unittest import mock
        from audiolink.wire import host_audio
        devs = [{"name": "Arctis GameBuds", "outputs": 2, "inputs": 0, "hostapi": "Core Audio", "hostapi_type": 5},
                {"name": "Arctis GameBuds", "outputs": 0, "inputs": 1, "hostapi": "Core Audio", "hostapi_type": 5},
                {"name": "Arctis GameBuds", "outputs": 2, "inputs": 0, "hostapi": "Core Audio", "hostapi_type": 5},
                {"name": "Arctis GameBuds", "outputs": 0, "inputs": 1, "hostapi": "Core Audio", "hostapi_type": 5}]
        where = ["real", "real", "clone", "clone"]
        for i, d in enumerate(devs):
            d["index"] = i
        with mock.patch("audiolink.wire.portaudio.devices", lambda: [dict(d) for d in devs]), \
                mock.patch("audiolink.wire.usb_audio._lookup",
                           lambda: (lambda d, pos=None: ((0x1038, 0x230a), where[pos]))), \
                mock.patch("audiolink.wire.usb_audio.known_ids", lambda: {(0x1038, 0x230a)}):
            pairs = host_audio.find_pairs()
        self.assertEqual([(o["index"], i["index"], k) for o, i, k in pairs], [(0, 1, "real"), (2, 3, "clone")])


class Linux(unittest.TestCase):
    def test_cards_by_hw_number(self):
        with tempfile.TemporaryDirectory() as proc:
            for card, usbid in (("1", "1d6b:0104\n"), ("3", "046d:0a44\n")):
                os.makedirs(os.path.join(proc, "card" + card))
                with open(os.path.join(proc, "card" + card, "usbid"), "w") as f:
                    f.write(usbid)
            os.makedirs(os.path.join(proc, "card0"))            # built-in: no usbid
            ids = usb_audio.linux_ids(proc)
            self.assertEqual(ids(dev("AudioPi: USB Audio (hw:1,0)")), ((0x1d6b, 0x0104), "card1"))
            self.assertEqual(ids(dev("Logitech Headset: USB Audio (hw:3,0)")), ((0x046d, 0x0a44), "card3"))
            self.assertEqual(ids(dev("bcm2835 Headphones: - (hw:0,0)")), (None, None))
            self.assertEqual(ids(dev("default")), (None, None))


if __name__ == "__main__":
    unittest.main()
