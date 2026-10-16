"""pi/card-settings.sh: the card's settings.txt (through the platform's
settings files) as `audiolink pi` arguments; config/pi.toml's defaults."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from audiolink.__main__ import build_parser
from audiolink.relay import DEFAULT

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIN = "ab" * 32
pi_only = unittest.skipIf(sys.platform == "win32", "the Pi's shell scripts (Linux): no sh here")


@pi_only
class CardSettings(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        for d in ("etc/usb-gadget", "run/usb-gadget", "var/log"):
            os.makedirs(os.path.join(self.root, d))
        self.write("etc/usb-gadget/settings.defaults",
                   "APP_SETTING_RELAY=relay.lyuhau.com:443\nAPP_SETTING_EXPOSE_SETUP=off\n")

    def write(self, path, text):
        with open(os.path.join(self.root, path), "w") as f:
            f.write(text)

    def args(self, env=None):
        if env is not None:
            self.write("run/usb-gadget/settings.env", env)
        p = subprocess.run(["sh", os.path.join(REPO, "pi", "card-settings.sh")], capture_output=True, text=True,
                           env=dict(os.environ, USB_TEST_ROOT=self.root), check=True)
        return p.stdout.splitlines(), p.stderr

    def test_defaults(self):
        self.assertEqual(self.args(), (["--relay=relay.lyuhau.com:443"], ""))

    def test_values(self):
        for relay in ("off", "relay.example.com:8443", "relay.example.com", "10.0.0.2:443#" + PIN, "[::1]:443"):
            with self.subTest(relay):
                args, err = self.args(f"APP_SETTING_RELAY='{relay}'\nAPP_SETTING_EXPOSE_SETUP='on'\n")
                self.assertEqual((args, err), ([f"--relay={relay}", "--expose-setup"], ""))
                a = build_parser().parse_args(["pi"] + args)
                self.assertEqual((a.relay, a.expose_setup), (relay, True))

    def test_bad_values_left_out(self):
        for relay in ("-x", "relay example.com", "host:443#short", "host:port", "a;reboot"):
            with self.subTest(relay):
                args, err = self.args(f"APP_SETTING_RELAY='{relay}'\nAPP_SETTING_EXPOSE_SETUP='maybe'\n")
                self.assertEqual(args, [])
                self.assertIn(f"audiolink.relay = {relay} isn't", err)
                self.assertIn("audiolink.expose_setup = maybe isn't on or off", err)
        with open(os.path.join(self.root, "var/log/settings.log")) as f:
            self.assertIn("using audiolink's default", f.read())

    def test_off_the_platform(self):
        os.remove(os.path.join(self.root, "etc/usb-gadget/settings.defaults"))
        self.assertEqual(self.args(), ([], ""))


class Config(unittest.TestCase):
    def test_relay_default(self):
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                self.skipTest("no tomllib (Python 3.11+) or tomli")
        with open(os.path.join(REPO, "config", "pi.toml"), "rb") as f:
            cfg = tomllib.load(f)
        self.assertEqual(cfg["app"]["settings"]["relay"]["default"], DEFAULT)
        self.assertEqual(build_parser().parse_args(["pi"]).relay, DEFAULT)

    @pi_only
    def test_shell_syntax(self):
        for p in ("card-settings.sh", "start-audiolink.sh", "app-start.sh"):
            subprocess.run(["sh", "-n", os.path.join(REPO, "pi", p)], check=True)


if __name__ == "__main__":
    unittest.main()
