#!/bin/sh
# audiolink on the usb platform: its [app] run (config/pi.toml). The platform
# starts this once, after the USB device is up and the card is mounted, with
# /etc/usb-gadget/app.env in the environment and output in $APP_LOG.
# audiolink owns Wi-Fi and the clock ([app] owns): both are started here.
set -x
HERE=$(dirname "$0")

# ---- dev images (ECM function): usb0 is the Pi at 10.55.0.2; DHCP hands the host 10.55.0.1
if [ -e /sys/class/net/usb0 ]; then
    ip addr add 10.55.0.2/24 dev usb0
    ip link set usb0 up
    cat > /run/udhcpd-usb0.conf <<EOF
start 10.55.0.1
end 10.55.0.1
interface usb0
max_leases 1
option subnet 255.255.255.0
option lease 86400
lease_file /run/udhcpd-usb0.leases
pidfile /run/udhcpd-usb0.pid
EOF
    touch /run/udhcpd-usb0.leases
    udhcpd /run/udhcpd-usb0.conf
fi

# ---- audiolink (before Wi-Fi: the audio link doesn't need it)
"$HERE/start-audiolink.sh" $(grep -v '^#' /etc/audiolink.args)

# ---- mDNS: audiopi.local (avahi follows wlan0 as it comes up)
mkdir -p /run/avahi-daemon
avahi-daemon -D --no-drop-root --no-chroot

# ---- Wi-Fi: wpa_supplicant with no networks of its own; audiolink loads the saved ones
(
    for i in $(seq 1 50); do [ -e /sys/class/net/wlan0 ] && break; sleep 0.2; done
    ip link set wlan0 up
    wpa_supplicant -B -P /run/wpa_supplicant.pid -i wlan0 -c /etc/wpa_supplicant/wpa_supplicant.conf
    udhcpc -i wlan0 -b -s /usr/share/udhcpc/default.script -x hostname:"$(hostname)"
) >/var/log/wifi-up.log 2>&1

# ---- the clock (no RTC: it starts at 1970), retried until there is internet
( until ntpd -n -q -p pool.ntp.org; do sleep 60; done; ntpd -p pool.ntp.org ) >/var/log/ntp.log 2>&1 &

echo "audiolink started"
