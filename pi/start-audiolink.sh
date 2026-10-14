#!/bin/sh
# (Re)start the Pi-side audiolink daemon, in a loop that restarts it whenever it
# exits (a crash must never leave the Pi unreachable). Args go to `audiolink pi`.
# Restarts back off (2 s, doubling to 60 s) while it keeps dying within a minute.
# Log: /var/log/audiolink.log; earlier runs' (each ending with how it ended):
# audiolink.log.1 .. .3
#
# Code: the built-in copy is audiolink.pyz next to this script (the image's: /opt/audiolink/).
# An update on the card (`audiolink update`) runs instead of the built-in copy,
# except while the reset pins are shorted (services/button.py), and once it has died
# within a minute 3 times in a row: then it's set aside (.bad) for the built-in one.
# Exit status 75 is a restart the daemon asked for (an update): no back-off.
# The card's settings (card-settings.sh: the relay...) come after the args, so they win.
PIDF=/run/audiolink.pid
LOG=/var/log/audiolink.log
HERE=$(cd "$(dirname "$0")" && pwd)
BUILT_IN=$HERE/audiolink.pyz
set -f   # one argument per line, no globbing ([::1]:443)
IFS='
'
[ -f "$HERE/card-settings.sh" ] && set -- "$@" $(sh "$HERE/card-settings.sh")
unset IFS
set +f
# The loop and the daemon share a process group: stop both.
[ -f $PIDF ] && kill -TERM -"$(cat $PIDF)" 2>/dev/null && sleep 1
killall -q aplay arecord
setsid sh -c '
    LOG='$LOG'
    BUILT_IN='$BUILT_IN'
    UPDATE=/var/lib/audiolink/audiolink.pyz
    pins_shorted() {        # GPIO21 to GND; open reads 1 (pull-up from config.txt)
        for chip in /sys/class/gpio/gpiochip*; do
            grep -q "^pinctrl-bcm" $chip/label 2>/dev/null || continue
            n=$(( $(cat $chip/base) + 21 ))
            [ -e /sys/class/gpio/gpio$n ] || echo $n > /sys/class/gpio/export 2>/dev/null
            [ "$(cat /sys/class/gpio/gpio$n/value 2>/dev/null)" = 0 ]
            return
        done
        return 1
    }
    delay=2
    fails=0
    while :; do
        [ -f $LOG.2 ] && mv $LOG.2 $LOG.3
        [ -f $LOG.1 ] && mv $LOG.1 $LOG.2
        [ -f $LOG ] && mv $LOG $LOG.1
        PYZ=$BUILT_IN
        if [ -f $UPDATE ] && [ ! -f $UPDATE.bad ]; then
            if pins_shorted; then
                echo "reset pins shorted: running the built-in code" > $LOG
            else
                PYZ=$UPDATE
            fi
        fi
        started=$(cut -d. -f1 /proc/uptime)
        python3 $PYZ pi "$@" >> $LOG 2>&1 < /dev/null
        status=$?
        killall -q aplay arecord
        if [ $status = 75 ]; then
            delay=2
        elif [ $(( $(cut -d. -f1 /proc/uptime) - started )) -gt 60 ]; then
            delay=2
            fails=0
        elif [ $PYZ = $UPDATE ]; then
            fails=$(( fails + 1 ))
            if [ $fails -ge 3 ]; then
                touch $UPDATE.bad
                echo "the update died within a minute 3 times; set aside, running the built-in code" >> $LOG
                fails=0
            fi
        fi
        echo "audiolink exited with status $status; restarting in ${delay}s" >> $LOG
        sleep $delay
        delay=$(( delay * 2 > 60 ? 60 : delay * 2 ))
    done' audiolink "$@" &
echo $! > $PIDF
