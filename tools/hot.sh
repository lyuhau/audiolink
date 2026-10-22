#!/bin/sh
# Dev: run the current code on the Pi in seconds. Builds the .pyz, copies it (and
# the start script) over USB ethernet and restarts the daemon; no reboot. Lost at
# the next boot: audiolink update --code keeps code on the card; other pi/ changes
# need a card (make card; audiolink update build/card-pi). Over Wi-Fi:
# PI=root@ADDRESS tools/hot.sh
set -e
PYTHON=${PYTHON:-python3}
cd "$(dirname "$0")/.."
PI=${PI:-root@10.55.0.2}
SSH="ssh -i keys/id_ed25519 -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR -o ConnectTimeout=5"
. tools/pi_code.sh
"$PYTHON" build_pyz.py >/dev/null
pi_reachable
# Restart at the baseline audio format: a daemon that dies mid-stream at a high rate
# can leave macOS unable to use that format until the Pi is replugged.
"$PYTHON" dist/audiolink.pyz audio set baseline >/dev/null 2>&1 || true
D=/opt/audiolink         # the built-in code (config/pi-release.toml's [app.files])
$SSH $PI "cat > $D/audiolink.pyz.new && mv $D/audiolink.pyz.new $D/audiolink.pyz" < dist/audiolink.pyz
$SSH $PI "cat > $D/start-audiolink.sh && chmod +x $D/start-audiolink.sh" < pi/start-audiolink.sh
$SSH $PI "cat > $D/card-settings.sh" < pi/card-settings.sh
# Detached: a restart can re-present the USB gadget (audio format back to the baseline),
# which drops USB ethernet, and this ssh session, for a few seconds.
$SSH $PI "($D/start-audiolink.sh \$(grep -v '^#' /etc/audiolink.args)) >/dev/null 2>&1 &"
if ! pi_wait_up 60; then
    echo "daemon did not come up:" >&2; $SSH $PI 'tail -20 /var/log/audiolink.log' >&2; exit 1
fi
$SSH $PI 'grep -m1 " pi up " /var/log/audiolink.log'
pi_code_check
