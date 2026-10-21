"""Pi: re-present the USB audio gadget in another audio format.

The audio function is the device description's (pi-gadget-kit): uac2 for
audiolink's own device, or uac1 for a device cloned from real headphones. Its home
format (where every link starts, and what the Pi goes back to) is the one the
gadget plan sets, each direction its own (a clone: stereo out, mono in), and going
home writes the plan's values back exactly, so a clone is the device it copies
again. Other formats are the same both ways, and only those the USB speed carries.

The format lives in the function's configfs attributes, which can only change while
the function is in no configuration, so a switch is: unbind (the host sees the device
unplugged), unlink, write the attributes, link back, bind again (the host enumerates
the new format). On pi-gadget-kit its gadget.sh does that (`gadget.sh set audio
c_srate=...`: it relinks every function in order and brings usb0 up after the bind);
the device starts in the BASELINE format (config/device.toml). Without the platform
(the older, pre-platform image's gadget) it's done here.
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

LEGACY_GADGET = Path("/sys/kernel/config/usb_gadget/audiopi")      # the older image's
FUNCTION = "uac2.audio"         # config/device.toml's uac2 function, id "audio"
MAX_PACKET = 1024               # high-speed isochronous, one transaction per microframe
FS_PACKET = 1023                # full-speed isochronous: one packet per 1 ms frame
PLAN = "/etc/usb-gadget/device.plan"
# A direction's format in the function's attributes; going home also restores these
HOME_ATTRS = ("chmask", "srate", "ssize", "max_packet_size", "hs_bint")


class Gadget:
    def __init__(self, root=None, gadget_sh=None, env=None, plan=PLAN):
        env = platform_env() if env is None else env
        if root is None:
            root = env.get("GADGET") or LEGACY_GADGET
        self.root = Path(root)
        self.gadget_sh = gadget_sh if gadget_sh is not None else env.get("GADGET_SH")
        self.uac2 = self.root / "functions" / self._function(env.get("GADGET_FUNCTIONS"))
        self.kind = self.uac2.name.split(".")[0]           # uac2, or uac1 (a clone)
        self.plan_attrs = self._plan_attrs(plan)
        # The device leaves bInterval at the kernel default; formats too fast for it use 1.
        self.default_bint = {d: self._read("%s_hs_bint" % d) for d in "cp"}

    def _function(self, functions=None):
        """The audio function: the device description's (its list of functions), else
        config/device.toml's id, else any uac2 (the older image's uac2.usb0)."""
        try:
            with open(functions) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) == 3 and parts[1] in ("uac2", "uac1"):
                        return os.path.basename(parts[2])
        except (OSError, TypeError):
            pass
        if (self.root / "functions" / FUNCTION).is_dir():
            return FUNCTION
        found = sorted(glob.glob(str(self.root / "functions" / "uac2.*")))
        return os.path.basename(found[0]) if found else FUNCTION

    def _plan_attrs(self, plan):
        """{attribute: value} the plan writes for the function's formats (both directions)."""
        out = {}
        try:
            with open(plan) as f:
                for line in f:
                    parts = line.rstrip("\n").split(" ", 2)
                    if len(parts) == 3 and parts[0] in ("write", "write?") \
                            and parts[1].startswith("functions/%s/" % self.uac2.name):
                        name = parts[1].rsplit("/", 1)[1]
                        if name[:2] in ("c_", "p_") and name[2:] in HOME_ATTRS:
                            out[name] = parts[2]
        except OSError:
            pass
        return out

    def home(self):
        """(what the Pi sends: p_*, what it receives: c_*) at home: the plan's formats."""
        def fmt(d):
            try:
                return AudioFormat(int(self.plan_attrs["%s_srate" % d].split(",")[0]),
                                   bin(int(self.plan_attrs["%s_chmask" % d])).count("1"))
            except (KeyError, ValueError):
                return BASELINE
        return fmt("p"), fmt("c")

    def full_speed(self):
        return self._read_root("max_speed") == "full-speed" or self.kind == "uac1"

    def _read_root(self, name):
        try:
            return (self.root / name).read_text().strip()
        except OSError:
            return None

    def fits(self, fmt):
        """Does the device's USB speed carry fmt (each way)? Full speed: one packet of at
        most 1023 bytes per millisecond, with room for one more sample."""
        if self.full_speed():
            return fmt.bytes_per_sec / 1000 + fmt.frame_bytes <= FS_PACKET
        return True

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
        """The format the gadget is presenting, what it sends (p_*) (None if unknown)."""
        return self._current("p")

    def current_both(self):
        """(sends: p_*, receives: c_*) as presented now (None each, if unknown)."""
        return self._current("p"), self._current("c")

    def _current(self, d):
        try:
            return AudioFormat(int(self._read("%s_srate" % d).split(",")[0]),
                               bin(int(self._read("%s_chmask" % d))).count("1"))
        except (AttributeError, ValueError):
            return None

    def at_home(self):
        return self.current_both() == self.home()

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
        if fmt == "home":
            return [(name, self.plan_attrs[name]) for d in "cp" for name in
                    ("%s_%s" % (d, a) for a in HOME_ATTRS) if name in self.plan_attrs] \
                or self.attributes(BASELINE)
        bint = self.bint_for(fmt) if self.kind == "uac2" else None
        attrs = []
        for d in "cp":
            attrs += [("%s_chmask" % d, (1 << fmt.channels) - 1), ("%s_srate" % d, fmt.rate),
                      ("%s_ssize" % d, 2)]
            if self.kind == "uac1":             # its packets are as big as the plan says: fit them
                per_ms = -(-fmt.rate // 1000)
                attrs.append(("%s_max_packet_size" % d, min(per_ms * fmt.frame_bytes, FS_PACKET)))
            if bint is not None:
                attrs.append(("%s_hs_bint" % d, bint))
        return attrs

    def set_format(self, fmt):
        """Unbind, configure, rebind; fmt "home": the plan's formats again. Raises OSError
        if the kernel refuses the format (the caller then goes home)."""
        attrs = self.attributes(fmt)
        if self.gadget_sh:
            self._gadget_sh_set(attrs)
        else:
            self._set_here(attrs)
        log.info("gadget: now %s", "home %r/%r" % self.home() if fmt == "home" else repr(fmt))

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
