#!/bin/sh
# (Re)start the Pi-side audiolink daemon, in a loop that restarts it whenever it
# exits (a crash must never leave the Pi unreachable). Args go to `audiolink pi`.
# Restarts back off (2 s, doubling to 60 s) while it keeps dying within a minute.
# Log: /var/log/audiolink.log; earlier runs' (each ending with how it ended):
# audiolink.log.1 .. .3
PIDF=/run/audiolink.pid
LOG=/var/log/audiolink.log
# The loop and the daemon share a process group: stop both.
[ -f $PIDF ] && kill -TERM -"$(cat $PIDF)" 2>/dev/null && sleep 1
killall -q aplay arecord
setsid sh -c '
    LOG='$LOG'
    delay=2
    while :; do
        [ -f $LOG.2 ] && mv $LOG.2 $LOG.3
        [ -f $LOG.1 ] && mv $LOG.1 $LOG.2
        [ -f $LOG ] && mv $LOG $LOG.1
        started=$(cut -d. -f1 /proc/uptime)
        python3 /root/audiolink.pyz pi "$@" > $LOG 2>&1 < /dev/null
        status=$?
        killall -q aplay arecord
        if [ $(( $(cut -d. -f1 /proc/uptime) - started )) -gt 60 ]; then delay=2; fi
        echo "audiolink exited with status $status; restarting in ${delay}s" >> $LOG
        sleep $delay
        delay=$(( delay * 2 > 60 ? 60 : delay * 2 ))
    done' audiolink "$@" &
echo $! > $PIDF
