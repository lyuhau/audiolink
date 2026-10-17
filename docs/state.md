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
- The read secret is both the way through the Pi and the right to manage it: anyone
  who can ssh through a Pi can also change its Wi-Fi, read its logs and issue pairing
  codes.
- Custody: the host keeps the Pi's private key, on by default, so a Pi without saved
  state keeps its identity. Anyone who can read the host's config folder can pose as
  the Pi.

## Proposed

### Two gates

A client reaches a host when both hold, independently:

1. **Route** on a Pi in front of the host: the right to open a stream to a port the
   host exposes through that Pi. Checked by the Pi.
2. **ssh auth** on the host: its `authorized_keys` (or password). Checked by sshd.

audiolink owns gate 1 only. Who may log in, and revoking a client, is ssh's business, as
it would be without audiolink. Once through gate 1, ssh behaves as if the Pis didn't
exist.

### Privileges on the Pi

| Privilege | Allows | Held by | Proven with |
|---|---|---|---|
| route | open a stream to an exposed port; read the Pi's record (to find it) | clients | the route secret |
| admin | manage the Pi: Wi-Fi, name, logs, pairing codes, updates, giving out route, backup and restore | whoever manages the Pi | the admin secret (today's read secret) |
| host | everything | the computer on its USB | being on the USB link (as now) |

Admin doesn't include route; an admin can give route to anyone, itself included.
Revoking route: rotate the route secret (admin), then hand the new one to the clients
that keep it. Rotating it doesn't touch admin, and the reverse.

Codes: `al1-` (admin, as now) and a route code (public key + route secret). Pairing
with a 6-digit code gives what the person issuing the code chooses: route by default.

### Hosts get an identity

The host becomes something a client knows; the Pi stays the route. Knowing a host is
not a gate: it's how a client finds the host's Pis and checks the host's ssh keys.
Both kinds of ssh entry are supported:

- **Host entry** (`ssh macbook`): a computer, whichever Pi it's behind. Everything ssh
  needs is fixed and checked when the host is added.
- **Pi entry** (`ssh green-rowan`): whatever computer this Pi is plugged into (a Pi
  carried between machines). The host key is resolved at connect time, by host
  identity. No `User` line: ssh's default login, or `ssh user@green-rowan`.

`audiolink invite` on a host hands a client both at once (the host's code, and route on
its Pis), but they stay two credentials, revoked separately.

### New on the host

| What | Where | Made | Used |
|---|---|---|---|
| Host identity: ed25519 key, secret, name | config/host-identity.json | first `audiolink setup` that makes this computer a host | signing "I'm behind Pi P"; sealing its own DHT record; `audiolink invite` |
| Its sshd host keys (public) | read from the ssh server (/etc/ssh/*.pub; %ProgramData%\ssh) | the OS | sent to clients by `invite`; in its signed statement |

The host's code would be `al1h-` + public key + secret, shared like a Pi's.

Removed: custody. A Pi without saved state gets a new identity at each start; that's
expected off the happy path (the Pi has an SD card). For those who want it:
`audiolink pi backup NAME FILE` and `audiolink pi restore NAME FILE`, admin only (over
USB or with the admin code), the file encrypted with a passphrase.

### New on the Pi

| What | Where | Made | Used |
|---|---|---|---|
| Route secret | identity.json, beside the admin secret | first start; rotated by an admin | proving route; sealing the Pi's DHT record (route holders need it to find the Pi) |
| Host statement: host public key, name, sshd host keys, since, signed by the host | memory (and identity.json if wanted across restarts) | each time a host connects over USB | added to the Pi's DHT record; sent on request to `connect` and `known-hosts` |

### New in the DHT

| What | Made | Used |
|---|---|---|
| Each Pi's record (now sealed with the route secret) also carries its host statement | the Pi, as now | clients checking which host is behind a Pi |
| One item per host (under the host's key, sealed with the host's secret): its current Pi(s), by public key | the host, published through its Pi when online | host entries: which Pi to go through |

### New on the client

| What | Where | Made | Used |
|---|---|---|---|
| Hosts: per host key alias, host code, sshd keys, last record | config/hosts.json | `audiolink invite` on the host, 6-digit code here | `connect --host`; setup |
| Pis: route code (and admin code, for those who manage it) | config/pis.json, as now | invite, pairing, `add CODE` | gate 1; finding the Pi |
| Host entry: `Host macbook`, `User`, `HostKeyAlias audiolink-<host key>`, `ProxyCommand audiolink connect --host <key> %p` | ~/.ssh/config | setup | `ssh macbook` |
| Its sshd keys, under `audiolink-<host key>` | ~/.ssh/known_hosts | setup, from the invite: no first-use prompt | every `ssh macbook` |
| Pi entry: `Host green-rowan`, no `User`, `KnownHostsCommand audiolink known-hosts %n`, `ProxyCommand audiolink connect green-rowan %p` | ~/.ssh/config | setup | each `ssh green-rowan`: `known-hosts` asks the Pi for its host statement, checks the host's signature, answers with the keys kept for that host (a new host: ssh's own first-use prompt) |

### What `connect` would do

1. Host entry: the host's record (cache, then DHT) gives its Pi(s); any of them will
   do. Pi entry: the Pi.
2. Reach the Pi: cached LAN address, relay, DHT, **with TLS pinned to the Pi's key** on
   the direct path too (fixes trusting an IP address).
3. Prove route (gate 1).
4. Host entry: check the Pi's host statement names this host, signed by it.
5. After going through the relay, refresh the Pi's record, so the next time is direct.

### Moves, under each entry

| Change | Host entry (`ssh macbook`) | Pi entry (`ssh green-rowan`) |
|---|---|---|
| Pi moves to another host | "macbook has no Pi right now" | new host: first-use prompt, keyed by that host; a host seen before: no prompt |
| Another Pi behind the same host | nothing to do (the host's record says so) | a new Pi: pair with it |
| Host image cloned (same sshd key) | the clone has another host identity: refused | the statement names the clone: told apart |
| Pi card cloned (same Pi identity) | two Pis, one key: the relay could refuse the second registration | same |

## Decided

- One host, two Pis: one host entry; either Pi is the route. Later: choosing the best
  one (same LAN, fastest), or using both.
- Knowing a host gives nothing on its Pis beyond route. Managing a Pi takes admin, by
  other paths; the ssh session never knows about Pis.
- Per-client access is ssh's (`authorized_keys`), as without audiolink. audiolink's gate
  is route, revoked by rotating the route secret.
- Pi entries have no `User`.
- No custody by default; backup and restore as admin commands.

## Open questions

- How route reaches a host's clients after a rotation: re-invite, or the host
  republishes it to those it invited (which means the host keeps a list).
- Whether a Pi without saved state should refuse to pair, or pair with a warning that
  the pairing lasts until it restarts.
