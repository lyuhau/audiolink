"""Terminal plumbing shared by the commands: pausing logs, asking questions, output order,
and the two levels of output.

Command output is for people who don't know how audiolink works: say what happened
in plain words and what to do next. Addresses, timings, codes and other internals go
through detail(), shown only with -v/--verbose.
"""
import logging
import sys
import threading

VERBOSE = False                         # set from -v/--verbose (__main__)


def duration(seconds):
    """30 -> "30 seconds", 180 -> "3 minutes"."""
    seconds = int(seconds)
    if seconds < 90:
        return "%d seconds" % seconds
    return "%d minutes" % round(seconds / 60)


def detail(msg, *args):
    """Technical detail: printed only with -v."""
    if VERBOSE:
        print("  [%s]" % (msg % args if args else msg), flush=True)


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


class Prompter:
    """Interactive questions, or clear errors when there's no terminal to ask on."""

    def __init__(self, a, flags_hint=""):
        self.a = a
        self.flags_hint = flags_hint    # options that answer the questions up front
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
            raise SystemExit("%s (no terminal to ask on%s)" % (
                what.rstrip(" ?:"), "; see " + self.flags_hint if self.flags_hint else ""))

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

    def text(self, prompt):
        self.need_tty(prompt)
        return self._input(prompt)

    def secret(self, prompt):
        """Typed without echo; "cancelled" on Ctrl-C / EOF."""
        import getpass
        self.need_tty(prompt)
        try:
            return getpass.getpass(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit("cancelled")
