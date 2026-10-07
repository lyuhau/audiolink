#!/bin/sh
# Dev: run the current code on the Pi in seconds. Builds the .pyz, copies it (and
# the start script) over USB ethernet and restarts the daemon; no reboot. Lost at
# the next boot: ./deploy_card.sh puts it on the SD card (needed for pi/ changes).
set -e
cd "$(dirname "$0")/.."
PI=${PI:-root@10.55.0.2}
SSH="ssh -i keys/id_ed25519 -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR"
python3 build_pyz.py >/dev/null
$SSH $PI 'cat > /root/audiolink.pyz.new && mv /root/audiolink.pyz.new /root/audiolink.pyz' < dist/audiolink.pyz
$SSH $PI 'cat > /root/start-audiolink.sh && chmod +x /root/start-audiolink.sh' < pi/start-audiolink.sh
# Detached: a restart can re-present the USB gadget (audio format back to the baseline),
# which drops USB ethernet, and this ssh session, for a few seconds.
$SSH $PI '(/root/start-audiolink.sh $(grep -v "^#" /etc/audiolink.args)) >/dev/null 2>&1 &'
for i in $(seq 1 30); do
    sleep 2
    $SSH -o ConnectTimeout=3 $PI 'grep -m1 " pi up " /var/log/audiolink.log' 2>/dev/null && exit 0
done
echo "daemon did not come up:"; $SSH $PI 'tail -20 /var/log/audiolink.log'; exit 1
