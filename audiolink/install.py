"""`audiolink setup` puts audiolink somewhere permanent, so autostart and the ssh entries
keep working after Downloads is cleaned up:

  macOS    ~/Applications/audiolink (the folder, or audiolink.pyz), linked from ~/bin/audiolink
  Linux    ~/.local/share/audiolink, linked from ~/.local/bin/audiolink
  Windows  %LOCALAPPDATA%\\audiolink (add it to PATH; a .pyz gets audiolink.cmd next to it)

The release folder (PyInstaller) is copied whole; a .pyz is copied, with a small launcher
script. macOS's quarantine flag is cleared on the copy. Running the installed copy (or
from a source checkout) installs nothing.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

MARK = "# audiolink launcher (audiolink setup)"


class Installed:
    """argv: how to run the installed audiolink; env: what it needs in its environment;
    changed: copied just now; where: the installed folder (None: not installed)."""

    def __init__(self, argv, env=None, changed=False, where=None, notes=()):
        self.argv, self.env, self.changed, self.where, self.notes = argv, env or {}, changed, where, list(notes)


def places(platform=sys.platform, home=None, environ=None):
    """(the folder audiolink goes in, the folder its link goes in, or None on Windows)."""
    home = Path(home or Path.home())
    environ = os.environ if environ is None else environ
    if platform == "darwin":
        return home / "Applications" / "audiolink", home / "bin"
    if platform == "win32":
        return Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "audiolink", None
    data = Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return data / "audiolink", home / ".local" / "bin"


def running():
    """What's running: ("frozen", the release folder, the executable's name), ("pyz", the
    file, None) or ("source", the checkout, None)."""
    if getattr(sys, "frozen", False):
        exe = Path(os.path.realpath(sys.executable))
        return "frozen", exe.parent, exe.name
    arg0 = os.path.abspath(sys.argv[0])
    if arg0.endswith(".pyz"):
        return "pyz", Path(arg0), None
    return "source", Path(__file__).resolve().parent.parent, None


def _same(a, b):
    try:
        return os.path.samefile(str(a), str(b))
    except OSError:
        return False


def _copy_folder(src, dest):
    """Replace dest with a copy of src; a crash leaves the old or the new one in place."""
    new, old = dest.with_name(dest.name + ".new"), dest.with_name(dest.name + ".old")
    for leftover in (new, old):
        if leftover.exists():
            shutil.rmtree(str(leftover), ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(src), str(new), symlinks=True)       # PyInstaller's macOS folders have links
    if dest.exists():
        os.replace(str(dest), str(old))
    os.replace(str(new), str(dest))
    shutil.rmtree(str(old), ignore_errors=True)


def _same_bytes(a, b):
    try:
        return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _link(link, target, notes, shown):
    """bin/audiolink -> target: a symlink (frozen) or our launcher script (pyz, target is
    its text). Leaves anything else that's there alone, and says so."""
    link.parent.mkdir(parents=True, exist_ok=True)
    script = isinstance(target, str)
    if link.is_symlink():
        if not script and os.readlink(str(link)) == str(target):
            return
        link.unlink()
    elif link.exists():
        try:
            mine = MARK in link.read_text(errors="replace")[:500]
        except OSError:
            mine = False
        if not mine:
            notes.append("%s is something else; left it (audiolink is %s)" % (link, shown))
            return
        if script and link.read_text() == target:
            return
        link.unlink()
    if script:
        link.write_text(target)
        link.chmod(0o755)
    else:
        os.symlink(str(target), str(link))


def _path_note(folder, environ):
    dirs = [os.path.normcase(os.path.normpath(p)) for p in environ.get("PATH", "").split(os.pathsep) if p]
    if os.path.normcase(os.path.normpath(str(folder))) in dirs:
        return None
    if os.sep == "\\":
        return ("%s isn't on your PATH; to run plain `audiolink`, add it (Settings: search \"environment "
                "variables\" → Edit environment variables for your account → Path)" % folder)
    home = os.path.expanduser("~")
    shown = "$HOME" + str(folder)[len(home):] if str(folder).startswith(home + os.sep) else str(folder)
    profile = "~/.zprofile" if sys.platform == "darwin" else "~/.profile"
    return ("%s isn't on your PATH; to run plain `audiolink`: echo 'export PATH=\"%s:$PATH\"' >> %s, "
            "then open a new terminal" % (shown.replace("$HOME", "~", 1), shown, profile))


def install(runner, platform=sys.platform, home=None, environ=None, what=None, python=None):
    """Copy the running audiolink to its permanent place (if it isn't there) -> Installed."""
    environ = os.environ if environ is None else environ
    kind, src, exe = what or running()
    python = python or sys.executable
    dest, bindir = places(platform, home, environ)
    notes = []
    if kind == "source":
        return Installed([python, "-m", "audiolink"], env={"PYTHONPATH": str(src)},
                         notes=["Running from a source checkout (%s): not installing" % src])
    changed = False
    if kind == "frozen":
        if not _same(src, dest):
            _copy_folder(src, dest)
            changed = True
        target = dest / exe
        argv = [str(target)]
        if bindir:
            _link(bindir / "audiolink", target, notes, target)
    else:
        target = dest / "audiolink.pyz"
        if not _same(src, target) and not _same_bytes(src, target):
            dest.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".new")
            shutil.copyfile(str(src), str(tmp))
            os.replace(str(tmp), str(target))
            changed = True
        argv = [python, str(target)]
        if platform == "win32":
            launcher = dest / "audiolink.cmd"
            text = '@echo off\r\nREM %s\r\n%s %%*\r\n' % (MARK[2:], subprocess.list2cmdline(argv))
            if not launcher.exists() or launcher.read_text() != text:
                launcher.write_text(text)
        else:
            import shlex
            _link(bindir / "audiolink", "#!/bin/sh\n%s\nexec %s \"$@\"\n"
                  % (MARK, " ".join(shlex.quote(x) for x in argv)), notes, target)
    if changed and platform == "darwin":
        runner.run(["xattr", "-dr", "com.apple.quarantine", str(dest)])    # not code-signed yet
    note = _path_note(bindir or dest, environ)
    if note:
        notes.append(note)
    return Installed(argv, changed=changed, where=dest, notes=notes)


def here(python=None):
    """Installed for the audiolink running now, where it is (setup --no-install)."""
    kind, src, exe = running()
    python = python or sys.executable
    if kind == "frozen":
        return Installed([str(src / exe)], where=src)
    if kind == "pyz":
        return Installed([python, str(src)], where=src.parent)
    return Installed([python, "-m", "audiolink"], env={"PYTHONPATH": str(src)})
