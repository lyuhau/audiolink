# What each machine stores

What each machine keeps, when the data is made, and when it's used. First as it is now,
then the roles model that replaces it ("Proposed").

Roles: the **Pi**; the **host** (the computer the Pi is plugged into, running
`audiolink host`); a **client** (any computer that runs `ssh NAME`; a host is usually one
too); the **relay**; the **DHT**.

## How ssh stays unaware

ssh asks audiolink two questions and does the rest itself (crypto, host key checks,
login):

- `ProxyCommand audiolink connect NAME %p`: "give me a byte stream to NAME". ssh never
  sees an address, so `HostName` and `Port` in the block are only labels. Finding a
  route (the Pi's LAN address, the relay, the DHT) is audiolink's job.
- `HostKeyAlias` (not used yet; proposed): the name ssh files the host key under in
  known_hosts, in place of `[NAME]:PORT`.
- `KnownHostsCommand` (OpenSSH 8.5+): "which host keys should NAME have?". Not in the
  first version (see "Not in the first version").

## Folders

| | macOS | Windows | Linux |
|---|---|---|---|
| config | ~/Library/Application Support/audiolink | %APPDATA%\audiolink | ~/.config/audiolink |
| cache | ~/Library/Caches/audiolink | %LOCALAPPDATA%\audiolink | ~/.cache/audiolink |
| installed app | ~/Applications/audiolink | %LOCALAPPDATA%\Programs\audiolink (before d3f5dcd: the cache folder, so a reinstall deleted the cache) | ~/.local/share/audiolink |

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
| Card settings: relay, expose_setup | settings.txt (read only) | the card image's default; edited by hand (setup's card step only unzips the image) | card-settings.sh at each start |
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

The first version, cut down after the Mac session's review. Later options are under
"Not in the first version".

### Two gates

A client reaches a host when both hold:

1. **The Pi**: the client's key is on this Pi's list. Decided by the Pi's owners. The
   Pi knows nothing about the host's people.
2. **The host**: its own login (sshd and `authorized_keys`), as without audiolink. The
   host knows nothing about the Pi's list.

The host tells its Pi only **what** it exposes (`expose`, as today), never **who**.
Exposing a port through a Pi is the host's choice, like opening it on the LAN; the
service keeps its own auth. No lists move between host and Pi.

Supported scope: every host a Pi is plugged into is its owner's or trusted. Lending a
Pi to someone else's computer is at your own risk.

### Principals

| Principal | Key | Made |
|---|---|---|
| **Pi** | the Pi's ed25519 key (as now) | first start, or reset |
| **Computer** | one audiolink key per OS user on a computer | first run of `audiolink` |

A computer is a host while a Pi is on its USB and a client when it runs `ssh`: the same
key. People, the relay and channels are not principals.

### The Pi's list

One list on the Pi; each entry is a computer key, a name (`laptop`, `mom`) and a role:

| Role | May |
|---|---|
| **user** | pass through the Pi to whatever its current host exposes |
| **owner** | everything: pass through, and manage the Pi (Wi-Fi, name, logs, status, the list, invites, updates, the shell, the uplink proxy, reset) |

The last owner can't be removed (reset instead). `claimed` becomes "the list has an
owner". Removing a key takes effect on that Pi at once, for every host behind it; no
shared secret is rotated and nobody else is touched.

No debug switch: owners have the shell and updates; lost every owner key: the reset
pins.

### What the host does

Over USB, with no key needed, a host does **host jobs** only: audio and `expose`.
Anything else on the Pi needs an owner key, over USB like any other channel. Claiming
makes the host's key an owner, and the same exchange returns the list, so right after
setup the host can do owner things over USB. A reinstalled computer that isn't an owner
can still be the host, but can't change the Pi's Wi-Fi or anything else until it's
invited as an owner; the error says so.

The host's local control port requires a token from a file in the config folder,
readable only by the OS user who runs `audiolink host`. It carries no authority on the
Pi: owner requests are made with that computer's key.

### Channels and presence

USB, LAN, setup network, relay and the control port carry no authority: a key, then the
list. Physical presence counts for two things:

| Presence | Allows |
|---|---|
| USB on a Pi with no owner | **claim**: the host's key becomes the first owner (`audiolink setup`) |
| the reset pins | **reset**: new Pi key, empty list, no Wi-Fi: a new Pi |

A Pi without saved state claims with a warning (the list lasts until it restarts). An
unclaimed Pi no longer pairs with whoever reaches it first over the network.

On the LAN, exposed ports stop listening on the Pi's Wi-Fi: streams go through the
Pi's endpoint with a key, as relay streams do now, so LAN and relay are one path. Plain
`ssh -p 2222 PI-ADDRESS` stops working. The endpoint knows each stream's route, so
presence counts relay and LAN clients from there (today: from the source address).

### Context rules

A separate axis that only narrows: no Wi-Fi changes, shell port or uplink proxy over
the relay. (The setup-network rule and the exposed-port range go: they existed because
anyone nearby could reach the ports; with every stream needing a listed key, the setup
network is just another channel.)

### Finding a Pi

No DHT. The relay reaches a Pi from anywhere; on the LAN, the cached address or mDNS.
The Pi's record (its LAN address, SSID, ports) comes over the keyed connection: at
pairing, as now, and on each connect. `pis` shows "last seen on SSID X" from the last
contact.

| Thing | Contains | Gives |
|---|---|---|
| **Pi code** | the Pi's public key | finding it on the relay, and pinning TLS to it on every path (LAN and relay). No access, so it can be shown freely |
| **Invite** | a one-use 6-digit code (today's pairing code) | puts the redeemer's key on the list, as user (or owner, `--owner`), and hands over the Pi code |

The read secret goes: request proofs and the relay tunnel proof become keys; the record
seal goes with the DHT; field sealing goes to the keyed connection.

### Commands

| Command | Does |
|---|---|
| `audiolink setup` | claims a new Pi on USB, makes this computer its host, or redeems an invite |
| `audiolink invite NAME [--owner]` | a one-use code that puts NAME on the list |
| `audiolink remove NAME` | takes NAME off every Pi this computer owns; a Pi it can't reach: fails, and says which |
| `audiolink reset PI` | the same as the pins, by an owner: giving away and selling are both this |
| `audiolink forget PI` | this computer drops the Pi, and takes itself off the Pi's list when it can reach it |

`audiolink pis [PI]` shows each Pi: online (LAN or relay) or its last record ("last
seen on SSID X"), its code, and for owners its list. It takes over from `find`.

Gone: `find` (`pis`), `share` (the Pi code grants nothing: `pis` shows it),
`rotate-secret` (`remove` instead), `pairing code` (`invite` instead), `debug`.

Setup's last check runs real ssh and reports gate 2 plainly: "logged in", or "reached
desktop's sshd; your key was refused. On desktop, add your key; from here, if desktop
allows a password: `ssh-copy-id green-rowan`". audiolink never edits `authorized_keys`.

### The ssh side

Pi entries only: `ssh green-rowan` reaches whatever computer the Pi is plugged into.

| Lines | Why |
|---|---|
| `Host green-rowan` | the Pi's name |
| no `User` | ssh's default login, or `ssh user@green-rowan` |
| `HostKeyAlias audiolink-<pi key>` | the host key is filed under the Pi's key, not the name or port |
| `ProxyCommand audiolink connect green-rowan %p` | as now |

Moving the Pi to another of your computers gives ssh's "host key changed" warning,
which is right: it is another computer. Fix it as ssh says, or keep one Pi per host.

`connect`: reach the Pi by cached LAN address, mDNS, or the relay, with TLS pinned to
the Pi's key on every path; prove the computer key; refresh the Pi's record.

The Pi's own sshd (dev images) is reached on its Wi-Fi address directly, as today, not
through audiolink; owners also have `audiolink shell`.

### Lifecycle

| Event | What happens |
|---|---|
| Claim | `setup` with an unclaimed Pi on USB: first owner |
| Add a computer | `invite NAME --owner` on an owner computer, `setup` on the new one |
| Let a family member through | `invite NAME`; their key in the right hosts' `authorized_keys` |
| Remove someone | `remove NAME`; their line in `authorized_keys` is yours to remove |
| Lose a laptop | `remove laptop` from another owner computer: every Pi it was on |
| Pi moves between your hosts | nothing in audiolink; ssh warns once that the host key changed |
| Second Pi on a host | claimed by the host; its own list, invited to separately |
| Give away / sell | `reset` (or the pins): nothing carries over |
| Reinstall a computer | a new key: invite it from another owner computer, remove the old name. If it was the only owner: reset, claim, re-invite (setup suggests a second owner while there's only one) |
| Dead card | reset (new card), claim, re-invite |

### What each machine stores, new

| Machine | Adds | Drops |
|---|---|---|
| Pi | the list (key, name, role) | the read secret; DHT publishing; "USB = everything"; `claimed`; the debug switch |
| Host | its computer key; the control-port token | custody |
| Client | its computer key; Pi codes (the Pi's public key) and last records | per-Pi read secrets; found.json |
| DHT | nothing | the Pi records |

### Getting there

A clean break: there's one deployment, so one protocol version replaces the read secret
with keys; old peers get "update audiolink"; each Pi is claimed once more. The relay
server needs no change (it joins by Pi key and never sees the proofs): the break is the
Pi and the computers only. Wire details: docs/protocol.md.

| # | Step | Pi update |
|---|---|---|
| 1 | computer keys; the Pi's list; key proofs on the endpoint, the relay tunnel and LAN streams (exposed ports off the Wi-Fi); the read secret and the DHT removed; control-port token | yes |
| 2 | `invite`, `remove`, `pis` (with `find` folded in); `forget` removes itself; the gone commands removed | yes |
| 3 | USB narrowed to host jobs; custody removed | yes |
| 4 | Pi entries with `HostKeyAlias`; setup's gate-2 message | no |

Steps 1 and 2 ship together: after 1 alone nobody but the claimer gets on a list. They
are the release that breaks compatibility; 3 and 4 follow.

Moving a Pi over: today's Pi takes a code update over USB without a key, so `audiolink
update` with the new version comes first, then `setup` claims it under the new rules.

### Not in the first version

- **Host entries** (`ssh desktop`, whichever Pi it's behind) with binding statements,
  `KnownHostsCommand` and per-host host keys: when a real two-host case shows up.
- **Backup and restore** (passphrase-encrypted): if re-inviting after a dead card
  proves painful.
- **An ssh-key prompt on invite** (the host offering to add the redeemer's key to
  `authorized_keys`): setup's `ssh-copy-id` line covers it for now.
- **Copying or syncing lists** between two Pis on one host.

## Decided

- Two gates, decoupled: the Pi's list, then the host's own login. No lists between them.
- Lending a Pi to someone else's computer is unsupported (at your own risk).
- No "person": computers are the principals. Host identity is per OS user; a second OS
  user's `audiolink host` on the same computer is refused.
- Roles: user (pass through) and owner (everything).
- No debug switch; no custody; no DHT.
- Exposed services keep their own auth: no per-port roles.
- Pi entries only, for now; ssh's host key warning when a Pi moves is accepted.
- `remove` on a Pi it can't reach fails; no queue.
- A Pi without saved state claims with a warning.
- A clean break, not a gradual migration.
