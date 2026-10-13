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

> Status: early. Tested with macOS hosts and a Pi Zero 2 W booting from an SD card
> (or over USB with `rpiboot`). Windows builds exist but are untested on hardware;
> a ready-made card image and a relay for reaching the Pi from other networks are next.

## Downloads

Each [release](../../releases) has:

| File | Use it on |
|---|---|
| `audiolink-VER.pyz` | any machine with Python 3.9+: `python3 audiolink-VER.pyz ...` (macOS's built-in `/usr/bin/python3` works) |
| `audiolink-VER-macos-arm64`, `-macos-x86_64` | Macs without Python |
| `audiolink-VER-windows-x64.exe` | Windows |
| `audiolink-pi-VER.zip` | the Pi's boot files |
| `SHA256SUMS` | checksums |

The binaries aren't code-signed yet:
- **macOS:** the first run of a downloaded binary is blocked; allow it in System
  Settings → Privacy & Security, or run `xattr -d com.apple.quarantine audiolink-*`.
  The `.pyz` isn't affected.
- **Windows:** SmartScreen warns about an unknown publisher; choose "Run anyway".

## Quick start

1. **Boot the Pi.** Copy the boot files (`audiolink-pi-VER.zip`, unzipped) onto a
   FAT32-formatted microSD card and put it in the Pi; it boots from the card and
   keeps its identity and saved networks in the card's `audiolink/` folder. Without
   a card, serve the same files with Raspberry Pi's
   [`rpiboot`](https://github.com/raspberrypi/usbboot) (`rpiboot -d bootdir`); it
   then runs from RAM and the host restores its identity after each reboot. The
   Pi appears as an audio device.
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
| `audiolink update [PYZ] [--revert\|--status]` | new code for the plugged-in Pi, over the link (kept on its card) |
| `audiolink logs [FILE] [-n N] [--list]` | the plugged-in Pi's logs, over the link |
| `audiolink list-devices` / `dht-size` | audio devices / how big the DHT is |

`audiolink --help` and `audiolink COMMAND --help` have the details.

**Reset pins.** Short header pins 39 (GND) and 40 (GPIO21), with a jumper or a
paperclip, and hold. 5 s: back to the code in the Pi's image (drops an update).
15 s: also start over, with a new identity (unclaimed; codes given out stop working)
and no saved Wi-Fi. The green light blinks slowly past 5 s, quickly past 15 s; the
Pi acts when you let go. Shorted while the Pi starts, it runs the image's code.

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
`build_initramfs.py` builds the Pi's RAM-only Alpine image.

## Security notes

- Exposed ports are reachable by anyone who can reach the Pi; they're protected by
  the service itself (e.g. SSH authentication).
- Claiming an unclaimed Pi over the network trusts the first claimant; use USB for
  a guaranteed-safe first contact.
- The SD card holds the Pi's identity and its saved Wi-Fi keys, readable by anyone
  who has the card; treat it like a key.
- The setup network is open; pairing over it is protected by SPAKE2, and Wi-Fi
  passwords sent over the network are encrypted with the Pi's read secret.

## Development

```
python3 build_pyz.py                  # dist/audiolink.pyz
python3 build_bin.py                  # dist/ single-file binary for this OS (needs pyinstaller)
python3 -m unittest discover -s tests -t .
```

The Pi runs audiolink as an app on the usb platform (Alpine in one initramfs;
its README: Apps), from a checkout at `USB_PLATFORM` (default `~/Code/usb-initramfs`). `config/` describes the card:
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
(replaces everything on the card, its `audiolink/` state too).
State (identity, saved networks, updates) is in the card's `audiolink/`; logs
in `logs/last` and `logs/prev`.

Being replaced (kept until the platform image is checked on hardware):
`build_initramfs.py`, `pi/init`, `deploy_card.sh`, `fetch_firmware.sh` (Raspberry
Pi kernel 6.12, booted over USB with `rpiboot` or from the card).

## License

MIT. Bundles PortAudio (MIT-style license, `audiolink/wire/_lib/PORTAUDIO-LICENSE.txt`).
