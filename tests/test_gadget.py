"""wire/gadget.py (audio format switches) and paths.platform_env."""
import os
import shutil
import stat
import sys
import tempfile
import unittest

from audiolink.paths import platform_env
from audiolink.wire.fmt import BASELINE, AudioFormat
from audiolink.wire.gadget import Gadget


class PlatformEnv(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "app.env")
        with open(self.path, "w") as f:
            f.write("# comment\nAPP_NAME=audiolink\nAPP_RUN='/opt/audiolink/app-start.sh --x'\n"
                    "CARD=/media/mmcblk0p1\nGADGET=/sys/kernel/config/usb_gadget/device\nSTATE_ON_CARD=\n")

    def test_file_then_environment(self):
        env = platform_env(self.path, {"GADGET": "/elsewhere", "HOME": "/root"})
        self.assertEqual(env["APP_RUN"], "/opt/audiolink/app-start.sh --x")
        self.assertEqual(env["CARD"], "/media/mmcblk0p1")
        self.assertEqual(env["GADGET"], "/elsewhere")
        self.assertEqual(env["STATE_ON_CARD"], "")
        self.assertNotIn("HOME", env)

    def test_off_the_platform(self):
        self.assertEqual(platform_env(os.path.join(self.dir, "none"), {}), {})


def make_gadget(root, function, bint="4"):
    fn = os.path.join(root, "functions", function)
    os.makedirs(fn)
    os.makedirs(os.path.join(root, "configs", "c.1"))
    for d in "cp":
        for name, v in (("chmask", "3"), ("srate", "48000"), ("ssize", "2"), ("hs_bint", bint)):
            with open(os.path.join(fn, "%s_%s" % (d, name)), "w") as f:
                f.write(v + "\n")
    os.symlink(fn, os.path.join(root, "configs", "c.1", function))
    with open(os.path.join(root, "UDC"), "w") as f:
        f.write("3f980000.usb\n")


def read(path):
    with open(path) as f:
        return f.read().strip()


FAST = AudioFormat(96000, 8)

# The gadget is the Pi's: its stand-in here is configfs made of symlinks (and gadget.sh)
pi_only = unittest.skipIf(sys.platform == "win32", "the Pi's USB gadget (Linux): symlinks and sh scripts")


@pi_only
class OnThePlatform(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.root = os.path.join(self.dir, "device")
        make_gadget(self.root, "uac2.audio")
        self.calls = os.path.join(self.dir, "calls")
        self.sh = os.path.join(self.dir, "gadget.sh")      # records its arguments
        self.status = 0
        self.write_sh()
        self.g = Gadget(env={"GADGET": self.root, "GADGET_SH": self.sh})

    def write_sh(self):
        with open(self.sh, "w") as f:
            f.write('echo "$@" >> %s\n[ %d = 0 ] || { echo "gadget.sh: rejected" >&2; exit %d; }\n'
                    % (self.calls, self.status, self.status))
        os.chmod(self.sh, stat.S_IRWXU)

    def test_finds_its_function(self):
        self.assertTrue(self.g.available())
        self.assertEqual(self.g.uac2.name, "uac2.audio")
        self.assertEqual(self.g.current(), BASELINE)

    def test_set_uses_gadget_sh(self):
        self.g.set_format(FAST)
        # 96k x 8 x 2 bytes doesn't fit a packet every 8 microframes: bInterval 1
        self.assertEqual(read(self.calls),
                         "set uac2.audio c_chmask=255 c_srate=96000 c_ssize=2 c_hs_bint=1 "
                         "p_chmask=255 p_srate=96000 p_ssize=2 p_hs_bint=1")
        self.g.set_format(BASELINE)
        self.assertTrue(read(self.calls).endswith("p_chmask=3 p_srate=48000 p_ssize=2 p_hs_bint=4"))

    def test_refused(self):
        self.status = 1
        self.write_sh()
        with self.assertRaisesRegex(OSError, "rejected"):
            self.g.set_format(FAST)


@pi_only
class WithoutThePlatform(unittest.TestCase):
    """The older, pre-platform image's gadget, changed here."""
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        make_gadget(self.dir, "uac2.usb0")
        self.g = Gadget(self.dir, env={})

    def test_set_here(self):
        self.assertEqual(self.g.uac2.name, "uac2.usb0")
        self.g.set_format(FAST)
        fn = os.path.join(self.dir, "functions", "uac2.usb0")
        self.assertEqual([read(os.path.join(fn, a)) for a in ("c_chmask", "p_srate", "p_hs_bint")],
                         ["255", "96000", "1"])
        self.assertTrue(os.path.islink(os.path.join(self.dir, "configs", "c.1", "uac2.usb0")))
        self.assertEqual(read(os.path.join(self.dir, "UDC")), "3f980000.usb")
        self.assertEqual(self.g.current(), FAST)


if __name__ == "__main__":
    unittest.main()
