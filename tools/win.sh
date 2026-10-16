#!/bin/sh
# Dev on WSL, run on Windows. Copies the working tree to a Windows folder (Windows
# Python reading \\wsl$ is slow and flaky) and runs it there with the Windows uv
# (scoop install uv), so audio, ssh.exe, Task Scheduler and %APPDATA% are the real ones.
#   tools/win.sh test [unittest args]   the offline tests
#   tools/win.sh build                  build_bin.py; the zip comes back to dist/
#   tools/win.sh run ARGS...            python -m audiolink ARGS
#   tools/win.sh sync                   copy only
# Windows folder: AUDIOLINK_WIN_DIR (default %USERPROFILE%\dev\audiolink-win).
set -e
cd "$(dirname "$0")/.."
WINHOME=$(wslpath "$(cmd.exe /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r')")
DIR=${AUDIOLINK_WIN_DIR:-$WINHOME/dev/audiolink-win}
UV=$WINHOME/scoop/shims/uv.exe
[ -x "$UV" ] || UV=$(command -v uv.exe) || { echo "no uv.exe on Windows: scoop install uv" >&2; exit 1; }
PY="--no-project --python 3.12"

mkdir -p "$DIR"
rsync -a --delete --exclude .git --exclude dist --exclude build --exclude out \
    --exclude .venv --exclude __pycache__ --exclude keys --exclude wifi.conf ./ "$DIR/"
cd "$DIR"

case "${1:-}" in
    sync) ;;
    test) shift; exec "$UV" run $PY python -m unittest discover -s tests -t . "$@" ;;
    build)
        "$UV" run $PY --with pyinstaller python build_bin.py
        mkdir -p "$OLDPWD/dist" && cp dist/audiolink-windows-x64.zip "$OLDPWD/dist/" ;;
    run) shift; exec "$UV" run $PY python -m audiolink "$@" ;;
    *) sed -n '2,9p' "$OLDPWD/tools/win.sh" >&2; exit 2 ;;
esac
