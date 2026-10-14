# audiolink

Network connections over a USB sound card. A Raspberry Pi Zero 2 W plugs into a
computer (the **host**) and shows up as an ordinary USB audio device; audiolink
carries TCP streams through the audio samples, bit-exact, at up to about 2.7 MB/s
(22 Mbps) each way. The Pi's other side is Wi-Fi, so machines on the network (**clients**) can
reach services on the host, such as SSH, without the host having any network
setup, drivers or admin rights.

```
client ──Wi-Fi/LAN──► Pi Zero 2 W ──USB audio──► host (audiolink host) ──► sshd, ...
```

- **No admin, no drivers, no install** on the host: it's a sound card, and
  audiolink is one file (`.pyz` for any Python 3.9+, or a single binary).
- **The host decides what's reachable** (`--expose 2222:22`); everything else is denied.
- **Find the Pi from anywhere:** it publishes a signed, encrypted record on the
  BitTorrent DHT; clients holding its code look it up.
- **Pairing like Bluetooth:** USB is trusted; over the network, the first machine
  claims an unclaimed Pi, and later ones use a one-time 6-digit code (SPAKE2, so
  the code never crosses the network).
- **Wi-Fi managed from the host,** including captive portals (logged in from a
  browser that browses *as the Pi*), and a setup network the Pi opens whenever
  nobody has been connected to it for a while, next to its Wi-Fi. Changes made over the network are tried first without
  leaving the current network (a wrong password is refused on the spot), then
  made; if nobody can reach the Pi there, its setup network opens.
- **Pure standard-library Python;** the only native code is the bundled PortAudio.

> Status: early. Tested with macOS hosts and a Pi Zero 2 W booting from an SD card.
> Windows builds exist but are untested on hardware;
> a ready-made card image and a relay for reaching the Pi from other networks are next.

## Downloads

Each [release](../../releases) has:

| File | Use it on |
|---|---|
| `audiolink-VER.pyz` | any machine with Python 3.9+: `python3 audiolink-VER.pyz ...` (macOS's built-in `/usr/bin/python3` works over USB, but its old LibreSSL can't reach a Pi over the network) |
| `audiolink-VER-macos-arm64`, `-macos-x86_64` | Macs without Python |
| `audiolink-VER-windows-x64.exe` | Windows |
| `SHA256SUMS` | checksums |

The binaries aren't code-signed yet:
- **macOS:** the first run of a downloaded binary is blocked; allow it in System
  Settings → Privacy & Security, or run `xattr -d com.apple.quarantine audiolink-*`.
  The `.pyz` isn't affected.
- **Windows:** SmartScreen warns about an unknown publisher; choose "Run anyway".

## Quick start

1. **Boot the Pi.** Build its microSD card (`tools/build_card.sh
   config/pi-release.toml --install /Volumes/CARD`, see *Development*) and put it
   in the Pi; it boots from the card and keeps its identity and saved networks in
   the card's `audiolink/` folder. The Pi appears as an audio device.
2. **On the host** (the machine the Pi is plugged into):
   ```
   audiolink host --expose 2222:22          # Pi port 2222 -> this machine's sshd
   audiolink add                            # remember this Pi (named e.g. green-rowan)
   audiolink wifi                           # pick a Wi-Fi network for the Pi
   ```
3. **On a client** on the same network:
   ```
   ssh -p 2222 you@audiopi.local            # mDNS (one Pi per network for now)
   audiolink ssh-config >> ~/.ssh/config    # or: then just `ssh green-rowan`, from anywhere it's reachable
   ```

## Commands

| Command | |
|---|---|
| `audiolink host [--expose PIPORT:PORT] [--wifi NAME]` | run on the host; keeps the link up, reconnects on its own |
| `audiolink add [CODE] [--at ADDR] [--pairing-code N]` | add a Pi: over USB, found on the network, or by connect code |
| `audiolink pis` / `share PI` / `forget PI` / `rename PI NEW [--on-pi]` | the Pis this machine knows |
| `audiolink pairing code PI` | a one-time code for adding PI on another machine |
| `audiolink find PI` / `connect PI PORT` | look a Pi up on the DHT / pipe to it (ssh `ProxyCommand`) |
| `audiolink ssh-config` | `~/.ssh/config` entries for known Pis |
| `audiolink wifi [scan\|status\|list\|join\|forget\|portal\|check\|ap]` | the Pi's Wi-Fi |
| `audiolink discovery [enable\|disable\|status]` | whether the Pi publishes on the DHT |
| `audiolink audio [status\|sweep\|set RATExCH\|forget]` | the link's audio format (see *How it works*) |
| `audiolink update [PYZ] [--revert\|--status] [--pi PI]` | new code for the plugged-in Pi, over the link (kept on its card); `--pi`: over the network |
| `audiolink logs [FILE] [-n N] [--list] [--pi PI]` | the plugged-in Pi's logs, over the link; `--pi`: over the network |
| `audiolink shell [PI] [--key FILE]` | a root shell: the plugged-in Pi, or PI over the network (see *Access*) |
| `audiolink debug [on\|off\|status]` | the plugged-in Pi's debug switch: root access over the network |
| `audiolink rotate-secret` | a new code for the plugged-in Pi: every other computer loses access |
| `audiolink list-devices` / `dht-size` | audio devices / how big the DHT is |

`audiolink --help` and `audiolink COMMAND --help` have the details.

**Reset pins.** Short header pins 39 (GND) and 40 (GPIO21), with a jumper or a
paperclip, and hold. 5 s: back to the code in the Pi's image (drops an update).
15 s: also start over, with a new identity (unclaimed; codes given out stop working)
and no saved Wi-Fi. The green light blinks slowly past 5 s, quickly past 15 s; the
Pi acts when you let go. Shorted while the Pi starts, it runs the image's code.

## Access

What a request needs depends on what it does (level) and how it arrived (route):

| Level | | USB (the host) | LAN | setup network | relay |
|---|---|---|---|---|---|
| public | find the Pi, pair | yes | yes | yes | yes |
| paired | use and manage it, read its logs | yes | yes | yes | yes, but no Wi-Fi changes |
| debug | `shell`, `update` (root) | yes | per the debug switch | per the debug switch | per the debug switch |

A connection the Pi can't place counts as the relay. **Paired** means the machine
holds the Pi's code (`audiolink add`): claim an unclaimed Pi over USB, on its LAN or
on its setup network, or pair with a 6-digit code from a computer that has it
(`audiolink pairing code PI`), or paste its code (`audiolink share PI`).

**Debug switch** (off by default; only the host changes it, `audiolink debug`):

| `debug on --auth` | who gets debug access | routes | ends after |
|---|---|---|---|
| `paired` (default) | computers that added the Pi | LAN, setup network, relay | 24 h |
| `key --key FILE` | holders of these ed25519 keys (`ssh-keygen -t ed25519`; passphrase-protected ones through ssh-agent) | LAN, setup network, relay* | 24 h |
| `none` | anyone | LAN; setup network only with `--setup`; never the relay | 1 h |

\* The relay only carries computers that have the Pi's code. `--for 30m|2h|1d` sets
the time, `--persist` keeps the switch across the Pi's restarts (otherwise it's in
RAM). Turning it off, the time running out and holding the reset pins for 5 s end it
and close open network shells (a new code closes them too). Over USB, `audiolink shell` always
works. The Pi logs every debug request and every refusal.

**Encryption:** every network connection to the Pi (LAN, setup network, relay) is
TLS 1.3 end to end; the relay only passes it on. The Pi's certificate is for its own
identity key, and a computer with its code checks that key, so nobody in between can
read, change or take over a shell, an update or anything else, or pass off a proof
made for them. Older audiolink versions on either side get "update audiolink"
instead; macOS's built-in `/usr/bin/python3` can't do it (use the Mac binary, or
Python from python.org or Homebrew).

**Revoking:** `audiolink rotate-secret` (Pi plugged in) gives the Pi a new code; the
host keeps it, every other computer has to pair again.

Caveats:
- Anyone who can use the host computer can use its control port (127.0.0.1), so has
  root on the Pi.
- The setup network is open Wi-Fi.
- Any paired computer can issue pairing codes, so can let others in.
- Without the Pi's code (`--key` or `none` from a computer that hasn't added it), the
  connection is encrypted but can't be checked: it stops someone listening, not
  someone in between pretending to be the Pi (who still can't use your key on it).
- A network update that breaks networking without crashing leaves USB or the reset
  pins as the way back.

## How it works

- **Wire:** 16-bit PCM is a byte pipe when nothing resamples or mixes it
  (audiolink opens the devices directly; on Windows in WASAPI exclusive mode).
  Frames carry a sync word and CRC; a go-back-N sliding window retransmits
  anything lost.
- **Format negotiation:** every link starts at 48 kHz stereo (192 KB/s). The host
  then asks the Pi to re-present its sound card in faster formats, up to 192 kHz
  x 8 channels (3 MB/s), sends test data both ways, and keeps the fastest that
  works; the Pi drops back to 48 kHz stereo unless the host confirms, and whenever
  no host is connected. Results are remembered per Pi. `--audio-format baseline`
  turns this off.
- **Session:** a HELLO exchange (version, capabilities, the host's clock), a
  control channel (requests, replies, events) and services on top: TCP streams,
  Wi-Fi, identity, pairing, discovery, the setup network.
- **Identity:** each Pi has an ed25519 key and a read secret; its connect code
  (`al1-...`) is the public key plus the secret, enough to find and read its DHT
  record but not to forge it. It's kept on the SD card; the host that added the Pi
  also keeps a custody copy, restored when the Pi comes up without one (no card,
  or a new card).

Code layout: `audiolink/{wire, session.py, services, host, client, discovery}`;
`config/` and `pi/` make the Pi's image (an app on the usb platform, see
*Development*).

## Security notes

- Exposed ports are reachable by anyone who can reach the Pi; they're protected by
  the service itself (e.g. SSH authentication).
- Claiming an unclaimed Pi over the network trusts the first claimant; use USB for
  a guaranteed-safe first contact.
- The SD card holds the Pi's identity and its saved Wi-Fi keys, readable by anyone
  who has the card; treat it like a key.
- The setup network is open; pairing over it is protected by SPAKE2, and Wi-Fi
  passwords sent over the network are encrypted with the Pi's read secret (inside TLS).

## Development

```
python3 build_pyz.py                  # dist/audiolink.pyz
python3 build_bin.py                  # dist/ single-file binary for this OS (needs pyinstaller)
python3 -m unittest discover -s tests -t .
```

`build_pyz.py` is reproducible: the same sources give the same bytes. Under macOS's
`/usr/bin/python3` the tests that need TLS 1.3 with Ed25519 are skipped.

The Pi runs audiolink as an app on the usb platform (Alpine in one initramfs; a
separate, private project: `~/Code/usb`, its README: Apps). Building the card needs
a checkout of it at `USB_PLATFORM` (default: `~/Code/usb`) and its venv. `config/` describes the card:
`pi.toml` (dev: USB ethernet with the Pi at `10.55.0.2`, SSH with
`keys/id_ed25519.pub`, the debug console) and `pi-release.toml` (no USB
ethernet, no SSH). `pi/app-start.sh` is the app's start: USB ethernet, the
daemon's respawn loop (`pi/start-audiolink.sh`), mDNS, Wi-Fi, the clock.

```
tools/build_card.sh [config/pi.toml]  # -> build/card-pi/
tools/deploy.sh [build/card-pi]       # onto a running dev Pi's card over USB ethernet, then reboot
tools/hot.sh                          # just the daemon, no reboot (lost at the next boot)
tools/smoke.sh                        # end-to-end check of the host daemon and the Pi
```

With a card reader: `tools/build_card.sh config/pi.toml --install /Volumes/CARD`
(replaces the card's system files; its `audiolink/` state and `logs/` stay). Without one: a
running dev Pi takes a new card over ssh (`tools/deploy.sh`); otherwise expose the
card through the Pi with `rpiboot`'s mass-storage mode (the platform's README:
*Build and install the card*) and install onto it the same way.
State (identity, saved networks, updates) is in the card's `audiolink/`; logs
in `logs/last` and `logs/prev`.

**Keys** (`keys/`, never committed):
- `id_ed25519`: SSH into dev cards. `ssh-keygen -t ed25519 -f keys/id_ed25519 -N ''`
  before the first dev build.
- `relay.crt`, `relay.key`: the relay's certificate, made once:
  `openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout keys/relay.key -out keys/relay.crt -days 36500 -subj "/CN=relay.lyuhau.com"`.
  Clients and Pis pin it: `PIN` in `audiolink/relay/__init__.py` is the sha256 of
  its DER form (`openssl x509 -in keys/relay.crt -outform der | shasum -a 256`).
- `relay_ed25519`: root SSH on the relay server, for `tools/relay_deploy.sh`.

**The relay** (`relay.lyuhau.com`, an Ubuntu box): `tools/relay_deploy.sh` puts the
current code and certificate there and restarts the service only if something
changed (a restart drops every relayed session). On the box:
`systemctl status audiolink-relay`, `journalctl -u audiolink-relay`,
`python3 /opt/audiolink/audiolink.pyz relay --status`. A new certificate (or pin)
cuts off everything that has the old pin: ship code that accepts the new one to all
clients and Pis first, then swap it.

**Gotchas:**
- Run the host daemon from a copy (`cp dist/audiolink.pyz build/host.pyz`): every
  build rewrites `dist/audiolink.pyz` under it.
- `hot.sh`, `deploy.sh` and `smoke.sh` reach the Pi over USB ethernet; over Wi-Fi:
  `PI=root@ADDRESS tools/hot.sh`. An update on the card (`audiolink update`) runs
  instead of the code they install; `hot.sh` and `deploy.sh` say so and offer to remove it.
- Many daemon restarts or USB rebinds in a row can wedge the Pi's USB until it's
  replugged.
- macOS asks to "Allow accessory" whenever the Pi shows up with a new USB
  configuration (e.g. a release card after a dev one).

**Release:**
1. Bump `__version__` in `audiolink/__init__.py` (the release workflow refuses a
   tag that doesn't match it).
2. Run the tests (also under `/usr/bin/python3`); build both cards
   (`tools/build_card.sh`, `tools/build_card.sh config/pi-release.toml`) and try
   them on a Pi.
3. `git tag vX.Y.Z && git push origin main vX.Y.Z`.
4. Check the release's files: the macOS and Windows binaries, the `.pyz` (also
   what `audiolink update` sends to a Pi) and `SHA256SUMS`. No card image while
   the platform is private.

## License

MIT. Bundles PortAudio (MIT-style license, `audiolink/wire/_lib/PORTAUDIO-LICENSE.txt`).
