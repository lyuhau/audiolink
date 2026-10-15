"""`audiolink setup`: set up this computer, and the Pi if it's plugged in here.

  1. audiolink itself, somewhere permanent (install.py)
  With the Pi on USB (through `audiolink host`; setup offers to start one for itself):
  2. the Pi: added here (USB is the trust: no code, no question), a name, Wi-Fi
  3. "Will it stay plugged into this computer?" yes: this computer is its host: what it
     exposes (ssh), saved settings (host/config.py), started at login (host/autostart.py)
  4. ssh NAME from this computer: a block in ~/.ssh/config (client/sshconfig.py)
  Without it: find it on this network (or take its connect code), then 4, and try ssh.
`audiolink setup host`: just this computer's host part (3).

Running it again shows what's set and lets you change answers; what's already done is
skipped. Every question has a flag (--yes takes the suggested answers). The audiolink
commands it stands for are shown as "  (audiolink ...)" lines; system commands with -v.
"""
import getpass
import os
import socket
import sys
import time
from types import SimpleNamespace

from . import install
from .client import keyring, pis, sshconfig
from .host import autostart as autostart_mod, config as host_config, control
from .system import Runner
from .ui import Prompter, detail, done, line_buffered, step

SSH_PI_PORT = 2222
MAC_SHARING = "x-apple.systempreferences:com.apple.Sharing-Settings.extension"
FLAGS = "--yes, or the setup flags (audiolink setup --help)"


class Ask(Prompter):
    """Questions with a suggested answer, which --yes takes without asking."""

    def __init__(self, a):
        super().__init__(SimpleNamespace(), FLAGS)       # not a: --password-stdin is Wi-Fi's
        self.auto = bool(getattr(a, "yes", False))

    def yes(self, question, default=False):
        if self.auto:
            print("%s %s" % (question, "yes" if default else "no"))
            return default
        return super().yes(question, default)

    def text_default(self, question, default):
        if self.auto:
            return default
        return self.text("%s [%s]: " % (question, default)).strip() or default


def tilde(path):
    """A path as people write it: ~/... under the home folder."""
    home, path = os.path.expanduser("~"), str(path)
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def ran(cmd):
    """The audiolink command a step stands for."""
    print("  (%s)" % cmd)


def _ssh_target(spec):
    """'2222:22' -> (2222, ('127.0.0.1', 22))."""
    from .services.streams import parse_forward
    return parse_forward(spec)


def ssh_exposed(exposes):
    """The Pi port that reaches this computer's ssh (port 22), or None."""
    for spec in exposes:
        pi_port, (host, port) = _ssh_target(spec)
        if port == 22 and host in ("127.0.0.1", "localhost"):
            return pi_port
    return None


def sshd_running(port=22):
    """Does this computer's ssh server answer on 127.0.0.1?"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
            s.settimeout(3)
            return s.recv(64).startswith(b"SSH-")
    except OSError:
        return False


def pi_device_present():
    """Does this computer see the Pi's sound device (with no host running)?"""
    try:
        from .wire import portaudio
        from .wire.host_audio import DEVICE_HINTS
        return any(any(h in d["name"].lower() for h in DEVICE_HINTS) for d in portaudio.devices())
    except Exception as e:              # no PortAudio here, ...
        detail("sound devices: %s", e)
        return False


def ssh_banner(alias, port):
    """Through the Pi to the ssh server behind it -> (True, banner) or (False, why)."""
    from .client.find import _connect
    try:
        sock = _connect(SimpleNamespace(pi=alias, port=port))
    except SystemExit as e:
        return False, str(e.code)
    try:
        sock.settimeout(15)
        data = b""
        while b"\n" not in data and len(data) < 256:
            chunk = sock.recv(256)
            if not chunk:
                break
            data += chunk
    except OSError as e:
        return False, "no answer (%s)" % e
    finally:
        sock.close()
    if data.startswith(b"SSH-"):
        return True, data.split(b"\r")[0].split(b"\n")[0].decode(errors="replace")
    return False, "no ssh server answered" if not data else "something other than ssh answered"


class Guide:
    def __init__(self, a, runner=None, platform=sys.platform, home=None, sleep=None,
                 pi_device=pi_device_present, sshd=sshd_running, banner=ssh_banner, installer=None):
        self.a = a
        self.ask = Ask(a)
        self.runner = runner or Runner()
        self.platform, self.home = platform, home
        self.port = getattr(a, "control_port", None) or control.DEFAULT_PORT
        self.sleep = sleep or time.sleep
        self.pi_device, self.sshd, self.banner = pi_device, sshd, banner
        self.installer = installer or (lambda: install.install(self.runner, platform, home))
        self.temp_host = None           # an `audiolink host` started for this setup
        self.installed = None
        self.name = "the Pi"
        self.entry = None
        self.is_host = False
        self.ssh_port = SSH_PI_PORT

    def call(self, op, args=None, timeout=120):
        return control.call(op, args, port=self.port, timeout=timeout)

    # ------------------------------------------------------------ the parts
    def run(self):
        self.install_step()
        if self.a.part == "host":
            if self.peer(connected=True):
                self.name = self.pi_name()
            return self.host_part()
        if self.usb():
            self.pi_part()
            stays = self.is_host = self.stays()
            if stays:
                self.host_part()
            if stays and not ssh_exposed(host_config.load(strict=False).get("expose", [])):
                return                  # nothing to ssh to from here
            if self.client_block() and stays:
                self.try_ssh()
            if not stays:
                print("Next: plug %s into its home computer and run audiolink setup there" % self.name)
            return
        self.client_part()

    def install_step(self):
        if self.a.no_install:
            self.installed = install.here()
            return
        got = self.installer()
        self.installed = got
        if got.changed:
            print("Installed audiolink in %s" % tilde(got.where))
        else:
            detail("audiolink is installed in %s", got.where)
        for note in got.notes:
            print(note)

    # ---- is the Pi on USB?
    def peer(self, connected=False):
        """The running host's peer state, or None if no host runs here (connected: and
        only if the Pi is on it)."""
        try:
            p = self.call("peer", timeout=10)
        except SystemExit:
            return None
        return p if p.get("connected") or not connected else None

    def pi_name(self):
        p = self.peer() or {}
        return ((p.get("peer") or {}).get("identity") or {}).get("name") or "the Pi"

    def usb(self):
        """Is the Pi plugged in here (with a host running for it, started now if needed)?"""
        p = self.peer()
        if p is None:
            if not self.pi_device():
                return False
            print("The Pi is plugged in here, and audiolink host isn't running")
            if not self.ask.yes("Start it for this setup?", default=True):
                return False
            self.start_temp_host()
        elif not p.get("connected"):
            if not self.pi_device():
                print("audiolink host runs here, with no Pi plugged in: setting up this computer to use one")
                return False
        if not self.wait_for_pi(90 if self.temp_host else 60):
            raise SystemExit("The Pi didn't connect (see: audiolink host's window, or the troubleshooting in "
                             "the README); then run audiolink setup again")
        caps = ((self.peer() or {}).get("peer") or {}).get("caps", [])
        if "identity" not in caps:
            raise SystemExit("%s's audiolink is too old for this; update it: audiolink update" % self.name)
        return True

    def wait_for_pi(self, seconds):
        p = self.peer(connected=True)
        if p is None:
            step("Waiting for the Pi")
            end = time.monotonic() + seconds
            while p is None and time.monotonic() < end:
                self.sleep(1)
                p = self.peer(connected=True)
            done("connected" if p else "not connected")
            if p is None:
                return False
        self.name = self.pi_name()
        try:                            # the first minute: the host tries faster audio formats
            if self.call("audio.status", timeout=10).get("running"):
                step("Measuring the link")
                end = time.monotonic() + 120
                while self.call("audio.status", timeout=10).get("running") and time.monotonic() < end:
                    self.sleep(1)
                done("done")
        except SystemExit:
            pass
        return True

    def start_temp_host(self):
        from .paths import cache_dir
        log = cache_dir() / "setup-host.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        cmd = self.installed.argv + ["host"]
        if self.a.control_port:
            cmd += ["--control-port", str(self.a.control_port)]
        env_note = self.installed.env
        if env_note:
            os.environ.update(env_note)                 # the child inherits it
        self.temp_host = self.runner.spawn(cmd, log)
        ran("audiolink host, until setup is done")
        if self.platform == "darwin":
            print("If macOS asks whether your terminal may use the microphone, or to allow an accessory: allow it")
        end = time.monotonic() + 15
        while self.peer() is None and time.monotonic() < end:
            if self.temp_host.poll() is not None:
                raise SystemExit("audiolink host stopped (its output: %s)" % log)
            self.sleep(0.5)

    def stop_temp_host(self):
        if self.temp_host is None:
            return
        p, self.temp_host = self.temp_host, None
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()
        end = time.monotonic() + 10
        while self.peer() is not None and time.monotonic() < end:      # the control port is free again
            self.sleep(0.5)

    # ---- the Pi
    def pi_part(self):
        got = pis.add_usb_entry(self.port)
        if got is None:
            raise SystemExit("Lost the Pi; plug it in again and rerun audiolink setup")
        entry, created, first, custody = got
        self.entry = entry
        name = entry["alias"]
        if created:
            print("Added %s%s" % (name, " (claimed: this is its first computer)" if first else ""))
            ran("audiolink add")
        else:
            print("%s: added here" % name)
        if custody:
            print("No SD card: keeping a backup of its identity here")
        wanted = self.a.name
        if not wanted and created:
            wanted = self.ask.text_default("Its name", name).strip().lower()
        if wanted and wanted != name:
            pis.rename_command(SimpleNamespace(name=name, new=wanted, on_pi=True, control_port=self.port))
            ran("audiolink rename %s %s --on-pi" % (name, wanted))
            self.entry = keyring.lookup(wanted, known=True)
        self.name = self.entry["alias"]
        self.wifi_part()

    def wifi_part(self):
        from .host import wifi_cli
        peer = self.peer(connected=True) or {}
        wifi_cli._set_pi(peer.get("peer"))
        if "wifi" not in (peer.get("peer") or {}).get("caps", []):
            print("%s's audiolink can't manage Wi-Fi; update it: audiolink update" % self.name)
            return
        wa = SimpleNamespace(security=None, open=False, hidden=False, reenter=False, portal=False,
                             no_portal=False, password_stdin=self.a.password_stdin)
        st = self.call("wifi.status")
        if self.a.wifi:
            if st.get("ssid") == self.a.wifi:
                print("%s: on Wi-Fi %s" % (self.name, self.a.wifi))
                return
            wifi_cli.join_flow(self.a.wifi, wa, self.port)
            ran('audiolink wifi join "%s"' % self.a.wifi)
            return
        if st.get("ssid"):
            print("%s: on Wi-Fi %s%s" % (self.name, st["ssid"], {
                "offline": " (no internet)", "portal": " (sign-in page: audiolink wifi portal)"}
                .get(st.get("internet"), "")))
            return
        if self.a.no_wifi:
            print("%s: not on Wi-Fi (later: audiolink wifi)" % self.name)
            return
        if self.ask.auto and not self.ask.tty:
            print("%s: not on Wi-Fi (--wifi NAME, or later: audiolink wifi)" % self.name)
            return
        if not self.ask.yes("Put %s on Wi-Fi now?" % self.name, default=True):
            print("Later: audiolink wifi")
            return
        wifi_cli.pick_flow(wa, self.port)
        ran("audiolink wifi")

    def stays(self):
        if self.a.host is not None:
            return self.a.host
        return self.ask.yes("Will %s stay plugged into this computer?" % self.name, default=True)

    # ---- this computer as its host
    def host_part(self):
        saved = host_config.load()
        exposes = self.choose_exposes(saved.get("expose", []))
        changed = exposes != saved.get("expose", [])
        uses = (self.peer() or {}).get("expose") if self.temp_host is None else None
        restart = changed if uses is None else sorted(uses) != sorted(exposes)   # the running host's
        if changed:
            path = host_config.update(expose=exposes)
            print("Saved for audiolink host: %s" % (" ".join("--expose " + e for e in exposes) or "nothing exposed"))
            detail("in %s", path)
        else:
            print("audiolink host: %s" % (", ".join("exposes " + e for e in exposes) or "exposes nothing"))
        self.autostart_part(restart)

    def choose_exposes(self, current):
        if self.a.expose:
            exposes = list(dict.fromkeys(self.a.expose))
        elif self.a.no_expose:
            exposes = []
        else:
            had = ssh_exposed(current)
            others = [e for e in current if _ssh_target(e)[0] != had] if had else list(current)
            want = self.ask.yes("Reach this computer's ssh from elsewhere, through %s?" % self.name,
                                default=bool(had) or not current)
            exposes = others + (["%d:22" % (had or SSH_PI_PORT)] if want else [])
        if ssh_exposed(exposes):
            self.check_sshd()
        return exposes

    def check_sshd(self):
        if self.sshd():
            detail("this computer's ssh server answers")
            return
        print("This computer's ssh server is off")
        if self.platform == "darwin":
            print("Turn on Remote Login: System Settings → General → Sharing (opening it); it needs an admin")
            self.runner.run(["open", MAC_SHARING])
        elif self.platform == "win32":
            print("Turn on OpenSSH Server: Settings → System → Optional features, then start the sshd "
                  "service (as an admin)")
        else:
            print("Start it: e.g. sudo apt install openssh-server && sudo systemctl enable --now ssh")
        if self.ask.tty and not self.ask.auto:
            self.ask.text("Press Enter when it's on (or to go on without it) ")
            print("ssh server: %s" % ("on" if self.sshd() else "still off (ssh to this computer won't work "
                                                                "until it's on)"))

    def autostart_part(self, settings_changed):
        svc = autostart_mod.Autostart(self.installed.argv, self.installed.env, self.runner, self.platform, self.home)
        if self.a.no_autostart:
            want = False
        else:
            want = self.ask.yes("Start audiolink host at login, and keep it running?", default=True)
        running = self.peer() is not None and self.temp_host is None
        if not want:
            if svc.installed():
                why = svc.remove()
                print("audiolink host: no longer starts at login" if not why else "Couldn't remove it: %s" % why)
            if not running:
                print("Start it when you need the Pi: audiolink host")
            elif settings_changed:
                print("Restart audiolink host for the new settings")
            return
        if svc.current() and running and not settings_changed:
            print("audiolink host: starts at login, running")
            return
        if running and not svc.installed():
            self.wait_for_other_host()
        self.stop_temp_host()
        if self.platform == "darwin":
            print(autostart_mod.MAC_MIC)
        why = svc.restart() if svc.current() else svc.install()
        if why:
            print("Couldn't set it to start at login: %s" % why)
            return
        print("audiolink host: starts at login (%s)" % tilde(svc.shown()))
        detail("its output: %s", svc.where_output())
        step("Starting it")
        end = time.monotonic() + 60
        p = None
        while p is None and time.monotonic() < end:
            self.sleep(1)
            p = self.peer(connected=True)
        done("connected to %s" % self.pi_name() if p else "not connected yet")
        if p is None:
            print("If it doesn't connect: its output is in %s%s" % (
                svc.where_output(), "; and see System Settings → Privacy & Security → Microphone"
                if self.platform == "darwin" else ""))

    def wait_for_other_host(self):
        """A host started by hand (in a terminal) holds the Pi: it has to stop first."""
        print("audiolink host is running here: stop it (Ctrl-C in its window), so the one started at login "
              "takes over")
        self.ask.need_tty("stop audiolink host")
        step("Waiting for it to stop")
        while self.peer() is not None:
            self.sleep(1)
        done("stopped")

    # ---- this computer as a client
    def client_part(self):
        entry = self.find_pi()
        self.entry, self.name = entry, entry["alias"]
        if self.client_block():
            self.try_ssh()

    def find_pi(self):
        a = self.a
        add = SimpleNamespace(code=a.code, name=a.name, no_lookup=False, at=a.at, pairing_code=a.pairing_code)
        if a.code:
            entry = pis._add_code(add)
            ran("audiolink add CODE")
            return entry
        if a.pi:
            return keyring.lookup(a.pi, known=True)
        from .client.pairing import add_over_network
        print("Looking for a Pi on this network...", flush=True)
        entry = add_over_network(add, self.ask)
        if entry:
            ran("audiolink add")
            return entry
        known = sorted(keyring.load().values(), key=lambda e: e["alias"])
        if len(known) == 1:
            print("No new Pi here; setting up %s (added before)" % known[0]["alias"])
            return known[0]
        if known:
            return self.ask.choose("Set up which Pi?", [(e["alias"], e) for e in known])
        print("No Pi found on this network")
        code = self.ask.text("Its connect code (al1-..., from audiolink share NAME on a computer that has it): ")
        add.code = code.strip()
        if not add.code:
            raise SystemExit("Not set up. Join the Pi's network (or its setup network, audiopi-...) and run "
                             "audiolink setup again")
        entry = pis._add_code(add)
        ran("audiolink add CODE")
        return entry

    def client_block(self):
        """The ~/.ssh/config block for ssh NAME -> whether it's there."""
        if self.a.no_ssh:
            return False
        entry = keyring.lookup(self.name, known=True)
        if self.a.user is None and not self.ask.yes("Set up ssh %s on this computer?" % self.name, default=True):
            return False
        this_is_host = self.is_host
        user = self.a.user
        if user is None:
            here = getpass.getuser()
            if this_is_host:
                user = here
            else:
                old = sshconfig.current(sshconfig.default_path(self.home), keyring.pub_of(entry["code"])[:8])
                if old:
                    here = next((ln.split()[1] for ln in old.splitlines() if ln.strip().startswith("User ")), here)
                user = self.ask.text_default("Your login on the computer %s is plugged into" % self.name, here)
        port = ssh_exposed(host_config.load(strict=False).get("expose", [])) if this_is_host else None
        port = port or pis.ssh_port(entry, SSH_PI_PORT)
        path = sshconfig.default_path(self.home)
        tag = keyring.pub_of(entry["code"])[:8]
        text = sshconfig.block(tag, self.name, port, user,
                               pis._launcher(self.installed.argv, self.installed.env))
        result = sshconfig.write(path, tag, text)
        print("ssh %s: %s (%s)" % (self.name, {"added": "set up", "updated": "updated",
                                               "unchanged": "set up"}[result], tilde(path)))
        for line in sshconfig.others(path, tag, self.name):
            print("%s line %d has another Host %s, which ssh may use instead: remove it" % (path, line, self.name))
        self.ssh_port = port
        return True

    def try_ssh(self):
        step("Trying ssh %s" % self.name)
        ok, what = self.banner(self.name, self.ssh_port)
        done("works" if ok else what)
        detail("%s", what)
        if ok:
            print("Done: ssh %s" % self.name)
        elif self.is_host:
            print("Set up; ssh %s works once %s is online (audiolink wifi status) and this computer's ssh "
                  "server is on" % (self.name, self.name))
        else:
            print("Set up; ssh %s works once %s is online and its computer exposes ssh (audiolink setup "
                  "there)" % (self.name, self.name))


def setup_command(a):
    line_buffered()
    g = Guide(a)
    try:
        return g.run()
    finally:
        g.stop_temp_host()
