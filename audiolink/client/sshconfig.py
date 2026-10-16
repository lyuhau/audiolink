"""The ~/.ssh/config entry `audiolink setup` keeps for each Pi: a block between markers
named by the Pi's key (so renaming it replaces the block), replaced in place when setup
runs again, never added twice. Everything else in the file stays as it is."""
import os
import re
import sys
from pathlib import Path

BEGIN, END = "# BEGIN audiolink %s", "# END audiolink %s"


def default_path(home=None):
    return Path(home or Path.home()) / ".ssh" / "config"


def ssh_default_path():
    """The file ssh itself reads: under the passwd entry's home (ssh ignores $HOME),
    or the profile folder on Windows. None if that can't be told."""
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".ssh" / "config"
    except (ImportError, KeyError):
        return Path(os.environ["USERPROFILE"]) / ".ssh" / "config" if os.environ.get("USERPROFILE") else None


def block(tag, alias, port, user, proxy):
    """proxy: how to run audiolink in a ProxyCommand (already quoted)."""
    return "\n".join([
        BEGIN % tag,
        "# %s: written by audiolink setup (run it again to change this; it replaces this block)" % alias,
        "Host %s" % alias,
        "  HostName %s" % alias,
        "  Port %d" % port] + (["  User %s" % user] if user else []) + [
        "  ProxyCommand %s connect %s %%p" % (proxy, alias),
        END % tag]) + "\n"


def _span(lines, tag):
    """(first, last) line index of the block for tag, or None."""
    try:
        first = lines.index(BEGIN % tag)
        return first, lines.index(END % tag, first)
    except ValueError:
        return None


def read(path):
    try:
        with open(str(path), encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def current(path, tag):
    """The block for tag as it is in the file now, or None."""
    lines = read(path).splitlines()
    span = _span(lines, tag)
    return "\n".join(lines[span[0]:span[1] + 1]) + "\n" if span else None


def others(path, tag, alias):
    """Line numbers (from 1) of Host lines for alias outside the block: ssh takes the
    first entry that matches, so an earlier one wins over ours."""
    lines = read(path).splitlines()
    span = _span(lines, tag) or (-1, -1)
    out = []
    for i, line in enumerate(lines):
        m = re.match(r"\s*Host\s+(.*)", line, re.I)
        if m and alias in m.group(1).split() and not span[0] < i < span[1]:
            out.append(i + 1)
    return out


def write(path, tag, text):
    """Put the block in (replacing the old one) -> "added", "updated" or "unchanged"."""
    path = Path(path)
    old = read(path)
    lines = old.splitlines()
    span = _span(lines, tag)
    if span:
        if "\n".join(lines[span[0]:span[1] + 1]) + "\n" == text:
            return "unchanged"
        new_lines = lines[:span[0]] + text.rstrip("\n").split("\n") + lines[span[1] + 1:]
        new = "\n".join(new_lines) + "\n"
    else:
        new = old + ("\n" if old and not old.endswith("\n") else "") + ("\n" if old.strip() else "") + text
    # Windows: no mode. Python 3.12.4+ turns 0o700 into an ACL for OWNER RIGHTS, which
    # Windows OpenSSH refuses ("Bad permissions"); the profile's inherited ACL is what it wants.
    path.parent.mkdir(**({} if sys.platform == "win32" else {"mode": 0o700}), parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".audiolink-tmp")
    with open(str(tmp), "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    try:
        os.chmod(str(tmp), (os.stat(str(path)).st_mode & 0o777) if old else 0o600)
    except OSError:
        pass
    os.replace(str(tmp), str(path))
    return "updated" if span else "added"

