# What each machine stores

What each machine keeps, when the data is made, and when it's used. First as it is now,
then what the client-host model (reaching a computer, not a Pi) would add.

Roles: the **Pi**; the **host** (the computer the Pi is plugged into, running
`audiolink host`); a **client** (any computer that runs `ssh NAME`; a host is usually one
too); the **relay**; the **DHT**.

## How ssh stays unaware

ssh asks audiolink two questions and does the rest itself (crypto, host key checks,
login):

- `ProxyCommand audiolink connect NAME %p`: "give me a byte stream to NAME". ssh never
  sees an address, so `HostName` and `Port` in the block are only labels. Finding a
  route (the Pi's LAN address, the relay, the DHT) is audiolink's job.
- `KnownHostsCommand` (OpenSSH 8.5+; not used yet): "which host keys should NAME
  have?". Only needed where NAME doesn't always mean the same computer (see Pi entries
  below).
- `HostKeyAlias` (not used yet): the name ssh files the host key under in known_hosts,
  in place of `[NAME]:PORT`.

## Folders

| | macOS | Windows | Linux |
|---|---|---|---|
| config | ~/Library/Application Support/audiolink | %APPDATA%\audiolink | ~/.config/audiolink |
| cache | ~/Library/Caches/audiolink | %LOCALAPPDATA%\audiolink | ~/.cache/audiolink |

`AUDIOLINK_CONFIG_DIR` moves config, not cache. The Pi keeps state in
`/var/lib/audiolink` (the card's `audiolink/` folder; in RAM without a card), as two
alternating slots per file (`NAME.a`, `NAME.b`).

## Now

### Pi

| What | Where | Made | Used |
|---|---|---|---|
| Identity: ed25519 seed (private key), read secret, name, claimed, discoverable | identity.json | first start (name from the board serial); changed by claim, rename, rotate-secret, restore from the host's copy, reset pins | every start; the key signs relay registration and DHT records and makes the TLS certificate; the secret proves "paired" requests and seals the DHT record |
| Wi-Fi networks: ssid, security, WPA2 psk (derived) or WPA3 password (plain), hidden, last used | networks.json | a join that gets an address; `audiolink wifi` from the host or remotely | every start, loaded into wpa_supplicant |
| Debug switch: mode, allowed keys, expiry | debug.json (only if the host asks to keep it), else /run (tmpfs) | `debug.set` from the host | every start; each debug-level request |
| Code update | audiolink.pyz (+ .bad after 3 quick crashes) | `audiolink update` | start-audiolink.sh at each start |
| Card settings: relay, expose_setup | settings.txt (read only) | written by hand or by setup's card step | card-settings.sh at each start |
| Logs | /var/log/audiolink.log (+3 rotated) | always | reading by hand, `audiolink logs` |

Not stored: pairing codes (memory, one use, 5 min), the TLS certificate (made from the
seed when needed), **any list of clients**. "Paired" means "can prove the read secret".
The only way to cut a client off is to rotate the secret, which cuts off all of them.

### Host

Everything a client keeps, plus:

| What | Where | Made | Used |
|---|---|---|---|
| Host settings: expose, forward, allow, control_port, audio devices, ports | config/host.json | `audiolink setup` (host part) | `audiolink host` at start (flags win) |
| Custody: a copy of the Pi's whole identity, **private seed included** | `custody` in config/pis.json | adding the Pi over USB; rotate-secret; refreshed on each USB hello, while the Pi has no card | restoring a Pi that comes up with a fresh identity (no card, new card) |
| Audio format per Pi serial | config/audio_formats.json | format negotiation | the next negotiation |
| Autostart | LaunchAgent plist / scheduled task / systemd user unit | `setup --autostart` | login |

### Client

| What | Where | Made | Used |
|---|---|---|---|
| Keyring: per Pi (by public key) alias, name, connect code, added, last_seen, last_record | config/pis.json | pairing (network or USB), `add CODE`; last_record by every DHT lookup and by pairing; last_seen by find, connect, remote commands | every command that names a Pi |
| Last DHT record per Pi | cache/found.json | each DHT lookup (find, connect's fallback, add, setup's refresh) | `connect`: LAN addresses to try first. Duplicates `last_record`; not moved by `AUDIOLINK_CONFIG_DIR` |
| ssh block: `Host ALIAS`, `HostName ALIAS`, `Port`, `User`, `ProxyCommand ... connect ALIAS %p` | ~/.ssh/config, between `# BEGIN/END audiolink <tag>` (tag: the Pi's key) | setup, when this computer is a client | every `ssh ALIAS` |
| Host key of whatever was behind the Pi at first contact | ~/.ssh/known_hosts as `[ALIAS]:PORT` (written by ssh, not audiolink) | the first `ssh ALIAS` | every `ssh ALIAS`: wrong as soon as the Pi moves to another host |

Not stored: TLS pins (the Pi's is the public key in its connect code; the relay's is
hard-coded in relay/__init__.py).

### Relay

Nothing on disk. In memory: Pi public key → its control connection (a newer
registration for the same key replaces the old one), waiting and joined sessions.
It never sees a read secret: client and Pi prove themselves to each other inside the
joined connection. Logs: Pi up/down by key prefix, session bytes.

### DHT

One mutable item per Pi, under SHA1(public key + "audiolink-v1"), signed with the Pi's
key. Public: the key, `seq` (publish time). Sealed with the read secret: name, ts, LAN
address, SSID, exposed ports. The Pi publishes when discoverable, online and its clock is
known: on any change (checked every 30 s) and every 30 min; nodes drop it after ~2 h.
Read by clients' lookups.

### The connect code

`al1-` + base64url(public key 32 B ‖ read secret 16 B). The public key finds the Pi
(relay, DHT) and proves it (TLS pin). The secret is the access: whoever has it is paired.
Pairing (SPAKE2 with the 6-digit code, or USB) hands this code over, plus the Pi's
current record. An unclaimed Pi pairs with anyone who reaches it, with no code.

## What's wrong with it

- The ssh block names a Pi, but ssh treats the name as a computer: `known_hosts` pins
  the host behind the Pi at first contact, and `User` is that host's login. Move the
  Pi and both are wrong.
- `connect` trusts an IP address: the cached LAN address gets a plain TCP connection, no
  proof it's the right Pi. ssh's host key check is the only safety net, and it fails
  open for cloned host images (same sshd key) and on a first contact.
- No per-client access: revoking one client means re-pairing all of them.
- WPA3 passwords are stored in plain text on the card (WPA2 stores the derived key).
- found.json duplicates `last_record`, and ignores `AUDIOLINK_CONFIG_DIR`.
- discovery/record.py's docstring says the host keeps the identity and pushes it to
  the Pi; the Pi has made its own since the move to Pi-side identity.

## Proposed: hosts get an identity

The host becomes something a client pairs with; the Pi stays the route. Both kinds of
ssh entry are supported:

- **Host entry** (`ssh macbook`): a computer, whichever Pi it's behind. Everything ssh
  needs is fixed and checked at pairing.
- **Pi entry** (`ssh green-rowan`): whatever computer this Pi is plugged into (a Pi
  carried between machines). The host key is resolved at connect time, by host
  identity.

### New on the host

| What | Where | Made | Used |
|---|---|---|---|
| Host identity: ed25519 key, secret, name | config/host-identity.json | first `audiolink setup` that makes this computer a host | signing "I'm behind Pi P"; sealing its own DHT record; pairing as a host |
| Its sshd host keys (public) | read from the ssh server (/etc/ssh/*.pub; %ProgramData%\ssh) | the OS | sent to clients at pairing; published in its signed statement |

The host's code would be `al1h-` + public key + secret, shared like a Pi's.

### New on the Pi

| What | Where | Made | Used |
|---|---|---|---|
| Host statement: host public key, name, sshd host keys, since, signed by the host | memory (and identity.json if wanted across restarts) | each time a host connects over USB | added to the Pi's DHT record; sent on request to `connect` and `known-hosts` |

### New in the DHT

| What | Made | Used |
|---|---|---|
| Each Pi's record also carries its host statement | the Pi, as now | clients checking which host is behind a Pi |
| One item per host (under the host's key, sealed with the host's secret): its current Pi(s) and their connect codes | the host, published through its Pi when online | host entries: which Pi to go through |

### New on the client

| What | Where | Made | Used |
|---|---|---|---|
| Hosts: per host key alias, host code, sshd keys, last record | config/hosts.json | pairing with a host (`audiolink invite` on the host, 6-digit code here) | `connect --host`; setup |
| Pis learned through a host | config/pis.json, as now, marked as learned | the host's record | routing only: no Pi entry is written for them |
| Host entry: `Host macbook`, `User`, `HostKeyAlias audiolink-<host key>`, `ProxyCommand audiolink connect --host <key> %p` | ~/.ssh/config | setup | `ssh macbook` |
| Its sshd keys, under `audiolink-<host key>` | ~/.ssh/known_hosts | setup, from pairing: no first-use prompt | every `ssh macbook` |
| Pi entry: adds `KnownHostsCommand audiolink known-hosts %n` | ~/.ssh/config | setup | each `ssh green-rowan`: asks the Pi for its host statement, checks the host's signature, answers with that host's keys (first contact with a new host: as ssh's own first-use prompt) |

### What `connect` would do

1. Host entry: the host's record (cache, then DHT) gives its Pi(s). Pi entry: the Pi.
2. Reach the Pi: cached LAN address, relay, DHT, **with TLS pinned to the Pi's key** on
   the direct path too (fixes trusting an IP address).
3. Host entry: check the Pi's host statement names this host, signed by it.
4. After going through the relay, refresh the Pi's record, so the next time is direct.

### Moves, under each entry

| Change | Host entry (`ssh macbook`) | Pi entry (`ssh green-rowan`) |
|---|---|---|
| Pi moves to another host | "macbook has no Pi right now" | new host: first-use prompt, keyed by that host; a host seen before: no prompt |
| Another Pi behind the same host | nothing to do (the host's record says so) | a new Pi: pair with it |
| Host image cloned (same sshd key) | the clone has another host identity: refused | the statement names the clone: told apart |
| Pi card cloned (same Pi identity) | two Pis, one key: the relay could refuse the second registration | same |

## Open questions

- One host, two Pis: one host entry, either Pi as the route?
- Does pairing with a host give any access to its Pi beyond routing (logs, Wi-Fi)?
  Probably not: managing the Pi stays with the Pi's own code.
- Per-client access: the host identity makes it possible (the host keeps a list of
  paired clients and their keys) without changing the Pi.
- What a Pi entry's `User` should be when the host changes: ssh's default login, or
  `user@green-rowan`.
