#!/bin/sh
# Dev: quick end-to-end check of a running host daemon and the Pi behind it (after
# tools/hot.sh or tools/deploy.sh). No human needed. Exit status: number of failures.
cd "$(dirname "$0")/.."
PI=${PI:-root@10.55.0.2}
SSH="ssh -i keys/id_ed25519 -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR -o ConnectTimeout=5"
AL="python3 dist/audiolink.pyz"
fails=0
check() {   # check NAME COMMAND...: run it, show the outcome
    name=$1; shift
    if out=$("$@" 2>&1); then echo "ok    $name"; else echo "FAIL  $name"; echo "$out" | sed 's/^/      /'; fails=$((fails+1)); fi
}
check "link up, Wi-Fi status"   $AL wifi status
check "audio format"            $AL audio
check "setup network status"    $AL wifi ap
check "streams over the link (proxy -> internet)" \
      sh -c 'curl -s -m 20 -x http://127.0.0.1:47801 http://captive.apple.com/hotspot-detect.html | grep -q Success'
check "Pi daemon under its restart loop" \
      $SSH $PI 'ps | awk "\$4==\"python3\" && \$5 ~ /audiolink[.]pyz\$/ {d=1} /while :; do/ {l=1} END {exit !(d && l)}"'
check "no tracebacks in the Pi log" \
      $SSH $PI '! grep -B2 -A8 Traceback /var/log/audiolink.log'
exit $fails
