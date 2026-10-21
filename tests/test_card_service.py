"""The Pi's card service (services/card.py): settings and backups over the link, and
the commands that use it (host/settings_cli.py, card_cli.backup_from_pi)."""
import base64
import contextlib
import io
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from audiolink import card_cli
from audiolink.host import settings_cli
from audiolink.services import card as card_svc

KEYS = """# the card's settings (build_card.py): KEY TYPE VARIABLE
hostname hostname SETTING_HOSTNAME
console onoff SETTING_CONSOLE
usb.vendor_id hex16 SETTING_USB_VENDOR_ID
usb.product usbstring SETTING_USB_PRODUCT
audiolink.relay text APP_SETTING_RELAY
"""
DEFAULTS = "SETTING_HOSTNAME=audiopi\nSETTING_CONSOLE=on\nSETTING_USB_VENDOR_ID=\nAPP_SETTING_RELAY=relay.example:443\n"
SETTINGS = "# The Pi's name.\n#hostname = audiopi\n\n# Console.\n#console = on\nconsole = off\n\n#usb.vendor_id = \n"


class Service(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "keys").write_text(KEYS)
        (d / "defaults").write_text(DEFAULTS)
        self.card = d / "card"
        self.card.mkdir()
        (self.card / "settings.txt").write_text(SETTINGS)
        self.rebooted = []
        self.svc = card_svc.CardService(card=str(self.card), keys=str(d / "keys"), defaults=str(d / "defaults"),
                                        reboot=lambda: self.rebooted.append(True))

    def tearDown(self):
        self.tmp.cleanup()

    def test_settings_listed(self):
        got = {s["key"]: (s["value"], s["default"]) for s in self.svc.handle("settings", {})["settings"]}
        self.assertEqual(got["hostname"], (None, "audiopi"))
        self.assertEqual(got["console"], ("off", "on"))
        self.assertEqual(got["usb.vendor_id"], (None, ""))

    def test_set_and_unset_in_place(self):
        r = self.svc.handle("set", {"changes": {"hostname": "kitchen", "usb.vendor_id": "1209", "console": None}})
        self.assertEqual(r["changes"], {"hostname": "kitchen", "usb.vendor_id": "0x1209", "console": None})
        text = (self.card / "settings.txt").read_text()
        self.assertIn("#hostname = audiopi\nhostname = kitchen\n", text)          # next to its help
        self.assertNotIn("console = off", text)                                   # unset: the built-in value
        self.assertIn("#console = on", text)
        self.assertIn("usb.vendor_id = 0x1209", text)
        self.assertEqual(self.rebooted, [])
        self.svc.handle("set", {"changes": {"hostname": "den"}})
        self.assertEqual(text.count("hostname = kitchen"), 1)
        self.assertEqual((self.card / "settings.txt").read_text().count("\nhostname ="), 1)  # replaced, not added

    def test_bad_values_and_keys(self):
        for changes, msg in (({"hostname": "no spaces"}, "letters, digits"), ({"console": "maybe"}, "on or off"),
                             ({"usb.vendor_id": "0x12345"}, "hex ID"), ({"typo": "x"}, "isn't one of")):
            with self.assertRaisesRegex(ValueError, msg):
                self.svc.handle("set", {"changes": changes})
        self.assertEqual((self.card / "settings.txt").read_text(), SETTINGS)       # nothing changed

    def test_reboot_after_the_reply(self):
        self.svc.handle("set", {"changes": {"hostname": "kitchen"}, "reboot": True})
        for _ in range(30):
            if self.rebooted:
                break
            time.sleep(0.1)
        self.assertEqual(self.rebooted, [True])

    def test_backup_made_on_the_card_then_read(self):
        (self.card / "audiolink").mkdir()
        (self.card / "audiolink/identity.json.a").write_text("{}")
        (self.card / "config.txt").write_text("os_prefix=a/\n")
        st = self.svc.handle("backup", {})
        for _ in range(50):
            st = self.svc.handle("progress", {})
            if st["state"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual((st["state"], st["files"]), ("done", 3))
        data = b"".join(base64.b64decode(self.svc.handle("read", {"offset": off, "size": 100})["data"])
                        for off in range(0, st["size"], 100))
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertEqual(sorted(z.namelist()), ["audiolink/identity.json.a", "config.txt", "settings.txt"])
        self.svc.handle("done", {})
        self.assertEqual([p.name for p in self.card.iterdir() if p.name.startswith(".")], [])

    def test_check(self):
        self.assertEqual(card_svc.check("onoff", "yes"), "on")
        self.assertEqual(card_svc.check("text", '"quoted"'), "quoted")
        self.assertEqual(card_svc.check("hex16", "0X1D6B"), "0x1d6b")


class Commands(unittest.TestCase):
    def test_settings_command(self):
        sent = []

        def call(op, args=None, port=None):
            sent.append((op, args))
            if op == "card.settings":
                return {"settings": [{"key": "hostname", "type": "hostname", "default": "audiopi", "value": None},
                                     {"key": "usb.product", "type": "usbstring", "default": "", "value": "Mine"}]}
            return {"ok": True, "changes": args["changes"]}
        a = SimpleNamespace(items=[], unset=None, reboot=False)
        out = io.StringIO()
        with mock.patch.object(settings_cli, "pi_caller", lambda a, cap: (call, False)), contextlib.redirect_stdout(out):
            settings_cli.settings_command(a)
            settings_cli.settings_command(SimpleNamespace(items=["hostname=kitchen"], unset=["usb.product"], reboot=False))
        self.assertIn("hostname     audiopi                   built in", out.getvalue())
        self.assertIn("usb.product  Mine                      set", out.getvalue())
        self.assertEqual(sent[-1][1]["changes"], {"hostname": "kitchen", "usb.product": None})
        self.assertIn("Takes effect when the Pi next starts", out.getvalue())

    def test_backup_from_pi(self):
        with tempfile.TemporaryDirectory() as tmp:
            card = Path(tmp, "card")
            (card / "a").mkdir(parents=True)
            (card / "a/config.txt").write_text("x" * 5000)
            (card / "settings.txt").write_text("y")
            svc = card_svc.CardService(card=str(card), keys=os.devnull, defaults=os.devnull)
            out = Path(tmp, "b.zip")

            def call(op, args=None, port=None):
                return svc.handle(op.split(".", 1)[1], args or {})
            card_cli.READ = 1000                                    # several pieces, in parallel
            try:
                with mock.patch("audiolink.host.pi_call.pi_caller", lambda a, cap: (call, False)), \
                        contextlib.redirect_stdout(io.StringIO()) as printed:
                    card_cli.backup_from_pi(SimpleNamespace(file=str(out)), sleep=lambda s: time.sleep(0.05))
            finally:
                card_cli.READ = 1024 * 1024
            with zipfile.ZipFile(str(out)) as z:
                self.assertEqual(z.read("a/config.txt"), b"x" * 5000)
            self.assertEqual(oct(os.stat(str(out)).st_mode & 0o777), "0o600")
            self.assertIn("keep it private", printed.getvalue())
            self.assertFalse(any(p.name.startswith(".audiolink-backup") for p in card.iterdir()))


if __name__ == "__main__":
    unittest.main()
