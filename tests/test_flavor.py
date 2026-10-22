"""The Pi's flavors (services/flavor.py) and `audiolink flavor` (host/flavor_cli.py): listing, adding and removing them on the card,
and switching (use, try) with confirm-or-revert over USB; with stand-ins for the
platform's planner and gadget.sh, the audio, the card's settings and the link."""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from audiolink.services import card as card_svc
from audiolink.services import flavor
from audiolink.services.flavor import FlavorService

from .helpers import PI_USB, PI_USB_ID, wait

REPO = Path(__file__).resolve().parent.parent

# The platform's planner, as far as these tests need it: a plan with the file's IDs and
# product, a note, and its error for a file that says BROKEN.
PLANNER = r'''
import re, sys
args = sys.argv[1:]
src, out = args[0], args[args.index("-o") + 1]
text = open(src).read()
if "BROKEN" in text:
    sys.exit(src + ": [device] needs idVendor and idProduct")
vid = re.search(r"idVendor = (0x[0-9a-fA-F]+)", text).group(1)
pid = re.search(r"idProduct = (0x[0-9a-fA-F]+)", text).group(1)
product = re.search(r'product = "([^"]*)"', text).group(1)
open(out, "w").write("# plan, options: %s\nname device\nwrite idVendor %s\nwrite idProduct %s\n"
                     "write strings/0x409/product %s\n" % (" ".join(args[5:]), vid, pid, product))
open(args[args.index("--functions") + 1], "w").write("audio uac1 /x/functions/uac1.0\n")
print("wrote %s (3 operations)" % out)
print("  note: string 33 not reproduced")
'''

CLONE = '''extras = false
speed = "full-speed"
[device]
idVendor = 0x1038
idProduct = 0x230a
[strings]
product = "Arctis GameBuds"
'''
OTHER = CLONE.replace("0x230a", "0x2300").replace("Arctis GameBuds", "Other Buds")

KEYS = "hostname hostname SETTING_HOSTNAME\nflavor flavor SETTING_FLAVOR\n"
DEFAULTS = "SETTING_HOSTNAME=audiopi\nSETTING_FLAVOR=audiolink\n"


class FakeFormats:
    """services/audio_format.py's part in a switch: its lock, and replace_device."""

    def __init__(self):
        self.lock = threading.Lock()
        self.replaced = 0

    def replace_device(self, change):
        assert self.lock.locked()
        self.replaced += 1
        return change()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.dir))
        d = self.dir
        (d / "built-in").mkdir()
        shutil.copy(str(REPO / "config" / "flavors" / "audiolink.toml"), str(d / "built-in" / "audiolink.toml"))
        (d / "card").mkdir()
        (d / "card" / "settings.txt").write_text("#flavor = audiolink\n")
        (d / "run").mkdir()
        (d / "run" / "flavor").write_text("audiolink\n")
        (d / "planner.py").write_text(PLANNER)
        (d / "flavors.env").write_text("FLAVOR_DEFAULT=audiolink\nFLAVOR_OPTIONS='--console'\n")
        (d / "keys").write_text(KEYS)
        (d / "defaults").write_text(DEFAULTS)
        self.card = card_svc.CardService(card=str(d / "card"), keys=str(d / "keys"), defaults=str(d / "defaults"))
        self.formats = FakeFormats()
        self.uses = []                  # gadget.sh use NAME, in order
        self.refuse = set()             # flavors the kernel refuses (gadget.sh: the default instead, exit 3)
        self.waits = []
        self.svc = FlavorService(self.formats, self.card, card=str(d / "card"), env={}, gadget_sh="gadget.sh",
                                 built_in=str(d / "built-in"), flavors_env=str(d / "flavors.env"),
                                 planner=str(d / "planner.py"), python=sys.executable, run_dir=str(d / "run"),
                                 switch_file=str(d / "switch.json"), run=self.fake_run, within=1.5,
                                 wait=lambda: self.waits.append(1) or 0)
        self.host_caps, self.host_takes = ["usb_ids"], True
        self.told = []                  # what the host was told, and how many gadget.sh uses before
        self.session = SimpleNamespace(peer_alive=True, link=SimpleNamespace(last_rx=0.0),
                                       peer_has=lambda cap: cap in self.host_caps, request=self.host_request)
        self.svc.attach(self.session)
        p = mock.patch.object(flavor, "REPLY_FIRST", 0)
        p.start()
        self.addCleanup(p.stop)

    def fake_run(self, cmd, **kw):
        if cmd[:2] == ["sh", "gadget.sh"]:
            assert cmd[2] == "use"
            self.uses.append(cmd[3])
            up, st = (cmd[3], 0) if cmd[3] not in self.refuse else ("audiolink", 3)
            (self.dir / "run" / "flavor").write_text(up + "\n")
            return subprocess.CompletedProcess(cmd, st, "flavor %s up\n" % up if st == 0 else "",
                                               "" if st == 0 else "gadget.sh: flavor %s refused" % cmd[3])
        return subprocess.run(cmd, **kw)

    def host_request(self, op, args=None, timeout=60):
        if not self.host_takes:
            raise RuntimeError("host does not support %r" % op)
        self.told.append((op, args, len(self.uses)))

    def settings(self):
        return (self.dir / "card" / "settings.txt").read_text()

    def link_comes_back(self):
        """A computer's USB link on the new device (frames heard after it came up)."""
        def back():
            wait(lambda: self.svc.switch and self.svc.switch["state"] == "waiting")
            self.session.link.last_rx = time.monotonic() + 1
        threading.Thread(target=back, daemon=True).start()

    def finished(self, sw):
        self.assertTrue(wait(lambda: self.svc.switch["id"] == sw["id"]
                             and self.svc.switch["state"] not in ("switching", "waiting"), 10))
        return self.svc.switch


class Listing(Base):
    def test_built_in_and_yours(self):
        (self.dir / "card" / "flavors").mkdir()
        (self.dir / "card" / "flavors" / "gamebuds.toml").write_text(CLONE)
        (self.dir / "card" / "flavors" / "notes.txt").write_text("not a flavor")
        r = self.svc.handle("list", {})
        self.assertTrue(r["switchable"])
        self.assertEqual((r["running"], r["default"], r["chosen"]), ("audiolink", "audiolink", None))
        got = {f["name"]: (f["where"], f["usb"], f["product"]) for f in r["flavors"]}
        self.assertEqual(got, {"audiolink": ("built in", PI_USB_ID, "AudioPi"),
                               "gamebuds": ("card", "1038:230a", "Arctis GameBuds")})
        self.assertTrue(r["usb_peer"])

    def test_without_tomllib(self):
        """Python before 3.11 (a computer's; the Pi has 3.11+): enough of TOML for the IDs."""
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            info = flavor._toml(CLONE + '[[functions]]\nkind = "uac1"\nproduct = "not this"\n')
        self.assertEqual(info["device"], {"idVendor": 0x1038, "idProduct": 0x230a})
        self.assertEqual(info["strings"]["product"], "Arctis GameBuds")

    def test_a_capture(self):
        import json
        p = self.dir / "cap.json"
        p.write_text(json.dumps({"devices": [
            {"device_descriptor": {"bDeviceClass": 9}, "model": {"device": {"idVendor": 1, "idProduct": 2}}},
            {"device_descriptor": {"bDeviceClass": 0}, "model": {
                "device": {"idVendor": 0x1038, "idProduct": 0x230a, "iProduct": 2},
                "strings": {"table": {"2": "Arctis GameBuds"}}}}]}))
        self.assertEqual(flavor.describe(str(p)), {"usb": "1038:230a", "product": "Arctis GameBuds"})

    def test_plan_info_and_an_image_without_flavors(self):
        p = self.dir / "device.plan"
        p.write_text("# x\nname device\nwrite idVendor 0x%04x\nwrite idProduct 0x%04x\n" % PI_USB +
                     "write strings/0x409/product AudioPi\n")
        self.assertEqual(flavor.plan_info(str(p)), (PI_USB_ID, "AudioPi"))
        with mock.patch.object(flavor, "running_plan", lambda: str(p)):
            self.assertEqual(flavor.usb_ids(), [PI_USB_ID])          # the Pi's network record
        (self.dir / "flavors.env").unlink()
        svc = FlavorService(self.formats, self.card, card=str(self.dir / "card"), env={}, gadget_sh="gadget.sh",
                            flavors_env=str(self.dir / "flavors.env"), planner=str(self.dir / "planner.py"))
        self.assertFalse(svc.handle("list", {})["switchable"])
        with self.assertRaisesRegex(ValueError, "no flavors to switch"):
            svc.handle("use", {"name": "audiolink"})


class AddRemove(Base):
    def test_add_plans_it_first(self):
        r = self.svc.handle("add", {"name": "gamebuds", "toml": CLONE})
        self.assertEqual((r["usb"], r["product"], r["notes"]), ("1038:230a", "Arctis GameBuds",
                                                                ["string 33 not reproduced"]))
        self.assertEqual((self.dir / "card" / "flavors" / "gamebuds.toml").read_text(), CLONE)
        self.assertFalse(r["replaced"])
        self.assertTrue(self.svc.handle("add", {"name": "gamebuds", "toml": OTHER})["replaced"])

    def test_one_that_doesnt_plan_isnt_kept(self):
        with self.assertRaisesRegex(ValueError, r"doesn't plan: the flavor: \[device\] needs idVendor"):
            self.svc.handle("add", {"name": "bad", "toml": "BROKEN"})
        self.assertFalse((self.dir / "card" / "flavors" / "bad.toml").exists())

    def test_names(self):
        for name, why in (("Bad Name", "lowercase"), ("../x", "lowercase"), ("audiolink", "built in"),
                          ("try", "kept for")):
            with self.assertRaisesRegex(ValueError, why):
                self.svc.handle("add", {"name": name, "toml": CLONE})
        with self.assertRaisesRegex(ValueError, "no flavor"):
            self.svc.handle("add", {"name": "x", "toml": ""})

    def test_remove(self):
        self.svc.handle("add", {"name": "gamebuds", "toml": CLONE})
        self.card.handle("set", {"changes": {"flavor": "gamebuds"}})
        with self.assertRaisesRegex(ValueError, "built in"):
            self.svc.handle("remove", {"name": "audiolink"})
        with self.assertRaisesRegex(ValueError, "no flavor"):
            self.svc.handle("remove", {"name": "nope"})
        r = self.svc.handle("remove", {"name": "gamebuds"})
        self.assertEqual((r["removed"], r["unchosen"]), ("gamebuds", True))     # the Pi starts as the default
        self.assertNotIn("\nflavor = gamebuds", self.settings())
        self.assertFalse((self.dir / "card" / "flavors" / "gamebuds.toml").exists())

    def test_not_the_one_thats_up(self):
        self.svc.handle("add", {"name": "gamebuds", "toml": CLONE})
        (self.dir / "run" / "flavor").write_text("gamebuds\n")
        with self.assertRaisesRegex(ValueError, "the Pi is gamebuds now"):
            self.svc.handle("remove", {"name": "gamebuds"})


class Switching(Base):
    def setUp(self):
        super().setUp()
        self.svc.handle("add", {"name": "gamebuds", "toml": CLONE})

    def test_kept_once_the_link_comes_back(self):
        self.link_comes_back()
        sw = self.svc.handle("use", {"name": "gamebuds"})
        self.assertEqual((sw["name"], sw["back"], sw["state"]), ("gamebuds", "audiolink", "switching"))
        self.assertEqual(self.finished(sw)["state"], "kept")
        self.assertEqual(self.uses, ["gamebuds"])
        self.assertEqual(self.formats.replaced, 1)
        self.assertIn("flavor = gamebuds", self.settings())       # it starts as gamebuds from now on
        self.assertEqual(self.waits, [1])                          # after the gap
        self.assertFalse((self.dir / "switch.json").exists())
        self.assertEqual(self.told, [("usb_ids.add", {"ids": ["1038:230a"]}, 0)])     # before it switched
        self.assertEqual(self.svc.switch["host"], "told")

    def test_the_host_is_told_the_new_id(self):
        """An older host: the switch goes on (and says so); no host: nothing to tell."""
        self.host_caps = []
        self.assertEqual(self.finished(self.svc.handle("use", {"name": "gamebuds"}))["host"], "old")
        self.host_caps, self.host_takes = ["usb_ids"], False
        self.assertEqual(self.finished(self.svc.handle("use", {"name": "gamebuds"}))["host"], "failed")
        self.session.peer_alive = False
        self.assertIsNone(self.finished(self.svc.handle("use", {"name": "gamebuds"}))["host"])
        self.assertEqual(self.told, [])
        self.assertEqual(self.uses, ["gamebuds", "audiolink"] * 3)

    def test_back_without_a_computers_link(self):
        sw = self.svc.handle("use", {"name": "gamebuds"})
        with self.assertRaisesRegex(ValueError, "under way"):
            self.svc.handle("use", {"name": "audiolink"})
        self.assertEqual(self.finished(sw)["state"], "back")
        self.assertEqual(self.uses, ["gamebuds", "audiolink"])
        self.assertEqual(self.svc.running(), "audiolink")
        self.assertNotIn("\nflavor = ", self.settings())
        self.assertEqual(len(self.waits), 2)                       # the gap before going back too

    def test_back_to_the_default_is_no_setting(self):
        self.link_comes_back()
        self.finished(self.svc.handle("use", {"name": "gamebuds"}))
        self.session.link.last_rx = 0
        self.link_comes_back()
        self.assertEqual(self.finished(self.svc.handle("use", {"name": "audiolink"}))["state"], "kept")
        self.assertNotIn("\nflavor = ", self.settings())

    def test_refused_by_the_kernel(self):
        self.refuse.add("gamebuds")
        sw = self.finished(self.svc.handle("use", {"name": "gamebuds"}))
        self.assertEqual((sw["state"], sw["running"]), ("refused", "audiolink"))
        self.assertIn("refused", sw["error"])
        self.assertNotIn("\nflavor = ", self.settings())

    def test_the_one_thats_up(self):
        self.assertEqual(self.svc.handle("use", {"name": "audiolink"}), {"name": "audiolink", "unchanged": True})
        with self.assertRaisesRegex(ValueError, "no flavor nope"):
            self.svc.handle("use", {"name": "nope"})
        self.assertEqual(self.uses, [])

    def test_try_isnt_kept(self):
        self.link_comes_back()
        sw = self.svc.handle("try", {"toml": OTHER})
        self.assertEqual((sw["name"], sw["usb"], sw["notes"]), ("try", "1038:2300", ["string 33 not reproduced"]))
        self.assertEqual(self.finished(sw)["state"], "kept")
        self.assertEqual(self.told, [("usb_ids.add", {"ids": ["1038:2300"]}, 0)])
        self.assertNotIn("\nflavor = ", self.settings())           # until the next start
        listed = self.svc.handle("list", {})
        self.assertEqual(listed["running"], "try")
        self.assertIn(("try", "tried"), [(f["name"], f["where"]) for f in listed["flavors"]])
        self.session.link.last_rx = 0                              # then back: the file goes
        self.finished(self.svc.handle("use", {"name": "gamebuds"}))
        self.assertFalse((self.dir / "card" / "flavors" / "try.toml").exists())

    def test_try_one_that_doesnt_plan(self):
        with self.assertRaisesRegex(ValueError, "doesn't plan"):
            self.svc.handle("try", {"toml": "BROKEN"})
        self.assertEqual(self.uses, [])

    def test_restarted_during_a_switch(self):
        (self.dir / "switch.json").write_text('{"name": "gamebuds", "back": "audiolink"}')
        (self.dir / "run" / "flavor").write_text("gamebuds\n")
        self.svc.run()
        self.assertTrue(wait(lambda: self.uses == ["audiolink"]))
        self.assertFalse((self.dir / "switch.json").exists())

    def test_reset_pins(self):
        self.link_comes_back()
        self.finished(self.svc.handle("use", {"name": "gamebuds"}))
        self.svc.to_default()
        self.assertEqual(self.uses, ["gamebuds", "audiolink"])
        self.assertNotIn("\nflavor = ", self.settings())

    def test_reset_pins_during_a_switch(self):
        self.svc.handle("use", {"name": "gamebuds"})
        sw = self.svc.switch
        wait(lambda: sw["state"] == "waiting")
        self.svc.to_default()
        self.assertEqual(sw["state"], "cancelled")
        time.sleep(2)                                               # past the confirm time: no second revert
        self.assertEqual(self.uses, ["gamebuds", "audiolink"])


class Command(Base):
    """`audiolink flavor` (host/flavor_cli.py) against the service above."""

    def setUp(self):
        super().setUp()
        from audiolink.host import flavor_cli
        self.cli = flavor_cli
        self.taught = []
        for p in (mock.patch.object(flavor_cli, "POLL", 0.05),
                  mock.patch.dict(os.environ, {"AUDIOLINK_CONFIG_DIR": str(self.dir / "config")}),
                  mock.patch("audiolink.host.control.call", lambda op, args=None, **kw: self.taught.extend(args["ids"])),
                  mock.patch.object(flavor_cli, "pi_caller", lambda a, cap: (self.call, self.net))):
            p.start()
            self.addCleanup(p.stop)
        self.net = False
        (self.dir / "gamebuds.toml").write_text(CLONE)

    def call(self, op, args=None, port=None):
        area, _, action = op.partition(".")
        try:
            return self.svc.handle(action, dict(args or {}))
        except ValueError as e:                 # as control.call and NetCaller say it
            raise SystemExit(str(e))

    def command(self, action=None, arg=None, name=None):
        out = io.StringIO()
        a = SimpleNamespace(action=action, arg=arg, name=name, pi="kitchen" if self.net else None, control_port=None)
        with contextlib.redirect_stdout(out):
            try:
                self.cli.flavor_command(a)
            except SystemExit as e:
                print("EXIT", e)
        return out.getvalue()

    def saved_ids(self):
        from audiolink.host import config
        return config.load().get("usb_ids", [])

    def test_list(self):
        self.command("add", str(self.dir / "gamebuds.toml"))
        out = self.command()
        self.assertTrue(out.startswith("Flavor: audiolink\n"), out)
        self.assertRegex(out, r"\* audiolink +AudioPi +%s +\(built in, the default\)" % PI_USB_ID)
        self.assertRegex(out, r"  gamebuds +Arctis GameBuds +1038:230a +\(yours\)")
        self.assertTrue(out.endswith("Switch: audiolink flavor use NAME\n"))

    def test_add_remembers_its_id(self):
        out = self.command("add", str(self.dir / "gamebuds.toml"))
        self.assertIn("Note: string 33 not reproduced\nAdded gamebuds (Arctis GameBuds, 1038:230a)", out)
        self.assertIn("This computer finds the Pi as 1038:230a from now on", out)
        self.assertEqual(self.saved_ids(), ["1038:230a"])
        self.assertEqual(self.taught, ["1038:230a"])                # the running host too
        self.assertIn("Added my-buds", self.command("add", str(self.dir / "gamebuds.toml"), name="my-buds"))
        self.assertEqual(self.saved_ids(), ["1038:230a"])           # once
        self.assertEqual(self.cli.name_of("/x/My GameBuds v2.toml"), "my-gamebuds-v2")

    def test_use_kept(self):
        self.command("add", str(self.dir / "gamebuds.toml"))
        self.link_comes_back()
        out = self.command("use", "gamebuds")
        self.assertIn("may ask to allow it (macOS: Allow accessory)", out)
        self.assertIn("Switching the Pi to gamebuds; it keeps it once this computer's link comes back (1s)... kept", out)
        self.assertIn("The Pi starts as gamebuds from now on", out)
        self.assertTrue(self.command().startswith("Flavor: gamebuds\n"))
        self.assertIn("EXIT the Pi is gamebuds now", self.command("remove", "gamebuds"))

    def test_use_went_back(self):
        self.command("add", str(self.dir / "gamebuds.toml"))
        out = self.command("use", "gamebuds")
        self.assertIn("... went back\nEXIT No computer's link came back on gamebuds, so the Pi went back to "
                      "audiolink. Likely:\n  - this computer asked to allow the new device", out)
        self.assertNotIn("doesn't find the Pi", out)                # its host was told the new ID
        self.host_caps, self.session.peer_alive = [], False          # nobody to tell: maybe
        self.assertIn("doesn't find the Pi as 1038:230a (an older one?; or start it with: audiolink host --usb-id "
                      "1038:230a)", self.command("use", "gamebuds"))
        self.session.peer_alive = True
        self.assertIn("  - gamebuds has no audio both ways (the link runs over it)\n(audiolink logs)", out)
        self.host_caps = []                                         # a host from before: likeliest first
        self.assertIn("Likely:\n  - this computer's audiolink host doesn't find the Pi as 1038:230a (it's older",
                      self.command("use", "gamebuds"))
        self.assertIn("EXIT No flavor nope", self.command("use", "nope"))
        self.assertIn("The Pi is audiolink already", self.command("use", "audiolink"))

    def test_try(self):
        self.link_comes_back()
        out = self.command("try", str(self.dir / "gamebuds.toml"))
        self.assertIn("Note: string 33 not reproduced", out)
        self.assertIn("Switching the Pi to the tried flavor", out)
        self.assertIn("Until the Pi restarts", out)
        self.assertEqual(self.saved_ids(), ["1038:230a"])            # kept: a restarted host still finds the Pi
        self.assertEqual(set(self.taught), {"1038:230a"})
        self.assertIn("Flavor: the tried flavor (starts as audiolink)", self.command())

    def test_a_capture_isnt_a_flavor(self):
        cap = str(self.dir / "GameBuds.json")
        Path(cap).write_text('{"devices": []}')
        for action in ("add", "try"):
            self.assertIn("EXIT %s is a capture, not a flavor: convert it first: platform/capture_to_toml.py %s "
                          "-o gamebuds.toml" % (cap, cap), self.command(action, cap))
        self.assertEqual(self.uses, [])

    def test_a_chosen_flavor_that_isnt_there(self):
        self.card.handle("set", {"changes": {"flavor": "typo"}})
        self.assertIn("Flavor: audiolink (settings.txt's flavor = typo: there's no such flavor, so it starts as "
                      "audiolink)", self.command())

    def test_replacing_the_one_thats_up_or_chosen(self):
        self.command("add", str(self.dir / "gamebuds.toml"))
        self.card.handle("set", {"changes": {"flavor": "gamebuds"}})
        out = self.command("add", str(self.dir / "gamebuds.toml"))
        self.assertIn("The Pi starts as gamebuds: the new file shows then, unchecked (to check it now: "
                      "audiolink flavor use gamebuds)", out)
        (self.dir / "run" / "flavor").write_text("gamebuds\n")
        out = self.command("add", str(self.dir / "gamebuds.toml"))
        self.assertIn("The Pi is gamebuds now: the new file shows when it next starts, unchecked (to check it "
                      "first: audiolink flavor try %s)" % (self.dir / "gamebuds.toml"), out)
        self.assertNotIn("Switch:", out)

    def test_over_the_network_without_a_computer(self):
        self.net = True
        self.session.peer_alive = False
        out = self.command("use", "audiolink")
        self.assertIn("EXIT Nothing can confirm a switch: no computer is plugged into the Pi's USB", out)
        self.assertIn("audiolink settings flavor=NAME --pi kitchen --reboot", out)

    def test_learned_from_the_record(self):
        from audiolink.client.remote import learn_usb_ids
        learn_usb_ids({"usb": ["1038:230a", PI_USB_ID]})            # the built-in one isn't saved
        learn_usb_ids({"name": "a Pi from before flavors"})
        self.assertEqual(self.saved_ids(), ["1038:230a"])


class OverTheLink(unittest.TestCase):
    """The Pi tells its host the new USB ID over a real link (session.py): a host that knows
    how saves it; one from before refuses, and the Pi carries on."""

    def pair(self, host_services):
        from audiolink.session import Session
        host, pi = Session("host", host_services), Session("pi", [])
        running = [True]

        def pump():
            while running[0]:
                pi.link.feed(host.link.pull(4096))
                host.link.feed(pi.link.pull(4096))
                time.sleep(0.001)
        threading.Thread(target=pump, daemon=True).start()
        self.addCleanup(running.__setitem__, 0, False)
        self.assertTrue(wait(lambda: pi.peer_alive and pi.peer_info))
        svc = FlavorService(FakeFormats(), None, card="/nonexistent", env={})
        svc.attach(pi)
        return svc

    def setUp(self):
        from audiolink.wire import usb_audio
        self.usb_audio = usb_audio
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        for p in (mock.patch.dict(os.environ, {"AUDIOLINK_CONFIG_DIR": d}),
                  mock.patch.object(flavor, "TELL_WITHIN", 3)):
            p.start()
            self.addCleanup(p.stop)
        usb_audio.set_extra_ids(None)
        self.addCleanup(usb_audio.set_extra_ids, None)

    def test_a_host_that_knows(self):
        from audiolink.host import config
        svc = self.pair([self.usb_audio.UsbIds()])
        self.assertEqual(svc._tell_host("1038:230a"), "told")
        self.assertIn((0x1038, 0x230a), self.usb_audio.known_ids())
        self.assertEqual(config.load()["usb_ids"], ["1038:230a"])

    def test_a_host_from_before(self):
        svc = self.pair([])
        self.assertEqual(svc._tell_host("1038:230a"), "old")
        svc.session.peer_info["caps"].append("usb_ids")             # says it can, then refuses
        self.assertEqual(svc._tell_host("1038:230a"), "failed")
        self.assertNotIn((0x1038, 0x230a), self.usb_audio.known_ids())


class Settings(unittest.TestCase):
    def test_the_card_setting(self):
        self.assertEqual(card_svc.check("flavor", "gamebuds"), "gamebuds")
        with self.assertRaisesRegex(ValueError, "flavor's name"):
            card_svc.check("flavor", "Game Buds")


if __name__ == "__main__":
    unittest.main()
