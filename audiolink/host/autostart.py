"""`audiolink host` started at login and kept running, by the system's own service manager:

  macOS    a launchd agent, ~/Library/LaunchAgents/com.audiolink.host.plist (under
           caffeinate -i, so the Mac stays awake), output in ~/Library/Logs/audiolink-host.log
  Linux    a systemd user unit, ~/.config/systemd/user/audiolink-host.service
           (journalctl --user -u audiolink-host)
  Windows  a Task Scheduler task at logon, audiolink-host (in its own window)

The host reads its settings from host.json (host/config.py), so the service itself is just
`audiolink host`: changing a setting restarts it, it doesn't rewrite it.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

from ..paths import config_dir
from ..system import Runner

LABEL = "com.audiolink.host"
UNIT = "audiolink-host"
TASK = "audiolink-host"

MAC_MIC = "If macOS asks whether audiolink may use the microphone: allow it (that's how the Pi's data comes in)"


class Autostart:
    def __init__(self, argv, env=None, runner=None, platform=sys.platform, home=None, uid=None):
        """argv: how to run audiolink (install.Installed.argv); env: its extra environment."""
        self.argv, self.env = list(argv), dict(env or {})
        if os.environ.get("AUDIOLINK_CONFIG_DIR"):          # a second identity on this machine
            self.env.setdefault("AUDIOLINK_CONFIG_DIR", os.environ["AUDIOLINK_CONFIG_DIR"])
        self.runner = runner or Runner()
        self.platform = platform
        self.home = Path(home or Path.home())
        self.sleep = time.sleep
        self.uid = uid if uid is not None else (os.getuid() if hasattr(os, "getuid") else 0)

    # ---- what goes where
    @property
    def path(self):
        if self.platform == "darwin":
            return self.home / "Library" / "LaunchAgents" / (LABEL + ".plist")
        if self.platform == "win32":
            return config_dir() / "autostart.txt"          # what the task runs, to tell changes
        return self.home / ".config" / "systemd" / "user" / (UNIT + ".service")

    @property
    def log(self):
        if self.platform == "darwin":
            return self.home / "Library" / "Logs" / "audiolink-host.log"
        return None

    def command(self):
        return self.argv + ["host"]

    def content(self):
        if self.platform == "darwin":
            plist = {"Label": LABEL, "ProgramArguments": ["/usr/bin/caffeinate", "-i"] + self.command(),
                     "RunAtLoad": True, "KeepAlive": True, "ProcessType": "Interactive",
                     "StandardOutPath": str(self.log), "StandardErrorPath": str(self.log)}
            if self.env:
                plist["EnvironmentVariables"] = self.env
            return _plist(plist)
        if self.platform == "win32":
            return self.task_command() + "\n"
        lines = ["[Unit]", "Description=audiolink host (the Pi on USB)", "", "[Service]",
                 "ExecStart=" + " ".join(_systemd_quote(x) for x in self.command())]
        lines += ["Environment=" + _systemd_quote("%s=%s" % kv) for kv in sorted(self.env.items())]
        lines += ["Restart=always", "RestartSec=5", "", "[Install]", "WantedBy=default.target", ""]
        return "\n".join(lines)

    def task_command(self):
        return subprocess.list2cmdline(self.command())

    def shown(self):
        """Where it is, for people."""
        return "Task Scheduler: %s" % TASK if self.platform == "win32" else str(self.path)

    # ---- state
    def installed(self):
        return self.path.exists()

    def current(self):
        """Installed, and running what it would be installed with now?"""
        try:
            return self.path.read_text() == self.content()
        except OSError:
            return False

    # ---- changes
    def install(self):
        """Write it and (re)start it -> None, or why it failed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.platform == "darwin":
            self.log.parent.mkdir(parents=True, exist_ok=True)
            target = "gui/%d" % self.uid
            self.runner.run(["launchctl", "bootout", "%s/%s" % (target, LABEL)])   # not loaded: fails, fine
            self.path.write_text(self.content())
            self.runner.run(["launchctl", "enable", "%s/%s" % (target, LABEL)])
            for _ in range(5):          # a bootout just now can still be finishing ("5: Input/output error")
                why = self._ok(["launchctl", "bootstrap", target, str(self.path)])
                if not why:
                    return None
                self.sleep(1)
            return why
        if self.platform == "win32":
            why = self._ok(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", TASK, "/TR", self.task_command()])
            if why:
                return why
            self.path.write_text(self.content())
            return self.restart()
        self.path.write_text(self.content())
        return (self._ok(["systemctl", "--user", "daemon-reload"])
                or self._ok(["systemctl", "--user", "enable", UNIT])
                or self._ok(["systemctl", "--user", "restart", UNIT]))

    def restart(self):
        if self.platform == "darwin":
            return self._ok(["launchctl", "kickstart", "-k", "gui/%d/%s" % (self.uid, LABEL)])
        if self.platform == "win32":
            self.runner.run(["schtasks", "/End", "/TN", TASK])
            return self._ok(["schtasks", "/Run", "/TN", TASK])
        return self._ok(["systemctl", "--user", "restart", UNIT])

    def remove(self):
        """Stop it and take it out (nothing there: nothing to do) -> None, or why it failed."""
        if self.platform == "darwin":
            self.runner.run(["launchctl", "bootout", "gui/%d/%s" % (self.uid, LABEL)])
        elif self.platform == "win32":
            self.runner.run(["schtasks", "/End", "/TN", TASK])
            why = self._ok(["schtasks", "/Delete", "/F", "/TN", TASK]) if self.installed() else None
            if why:
                return why
        else:
            self.runner.run(["systemctl", "--user", "disable", "--now", UNIT])
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        if self.platform not in ("darwin", "win32"):
            self.runner.run(["systemctl", "--user", "daemon-reload"])
        return None

    def _ok(self, cmd):
        rc, out = self.runner.run(cmd)
        return None if rc == 0 else "%s failed: %s" % (" ".join(cmd[:2]), out.strip() or "exit %d" % rc)

    def where_output(self):
        """Where to look when it doesn't connect."""
        if self.platform == "darwin":
            return str(self.log)
        if self.platform == "win32":
            return "its window"
        return "journalctl --user -u %s" % UNIT


def _plist(d):
    """A launchd plist (written by hand: plistlib needs pyexpat, which some Pythons lack)."""
    from xml.sax.saxutils import escape

    def value(v, ind):
        if isinstance(v, bool):
            return "<true/>" if v else "<false/>"
        if isinstance(v, str):
            return "<string>%s</string>" % escape(v)
        if isinstance(v, list):
            return "<array>\n%s\n%s</array>" % ("\n".join(ind + "  " + value(x, ind + "  ") for x in v), ind)
        return "<dict>\n%s\n%s</dict>" % ("\n".join("%s  <key>%s</key>%s" % (ind, escape(k), value(x, ind + "  "))
                                                      for k, x in v.items()), ind)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n%s\n</plist>\n' % value(d, ""))


def _systemd_quote(arg):
    """One word of a systemd ExecStart= / Environment= line."""
    if arg and all(c.isalnum() or c in "/._-=:+,@" for c in arg):
        return arg
    return '"%s"' % arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")

