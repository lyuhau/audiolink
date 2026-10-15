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
from audiolink.host import config as host_config


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


if __name__ == "__main__":
    unittest.main()
