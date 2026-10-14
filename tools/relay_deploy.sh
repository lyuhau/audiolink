#!/bin/sh
# Put the current code on the relay server (an Ubuntu box reachable as root) and
# (re)start it. Safe to run again: restarts (dropping every relayed session) only
# when the code, certificate or systemd unit changed; build_pyz.py is reproducible.
#   RELAY=root@relay.lyuhau.com tools/relay_deploy.sh
set -e
cd "$(dirname "$0")/.."
RELAY=${RELAY:-root@relay.lyuhau.com}
SSH="ssh -i keys/relay_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR"
python3 build_pyz.py >/dev/null
# Everything the service runs from: a change to any of it restarts it.
STATE='cat /opt/audiolink/*.pyz /etc/audiolink/relay.* /etc/systemd/system/audiolink-relay.service 2>/dev/null | sha256sum'
before=$($SSH $RELAY "$STATE")
$SSH $RELAY 'mkdir -p /opt/audiolink /etc/audiolink && chmod 700 /etc/audiolink'
$SSH $RELAY 'cat > /opt/audiolink/audiolink.pyz' < dist/audiolink.pyz
$SSH $RELAY 'cat > /etc/audiolink/relay.crt' < keys/relay.crt
$SSH $RELAY 'umask 077; cat > /etc/audiolink/relay.key' < keys/relay.key
$SSH $RELAY 'cat > /etc/systemd/system/audiolink-relay.service' <<'UNIT'
[Unit]
Description=audiolink relay
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/audiolink/audiolink.pyz relay --listen 0.0.0.0:443 --cert ${CREDENTIALS_DIRECTORY}/relay.crt --key ${CREDENTIALS_DIRECTORY}/relay.key
LoadCredential=relay.crt:/etc/audiolink/relay.crt
LoadCredential=relay.key:/etc/audiolink/relay.key
Restart=always
RestartSec=2
DynamicUser=yes
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
LimitNOFILE=65536
TasksMax=4096
MemoryMax=300M

[Install]
WantedBy=multi-user.target
UNIT
after=$($SSH $RELAY "$STATE")
$SSH $RELAY 'command -v ufw >/dev/null && { ufw allow 22/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw --force enable >/dev/null; }
             systemctl daemon-reload; systemctl enable -q audiolink-relay'
if [ "$before" != "$after" ] || ! $SSH $RELAY 'systemctl -q is-active audiolink-relay'; then
    echo "restarting the relay (relayed sessions drop)"
    $SSH $RELAY 'systemctl restart audiolink-relay'
fi
sleep 2
$SSH $RELAY 'systemctl -q is-active audiolink-relay && python3 /opt/audiolink/audiolink.pyz relay --listen 127.0.0.1:443 --status' \
    || { $SSH $RELAY 'journalctl -u audiolink-relay -n 20 --no-pager'; exit 1; }
