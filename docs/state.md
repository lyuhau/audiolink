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

The decoupled model. Built from four drafts (minimal, principled, UX first, from the
code) and a critique; where they differ is in "Open questions".

### Two gates

A client reaches a host when both hold:

1. **The Pi**: the client's key is on this Pi's list. Decided by the Pi's owners. The
   Pi knows nothing about the host's people.
2. **The host**: the host's own login (sshd and `authorized_keys`), as without
   audiolink. The host knows nothing about the Pi's list.

The only thing the host tells its Pi is **what** it exposes (`expose`, as today), never
**who**. Exposing a port through a Pi means "this Pi's people may knock", like opening a
port on a LAN. Who gets in to which host is gate 2's job. No lists move between host and
Pi.

Supported scope: every host a Pi is plugged into is its owner's or trusted. Lending a
Pi to someone else's computer is at your own risk.

### Principals

| Principal | Key | Made |
|---|---|---|
| **Pi** | the Pi's ed25519 key (as now) | first start, or reset |
| **Computer** | one audiolink key per OS user on a computer | first run of `audiolink` |

A computer is a **host** while a Pi is on its USB, and a **client** when it runs `ssh`:
the same key. People, the relay, the DHT and channels are not principals.

### The Pi's list

One list on the Pi, kept by its owners, each entry a computer key, a name
(`laptop`, `mom`) and a level:

| Level | May |
|---|---|
| **user** | pass through the Pi to whatever its current host exposes |
| **owner** | everything a user may, plus manage the Pi: Wi-Fi, name, logs, status, the list and invites, updates, the shell, backup and restore, reset |

Owners are users: an owner could always add themselves, so separating them would
protect nothing. The last owner can't be removed (reset instead). `claimed` becomes "the
list has an owner".

Removing a key takes effect on that Pi at once, for every host behind it. No shared
secret is rotated and nobody else is touched.

### What the host does

Over USB, with no key needed, a host does **host jobs** only: audio, `expose`, and the
binding statement. Anything else on the Pi needs an owner key, over USB like any other
channel. A reinstalled computer that isn't an owner yet can still be the host.

- **Decides**: which ports it exposes; who logs in (gate 2).
- **Never decides**: who is on the Pi's list, or the Pi's settings.
- Its local control port answers only the OS user whose key it is, and carries no
  authority of its own: owner requests are signed with that key.

### Binding

"Host H (its computer key, name and sshd host keys) is on Pi P, since t." The host
states it over USB; the Pi publishes it in its record. It grants nothing. It says where
the Pi's streams go, which computer a Pi entry means (and its host keys), and lets a
client learn host entries: `ssh desktop` means "whichever of my Pis carries desktop's
binding". So hosts need no code of their own, no record of their own and no list.

### Channels and presence

USB, LAN, setup network, relay and the control port carry no authority: a key, then the
list. Physical presence counts for two things:

| Presence | Allows |
|---|---|
| USB on a Pi with no owner | **claim**: the host's key becomes the first owner (`audiolink setup`) |
| the reset pins | **reset**: new Pi key, empty list, no Wi-Fi: a new Pi |

A Pi without saved state claims with a warning (the list lasts until it restarts). An
unclaimed Pi no longer pairs with whoever reaches it first over the network.

On the LAN, exposed ports stop being plain TCP on the Pi's Wi-Fi: streams go through
the Pi's endpoint with a key, as relay streams do now. Without this, gate 1 doesn't
exist on the LAN. Plain `ssh -p 2222 PI-ADDRESS` stops working.

### Context rules

A separate axis that only narrows:

- relay: no Wi-Fi changes, no shell port, no uplink proxy;
- setup network: exposed ports refused unless an owner allows it;
- debug "anyone" (no key): LAN only, timed;
- the exposed-port range, set by owners.

### Codes and invites

| Thing | Contains | Gives |
|---|---|---|
| **Pi code** (`al1-`) | the Pi key + a find secret | finding the Pi (its record, sealed with the find secret) and checking it's the right one. No access |
| **Invite** | a one-use 6-digit code (today's pairing code) | puts the redeemer's key on the list, as user or owner, and hands over the Pi code |

Today's read secret does four jobs: sealing the record goes to the find secret; request
proofs and the relay tunnel proof go to keys; sealing fields goes to the keyed
connection. A removed key still knows the find secret: it can see where the Pi is, not
get through. Rotating the find secret hides that; it revokes nothing.

### Commands

| Command | Does |
|---|---|
| `audiolink setup` | claims a new Pi on USB, makes this computer its host, or redeems an invite |
| `audiolink invite NAME [--owner]` | a code that puts NAME on the list (on a host: on all its Pis) |
| `audiolink remove NAME` | takes NAME off every Pi this computer owns; reminds about `authorized_keys` |
| `audiolink reset PI` | the same as the pins, by an owner: giving away and selling are both this |

Setup's last check runs real ssh (as now) and reports gate 2 plainly: "logged in", or
"reached desktop's sshd; your key was refused. Add this key on desktop: ssh-ed25519 …".
audiolink never edits `authorized_keys`.

### Lifecycle

| Event | What happens |
|---|---|
| Claim | `setup` with an unclaimed Pi on USB: first owner |
| Add a computer | `invite NAME --owner` on an owner computer, `setup` on the new one |
| Let a family member through | `invite NAME`; their key in the right hosts' `authorized_keys` |
| Remove someone | `remove NAME`; their line in `authorized_keys` is yours to remove |
| Lose a laptop | `remove laptop` from another owner computer: every Pi it was on |
| Pi moves between your hosts | nothing. Setup on the new host says who can now knock. `ssh macbook`: "no Pi now (green-rowan moved to desktop)" |
| Second Pi on a host | claimed by the host; setup offers to copy the first Pi's list (a snapshot) |
| Give away / sell | `reset` (or the pins): nothing carries over; the new owner claims it |
| Reinstall a computer | a new key: invite it, remove the old name; ssh warns about the new host key as usual |
| Backup / restore | owners only, encrypted with a passphrase. No custody |

### The ssh side

| Entry | Lines | Host key |
|---|---|---|
| Host entry (`ssh desktop`) | `User`, `HostKeyAlias audiolink-<host key>`, `ProxyCommand audiolink connect --host <key> %p` | from the binding, written to known_hosts |
| Pi entry (`ssh green-rowan`) | no `User`, `KnownHostsCommand audiolink known-hosts %n`, `ProxyCommand audiolink connect green-rowan %p` | per host behind the Pi, from its binding |

The Pi's own sshd (dev images) is a different machine: its own entry
(`green-rowan-pi`), owners only.

`connect`: find a Pi (host entry: one of my Pis carrying that host's binding; Pi entry:
the Pi), reach it by cached LAN address, relay or DHT, with TLS pinned to the Pi's key
on every path, prove the computer key, and refresh the Pi's record after going through
the relay.

### What each machine stores, new

| Machine | Adds | Drops |
|---|---|---|
| Pi | the list (key, name, level); the find secret; the current binding | the read secret; "USB = everything"; `claimed`; debug `paired` mode |
| Host | its computer key | custody |
| Client | its computer key; Pi codes (pub + find secret) | per-Pi read secrets |
| DHT | each Pi's record, sealed with the find secret, with its binding | records sealed with the read secret |

### Getting there

In order, each step keeping one person's setup working (from the code draft):

| # | Step | Pi update | Wire change |
|---|---|---|---|
| 1 | computer keys; control port limited to its OS user | no | no |
| 2 | the list on the Pi; USB claim puts the host's key on it as owner | yes | the host's key in the USB hello |
| 3 | key proofs beside the read secret, on the endpoint and the relay tunnel | yes | new proof fields |
| 4 | invites put keys on the list; `new_code` owner-only | yes | pairing payload |
| 5 | today's `paired` split into owner and user ops | yes | no |
| 6 | USB narrowed to host jobs; other link requests need an owner key | yes | key proof on link requests |
| 7 | find secret seals the record; new Pi code | yes | record and code format |
| 8 | exposed ports gated on the LAN | yes | **yes, the big one** |
| 9 | custody dropped; backup and restore | yes | `restore` changes |
| 10 | binding statement; host and Pi ssh entries | binding only | binding |
| 11 | read secret removed | yes | breaks old peers: last, behind a version check |

## Decided

- Two gates, decoupled: the Pi's list, then the host's own login. No lists between them.
- Lending a Pi to someone else's computer is unsupported (at your own risk).
- No "person": computers are the principals.
- Host identity is per OS user.
- One host, several Pis: any Pi is a route.
- Managing a Pi is separate from passing through it; owners are users.
- Pi entries have no `User`.
- No custody; backup and restore as owner commands.
- A Pi without saved state claims with a warning.

## Open questions

- **Ports without a real login.** A VNC, dev web server or database exposed through
  the Pi is reached from the host itself, so services that trust local connections let
  every Pi user straight in; a LAN port never did that. Proposal (critique): `expose`
  per port can say `--to owners` (default: users). The host names a level the Pi
  defines, never a person. Adopt?
- **Two Pis on one host**: copy the list once (and let them drift), keep them in sync,
  or let the host expose per Pi?
- **Root for owners**: does an owner still need the debug switch on to get a shell or
  update, or is being an owner enough?
- **The uplink proxy** (the host browsing as the Pi, for captive portals): a host job
  or owners only?
- **Invite and gate 2**: should an invite carry the redeemer's ssh public key back, so
  the inviting host can offer to add it to `authorized_keys`? The one shortcut into
  gate 2; nothing stored.
- **Removal while a Pi is offline**: queued on the removing computer, retried until the
  Pi is reachable. How long, and is that enough?
- **Two OS users on one computer** both running `audiolink host`: only one can hold the
  USB; refuse the second?
