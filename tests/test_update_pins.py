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


def pyz(text="print('hi')", image=False):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("__main__.py", text)
        if image:
            z.writestr("audiolink/services/image.py", "")
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

    def test_card_with_slots_refuses_code_without_the_image_service(self):
        from audiolink.services.access import Refused
        self.u.slots = lambda: {"current": "a/", "slots": ["a/"]}
        with self.assertRaises(Refused) as e:          # an old program's own .pyz: refused, shown as is
            self.send(pyz())
        self.assertEqual(e.exception.code, "too_old")
        self.assertIn("get the new audiolink", str(e.exception))
        self.assertFalse(os.path.exists(self.path) or os.path.exists(self.path + ".new"))
        self.assertEqual(self.restarts, [])
        self.send(pyz(image=True))
        self.assertTrue(os.path.exists(self.path))

    def test_chunk_without_begin(self):
        with self.assertRaises(ValueError):
            self.u.handle("chunk", {"offset": 0, "data": ""})

    def test_revert_with_nothing_installed_doesnt_restart(self):
        self.assertTrue(self.u.handle("revert", {})["already"])
        self.assertEqual(self.restarts, [])
        self.u.handle("revert", {"restart": True})      # the reset pins: always
        wait(lambda: self.restarts)


class UpdateCommand(unittest.TestCase):
    """`audiolink update` over USB, against an UpdateService behind a fake host daemon."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.u = UpdateService(self.dir, restart=lambda: None)
        self.link = self.fast = {"rate": 96000, "channels": 8}
        self.calls = []
        self.answers = True                     # does the Pi answer after the restart?
        self.caps = ["update"]
        self.starting = False

    def call(self, op, args=None, port=None):
        self.calls.append(op)
        if op == "peer":
            return {"connected": True, "peer": {"caps": self.caps}}
        if op == "image.status" and "image" in self.caps and self.starting:
            raise SystemExit("Timed out")             # has versions, not answering yet
        if op == "audio.status":
            return {"link": self.link, "running": False, "pi": {}, "remembered": {"best": self.fast}}
        if op == "audio.run":
            self.link = {"rate": 48000, "channels": 2}
            return {}
        if op == "audio.resume":
            self.link = self.fast
            return {"started": True}
        if op == "update.status" and "update.finish" in self.calls:     # restarted
            if not self.answers:
                raise SystemExit("No Pi plugged in")
            return dict(self.u.status(), running_hash=self.u.status()["update_hash"])
        return self.u.handle(op.split(".", 1)[1], args or {})

    def run_cmd(self, *argv):
        import contextlib
        import io
        from unittest import mock
        from audiolink.__main__ import main
        from audiolink.host import update_cli
        from .helpers import FakeClock, patch_time
        out = io.StringIO()
        with mock.patch("audiolink.host.control.call", self.call), patch_time(update_cli, FakeClock()), \
                contextlib.redirect_stdout(out):
            main(["update"] + list(argv))
        return out.getvalue()

    def file(self, data):
        path = os.path.join(self.dir, "x.pyz")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def refused(self, argv, msg):
        with self.assertRaises(SystemExit) as e:
            self.run_cmd(*argv)
        self.assertIn(msg, str(e.exception.code))

    def test_file_checked_before_the_pi(self):
        self.refused(["--code", os.path.join(self.dir, "nope.pyz")], "No file")
        self.refused(["--code", self.file(b"")], "is empty")
        self.refused(["--code", self.file(b"just bytes")], "isn't an audiolink .pyz")
        self.refused(["--code", self.file(pyz_without_main())], "isn't an audiolink .pyz")
        self.assertEqual(self.calls, [])

    def test_options_dont_combine(self):
        self.refused(["--status", "--back"], "Pick one")
        self.refused(["build/card-pi", "--status"], "Pick one")
        self.refused(["--code", self.file(pyz()), "--status"], "--code goes alone")
        self.refused(["--status", "--at", "10.0.0.2"], "goes with --pi")
        self.assertEqual(self.calls, [])

    def test_revert_with_nothing_installed(self):
        self.assertIn("already runs its image's code", self.run_cmd("--code", "--back"))

    def test_status_compares_with_this_computer(self):
        from unittest import mock
        path = self.file(pyz())
        h = hashlib.sha256(pyz()).hexdigest()
        st = {"running": "/opt/audiolink/audiolink.pyz", "running_hash": h, "update": True, "set_aside": False}
        self.u.status = lambda: st
        with mock.patch("audiolink.host.update_cli._this_pyz", lambda: path):
            out = self.run_cmd("--status", "-v")
            plain = self.run_cmd("--status")
        self.assertIn("Code:     the image's (%s, the same as this computer's)" % h[:12], out)
        self.assertIn("An update is on the card but isn't running", out)
        self.assertNotIn("Code:", plain)                         # builds: -v only
        st["running"] = os.path.join(self.dir, "audiolink.pyz")
        with mock.patch("audiolink.host.update_cli.PI_STATE_DIR", self.dir):
            self.assertIn("Running a code update on top (developers): audiolink update --code --back",
                          self.run_cmd("--status"))

    def test_status_tells_an_old_pi_from_one_starting(self):
        self.assertIn("from before image slots", self.run_cmd("--status"))     # no image in its caps
        self.caps, self.starting = ["update", "image"], True
        self.refused(["--status"], "it may still be starting")

    def test_slows_the_link_before_sending(self):
        out = self.run_cmd("--code", self.file(pyz()))
        self.assertLess(self.calls.index("audio.run"), self.calls.index("update.begin"))
        self.assertIn("sent", out)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "audiolink.pyz")))
        self.assertIn("Speeding the link back up... 96 kHz \u00d7 8 channels", out)    # and back up after
        self.assertEqual(self.link, self.fast)

    def test_a_link_at_baseline_stays_there(self):
        self.link = {"rate": 48000, "channels": 2}
        self.run_cmd("--code", self.file(pyz()))
        self.assertNotIn("audio.resume", self.calls)

    def test_without_image_slots_says_why(self):
        self.refused([], "card write")                  # a card from before slots
        self.u.status = lambda: {"running": "/var/lib/audiolink/audiolink.pyz"}
        self.refused([], "audiolink update --code --back")      # an old code update runs instead
        self.refused([], "can't take versions: put this version on its card once with audiolink card write")
        self.assertNotIn("image.status", self.calls)

    def test_over_the_network_asks_the_pi_before_the_image(self):
        from unittest import mock
        from audiolink.host import update_cli

        class Remote:
            name, conn = "pi", object()

            def ask(self, op, **fields):
                if op == "image_status":
                    return {"error": "unknown op 'image_status'", "code": "unknown"}
                return {"result": {"running": "/opt/audiolink/audiolink.pyz"}}

            def refusal(self, r):
                return "too old"
        with mock.patch("audiolink.client.remote.open_remote", lambda pi, at: Remote()), \
                mock.patch("audiolink.card.fetch_image", lambda *a, **k: self.fail("downloaded")):
            self.refused(["--pi", "pi"], "card write")
        self.assertTrue(issubclass(update_cli.NoOp, SystemExit))

    def test_says_when_the_pi_hasnt_come_back(self):
        self.answers = False
        self.refused(["--code", self.file(pyz())], "hasn't come back yet")


def pyz_without_main():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("other.py", "")
    return buf.getvalue()


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
        for _ in range(8):                  # released for longer than BOUNCE
            self.p.step(t)
            t += 0.25

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

    def test_slips_dont_end_the_hold(self):
        t = 100.0
        for i in range(16):                 # 8 s, contact lost for 0.5 s every 2 s
            self.shorted = (i % 4) != 3
            for _ in range(2):
                self.p.step(t)
                t += 0.25
        self.shorted = False
        for _ in range(8):
            self.p.step(t)
            t += 0.25
        self.assertEqual(self.done, ["revert"])

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
        self.assertEqual(gpio_path(21, d), d + "/gpio533/value")             # sysfs paths: /


if __name__ == "__main__":
    unittest.main()
