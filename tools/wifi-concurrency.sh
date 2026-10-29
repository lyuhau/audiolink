#!/bin/sh
# Wi-Fi concurrency experiments on the Pi (dev image; copy it over and run as root).
#   ap_up / ap_down        a second interface (uap0) running an open AP "audiopi-test" on wlan0's channel
#   sta2_up SSID           a second station (wlan1) on another network (asks for its password)
#   sta2_start / sta2_down reuse the saved station config / remove it
#   savekey SSID           store another network's derived key (asks for its password)
#   drag N                 N cycles: move wlan0 to the saved network and back, AP up
#   monitor                log wlan0, AP and wlan1 health every 2 s to /tmp/exp/mon.log
#   report [LINE]          summarize the log from LINE on
# Results (2026-10-07, BCM43430): AP + station on one channel is solid; two stations on
# two channels stay associated but lose 30-50% of pings (the radio time-slices); moving
# the station to another channel drags the AP along silently (hostapd isn't told), once
# with an SDIO error burst (none in 30 later moves). After a drag, clients stay
# associated but receive nothing until hostapd restarts; phones then return to their
# usual Wi-Fi rather than follow the AP to the new channel. Don't rely on the drag:
# restart the AP on the new channel, and expect clients to have left.
# Note: udhcpc on wlan1 overwrites /etc/resolv.conf.
case "$1" in
ap_up|ap_down|sta2_up|sta2_start|sta2_down|savekey|drag|monitor|report) ;;
-h|--help) sed -n '2,17s/^# \{0,1\}//p' "$0"; exit 0 ;;
*) sed -n '2,9s/^# \{0,1\}//p' "$0" >&2; exit 2 ;;
esac
L=/tmp/exp
mkdir -p $L
case "$1" in
ap_up)
    CH=$(iw dev wlan0 info | sed -n 's/.*channel \([0-9]*\).*/\1/p'); CH=${CH:-6}
    iw dev uap0 info >/dev/null 2>&1 || iw dev wlan0 interface add uap0 type __ap
    ip link set uap0 address 2e:cf:67:e5:f8:b8
    ip addr flush dev uap0; ip addr add 192.168.5.1/24 dev uap0; ip link set uap0 up
    cat > $L/hostapd.conf <<C
interface=uap0
driver=nl80211
ctrl_interface=/var/run/hostapd-exp
ssid=audiopi-test
hw_mode=g
channel=$CH
auth_algs=1
wmm_enabled=1
C
    hostapd -B -P $L/hostapd.pid -f $L/hostapd.log $L/hostapd.conf
    cat > $L/udhcpd.conf <<C
start 192.168.5.10
end 192.168.5.50
interface uap0
lease_file $L/udhcpd.leases
option subnet 255.255.255.0
option lease 3600
C
    touch $L/udhcpd.leases; udhcpd -S $L/udhcpd.conf
    sleep 2; hostapd_cli -p /var/run/hostapd-exp -i uap0 status | grep -E "^state|^channel|^ssid"
    ;;
ap_down)
    [ -f $L/hostapd.pid ] && kill $(cat $L/hostapd.pid); pkill -f "udhcpd -S $L"; sleep 1
    iw dev uap0 del 2>/dev/null; echo ap down
    ;;
sta2_start)
    iw dev wlan1 info >/dev/null 2>&1 || iw dev wlan0 interface add wlan1 type managed
    ip link set wlan1 address 2e:cf:67:e5:f8:b9; ip link set wlan1 up
    grep -q ctrl_interface $L/sta2.conf || sed -i "1i ctrl_interface=/var/run/wpa_supplicant" $L/sta2.conf
    wpa_supplicant -B -i wlan1 -c $L/sta2.conf -P $L/sta2.pid
    for i in $(seq 1 20); do wpa_cli -i wlan1 status 2>/dev/null | grep -q wpa_state=COMPLETED && break; sleep 1; done
    wpa_cli -i wlan1 status | grep -E "wpa_state|freq|^ssid"
    udhcpc -i wlan1 -b -p $L/udhcpc2.pid -s /usr/share/udhcpc/default.script -x hostname:audiopi >/dev/null 2>&1
    ;;
sta2_up)
    iw dev wlan1 info >/dev/null 2>&1 || iw dev wlan0 interface add wlan1 type managed
    ip link set wlan1 address 2e:cf:67:e5:f8:b9; ip link set wlan1 up
    printf "password for %s (empty = open network): " "$2"; stty -echo 2>/dev/null; read PW; stty echo 2>/dev/null; echo
    if [ -n "$PW" ]; then wpa_passphrase "$2" "$PW" | grep -v "#psk" > $L/sta2.conf
    else printf 'network={\n ssid="%s"\n key_mgmt=NONE\n}\n' "$2" > $L/sta2.conf; fi
    unset PW; sed -i "1i ctrl_interface=/var/run/wpa_supplicant" $L/sta2.conf; chmod 600 $L/sta2.conf
    wpa_supplicant -B -i wlan1 -c $L/sta2.conf -P $L/sta2.pid
    for i in $(seq 1 20); do wpa_cli -i wlan1 status 2>/dev/null | grep -q wpa_state=COMPLETED && break; sleep 1; done
    wpa_cli -i wlan1 status | grep -E "wpa_state|freq|^ssid"
    udhcpc -i wlan1 -b -p $L/udhcpc2.pid -s /usr/share/udhcpc/default.script -x hostname:audiopi >/dev/null 2>&1
    ;;
sta2_down)
    [ -f $L/udhcpc2.pid ] && kill $(cat $L/udhcpc2.pid); [ -f $L/sta2.pid ] && kill $(cat $L/sta2.pid); sleep 1
    iw dev wlan1 del 2>/dev/null; echo sta2 down
    ;;
savekey)
    # store a network's derived key (not the password) for the drag tests
    printf "password for %s: " "$2"; stty -echo 2>/dev/null; read PW; stty echo 2>/dev/null; echo
    wpa_passphrase "$2" "$PW" | sed -n 's/^\s*psk=\([0-9a-f]*\)$/\1/p' > $L/hot.psk; unset PW
    echo "$2" > $L/hot.ssid; chmod 600 $L/hot.psk; [ -s $L/hot.psk ] && echo saved || echo FAILED
    ;;
drag)
    # $2 cycles: move wlan0 to the saved network (another channel) and back, with the AP up
    N=${2:-20}; LOG=$L/drag.log; : > $LOG
    errs() { dmesg | grep -cE "brcmf_sdio|CMD53|inhibit|brcmf_c_|fw error|firmware has halted"; }
    waitc() { i=0; while [ $i -lt 40 ]; do wpa_cli -i wlan0 status | grep -q wpa_state=COMPLETED && wpa_cli -i wlan0 status | grep -q "^freq=$1" && { echo $i; return; }; sleep 0.5; i=$((i+1)); done; echo FAIL; }
    apch() { iw dev uap0 info 2>/dev/null | sed -n 's/.*channel \([0-9]*\).*/\1/p'; }
    apst() { hostapd_cli -p /var/run/hostapd-exp -i uap0 status 2>/dev/null | sed -n 's/^state=//p'; }
    HOME_ID=$(wpa_cli -i wlan0 list_networks | awk '/CURRENT/{print $1}')
    H=$(wpa_cli -i wlan0 add_network | tail -1)
    wpa_cli -i wlan0 set_network $H ssid "\"$(cat $L/hot.ssid)\"" >/dev/null
    wpa_cli -i wlan0 set_network $H psk $(cat $L/hot.psk) >/dev/null
    wpa_cli -i wlan0 disable_network $H >/dev/null
    E0=$(errs); echo "start: errors=$E0 ap=ch$(apch) $(apst)" >> $LOG
    c=1; while [ $c -le $N ]; do
        wpa_cli -i wlan0 select_network $H >/dev/null; t1=$(waitc 2462); sleep 4; a1=$(apch); s1=$(apst)
        wpa_cli -i wlan0 select_network $HOME_ID >/dev/null; t2=$(waitc 2412); sleep 4; a2=$(apch); s2=$(apst)
        echo "cycle $c: to-hotspot ${t1}x0.5s ap=ch$a1 $s1 | home ${t2}x0.5s ap=ch$a2 $s2 | new errors=$(( $(errs) - E0 ))" >> $LOG
        c=$((c+1))
    done
    wpa_cli -i wlan0 remove_network $H >/dev/null; wpa_cli -i wlan0 enable_network all >/dev/null
    echo done >> $LOG
    ;;
monitor)
    # one line every 2 s: time | wlan0 state/freq, gw ping | uap0 state/chan, stations | phone ping | wlan1
    [ -f $L/mon.pid ] && kill $(cat $L/mon.pid) 2>/dev/null
    (
    while :; do
        t=$(cut -d' ' -f1 /proc/uptime)
        s0=$(wpa_cli -i wlan0 status 2>/dev/null | sed -n 's/^wpa_state=//p;s/^freq=/f/p' | tr '\n' ' ')
        g0=$(ping -c1 -W1 -I wlan0 192.168.1.1 >/dev/null 2>&1 && echo ok || echo LOSS)
        if iw dev uap0 info >/dev/null 2>&1; then
            ap=$(hostapd_cli -p /var/run/hostapd-exp -i uap0 status 2>/dev/null | sed -n 's/^state=//p;s/^channel=/ch/p' | tr '\n' ' ')
            ns=$(iw dev uap0 station dump | grep -c ^Station)
            ph=$(awk 'NR>0{print}' /proc/net/arp | grep uap0 | awk '{print $1}' | head -1)
            pp=-; [ -n "$ph" ] && { ping -c1 -W1 -I uap0 $ph >/dev/null 2>&1 && pp=ok || pp=LOSS; }
            apinfo="ap: $ap sta=$ns phone=$pp"
        else apinfo="ap: off"; fi
        if iw dev wlan1 info >/dev/null 2>&1; then
            s1=$(wpa_cli -i wlan1 -p /var/run/wpa_supplicant status 2>/dev/null | sed -n 's/^wpa_state=//p;s/^freq=/f/p;s/^ip_address=//p' | tr '\n' ' ')
            g1=$(ip route show dev wlan1 | sed -n 's/default via \([0-9.]*\).*/\1/p' | head -1)
            p1=-; [ -n "$g1" ] && { ping -c1 -W1 -I wlan1 $g1 >/dev/null 2>&1 && p1=ok || p1=LOSS; }
            sta2="wlan1: $s1 gw=$p1"
        else sta2=""; fi
        echo "$t | wlan0: $s0 gw=$g0 | $apinfo | $sta2"
        sleep 2
    done ) >> $L/mon.log 2>&1 &
    echo $! > $L/mon.pid; echo monitoring
    ;;
report)
    # summary of the monitor log since line $2
    tail -n +${2:-1} $L/mon.log | awk -F'|' '
      {n++; if ($2 ~ /LOSS/) g++; if ($2 !~ /COMPLETED/) d++; if ($3 ~ /phone=LOSS/) pl++; if ($3 ~ /phone=ok/) po++;
       if ($3 ~ /ap: off/ ) {} else if ($3 !~ /ENABLED/) ad++; if ($4 ~ /gw=LOSS/) l1++; if ($4 ~ /gw=ok/) o1++}
      END {printf "samples=%d  wlan0: not-connected=%d gw-loss=%d | ap: not-enabled=%d phone ok=%d loss=%d | wlan1 gw ok=%d loss=%d\n", n,d,g,ad,po,pl,o1,l1}'
    ;;
esac
