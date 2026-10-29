"""Guided setup without a Pi: the saved host settings, autostart, self-install, the ssh
config block and `audiolink setup`'s flow (with the host daemon, the Pi and the system
stubbed). Everything writes under temporary directories."""
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiolink.__main__ import build_parser
from audiolink import guide, install
from audiolink.client import keyring, sshconfig
from audiolink.client.computer import this_computer
from tests.helpers import PI_USB, FakeClock, needs_modes, needs_symlinks
from audiolink.host import config as host_config
from audiolink.host.autostart import Autostart


def new_identity():
    return os.urandom(32)          # a Pi's public key: all these tests need of it


code_for = keyring.code_for


class Banner(unittest.TestCase):
    def test_reads_the_banner_off_the_stream(self):
        import socket as _socket
        mine, theirs = _socket.socketpair()
        theirs.sendall(b"SSH-2.0-OpenSSH_9.8\r\n")
        conn = mock.Mock(sock=mine)                     # connect's PiConn, now the stream
        with mock.patch("audiolink.client.find._connect", return_value=conn):
            self.assertEqual(guide.ssh_banner("green-rowan", 2222), (True, "SSH-2.0-OpenSSH_9.8"))
        conn.close.assert_called_once()
        theirs.close()
        mine.close()


class FakeRunner:
    """Records commands; answers (status, output) by the first matching prefix in `answers`."""

    def __init__(self, answers=None):
        self.cmds, self.answers, self.spawned = [], answers or {}, []

    def run(self, cmd, timeout=60):
        self.cmds.append(list(cmd))
        for prefix, reply in self.answers.items():
            if " ".join(cmd).startswith(prefix):
                return reply() if callable(reply) else reply
        return 0, ""

    def spawn(self, cmd, log_path):
        self.spawned.append(list(cmd))
        return mock.Mock(poll=lambda: None)

    def ran(self, prefix):
        return [c for c in self.cmds if " ".join(c).startswith(prefix)]


class TempConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"AUDIOLINK_CONFIG_DIR": str(self.dir / "config")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AUDIOLINK_CONTROL_PORT", None)


class HostConfig(TempConfig):
    def parse(self, *argv):
        a = build_parser().parse_args(["host", *argv])
        return host_config.apply(a)

    def test_builtin_values_without_a_file(self):
        a = self.parse()
        self.assertEqual((a.expose, a.forward, a.allow, a.audio_format, a.proxy_port, a.shell_port),
                         ([], [], [], "auto", 47801, 47802))
        self.assertIsNone(a.control_port)

    def test_saved_values_and_flags_win(self):
        host_config.save({"expose": ["2222:22"], "proxy_port": 0, "audio_format": "baseline",
                          "out_device": "AudioPi", "control_port": 47900})
        a = self.parse()
        self.assertEqual((a.expose, a.proxy_port, a.audio_format, a.out_device, a.control_port),
                         (["2222:22"], 0, "baseline", "AudioPi", 47900))
        a = self.parse("--expose", "2200:22", "--proxy-port", "47811")
        self.assertEqual((a.expose, a.proxy_port, a.audio_format), (["2200:22"], 47811, "baseline"))

    def test_update_keeps_the_rest_and_none_removes(self):
        host_config.save({"expose": ["2222:22"], "shell_port": 0})
        host_config.update(expose=["2222:22", "8080:80"])
        self.assertEqual(host_config.load(), {"expose": ["2222:22", "8080:80"], "shell_port": 0})
        host_config.update(shell_port=None)
        self.assertEqual(host_config.load(), {"expose": ["2222:22", "8080:80"]})

    def test_bad_values_say_where(self):
        for bad in ({"expose": "2222:22"}, {"expose": ["nope"]}, {"proxy_port": 70000}, [1]):
            host_config.path().parent.mkdir(parents=True, exist_ok=True)
            host_config.path().write_text(json.dumps(bad))
            with self.assertRaises(SystemExit) as e:
                self.parse()
            self.assertIn("host.json", str(e.exception.code))
            self.assertIsNone(host_config.control_port())        # other commands carry on
        with self.assertRaises(ValueError):
            host_config.save({"expose": ["x"]})

    def test_other_commands_use_the_saved_control_port(self):
        from audiolink import __main__ as cli
        host_config.save({"control_port": 47900})
        seen = {}
        with mock.patch("audiolink.host.logs_cli.logs_command", lambda a: seen.setdefault("port", a.control_port)):
            cli.main(["logs"])
        self.assertEqual(seen["port"], 47900)
        with mock.patch.dict(os.environ, {"AUDIOLINK_CONTROL_PORT": "47950"}):
            self.assertIsNone(host_config.control_port())


class Autostarts(TempConfig):
    def make(self, platform, runner=None):
        return Autostart(["/Apps/audio link/audiolink"], runner=runner or FakeRunner(), platform=platform,
                         home=self.dir / "home", uid=501)

    def test_macos_agent(self):
        r = FakeRunner({"launchctl bootstrap": (0, "")})
        s = self.make("darwin", r)
        self.assertFalse(s.installed())
        self.assertIsNone(s.install())
        try:
            import plistlib
            plist = plistlib.loads(s.path.read_bytes())
        except ImportError:                     # a Python without pyexpat
            self.skipTest("no plistlib")
        self.assertEqual(s.path, self.dir / "home/Library/LaunchAgents/com.audiolink.host.plist")
        self.assertEqual(plist["ProgramArguments"], ["/usr/bin/caffeinate", "-i", "/Apps/audio link/audiolink", "host"])
        self.assertEqual(plist["EnvironmentVariables"], {"AUDIOLINK_CONFIG_DIR": str(self.dir / "config")})
        self.assertTrue(plist["KeepAlive"] and plist["RunAtLoad"])
        self.assertEqual(r.ran("launchctl bootstrap"), [["launchctl", "bootstrap", "gui/501", str(s.path)]])
        self.assertTrue(s.current())
        self.assertIsNone(s.restart())
        self.assertEqual(r.cmds[-1], ["launchctl", "kickstart", "-k", "gui/501/com.audiolink.host"])
        self.assertIsNone(s.remove())
        self.assertFalse(s.installed())
        self.assertEqual(r.cmds[-1], ["launchctl", "bootout", "gui/501/com.audiolink.host"])

    def test_macos_bootstrap_retries_then_says_why(self):
        r = FakeRunner({"launchctl bootstrap": (5, "Bootstrap failed: 5: Input/output error")})
        s = self.make("darwin", r)
        s.sleep = lambda _: None
        self.assertIn("Input/output error", s.install())
        self.assertEqual(len(r.ran("launchctl bootstrap")), 5)

    def test_systemd_unit(self):
        r = FakeRunner()
        s = self.make("linux", r)
        self.assertIsNone(s.install())
        text = s.path.read_text()
        self.assertIn('ExecStart="/Apps/audio link/audiolink" host\n', text)
        self.assertIn("Restart=always", text)
        self.assertEqual(r.cmds, [["systemctl", "--user", "daemon-reload"],
                                  ["systemctl", "--user", "enable", "audiolink-host"],
                                  ["systemctl", "--user", "restart", "audiolink-host"]])
        s.remove()
        self.assertIn(["systemctl", "--user", "disable", "--now", "audiolink-host"], r.cmds)
        self.assertFalse(s.path.exists())

    def test_windows_task(self):
        r = FakeRunner()
        s = self.make("win32", r)
        self.assertIsNone(s.install())
        self.assertEqual(r.cmds[0], ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", "audiolink-host",
                                     "/TR", '"/Apps/audio link/audiolink" host'])
        self.assertEqual(r.cmds[-1], ["schtasks", "/Run", "/TN", "audiolink-host"])
        self.assertTrue(s.current())
        s.remove()
        self.assertIn(["schtasks", "/Delete", "/F", "/TN", "audiolink-host"], r.cmds)
        self.assertFalse(s.installed())


class SelfInstall(TempConfig):
    def setUp(self):
        super().setUp()
        self.home = self.dir / "home"
        self.env = {"PATH": "/usr/bin"}

    def release(self, link=True):
        src = self.dir / "Downloads" / "audiolink"
        (src / "_internal").mkdir(parents=True)
        (src / "audiolink").write_text("v1")
        (src / "_internal" / "lib").write_text("lib")
        if link:
            os.symlink("lib", str(src / "_internal" / "link"))
        return src

    @needs_symlinks
    def test_frozen_folder_on_macos(self):
        src = self.release()
        r = FakeRunner()
        got = install.install(r, "darwin", self.home, self.env, what=("frozen", src, "audiolink"))
        dest = self.home / "Applications" / "audiolink"
        self.assertTrue(got.changed)
        self.assertEqual(got.argv, [str(dest / "audiolink")])
        self.assertEqual((dest / "audiolink").read_text(), "v1")
        self.assertTrue((dest / "_internal" / "link").is_symlink())
        self.assertEqual(os.readlink(str(self.home / "bin" / "audiolink")), str(dest / "audiolink"))
        self.assertEqual(r.cmds, [["xattr", "-dr", "com.apple.quarantine", str(dest)]])
        self.assertTrue(any("PATH" in n for n in got.notes))
        (src / "audiolink").write_text("v2")              # a newer download replaces it
        got = install.install(r, "darwin", self.home, self.env, what=("frozen", src, "audiolink"))
        self.assertEqual((dest / "audiolink").read_text(), "v2")
        self.assertFalse((dest.parent / "audiolink.old").exists())
        got = install.install(r, "darwin", self.home, self.env, what=("frozen", dest, "audiolink"))
        self.assertFalse(got.changed)                     # running the installed one: nothing to do

    def test_someone_elses_file_is_left_alone(self):
        src = self.release(link=False)
        (self.home / "bin").mkdir(parents=True)
        (self.home / "bin" / "audiolink").write_text("#!/bin/sh\necho mine\n")
        got = install.install(FakeRunner(), "darwin", self.home, {"PATH": str(self.home / "bin")},
                              what=("frozen", src, "audiolink"))
        self.assertEqual((self.home / "bin" / "audiolink").read_text(), "#!/bin/sh\necho mine\n")
        self.assertEqual(len(got.notes), 1)
        self.assertIn("something else", got.notes[0])

    def test_pyz_on_linux_gets_a_launcher(self):
        src = self.dir / "audiolink-1.0.pyz"
        src.write_bytes(b"PK zip")
        r = FakeRunner()
        got = install.install(r, "linux", self.home, self.env, what=("pyz", src, None), python="/usr/bin/python3")
        dest = self.home / ".local" / "share" / "audiolink" / "audiolink.pyz"
        self.assertEqual(got.argv, ["/usr/bin/python3", str(dest)])
        launcher = (self.home / ".local" / "bin" / "audiolink").read_text()
        self.assertIn("exec /usr/bin/python3 %s \"$@\"" % shlex.quote(str(dest)), launcher)
        self.assertEqual(r.cmds, [])
        again = install.install(r, "linux", self.home, self.env, what=("pyz", src, None), python="/usr/bin/python3")
        self.assertFalse(again.changed)
        self.assertEqual((self.home / ".local" / "bin" / "audiolink").read_text(), launcher)

    def test_pyz_on_windows(self):
        src = self.dir / "audiolink.pyz"
        src.write_bytes(b"PK zip")
        env = {"LOCALAPPDATA": str(self.dir / "local"), "PATH": ""}
        got = install.install(FakeRunner(), "win32", self.home, env, what=("pyz", src, None), python="C:\\Py\\python.exe")
        self.assertTrue((self.dir / "local" / "Programs" / "audiolink" / "audiolink.cmd").exists())
        self.assertTrue(any("PATH" in n for n in got.notes))

    def old_windows_copy(self):
        """An install by an earlier version, in the cache folder, beside cache files."""
        old = self.dir / "local" / "audiolink"
        (old / "_internal").mkdir(parents=True)
        (old / "audiolink.exe").write_text("v0")
        (old / "found.json").write_text("{}")
        (old / "cards").mkdir()
        return old, {"LOCALAPPDATA": str(self.dir / "local"), "PATH": ""}

    def test_windows_install_leaves_the_cache_alone(self):
        old, env = self.old_windows_copy()
        src = self.dir / "Downloads" / "audiolink"
        (src / "_internal").mkdir(parents=True)
        (src / "audiolink.exe").write_text("v1")
        got = install.install(FakeRunner(), "win32", self.home, env, what=("frozen", src, "audiolink.exe"))
        dest = self.dir / "local" / "Programs" / "audiolink"
        self.assertEqual(got.argv, [str(dest / "audiolink.exe")])
        self.assertEqual((dest / "audiolink.exe").read_text(), "v1")
        (src / "audiolink.exe").write_text("v2")                    # replacing the install
        got = install.install(FakeRunner(), "win32", self.home, env, what=("frozen", src, "audiolink.exe"))
        self.assertEqual((dest / "audiolink.exe").read_text(), "v2")
        self.assertTrue((old / "found.json").exists())              # the cache stays
        self.assertTrue((old / "cards").is_dir())
        self.assertTrue((old / "audiolink.exe").exists())           # the old copy too: something may use it
        self.assertIn("An earlier install is still in %s (audiolink.exe, _internal); delete those once "
                      "nothing uses them" % old, got.notes)

    def test_source_checkout_isnt_installed(self):
        got = install.install(FakeRunner(), "darwin", self.home, self.env, what=("source", Path("/src"), None),
                              python="/py")
        self.assertEqual((got.argv, got.env, got.where), (["/py", str(Path("/src"))], {}, None))
        self.assertIn("source checkout", got.notes[0])
        self.assertFalse(self.home.exists())


class Launcher(unittest.TestCase):
    def test_environment_goes_through_env(self):          # ssh execs ProxyCommand: VAR=x alone fails
        from audiolink.client import pis
        with mock.patch.object(sys, "platform", "linux"):
            got = pis._launcher(["/py", "-m", "audiolink"], {"PYTHONPATH": "/src dir"})
        self.assertEqual(got, "env PYTHONPATH='/src dir' /py -m audiolink")
        self.assertEqual(pis._launcher(["/inst/audiolink"], {}), "/inst/audiolink")

    def test_checkout_runs_without_pythonpath(self):     # what a source checkout's ProxyCommand runs
        checkout = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        p = subprocess.run([sys.executable, checkout, "--help"], cwd=tempfile.gettempdir(), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(b"audiolink", p.stdout)


class CardUsb(unittest.TestCase):
    IOREG = """+-o Root  <class IORegistryEntry>
  +-o AppleT8112USBXHCI@00000000
  | +-o BCM2710 Boot@00100000  <class IOUSBHostDevice>
  |     "idProduct" = 10084
  |     "USB Product Name" = "BCM2710 Boot"
  |     "idVendor" = 2652
"""

    def test_boot_mode_on_macos(self):
        from audiolink import card
        runner = FakeRunner({"ioreg": (0, self.IOREG)})
        self.assertEqual(card.usb_devices(runner, "darwin"), [("0a5c", "2764", "BCM2710 Boot")])
        self.assertEqual(card.usb_state(runner, "darwin", hints=("audiopi",)), "rom")
        runner = FakeRunner({"ioreg": (0, self.IOREG.replace("BCM2710 Boot", "AudioPi"))})
        self.assertEqual(card.usb_state(runner, "darwin", hints=("audiopi",)), "pi")
        # by its USB ID, whatever its name
        runner = FakeRunner({"ioreg": (0, self.IOREG.replace("BCM2710 Boot", "My Link")
                                       .replace("10084", str(PI_USB[1])).replace("2652", str(PI_USB[0])))})
        self.assertEqual(card.usb_state(runner, "darwin"), "pi")

    def test_disks_from_diskutil(self):
        from audiolink import card
        runner = FakeRunner({"diskutil list": (0, "/dev/disk7 (external, physical):\n   #: TYPE NAME\n"),
                             "diskutil info": (0, "   Disk Size:   31.9 GB (31914983424 Bytes) (exactly 1 512-Byte-Units)\n")})
        self.assertEqual(card.external_disks(runner), {"/dev/disk7": 31914983424})


class SshBlock(TempConfig):
    def test_replaced_in_place_never_twice(self):
        path = self.dir / ".ssh" / "config"
        path.parent.mkdir()
        path.write_text("Host *\n  ServerAliveInterval 30")
        b1 = sshconfig.block("abcd1234", "kitchen", 2222, "ab" * 32, "/bin/audiolink")
        self.assertEqual(sshconfig.write(path, "abcd1234", b1), "added")
        self.assertEqual(sshconfig.write(path, "abcd1234", b1), "unchanged")
        with open(str(path), "a") as f:
            f.write("\nHost other\n  Port 1\n")
        b2 = sshconfig.block("abcd1234", "pantry", 2200, "ab" * 32, "/bin/audiolink")
        self.assertEqual(sshconfig.write(path, "abcd1234", b2), "updated")
        text = path.read_text()
        self.assertEqual(text.count("BEGIN audiolink abcd1234"), 1)
        self.assertTrue(text.startswith("Host *\n  ServerAliveInterval 30\n\n# BEGIN"))
        self.assertIn("Host pantry\n  HostName pantry\n  Port 2200\n  HostKeyAlias audiolink-%s\n"
                      "  ServerAliveInterval 30\n  ProxyCommand /bin/audiolink connect pantry %%p\n# END audiolink abcd1234\n" % ("ab" * 32), text)
        self.assertNotIn("kitchen", text)
        self.assertTrue(text.endswith("Host other\n  Port 1\n"))
        self.assertEqual(sshconfig.current(path, "abcd1234"), b2)
        self.assertEqual(sshconfig.others(path, "abcd1234", "pantry"), [])
        with open(str(path), "a") as f:
            f.write("Host pantry\n")
        self.assertEqual(sshconfig.others(path, "abcd1234", "pantry"), [len(text.splitlines()) + 1])

    @needs_modes
    def test_new_file_is_private(self):
        path = self.dir / "h" / ".ssh" / "config"
        sshconfig.write(path, "t", sshconfig.block("t", "x", 2222, "ab" * 32, "a"))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_windows_ssh_folder_keeps_the_profiles_acl(self):
        # a mode on Windows becomes an OWNER RIGHTS ACL, which Windows OpenSSH refuses
        path = self.dir / "h" / ".ssh" / "config"
        with mock.patch.object(sshconfig.sys, "platform", "win32"), \
                mock.patch.object(Path, "mkdir", autospec=True) as mkdir:
            mkdir.side_effect = lambda p, *a, **k: os.makedirs(str(p), exist_ok=True)
            sshconfig.write(path, "t", sshconfig.block("t", "x", 2222, "ab" * 32, "a"))
        self.assertNotIn("mode", mkdir.call_args.kwargs)


class SshLogin(unittest.TestCase):
    """setup's "Trying ssh NAME": the real ssh, so a config ssh refuses shows."""

    def login(self, returncode, stderr, ssh="/usr/bin/ssh", **kw):
        done = subprocess.CompletedProcess([], returncode, "", stderr)
        with mock.patch.object(guide.shutil, "which", lambda name: ssh), \
                mock.patch.object(guide.subprocess, "run", return_value=done) as run, \
                mock.patch.object(guide, "ssh_banner", lambda alias, port: (True, "banner")):
            got = guide.ssh_login("green-rowan", 2222, **kw)
        return got, run

    def test_reaching_the_login_is_enough(self):
        got, run = self.login(255, "lyuhau@green-rowan: Permission denied (publickey,password).\n")
        self.assertEqual(got, (True, "reached the login"))
        argv = run.call_args.args[0]
        self.assertEqual(argv[-2:], ["green-rowan", "exit"])
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("UserKnownHostsFile=" + os.devnull, argv)     # the user's known_hosts untouched

    def test_another_config_file(self):
        _, run = self.login(0, "", config="/tmp/home/.ssh/config")
        self.assertEqual(run.call_args.args[0][1:3], ["-F", "/tmp/home/.ssh/config"])
        _, run = self.login(0, "")
        self.assertNotIn("-F", run.call_args.args[0])

    def test_logged_in(self):
        self.assertEqual(self.login(0, "")[0], (True, "logged in"))

    def test_a_refused_config_is_a_failure(self):
        got, _ = self.login(255, "Bad permissions. Try removing permissions for user: OWNER RIGHTS\n"
                                 "Bad owner or permissions on C:\\Users\\me/.ssh/config\n")
        self.assertEqual(got, (False, "Bad owner or permissions on C:\\Users\\me/.ssh/config"))

    def test_no_ssh_here_checks_the_banner(self):
        self.assertEqual(self.login(0, "", ssh=None)[0], (True, "banner"))


class FakeHost:
    """A running `audiolink host` and the Pi on it, as the control port shows them."""

    def __init__(self, running=True, connected=True):
        self.running, self.connected = running, connected
        self.ident = new_identity()
        self.ident_name, self.claimed = "green-rowan", False
        self.ssid, self.calls = None, []
        self.role = None                # the link's role, once claimed by another computer
        self.expose = None              # what the running host exposes (None: it doesn't say)

    def call(self, op, args=None, port=None, timeout=None):
        self.calls.append(op)
        if not self.running:
            raise SystemExit("audiolink host isn't running (start it: audiolink host)")
        if op == "peer":
            return {"connected": self.connected, "peer": {"caps": ["identity", "wifi", "proxy"],
                                                          "identity": {"name": self.ident_name}},
                    "link": {"role": self.role or ("owner" if self.claimed else None)}, "expose": self.expose}
        if op == "audio.status":
            return {"running": False}
        if not self.connected:
            raise SystemExit("No Pi plugged in")
        if op == "identity.info":
            return {"name": self.ident_name, "serial": "s1", "persistent": True,
                    "pub": keyring.pub_of(code_for(self.ident)), "owners": 1 if self.claimed else 0}
        if op == "identity.claim":
            assert not self.claimed and len(bytes.fromhex(args["key"])) == 32
            self.claimed = True
            return {"members": [{"key": args["key"], "name": args["name"], "role": "owner"}]}
        if op == "identity.rename":
            self.ident_name = args["name"]
            return {}
        if op == "wifi.status":
            return {"ssid": self.ssid, "internet": "online" if self.ssid else None}
        if op == "wifi.list":
            return [{"ssid": "Home", "security": "wpa2", "hidden": False}]
        if op == "wifi.join":
            self.ssid = args["ssid"]
            return {"joined": True, "ip": "10.0.0.5", "internet": "online", "security": "wpa2"}
        raise AssertionError(op)


class Setup(TempConfig):
    def setUp(self):
        super().setUp()
        self.home = self.dir / "home"
        self.host = FakeHost()
        for target in ("audiolink.host.control.call", "audiolink.host.wifi_cli.call"):
            p = mock.patch(target, self.host.call)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(keyring, "PATH", self.dir / "config" / "pis.json")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(guide, "time", FakeClock())
        p.start()
        self.addCleanup(p.stop)
        host = self.host

        def bootstrap():
            host.running = True
            return 0, ""
        self.runner = FakeRunner({"launchctl bootstrap": bootstrap, "launchctl kickstart": bootstrap})

        def spawn(cmd, log):
            self.runner.spawned.append(cmd)
            host.running = True

            def stop():
                host.running = False
            return mock.Mock(poll=lambda: None, terminate=stop, wait=lambda timeout: 0)
        self.runner.spawn = spawn
        self.banners, self.ssh_configs, self.lookups = [], [], []

    def setup(self, *argv, pi_device=True, sshd=True, banner=(True, "SSH-2.0-OpenSSH_9.8"), usb_state=None):
        a = build_parser().parse_args(["setup", *argv])
        inst = install.Installed(["/inst/audiolink"], where=self.dir / "inst", changed=True)

        def ssh(alias, port, config=None):
            self.banners.append((alias, port))
            self.ssh_configs.append(config)
            return banner
        g = guide.Guide(a, runner=self.runner, platform="darwin", home=self.home, pi_device=lambda: pi_device,
                        sshd=lambda: sshd, banner=ssh, login=ssh, installer=lambda: inst,
                        lookup=lambda entry: (self.lookups.append(entry["code"]), None)[1],
                        usb_state=usb_state or (lambda: "pi" if pi_device else None))
        g.ask.tty = False
        out = io.StringIO()
        code = None
        with contextlib.redirect_stdout(out):
            try:
                g.run()
            except SystemExit as e:
                code = e.code
            finally:
                g.stop_temp_host()
        return out.getvalue(), code

    def ssh_config(self):
        return (self.home / ".ssh" / "config").read_text()

    def plist(self):
        return self.home / "Library" / "LaunchAgents" / "com.audiolink.host.plist"

    def test_first_plug_in_with_the_host_part(self):
        self.host.running = False                   # no host yet: setup starts one for itself
        out, code = self.setup("--yes", "--wifi", "Home", "--autostart")
        self.assertIsNone(code, out)
        self.assertEqual(self.runner.spawned, [["/inst/audiolink", "host"]])
        for line in ("Installed audiolink in", "Claimed green-rowan: this computer owns it",
                     "Connecting green-rowan to Home... online", '  (audiolink wifi join "Home")',
                     "This computer's ssh is reachable through green-rowan", "microphone",
                     "audiolink host: starts at login\n", "Starting audiolink host... connected to green-rowan",
                     "Make this computer green-rowan's host? yes",
                     "Register this computer as a client (ssh green-rowan)? yes", "ssh green-rowan: set up",
                     "Trying ssh green-rowan... works", "Done: ssh green-rowan",
                     "Next, for each of your other computers: here, audiolink invite NAME"):
            self.assertIn(line, out)
        self.assertEqual(host_config.load(), {"expose": ["2222:22"]})
        self.assertTrue(self.plist().exists())
        self.assertIn("Host green-rowan\n  HostName green-rowan\n  Port 2222\n  HostKeyAlias audiolink-%s\n"
                      "  ServerAliveInterval 30\n  ProxyCommand /inst/audiolink " % keyring.pub_of(code_for(self.host.ident)) +
                      "connect green-rowan %p", self.ssh_config())
        self.assertNotIn("Start it for this setup", out)                 # the Pi is here: no question
        self.assertEqual(self.banners, [("green-rowan", 2222)])

        out, code = self.setup("--yes")             # again: nothing to do, nothing doubled
        self.assertIsNone(code, out)
        for line in ("green-rowan: added here", "green-rowan: on Wi-Fi Home",
                     "This computer's ssh is reachable through green-rowan",
                     "audiolink host: starts at login, running", "ssh green-rowan: set up", "Done: ssh green-rowan"):
            self.assertIn(line, out)
        self.assertNotIn("(audiolink", out)
        self.assertNotIn("?", out.replace("? yes", ""))                   # nothing asked again
        self.assertNotIn("client", out)                                  # it is one already
        self.assertEqual(self.ssh_config().count("# BEGIN audiolink"), 1)
        self.assertEqual(len(self.runner.ran("launchctl bootstrap")), 1)

    def test_first_plug_in_not_a_client(self):
        self.host.running = False
        out, code = self.setup("--host", "--no-client", "--no-wifi", "--name", "kitchen")
        self.assertIsNone(code, out)
        self.assertIn("  (audiolink rename green-rowan kitchen --on-pi)", out)
        self.assertIn("kitchen: not on Wi-Fi", out)
        self.assertIn("To reach kitchen through this computer, keep audiolink host running", out)
        self.assertEqual(len(self.runner.spawned), 2)        # its own host again, with the new settings
        self.assertFalse(self.plist().exists())              # not started at login unless asked
        self.assertIn("Checking that ssh kitchen reaches this computer... yes", out)
        self.assertIn("Done\nTo reach kitchen through this computer, keep audiolink host running", out)
        self.assertIn("Next, for each of your other computers: here, audiolink invite NAME", out)
        self.assertEqual(host_config.load(), {"expose": ["2222:22"]})
        self.assertFalse((self.home / ".ssh" / "config").exists())
        self.assertEqual(self.banners, [("kitchen", 2222)])
        self.assertEqual(keyring.lookup("kitchen")["name"], "kitchen")

    def test_first_plug_in_client_not_host(self):         # set it up here, plug it in at its host later
        self.host.running = False
        out, code = self.setup("--no-host", "--client", "--no-wifi", "--name", "green-rowan")
        self.assertIsNone(code, out)
        self.assertIn("ssh green-rowan: set up", out)
        self.assertIn("ssh green-rowan works once green-rowan is plugged into a host set up with "
                      "audiolink setup", out)
        self.assertNotIn("Next, for each of your other computers", out)
        self.assertEqual(host_config.load(), {})
        self.assertFalse((self.home / "Library").exists())
        self.assertEqual(self.banners, [])
        self.assertEqual(self.runner.spawned, [["/inst/audiolink", "host"]])   # only for the setup

    def card_states(self, *states):
        """usb_state answers: each given state once, the last one from then on."""
        seq = list(states)
        return lambda: seq.pop(0) if len(seq) > 1 else seq[0]

    def card_mocks(self, disks_after, rpiboot=["/rp", "-d", "/msd"]):
        calls = {"disks": 0, "written": []}

        def disks(runner):
            calls["disks"] += 1
            return {"/dev/disk2": 10 ** 12} if calls["disks"] == 1 else dict({"/dev/disk2": 10 ** 12}, **disks_after)
        for target, value in (("audiolink.card.rpiboot_command", lambda platform: rpiboot),
                              ("audiolink.card.external_disks", disks),
                              ("audiolink.card.write", lambda runner, dev, image, sleep: calls["written"].append((dev, image))),
                              ("audiolink.card.fetch_image", lambda runner, folder: Path("/cache/audiolink-pi.zip"))):
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        return calls

    def test_card_through_the_pi(self):
        self.host.running = False
        calls = self.card_mocks({"/dev/disk7": 31914983424})
        out, code = self.setup("--yes", "--no-wifi", "--no-host", "--no-client",
                               usb_state=self.card_states("rom", "rom", "msd", "pi"))
        self.assertIsNone(code, out)
        for line in ("Pi in USB boot mode: no card set up", "Set up the Pi's card? (erases the card) yes",
                     "Downloading the card image", "Opening the card... 31.9 GB",
                     "Writing the card... done", "Unplug the Pi and plug it back in", "Waiting for the Pi to start... started",
                     "Claimed green-rowan"):
            self.assertIn(line, out)
        self.assertEqual(calls["written"], [("/dev/disk7", Path("/cache/audiolink-pi.zip"))])   # never disk2
        self.assertEqual(self.runner.ran("/rp"), [["/rp", "-d", "/msd"]])

    def test_card_asked_for_only_when_none_shows_up(self):
        self.host.running = False
        calls = self.card_mocks({})
        n = {"calls": 0}

        def disks(runner):                      # the card goes in a while after rpiboot
            n["calls"] += 1
            return {"/dev/disk7": 31914983424} if n["calls"] > 30 else {}
        with mock.patch("audiolink.card.external_disks", disks):
            out, code = self.setup("--yes", "--no-wifi", "--no-host", "--no-client",
                                   usb_state=self.card_states("rom", "rom", "msd", "pi"))
        self.assertIsNone(code, out)
        self.assertIn("Opening the card... no card detected\nWaiting for a card... 31.9 GB", out)
        self.assertEqual(calls["written"][0][0], "/dev/disk7")

    def test_card_needs_rpiboot(self):
        self.host.running = False
        self.card_mocks({}, rpiboot=None)
        out, code = self.setup("--yes", usb_state=lambda: "rom")
        self.assertIn("needs rpiboot: brew install rpiboot", code)

    def test_card_too_big_is_not_written(self):
        self.host.running = False
        calls = self.card_mocks({"/dev/disk9": 2 * 10 ** 12})
        out, code = self.setup("--yes", usb_state=lambda: "rom")
        self.assertIn("too big to be the Pi's card", code)
        self.assertEqual(calls["written"], [])

    def test_a_running_host_is_left_alone_without_a_terminal(self):
        out, code = self.setup("--yes", "--no-wifi", "--autostart")     # started by hand: it has to stop first
        self.assertIn("stop that one (Ctrl-C in its terminal)", out)
        self.assertIn("without a terminal", code)
        self.assertEqual(self.runner.ran("launchctl"), [])

    def test_restart_only_if_the_running_host_differs(self):
        self.host.expose = ["2222:22"]                   # started by hand with --expose 2222:22
        out, code = self.setup("--yes", "--no-wifi", "--no-autostart")
        self.assertIsNone(code, out)
        self.assertIn("This computer's ssh is reachable through green-rowan", out)
        self.assertNotIn("Restart audiolink host", out)
        self.host.expose = []
        out, code = self.setup("host", "--expose", "2222:22", "--expose", "8080:80", "--no-autostart")
        self.assertIn("Restart audiolink host for the new settings", out)

    def test_not_an_owner_says_how(self):              # claimed on another computer
        self.host.claimed, self.host.role = True, "user"
        out, code = self.setup("--yes", "--no-wifi", "--no-host", "--no-client")
        self.assertIsNone(code, out)
        self.assertIn("audiolink invite %s --owner; then here: audiolink setup --invite CODE" % this_computer(), out)

    def test_invite_with_the_pi_plugged_in(self):
        self.host.claimed, self.host.role = True, "user"
        joined = []

        def add_known(entry, a):
            joined.append((entry["alias"], a.invite))
            self.host.role = "owner"                    # the Pi tells the host its list changed
            keyring.update(keyring.pub_of(entry["code"]), role="owner")
            return keyring.lookup(entry["alias"], known=True)
        with mock.patch("audiolink.client.pairing.add_known", add_known):
            out, code = self.setup("--yes", "--no-wifi", "--no-host", "--no-client", "--invite", "123456")
        self.assertIsNone(code, out)
        self.assertEqual(joined, [("green-rowan", "123456")])
        self.assertIn("Joining green-rowan with the invite code given", out)
        self.assertNotIn("isn't an owner", out)
        self.assertEqual(keyring.lookup("green-rowan")["role"], "owner")

        with mock.patch("audiolink.client.pairing.add_known", add_known):     # an owner already
            out, code = self.setup("--yes", "--no-wifi", "--no-host", "--no-client", "--invite", "123456")
        self.assertIn("an owner of green-rowan already", out)
        self.assertEqual(len(joined), 1)

    def test_second_computer(self):
        self.host.running = False
        code = code_for(new_identity())

        def found(a, ask):
            entry, _ = keyring.upsert(code, name="green-rowan", seen=True)
            print("Added green-rowan")
            return entry
        with mock.patch("audiolink.client.pairing.add_over_network", found):
            out, exit_code = self.setup("--yes", pi_device=False)
        self.assertIsNone(exit_code, out)
        self.assertIn("Looking for a Pi on this network...", out)
        self.assertIn("Done: ssh green-rowan", out)
        self.assertNotIn("User ", self.ssh_config())        # the computer behind the Pi can change
        self.assertIn("HostKeyAlias audiolink-%s" % keyring.pub_of(code), self.ssh_config())
        self.assertEqual(self.runner.spawned, [])
        self.assertFalse((self.dir / "config" / "host.json").exists())
        with mock.patch("audiolink.client.pairing.add_over_network", lambda a, ask: None):
            out, exit_code = self.setup("--yes", pi_device=False, banner=(False, "green-rowan is offline"))
        self.assertIn("No new Pi here; setting up green-rowan (added before)", out)
        self.assertIn("Trying ssh green-rowan... green-rowan is offline", out)
        self.assertEqual(self.ssh_config().count("# BEGIN audiolink"), 1)
        self.assertEqual(self.lookups, [keyring.code_for(keyring.pub_of(code))])   # added before: refreshed

    def test_reaching_the_login_explains_gate_2(self):
        self.host.running = False
        keyring.upsert(code_for(new_identity()), name="green-rowan", seen=True)
        with mock.patch("audiolink.client.pairing.add_over_network", lambda a, ask: None):
            out, exit_code = self.setup("--yes", pi_device=False, banner=(True, "reached the login"))
        self.assertIsNone(exit_code, out)
        self.assertIn("Trying ssh green-rowan... works", out)
        self.assertIn("didn't take this computer's key", out)
        self.assertIn(guide.copy_id("green-rowan"), out)
        self.assertIn("Done: ssh green-rowan (once your key is let in)", out)

    def test_copy_id_on_windows(self):                   # Windows' OpenSSH has no ssh-copy-id
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        with mock.patch.object(guide.sys, "platform", "win32"):
            self.assertTrue(guide.copy_id("pi", home).startswith("ssh-keygen -t ed25519, then type "))
            os.mkdir(os.path.join(home, ".ssh"))
            open(os.path.join(home, ".ssh", "id_rsa.pub"), "w").close()
            self.assertEqual(guide.copy_id("pi", home), 'type "%s" | ssh pi "mkdir -p .ssh && cat >> '
                             '.ssh/authorized_keys"' % os.path.join(home, ".ssh", "id_rsa.pub"))
        with mock.patch.object(guide.sys, "platform", "darwin"):
            self.assertEqual(guide.copy_id("pi", home), "ssh-copy-id pi")

    def test_added_before_gets_its_current_record(self):
        self.host.running = False
        code = code_for(new_identity())
        keyring.upsert(code, name="green-rowan", seen=True)
        rec = {"name": "green-rowan", "lan": ["192.168.99.185"], "ports": [2222], "ts": 1}

        def lookup(entry):                              # a connection: auth brings the record
            keyring.update(keyring.pub_of(entry["code"]), last_record=rec)
            return rec
        with mock.patch("audiolink.client.pairing.add_over_network", lambda a, ask: None):
            a = build_parser().parse_args(["setup", "--yes", "-v"])
            g = guide.Guide(a, home=self.home, pi_device=lambda: False, lookup=lookup)
            g.ask.tty = False
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch("audiolink.ui.VERBOSE", True):
                entry = g.find_pi()
        self.assertEqual(entry["last_record"]["lan"], ["192.168.99.185"])
        self.assertIn("[record: 192.168.99.185]", out.getvalue())

    def test_no_record_is_no_failure(self):
        self.host.running = False
        code = code_for(new_identity())
        keyring.upsert(code, name="green-rowan", seen=True)

        def lookup(entry):
            raise SystemExit("green-rowan is offline")
        a = build_parser().parse_args(["setup", "--yes", "--pi", "green-rowan"])
        g = guide.Guide(a, home=self.home, pi_device=lambda: False, lookup=lookup)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(g.find_pi()["alias"], "green-rowan")

    def test_another_host_holds_the_pi(self):           # another config's, or another control port
        self.host.running = False
        for line, port in (("/Applications/audiolink/audiolink host --control-port 47811", "control port 47811"),
                           ("/usr/bin/python3 /Users/me/audiolink host", "another control port"),
                           ("/usr/bin/python3 -m audiolink host -v", "another control port")):
            self.runner.answers["ps"] = (0, "/sbin/launchd\n%s\n" % line)
            out, code = self.setup("--yes")
            self.assertIn("audiolink host is already running for the Pi (%s)" % port, code)
            self.assertEqual(self.runner.spawned, [])                     # no second host
        self.runner.answers["ps"] = (0, "/usr/bin/python3 -m audiolink setup host\nvim audiolink/host.py\n")
        out, code = self.setup("--yes", "--no-wifi", "--no-host", "--no-client", "--name", "green-rowan")
        self.assertIsNone(code, out)
        self.assertEqual(self.runner.spawned, [["/inst/audiolink", "host"]])

    def test_ssh_check_reads_the_file_written(self):      # ssh reads the passwd home's, not $HOME's
        self.host.running = False
        code = code_for(new_identity())
        found = lambda a, ask: keyring.upsert(code, name="green-rowan", seen=True)[0]
        with mock.patch("audiolink.client.pairing.add_over_network", found):
            self.setup("--yes", pi_device=False)
            self.assertEqual(self.ssh_configs, [self.home / ".ssh" / "config"])
            with mock.patch.object(sshconfig, "ssh_default_path", lambda: self.home / ".ssh" / "config"):
                self.setup("--yes", pi_device=False)
            self.assertEqual(self.ssh_configs[1:], [None])           # the same file: ssh as it is

    def test_second_computer_asks_nothing(self):
        self.host.running = False
        code = code_for(new_identity())

        def found(a, ask):
            entry, _ = keyring.upsert(code, name="green-rowan", seen=True)
            return entry
        with mock.patch("audiolink.client.pairing.add_over_network", found):
            out, exit_code = self.setup(pi_device=False)          # no --yes, no terminal: no questions
        self.assertIsNone(exit_code, out)
        self.assertNotIn("?", out)
        self.assertNotIn("User ", self.ssh_config())               # ssh's own default: this login
        self.assertIn("Done: ssh green-rowan", out)

    def test_nothing_found_asks_for_a_code(self):
        self.host.running = False
        with mock.patch("audiolink.client.pairing.add_over_network", lambda a, ask: None):
            out, code = self.setup("--yes", pi_device=False)
        self.assertIn("No Pi found: not plugged in here, and not on this network", out)
        self.assertIn("without a terminal", code)

    def test_nothing_found_in_wsl_says_use_at(self):
        self.host.running = False
        for wsl, line in ((True, "WSL doesn't hear the Pi's answers on the network: audiolink setup --at ADDRESS"),
                          (False, "Know its address? audiolink setup --at ADDRESS")):
            with mock.patch("audiolink.client.pairing.add_over_network", lambda a, ask: None), \
                    mock.patch.object(guide, "in_wsl", lambda: wsl):
                out, _ = self.setup("--yes", pi_device=False)
            self.assertIn(line, out)
            self.assertEqual(out.count("--at ADDRESS"), 1)

    def test_running_where_it_is_names_no_folder(self):
        g = guide.Guide(build_parser().parse_args(["setup", "-v"]), home=self.home,
                        installer=lambda: install.Installed(["/py", "/src"], notes=["Running from a source checkout (/src): not installing"]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("audiolink.ui.VERBOSE", True):
            g.install_step()
        self.assertNotIn("None", out.getvalue())
        self.assertIn("not installing", out.getvalue())

    def test_setup_host_alone(self):
        self.host.running = False
        out, code = self.setup("host", "--expose", "2222:22", "--expose", "8080:80", "--no-autostart",
                               sshd=False)
        self.assertIsNone(code, out)
        self.assertEqual(host_config.load(), {"expose": ["2222:22", "8080:80"]})
        self.assertIn("Remote Login", out)
        self.assertEqual(self.runner.ran("open"), [["open", guide.MAC_SHARING]])
        self.assertIn("keep audiolink host running", out)
        out, code = self.setup("host", "--yes", "--no-expose", "--autostart")
        self.assertEqual(host_config.load(), {"expose": []})
        self.assertIn("audiolink host: starts at login", out)
        out, code = self.setup("host", "--no-expose", "--no-autostart")
        self.assertIn("audiolink host: no longer starts at login", out)
        self.assertFalse(self.plist().exists())


if __name__ == "__main__":
    unittest.main()
