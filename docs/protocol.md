# Protocol v2: keys instead of the read secret

The wire changes for steps 1 and 2 of docs/state.md ("Getting there"): computer keys,
the Pi's list, one path for LAN and relay streams, invites. What stays as it is is
said once and not repeated. File references are to the code as of 9ffbf00.

## Versions

| Protocol | Today | v2 |
|---|---|---|
| USB link | SYNC byte 4 = `PROTOCOL_VERSION` 2 | **`PROTOCOL_VERSION = 3`**. Peers on 2 never parse each other's frames; `Session.request` already says "update the older one" |
| Endpoint (TCP 47900) | no version; TLS vs plaintext only | **`ENDPOINT_VERSION = 2`** (auth.py): the hello reply carries `"v": 2`. A client finding no `v` says "update the Pi: plug it in and run `audiolink update`". A Pi getting an old-style request (`proof` field, or any op before `auth`) answers `{"code": "upgrade"}`, as for plaintext today |
| Relay server | `"v": 1` | unchanged: it joins by Pi key and splices raw bytes |
| Relay tunnel (inside the splice) | nonce, `{port, cnonce, proof}`, HMACs | **gone**: the splice goes straight to the endpoint (see Relay) |

Moving a Pi over: the old Pi takes `audiolink update` over USB without a key (link v2
with an old host), so the host updates it first, then claims it under v2. A v2 Pi
loading an old identity.json drops `secret`, `claimed` and `discoverable`: it comes up
unclaimed, keeping its key and name, and the host claims it over USB.

## Keys

| Key | What | Where | Made |
|---|---|---|---|
| Pi key | ed25519 seed, as now | identity.json | first start, reset |
| Computer key | ed25519, an unencrypted OpenSSH `openssh-key-v1` file (`client/remote.py Key` reads it) | `<config>/key`, `<config>/key.pub`, user-only | first run of any `audiolink` command |
| Control token | 32 random bytes, hex | `<config>/control-token`, user-only | first `audiolink host` |

A computer's name on a list is chosen by whoever invites it (`invite NAME`), or the
hostname at claim.

One message proves a computer key, on the endpoint and on the USB link alike
(`auth.auth_message`):

```
ed25519 over  "audiolink-auth-v2\0" + nonce(16) + pi_pub(32)
```

The nonce is fresh from the Pi per connection or link-up, so a signature can't be
replayed on another connection, channel or Pi. (The Pi's relay registration keeps its own
`audiolink-relay-register-v1\0`.)

## The Pi's list

In identity.json, replacing `secret` and `claimed`:

```json
{"seed": "...", "name": "green-rowan",
 "members": [{"key": "<hex32>", "name": "macbook", "role": "owner", "added": 1760000000}]}
```

`role` is `user` or `owner` (owner may do everything). "Claimed" means an owner is on
the list. The last owner can't be removed.

## Endpoint (TCP 47900)

Transport, TLS (Pi certificate from its seed; the client pins the certificate's key to
the Pi code), JSON lines and in-order replies: as now (endpoint.py, tls.py).

### Handshake

```
C: {"op": "hello"}
P: {"v": 2, "name", "tag", "pub", "claimed", "nonce"}
C: {"op": "auth", "key": "<hex32>", "sig": "<hex64>"}        (sig over auth_message)
P: {"ok": true, "name": "laptop", "role": "user",
    "record": {"name", "ts", "lan", "ssid", "ports"}}  |  {"error", "code": "not_listed"}
```

The record (today's DHT record, unsealed) comes with every auth, so each connect
refreshes the client's copy.

`auth` is once per connection: from then on the connection has that key's role.
(Replaces the per-op `proof` and the debug `key`/`sig`.) Without `auth`, only `hello`
and `pair` work.

### Ops

| Op | Role | Request | Reply |
|---|---|---|---|
| hello | none | — | above |
| auth | none | key, sig | above |
| pair | none | SPAKE2 (see Invites) | — |
| **open** | user | `port` | `{"ok": true}`, then the connection becomes the stream (see Streams) |
| **remove** | owner: any key; anyone: their own | `key` or `name` | `{"ok": true}`  \|  `{"error", "code": "last_owner"}` (`forget` removes its own key) |
| **members** | owner | — | `{"members": [{"key", "name", "role", "added"}]}` |
| **invite** | owner | `name`, `role` | `{"code", "expires_in"}` (no sealing: TLS) |
| **reset** | owner | — | `{"ok": true}`; the Pi forgets everything and restarts (new key) |
| change_wifi, change_status, wifi_status | owner | as now; `change_wifi`'s fields go in the clear (TLS), no `sealed` | as now |
| logs, update, update_status, shell | owner | as now | as now |
| rename | owner | `name` | `{"ok": true}` (today USB only) |

Refusals: `{"error", "code"}` with `not_listed` (no auth or not on the list),
`not_owner`, `via_relay` (context rule), `last_owner`, `upgrade`.

Gone from the endpoint: `new_code` (`invite`), the `proof` field, `sealed`, the
`paired`/`debug` levels.

### Context rules

The route is taken from the connection as today (`access.route_of`: source 127.0.0.2 is
the relay). Over the relay: `change_wifi` refused (`via_relay`). Nothing else differs by
route.

## Streams

Exposed ports stop listening on the Pi's Wi-Fi. A stream to the host is:

```
C: TLS to the endpoint, hello, auth
C: {"op": "open", "port": 2222}
P: {"ok": true}  |  {"error": "port 2222 isn't exposed", "code": "not_exposed"}
   then raw bytes both ways; the Pi sends T_OPEN "host:port" on the link, as the
   listener does today (streams.py), and splices
```

Once `open` succeeds the connection leaves the request loop: no IDLE timeout (an ssh
session idles for minutes), not counted against the endpoint's request limits
(MAX_CONNS, per-address unproven), counted instead against a stream cap like the relay's
SESSIONS = 8.

So a LAN client and a relay client do the same thing. `audiolink connect PI PORT`
(the ssh ProxyCommand) is TLS to the endpoint plus `open`; its stdin/stdout are the
stream inside TLS. The Pi's static `--forward` listeners go the same way (no plain
listeners on 0.0.0.0 at all). The host's `allow` check on T_OPEN stays.

## Relay

The relay server is unchanged. On the Pi, a data connection for a client is spliced to
the local endpoint (127.0.0.1:47900, source 127.0.0.2) instead of running the tunnel
handshake. The client then does TLS, hello, auth and `open` through the splice, exactly
as on the LAN. relay/tunnel.py and the `allowed()` port list go. The relay's
known/unknown client split goes with the tunnel (the proof is now in the endpoint); the
per-client and overall open caps stay. The Pi's own sshd (dev images) is reached on its
Wi-Fi address directly, as today; owners also have `shell`.

`pis` checks "online over the relay" with a relay connect plus `hello` (no secret).

## Invites

`invite` (owner) makes a one-use 6-digit code for `{name, role}`; it lasts 300 s, 3
tries, as today's codes. Redeeming, on an endpoint connection without auth:

```
C: {"op": "pair", "msg": hex(X), "client": hostname}
P: {"msg": hex(Y)}                                         (SPAKE2, password: the code)
C: {"confirm": hex(conf_A), "key": hex(seal(ke, {"key": "<hex32>"}))}
P: {"confirm": hex(conf_B), "payload": hex(seal(ke, {"name", "role", "pub", "record"}))}
```

The Pi adds the client's key with the code's name and role. The client stores the Pi's
`pub` (the Pi code) and `record`. A Pi with no owner refuses `pair`
(`{"code": "claim_usb"}`): claiming is over USB only.

## USB link (protocol 3)

Framing, ARQ, streams and CTRL framing as now (wire/protocol.py, session.py).

HELLO: the Pi's adds `nonce` (16 bytes hex, per link-up); its `identity` info drops
`claimed`, adds `owners` (count). The host's adds nothing.

The host proves its key once per link-up, before any owner request:

```
host CTRL: {"op": "link.auth", "args": {"key": "<hex32>", "sig": "<hex64>"}}   (sig over auth_message)
Pi:        {"ok": true, "result": {"role": "owner"}}  |  {"ok": true, "result": {"role": null}}
```

After `identity.claim` succeeds, that link's role is owner at once (no separate
`link.auth`), so setup goes straight on to Wi-Fi.

| CTRL op | Needs |
|---|---|
| audio.*, link.auth, identity.info, `expose` (in HELLO) | nothing: host jobs |
| **identity.claim** `{key, name}` | a Pi with no owner; makes that key the first owner, returns `members` |
| everything else (wifi.*, update.*, logs.*, identity.rename, members/invite/remove, setup_ap.*, relay.status) | the link's role is owner |
| the host's T_OPEN to the Pi's proxy or shell | the link's role is owner |

Refusal: `{"ok": false, "error": "...", "code": "not_owner"}`. The message says what to
do: "this computer isn't an owner of green-rowan: on an owner computer, `audiolink
invite NAME --owner`".

Gone: `identity.code`, `identity.rotate_secret`, `debug.*`, `discovery.*`,
`pairing.*` (moves to the endpoint's `invite`). `identity.export`/`restore` (custody) go
in step 3.

## Host control port (127.0.0.1:47800)

Every request line carries the token: `{"op", "args", "token"}`. A wrong or missing
token: `{"ok": false, "error": "not this user's audiolink host"}`. Forwarded ops travel
on the link session, which has the host's key's role: the control port adds no
authority. The 47801 (proxy) and 47802 (shell) listeners stay; the Pi accepts the
host's T_OPEN to its proxy (127.0.0.1:3128) or shell (127.0.0.1:47901) only when the
link's role is owner. They answer only on 127.0.0.1, but any local user can connect:
for the first version that's accepted on a one-user computer (a second OS user's host is
refused anyway).

## Removed

| Thing | Where |
|---|---|
| The read secret, `Conn.seal/open`, `proof_for`, `key_message`, `KEY_LABEL` | identity.py, endpoint.py, access.py |
| The DHT: publishing and lookup, the record seal, `found.json`, `dht-size` | services/discovery.py, discovery/dht.py, discovery/record.py, client/find.py |
| The debug switch, its modes, debug.json | services/access.py (DebugService), shell/update levels |
| The relay tunnel | relay/tunnel.py, services/relay.py `allowed()` |
| Plain listeners for exposed ports and `--forward` | services/streams.py |
| `al1-` codes with a secret | discovery/record.py, client/keyring.py (entries keep `pub`, `alias`, `name`, `last_record`) |

## Split

| Pi side (Mac session) | Host and client (this session) |
|---|---|
| identity.json `members`; `identity.claim` | the computer key; keyring without secrets |
| endpoint `auth`, roles, the ops above, `open` | `PiConn.auth`; `connect` = TLS + auth + `open` on every path |
| relay data connection → local endpoint; tunnel removed | `pis` (with `find` folded in), relay online check without a secret |
| link 3, `link.auth`, CTRL role checks | the host's `link.auth` at link-up; control token |
| streams: no plain listeners; streams leave the request loop | `invite`, `remove`, `forget`, setup's claim |
| pairing redeem (Pi side); `pair` refused when unclaimed | pairing redeem (client side) |
| removing the DHT publisher, debug switch, read secret on the Pi | removing the DHT lookup, `found.json`, `share`, `rotate-secret`, `pairing code` |

Shared, written first: `audiolink/auth.py` (`auth_message`, `verify`,
`ENDPOINT_VERSION`; signing is `client/remote.py Key.sign`), the op names and error codes
above, and `PROTOCOL_VERSION = 3`.

Not fixed on main: today's debug-key cache (endpoint.py:156 accepts one verified
signature for any later op on the connection). Debug is off on the only Pi and v2 deletes
that path.
