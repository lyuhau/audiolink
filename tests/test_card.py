"""audiolink card backup|restore (card_cli.py) and card.py's backup and card finding."""
import contextlib
import io
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from audiolink import card, card_cli

DISKUTIL_LIST = """/dev/disk7 (external, physical):
   #:                       TYPE NAME                    SIZE       IDENTIFIER
   0:     FDisk_partition_scheme                        *31.9 GB    disk7
   1:                 DOS_FAT_32 AUDIOPI                 31.9 GB    disk7s1
"""


class Runner:
    def __init__(self, answers):
        self.cmds, self.answers = [], answers

    def run(self, cmd, timeout=60):
        self.cmds.append(" ".join(cmd))
        for prefix, reply in self.answers.items():
            if " ".join(cmd).startswith(prefix):
                return reply
        return 0, ""


class Clock:
    def __init__(self):
        self.t = 0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make_card(root):
    for path, text in {"config.txt": "boot", "settings.txt": "#x", "boot/initrd.img": "img",
                       "audiolink/identity.json.a": "{}", "logs/last/app.log": "log", "._config.txt": "meta",
                       ".DS_Store": "", ".Spotlight-V100/store": "", ".fseventsd/x": ""}.items():
        p = Path(root, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


class Backup(unittest.TestCase):
    def test_zip_has_the_card_not_macos_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount, out = Path(tmp, "card"), Path(tmp, "backup.zip")
            make_card(mount)
            os.utime(str(mount / "logs/last/app.log"), (0, 0))       # the Pi before it knew the time
            self.assertEqual(card.backup(str(mount), out), 5)
            with zipfile.ZipFile(str(out)) as z:
                self.assertEqual(sorted(z.namelist()), ["audiolink/identity.json.a", "boot/initrd.img",
                                                        "config.txt", "logs/last/app.log", "settings.txt"])
            self.assertEqual(stat.S_IMODE(os.stat(str(out)).st_mode), 0o600)
            self.assertEqual(card.check_image(out).path, out)   # a backup is a card image too
            self.assertFalse(Path(tmp, "backup.zip.part").exists())

    def test_mounted_cards(self):
        with tempfile.TemporaryDirectory() as mount:
            Path(mount, "config.txt").write_text("")
            runner = Runner({"diskutil list": (0, DISKUTIL_LIST),
                             "diskutil info /dev/disk7": (0, "   Disk Size: 31.9 GB (31914983424 Bytes)\n"),
                             "diskutil info disk7s1": (0, "   Mount Point:   %s\n" % mount)})
            self.assertEqual(card.mounted_cards(runner), [("/dev/disk7", 31914983424, mount)])
            os.remove(os.path.join(mount, "config.txt"))            # not a Pi card
            self.assertEqual(card.mounted_cards(runner), [])


def put_files(root, files):
    for path, text in files.items():
        p = Path(root, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def image_dir(root, files):
    put_files(root, files)
    return card.Image(root)


IMAGE = {"config.txt": "new boot", "settings.txt": "# The Pi's name.\n#hostname = audiopi\n\n# New one.\n#audiolink.fresh = on\n",
         "boot/initrd.img": "new img"}


class Images(unittest.TestCase):
    def test_folder_and_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = image_dir(Path(tmp, "build"), dict(IMAGE, **{"._config.txt": "meta"}))
            self.assertEqual(folder.names, ["boot/initrd.img", "config.txt", "settings.txt"])
            with zipfile.ZipFile(str(Path(tmp, "i.zip")), "w") as z:
                for n in folder.names:
                    z.writestr(n, folder.read(n))
            self.assertEqual(dict(card.Image(Path(tmp, "i.zip")).files()), dict(folder.files()))
            with self.assertRaises(SystemExit):
                card.Image(Path(tmp, "build", "boot"))            # no config.txt, settings.txt

    def test_settings_renewed_keeping_what_was_set(self):
        old = "# Old help.\n#hostname = usb-gadget\n\nhostname = kitchen\nusb.product = Mine\n"
        text, new = card.merge_settings(old, IMAGE["settings.txt"])
        self.assertEqual(new, ["audiolink.fresh"])
        self.assertIn("# The Pi's name.\nhostname = kitchen\n", text)          # set: carried over, in place
        self.assertNotIn("usb-gadget", text)                                     # stale examples: gone
        self.assertIn("# New one.\n#audiolink.fresh = on", text)
        self.assertTrue(text.endswith("(kept as they were)\nusb.product = Mine\n"))   # not this image's: kept
        self.assertEqual(card.merge_settings(text, IMAGE["settings.txt"]), (text, []))  # stable


class Update(unittest.TestCase):
    def test_keeps_the_pis_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp, "card")
            make_card(mount)
            Path(mount, "settings.txt").write_text("#hostname = audiopi\nusb.product = Mine\n")
            Path(mount, "stale.txt").write_text("old")
            Path(mount, "audiolink/audiolink.pyz").write_text("older code")
            image = image_dir(Path(tmp, "build"), dict(IMAGE, **{"audiolink/identity.json.a": "not mine"}))
            runner = Runner({})
            n, new, removed = card.update(runner, "/dev/disk7", str(mount), image, sleep=lambda s: None)
            self.assertEqual((n, new, removed), (2, ["audiolink.fresh"], "unknown"))  # settings.txt kept, not checked
            self.assertFalse(Path(mount, "audiolink/audiolink.pyz").exists())    # the image's code runs
            self.assertFalse(Path(mount, "stale.txt").exists())
            self.assertEqual(Path(mount, "boot/initrd.img").read_text(), "new img")
            self.assertEqual(Path(mount, "audiolink/identity.json.a").read_text(), "{}")   # the Pi's, kept
            self.assertEqual(Path(mount, "logs/last/app.log").read_text(), "log")
            self.assertIn("usb.product = Mine", Path(mount, "settings.txt").read_text())
            self.assertIn("diskutil eject /dev/disk7", runner.cmds)

    def test_interrupted_write_keeps_the_old_system_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp, "card")
            make_card(mount)
            image = image_dir(Path(tmp, "build"), IMAGE)
            real = card.Image.files

            def pulled_out(self):                        # the card goes away after one file
                for i, item in enumerate(real(self)):
                    if i == 1:
                        raise OSError("card removed")
                    yield item
            with mock.patch.object(card.Image, "files", pulled_out), self.assertRaises(OSError):
                card.update(Runner({}), "/dev/disk7", str(mount), image, sleep=lambda s: None)
            self.assertEqual(Path(mount, "config.txt").read_text(), "boot")          # the old system, whole
            self.assertEqual(Path(mount, "boot/initrd.img").read_text(), "img")
            n, _, _ = card.update(Runner({}), "/dev/disk7", str(mount), image, sleep=lambda s: None)
            self.assertEqual(n, 2)
            self.assertEqual(Path(mount, "boot/initrd.img").read_text(), "new img")
            leftovers = [str(p) for p in mount.rglob("*.new")]
            self.assertEqual(leftovers, [])

    def test_keep_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp, "card")
            make_card(mount)
            Path(mount, "audiolink/audiolink.pyz").write_text("newer code")
            image = image_dir(Path(tmp, "build"), IMAGE)
            _, _, removed = card.update(Runner({}), "/dev/disk7", str(mount), image, sleep=lambda s: None,
                                        keep_update=True)
            self.assertFalse(removed)
            self.assertEqual(Path(mount, "audiolink/audiolink.pyz").read_text(), "newer code")

    def test_check_catches_a_bad_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = image_dir(Path(tmp, "build"), IMAGE)
            mount = Path(tmp, "card")
            card.put(str(mount), image)
            self.assertEqual(card.check(str(mount), image), 3)
            Path(mount, "config.txt").write_text("garbled")
            with self.assertRaises(SystemExit):
                card.check(str(mount), image)


class Classify(unittest.TestCase):
    def kind(self, files, parts=1):
        listing = "".join("   %d: DOS_FAT_32 X  1 GB   disk7s%d\n" % (i, i) for i in range(1, parts + 1))
        runner = Runner({"diskutil list /dev/disk7": (0, listing)})
        with tempfile.TemporaryDirectory() as tmp:
            put_files(tmp, files)
            return card.classify(runner, "/dev/disk7", tmp)

    def test_kinds(self):
        self.assertEqual(self.kind({}), ("blank", "blank"))
        self.assertEqual(self.kind({"audiolink/x": "", "config.txt": ""}), ("audiolink", "audiolink"))
        self.assertEqual(self.kind({"settings.txt": "#audiolink.relay = x\n", "config.txt": ""})[0], "audiolink")
        # a pi-gadget-kit card for another device: a Pi card, not ours
        self.assertEqual(self.kind({"settings.txt": "#usb.product = Arctis\n", "config.txt": ""}),
                         ("other", "a Raspberry Pi system?"))
        self.assertEqual(self.kind({"config.txt": "", "cmdline.txt": ""}, parts=2),
                         ("other", "2 partitions, a Raspberry Pi system?"))
        self.assertEqual(self.kind({"DCIM/1.jpg": ""}), ("other", "1 files, not a Pi card"))


class Command(unittest.TestCase):
    def run_card(self, *argv, usb_state=lambda: None, cards=(), through_pi=None, wipe=False, eject=False,
                 kind=("audiolink", "audiolink")):
        a = SimpleNamespace(action=argv[0], file=argv[1] if len(argv) > 1 else None, disk=None, wipe=wipe,
                            eject=eject, keep_update=False)
        runner, clock = Runner({}), Clock()
        calls = []
        patches = [mock.patch.object(card, "mounted_cards", lambda r: list(cards)),
                   mock.patch.object(card, "write", lambda r, d, i, sleep: calls.append(("write", d, i.name)) or 3),
                   mock.patch.object(card, "update", lambda r, d, m, i, sleep, keep_update:
                                     calls.append(("update", d, i.name)) or (3, ["audiolink.fresh"], "0123456789ab")),
                   mock.patch.object(card, "classify", lambda r, d, m: kind),
                   mock.patch.object(card, "open_through_pi", through_pi or (lambda *x, **k: self.fail("opened"))),
                   mock.patch.object(card, "volume_of", lambda r, d, s: cards[0][2] if cards else None)]
        out = io.StringIO()
        with contextlib.ExitStack() as stack, contextlib.redirect_stdout(out):
            for p in patches:
                stack.enter_context(p)
            try:
                code = card_cli.card_command(a, runner=runner, platform="darwin", usb_state=usb_state,
                                             sleep=clock.sleep, clock=clock)
            except SystemExit as e:
                code = str(e)
        return out.getvalue(), code, runner, calls

    CARD = [("/dev/disk7", 31.9e9, "/Volumes/AUDIOPI")]

    def image(self, tmp):
        image_dir(Path(tmp, "build"), IMAGE)
        return str(Path(tmp, "build"))

    def test_write_keeps_state_on_an_audiolink_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, code, _, calls = self.run_card("write", self.image(tmp), cards=self.CARD)
        self.assertEqual(code, 0, out)
        self.assertIn("Card: /dev/disk7, 31.9 GB, audiolink", out)
        self.assertIn("Writing build, keeping the Pi's state... done, 3 files checked", out)
        self.assertIn("New settings in its settings.txt: audiolink.fresh", out)
        self.assertIn("Removed an earlier update (build 0123456789ab) from the card, so the Pi runs this image's code", out)
        self.assertEqual(calls, [("update", "/dev/disk7", "build")])

    def test_another_system_needs_wipe(self):
        other = ("other", "2 partitions, a Raspberry Pi system?")
        with tempfile.TemporaryDirectory() as tmp:
            out, code, _, calls = self.run_card("write", self.image(tmp), cards=self.CARD, kind=other)
            self.assertEqual(code, "Not an audiolink card: nothing changed. To erase it and write build: --wipe")
            self.assertEqual(calls, [])
            out, code, _, calls = self.run_card("write", self.image(tmp), cards=self.CARD, kind=other, wipe=True)
        self.assertEqual(code, 0, out)
        self.assertIn("Erasing it and writing build... done", out)
        self.assertEqual(calls, [("write", "/dev/disk7", "build")])

    def test_blank_card_written_without_wipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, code, _, calls = self.run_card("write", self.image(tmp), cards=self.CARD, kind=("blank", "blank"))
        self.assertEqual(code, 0, out)
        self.assertIn("Writing build... done", out)
        self.assertEqual(calls, [("write", "/dev/disk7", "build")])

    def test_restore_is_write_wipe_without_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_card(Path(tmp, "card"))
            card.backup(str(Path(tmp, "card")), Path(tmp, "b.zip"))
            out, code, _, calls = self.run_card("restore", str(Path(tmp, "b.zip")), cards=self.CARD)
        self.assertEqual(code, 0, out)
        self.assertEqual(calls, [("write", "/dev/disk7", "b.zip")])
        self.assertIn("Put the card back in the Pi", out)

    def test_backup_of_a_mounted_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_card(Path(tmp, "card"))
            dest = Path(tmp, "b.zip")
            out, code, runner, _ = self.run_card("backup", str(dest), cards=[("/dev/disk7", 32e9, str(Path(tmp, "card")))])
            self.assertEqual(code, 0, out)
            self.assertIn("Backing up the card... 5 files", out)
            self.assertIn("keep it private", out)
            self.assertIn("Card still open: write or restore onto it now", out)
            self.assertNotIn("diskutil eject /dev/disk7", runner.cmds)
            self.assertTrue(dest.exists())
            out, code, _, _ = self.run_card("backup", str(dest), cards=[("/dev/disk7", 32e9, "/x")])
            self.assertIn("exists; nothing written", code)               # never over an old backup
            dest.unlink()
            out, code, runner, _ = self.run_card("backup", str(dest), eject=True,
                                                 cards=[("/dev/disk7", 32e9, str(Path(tmp, "card")))])
            self.assertIn("diskutil eject /dev/disk7", runner.cmds)
            self.assertIn("Put the card back in the Pi", out)

    def test_through_the_pi_once_it_waits_without_its_card(self):
        states = iter(["pi", "pi", "pi", "rom"])
        with tempfile.TemporaryDirectory() as tmp:
            out, code, _, calls = self.run_card(
                "write", self.image(tmp), wipe=True, usb_state=lambda: next(states, "msd"),
                through_pi=lambda *x, **k: ("/dev/disk9", 16e9))
        self.assertEqual(code, 0, out)
        self.assertIn("The Pi is running from its card", out)
        self.assertIn("Waiting for the Pi without its card... ready", out)
        self.assertEqual(calls, [("write", "/dev/disk9", "build")])
        self.assertIn("Unplug the Pi and plug it back in", out)

    def test_not_a_card_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(str(Path(tmp, "x.zip")), "w") as z:
                z.writestr("hello.txt", "")
            out, code, _, _ = self.run_card("restore", str(Path(tmp, "x.zip")))
        self.assertIn("isn't an audiolink card image", code)


if __name__ == "__main__":
    unittest.main()
