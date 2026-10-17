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

Replaces the earlier route/admin proposal. Built from five drafts (minimal, principled,
UX first, from the code, contrarian); where they disagreed is listed at the end.

### Two gates

A client reaches a host when both hold, independently:

1. **audiolink's gate**: the host lets this client in, and the Pi in front of it serves
   that host. Checked by the Pi, against lists the owners keep.
2. **The host's own auth**: sshd's `authorized_keys` (or the login of whatever else is
   exposed). Outside audiolink.

Once through gate 1, ssh behaves as if the Pis didn't exist.

### Principals

| Principal | Key | Made |
|---|---|---|
| **Pi** | the Pi's ed25519 key (as now) | first start |
| **Computer** | one audiolink key per computer | first run of `audiolink` |

A computer is a **host** while a Pi is on its USB, and a **client** when it runs `ssh`:
roles, not separate identities. The relay and the DHT are infrastructure, not
principals. Channels (USB, LAN, setup network, relay, the host's local control port)
are not principals either.

No shared secrets grant anything. Every right is a computer key on a list, kept by the
owner of what it protects, revoked one key at a time. (Debug `key` mode already works
this way.)

### What's protected, and who decides

| Destination | Decided by | List | Kept on |
|---|---|---|---|
| A host's exposed ports (ssh, VNC, web UIs) | the host's owners | **host clients**: keys allowed in, optionally per port | the host (host.json, beside `expose`) |
| Which hosts a Pi serves (transit) | the Pi's owners | **Pi serves**: its owners' computers, plus hosts it's lent to | the Pi |
| The Pi's own services: Wi-Fi, name, logs, updates, debug, backup/restore | the Pi's owners | **Pi owners** | the Pi |
| Who may change a host's lists | the host's owners | **host owners**: the host itself, plus computers it names | the host |

Transit is granted to **hosts**, not clients: a Pi serves its owners' computers by
default, and others only when lent. Clients never hold anything on a Pi just to pass
through it. So:

- A Pi moved to a stranger's computer serves nobody there until it's lent: route
  doesn't follow the Pi (problem 1).
- A second Pi behind the same host needs nothing from clients: the host's list is what
  counts.
- Owning a Pi doesn't let you into the hosts it's plugged into, and being on a host's
  list doesn't let you manage its Pi.

How the Pi checks gate 1: the host hands its clients list and expose list to the Pi
over USB (as it hands over `expose` now); the Pi forgets them at unplug. A stream to
port N is allowed when the Pi serves this host, the client's key is on the host's
list (for N), N is exposed, and no context rule says no. The host still refuses
streams to anything it didn't expose, as now.

### Bindings

"Host H is on Pi P, since t": a fact, stated by both and checkable by clients. It
grants nothing. It says where a Pi's streams go and which host a Pi entry means.

### Channels

USB, LAN and relay all prove the same way: a key, then a list. Physical presence counts
for two things only:

| Presence | Allows |
|---|---|
| USB on an **unclaimed** Pi | claim: the computer becomes the Pi's first owner |
| the reset pins (or wiping the card) | reset: new Pi key, no owners, no Wi-Fi: a new Pi |

Over USB a host does its host jobs (audio, `expose`, handing over its lists, the
binding), because those are about its own ports. Anything else on the Pi needs its
key on the Pi owners list, over USB like any other channel. The host's local control
port answers only the OS user who owns the computer key.

An unclaimed Pi no longer pairs with whoever reaches it first over the network.

### Context rules

A separate axis. They only narrow what a list allows:

- no Wi-Fi changes over the relay; no Pi shell or proxy over the relay;
- exposed ports refused on the setup network unless the Pi's owner allows it;
- debug `none` ("anyone", no key): LAN only, timed;
- the Pi's exposed-port range, set by its owners.

### Codes and invites

| Thing | Contains | Gives |
|---|---|---|
| **Pi code** (`al1-`) | the Pi's public key + a find secret | finding the Pi (its sealed record) and checking it's the right one. No access |
| **Host code** | the host's public key + a find secret | finding the host (its sealed record: current Pis) and its sshd keys. No access |
| **Invite** | a one-use 6-digit code (or a pasted one) | adds the redeemer's key to one list, plus the codes it needs |

The find secret takes one of the read secret's four jobs; the others go to keys.
Rotating it hides the record from those who lost access; it never revokes anything.

| Command | Puts the redeemer on | Writes |
|---|---|---|
| `audiolink invite` (on a host, or by a host owner) | host clients | a host entry (`ssh macbook`) |
| `audiolink invite --owner` (by a Pi owner) | Pi owners (and, from a host that owns the Pi, host owners and clients too: "this is my computer as well") | a Pi entry (`ssh green-rowan`) |
| `audiolink lend NAME` (by a Pi owner) | Pi serves: the borrower's host | nothing on the borrower's side beyond host setup |

A copied invite is spent once used, and the entry it made has a name, so one removal
undoes it.

### Lifecycle

| Event | What happens |
|---|---|
| Claim | USB on an unclaimed Pi: that computer is its first owner |
| Add / remove owner | a Pi owner (`invite --owner`, `remove`) |
| Transfer (give away) | add the new owner, then remove yourself; their list review is up to them |
| Sell | `reset`: a new Pi, nothing carries over |
| Lend / take back | `lend` adds the borrower's host to Pi serves; plugging the Pi into one of yours, or `remove`, takes it back |
| Pi moves between your hosts | nothing: your computers are served by default |
| Revoke a client | remove its key from the host clients list |
| Lose a laptop | remove its key wherever it is (from any computer of yours: owner lists can be edited remotely) |
| Reinstall a computer | a new key, so a new computer: remove the old name, invite the new one |
| Backup / restore | Pi owners only; the file encrypted with a passphrase. No custody |

### The ssh side

Unchanged from before: what ssh needs, written by setup.

| Entry | Lines | Host key |
|---|---|---|
| Host entry (`ssh macbook`) | `User`, `HostKeyAlias audiolink-<host key>`, `ProxyCommand audiolink connect --host <key> %p` | written into known_hosts from the invite: no first-use prompt |
| Pi entry (`ssh green-rowan`) | no `User`, `KnownHostsCommand audiolink known-hosts %n`, `ProxyCommand audiolink connect green-rowan %p` | per host behind the Pi, from its signed binding statement; a new host: ssh's first-use prompt |

A Pi entry works on whichever host the Pi is in, if that host lets you in.

### What `connect` does

1. Host entry: the host's record (cache, then DHT, then asking the Pis it knows on the
   LAN) gives its Pi(s); any will do. Pi entry: the Pi.
2. Reach the Pi: cached LAN address, relay, DHT, TLS pinned to the Pi's key on every
   path.
3. Prove the computer key (gate 1).
4. Host entry: check the binding names this host.
5. After going through the relay, refresh the Pi's record.

Exposed ports stop being plain TCP on the Pi's Wi-Fi: on the LAN too, streams go
through the Pi's endpoint, like the relay path. Plain `ssh -p 2222 PI-ADDRESS` stops
working. This is a wire change.

### What each machine stores, new

| Machine | Adds | Drops |
|---|---|---|
| Pi | Pi owners; Pi serves; find secret; the current host's lists and binding (memory) | the read secret; "USB = everything"; debug `paired` mode (becomes owners) |
| Host | computer key; host clients and host owners (host.json); its find secret; its sshd keys (read from the OS) | custody |
| Client | computer key; hosts (key, code, sshd keys, last record); Pi codes | per-Pi read secrets |
| DHT | each Pi's record sealed with its find secret, with its binding; one record per host (its current Pis) | records sealed with the read secret |

### Moves

| Change | Host entry (`ssh macbook`) | Pi entry (`ssh green-rowan`) |
|---|---|---|
| Pi moves to another of your hosts | "macbook has no Pi right now"; `ssh studio` works | reaches studio |
| Pi lent to a friend's host | "no Pi right now" | refused by the friend's host, unless it lets you in |
| Pi plugged into a stranger's computer, not lent | served: nobody | refused |
| Another Pi behind the same host | nothing to do | a new Pi: invite to it |
| Host image cloned (same sshd key) | the clone has another computer key: refused | the binding names the clone |
| Pi card cloned (same Pi key) | the relay could refuse the second registration | same |

### Where the drafts disagreed

| Question | Chosen here | Other drafts |
|---|---|---|
| Transit: a grant to clients? | granted to hosts (Pi serves its owners' computers, plus lent hosts) | principled, from the code: a per-client transit grant as well; minimal, contrarian: none, the Pi serves any host it's in |
| Where gate 1 is checked | the Pi, with lists the host hands over | contrarian: the host's end of the link, so the Pi checks nothing about hosts |
| Owner as a principal | no: owners are computer keys on a list | UX first: a "person" (a group of computers), so one invite adds a computer to everything |
| Host: computer or OS user | open (see below) | principled, UX first: computer; minimal, contrarian, from the code: OS user |
| What a lent-to host may do on the Pi | its host jobs only | contrarian: also add its own Wi-Fi networks, tagged |
| Transfer | add then remove; selling is `reset` | principled: transfer with grants cleared or kept by choice |

## Decided

- Two gates: audiolink's, then the host's own auth.
- One host, two Pis: either Pi is the route. Later: picking the best one, or both.
- Managing a Pi is separate from reaching a host through it.
- Pi entries have no `User`.
- No custody by default; backup and restore as owner commands.
- Grants attach to the thing they protect, are kept by its owners, and go to keys,
  revoked singly. Channels carry no authority beyond claim and reset.

## Open questions

- Host: one key per computer (matches sshd, needs a system-wide service and admin
  rights to set up) or per OS user (simpler, but two users can expose one sshd)?
- A "person" (your computers as a group): needed so one invite adds a new laptop to
  everything, or are owner lists plus `invite --owner` enough?
- Should the Pi check gate 1 (lists handed over by the host), the host (at its end of
  the link), or both?
- What may a host the Pi is lent to do on the Pi beyond its host jobs (Wi-Fi, logs)?
- A Pi without saved state: refuse to pair, or pair with a warning that it lasts until
  it restarts?
- Removal while a host or Pi is offline: how long does a removed key still work?
