#!/bin/sh
# (Re)start the Pi-side audiolink daemon. Args are passed to `audiolink pi`.
PIDF=/run/audiolink.pid
[ -f $PIDF ] && kill "$(cat $PIDF)" 2>/dev/null && sleep 0.5
killall -q aplay arecord
setsid python3 /root/audiolink.pyz pi "$@" > /var/log/audiolink.log 2>&1 < /dev/null &
echo $! > $PIDF
