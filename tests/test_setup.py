"""Guided setup without a Pi: the saved host settings, autostart, self-install, the ssh
config block and `audiolink setup`'s flow (with the host daemon, the Pi and the system
stubbed). Everything writes under temporary directories."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiolink.__main__ import build_parser
from audiolink import install
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


if __name__ == "__main__":
    unittest.main()
