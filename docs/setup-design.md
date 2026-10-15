# Guided setup: design (agreed 2026-10-15)

## Terminology rule
Things that run are nouns: `audiolink host`, `audiolink pi`, `audiolink relay` (long-running
services; autostart runs `host`). Everything you do is a verb: `setup`, `ssh`, `add`,
`find`, `wifi`, `invite`, `update`, `logs`, ... No `client` noun (there's no client service).
No aliases: renamed commands are replaced outright.

## Layers (`audiolink --help` shows these groups, Start here first)
- Start here: `audiolink setup` (this computer; role worked out), `audiolink setup card`
  (make the Pi's SD card).
- Everyday: `audiolink ssh NAME` (or plain `ssh NAME` once set up), `find`, `pis`, `wifi`,
  `invite`.
- Services: `host`, `pi`, `relay`.
- Advanced: `connect`, `audio`, `update`, `logs`, `shell`, `debug`, `share`, `forget`,
  `rename`, `rotate-secret`, `discovery`, `list-devices`, `dht-size`, `add`.

## Key fact
USB = ownership/trust. The Pi stores nothing about which computer is its host (only its
identity, claimed flag, saved Wi-Fi). Being plugged in makes a computer the host;
host-specific things live on that computer: exposed ports (sent to the Pi each
connection), autostart, the identity custody backup. A computer with the Pi on USB never
needs a pairing code.

## `audiolink setup` = do whatever this computer can do right now
With the Pi on USB (first plug-in, or its host): a host is started for the setup if none
runs (no question). Three questions at most: its name, its Wi-Fi, and "Make this
computer NAME's host?"
1. The Pi: claim it over USB (no code, no "claim it?" question), optional name, Wi-Fi
   (picker). Skip what's already done.
2. Host, yes: expose this computer's ssh (2222:22; Remote Login pane if it's off), saved
   host config, autostart, start it; check that ssh NAME reaches this computer. No ssh
   block here (ssh to itself is no use). End: "set up each computer that should reach
   this one with: audiolink setup".
3. Host, no: this computer is a client: the ssh block; "plug it into its host and run
   audiolink setup there".
Everything else is a flag, never a question (--expose, --no-autostart, --no-ssh, --user):
questions only where the answer isn't obvious, and each one's effect is said after.
Without the Pi on USB: client only: find the Pi (LAN/mDNS) or ask for a code (from
`audiolink invite`), pair, write the ssh block, test (ssh banner through the Pi), end
with "Done: ssh NAME".
Also: install itself (copy to a permanent place, PATH link, clear macOS quarantine).
Re-running is idempotent and shows what's set.
`audiolink setup host` == configure; `audiolink host` == run the daemon (what autostart runs).
Every question has a flag for scripting (--yes takes the suggested answers); the guide
prints the lower-level commands it ran.

## Saved host config
A small file (e.g. ~/Library/Application Support/audiolink/host.toml or json) read by
`audiolink host`: exposes, forwards, allow, audio format, ports. Flags override it.
Changing a port = editing config (via setup), not restarting with flags by hand.

## `audiolink invite [NAME]` (replaces `pairing code`)
Same network: the 6-digit code (as today). Other network: a split code, e.g.
`4821-7790-1235`: first group = rendezvous label the relay sees (to connect the two
sides); the rest = the SPAKE2 password the relay never sees. Relay learns nothing;
active attacker gets online guesses only (3 tries per code, 5 min life). Pairing then
runs over the relay. (A naive "relay looks up the 6-digit code" lets the relay operator
brute-force it offline and pair: don't.)

## Order
1. Saved host config + autostart + `audiolink setup` (Pi/client/host parts, role detection,
   self-install).
2. `invite` with split codes + pairing over the relay.
3. `audiolink ssh NAME`.
4. `audiolink setup card` (list removable disks, confirm, download+verify the image for
   its own version, format FAT32, unzip, retry eject, optional settings.txt answers;
   macOS/Linux first, Windows needs admin).
5. `--help` grouped by layer; README rewritten around `audiolink setup`.
