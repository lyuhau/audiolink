"""`audiolink setup`: set up this computer, and the Pi if it's plugged in here.

  1. audiolink itself, somewhere permanent (install.py)
  With the Pi on USB (through `audiolink host`, started for the setup if none runs):
  2. the Pi: added here (USB is the trust: no code, no question), its name, Wi-Fi
  3. "Make this computer its host?" yes: it exposes this computer's ssh (saved settings,
     host/config.py), and starts at login if asked to (--autostart, host/autostart.py); a check that ssh
     through the Pi reaches this computer
  4. "Register this computer as a client?" ssh NAME from here (no code: USB is the trust).
     Either, both or neither: setting the Pi up here and plugging it in at its host later
     is no host, yes client.
  Without it: this computer is a client: find the Pi on this network (or take its connect
  code), then 4 without asking.
`audiolink setup host`: just this computer's host part (3).

The only questions are the Pi's name, its Wi-Fi, and whether this computer is its host
and its client;
the rest has flags (--expose, --no-autostart, --user). Running it again shows what's
set and skips what's done. The audiolink commands it stands for are shown as
"  (audiolink ...)" lines; system commands with -v.
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

from . import card, install
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


def copy_id(alias, home=None):
    """How to send this computer's ssh key to ALIAS's host over a password login. Windows'
    OpenSSH has no ssh-copy-id: pipe the .pub file (assumes a Mac or Linux host)."""
    if sys.platform != "win32":
        return "ssh-copy-id %s" % alias
    folder = os.path.join(home or os.path.expanduser("~"), ".ssh")
    pubs = [os.path.join(folder, n + ".pub") for n in ("id_ed25519", "id_ecdsa", "id_rsa")]
    pub = next((p for p in pubs if os.path.exists(p)), None)
    send = 'type "%s" | ssh %s "mkdir -p .ssh && cat >> .ssh/authorized_keys"' % (pub or pubs[0], alias)
    return send if pub else "ssh-keygen -t ed25519, then " + send


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
        from .wire import portaudio, usb_audio
        return bool(usb_audio.pi_devices(usb_audio.annotate(portaudio.devices())))
    except Exception as e:              # no PortAudio here, ...
        detail("sound devices: %s", e)
        return False


def ssh_banner(alias, port):
    """Through the Pi to the ssh server behind it -> (True, banner) or (False, why)."""
    from .client.find import _connect
    try:
        conn = _connect(SimpleNamespace(pi=alias, port=port))      # a PiConn that is the stream now
    except SystemExit as e:
        return False, str(e.code)
    sock = conn.sock
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
        conn.close()
    if data.startswith(b"SSH-"):
        return True, data.split(b"\r")[0].split(b"\n")[0].decode(errors="replace")
    return False, "no ssh server answered" if not data else "something other than ssh answered"


def _refresh_record(entry):
    """Connect to a known Pi once: auth brings its current record. -> the record, or None."""
    from .client.remote import Remote
    r = Remote(entry, []).open(quiet=True)
    try:
        return r.conn.record
    finally:
        r.close()


def in_wsl():
    """Linux under Windows (WSL): mDNS answers don't reach it, NAT or mirrored networking."""
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def ssh_login(alias, port, config=None):
    """ssh ALIAS as the user will run it -> (True, what) or (False, why). Reaching the
    login is enough: a refused password-less try still means ssh read the config, ran the
    ProxyCommand and got through to the ssh server. config: the file to read instead of
    ssh's own (-F). No ssh here: the banner check."""
    ssh = shutil.which("ssh")
    if not ssh:
        return ssh_banner(alias, port)
    try:
        r = subprocess.run([ssh] + (["-F", str(config)] if config else []) + [
                            "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=" + os.devnull,
                            "-o", "LogLevel=ERROR", alias, "exit"],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "no answer"
    except OSError as e:
        return False, "ssh didn't start (%s)" % e
    lines = [ln.strip() for ln in r.stderr.splitlines() if ln.strip()]
    if r.returncode == 0:
        return True, "logged in"
    if any("Permission denied" in ln for ln in lines):
        return True, "reached the login"
    return False, lines[-1] if lines else "ssh exited with %d" % r.returncode


class Guide:
    def __init__(self, a, runner=None, platform=sys.platform, home=None, sleep=None,
                 pi_device=pi_device_present, sshd=sshd_running, banner=ssh_banner, installer=None,
                 usb_state=None, login=ssh_login, lookup=None):
        self.a = a
        self.usb_state = usb_state or (lambda: card.usb_state(self.runner, self.platform))
        self.ask = Ask(a)
        self.runner = runner or Runner()
        self.platform, self.home = platform, home
        self.port = getattr(a, "control_port", None) or control.DEFAULT_PORT
        self.sleep = sleep or time.sleep
        self.pi_device, self.sshd, self.banner, self.login = pi_device, sshd, banner, login
        self.lookup = lookup or _refresh_record
        self.installer = installer or (lambda: install.install(self.runner, platform, home))
        self.temp_host = None           # an `audiolink host` started for this setup
        self.installed = None
        self.name = "the Pi"
        self.entry = None
        self.is_host = False
        self.ssh_port = SSH_PI_PORT
        self.end_note = None            # what to do once setup is done

    def call(self, op, args=None, timeout=120):
        return control.call(op, args, port=self.port, timeout=timeout)

    # ------------------------------------------------------------ the parts
    def run(self):
        self.install_step()
        if self.a.part == "host":
            if self.peer(connected=True):
                self.name = self.pi_name()
            self.host_part()
            if self.end_note:
                print(self.end_note)
            return
        if self.usb():
            self.pi_part()
            host, client = self.host_wanted(), self.client_wanted()
            if host:
                self.is_host = True
                self.host_part()
            if client:
                self.client_block()
            if host and self.ssh_port:
                self.try_ssh(client)
            elif client:
                print("ssh %s works once %s is plugged into a host set up with audiolink setup"
                      % (self.name, self.name))
            if self.end_note:
                print(self.end_note)
            if host:
                print("Next, on your other computers: audiolink setup")
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
        elif got.where:                 # None: run where it is (a source checkout: its note says)
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
        if p is None or not p.get("connected"):
            state = self.usb_state()
            if state in ("rom", "msd"):
                self.card_part(state)
                p = self.peer()
        if p is None:
            if not self.pi_device():
                return False
            held = self.other_host()
            if held:
                raise SystemExit("audiolink host is already running for the Pi (%s): run audiolink setup "
                                 "--control-port %s, or stop that host first" % (
                                     "control port %d" % held if held is not True else "another control port",
                                     held if held is not True else "PORT"))
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
            raise SystemExit("%s runs an audiolink too old for setup; update: audiolink update" % self.name)
        return True

    def other_host(self):
        """An audiolink host already running here, not on our control port (none answers
        there): its port, True if it can't be told (saved in another config), or None.
        Two hosts on the Pi's sound device corrupt the link; holding the control port is
        what normally keeps a second one off, and another port gets past it."""
        if self.port != control.DEFAULT_PORT:
            try:
                control.call("peer", port=control.DEFAULT_PORT, timeout=5)
                return control.DEFAULT_PORT
            except SystemExit:
                pass
        if self.platform == "win32":
            return None
        _, out = self.runner.run(["ps", "-axo", "args="])
        for line in out.splitlines():
            if re.search(r"(?:^|[ /])audiolink(?:\.pyz)?\s+host(?:\s|$)", line):
                m = re.search(r"--control-port[ =](\d+)", line)
                return int(m.group(1)) if m else True
        return None

    # ---- the card, through the Pi (card.py)
    def card_part(self, state):
        print("Pi in USB boot mode: no card set up")
        if not self.ask.yes("Set up the Pi's card? (erases the card)", default=True):
            raise SystemExit("Card not set up: insert a card set up for audiolink, or run audiolink setup again")
        if self.platform != "darwin":
            raise SystemExit("Setting up the card through the Pi works on macOS only so far; write the card by hand "
                             "(README: Make the card), then run audiolink setup again")
        cmd = card.rpiboot_command(self.platform)
        if not cmd:
            raise SystemExit("Setting up the card through the Pi needs rpiboot: %s; then run "
                             "audiolink setup again" % card.install_hint(self.platform))
        image = self.card_image()
        if state == "msd":
            print("The Pi is still in card mode from an earlier try: unplug the Pi and plug it back in")
            self.wait_usb("rom", "Waiting for the Pi", 180)
        dev, _ = card.open_through_pi(self.runner, self.usb_state, "run audiolink setup again", self.platform,
                                      self.sleep, time)
        step("Writing the card")
        card.write(self.runner, dev, image, sleep=self.sleep)
        done("done")
        print("Unplug the Pi and plug it back in")
        self.wait_usb("pi", "Waiting for the Pi to start", 300)
        self.sleep(3)                   # its sound device, a moment after the USB device

    def card_image(self):
        if self.a.card_image:
            image = card.check_image(self.a.card_image)
            if image.is_backup:
                raise SystemExit("%s is a card backup, not an image: put it back with audiolink card restore %s"
                                 % (image.name, self.a.card_image))
            return image
        from .paths import cache_dir
        step("Downloading the card image (audiolink %s)" % card.__version__)
        try:
            path = card.fetch_image(self.runner, cache_dir() / "cards")
        except SystemExit:
            done("failed")
            raise
        done("checked")
        return path

    def wait_usb(self, state, what, seconds):
        step(what)
        end = time.monotonic() + seconds
        while self.usb_state() != state:
            if time.monotonic() >= end:
                done("not yet")
                raise SystemExit("The Pi didn't come back; plug it in again and run audiolink setup again")
            self.sleep(1)
        done("yes" if state != "pi" else "started")

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

    def start_temp_host(self, again=False):
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
        if again:
            return
        ran("audiolink host, until setup is done")
        if self.platform == "darwin":
            print("If macOS asks whether your terminal may use the microphone, or to allow an accessory: allow it")
        end = time.monotonic() + 15
        while self.peer() is None and time.monotonic() < end:
            if self.temp_host.poll() is not None:
                raise SystemExit("audiolink host stopped (output: %s)" % log)
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
        entry, created, first, no_state = got
        self.entry = entry
        name = entry["alias"]
        if first:
            print("Claimed %s: this computer owns it" % name)
        elif created:
            print("Added %s (%s)" % (name, entry.get("role") or "not on its list: host jobs only"))
        else:
            print("%s: added here" % name)
        if no_state:
            print("%s keeps nothing across restarts (no SD card?): its list lasts until it restarts" % name)
        if not first and entry.get("role") != "owner":
            from .client.computer import this_computer
            print("This computer isn't an owner of %s, so it can be its host but not change it (Wi-Fi, name). "
                  "On an owner computer: audiolink invite %s --owner" % (name, this_computer()))
        wanted = self.a.name
        if not wanted and created:
            wanted = self.ask.text_default("Name", name).strip().lower()
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
            print("%s runs an audiolink that can't manage Wi-Fi; update: audiolink update" % self.name)
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

    def host_wanted(self):
        if self.a.host is not None:
            return self.a.host
        if autostart_mod.Autostart(self.installed.argv, self.installed.env, self.runner, self.platform,
                                   self.home).installed() or "expose" in host_config.load(strict=False):
            return True                 # it is already
        return self.ask.yes("Make this computer %s's host?" % self.name, default=True)

    def client_wanted(self):
        if self.a.client is not None:
            return self.a.client
        if self.has_block():
            return True                 # it is already
        return self.ask.yes("Register this computer as a client (ssh %s)?" % self.name, default=True)

    def has_block(self):
        return bool(sshconfig.current(sshconfig.default_path(self.home), keyring.pub_of(self.entry["code"])[:8]))

    # ---- this computer as its host
    def host_part(self):
        saved = host_config.load()
        exposes = self.choose_exposes(saved.get("expose", []))
        changed = exposes != saved.get("expose", [])
        uses = (self.peer() or {}).get("expose") if self.temp_host is None else None
        restart = changed if uses is None else sorted(uses) != sorted(exposes)   # the running host's
        if changed:
            path = host_config.update(expose=exposes)
            detail("saved %s in %s", " ".join("--expose " + e for e in exposes) or "no --expose", path)
        print(self.exposes_shown(exposes))
        self.ssh_port = ssh_exposed(exposes)
        self.autostart_part(restart)

    def exposes_shown(self, exposes):
        ssh = ssh_exposed(exposes)
        shown = ["This computer's ssh"] if ssh else []
        shown += ["--expose %s" % e for e in exposes if _ssh_target(e)[0] != ssh]
        if not shown:
            return "Nothing on this computer is reachable through %s" % self.name
        return "%s %s reachable through %s" % (" and ".join(shown), "is" if len(shown) == 1 else "are",
                                               self.name)

    def choose_exposes(self, current):
        if self.a.expose:
            exposes = list(dict.fromkeys(self.a.expose))
        elif self.a.no_expose:
            exposes = []
        elif "expose" in host_config.load(strict=False):
            exposes = list(current)     # as saved (setup host --expose/--no-expose changes it)
        else:
            exposes = ["%d:22" % SSH_PI_PORT]
        if ssh_exposed(exposes):
            self.check_sshd()
        return exposes

    def check_sshd(self):
        if self.sshd():
            detail("this computer's ssh server answers")
            return
        print("This computer's ssh server is off")
        if self.platform == "darwin":
            print("Turn on Remote Login (needs an admin): System Settings → General → Sharing (opening now)")
            self.runner.run(["open", MAC_SHARING])
        elif self.platform == "win32":
            print("Turn on OpenSSH Server: Settings → System → Optional features, then start the sshd "
                  "service (as an admin)")
        else:
            print("Start an ssh server: e.g. sudo apt install openssh-server && sudo systemctl enable --now ssh")
        if self.ask.tty and not self.ask.auto:
            self.ask.text("Press Enter once Remote Login is on (or to skip) ")
            print("ssh server: %s" % ("on" if self.sshd() else "still off (ssh to this computer won't work "
                                                                "until it's on)"))

    def autostart_part(self, settings_changed):
        svc = autostart_mod.Autostart(self.installed.argv, self.installed.env, self.runner, self.platform, self.home)
        want = self.a.autostart if self.a.autostart is not None else svc.installed()   # opt-in
        running = self.peer() is not None and self.temp_host is None
        if not want:
            if svc.installed():
                why = svc.remove()
                print("audiolink host: no longer starts at login" if not why else "Couldn't remove the login item: %s" % why)
            if running:
                if settings_changed:
                    print("Restart audiolink host for the new settings")
                return
            if self.temp_host is not None and settings_changed:
                self.stop_temp_host()           # the setup's own host: again, with the new settings
                self.start_temp_host(again=True)
                if not self.wait_for_pi(60):
                    self.ssh_port = None
            self.end_note = "To reach %s through this computer, keep audiolink host running" % self.name
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
            print("Couldn't set audiolink host to start at login: %s" % why)
            return
        print("audiolink host: starts at login")
        detail("%s", svc.shown())
        detail("output: %s", svc.where_output())
        step("Starting audiolink host")
        end = time.monotonic() + 60
        p = None
        while p is None and time.monotonic() < end:
            self.sleep(1)
            p = self.peer(connected=True)
        done("connected to %s" % self.pi_name() if p else "not connected yet")
        if p is None:
            print("If the Pi doesn't connect: output is in %s%s" % (
                svc.where_output(), "; and see System Settings → Privacy & Security → Microphone"
                if self.platform == "darwin" else ""))

    def wait_for_other_host(self):
        """A host started by hand (in a terminal) holds the Pi: it has to stop first."""
        print("audiolink host is running here: stop that one (Ctrl-C in its terminal), so the one started at login "
              "takes over")
        self.ask.need_tty("stop audiolink host")
        step("Waiting for audiolink host to stop")
        while self.peer() is not None:
            self.sleep(1)
        done("stopped")

    # ---- this computer as a client
    def client_part(self):
        entry = self.find_pi()
        self.entry, self.name = entry, entry["alias"]
        if self.a.client is False:
            return
        self.client_block()
        self.try_ssh()

    def refreshed(self, entry):
        """A Pi added before: connect once, which brings its current record (and this
        computer's role), so ssh NAME tries its LAN address before the relay. Quiet: the
        relay still works without it."""
        try:
            rec = self.lookup(entry)
        except (SystemExit, OSError, ValueError) as e:
            detail("no record (%s)", getattr(e, "code", e))
            return entry
        detail("record: %s", ", ".join(rec["lan"]) if rec and rec.get("lan") else "none")
        return keyring.lookup(entry["alias"], known=True) if rec else entry

    def find_pi(self):
        a = self.a
        add = SimpleNamespace(code=a.code, name=a.name, at=a.at, invite=a.invite)
        from .client.pairing import add_over_network, add_through_relay
        if a.code:
            return add_through_relay(a.code, add, self.ask)
        if a.pi:
            return self.refreshed(keyring.lookup(a.pi, known=True))
        print("Looking for a Pi on this network...", flush=True)
        entry = add_over_network(add, self.ask)
        if entry:
            return entry
        known = sorted(keyring.load().values(), key=lambda e: e["alias"])
        if len(known) == 1:
            print("No new Pi here; setting up %s (added before)" % known[0]["alias"])
            return self.refreshed(known[0])
        if known:
            return self.refreshed(self.ask.choose("Set up which Pi?", [(e["alias"], e) for e in known]))
        print("No Pi found: not plugged in here, and not on this network")
        print("Just plugged in? The Pi takes about 15 s to start; then run audiolink setup again")
        if in_wsl():
            print("WSL doesn't hear the Pi's answers on the network: audiolink setup --at ADDRESS")
        else:
            print("Know its address? audiolink setup --at ADDRESS")
        code = self.ask.text("Or join through its relay: the Pi's code (al2-..., from audiolink pis PI on a "
                             "computer that has it): ")
        if not code.strip():
            raise SystemExit("Not set up. Join the Pi's network (or its setup network, audiopi-...) and run "
                             "audiolink setup again")
        return add_through_relay(code.strip(), add, self.ask)

    def client_block(self):
        """The ~/.ssh/config block for ssh NAME (not on its host: that's this computer)."""
        entry = keyring.lookup(self.name, known=True)
        path = sshconfig.default_path(self.home)
        pub = keyring.pub_of(entry["code"])
        tag = pub[:8]
        port = (self.is_host and self.ssh_port) or pis.ssh_port(entry, SSH_PI_PORT)
        text = sshconfig.block(tag, self.name, port, pub,
                               pis._launcher(self.installed.argv, self.installed.env))
        result = sshconfig.write(path, tag, text)
        print("ssh %s: %s" % (self.name, "updated" if result == "updated" else "set up"))
        detail("in %s", path)
        for line in sshconfig.others(path, tag, self.name):
            print("%s line %d has another Host %s, which ssh may use instead: remove that one" % (path, line, self.name))
        self.ssh_port = port

    def try_ssh(self, client=True):
        step(("Trying ssh %s" if client else "Checking that ssh %s reaches this computer") % self.name)
        # a client has its ssh NAME block: try that, as the user will; the host's own
        # check has no block to use
        if client:
            written = sshconfig.default_path(self.home)
            own = sshconfig.ssh_default_path()     # where ssh looks: the passwd home, not $HOME
            ok, what = self.login(self.name, self.ssh_port, **({} if own == written else {"config": written}))
        else:
            ok, what = self.banner(self.name, self.ssh_port)
        done(("works" if client else "yes") if ok else what)
        detail("%s", what)
        if ok and client and what == "reached the login":
            # gate 2 is the host's own login (docs/state.md "Two gates"): audiolink got this far
            print("ssh %s reached its host's ssh server, which didn't take this computer's key. On that "
                  "computer, add your key to ~/.ssh/authorized_keys; or from here, if it allows a password: "
                  "%s" % (self.name, copy_id(self.name)))
            print("Done: ssh %s (once your key is let in)" % self.name)
        elif ok:
            print("Done%s" % (": ssh %s" % self.name if client else ""))
        elif self.is_host:
            print("ssh %s works once %s is online (audiolink wifi status) and this computer's ssh server is on"
                  % (self.name, self.name))
        else:
            print("ssh %s works once %s is online and its host is set up (audiolink setup on that computer)"
                  % (self.name, self.name))


def setup_command(a):
    line_buffered()
    g = Guide(a)
    try:
        return g.run()
    finally:
        g.stop_temp_host()
