#!/bin/sh
# Before pushing audiolink: is the pi-gadget-kit commit platform/ is pinned to on
# GitHub? If not, a fresh clone (and CI, and the release workflow) can't fetch it:
# push pi-gadget-kit first. Fetches platform/'s origin, then checks its branches.
#   tools/check_push.sh [REF]   (default: HEAD, the commit you'd push)
set -e
case "$1" in -h|--help) sed -n '2,5s/^# \{0,1\}//p' "$0"; exit 0 ;; esac
[ $# -le 1 ] || { echo "usage: tools/check_push.sh [REF]" >&2; exit 2; }
cd "$(dirname "$0")/.."
PIN=$(git rev-parse "${1:-HEAD}:platform")
[ -e platform/.git ] || { echo "platform/ isn't checked out: git submodule update --init" >&2; exit 1; }
git -C platform fetch -q origin
if [ -n "$(git -C platform branch -r --contains "$PIN" 2>/dev/null)" ]; then
    echo "ok: platform/ is pinned to $PIN, on $(git -C platform remote get-url origin)"
else
    echo "platform/ is pinned to $PIN, which isn't on $(git -C platform remote get-url origin):" >&2
    echo "push pi-gadget-kit first (git -C platform push origin HEAD:main, or from its own checkout)" >&2
    exit 1
fi
