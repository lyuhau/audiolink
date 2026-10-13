"""services/usbnet.py (stuck USB networking) and services/logs.py."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from audiolink.services import usbnet
from audiolink.services.logs import LogsService, prev_dir
from audiolink.services.usbnet import UsbNetWatch


class Watch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        os.makedirs(os.path.join(self.dir, "net/usb0/statistics"))
        os.makedirs(os.path.join(self.dir, "udc/3f980000.usb"))
        self.set(tx=0, state="configured", carrier=1)
        self.fixes = []
        self.w = UsbNetWatch(lambda: self.fixes.append(1), sys_net=self.dir + "/net",
                             sys_udc=self.dir + "/udc")
        self.w.started -= usbnet.SETTLE
        self.sends = 0                  # what a probe makes the interface send
        p = mock.patch.object(UsbNetWatch, "probe", self.fake_probe)
        p.start()
        self.addCleanup(p.stop)

    def set(self, tx=None, state=None, carrier=None):
        for path, v in (("net/usb0/statistics/tx_packets", tx), ("udc/3f980000.usb/state", state),
                        ("net/usb0/carrier", carrier)):
            if v is not None:
                with open(os.path.join(self.dir, path), "w") as f:
                    f.write("%s\n" % v)

    def fake_probe(self):
        before = self.w.tx()
        self.set(tx=before + self.sends)
        return self.w.tx() > before

    def test_stuck_interface_gets_one_fix(self):
        for _ in range(usbnet.PROBES * 3):
            self.w.check()
        self.assertEqual(self.fixes, [1])           # then GAP keeps it from repeating

    def test_healthy_interface_left_alone(self):
        self.sends = 1
        for _ in range(10):
            self.w.check()
        self.assertEqual(self.fixes, [])

    def test_not_while_the_host_is_asleep_or_gone(self):
        self.set(state="suspended")
        for _ in range(10):
            self.w.check()
        self.assertEqual(self.fixes, [])

    def test_traffic_resets_the_count(self):
        for i in range(10):
            self.set(tx=i * 5)                       # something went out since the last look
            self.w.check()
        self.assertEqual(self.fixes, [])


class Logs(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        with open(os.path.join(self.dir, "audiolink.log"), "w") as f:
            f.write("".join("line %d\n" % i for i in range(1000)))
        self.s = LogsService(self.dir, os.path.join(self.dir, "prev"))

    def test_tail(self):
        r = self.s.handle("tail", {"lines": 3})
        self.assertEqual(r["text"], "line 997\nline 998\nline 999")

    def test_only_known_files(self):
        with self.assertRaises(ValueError):
            self.s.handle("tail", {"file": "../../etc/shadow"})
        with self.assertRaises(ValueError):
            self.s.handle("tail", {"file": "prev/../../etc/shadow"})
        self.assertEqual(self.s.handle("list", {})["files"], ["audiolink.log"])

    def test_prev_dir(self):
        self.assertEqual(prev_dir({"APP_LOGS": "/media/mmcblk0p1/logs"}), "/media/mmcblk0p1/logs/prev")
        self.assertEqual(prev_dir({}), "/boot/logs/prev")          # the older image


if __name__ == "__main__":
    unittest.main()
