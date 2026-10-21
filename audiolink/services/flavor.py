"""Pi side: its flavors, what it presents on USB (`audiolink flavor`; docs/flavors-plan.md).

A flavor is a device description (pi-gadget-kit's TOML, README: Hand-written devices):
built into the image (/etc/usb-gadget/flavors/), or added to the card (flavors/NAME.toml;
the card's come first). The platform plans the chosen one when the Pi starts (the card's
setting `flavor`, else the image's default) and switches live with `gadget.sh use NAME`
(its README: Flavors).

  flavor.list               the flavors; which is up, the default, the card's choice; the
                            switch under way (or the last one) and whether a computer's
                            USB link is up to confirm one
  flavor.add {name, toml}   onto the card, once it plans (the image's planner and options)
  flavor.remove {name}      off the card (not the one that's up)
  flavor.use {name}         present it; kept (the card's setting) once a computer's USB link
                            comes back on it within CONFIRM_WITHIN, else back to the one before
  flavor.try {toml}         the same for a file that isn't kept (developers): on the card as
                            try.toml while it's up, never the card's setting

Only USB confirms: a flavor changes only USB, so the relay or Wi-Fi coming back proves
nothing about it. A switch stops the audio, re-presents the device and reads it again
(services/audio_format.py replace_device, under its lock: a format change and a flavor
change never overlap), RESET_GAP after the last re-presentation (audio_format.reset_wait).
A daemon restarted during a switch goes back to the flavor before it. The reset pins
bring the default back (to_default; no gap: someone is there with a paperclip).

Owners only, over USB (the session's gate) or the network (endpoint "flavor").
"""
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time

from ..paths import platform_env
from ..wire.gadget import running_plan
from . import Service
from .access import OWNER
from .audio_format import reset_wait

log = logging.getLogger("audiolink")

BUILT_IN = "/etc/usb-gadget/flavors"
FLAVORS_ENV = "/etc/usb-gadget/flavors.env"     # FLAVOR_DEFAULT, FLAVOR_OPTIONS (the planner's)
PLANNER = "/usr/lib/usb-gadget/gadget_plan.py"
RUN = "/run/usb-gadget"                         # flavor: the name of the one that's up
SWITCH = "/run/audiolink-flavor-switch"         # the switch under way: {name, back}
CONFIRM_WITHIN = 60             # s for a computer's USB link to come back on a new flavor
REPLY_FIRST = 0.5               # s: let the reply reach the computer before USB goes
TRY = "try"                     # a tried file's name on the card while it's up
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
MAX_SIZE = 64 * 1024


def check_name(name):
    name = str(name or "")
    if not NAME.fullmatch(name):
        raise ValueError("%r: a flavor's name is lowercase letters, digits, - and _ (at most 32)" % name)
    return name


def plan_info(path):
    """(USB ID "vvvv:pppp", product string) a gadget plan writes (None each if it doesn't)."""
    vals = {}
    try:
        with open(path) as f:
            for line in f:
                parts = line.rstrip("\n").split(" ", 2)
                if len(parts) == 3 and parts[0] == "write":
                    vals[parts[1]] = parts[2]
    except OSError:
        pass
    try:
        usb = "%04x:%04x" % (int(vals["idVendor"], 16), int(vals["idProduct"], 16))
    except (KeyError, ValueError):
        usb = None
    return usb, vals.get("strings/0x409/product")


def usb_ids():
    """The USB ID(s) of the device that's up (the Pi's network record)."""
    usb, _ = plan_info(running_plan())
    return [usb] if usb else []


def _toml(text):
    """A TOML's tables, as far as describe() needs them (tomllib on 3.11+, the Pi's)."""
    try:
        import tomllib
        return tomllib.loads(text)
    except ImportError:
        pass
    out, table = {}, {}
    for line in text.splitlines():
        m = re.match(r"\s*\[([A-Za-z0-9_.]+)\]\s*(#.*)?$", line)
        if m:
            table = out.setdefault(m.group(1), {})
            continue
        if line.strip().startswith("[["):
            table = {}
            continue
        m = re.match(r'\s*([A-Za-z0-9_]+)\s*=\s*("(?:[^"\\]|\\.)*"|0[xX][0-9A-Fa-f_]+|[0-9_]+)', line)
        if m:
            v = m.group(2)
            table[m.group(1)] = json.loads(v) if v.startswith('"') else int(v.replace("_", ""), 0)
    return out


def describe(path):
    """{"usb": "vvvv:pppp", "product"} a flavor's file says (None each where it doesn't)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        data = json.loads(text) if path.endswith(".json") else _toml(text)
        if "devices" in data:           # a usb-capture: its first device that isn't a hub
            d = next((d for d in data["devices"] if d.get("device_descriptor", {}).get("bDeviceClass") != 9),
                     data["devices"][0])["model"]
            dev = d["device"]
            product = d.get("strings", {}).get("table", {}).get(str(dev.get("iProduct")))
        else:
            dev, product = data.get("device", {}), data.get("strings", {}).get("product")
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        return {"usb": None, "product": None}
    try:
        usb = "%04x:%04x" % (int(dev["idVendor"]), int(dev["idProduct"]))
    except (KeyError, TypeError, ValueError):
        usb = None
    return {"usb": usb, "product": product}


class FlavorService(Service):
    name = "flavor"
    caps = ("flavor",)

    def __init__(self, formats, card_settings, endpoint=None, card=None, env=None, gadget_sh=None,
                 built_in=BUILT_IN, flavors_env=FLAVORS_ENV, planner=PLANNER, python=None, run_dir=RUN,
                 switch_file=SWITCH, run=subprocess.run, within=CONFIRM_WITHIN, wait=reset_wait):
        env = platform_env() if env is None else env
        self.formats = formats                  # services/audio_format.py AudioFormatService
        self.card_settings = card_settings      # services/card.py CardService: the card's `flavor`
        self.card = card or env.get("CARD") or "/media/mmcblk0p1"
        self.card_dir = os.path.join(self.card, "flavors")
        self.gadget_sh = gadget_sh if gadget_sh is not None else env.get("GADGET_SH")
        self.built_in, self.planner, self.run_dir, self.switch_file = built_in, planner, run_dir, switch_file
        self.python = python or ("/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable)
        self.env = platform_env(flavors_env, {})
        self.run_cmd, self.within, self.wait = run, within, wait
        self.lock = threading.Lock()
        self.switch = None                      # the switch under way, or the last one
        self.ids = 0
        if endpoint:
            endpoint.register("flavor", lambda conn, req: {"result": self.handle(req.get("action"), req)}, OWNER)

    # ---- what there is
    @property
    def default(self):
        return self.env.get("FLAVOR_DEFAULT") or None

    def switchable(self):
        return bool(self.default and self.gadget_sh and os.path.exists(self.planner))

    def running(self):
        try:
            with open(os.path.join(self.run_dir, "flavor")) as f:
                return f.read().strip() or None
        except OSError:
            return self.default if self.default else None

    def chosen(self):
        """The card's setting `flavor` (None: the default)."""
        try:
            got = {s["key"]: s["value"] for s in self.card_settings.settings()["settings"]}
        except (OSError, KeyError, TypeError):
            return None
        return got.get("flavor")

    def find(self, name):
        """NAME -> (its file, "card" or "built in"), as gadget.sh finds it; None if there's none."""
        for d, where in ((self.card_dir, "card"), (self.built_in, "built in")):
            for ext in (".toml", ".json"):
                p = os.path.join(d, name + ext)
                if os.path.isfile(p):
                    return p, where
        return None

    def names(self):
        out = set()
        for d in (self.card_dir, self.built_in):
            try:
                files = os.listdir(d)
            except OSError:
                continue
            for f in files:
                stem, ext = os.path.splitext(f)
                if ext in (".toml", ".json") and NAME.fullmatch(stem) and not (stem == TRY and d == self.card_dir):
                    out.add(stem)
        return sorted(out)

    def listing(self):
        flavors = []
        for name in self.names():
            path, where = self.find(name)
            replaces = where == "card" and os.path.isfile(os.path.join(self.built_in, name + ".toml"))
            flavors.append(dict(describe(path), name=name, where=where, replaces=replaces))
        running = self.running()
        if running == TRY and self.find(TRY):
            flavors.append(dict(describe(self.find(TRY)[0]), name=TRY, where="tried"))
        return {"switchable": self.switchable(), "running": running, "default": self.default,
                "chosen": self.chosen(), "flavors": flavors, "switch": self.switch,
                "usb_peer": bool(getattr(self, "session", None) and self.session.peer_alive),
                "confirm_within": self.within}

    # ---- requests
    def handle(self, op, args):
        if op == "list":
            return self.listing()
        if op == "add":
            return self.add(args.get("name"), args.get("toml"))
        if op == "remove":
            return self.remove(args.get("name"))
        if op == "use":
            return self.use(check_name(args.get("name")))
        if op == "try":
            return self.try_(args.get("toml"))
        return super().handle(op, args)

    def plan(self, toml):
        """Plan a flavor's TOML as the Pi would (gadget.sh): {usb, product, notes}, or
        ValueError with the planner's reason."""
        if not isinstance(toml, str) or not toml.strip():
            raise ValueError("no flavor (a TOML file's text)")
        if len(toml.encode()) > MAX_SIZE:
            raise ValueError("that flavor is too big (at most %d KB)" % (MAX_SIZE // 1024))
        if not os.path.exists(self.planner):
            raise ValueError("this Pi's image has no flavors (no planner): audiolink update --image first")
        with tempfile.TemporaryDirectory() as d:
            src, plan = os.path.join(d, "flavor.toml"), os.path.join(d, "flavor.plan")
            with open(src, "w", encoding="utf-8") as f:
                f.write(toml)
            cmd = [self.python, self.planner, src, "-o", plan, "--functions", os.path.join(d, "functions")]
            cmd += shlex.split(self.env.get("FLAVOR_OPTIONS", ""))
            try:
                p = self.run_cmd(cmd, capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise ValueError("can't plan it: %s" % e)
            if p.returncode:
                why = [line for line in ((p.stderr or "") + (p.stdout or "")).splitlines() if line.strip()]
                raise ValueError("it doesn't plan: %s" % (why[-1].replace(src, "the flavor") if why else
                                                         "the planner's exit status %d" % p.returncode))
            usb, product = plan_info(plan)
        notes = [line.strip()[len("note: "):] for line in (p.stdout or "").splitlines()
                 if line.strip().startswith("note: ")]
        return {"usb": usb, "product": product, "notes": notes}

    def _write(self, name, toml):
        os.makedirs(self.card_dir, exist_ok=True)
        path = os.path.join(self.card_dir, name + ".toml")
        with open(path + ".new", "w", encoding="utf-8") as f:
            f.write(toml)
            f.flush()
            os.fsync(f.fileno())
        os.replace(path + ".new", path)
        if os.path.exists(os.path.join(self.card_dir, name + ".json")):     # the .toml is the one now
            os.remove(os.path.join(self.card_dir, name + ".json"))
        os.sync()
        return path

    def add(self, name, toml):
        name = check_name(name)
        if name == TRY:
            raise ValueError("%s is kept for audiolink flavor try: pick another name" % TRY)
        if self.find(name) and self.find(name)[1] == "built in":
            raise ValueError("%s is built in: give yours another name (--name)" % name)
        info = self.plan(toml)
        with self.lock:
            existed = bool(self.find(name))
            self._write(name, toml)
        log.info("flavor: %s %s (%s)", "replaced" if existed else "added", name, info["usb"])
        return dict(info, name=name, replaced=existed)

    def remove(self, name):
        name = check_name(name)
        with self.lock:
            found = self.find(name)
            if not found or name == TRY:
                raise ValueError("no flavor %s on the Pi (audiolink flavor lists them)" % name)
            if found[1] == "built in":
                raise ValueError("%s is built in: only flavors you added can be removed" % name)
            if name == self.running():
                raise ValueError("the Pi is %s now: switch to another first (audiolink flavor use NAME)" % name)
            if self._busy():
                raise ValueError("a switch to %s is under way: wait for it" % self.switch["name"])
            for ext in (".toml", ".json"):
                p = os.path.join(self.card_dir, name + ext)
                if os.path.exists(p):
                    os.remove(p)
            unchosen = self.chosen() == name
            if unchosen:
                self.card_settings.handle("set", {"changes": {"flavor": None}})
            os.sync()
        log.info("flavor: removed %s", name)
        return {"removed": name, "unchosen": unchosen, "default": self.default}

    def _busy(self):
        return bool(self.switch and self.switch["state"] in ("waiting", "switching"))

    def use(self, name, tried=None):
        if not self.switchable():
            raise ValueError("this Pi's image has no flavors to switch between: audiolink update --image first")
        if not self.find(name):
            raise ValueError("no flavor %s on the Pi (audiolink flavor lists them)" % name)
        with self.lock:
            if self._busy():
                raise ValueError("a switch to %s is under way: wait for it" % self.switch["name"])
            running = self.running()
            if name == running and not tried:
                if self.chosen() != (None if name == self.default else name):
                    self._choose(name)
                return {"name": name, "unchanged": True}
            back = running if running and running != TRY else (self.chosen() or self.default)
            self.ids += 1
            wait = self.wait()
            self.switch = {"id": self.ids, "name": name, "back": back, "state": "switching", "tried": bool(tried),
                           "wait": round(wait), "within": self.within}
            if tried:
                self.switch.update(tried)
            sw = self.switch
        threading.Thread(target=self._run, args=(sw, wait), name="flavor-switch", daemon=True).start()
        return dict(sw)

    def try_(self, toml):
        if not self.switchable():
            raise ValueError("this Pi's image has no flavors to switch between: audiolink update --image first")
        info = self.plan(toml)
        with self.lock:
            if self._busy():
                raise ValueError("a switch to %s is under way: wait for it" % self.switch["name"])
            self._write(TRY, toml)        # trying again: goes back to what was up before the first
        return self.use(TRY, tried=info)

    # ---- the switch, and its confirmation
    def _present(self, name):
        """gadget.sh use NAME, the audio stopped around it -> (exit status, what it said)."""
        def change():
            try:
                p = self.run_cmd(["sh", self.gadget_sh, "use", name], capture_output=True, text=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as e:
                return 1, str(e)
            said = (p.stderr or p.stdout or "").strip().splitlines()
            return p.returncode, said[-1] if said else ""
        with self.formats.lock:
            st, said = self.formats.replace_device(change)
        log.log(logging.INFO if st == 0 else logging.WARNING, "flavor: gadget.sh use %s: %s", name,
                said or "exit status %d" % st)
        return st, said

    def _note(self, sw):
        try:
            if sw is None:
                os.remove(self.switch_file)
            else:
                with open(self.switch_file, "w") as f:
                    json.dump({"name": sw["name"], "back": sw["back"]}, f)
        except OSError:
            pass

    def _run(self, sw, wait, sleep=time.sleep, clock=time.monotonic):
        sleep(max(REPLY_FIRST, wait))
        if self.switch is not sw:
            return
        self._note(sw)
        log.info("flavor: switching to %s (back to %s unless a computer's USB link comes back on it in %ds)",
                 sw["name"], sw["back"], self.within)
        st, said = self._present(sw["name"])
        if st != 0:
            sw.update(state="refused", error=said or "exit status %d" % st, running=self.running())
            self._note(None)
            self._tidy()
            return
        sw["state"] = "waiting"
        since = clock()
        end = since + self.within
        while clock() < end and self.switch is sw:
            if self._link_back(since):
                break
            sleep(0.5)
        if self.switch is not sw:           # the pins: the default's back already
            return
        if self._link_back(since):
            if not sw["tried"]:
                self._choose(sw["name"])
            sw.update(state="kept", running=sw["name"])
            log.info("flavor: %s kept: a computer's USB link came back on it", sw["name"])
        else:
            log.warning("flavor: no computer's USB link on %s in %ds; back to %s", sw["name"], self.within, sw["back"])
            sleep(self.wait())
            if self.switch is sw:
                self._present(sw["back"])
                sw.update(state="back", running=self.running())
        self._note(None)
        self._tidy()

    def _link_back(self, since):
        """A computer's USB link up again, heard since the new device came up."""
        s = getattr(self, "session", None)
        return bool(s and s.peer_alive and s.link.last_rx > since)

    def _choose(self, name):
        """The card's setting: the Pi starts as NAME (the default: no setting)."""
        try:
            self.card_settings.handle("set", {"changes": {"flavor": None if name == self.default else name}})
        except (OSError, ValueError) as e:
            log.warning("flavor: can't keep %s in settings.txt: %s", name, e)

    def _tidy(self):
        """A tried file goes once something else is up."""
        p = os.path.join(self.card_dir, TRY + ".toml")
        if self.running() != TRY and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    def run(self):
        """At start: a switch the daemon was in the middle of goes back."""
        try:
            with open(self.switch_file) as f:
                was = json.load(f)
        except (OSError, ValueError):
            self._tidy()
            return
        self._note(None)
        if was.get("name") == self.running() and was.get("back") and self.find(str(was["back"])):
            log.warning("flavor: restarted during the switch to %s; back to %s", was["name"], was["back"])

            def back():
                time.sleep(self.wait())
                with self.lock:
                    if self._busy():
                        return
                self._present(str(was["back"]))
                self._tidy()
            threading.Thread(target=back, name="flavor-back", daemon=True).start()
        else:
            self._tidy()

    def to_default(self):
        """The reset pins: the default flavor, now and from now on."""
        if not self.switchable():
            return
        with self.lock:
            sw, self.switch = self.switch, None
            if sw and sw["state"] in ("waiting", "switching"):
                sw["state"] = "cancelled"
        self._note(None)
        if self.chosen():
            self._choose(self.default)
        if self.running() != self.default:
            log.warning("flavor: the reset pins: back to %s", self.default)
            self._present(self.default)
        self._tidy()
