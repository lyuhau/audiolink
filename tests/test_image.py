"""Image updates: the Pi's side (services/image.py) and the command's (host/image_cli.py),
with stand-ins for the card, slot.sh and the link."""
import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from audiolink.host import image_cli
from audiolink.services import image as image_mod

IMAGE = {"config.txt": "os_prefix=a/\ninclude a/config.txt\n", "settings.txt": "#hostname = audiopi\n\n#audiolink.new = x\n",
         "bootcode.bin": "fw", "a/config.txt": "kernel=boot/vmlinuz-rpi\n", "a/boot/initrd.img": "new initrd",
         "a/boot/vmlinuz-rpi": "kernel"}


def zipped(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, t in files.items():
            z.writestr(n, t)
    return buf.getvalue()


class SlotSh:
    """slot.sh, on a card in a folder: a/ running, b/ spare."""

    def __init__(self, card, current="a/", trial=False, committed=True):
        self.card, self.current, self.trial, self.committed = card, current, trial, committed
        self.calls = []

    def __call__(self, cmd, capture_output=True, text=True):
        self.calls.append(cmd[2:] if cmd[:2] == ["sh", image_mod.SLOT_SH] else cmd)
        if cmd[2:3] == ["status"]:
            slots = " ".join(d + "/" for d in sorted(os.listdir(self.card))
                             if os.path.isfile(os.path.join(self.card, d, "config.txt")))
            out = 'current=%s trial=%d committed=%d slots="%s"\n' % (self.current, self.trial, self.committed, slots)
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        if cmd[2:3] == ["commit"]:
            self.committed = True
            return SimpleNamespace(returncode=0, stdout="committed %s\n" % self.current, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class Service(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.card = Path(self.tmp.name, "card")
        self.state = self.card / "audiolink"
        for p, t in {"a/config.txt": "old", "a/boot/initrd.img": "old", "bootcode.bin": "fw",
                     "settings.txt": "#hostname = audiopi\nhostname = kitchen\n", "audiolink/audiolink.pyz": "older code",
                     "audiolink/identity.json.a": "{}"}.items():
            (self.card / p).parent.mkdir(parents=True, exist_ok=True)
            (self.card / p).write_text(t)
        self.slot = SlotSh(str(self.card))
        self.orig = image_mod.SLOT_SH
        image_mod.SLOT_SH = __file__            # "exists"
        self.svc = image_mod.ImageService(state_dir=str(self.state), card=str(self.card), run=self.slot)
        self.svc._later = lambda fn: fn()

    def tearDown(self):
        image_mod.SLOT_SH = self.orig
        self.tmp.cleanup()

    def send(self, data, name="img.zip"):
        self.svc.handle("begin", {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "name": name})
        for off in range(0, len(data), 7):
            self.svc.handle("chunk", {"offset": off, "data": base64.b64encode(data[off:off + 7]).decode()})
        return self.svc.handle("finish", {})

    def test_into_the_spare_slot_then_tried(self):
        data = zipped(dict(IMAGE, **{"bootcode.bin": "new fw"}))
        r = self.send(data)
        self.assertEqual((r["slot"], r["from"], r["firmware_differs"]), ("b/", "a/", True))
        self.assertEqual((self.card / "b/boot/initrd.img").read_text(), "new initrd")
        self.assertEqual((self.card / "a/boot/initrd.img").read_text(), "old")          # the running one: untouched
        self.assertEqual((self.card / "bootcode.bin").read_text(), "fw")                 # firmware: kept
        self.assertEqual(json.loads((self.card / "b" / image_mod.INFO).read_text())["sha256"],
                         hashlib.sha256(data).hexdigest())
        settings = (self.card / "settings.txt").read_text()
        self.assertIn("hostname = kitchen", settings)                                    # what was set: kept
        self.assertIn("#audiolink.new = x", settings)                                    # the new image's: added
        self.assertFalse((self.state / "audiolink.pyz").exists())                        # its own code runs
        self.assertTrue((self.state / "identity.json.a").exists())
        self.assertFalse((self.state / "image.zip").exists())
        self.assertEqual(self.slot.calls[-1], ["try", "b/"])
        self.assertEqual(self.svc.status()["images"]["b/"]["name"], "img.zip")

    def test_damaged_or_without_slots(self):
        data = zipped(IMAGE)
        self.svc.handle("begin", {"size": len(data), "sha256": "0" * 64})
        self.svc.handle("chunk", {"offset": 0, "data": base64.b64encode(data).decode()})
        with self.assertRaisesRegex(ValueError, "damaged"):
            self.svc.handle("finish", {})
        old = {k: v for k, v in IMAGE.items() if not k.startswith("a/")}
        with self.assertRaisesRegex(ValueError, "card write"):
            self.send(zipped(dict(old, **{"boot/initrd.img": "x"})))
        self.assertNotIn(["try", "b/"], self.slot.calls)

    def test_revert(self):
        self.send(zipped(IMAGE))
        self.svc.trying = False                  # (rebooted, the trial committed)
        rebooted = []
        self.svc.reboot = lambda: rebooted.append(True)
        r = self.svc.handle("revert", {})
        self.assertEqual(r["slot"], "b/")
        self.assertEqual(self.slot.calls[-1], ["switch", "b/"])
        self.assertEqual(rebooted, [True])

    def test_nothing_new_while_on_trial(self):
        self.slot.current, self.slot.trial, self.slot.committed = "b/", True, False
        (self.card / "b").mkdir()
        (self.card / "b/config.txt").write_text("trial")
        for op in ("begin", "revert"):
            with self.assertRaisesRegex(ValueError, "trying a version"):
                self.svc.handle(op, {"size": 1, "sha256": "0" * 64})
        self.assertEqual((self.card / "a/boot/initrd.img").read_text(), "old")          # the good one: untouched
        self.assertNotIn(["switch", "a/"], self.slot.calls)

    def test_nothing_new_once_a_try_is_scheduled(self):
        later = []
        self.svc._later = later.append            # the try: not yet run, not yet booted
        self.send(zipped(IMAGE))
        with self.assertRaisesRegex(ValueError, "trying a version"):
            self.send(zipped(dict(IMAGE, **{"a/boot/initrd.img": "newer"})))
        with self.assertRaisesRegex(ValueError, "trying a version"):
            self.svc.handle("revert", {})
        failed = SimpleNamespace(returncode=1, stdout="", stderr="slot.sh: no python3")
        self.svc.run_cmd = lambda cmd, **kw: failed if cmd[2:3] == ["try"] else self.slot(cmd)
        later[0]()                               # the try failed: no reboot, nothing on trial
        self.assertFalse(self.svc.trying)
        self.svc.run_cmd = self.slot
        self.svc._later = lambda fn: fn()
        self.assertEqual(self.send(zipped(IMAGE))["slot"], "b/")

    def test_trial_commits_once_through(self):
        self.slot.trial, self.slot.committed = True, False
        through = iter([False, False, True])
        self.svc.healthy = lambda: next(through)
        t = [0.0]
        threads = []
        import threading
        real = threading.Thread

        class Now(real):                         # run the watcher in this thread
            def start(self):
                threads.append(self)
                self.run()
        threading.Thread = Now
        try:
            self.svc.run(sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
        finally:
            threading.Thread = real
        self.assertTrue(self.slot.committed)
        self.assertIn(["commit"], self.slot.calls)


class Status(unittest.TestCase):
    def test_roles_and_letters(self):
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            image_cli.show_image_status({"current": "a/", "trial": False, "committed": True, "slots": ["a/", "b/"],
                                         "images": {"a/": {"name": "clone"}, "b/": {"name": "card-pi"}}})
            image_cli.show_image_status({"current": "b/", "trial": True, "committed": False, "slots": ["b/"],
                                         "images": {}})
        self.assertEqual(out.getvalue().splitlines(), [
            "Running:  clone (a)",
            "Previous: card-pi (b): audiolink update --back",
            "Running:  as written (b) on trial: kept once it's back online",
            "Previous: none yet (the next audiolink update keeps this one to go back to)"])


class Command(unittest.TestCase):
    def test_refused_while_on_trial(self):
        calls = []

        def call(op, args=None, port=None):
            calls.append(op)
            return {"slots": ["a/", "b/"], "current": "b/", "trial": True, "committed": False, "images": {}}
        orig = image_cli.read_image
        image_cli.read_image = lambda p: calls.append("read") or ("x.zip", b"")
        try:
            with self.assertRaisesRegex(SystemExit, "trying a version.*update --status"):
                image_cli.image_command(SimpleNamespace(back=False, image=None), call, None, over_network=False)
        finally:
            image_cli.read_image = orig
        self.assertEqual(calls, ["image.status"])               # nothing downloaded or sent

    def test_build_folder_becomes_a_zip(self):
        with tempfile.TemporaryDirectory() as d:
            for p, t in IMAGE.items():
                Path(d, p).parent.mkdir(parents=True, exist_ok=True)
                Path(d, p).write_text(t)
            name, data = image_cli.read_image(d)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                self.assertEqual(sorted(z.namelist()), sorted(IMAGE))
            Path(d, "a/config.txt").unlink()
            with self.assertRaisesRegex(SystemExit, "card write"):
                image_cli.read_image(d)                          # an image from before slots

    def test_waits_for_the_commit_or_the_fall_back(self):
        data = zipped(IMAGE)
        for outcome, expect in ((("b/", False, True), None), (("a/", False, True), "went back")):
            states = iter([{"slots": ["a/"], "current": "a/", "images": {}},
                           {"current": "b/", "trial": True, "committed": False},
                           dict(zip(("current", "trial", "committed"), outcome))])
            calls = []

            def call(op, args=None, port=None):
                calls.append(op)
                if op == "image.status":
                    return next(states)
                if op == "image.finish":
                    return {"slot": "b/", "from": "a/", "firmware_differs": False}
                return {"ok": True}
            a = SimpleNamespace(back=False, image="x.zip")
            orig_read, orig_sleep = image_cli.read_image, image_cli.time.sleep
            image_cli.read_image = lambda p: ("x.zip", data)
            image_cli.time.sleep = lambda s: None
            out = io.StringIO()
            try:
                import contextlib
                with contextlib.redirect_stdout(out):
                    try:
                        image_cli.image_command(a, call, None, over_network=True)
                        err = None
                    except SystemExit as e:
                        err = str(e)
            finally:
                image_cli.read_image, image_cli.time.sleep = orig_read, orig_sleep
            if expect:
                self.assertIn(expect, err)
            else:
                self.assertIsNone(err, out.getvalue())
                self.assertIn("Trying it on the Pi... running it", out.getvalue())


if __name__ == "__main__":
    unittest.main()
