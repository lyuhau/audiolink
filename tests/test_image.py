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
        self.failed = Path(self.tmp.name, "trial-failed")          # slot.sh guard's record
        self.svc = image_mod.ImageService(state_dir=str(self.state), card=str(self.card), run=self.slot,
                                          trial_failed=str(self.failed))
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
        self.assertEqual((r["slot"], r["from"], r["firmware_differs"], r["usb_settings"]), ("b/", "a/", True, False))
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
        self.assertEqual(self.svc.status()["images"]["b/"]["state"], "trying")

    def test_says_the_card_sets_usb(self):
        (self.card / "settings.txt").write_text("#hostname = audiopi\nusb.vendor_id = 0x1234\n")
        self.assertTrue(self.send(zipped(IMAGE))["usb_settings"])
        self.assertIn("usb.vendor_id = 0x1234", (self.card / "settings.txt").read_text())     # kept as it was

    def test_version_recorded(self):
        version = {"version": "0.15.0", "build": "3871eed7e803", "release": False}
        self.send(zipped(dict(IMAGE, **{"a/" + image_mod.VERSION_FILE: json.dumps(version)})))
        self.assertEqual({k: self.info("b/")[k] for k in version}, version)
        # INFO from before versions (an older Pi wrote it): the slot's own file says
        (self.card / "a" / image_mod.VERSION_FILE).write_text(json.dumps(dict(version, version="0.14.0")))
        (self.card / "a" / image_mod.INFO).write_text(json.dumps({"name": "card-pi"}))
        self.assertEqual(self.svc.status()["images"]["a/"]["version"], "0.14.0")

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

    def info(self, slot):
        return json.loads((self.card / slot / image_mod.INFO).read_text())

    def test_half_written_is_marked(self):
        fsync, n = os.fsync, []

        def failing(fd):
            n.append(fd)
            if len(n) == 2:
                raise OSError("card gone")
            fsync(fd)
        image_mod.os.fsync = failing
        try:
            with self.assertRaises(OSError):
                self.send(zipped(IMAGE))
        finally:
            image_mod.os.fsync = fsync
        self.assertEqual(self.info("b")["state"], "writing")
        self.assertNotIn(["try", "b/"], self.slot.calls)
        with self.assertRaisesRegex(ValueError, "partly written: send a new one"):
            self.svc.handle("revert", {})

    def test_revert_is_a_trial(self):
        self.send(zipped(IMAGE))
        self.svc.trying = False                   # (rebooted: a new daemon)
        self.failed.write_text("b/\n")            # the trial of b/ wasn't kept: a/ runs, committed
        self.svc.run()
        self.assertEqual(self.info("b")["state"], "failed")
        self.assertIn("failed_at", self.info("b"))
        with self.assertRaisesRegex(ValueError, "didn't work: send a new one"):
            self.svc.handle("revert", {})
        (self.card / "b" / image_mod.INFO).write_text(json.dumps({"name": "card-pi"}))  # a card write's: fine
        r = self.svc.handle("revert", {})
        self.assertEqual((r["slot"], r["from"]), ("b/", "a/"))
        self.assertEqual(self.slot.calls[-1], ["try", "b/"])
        self.assertNotIn("switch", [c[0] for c in self.slot.calls])
        self.assertEqual(self.info("b")["state"], "trying")

    def test_trying_alone_isnt_failed(self):
        # --back to a version from before slot states: it keeps itself without saying so,
        # then a newer one goes in a/ again; b/ still says "trying" but ran fine
        (self.card / "b" / "config.txt").parent.mkdir(parents=True)
        (self.card / "b" / "config.txt").write_text("x")
        (self.card / "b" / image_mod.INFO).write_text(json.dumps({"name": "0.14.0", "state": "trying"}))
        self.svc.run()
        self.assertEqual(self.info("b")["state"], "trying")
        self.assertTrue(image_mod.offered(self.info("b"), "b/", "a/"))
        self.assertEqual(self.svc.handle("revert", {})["slot"], "b/")

    def test_trial_of_the_running_one_kept(self):
        (self.card / "a" / image_mod.INFO).write_text(json.dumps({"name": "x", "state": "trying"}))
        self.svc.run()                            # committed by /init, or before this daemon
        self.assertEqual(self.info("a")["state"], "kept")
        self.slot.trial, self.slot.committed = True, False
        (self.card / "a" / image_mod.INFO).write_text(json.dumps({"name": "x", "state": "trying"}))
        self.svc._settle(image_mod.slot_status(self.slot))
        self.assertEqual(self.info("a")["state"], "trying")    # on trial: left alone

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

    def watch(self, left):
        """Runs the trial watcher in this thread on a fake clock -> seconds it watched."""
        t = [0.0]
        import threading
        real = threading.Thread

        class Now(real):                         # run the watcher in this thread
            def start(self):
                self.run()
        threading.Thread = Now
        try:
            self.svc.run(sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0], left=lambda: left)
        finally:
            threading.Thread = real
        return t[0]

    def test_trial_commits_once_through(self):
        self.slot.trial, self.slot.committed = True, False
        through = iter([False, False, True])
        self.svc.healthy = lambda: next(through)
        self.watch(100)
        self.assertTrue(self.slot.committed)
        self.assertIn(["commit"], self.slot.calls)
        self.assertEqual(self.info("a")["state"], "kept")

    def test_trial_tries_until_the_guard(self):
        self.slot.trial, self.slot.committed = True, False
        self.svc.healthy = lambda: False
        self.assertGreaterEqual(self.watch(250), 250)            # the whole window, not a shorter one
        self.assertNotIn(["commit"], self.slot.calls)

    def test_trial_left_counts_from_boot(self):
        with tempfile.NamedTemporaryFile("w", suffix="uptime") as f:
            f.write("40.50 70.00\n")
            f.flush()
            self.assertEqual(image_mod.trial_left({"TRIAL_SECONDS": "300"}, f.name),
                             300 - 40.5 - image_mod.COMMIT_MARGIN)
            self.assertEqual(image_mod.trial_left({"TRIAL_SECONDS": "600"}, f.name),
                             600 - 40.5 - image_mod.COMMIT_MARGIN)
            self.assertEqual(image_mod.trial_left({}, f.name),                   # an older app.env
                             image_mod.TRIAL_SECONDS - 40.5 - image_mod.COMMIT_MARGIN)

    def test_one_window_with_the_image(self):
        """The host's and the daemon's window is the one the release image's guard uses."""
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                self.skipTest("no tomllib (Python 3.11+) or tomli")
        repo = Path(__file__).resolve().parent.parent
        with open(repo / "config" / "pi-release.toml", "rb") as f:
            app = tomllib.load(f)["app"]
        build_card = repo / "platform" / "build_card.py"
        if build_card.exists():                     # (CI's test jobs check out no submodule)
            self.assertIn('app.get("trial_seconds", 300)', build_card.read_text())
        self.assertEqual(app.get("trial_seconds", 300), image_mod.TRIAL_SECONDS)
        self.assertIn("within %d minutes" % image_cli.minutes(image_mod.TRIAL_SECONDS),
                      (repo / "README.md").read_text())

    def test_status_tells_the_computer(self):
        self.assertFalse(self.svc.status()["trying"])
        later = []
        self.svc._later = later.append            # the try: scheduled, not run yet
        self.send(zipped(IMAGE))
        st = self.svc.status({"TRIAL_SECONDS": "600"})
        self.assertEqual((st["back"], st["trial_seconds"], st["trying"]), ("trial", 600, True))
        self.assertEqual(self.svc.status({})["trial_seconds"], image_mod.TRIAL_SECONDS)

    def test_code_update_goes_with_a_new_image(self):
        self.assertTrue(self.send(zipped(IMAGE))["code_update_gone"])
        self.assertFalse((self.state / "audiolink.pyz").exists())


class VersionFile(unittest.TestCase):
    def test_the_build_writes_it(self):
        repo = Path(__file__).resolve().parent.parent
        self.assertIn('"%s%s"' % (image_mod.IMAGE_SLOT, image_mod.VERSION_FILE),
                      (repo / "tools" / "build_card.sh").read_text())


class Status(unittest.TestCase):
    def show(self, *args, verbose=False):
        import contextlib
        from audiolink import ui
        out, ui.VERBOSE = io.StringIO(), verbose
        try:
            with contextlib.redirect_stdout(out):
                image_cli.show_image_status(*args)
        finally:
            ui.VERBOSE = False
        return out.getvalue().splitlines()

    def test_versions_in_words(self):
        rel = {"version": "0.14.0", "build": "3871eed7e803", "release": True}
        self.assertEqual(image_mod.version_name(rel), "0.14.0")
        self.assertEqual(image_mod.version_name(dict(rel, release=False)), "0.14.0 dev build 3871eed7")
        self.assertEqual(image_mod.version_name({"version": "0.14.0"}), "0.14.0 dev build")
        # INFO from before versions: a release zip says its version; else its name
        self.assertEqual(image_mod.version_name({"name": "audiolink-pi-0.13.2.zip"}), "0.13.2")
        self.assertEqual(image_mod.version_name({"name": "card-pi"}), "card-pi")
        self.assertIsNone(image_mod.version_name({}))
        # ... or what the Pi runs, for its own slot
        running = {"version": "0.14.0", "build": "d1a5f2b90444"}
        self.assertEqual(image_mod.version_name({"name": "card-pi"}, running), "0.14.0 dev build d1a5f2b9")
        self.assertEqual(image_mod.version_name({"name": "audiolink-pi-0.14.0.zip"}, running), "0.14.0")
        self.assertEqual(image_mod.version_name({}, running), "0.14.0")
        self.assertEqual(image_mod.version_info(b'{"version": "1", "build": 7, "release": true}'),
                         {"version": "1", "release": True})
        self.assertEqual(image_mod.version_info(b"not json"), {})

    def test_roles(self):
        lines = []
        lines += self.show({"current": "a/", "trial": False, "committed": True, "slots": ["a/", "b/"], "back": "trial",
                            "images": {"a/": {"name": "clone"}, "b/": {"name": "card-pi"}}})
        lines += self.show({"current": "b/", "trial": True, "committed": False, "slots": ["b/"], "images": {}})
        for state in ("failed", "trying", "writing", "kept"):
            lines += self.show({"current": "a/", "slots": ["a/", "b/"], "back": "trial",
                                "images": {"b/": {"version": "0.15.0", "release": True, "state": state}}},
                               None, "0.14.0")
        # v0.14.0's Pi: no "back": "trial"; its --back is for good, whatever b/ holds
        lines += self.show({"current": "a/", "slots": ["a/", "b/"], "images": {"b/": {"version": "0.15.0"}}},
                           None, "0.14.0")
        self.assertEqual(lines, [
            "Running:  clone",
            "Previous: an earlier dev build (audiolink update --back)",
            "Running:  the version written to its card on trial: kept once it's back online",
            "Previous: none yet (the next audiolink update keeps this one to go back to)",
            "Running:  0.14.0",
            "Previous: 0.15.0: didn't work, not offered",
            "Running:  0.14.0",
            "Previous: 0.15.0 (audiolink update --back)",
            "Running:  0.14.0",
            "Previous: 0.15.0: partly written, not offered",
            "Running:  0.14.0",
            "Previous: 0.15.0 (audiolink update --back)",
            "Running:  0.14.0",
            "Previous: 0.15.0 dev build (this Pi's audiolink goes back without a trial: update it first)"])

    def test_letters_and_builds_with_v(self):
        st = {"current": "a/", "slots": ["a/", "b/"], "back": "trial",
              "images": {"a/": {"name": "card-pi", "version": "0.14.0", "build": "3871eed7e803", "state": "kept"},
                         "b/": {"name": "audiolink-pi-0.13.2.zip"}}}
        self.assertEqual(self.show(st, verbose=True), [
            "Running:  0.14.0 dev build 3871eed7  [a/, card-pi, kept, build 3871eed7e803]",
            "Previous: 0.13.2 (audiolink update --back)  [b/, audiolink-pi-0.13.2.zip]"])
        st["images"]["b/"] = {"name": "card-pi"}
        self.assertEqual(self.show(st, verbose=True)[1],
                         "Previous: an earlier dev build (audiolink update --back)  [b/, card-pi]")

    def test_a_code_update_isnt_the_images_version(self):
        st = {"current": "a/", "slots": ["a/"], "images": {"a/": {}}}
        code = {"running": image_mod.PI_STATE_DIR + "/audiolink.pyz", "running_build": "ffff"}
        self.assertEqual(self.show(st, code, "0.99.0")[0], "Running:  the version written to its card")
        code = {"running": "/opt/audiolink/audiolink.pyz", "running_build": "d1a5f2b90444"}
        self.assertEqual(self.show(st, code, "0.14.0")[0], "Running:  0.14.0")


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
            states = iter([{"slots": ["a/"], "current": "a/", "images": {}, "back": "trial", "trial_seconds": 600},
                           {"current": "a/", "trial": False, "committed": True, "trying": True},  # not yet
                           {"current": "b/", "trial": True, "committed": False},
                           dict(zip(("current", "trial", "committed"), outcome), trying=False)])
            calls = []

            def call(op, args=None, port=None):
                calls.append(op)
                if op == "image.status":
                    return next(states)
                if op == "image.finish":
                    return {"slot": "b/", "from": "a/", "firmware_differs": False, "usb_settings": True}
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
                self.assertIn("audiolink logs prev/boot.log", err)      # the trial's boot: the one before
                self.assertIn("Allow accessory", err)
            else:
                self.assertIsNone(err, out.getvalue())
                self.assertIn("within 10 minutes", out.getvalue())           # the Pi's window
                self.assertIn("Trying it on the Pi (up to 13 minutes)... running it", out.getvalue())
                self.assertIn("settings.txt sets usb.* (product name, IDs): these no longer apply", out.getvalue())

    def test_back_refused_before_slowing(self):
        calls = []
        failed = {"current": "a/", "trial": False, "committed": True, "slots": ["a/", "b/"], "back": "trial",
                  "images": {"b/": {"version": "0.15.0", "release": True, "state": "failed"}}}

        def call(op, args=None, port=None):
            calls.append(op)
            return failed
        with self.assertRaisesRegex(SystemExit, r"version before \(0.15.0\) didn't work: send a new one.*"
                                                r"audiolink update audiolink-pi-VER.zip"):
            image_cli.image_command(SimpleNamespace(back=True), call, None, over_network=False)
        self.assertEqual(calls, ["image.status"])                 # the link left at its speed
        # v0.14.0's Pi: its revert is for good, no trial (and b/ has no state): not without --permanent
        del failed["back"], failed["images"]["b/"]["state"]
        with self.assertRaisesRegex(SystemExit, "for good.*--back --permanent"):
            image_cli.image_command(SimpleNamespace(back=True), call, None, over_network=False)
        self.assertEqual(calls, ["image.status"] * 2)

    def test_back_waits_for_its_trial(self):
        from audiolink.host import update_cli
        for outcome, expect in ((("b/", False, True), None), (("a/", False, True), "version before didn't")):
            states = iter([{"current": "a/", "trial": False, "committed": True, "slots": ["a/", "b/"],
                            "back": "trial", "images": {"b/": {"name": "x"}}},
                           {"current": "b/", "trial": True, "committed": False},
                           dict(zip(("current", "trial", "committed"), outcome), trying=False)])
            calls = []

            def call(op, args=None, port=None):
                calls.append(op)
                if op == "image.status":
                    return next(states)
                return {"ok": True, "slot": "b/", "from": "a/"}
            orig = update_cli.to_baseline, update_cli.restore, image_cli.time.sleep
            update_cli.to_baseline = lambda call, port: (calls.append("baseline"), (True, True))[1]
            update_cli.restore = lambda call, port: calls.append("restore")
            image_cli.time.sleep = lambda s: None
            out = io.StringIO()
            try:
                import contextlib
                with contextlib.redirect_stdout(out):
                    try:
                        image_cli.image_command(SimpleNamespace(back=True), call, None, over_network=False)
                        err = None
                    except SystemExit as e:
                        err = str(e)
            finally:
                update_cli.to_baseline, update_cli.restore, image_cli.time.sleep = orig
            self.assertEqual(calls[:3], ["image.status", "baseline", "image.revert"])
            if expect:
                self.assertIn(expect, err)
            else:
                self.assertIsNone(err, out.getvalue())
                self.assertIn("Trying it on the Pi (up to 8 minutes)... running it", out.getvalue())
                self.assertEqual(calls[-1], "restore")

    def test_speeds_the_link_back_up_either_way(self):
        from audiolink.host import update_cli
        data = zipped(IMAGE)
        for landed in ("b/", "a/"):                            # kept, or went back
            states = iter([{"slots": ["a/"], "current": "a/", "images": {}, "back": "trial"},
                           {"current": landed, "trial": False, "committed": True, "trying": False}])

            def call(op, args=None, port=None):
                if op == "image.status":
                    return next(states)
                if op == "image.finish":
                    return {"slot": "b/", "from": "a/", "firmware_differs": False}
                return {"ok": True}
            restored = []
            saved = (image_cli.read_image, image_cli.time.sleep, update_cli.to_baseline, update_cli.restore)
            image_cli.read_image = lambda p: ("x.zip", data)
            image_cli.time.sleep = lambda s: None
            update_cli.to_baseline = lambda call, port: (True, True)
            update_cli.restore = lambda call, port: restored.append(port)
            try:
                import contextlib
                with contextlib.redirect_stdout(io.StringIO()):
                    try:
                        image_cli.image_command(SimpleNamespace(back=False, image="x.zip"), call, 7, over_network=False)
                    except SystemExit:
                        self.assertEqual(landed, "a/")
            finally:
                image_cli.read_image, image_cli.time.sleep, update_cli.to_baseline, update_cli.restore = saved
            self.assertEqual(restored, [7], landed)

    def test_speeds_the_link_back_up_when_finish_fails(self):
        from audiolink.host import update_cli
        data = zipped(IMAGE)

        def call(op, args=None, port=None):
            if op == "image.status":
                return {"slots": ["a/"], "current": "a/", "images": {}, "back": "trial"}
            if op == "image.finish":
                raise SystemExit("the image arrived damaged; nothing changed")
            return {"ok": True}
        restored = []
        saved = (image_cli.read_image, update_cli.to_baseline, update_cli.restore)
        image_cli.read_image = lambda p: ("x.zip", data)
        update_cli.to_baseline = lambda call, port: (True, True)
        update_cli.restore = lambda call, port: restored.append(port)
        try:
            import contextlib
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(SystemExit, "damaged"):
                image_cli.image_command(SimpleNamespace(back=False, image="x.zip"), call, 7, over_network=False)
        finally:
            image_cli.read_image, update_cli.to_baseline, update_cli.restore = saved
        self.assertEqual(restored, [7])

    def wait(self, answers):
        """wait_trial from a/ to b/ over these answers (an exception: no answer) -> its SystemExit or None."""
        answers = iter(answers)

        def call(op, args=None, port=None):
            a = next(answers, None)
            if a is None or isinstance(a, BaseException):
                raise a or SystemExit("no Pi")
            return a
        orig, clock = image_cli.time.sleep, [0.0]
        image_cli.time.sleep = lambda s: clock.__setitem__(0, clock[0] + s)
        orig_mono, image_cli.time.monotonic = image_cli.time.monotonic, lambda: clock[0]
        try:
            import contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                image_cli.wait_trial(call, None, "a/", "b/", False, "went back")
        except SystemExit as e:
            return str(e)
        finally:
            image_cli.time.sleep, image_cli.time.monotonic = orig, orig_mono

    def test_an_answer_before_the_try_isnt_went_back(self):
        old = {"current": "a/", "trial": False, "committed": True}
        new = {"current": "b/", "trial": False, "committed": True}
        # This Pi says a try is scheduled; then it isn't
        self.assertIsNone(self.wait([dict(old, trying=True), dict(new, trying=False)]))
        self.assertEqual(self.wait([dict(old, trying=True), dict(old, trying=False)]), "went back")
        # A Pi that doesn't say (v0.14.0): only once it has dropped off
        self.assertIsNone(self.wait([old, old, SystemExit("rebooting"), new]))
        self.assertEqual(self.wait([old, SystemExit("rebooting"), old]), "went back")
        self.assertIn("unplugged and plugged in again", self.wait([old] * 500))
        self.assertIn("--status --pi NAME", self.wait([]))

    def test_back_for_good_on_an_old_pi(self):
        from audiolink.host import update_cli
        states = iter([{"current": "a/", "trial": False, "committed": True, "slots": ["a/", "b/"],
                        "images": {"b/": {"name": "audiolink-pi-0.13.2.zip"}}},
                       {"current": "b/", "trial": False, "committed": False},     # switched, not rebooted yet
                       SystemExit("rebooting"),
                       {"current": "b/", "trial": False, "committed": True}])
        calls = []

        def call(op, args=None, port=None):
            calls.append(op)
            if op == "image.status":
                s = next(states)
                if isinstance(s, BaseException):
                    raise s
                return s
            return {"ok": True, "slot": "b/", "from": "a/"}
        saved = (update_cli.to_baseline, update_cli.restore, image_cli.time.sleep)
        update_cli.to_baseline = lambda call, port: (False, False)
        update_cli.restore = lambda call, port: None
        image_cli.time.sleep = lambda s: None
        out = io.StringIO()
        try:
            import contextlib
            with contextlib.redirect_stdout(out):
                image_cli.image_command(SimpleNamespace(back=True, permanent=True), call, None, over_network=True)
        finally:
            update_cli.to_baseline, update_cli.restore, image_cli.time.sleep = saved
        self.assertIn("Going back to version 0.13.2 for good (no trial)", out.getvalue())
        self.assertIn("running it", out.getvalue())
        self.assertIn("image.revert", calls)


if __name__ == "__main__":
    unittest.main()
