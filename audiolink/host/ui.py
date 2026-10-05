"""Terminal plumbing for the host: pausing logs, asking questions, output order."""
import logging
import os
import sys
import threading


class LogGate(logging.Handler):
    """Passes records to the console, or holds them while a prompt owns the terminal.
    (pause/resume, not hold/release: logging.Handler uses acquire/release itself.)"""

    def __init__(self, target):
        super().__init__()
        self.target, self.held, self.depth = target, [], 0
        self.glock = threading.RLock()

    def emit(self, record):
        with self.glock:
            if self.depth:
                self.held.append(record)
            else:
                self.target.handle(record)

    def pause(self):
        with self.glock:
            self.depth += 1

    def resume(self):
        with self.glock:
            self.depth = max(0, self.depth - 1)
            if not self.depth:
                for record in self.held:
                    self.target.handle(record)
                self.held = []


def line_buffered():
    try:
        sys.stdout.reconfigure(line_buffering=True)   # keep prints in order with logs/errors
    except AttributeError:
        pass


SEC_LABEL = {"open": "open", "owe": "open+", "wpa2": "WPA2", "wpa3": "WPA3", "wpa2/wpa3": "WPA2/3",
             "wep": "WEP", "enterprise": "802.1X"}


class Prompter:
    """Interactive questions, or clear errors when there's no terminal to ask on."""

    def __init__(self, a):
        self.a = a
        self.tty = sys.stdin.isatty()
        self.stdin_pw = None
        if getattr(a, "password_stdin", False):
            self.stdin_pw = sys.stdin.readline().rstrip("\r\n")

    @staticmethod
    def _input(prompt):
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit("cancelled")

    def need_tty(self, what):
        if not self.tty:
            raise SystemExit("%s (no terminal to ask on; see --security, --hidden, --password-stdin)"
                             % what.rstrip(" ?:"))

    def yes(self, question, default=False):
        self.need_tty(question)
        ans = self._input("%s [%s] " % (question, "Y/n" if default else "y/N")).strip().lower()
        return default if not ans else ans.startswith("y")

    def choose(self, question, options):
        self.need_tty(question)
        for i, (label, _) in enumerate(options, 1):
            print("  %d) %s" % (i, label))
        while True:
            ans = self._input("%s [1-%d] " % (question, len(options))).strip()
            if ans.isdigit() and 1 <= int(ans) <= len(options):
                return options[int(ans) - 1][1]

    def password(self, ssid, security, retry=False):
        """Returns (password, can_retry)."""
        if self.stdin_pw is not None:
            return self.stdin_pw, False
        env = os.environ.get("AUDIOLINK_WIFI_PASSWORD")
        if env and not retry:
            return env, False
        import getpass
        self.need_tty("%r needs a password" % ssid)
        while True:
            try:
                pw = getpass.getpass("%s password for %r: " % (SEC_LABEL.get(security, security), ssid))
            except (EOFError, KeyboardInterrupt):
                print()
                raise SystemExit("cancelled")
            if security in ("wpa2", "wpa2/wpa3") and not 8 <= len(pw) <= 63:
                print("WPA2 passwords are 8-63 characters; try again")
                continue
            if not pw:
                continue
            return pw, True
