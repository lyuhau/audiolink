"""Pi: re-present the UAC2 gadget in another audio format.

The format lives in the function's configfs attributes, which can only change while
the function is in no configuration, so a switch is: unbind (the host sees the device
unplugged), unlink, write the attributes, link back, bind again (the host enumerates
the new format). On the usb platform its gadget.sh does that (`gadget.sh set audio
c_srate=...`: it relinks every function in order and brings usb0 up after the bind);
the device starts in the BASELINE format (config/device.toml). Without the platform
(the older image, pi/init's gadget) it's done here.
"""
import glob
import logging
import os
import subprocess
import time
from pathlib import Path

from ..paths import platform_env
from .fmt import BASELINE, AudioFormat

log = logging.getLogger("audiolink")

LEGACY_GADGET = Path("/sys/kernel/config/usb_gadget/audiopi")      # pi/init's
FUNCTION = "uac2.audio"         # config/device.toml's uac2 function, id "audio"
MAX_PACKET = 1024               # high-speed isochronous, one transaction per microframe


class Gadget:
    def __init__(self, root=None, gadget_sh=None, env=None):
        env = platform_env() if env is None else env
        if root is None:
            root = env.get("GADGET") or LEGACY_GADGET
        self.root = Path(root)
        self.gadget_sh = gadget_sh if gadget_sh is not None else env.get("GADGET_SH")
        self.uac2 = self.root / "functions" / self._function()
        # The device leaves bInterval at the kernel default; formats too fast for it use 1.
        self.default_bint = {d: self._read("%s_hs_bint" % d) for d in "cp"}

    def _function(self):
        if (self.root / "functions" / FUNCTION).is_dir():
            return FUNCTION
        found = sorted(glob.glob(str(self.root / "functions" / "uac2.*")))    # pi/init's uac2.usb0
        return os.path.basename(found[0]) if found else FUNCTION

    def available(self):
        return self.uac2.is_dir()

    def _read(self, name):
        try:
            return (self.uac2 / name).read_text().strip()
        except OSError:
            return None

    def _write(self, path, value):
        with open(path, "w") as f:
            f.write(str(value))

    def current(self):
        """The format the gadget is presenting (None if unknown)."""
        try:
            return AudioFormat(int(self._read("p_srate").split(",")[0]),
                               bin(int(self._read("p_chmask"))).count("1"))
        except (AttributeError, ValueError):
            return None

    def bint_for(self, fmt):
        """bInterval (2^(n-1) microframes per packet) for this format: the default when
        a packet fits, else every microframe."""
        default = self.default_bint["c"]
        if fmt == BASELINE or default in (None, "0"):     # 0 = the kernel picks one that fits
            return default
        per_packet = fmt.bytes_per_sec / 8000 * 2 ** (int(default) - 1) + fmt.frame_bytes
        return default if per_packet <= MAX_PACKET else "1"

    def attributes(self, fmt):
        """The function's attributes for this format, in writing order."""
        bint = self.bint_for(fmt)
        attrs = []
        for d in "cp":
            attrs += [("%s_chmask" % d, (1 << fmt.channels) - 1), ("%s_srate" % d, fmt.rate),
                      ("%s_ssize" % d, 2)]
            if bint is not None:
                attrs.append(("%s_hs_bint" % d, bint))
        return attrs

    def set_format(self, fmt):
        """Unbind, configure, rebind. Raises OSError if the kernel refuses the format
        (the caller then puts the baseline back)."""
        attrs = self.attributes(fmt)
        if self.gadget_sh:
            self._gadget_sh_set(attrs)
        else:
            self._set_here(attrs)
        log.info("gadget: now %r", fmt)

    def _gadget_sh_set(self, attrs):
        cmd = ["sh", self.gadget_sh, "set", self.uac2.name] + ["%s=%s" % a for a in attrs]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise OSError("gadget.sh set: %s" % e)
        if r.returncode != 0:       # the device is back either way (in what was accepted)
            raise OSError((r.stderr or r.stdout).strip() or "gadget.sh set: exit status %d" % r.returncode)

    def _set_here(self, attrs):
        udc = (self.root / "UDC").read_text().strip() or os.listdir("/sys/class/udc")[0]
        link = self.root / "configs" / "c.1" / self.uac2.name
        self._write(self.root / "UDC", "")
        try:
            if link.is_symlink():
                link.unlink()
            try:
                for name, value in attrs:
                    self._write(self.uac2 / name, value)
            finally:
                os.symlink(self.uac2, link)
        finally:
            time.sleep(0.2)
            self._write(self.root / "UDC", udc)

    def wait_card(self, timeout=5):
        """Wait for the gadget's ALSA card to come back after a rebind."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(Path("/dev/snd").glob("pcmC*D0c")):
                return True
            time.sleep(0.1)
        return False
