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
  audiolink is one file (`.pyz` for any Python 3.9+) or one folder (no Python needed).
- **The host decides what's reachable** (`--expose 2222:22`); everything else is denied.
- **Pairing like Bluetooth:** USB is trusted; over the network, the first machine
  claims an unclaimed Pi, and later ones use a one-time 6-digit code (SPAKE2, so
  the code never crosses the network).
- **Find it from anywhere:** through its relay (`relay.lyuhau.com`), or its
  record on the BitTorrent DHT.
- **Wi-Fi managed from the host,** including sign-in pages (loaded in a browser that
  browses *as the Pi*), and an open setup network the Pi opens whenever nobody has
  been connected to it for a while, next to its Wi-Fi. Changes made over the network
  are tried first without leaving the current network (a wrong password is refused on
  the spot), then made; if nobody can reach the Pi there, its setup network opens.
- **Pure standard-library Python;** the only native code is the bundled PortAudio.

> Status: early. Tested with macOS hosts and a Pi Zero 2 W booting from an SD card.
> Windows builds exist but are untested on hardware. Each release has a card image
> (see *Where card images come from*). The relay (`relay.lyuhau.com`), for reaching a
> Pi from other networks, is running.

## Quick start: got a ready Pi? Start here

You need the Pi with its card, a USB data cable to its port marked **USB** (not PWR
IN; it powers the Pi too), and audiolink on each computer you'll use.

**1. Get audiolink.** From the latest [release](../../releases):

| Your computer | File | |
|---|---|---|
| Mac with Apple silicon | `audiolink-VER-macos-arm64.zip` | no Python needed: a folder `audiolink` with the program in it |
| Intel Mac | `audiolink-VER-macos-x86_64.zip` | |
| Windows | `audiolink-VER-windows-x64.zip` | |
| Linux, or anything with Python 3.9+ | `audiolink-VER.pyz` | one file; run it as `python3 audiolink-VER.pyz ...`; a Linux host also needs PortAudio (e.g. `libportaudio2`) |

`SHA256SUMS` has the checksums. Unzip it and keep the `audiolink` folder somewhere
permanent: the ssh entries from `audiolink ssh-config` run the program from where it is.
On a Mac:

```
cd ~/Downloads && unzip audiolink-*-macos-*.zip                     # or double-click it (Safari may have)
mkdir -p ~/Applications && mv audiolink ~/Applications/
xattr -dr com.apple.quarantine ~/Applications/audiolink             # not code-signed yet
mkdir -p ~/bin && ln -s ~/Applications/audiolink/audiolink ~/bin/   # ~/bin on your PATH, or use the full path
```

Without the `xattr`, macOS blocks the first run; allowing it in System Settings →
Privacy & Security works too. The first run takes a few seconds (macOS checks the new
files); later ones start at once. To update, replace the folder; the link stays.

On Windows, extract it (Extract All) and move the `audiolink` folder to, say,
`C:\Tools\audiolink`; add that folder to your PATH (Settings: search "environment
variables" → Edit environment variables for your account → Path), or run
`C:\Tools\audiolink\audiolink.exe`. If SmartScreen warns about an unknown publisher,
choose "Run anyway".

Apple's own `/usr/bin/python3` runs the `.pyz` over USB but can't do the network
commands (its LibreSSL is too old): use the zip, or Python from python.org or Homebrew.

**2. Plug the Pi in and start the host.** On the computer the Pi is plugged into:

```
audiolink host
```

Keep it running in its own terminal window; the other commands go through it. Once
the Pi has started it says `Connected to NAME` (each Pi has a name like
`green-rowan`). The first time, macOS may ask to "Allow accessory" (allow it), and
asks whether your terminal may use the microphone: allow that too, it's how data
comes in from the Pi. The computer has to stay awake (on a Mac:
`caffeinate -i audiolink host`).

**3. Put the Pi on Wi-Fi.** In another terminal on the same computer:

```
audiolink add                      # remember the Pi on this computer
audiolink wifi                     # pick a network from a list; asks for its password
```

The Pi uses 2.4 GHz networks. If a network needs a sign-in page (hotels, cafés),
`audiolink wifi` offers it; later, `audiolink wifi portal`.

**4. Add the Pi on your other computers.** On the host: `audiolink pairing code
NAME` gives a one-time 6-digit code. On the other computer, on the same network as
the Pi (or on its setup network, see *Troubleshooting*): `audiolink add`, which finds
the Pi and asks for the code. Not on the same network: `audiolink share NAME` on the
host prints the Pi's connect code (`al1-...`); `audiolink add al1-...` on the other
computer. Treat that code like a password: whoever has it can use the Pi.

**5. Reach the host computer from anywhere.** Stop the host (Ctrl-C) and start it
again with a port for ssh:

```
audiolink host --expose 2222:22    # the Pi's port 2222 -> this computer's ssh
```

and turn on its ssh server: on a Mac, System Settings → General → Sharing → Remote
Login; on Windows, Settings → System → Optional features → OpenSSH Server, then start
the `sshd` service (both need an admin, once). Then on the other computer:

```
audiolink ssh-config --user YOU >> ~/.ssh/config    # YOU: your login on the host computer
ssh NAME
```

That logs in to the computer the Pi is plugged into, not the Pi: directly when you're
on the Pi's network, through the relay otherwise. `audiolink find NAME` says whether
the Pi is online. In Windows PowerShell, append with
`audiolink ssh-config --user YOU | Out-File -Append -Encoding ascii $HOME\.ssh\config`
(its `>>` writes UTF-16, which ssh can't read).

### Browse as the Pi

`audiolink host` also runs an HTTP proxy at `127.0.0.1:47801` (this computer only)
whose connections leave from the Pi, over its Wi-Fi. Sign-in pages need it (`audiolink
wifi portal` opens a browser that uses it), and it's handy for checking what the Pi's
network can reach: `curl -x http://127.0.0.1:47801 https://example.com`, or set
`127.0.0.1:47801` as a browser's HTTP and HTTPS proxy. `--proxy-port 0` turns it off.

### Keep `audiolink host` running

To start it at login and restart it if it stops, instead of a terminal window (stop
the one in the terminal first: only one host runs at a time):

**macOS:** save as `~/Library/LaunchAgents/com.audiolink.host.plist` (with your paths),
then `launchctl load ~/Library/LaunchAgents/com.audiolink.host.plist`. `caffeinate -i`
keeps the Mac from sleeping while it runs. If it doesn't connect, look in
`/tmp/audiolink-host.log`, and under System Settings → Privacy & Security →
Microphone: a program started this way may need its own permission.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.audiolink.host</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>/Users/you/bin/audiolink</string><string>host</string>
    <string>--expose</string><string>2222:22</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/audiolink-host.log</string>
  <key>StandardErrorPath</key><string>/tmp/audiolink-host.log</string>
</dict>
</plist>
```

`launchctl unload` with the same path stops it.

**Linux:** save as `~/.config/systemd/user/audiolink-host.service`, then
`systemctl --user enable --now audiolink-host` (`journalctl --user -u audiolink-host`
for its output; `loginctl enable-linger $USER` to run it without logging in).

```ini
[Unit]
Description=audiolink host

[Service]
ExecStart=/usr/bin/python3 %h/bin/audiolink.pyz host --expose 2222:22
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

**Windows:** a task that starts it at logon, in its own window:
`schtasks /Create /SC ONLOGON /TN audiolink-host /TR "C:\Tools\audiolink.exe host --expose 2222:22"`
(`schtasks /Delete /TN audiolink-host` removes it).

### The card's settings

A few things can change without a new card image: `settings.txt` at the top of the
Pi's card. Put the card in any computer (Mac, Windows, Linux: it's a plain FAT32
card), edit the file in a text editor, and put it back in the Pi. Each setting is
there commented out (`#`), with its built-in value and what it does; remove the `#`
to change it:

| Setting | | Built-in |
|---|---|---|
| `audiolink.relay` | the relay the Pi stays reachable through: `HOST:PORT`, `HOST:PORT#PIN` (a relay with another certificate) or `off` | `relay.lyuhau.com:443` |
| `audiolink.expose_setup` | `on`: exposed ports take connections from the setup network too | `off` |
| `console` | `on`: a root shell over USB, as an extra serial port next to the audio (no password: for troubleshooting) | `off` (dev cards: `on`) |
| `hostname` | the Pi's name on its network (`NAME.local`) | `audiopi` |
| `usb.vendor_id`, `usb.product_id` | the USB IDs, hex | `0x1d6b`, `0x0104` |
| `usb.manufacturer`, `usb.product`, `usb.serial` | what the computer shows for the device | `audiolink`, `AudioPi`, `audiopi0001` |

A setting that's misspelt or not valid is ignored: the Pi starts with the built-in
value and says why in `/var/log/settings.log` (`audiolink logs settings.log`). New
USB IDs or strings make the computer see a new device (on a Mac: "Allow accessory"
again). The release card has no SSH server, so it ignores an `ssh_authorized_keys`
file; dev cards add its keys to root's. Installing a new image onto the card keeps
`settings.txt` as it is and adds any new settings at its end.

### Where card images come from

Each [release](../../releases) has the Pi's card as `audiolink-pi-VER.zip`: the
files that go on a microSD card (any size; 8 GB is plenty), built in CI from
`config/pi-release.toml` with the [pi-gadget-kit](https://github.com/lyuhau/pi-gadget-kit)
(`audiolink-pi-VER.manifest` lists their sha256). One image is meant for everyone:
what differs between Pis is on the card (its state, and `settings.txt`).

1. **Format the card FAT32** (MBR). This erases it. Many cards up to 32 GB come
   FAT32 already; then just empty it.
   - macOS: find the card with `diskutil list external`, then
     `diskutil eraseDisk FAT32 AUDIOPI MBRFormat /dev/diskN`.
   - Windows (PowerShell, as admin; `E` is the card's drive letter):
     `Format-Volume -DriveLetter E -FileSystem FAT32 -NewFileSystemLabel AUDIOPI`.
     Windows won't format cards over 32 GB as FAT32: use Raspberry Pi Imager's
     *Erase* instead.
   - Linux: find the card with `lsblk`, then (it erases `/dev/sdX`)
     `printf 'label: dos\n,,c\n' | sudo sfdisk /dev/sdX && sudo mkfs.vfat -F 32 -n AUDIOPI /dev/sdX1`,
     and mount it (e.g. reinsert it).
2. **Unzip onto it**, so `config.txt` is at the top of the card:
   - macOS: `unzip audiolink-pi-VER.zip -d /Volumes/AUDIOPI`
   - Windows: `Expand-Archive audiolink-pi-VER.zip -DestinationPath E:\`
   - Linux: `unzip audiolink-pi-VER.zip -d /media/$USER/AUDIOPI`
3. Optionally edit its `settings.txt` (*The card's settings*). Eject the card
   (macOS: `diskutil eject /Volumes/AUDIOPI`; if it says the disk is busy, Spotlight
   is looking at the new card: wait a few seconds and eject again) and put it in the Pi.

**A newer image on a used card:** unzip over it without replacing `settings.txt`
(`unzip -o audiolink-pi-VER.zip -x settings.txt -d /Volumes/AUDIOPI`; on Windows,
keep a copy of `settings.txt` and put it back). The Pi's state (`audiolink/`) and
logs (`logs/`) stay. New settings the old `settings.txt` doesn't mention keep their
built-in values. Code alone updates without a new card: `audiolink update`.

## Commands

| Command | |
|---|---|
| `audiolink host [--expose PIPORT:PORT] [--wifi NAME]` | run on the host; keeps the link up, reconnects on its own |
| `audiolink add [CODE] [--at ADDR] [--pairing-code N]` | add a Pi: over USB, found on the network, or by connect code |
| `audiolink pis` / `share PI` / `forget PI` / `rename PI NEW [--on-pi]` | the Pis this machine knows |
| `audiolink pairing code PI` | a one-time code for adding PI on another machine |
| `audiolink find PI` | online or offline (on its relay, or this network), and where (its DHT record) |
| `audiolink connect PI PORT` | pipe to a Pi port (ssh `ProxyCommand`): this network, then the relay |
| `audiolink ssh-config [--user NAME]` | `~/.ssh/config` entries for known Pis (logging in to the computer each is plugged into) |
| `audiolink wifi [scan\|status\|list\|join\|forget\|portal\|check\|ap]` | the Pi's Wi-Fi; `join`, `status`, `check` also over the network (`--pi PI`); `check` exits 0 online, 3 sign-in page, 4 no internet |
| `audiolink discovery [status\|enable\|disable\|publish\|code]` | whether the plugged-in Pi publishes where it is on the DHT (the relay works either way) |
| `audiolink audio [status\|sweep\|set RATExCH\|forget]` | the link's audio format (see *How it works*) |
| `audiolink update [PYZ] [--revert\|--status] [--pi PI]` | new code for the plugged-in Pi, over the link (kept on its card); `--pi`: over the network |
| `audiolink logs [FILE] [-n N] [--list] [--pi PI] [--key FILE]` | the plugged-in Pi's logs, over the link; `--pi`: over the network |
| `audiolink shell [PI] [--key FILE] [-- CMD]` | a root shell: the plugged-in Pi, or PI over the network (see *Access*); `-- CMD` runs one command |
| `audiolink debug [on\|off\|status]` | the plugged-in Pi's debug switch: root access over the network |
| `audiolink rotate-secret` | a new code for the plugged-in Pi: every other computer loses access |
| `audiolink list-devices` / `dht-size` | audio devices / how big the DHT is |

`audiolink --help` and `audiolink COMMAND --help` have the details. Commands that talk
to the plugged-in Pi need `audiolink host` running on that computer.

**Reset pins.** Short header pins 39 (GND) and 40 (GPIO21), with a jumper or a
paperclip, and hold. 5 s: back to the code in the Pi's image (drops an update).
15 s: also start over, with a new identity (unclaimed; codes given out stop working)
and no saved Wi-Fi. The green light blinks slowly past 5 s, quickly past 15 s; the
Pi acts when you let go. Shorted while the Pi starts, it runs the image's code.

## Troubleshooting

- **"No Pi plugged in."** Is `audiolink host` running on this computer, and does it
  say `Connected`? Is the cable in the Pi's USB port (not PWR), and is it a data
  cable? On a Mac, look for an "Allow accessory" prompt (it comes back when the Pi
  shows up as a new kind of USB device, e.g. after a new card). Then unplug and replug.
  `audiolink list-devices` shows whether the computer sees the Pi's sound devices.
- **"The Pi's sound device dropped off USB."** Unplug it and plug it back in.
- **Slow, or the link keeps dropping.** Back to the starting audio format:
  `audiolink audio set baseline` (`audiolink host --audio-format baseline` keeps it
  there); `audiolink audio sweep` measures the others again.
- **Wi-Fi.** `audiolink wifi status` says where the Pi is. It only sees 2.4 GHz
  networks. Sign-in pages: `audiolink wifi portal`. When nobody has been connected to
  it for a while, the Pi opens its own open setup network, `audiopi-NAME`: join it
  from any computer and `audiolink add`, `audiolink wifi join NETWORK --pi NAME`
  work there.
- **Can't reach it from elsewhere.** `audiolink find NAME`: "offline" means the Pi
  isn't on the internet (check its Wi-Fi on the host: `audiolink wifi check`);
  "online" with ssh failing means the host isn't running with `--expose 2222:22`, or
  its ssh server is off.
- **An update broke it.** Over USB: `audiolink update --revert`. Or hold the reset
  pins for 5 s (back to the image's code), 15 s to also start over. With a card
  reader, the card's `logs/last` (and `logs/prev`, the boot before) has the Pi's logs,
  and `audiolink/` its state: deleting `audiolink/audiolink.pyz` drops an update.
- **Looking inside.** Over USB, `audiolink shell` is a root shell on the Pi and
  `audiolink logs` shows its logs. Over the network, `audiolink logs --pi NAME` works
  from any computer that added it; a shell needs the debug switch: `audiolink debug
  on` on the host, then `audiolink shell NAME` elsewhere. When audiolink itself
  doesn't start, `console = on` in the card's `settings.txt` adds a serial root shell
  (`/dev/cu.usbmodem*` on a Mac, 115200 baud) next to the audio.

## Access

What a request needs depends on what it does (level) and how it arrived (route):

| Level | | USB (the host) | LAN | setup network | relay |
|---|---|---|---|---|---|
| public | find the Pi, pair | yes | yes | yes | yes |
| paired | use and manage it, read its logs (debug access can too) | yes | yes | yes | yes, but no Wi-Fi changes |
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
the time, `--persist` keeps the switch across the Pi's restarts (otherwise it lasts
until the Pi restarts; audiolink restarting, as after a network update, keeps it). Turning it off, the time running out and holding the reset pins for 5 s end it
and close open network shells; so do a new code and narrowing the switch (another mode,
a key taken off, no more `--setup`). Over USB, `audiolink shell` always works. The Pi logs every debug request and every refusal.

**Encryption:** every audiolink connection to the Pi over the network (LAN, setup
network, relay) is TLS 1.3 end to end; the relay only passes it on. An exposed port
carries whatever its service sends: as is on the Pi's network, and through the relay
over TLS to the relay and from it to the Pi (the relay itself could read it). ssh
encrypts itself. The Pi's certificate is for its own
identity key, and a computer with its code checks that key, so nobody in between can
read, change or take over a shell, an update or anything else, or pass off a proof
made for them. Older audiolink versions on either side get "update audiolink"
instead; macOS's built-in `/usr/bin/python3` can't do it (use the Mac zip, or
Python from python.org or Homebrew).

**Revoking:** `audiolink rotate-secret` (Pi plugged in) gives the Pi a new code; the
host keeps it, every other computer has to pair again.

Caveats:
- Anyone who can use the host computer can use its control port (127.0.0.1), so has
  root on the Pi.
- The setup network is open Wi-Fi. Exposed ports don't take connections from it (the
  Pi's `--expose-setup` lets them).
- An exposed port is open to anyone who can reach the Pi on its LAN, and the service
  behind it sees every connection come from the host itself (127.0.0.1).
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
`config/` and `pi/` make the Pi's image (an app on pi-gadget-kit, see
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
python3 build_bin.py                  # dist/audiolink-OS-ARCH.zip for this OS (needs pyinstaller)
python3 -m unittest discover -s tests -t .
```

`build_pyz.py` is reproducible: the same sources give the same bytes. Under macOS's
`/usr/bin/python3` the tests that need TLS 1.3 with Ed25519 are skipped.

The Pi runs audiolink as an app on the [pi-gadget-kit](https://github.com/lyuhau/pi-gadget-kit)
(Alpine in one initramfs; its README: Apps), a git submodule at `platform/`, pinned to
the version audiolink was tested with: clone with `git clone --recursive`, or run
`git submodule update --init`. Building the card needs Python 3.11+ (or 3.9+ with
`tomli`) and, once, the platform's patched gadget modules:
`platform/alpine-module/fetch.sh` downloads the prebuilt ones (no Docker; the release
workflow does this too), `platform/alpine-module/build.sh` builds them (Docker). `PLATFORM` points the build at another
checkout of the platform, e.g. one you're changing. A card
for someone else: `tools/build_card.sh config/pi-release.toml --install /Volumes/CARD`
(a FAT32-formatted microSD card, see the platform's README); the Pi boots from it. `config/` describes the card:
`pi.toml` (dev: USB ethernet with the Pi at `10.55.0.2`, SSH with
`keys/id_ed25519.pub`, the debug console) and `pi-release.toml` (no USB
ethernet, no SSH, the debug console off). `pi/app-start.sh` is the app's start: USB ethernet, the
daemon's respawn loop (`pi/start-audiolink.sh`), mDNS, Wi-Fi, the clock.
`pi.toml`'s `[app.settings]` are the card's `audiolink.*` settings (*The card's
settings*); `pi/card-settings.sh` checks them and turns them into `audiolink pi`
arguments, after `/etc/audiolink.args` (the build's).

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
   them on a Pi. The release card has to build without `keys/` (CI has none).
   A changed `config/pi-release.packages.lock` goes in a commit first: the release
   workflow refuses a card whose packages differ from it (Alpine moved on; rebuild,
   review the diff, try the card).
3. `git tag vX.Y.Z && git push origin main vX.Y.Z`.
4. Check the release's files: the macOS and Windows zips, the `.pyz` (also
   what `audiolink update` sends to a Pi), the card (`audiolink-pi-VER.zip`, with
   its `.manifest`) and `SHA256SUMS`. Unzip the card onto a microSD card and boot a
   Pi from it.

## License

MIT. Bundles PortAudio (MIT-style license, `audiolink/wire/_lib/PORTAUDIO-LICENSE.txt`).
