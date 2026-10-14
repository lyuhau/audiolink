#!/bin/sh
# The card's settings for audiolink (settings.txt on the card; config/pi.toml's
# [app] settings, the platform's README: Card settings) as `audiolink pi`
# arguments, one per line, for start-audiolink.sh to add after its own.
#   audiolink.relay         HOST:PORT[#PIN] or off   -> --relay=...
#   audiolink.expose_setup  on or off                -> --expose-setup
# The platform passes any one line of text: a value that isn't valid is logged
# (/var/log/settings.log, next to the platform's notes) and left out, so
# audiolink uses its default. Never passed on: an argument audiolink refuses
# would keep it from starting.
R=${USB_TEST_ROOT:-}  # tests: a fake root
APP_SETTING_RELAY= APP_SETTING_EXPOSE_SETUP=
for f in "$R/etc/usb-gadget/settings.defaults" "$R/run/usb-gadget/settings.env"; do
    [ -f "$f" ] && . "$f"
done
bad() { echo "settings: audiolink.$1 = $2 isn't $3; using audiolink's default" | tee -a "$R/var/log/settings.log" >&2; }

if [ -n "$APP_SETTING_RELAY" ]; then
    if printf '%s' "$APP_SETTING_RELAY" | grep -Eqx \
        'off|([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?(#[0-9A-Fa-f]{64})?'; then
        echo "--relay=$APP_SETTING_RELAY"
    else
        bad relay "$APP_SETTING_RELAY" "HOST:PORT, HOST:PORT#PIN or off"
    fi
fi
case "$APP_SETTING_EXPOSE_SETUP" in
on|yes|true|1) echo "--expose-setup" ;;
""|off|no|false|0) ;;
*) bad expose_setup "$APP_SETTING_EXPOSE_SETUP" "on or off" ;;
esac
exit 0
