#!/bin/sh
# Put the current code on the relay server (an Ubuntu box reachable as root) and
# (re)start it. Safe to run again: restarts (dropping every relayed session) only
# when the code, certificate or systemd unit changed; build_pyz.py is reproducible.
#   RELAY=root@relay.example.com tools/relay_deploy.sh    (default: audiolink/relay/__init__.py DEFAULT's host)
# It listens on DEFAULT's port (443 if it names none); RELAY_PORT=N for another.
set -e
case "$1" in -h|--help) sed -n '2,6s/^# \{0,1\}//p' "$0"; exit 0 ;; esac
[ $# = 0 ] || { echo "usage: [RELAY=root@HOST] [RELAY_PORT=N] tools/relay_deploy.sh" >&2; exit 2; }
cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-python3}
set -- $("$PYTHON" -c 'from audiolink.relay import DEFAULT, parse_addr; print(*parse_addr(DEFAULT.partition("#")[0]))')
[ -n "$2" ] || exit 1
if [ -z "$RELAY" ]; then
    [ "$1" != off ] || { echo "audiolink/relay/__init__.py's DEFAULT is \"off\" (no relay): RELAY=root@HOST to deploy one anyway" >&2; exit 1; }
    RELAY=root@$1
fi
PORT=${RELAY_PORT:-$2}
case "$PORT" in ""|*[!0-9]*) echo "RELAY_PORT: a port number" >&2; exit 2 ;; esac
SSH="ssh -i keys/relay_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR"
"$PYTHON" build_pyz.py >/dev/null
# Everything the service runs from: a change to any of it restarts it.
STATE='cat /opt/audiolink/*.pyz /etc/audiolink/relay.* /etc/systemd/system/audiolink-relay.service 2>/dev/null | sha256sum'
before=$($SSH $RELAY "$STATE")
$SSH $RELAY 'mkdir -p /opt/audiolink /etc/audiolink && chmod 700 /etc/audiolink'
$SSH $RELAY 'cat > /opt/audiolink/audiolink.pyz' < dist/audiolink.pyz
$SSH $RELAY 'cat > /etc/audiolink/relay.crt' < keys/relay.crt
$SSH $RELAY 'umask 077; cat > /etc/audiolink/relay.key' < keys/relay.key
$SSH $RELAY 'cat > /etc/systemd/system/audiolink-relay.service' <<UNIT
[Unit]
Description=audiolink relay
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/audiolink/audiolink.pyz relay --listen 0.0.0.0:$PORT --cert \${CREDENTIALS_DIRECTORY}/relay.crt --key \${CREDENTIALS_DIRECTORY}/relay.key
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
$SSH $RELAY 'command -v ufw >/dev/null && { ufw allow 22/tcp >/dev/null; ufw allow '$PORT'/tcp >/dev/null; ufw --force enable >/dev/null; }
             systemctl daemon-reload; systemctl enable -q audiolink-relay'
if [ "$before" != "$after" ] || ! $SSH $RELAY 'systemctl -q is-active audiolink-relay'; then
    echo "restarting the relay (relayed sessions drop)"
    $SSH $RELAY 'systemctl restart audiolink-relay'
fi
sleep 2
$SSH $RELAY 'systemctl -q is-active audiolink-relay && python3 /opt/audiolink/audiolink.pyz relay --listen 127.0.0.1:'$PORT' --status' \
    || { $SSH $RELAY 'journalctl -u audiolink-relay -n 20 --no-pager'; exit 1; }
