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
            self.assertEqual(card.backup(str(mount), out), 5)
            with zipfile.ZipFile(str(out)) as z:
                self.assertEqual(sorted(z.namelist()), ["audiolink/identity.json.a", "boot/initrd.img",
                                                        "config.txt", "logs/last/app.log", "settings.txt"])
            self.assertEqual(stat.S_IMODE(os.stat(str(out)).st_mode), 0o600)
            self.assertEqual(card.check_image(out), out)        # a backup is a card image too
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


class Command(unittest.TestCase):
    def run_card(self, *argv, usb_state=lambda: None, cards=(), through_pi=None, yes=False):
        a = SimpleNamespace(action=argv[0], file=argv[1] if len(argv) > 1 else None, disk=None, yes=yes)
        runner, clock = Runner({}), Clock()
        written = []
        patches = [mock.patch.object(card, "mounted_cards", lambda r: list(cards)),
                   mock.patch.object(card, "write", lambda r, d, i, sleep: written.append((d, i.name))),
                   mock.patch.object(card, "open_through_pi", through_pi or (lambda *x, **k: self.fail("opened"))),
                   mock.patch.object(card, "mount_of", lambda r, d, s: cards[0][2] if cards else None)]
        out = io.StringIO()
        with contextlib.ExitStack() as stack, contextlib.redirect_stdout(out):
            for p in patches:
                stack.enter_context(p)
            try:
                code = card_cli.card_command(a, runner=runner, platform="darwin", usb_state=usb_state,
                                             sleep=clock.sleep, clock=clock)
            except SystemExit as e:
                code = str(e)
        return out.getvalue(), code, runner, written

    def test_backup_of_a_mounted_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_card(Path(tmp, "card"))
            dest = Path(tmp, "b.zip")
            out, code, runner, _ = self.run_card("backup", str(dest), cards=[("/dev/disk7", 32e9, str(Path(tmp, "card")))])
            self.assertEqual(code, 0, out)
            self.assertIn("Backing up the card... 5 files", out)
            self.assertIn("keep it private", out)
            self.assertIn("diskutil eject /dev/disk7", runner.cmds)
            self.assertTrue(dest.exists())
            out, code, _, _ = self.run_card("backup", str(dest), cards=[("/dev/disk7", 32e9, "/x")])
            self.assertIn("exists; nothing written", code)               # never over an old backup

    def test_restore_asks_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_card(Path(tmp, "card"))
            card.backup(str(Path(tmp, "card")), Path(tmp, "b.zip"))
            cards = [("/dev/disk7", 31.9e9, "/Volumes/AUDIOPI")]
            out, code, _, written = self.run_card("restore", str(Path(tmp, "b.zip")), cards=cards)
            self.assertIn("without a terminal (answer it with: --yes)", code)     # never erases unasked
            self.assertEqual(written, [])
            out, code, _, written = self.run_card("restore", str(Path(tmp, "b.zip")), cards=cards, yes=True)
            self.assertEqual(code, 0, out)
            self.assertIn("Erase the card (/dev/disk7, 31.9 GB, AUDIOPI) and restore b.zip? yes", out)
            self.assertEqual(written, [("/dev/disk7", "b.zip")])

    def test_through_the_pi_once_it_waits_without_its_card(self):
        states = iter(["pi", "pi", "pi", "rom"])
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "img.zip").write_bytes(b"")
            with zipfile.ZipFile(str(Path(tmp, "img.zip")), "w") as z:
                z.writestr("config.txt", "")
                z.writestr("settings.txt", "")
            out, code, _, written = self.run_card(
                "restore", str(Path(tmp, "img.zip")), yes=True, usb_state=lambda: next(states, "msd"),
                through_pi=lambda *x, **k: ("/dev/disk9", 16e9))
        self.assertEqual(code, 0, out)
        self.assertIn("The Pi is running from its card", out)
        self.assertIn("Waiting for the Pi without its card... ready", out)
        self.assertEqual(written, [("/dev/disk9", "img.zip")])
        self.assertIn("Unplug the Pi and plug it back in", out)

    def test_not_a_card_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(str(Path(tmp, "x.zip")), "w") as z:
                z.writestr("hello.txt", "")
            out, code, _, _ = self.run_card("restore", str(Path(tmp, "x.zip")), yes=True)
        self.assertIn("isn't an audiolink card image", code)


if __name__ == "__main__":
    unittest.main()
