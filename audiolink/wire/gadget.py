"""Pi: re-present the UAC2 gadget in another audio format.

The format lives in the function's configfs attributes, which can only change while
the function is in no configuration, so a switch is: unbind (the host sees the device
unplugged), unlink the function from the configuration, write the attributes, link
it back, bind again (the host enumerates the new format). pi/init
creates the gadget in the BASELINE format.
"""
import logging
import os
import time
from pathlib import Path

from .fmt import BASELINE, AudioFormat

log = logging.getLogger("audiolink")

GADGET = Path("/sys/kernel/config/usb_gadget/audiopi")
UAC2 = GADGET / "functions" / "uac2.usb0"
MAX_PACKET = 1024               # high-speed isochronous, one transaction per microframe


class Gadget:
    def __init__(self, root=GADGET):
        self.root = root
        self.uac2 = root / "functions" / "uac2.usb0"
        # pi/init leaves bInterval at the kernel default; formats too fast for it use 1.
        self.default_bint = {d: self._read("%s_hs_bint" % d) for d in "cp"}

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

    def set_format(self, fmt):
        """Unbind, configure, rebind. Raises OSError if the kernel refuses the format
        (the caller then puts the baseline back)."""
        udc = (self.root / "UDC").read_text().strip() or os.listdir("/sys/class/udc")[0]
        link = self.root / "configs" / "c.1" / self.uac2.name
        self._write(self.root / "UDC", "")
        try:
            if link.is_symlink():
                link.unlink()
            try:
                bint = self.bint_for(fmt)
                for d in "cp":
                    self._write(self.uac2 / ("%s_chmask" % d), (1 << fmt.channels) - 1)
                    self._write(self.uac2 / ("%s_srate" % d), fmt.rate)
                    self._write(self.uac2 / ("%s_ssize" % d), 2)
                    if bint is not None:
                        self._write(self.uac2 / ("%s_hs_bint" % d), bint)
            finally:
                os.symlink(self.uac2, link)
        finally:
            time.sleep(0.2)
            self._write(self.root / "UDC", udc)
        log.info("gadget: now %r", fmt)

    def wait_card(self, timeout=5):
        """Wait for the gadget's ALSA card to come back after a rebind."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(Path("/dev/snd").glob("pcmC*D0c")):
                return True
            time.sleep(0.1)
        return False
