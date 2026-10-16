"""Terminal plumbing shared by the commands: pausing logs, asking questions, output order,
and the two levels of output.

Command output is for people who don't know how audiolink works: say what happened
in plain words and what to do next. Addresses, timings, codes and other internals go
through detail(), shown only with -v/--verbose.
"""
import logging
import sys
import threading
import time

VERBOSE = False                         # set from -v/--verbose (__main__)
_open_step = False                      # a step() line is waiting for its done()


def step(msg):
    """Start a progress line: "Checking X... " (finish it with done())."""
    global _open_step
    print(msg + "... ", end="", flush=True)
    _open_step = True


def done(result):
    """Finish the progress line started by step()."""
    global _open_step
    print(result, flush=True)
    _open_step = False


def ago(ts):
    """How long ago a time (epoch seconds) was, roughly: "5 min ago", "3 h ago", "never"."""
    if not ts:
        return "never"
    s = time.time() - ts
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= n:
            return "%d %s ago" % (s // n, unit)
    return "just now"


def step_open():
    """Is a step() line waiting for its done()?"""
    return _open_step


def detail(msg, *args):
    """Technical detail: printed only with -v."""
    if VERBOSE:
        print("%s  [%s]" % ("\n" if _open_step else "", msg % args if args else msg), flush=True)


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


def safe_output():
    """Text a stream's encoding can't hold (→ in cp1252: Windows output to a pipe, a
    file or a scheduled task's log) comes out as ? instead of ending the command."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:          # None (no console) or not a text stream
            pass


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
            raise SystemExit("Cancelled")

    def need_tty(self, what):
        if not self.tty:
            raise SystemExit("Can't ask \"%s\" without a terminal%s" % (
                what.strip(), " (answer it with: %s)" % self.flags_hint if self.flags_hint else ""))

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
            raise SystemExit("Cancelled")
