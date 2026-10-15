"""Guided setup without a Pi: the saved host settings, autostart, self-install, the ssh
config block and `audiolink setup`'s flow (with the host daemon, the Pi and the system
stubbed). Everything writes under temporary directories."""
import contextlib
import getpass
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiolink.__main__ import build_parser
from audiolink import guide, install
from audiolink.client import keyring, sshconfig
from audiolink.discovery.record import code_for, new_identity
from tests.helpers import FakeClock
from audiolink.host import config as host_config
from audiolink.host.autostart import Autostart


class FakeRunner:
    """Records commands; answers (status, output) by the first matching prefix in `answers`."""

    def __init__(self, answers=None):
        self.cmds, self.answers, self.spawned = [], answers or {}, []

    def run(self, cmd):
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
        with mock.patch("audiolink.host.debug_cli.debug_command", lambda a: seen.setdefault("port", a.control_port)):
            cli.main(["debug"])
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

    def release(self):
        src = self.dir / "Downloads" / "audiolink"
        (src / "_internal").mkdir(parents=True)
        (src / "audiolink").write_text("v1")
        (src / "_internal" / "lib").write_text("lib")
        os.symlink("lib", str(src / "_internal" / "link"))
        return src

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
        src = self.release()
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
        self.assertIn("exec /usr/bin/python3 %s \"$@\"" % dest, launcher)
        self.assertEqual(r.cmds, [])
        again = install.install(r, "linux", self.home, self.env, what=("pyz", src, None), python="/usr/bin/python3")
        self.assertFalse(again.changed)
        self.assertEqual((self.home / ".local" / "bin" / "audiolink").read_text(), launcher)

    def test_pyz_on_windows(self):
        src = self.dir / "audiolink.pyz"
        src.write_bytes(b"PK zip")
        env = {"LOCALAPPDATA": str(self.dir / "local"), "PATH": ""}
        got = install.install(FakeRunner(), "win32", self.home, env, what=("pyz", src, None), python="C:\\Py\\python.exe")
        self.assertTrue((self.dir / "local" / "audiolink" / "audiolink.cmd").exists())
        self.assertTrue(any("PATH" in n for n in got.notes))

    def test_source_checkout_isnt_installed(self):
        got = install.install(FakeRunner(), "darwin", self.home, self.env, what=("source", Path("/src"), None),
                              python="/py")
        self.assertEqual((got.argv, got.env, got.where), (["/py", "-m", "audiolink"], {"PYTHONPATH": "/src"}, None))
        self.assertIn("source checkout", got.notes[0])
        self.assertFalse(self.home.exists())


class Launcher(unittest.TestCase):
    def test_environment_goes_through_env(self):          # ssh execs ProxyCommand: VAR=x alone fails
        from audiolink.client import pis
        got = pis._launcher(["/py", "-m", "audiolink"], {"PYTHONPATH": "/src dir"})
        self.assertEqual(got, "env PYTHONPATH='/src dir' /py -m audiolink")
        self.assertEqual(pis._launcher(["/inst/audiolink"], {}), "/inst/audiolink")


class SshBlock(TempConfig):
    def test_replaced_in_place_never_twice(self):
        path = self.dir / ".ssh" / "config"
        path.parent.mkdir()
        path.write_text("Host *\n  ServerAliveInterval 30")
        b1 = sshconfig.block("abcd1234", "kitchen", 2222, "me", "/bin/audiolink")
        self.assertEqual(sshconfig.write(path, "abcd1234", b1), "added")
        self.assertEqual(sshconfig.write(path, "abcd1234", b1), "unchanged")
        with open(str(path), "a") as f:
            f.write("\nHost other\n  Port 1\n")
        b2 = sshconfig.block("abcd1234", "pantry", 2200, "me", "/bin/audiolink")
        self.assertEqual(sshconfig.write(path, "abcd1234", b2), "updated")
        text = path.read_text()
        self.assertEqual(text.count("BEGIN audiolink abcd1234"), 1)
        self.assertTrue(text.startswith("Host *\n  ServerAliveInterval 30\n\n# BEGIN"))
        self.assertIn("Host pantry\n  HostName pantry\n  Port 2200\n  User me\n"
                      "  ProxyCommand /bin/audiolink connect pantry %p\n# END audiolink abcd1234\n", text)
        self.assertNotIn("kitchen", text)
        self.assertTrue(text.endswith("Host other\n  Port 1\n"))
        self.assertEqual(sshconfig.current(path, "abcd1234"), b2)
        self.assertEqual(sshconfig.others(path, "abcd1234", "pantry"), [])
        with open(str(path), "a") as f:
            f.write("Host pantry\n")
        self.assertEqual(sshconfig.others(path, "abcd1234", "pantry"), [len(text.splitlines()) + 1])

    def test_new_file_is_private(self):
        path = self.dir / "h" / ".ssh" / "config"
        sshconfig.write(path, "t", sshconfig.block("t", "x", 2222, "me", "a"))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)


class FakeHost:
    """A running `audiolink host` and the Pi on it, as the control port shows them."""

    def __init__(self, running=True, connected=True):
        self.running, self.connected = running, connected
        self.ident = new_identity()
        self.ident_name, self.claimed = "green-rowan", False
        self.ssid, self.calls = None, []
        self.expose = None              # what the running host exposes (None: it doesn't say)

    def call(self, op, args=None, port=None, timeout=None):
        self.calls.append(op)
        if not self.running:
            raise SystemExit("audiolink host isn't running (start it: audiolink host)")
        if op == "peer":
            return {"connected": self.connected, "peer": {"caps": ["identity", "wifi", "proxy"],
                                                          "identity": {"name": self.ident_name}},
                    "expose": self.expose}
        if op == "audio.status":
            return {"running": False}
        if not self.connected:
            raise SystemExit("No Pi plugged in")
        if op == "identity.info":
            return {"name": self.ident_name, "serial": "s1", "persistent": True,
                    "pub": keyring.pub_of(code_for(self.ident))}
        if op == "identity.claim":
            first, self.claimed = not self.claimed, True
            return {"first": first}
        if op == "identity.code":
            return {"code": code_for(self.ident)}
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
        self.banners = []

    def setup(self, *argv, pi_device=True, sshd=True, banner=(True, "SSH-2.0-OpenSSH_9.8")):
        a = build_parser().parse_args(["setup", *argv])
        inst = install.Installed(["/inst/audiolink"], where=self.dir / "inst", changed=True)

        def ssh(alias, port):
            self.banners.append((alias, port))
            return banner
        g = guide.Guide(a, runner=self.runner, platform="darwin", home=self.home, pi_device=lambda: pi_device,
                        sshd=lambda: sshd, banner=ssh, installer=lambda: inst)
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
        out, code = self.setup("--yes", "--wifi", "Home")
        self.assertIsNone(code, out)
        self.assertEqual(self.runner.spawned, [["/inst/audiolink", "host"]])
        for line in ("Installed audiolink in", "Added green-rowan (claimed: this is its first computer)",
                     "  (audiolink add)", "Connecting green-rowan to Home... online", '  (audiolink wifi join "Home")',
                     "This computer's ssh is reachable through green-rowan", "microphone",
                     "audiolink host: starts at login\n", "Starting it... connected to green-rowan",
                     "Make this computer green-rowan's host? yes",
                     "Use this computer as a client (ssh green-rowan)? yes", "ssh green-rowan: set up",
                     "Trying ssh green-rowan... works", "Done: ssh green-rowan",
                     "Next, on your other computers: audiolink setup"):
            self.assertIn(line, out)
        self.assertEqual(host_config.load(), {"expose": ["2222:22"]})
        self.assertTrue(self.plist().exists())
        self.assertIn("Host green-rowan\n  HostName green-rowan\n  Port 2222\n  ProxyCommand /inst/audiolink "
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
        self.assertIn("audiolink host: starts at login", out)            # plugged in here: its host
        self.assertIn("Checking that ssh kitchen reaches this computer... yes", out)
        self.assertIn("Done\nNext, on your other computers: audiolink setup", out)
        self.assertEqual(host_config.load(), {"expose": ["2222:22"]})
        self.assertFalse((self.home / ".ssh" / "config").exists())
        self.assertEqual(self.banners, [("kitchen", 2222)])
        self.assertEqual(keyring.lookup("kitchen")["name"], "kitchen")

    def test_first_plug_in_client_not_host(self):         # set it up here, plug it in at its host later
        self.host.running = False
        out, code = self.setup("--no-host", "--client", "--no-wifi", "--name", "green-rowan")
        self.assertIsNone(code, out)
        self.assertIn("ssh green-rowan: set up", out)
        self.assertIn("ssh green-rowan works once green-rowan is plugged into its host, set up there with "
                      "audiolink setup", out)
        self.assertNotIn("Next, on your other computers", out)
        self.assertEqual(host_config.load(), {})
        self.assertFalse((self.home / "Library").exists())
        self.assertEqual(self.banners, [])
        self.assertEqual(self.runner.spawned, [["/inst/audiolink", "host"]])   # only for the setup

    def test_a_running_host_is_left_alone_without_a_terminal(self):
        out, code = self.setup("--yes", "--no-wifi")     # started by hand: it has to stop first
        self.assertIn("stop it (Ctrl-C in its window)", out)
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

    def test_second_computer(self):
        self.host.running = False
        code = code_for(new_identity())

        def found(a, ask):
            entry, _ = keyring.upsert(code, name="green-rowan", seen=True)
            print("Added green-rowan")
            return entry
        with mock.patch("audiolink.client.pairing.add_over_network", found):
            out, exit_code = self.setup("--yes", "--user", "alice", pi_device=False)
        self.assertIsNone(exit_code, out)
        self.assertIn("Looking for a Pi on this network...", out)
        self.assertIn("Done: ssh green-rowan", out)
        self.assertIn("User alice", self.ssh_config())
        self.assertEqual(self.runner.spawned, [])
        self.assertFalse((self.dir / "config" / "host.json").exists())
        with mock.patch("audiolink.client.pairing.add_over_network", lambda a, ask: None):
            out, exit_code = self.setup("--yes", pi_device=False, banner=(False, "green-rowan is offline"))
        self.assertIn("No new Pi here; setting up green-rowan (added before)", out)
        self.assertIn("User alice", self.ssh_config())       # its earlier answer
        self.assertIn("Trying ssh green-rowan... green-rowan is offline", out)
        self.assertEqual(self.ssh_config().count("# BEGIN audiolink"), 1)

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

    def test_setup_host_alone(self):
        self.host.running = False
        out, code = self.setup("host", "--expose", "2222:22", "--expose", "8080:80", "--no-autostart",
                               sshd=False)
        self.assertIsNone(code, out)
        self.assertEqual(host_config.load(), {"expose": ["2222:22", "8080:80"]})
        self.assertIn("Remote Login", out)
        self.assertEqual(self.runner.ran("open"), [["open", guide.MAC_SHARING]])
        self.assertIn("Start it when you need the Pi: audiolink host", out)
        out, code = self.setup("host", "--yes", "--no-expose")
        self.assertEqual(host_config.load(), {"expose": []})
        self.assertIn("audiolink host: starts at login", out)
        out, code = self.setup("host", "--no-expose", "--no-autostart")
        self.assertIn("audiolink host: no longer starts at login", out)
        self.assertFalse(self.plist().exists())


if __name__ == "__main__":
    unittest.main()
