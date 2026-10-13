"""Pi side: the reset pins. Short header pins 39 (GND) and 40 (GPIO21), with a jumper
or a paperclip, and hold:

   5 s   back to the built-in code (removes an update from `audiolink update`)
  15 s   also start over: a new identity (unclaimed; codes given out stop working)
         and no saved Wi-Fi networks

The green light blinks slowly once the 5 s mark is reached, quickly past 15 s; the
action happens on release. Shorted when the daemon starts counts as holding from
then. pi/start-audiolink.sh also runs the built-in code while the pins are shorted,
so an update too broken to watch them can still be got rid of (short, then replug).
The pin is an input with the pull-up on (config.txt), so open reads 1, shorted 0.
"""
import glob
import logging
import os
import threading
import time

log = logging.getLogger("audiolink")

PIN = 21
SHORT, LONG = 5, 15
BOUNCE = 1.0                # s: an open shorter than this is a slip of the paperclip, not a release
LED = "/sys/class/leds/ACT"


def gpio_path(pin=PIN, sysfs="/sys/class/gpio"):
    """The value file for a BCM pin number, exporting it if needed (None without GPIO)."""
    for chip in glob.glob(sysfs + "/gpiochip*"):
        try:
            with open(chip + "/label") as f:
                if not f.read().startswith("pinctrl-bcm"):
                    continue
            with open(chip + "/base") as f:
                n = int(f.read()) + pin
        except (OSError, ValueError):
            continue
        path = "%s/gpio%d/value" % (sysfs, n)
        if not os.path.exists(path):
            try:
                with open(sysfs + "/export", "w") as f:
                    f.write(str(n))
            except OSError:
                return None
        return path
    return None


class ResetPins:
    def __init__(self, revert, start_over, read=None, led=None, poll=0.25):
        self.revert, self.start_over = revert, start_over     # () -> None each
        self.read = read or self._read                        # () -> True while shorted
        self.led = led or self._led                           # ("off" | "slow" | "fast") -> None
        self.poll = poll
        self.path = None
        self.trigger = None             # the green light's own trigger, put back afterwards

    def run(self):
        self.path = gpio_path()
        if self.path:
            threading.Thread(target=self._loop, name="reset-pins", daemon=True).start()

    def _read(self):
        try:
            with open(self.path) as f:
                return f.read().strip() == "0"
        except (OSError, TypeError):
            return False

    def _led(self, mode):
        try:
            if self.trigger is None:
                with open(LED + "/trigger") as f:           # "none [mmc0] timer ..."
                    words = f.read().split()
                self.trigger = next((w[1:-1] for w in words if w.startswith("[")), "default-on")
            if mode == "off":
                with open(LED + "/trigger", "w") as f:
                    f.write(self.trigger)
                return
            with open(LED + "/trigger", "w") as f:
                f.write("timer")
            on = 500 if mode == "slow" else 80
            for name in ("delay_on", "delay_off"):
                with open("%s/%s" % (LED, name), "w") as f:
                    f.write(str(on))
        except OSError:
            pass

    def _loop(self):
        while True:
            self.step()
            time.sleep(self.poll)

    held_since = None
    shown = "off"

    open_since = None

    def step(self, now=None):
        """One look at the pins (tests call this directly). A hand-held paperclip
        loses contact now and then: opens shorter than BOUNCE don't end the hold."""
        now = time.monotonic() if now is None else now
        if self.read():
            self.open_since = None
            if self.held_since is None:
                self.held_since = now
            held = now - self.held_since
            mode = "fast" if held >= LONG else "slow" if held >= SHORT else "off"
            if mode != self.shown:
                self.shown = mode
                self.led(mode)
            return
        if self.held_since is None:
            return
        if self.open_since is None:
            self.open_since = now
        if now - self.open_since < BOUNCE:
            return
        held, self.held_since, self.open_since = self.open_since - self.held_since, None, None
        if self.shown != "off":
            self.shown = "off"
            self.led("off")
        if held >= LONG:
            log.warning("reset pins held %ds: starting over", held)
            self.start_over()
        elif held >= SHORT:
            log.warning("reset pins held %ds: back to the built-in code", held)
            self.revert()
