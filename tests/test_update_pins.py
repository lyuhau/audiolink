"""services/update.py (code over the link) and services/button.py (the reset pins)."""
import base64
import hashlib
import io
import os
import shutil
import tempfile
import unittest
import zipfile

from audiolink.services.button import LONG, SHORT, ResetPins, gpio_path
from audiolink.services.update import UpdateService

from .helpers import wait


def pyz(text="print('hi')"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("__main__.py", text)
    return buf.getvalue()


class Update(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.restarts = []
        self.u = UpdateService(self.dir, restart=lambda: self.restarts.append(1))
        self.path = os.path.join(self.dir, "audiolink.pyz")

    def send(self, data, digest=None, chunk=7):
        self.u.handle("begin", {"size": len(data), "sha256": digest or hashlib.sha256(data).hexdigest()})
        for off in range(0, len(data), chunk):
            self.u.handle("chunk", {"offset": off, "data": base64.b64encode(data[off:off + chunk]).decode()})
        return self.u.handle("finish", {})

    def test_installs_and_restarts(self):
        data = pyz()
        self.send(data)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), data)
        wait(lambda: self.restarts)
        self.assertEqual(self.u.status()["update_hash"], hashlib.sha256(data).hexdigest())

    def test_damaged_upload_changes_nothing(self):
        with self.assertRaisesRegex(ValueError, "damaged"):
            self.send(pyz(), digest="0" * 64)
        self.assertFalse(os.path.exists(self.path))

    def test_not_a_pyz(self):
        with self.assertRaisesRegex(ValueError, "not an audiolink"):
            self.send(b"just some bytes")
        self.assertFalse(os.path.exists(self.path))

    def test_install_clears_set_aside_and_revert_removes(self):
        self.send(pyz())
        open(self.path + ".bad", "w").close()
        self.send(pyz("print('v2')"))
        self.assertFalse(os.path.exists(self.path + ".bad"))
        self.u.handle("revert", {})
        self.assertFalse(os.path.exists(self.path))

    def test_chunk_without_begin(self):
        with self.assertRaises(ValueError):
            self.u.handle("chunk", {"offset": 0, "data": ""})


class Pins(unittest.TestCase):
    def setUp(self):
        self.shorted = False
        self.done, self.lights = [], []
        self.p = ResetPins(lambda: self.done.append("revert"), lambda: self.done.append("start over"),
                           read=lambda: self.shorted, led=self.lights.append)

    def hold(self, seconds, start=100.0):
        self.shorted = True
        t = start
        while t <= start + seconds:
            self.p.step(t)
            t += 0.25
        self.shorted = False
        self.p.step(t)

    def test_brief_touch_does_nothing(self):
        self.hold(2)
        self.assertEqual(self.done, [])
        self.assertEqual(self.lights, [])

    def test_short_hold_reverts(self):
        self.hold(SHORT + 1)
        self.assertEqual(self.done, ["revert"])
        self.assertEqual(self.lights, ["slow", "off"])

    def test_long_hold_starts_over(self):
        self.hold(LONG + 1)
        self.assertEqual(self.done, ["start over"])
        self.assertEqual(self.lights, ["slow", "fast", "off"])

    def test_gpio_path_finds_the_bcm_chip(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        for name, label, base in (("gpiochip504", "raspberrypi-exp-gpio", 504), ("gpiochip512", "pinctrl-bcm2835", 512)):
            os.makedirs(os.path.join(d, name))
            for f, v in (("label", label), ("base", base)):
                with open(os.path.join(d, name, f), "w") as fh:
                    fh.write("%s\n" % v)
        os.makedirs(os.path.join(d, "gpio533"))
        open(os.path.join(d, "gpio533", "value"), "w").close()
        self.assertEqual(gpio_path(21, d), os.path.join(d, "gpio533", "value"))


if __name__ == "__main__":
    unittest.main()
