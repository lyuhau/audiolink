#!/bin/sh
# Dev: run the current code on the Pi in seconds. Builds the .pyz, copies it (and
# the start script) over USB ethernet and restarts the daemon; no reboot. Lost at
# the next boot: tools/build_card.sh + tools/deploy.sh put it on the SD card (needed
# for other pi/ changes).
set -e
cd "$(dirname "$0")/.."
PI=${PI:-root@10.55.0.2}
SSH="ssh -i keys/id_ed25519 -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR"
python3 build_pyz.py >/dev/null
# Restart at the baseline audio format: a daemon that dies mid-stream at a high rate
# can leave macOS unable to use that format until the Pi is replugged.
python3 dist/audiolink.pyz audio set baseline >/dev/null 2>&1 || true
# The built-in code: /opt/audiolink on the usb platform, /root on the older image.
D=$($SSH $PI '[ -d /opt/audiolink ] && echo /opt/audiolink || echo /root')
$SSH $PI "cat > $D/audiolink.pyz.new && mv $D/audiolink.pyz.new $D/audiolink.pyz" < dist/audiolink.pyz
$SSH $PI "cat > $D/start-audiolink.sh && chmod +x $D/start-audiolink.sh" < pi/start-audiolink.sh
# Detached: a restart can re-present the USB gadget (audio format back to the baseline),
# which drops USB ethernet, and this ssh session, for a few seconds.
$SSH $PI "($D/start-audiolink.sh \$(grep -v '^#' /etc/audiolink.args)) >/dev/null 2>&1 &"
for i in $(seq 1 30); do
    sleep 2
    $SSH -o ConnectTimeout=3 $PI 'grep -m1 " pi up " /var/log/audiolink.log' 2>/dev/null && exit 0
done
echo "daemon did not come up:"; $SSH $PI 'tail -20 /var/log/audiolink.log'; exit 1
