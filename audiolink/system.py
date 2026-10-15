"""Running the system's own tools (launchctl, systemctl, schtasks, xattr, open, ...) for
`audiolink setup`: one place, so tests can stand in for it (FakeRunner in the tests)
and -v shows each command."""
import subprocess

from .ui import detail


class Runner:
    def run(self, cmd, timeout=60):
        """Run a command to the end -> (exit status, its output). A missing tool is 127."""
        detail("$ %s", " ".join(cmd))
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            detail("%s", e)
            return 127, str(e)
        out = p.stdout.decode(errors="replace")
        if p.returncode:
            detail("exit %d: %s", p.returncode, out.strip())
        return p.returncode, out

    def spawn(self, cmd, log_path):
        """Start a command in the background, its output to log_path -> a Popen."""
        detail("$ %s &  (output: %s)", " ".join(cmd), log_path)
        log = open(str(log_path), "ab")
        try:
            return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        finally:
            log.close()
