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
        for name in ("device.toml",):
            with open(os.path.join(REPO, "config", name)) as f:
                text = f.read()
            vid = int(re.search(r"^idVendor = (0x[0-9a-fA-F]+)", text, re.M).group(1), 16)
            pid = int(re.search(r"^idProduct = (0x[0-9a-fA-F]+)", text, re.M).group(1), 16)
            self.assertIn((vid, pid), usb_audio.USB_IDS, "config/%s: add its ID to USB_IDS" % name)


class Mac(unittest.TestCase):
    def test_by_name_unless_ambiguous(self):
        from unittest import mock
        from audiolink.wire import coreaudio
        names = {93: "Playback Inactive", 91: "Capture Inactive", 76: "MacBook Air Speakers",
                 10: "USB Audio", 11: "USB Audio"}
        ids = {93: (0x1d6b, 0x0104), 91: (0x1d6b, 0x0104), 10: (0x1234, 1), 11: (0x5678, 2)}
        with mock.patch.object(coreaudio, "devices", lambda: list(names)), \
                mock.patch.object(coreaudio, "_string", lambda d, sel: names[d]), \
                mock.patch.object(coreaudio, "usb_id", lambda d: ids.get(d)):
            lookup = coreaudio.usb_ids()
        self.assertEqual(lookup(dev("Capture Inactive")), (0x1d6b, 0x0104))
        self.assertIsNone(lookup(dev("MacBook Air Speakers")))
        self.assertIsNone(lookup(dev("USB Audio")))         # two devices, two IDs: can't tell
        self.assertIsNone(lookup(dev("Not There")))

    def test_model_uid(self):
        from audiolink.wire import coreaudio
        m = coreaudio.MODEL_IDS.search("Topology Control:1D6B:0104")
        self.assertEqual((m.group(1), m.group(2)), ("1D6B", "0104"))
        self.assertIsNone(coreaudio.MODEL_IDS.search("Digital Mic"))


class Linux(unittest.TestCase):
    def test_cards_by_hw_number(self):
        with tempfile.TemporaryDirectory() as proc:
            for card, usbid in (("1", "1d6b:0104\n"), ("3", "046d:0a44\n")):
                os.makedirs(os.path.join(proc, "card" + card))
                with open(os.path.join(proc, "card" + card, "usbid"), "w") as f:
                    f.write(usbid)
            os.makedirs(os.path.join(proc, "card0"))            # built-in: no usbid
            ids = usb_audio.linux_ids(proc)
            self.assertEqual(ids(dev("AudioPi: USB Audio (hw:1,0)")), (0x1d6b, 0x0104))
            self.assertEqual(ids(dev("Logitech Headset: USB Audio (hw:3,0)")), (0x046d, 0x0a44))
            self.assertIsNone(ids(dev("bcm2835 Headphones: - (hw:0,0)")))
            self.assertIsNone(ids(dev("default")))


if __name__ == "__main__":
    unittest.main()
