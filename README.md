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
- **The Pi decides who gets through:** it keeps a list of computers, each with its own
  key, as a **user** (through to what the host exposes) or an **owner** (that, and
  managing the Pi). The computer it's plugged into claims it first, over USB; others
  join with a one-use 6-digit invite (SPAKE2, so the code never crosses the network).
- **Reachable from anywhere** through its relay (`relay.lyuhau.com`), over TLS that
  ends at the Pi.
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

**Start here: `audiolink setup`.** Get audiolink (step 1 below), plug the Pi in, and
run `audiolink setup` on that computer: it installs audiolink somewhere permanent,
claims the Pi (this computer becomes its first owner), puts it on Wi-Fi, and offers to
make that computer its host (its ssh reachable through the Pi; then keep `audiolink
host` running) and to set up `ssh NAME` there. For each other computer, run `audiolink
invite NAME` on an owner computer (NAME: what to call the other computer; `--owner`
to make it an owner too): it gives a one-use 6-digit code. Then `audiolink setup` on
the other computer finds the Pi on the network and asks for the code. Run setup again
any time to see what's set or change it; `audiolink setup --help` lists a flag for each
question. The steps below are what it does, by hand.

You need the Pi with its card, a USB data cable to its port marked **USB** (not PWR
IN; it powers the Pi too), and audiolink on each computer you'll use.

**No card yet?** On a Mac, the Pi writes its own card. Install rpiboot once (`brew
install rpiboot`), plug the Pi in with a blank card in it (or none: put the card in once
it's plugged in), and run `audiolink setup`. The Pi waits in its USB boot mode; setup
asks before erasing the card, downloads this version's card image, checks it, writes
it, and then asks you to replug the Pi. With a card reader instead: `audiolink card
write` (macOS; see *Write, back up and restore the card*), or by hand on any computer
(*Where card images come from*). Any microSD card works; 8 GB is plenty.

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

On Windows, extract it (Extract All) and run `audiolink.exe setup` from the extracted
folder: it copies itself to `%LOCALAPPDATA%\Programs\audiolink`. Add that folder to
your PATH (Settings: search "environment variables" → Edit environment variables for
your account → Path), or run `%LOCALAPPDATA%\Programs\audiolink\audiolink.exe`. If
SmartScreen warns about an unknown publisher, choose "Run anyway".

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

**3. Claim the Pi and put it on Wi-Fi.** A new Pi has no owner. Claiming happens
only over USB, through `audiolink setup` on the computer it's plugged into (`setup
--no-wifi --no-host --no-client` does just that, and asks for the Pi's name). Then, in another
terminal on the same computer:

```
audiolink wifi                     # pick a network from a list; asks for its password
```

The Pi uses 2.4 GHz networks. If a network needs a sign-in page (hotels, cafés),
`audiolink wifi` offers it; later, `audiolink wifi portal`.

**4. Add your other computers.** On an owner computer (the host, so far):

```
audiolink invite laptop            # laptop: what the Pi's list will call that computer
audiolink invite desktop --owner   # an owner too: manages the Pi, invites others
```

Each gives a one-use 6-digit code (5 minutes, 3 tries). On the other computer, on the
same network as the Pi (or on its setup network, see *Troubleshooting*): `audiolink
setup`, which finds the Pi and asks for the code. Not on the same network: `audiolink
setup --code al2-...`, with the Pi's code from `audiolink pis NAME` on a computer that
has it; it joins through the relay. The Pi's code only finds and identifies the Pi:
without an invite it lets nobody in.

**5. Reach the host computer from anywhere.** Stop the host (Ctrl-C) and start it
again with a port for ssh:

```
audiolink host --expose 2222:22    # the Pi's port 2222 -> this computer's ssh
```

and turn on its ssh server: on a Mac, System Settings → General → Sharing → Remote
Login; on Windows, Settings → System → Optional features → OpenSSH Server, then start
the `sshd` service (both need an admin, once). Then on the other computer:

```
audiolink ssh-config >> ~/.ssh/config    # setup keeps these entries for you instead
ssh NAME                                 # or ssh YOU@NAME: YOU, your login on the host computer
```

That logs in to the computer the Pi is plugged into, not the Pi: directly when you're
on the Pi's network, through the relay otherwise. The host's ssh server decides who
logs in, as always: this computer's ssh key goes in its `authorized_keys`
(`ssh-copy-id NAME` does it if the host takes a password; audiolink never edits that
file). `audiolink pis NAME` says whether the Pi is online. In Windows PowerShell,
append with `audiolink ssh-config | Out-File -Append -Encoding ascii $HOME\.ssh\config`
(its `>>` writes UTF-16, which ssh can't read).

Some ssh clients can't run the `ProxyCommand` those entries use (SSHFS-Win's own ssh,
apps with their own ssh). For them, `audiolink forward NAME 2222` listens on
`127.0.0.1:2222` (this computer only) and sends each connection to the Pi's port 2222;
it prints the `ssh -p 2222 -o HostKeyAlias=... USER@127.0.0.1` to use. Leave it running:
each connection finds the Pi again, so a restart of the host ends only the connections
open then (and a new one keeps trying for 20 s while the host comes back).
`--local PORT` picks another port here.

### Browse as the Pi

`audiolink host` also runs an HTTP proxy at `127.0.0.1:47801` (this computer only)
whose connections leave from the Pi, over its Wi-Fi. Sign-in pages need it (`audiolink
wifi portal` opens a browser that uses it), and it's handy for checking what the Pi's
network can reach: `curl -x http://127.0.0.1:47801 https://example.com`, or set
`127.0.0.1:47801` as a browser's HTTP and HTTPS proxy. `--proxy-port 0` turns it off.

### Keep `audiolink host` running

To start it at login and restart it if it stops, instead of a terminal window:
`audiolink setup host --autostart` sets this up (`--no-autostart` takes it out again), with what
it exposes saved in audiolink's settings (`host.json`), which `audiolink host` reads
(flags given to it win). By hand (stop the host in the terminal first: only one host
runs at a time):

**macOS:** save as `~/Library/LaunchAgents/com.audiolink.host.plist` (with your paths),
then `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.audiolink.host.plist`.
`caffeinate -i` keeps the Mac from sleeping while it runs. If it doesn't connect, look in
`~/Library/Logs/audiolink-host.log`, and under System Settings → Privacy & Security →
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
  <key>StandardOutPath</key><string>/Users/you/Library/Logs/audiolink-host.log</string>
  <key>StandardErrorPath</key><string>/Users/you/Library/Logs/audiolink-host.log</string>
</dict>
</plist>
```

`launchctl bootout gui/$(id -u)/com.audiolink.host` stops it.

**Linux:** save as `~/.config/systemd/user/audiolink-host.service`, then
`systemctl --user enable --now audiolink-host` (`journalctl --user -u audiolink-host`
for its output; `loginctl enable-linger $USER` to run it without logging in).

```ini
[Unit]
Description=audiolink host

[Service]
ExecStart=%h/.local/bin/audiolink host --expose 2222:22
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

**Windows:** a task that starts it at logon, in its own window:
`schtasks /Create /SC ONLOGON /TN audiolink-host /TR "%LOCALAPPDATA%\Programs\audiolink\audiolink.exe host --expose 2222:22"`
in Command Prompt (`schtasks /Delete /TN audiolink-host` removes it).

### The card's settings

A few things can change without a new version: `settings.txt` at the top of the
Pi's card. With the Pi connected, `audiolink settings` lists them and
`audiolink settings KEY=VALUE` changes them (owners only; `--unset KEY`: back to the
built-in value; `--reboot`: now, instead of at the next start; `--pi PI`: over the
network). Or put the card in any computer (Mac, Windows, Linux: it's a plain FAT32
card), edit the file in a text editor, and put it back in the Pi. Each setting is
there commented out (`#`), with its built-in value and what it does; remove the `#`
to change it:

| Setting | | Built-in |
|---|---|---|
| `audiolink.relay` | the relay the Pi stays reachable through: `HOST:PORT`, `HOST:PORT#PIN` (a relay with another certificate) or `off` | `relay.lyuhau.com:443` |
| `audiolink.expose_setup` | no effect now: exposed ports take connections from computers on the Pi's list, on every network, the setup network included | `off` |
| `console` | `on`: a root shell over USB, as an extra serial port next to the audio (no password: for troubleshooting) | `off` (dev cards: `on`) |
| `hostname` | the Pi's name on its network (`NAME.local`) | `audiopi` |
| `flavor` | what the Pi presents on USB (see *Flavors*) | `audiolink` |

A setting that's misspelt or not valid is ignored: the Pi starts with the built-in
value and says why in `/var/log/settings.log` (`audiolink logs settings.log`). The
`usb.*` settings are gone: a flavor names the device now (cards that still set them
get a note in `settings.log`). The release card has no SSH server, so it ignores an `ssh_authorized_keys`
file; dev cards add its keys to root's. A new version keeps the settings someone set,
and renews the rest from the new version's.

### Flavors

What the Pi presents on USB is its *flavor*: audiolink's own sound device (the
`audiolink` flavor, the fastest link), or another USB device. Switching is live, no
new version or reboot (owners only):

```
audiolink flavor                  what the Pi is, and what it could be
audiolink flavor use NAME         become that (kept if the link comes back)
audiolink flavor add FILE         add one (a .toml someone gave you, or one you made)
audiolink flavor try FILE         try one without keeping it
audiolink flavor remove NAME      remove one you added
```

Built-in flavors come with the version; yours are added to the Pi's card
(`flavors/` at its top, kept by updates and backups). A flavor that doesn't plan is
refused when you add it, with the reason. `use` and `try` warn first that the computer
may ask to allow a new accessory (on a Mac: "Allow accessory"; allow it), then wait:
the Pi keeps the new flavor once this computer's link comes back on it, within a
minute, or else goes back to the one it was by itself. A kept `use` is the Pi's
`flavor` setting from then on; `try` lasts until the Pi restarts. Over the network
(`--pi PI`) a computer must be plugged into the Pi to confirm a switch; without one,
`audiolink settings flavor=NAME --pi PI --reboot` changes it unchecked. The reset pins
(5 s) bring the default flavor back.

The computer that adds or switches to a flavor learns its USB ID (`host.json`'s
`usb_ids`), so it keeps finding the Pi; other computers learn it from the Pi when
they reach it over the network. A flavor is a TOML file (pi-gadget-kit's README:
*Hand-written devices*), or one made from a real device's capture
(`capture_usb.py`, then `capture_to_toml.py capture.json -o x.toml`).

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

A card that already has a Pi on it: `audiolink update` (*A new version*) or
`audiolink card write` keep its state; the by-hand way is in *Upgrading from v0.13.1
or earlier*.

### A new version

`audiolink update` puts a new version on the Pi without touching the card (owners
only): the release image of the program's own version (or `audiolink update IMAGE`: a
zip, or a build folder), over the link, or over the network with `--pi PI`. The card
holds two versions (image slots `a/`, `b/`); the new one goes in the one that isn't
running and boots once on trial. It stays if audiolink on it reaches its computer or
its relay within 5 minutes of its boot; otherwise the Pi goes back to the version it
had, by itself. `audiolink update --back` goes back to the version before, on trial
the same way (kept only once it gets through); `audiolink update --status` shows both.
A version whose trial failed, or that was only partly written, isn't offered to go
back to: send a new one. While a trial runs, neither is taken. The Pi's state,
settings and flavors are shared by the two versions. The firmware files at the top of
the card (`bootcode.bin`, `start.elf`, `fixup.dat`) only change with a card write.

Update the program on your computers first: `audiolink update` sends the program's
own version, and an older program can't talk to a newer Pi ("update audiolink").

### Upgrading from v0.13.1 or earlier

Cards written by v0.13.1 or earlier have no image slots, so `audiolink update` can't
put a version on them ("the Pi's card has no image slots yet"). Once, per Pi:

1. Get the new program (step 1 of *Quick start*) on the computers you use.
2. Put the new version on the card, keeping the Pi's state. On a Mac: `audiolink card
   write` (a card reader, or through the Pi: see *Write, back up and restore the
   card*). It keeps `audiolink/` (the Pi's keys and saved Wi-Fi), `flavors/`, `logs/`,
   `settings.txt` (with what someone set) and `ssh_authorized_keys`. By hand, on any
   computer, with the card in a card reader:
   - Linux: `unzip -o audiolink-pi-VER.zip -x settings.txt -d /media/$USER/AUDIOPI`
     (macOS: `-d /Volumes/AUDIOPI`).
   - Windows (PowerShell; `E` is the card's drive letter): `Copy-Item E:\settings.txt
     $HOME\settings.txt`, then `Expand-Archive -Force audiolink-pi-VER.zip
     -DestinationPath E:\`, then `Copy-Item $HOME\settings.txt E:\settings.txt`.

   Then delete `audiolink/audiolink.pyz` from the card if it's there (code sent by an
   older `audiolink update`, which would run instead of the new version's). The card's
   `config.txt` now boots `a/` (`os_prefix=a/`); the old version's files at the top
   of the card (`boot/`, `overlays/`, `cmdline.txt`, `bcm2710-rpi-zero-2-w.dtb`) are
   no longer used and can be deleted. New settings the old `settings.txt` doesn't
   mention keep their built-in values.
3. From then on: `audiolink update`.

A Pi from before v0.13.0 also comes up with no owner (its old access secret is gone):
claim it again with `audiolink setup` on the computer it's plugged into, and invite
the others.

### Write, back up and restore the card (macOS)

```
audiolink card write [IMAGE] [--wipe]   audiolink onto the card
audiolink card backup [FILE]            everything on the card to a zip
audiolink card restore FILE             erase the card and put a backup on it
```

`card write` puts this program's version on the card (its release image, downloaded
and checked), or IMAGE: a zip, or a build folder such as `build/card-pi`. On an
audiolink card it replaces the version's files and keeps the Pi's: `audiolink/` (its
keys, saved Wi-Fi), `flavors/` (the flavors added to it), `logs/`, `settings.txt`
(renewed from the new version's, with the settings someone set carried over),
`ssh_authorized_keys` and the version it boots: the new one goes in the other slot and
boots from then on, with no trial (`audiolink update --back` goes back to the one
before). On a card from before image slots the new version goes in `a/` (*Upgrading
from v0.13.1 or earlier*). It also updates the firmware files. A blank card is
written. Anything else (Raspberry Pi OS, another project's card) stops it, untouched:
`--wipe` erases the card first, keeping nothing. Every file is checked after writing.

`card backup` saves the Pi as it runs to one zip (by default
`audiolink-card-DATE.zip` in the current folder): its state (`audiolink/`: its keys and
saved Wi-Fi passwords, so keep the zip private; `settings.txt`; logs) and the version it
runs. `--all-images` adds the card's other version. Restoring a
backup gives a card that boots as that Pi; its other image slot fills with the next
`audiolink update`. A backup isn't an image: `update` and `card write` refuse it, and
`card restore` takes only backups. With the Pi connected and no card open here, the
Pi backs up its own card over the link (any OS; `--pi PI`: over the network; owners
only), so the card stays in the Pi. A restored card is the same Pi as the one backed
up: don't use both in two Pis at once. Backup leaves the card open, for writing onto
it next (`--eject`: eject it); write and restore eject it, ready for the Pi.

These commands don't ask questions (`audiolink setup` is the guided way): a card they
shouldn't touch stops them with the flag to give. They use a card in a card reader, or
open it through the Pi: unplug the Pi, take its card out, plug the Pi back in, then put
the card back in (needs rpiboot: `brew install rpiboot`, once); they say so when the
card isn't ready. `--disk /dev/diskN` picks the card when there's more than one, or a
blank one in a reader.

**Elsewhere than macOS**, by hand with a card reader: to back the card up, copy its
files to a folder (the over-the-link backup above works on any OS); to write a blank
card, *Where card images come from*; to put a new version on a used card keeping its
state, the by-hand steps in *Upgrading from v0.13.1 or earlier*; to restore, format the
card and copy the files back.

## Commands

| Command | |
|---|---|
| `audiolink setup [host]` | start here: this computer (and the Pi, plugged in here): install, claim or join (`--invite CODE`, `--code al2-...`), Wi-Fi, host, client (`ssh NAME`); `host`: just the host part |
| `audiolink card write [IMAGE] [--wipe]` / `backup [FILE]` / `restore FILE` | the Pi's card: audiolink onto it keeping its state, to a zip, and back (macOS; see *Write, back up and restore the card*) |
| `audiolink host [--expose PIPORT:PORT] [--wifi NAME]` | run on the host; keeps the link up, reconnects on its own |
| `audiolink pis [PI]` | the Pis this computer knows, with its role on each; PI: online or when last seen, its code, and (owners) its list |
| `audiolink invite NAME [--owner] [--pi PI]` | owners: a one-use 6-digit code that puts the computer NAME on the Pi's list, as a user (`--owner`: an owner) |
| `audiolink remove NAME [--pi PI]` | owners: take NAME off every Pi this computer owns (or just PI) |
| `audiolink forget PI` | drop a Pi here, and take this computer off its list (when it can reach it) |
| `audiolink rename PI NEW [--on-pi]` | rename a Pi here (`--on-pi`: the Pi itself, plugged in here) |
| `audiolink connect PI PORT` | pipe to a Pi port (ssh `ProxyCommand`): this network, then the relay |
| `audiolink forward PI PORT [--local LPORT]` | listen on `127.0.0.1:LPORT`; each connection goes to the Pi's port (ssh clients without `ProxyCommand`) |
| `audiolink ssh-config [--port PORT]` | `~/.ssh/config` entries for known Pis (logging in to the computer each is plugged into) |
| `audiolink wifi [scan\|status\|list\|join\|forget\|portal\|check\|ap]` | the Pi's Wi-Fi; `join`, `status`, `check` also over the network (`--pi PI`); `check` exits 0 online, 3 sign-in page, 4 no internet |
| `audiolink audio [status\|sweep\|set RATExCH\|forget]` | the link's audio format (see *How it works*) |
| `audiolink update [IMAGE\|--back\|--status] [--pi PI]` | a new version for the plugged-in Pi, over the link, tried before it's kept (see *A new version*); `--pi`: over the network |
| `audiolink settings [KEY=VALUE ...] [--unset KEY] [--reboot] [--pi PI]` | the Pi's card settings (see *The card's settings*) |
| `audiolink flavor [use NAME\|add FILE\|remove NAME\|try FILE] [--pi PI]` | what the Pi presents on USB (see *Flavors*) |
| `audiolink logs [FILE] [-n N] [--list] [--pi PI]` | the plugged-in Pi's logs, over the link; `--pi`: over the network |
| `audiolink shell [PI] [-- CMD]` | a root shell: the plugged-in Pi, or PI over the network; `-- CMD` runs one command |
| `audiolink list-devices` | audio devices (`*`: the Pi's) |

`audiolink --help` and `audiolink COMMAND --help` have the details. Commands that talk
to the plugged-in Pi need `audiolink host` running on that computer. Everything that
reads or changes the Pi itself (`wifi`, `update`, `settings`, `flavor`, `logs`,
`shell`, `card backup` over the link, `rename --on-pi`, `invite`, `remove`) is for its
owners (*Access*).

**Reset pins.** Short header pins 39 (GND) and 40 (GPIO21), with a jumper or a
paperclip, and hold. 5 s: back to the default flavor (from then on), and to the code
that came with the running version (a developer's code update goes). 15 s: also start
over, as a new Pi: a new key and code, no owner, an empty list (invites given out stop
working), its default name and no saved Wi-Fi; the version, the other settings and the
added flavors stay. The green light blinks slowly past 5 s, quickly past 15 s; the Pi
acts when you let go. Shorted while the Pi starts, it runs the version's own code.

## Troubleshooting

- **"No Pi plugged in."** Is `audiolink host` running on this computer, and does it
  say `Connected`? Is the cable in the Pi's USB port (not PWR), and is it a data
  cable? On a Mac, look for an "Allow accessory" prompt (it comes back when the Pi
  shows up as a new kind of USB device, e.g. after a new card). Then unplug and replug.
  `audiolink list-devices` shows whether the computer sees the Pi's sound devices.
- **A Pi with its own USB ID** (built from a fork, or in a flavor added on another
  computer): audiolink finds the Pi's sound devices
  by USB ID, and by name only when none has a known ID. `audiolink list-devices` shows
  each device's ID; tell the host about it with `audiolink host --usb-id VID:PID`, or
  for good with `"usb_ids": ["VID:PID"]` in `host.json` (audiolink's settings folder).
- **The Pi's sound device is there, but the host never says `Connected`.**
  `audiolink list-devices` shows it (`*`), yet nothing comes through. On a Mac, the
  program reading the Pi needs the microphone permission: System Settings → Privacy &
  Security → Microphone, for your terminal (or for audiolink, when it starts at
  login); without it macOS hands it silence. Then stop the host, unplug and replug the
  Pi, and start the host again. If it still doesn't connect, read the Pi's logs: over
  the network when it's on Wi-Fi (`audiolink logs --pi NAME`, owners), or from the
  card in a card reader (`logs/last`).
- **"The Pi's sound device dropped off USB."** Unplug it and plug it back in.
- **Slow, or the link keeps dropping.** Back to the starting audio format:
  `audiolink audio set baseline` (`audiolink host --audio-format baseline` keeps it
  there); `audiolink audio sweep` measures the others again.
- **Wi-Fi.** `audiolink wifi status` says where the Pi is. It only sees 2.4 GHz
  networks. Sign-in pages: `audiolink wifi portal`. When nobody has been connected to
  it for a while, the Pi opens its own open setup network, `audiopi-NAME`: join it
  from any computer, and `audiolink setup` (joining with an invite) and `audiolink
  wifi join NETWORK --pi NAME` (owners) work there.
- **Can't reach it from elsewhere.** `audiolink pis NAME`: "offline" means the Pi
  isn't on the internet (check its Wi-Fi on the host: `audiolink wifi check`);
  "doesn't have this computer on its list" means an owner has to invite it
  (`audiolink invite`, then `audiolink setup` here). Online, with ssh failing: the
  host isn't running with `--expose 2222:22`, its ssh server is off, or it doesn't
  take this computer's ssh key (its `authorized_keys`).
- **"Only an owner of NAME can do that."** This computer is a user, or not on the
  list (a host that isn't an owner still carries the link and its exposed ports). On
  an owner computer: `audiolink invite NAME --owner`; then `audiolink setup --invite
  CODE` here, with the Pi on its network rather than plugged in here (setup redeems
  invites over the network only).
- **Lost the owner computer.** Another owner can take it off: `audiolink remove NAME`.
  If it was the only owner, the Pi still lets everyone on its list through, but nobody
  can manage it any more (`audiolink pis PI` warns while there's only one: invite a
  second with `--owner`). The way back is the reset pins, 15 s: the Pi starts over as a new Pi, with a new code, no owner, its default name
  and no saved Wi-Fi. Claim it again with `audiolink setup` on the computer it's
  plugged into, put it on Wi-Fi, and invite the others again; on each of them,
  `audiolink forget OLDNAME` first. ssh asks once more about the host key (the entry
  is filed under the Pi's new key); remove the old block from `~/.ssh/config` if setup
  points one out.
- **A new version broke it.** One that doesn't reach its computer or relay within 5
  minutes goes back by itself (its logs: `audiolink logs prev/audiolink.log`). One that
  does but misbehaves: `audiolink update --back` (tried too: if the version before
  doesn't get through either, the Pi stays on this one). If it's USB that broke but the
  Pi still gets online, go back over the network from any owner computer: `audiolink
  update --back --pi NAME`.
- **The Pi doesn't start at all** (no sound device, and no `audiolink pis` online).
  With the card in a card reader, `logs/last` (and `logs/prev`, the boot before) has
  its logs. Put a version on it again, keeping the Pi's state: `audiolink card write`
  on a Mac (a card reader, or through the Pi with rpiboot), or by hand (*Upgrading from
  v0.13.1 or earlier*). Or boot the other version: in `config.txt` at the top of the
  card, change both `a/`s to `b/` (`os_prefix=b/`, `include b/config.txt`), or the
  other way round; the folder has to have a `config.txt` of its own.
- **Looking inside.** For owners: `audiolink shell` is a root shell on the plugged-in
  Pi, `audiolink shell NAME` on one over the network, and `audiolink logs` (`--pi
  NAME`) shows its logs. When audiolink itself doesn't start, `console = on` in the
  card's `settings.txt` adds a serial root shell (`/dev/cu.usbmodem*` on a Mac, 115200
  baud) next to the audio.

## Access

Each computer has its own key, made by its first `audiolink` command and kept in
audiolink's settings folder. Each Pi keeps a list of computer keys, each with a name
and a role:

| Role | May |
|---|---|
| user | reach what the Pi's host exposes (`ssh NAME`, `connect`, `forward`) |
| owner | that, and manage the Pi: Wi-Fi, name, settings, flavor, logs, versions, the shell, a card backup over the link, the browse-as-the-Pi proxy, and the list |

- **Claim:** the first owner is the computer the Pi is plugged into when `audiolink
  setup` runs there and the Pi has no owner. Over USB only: a Pi with no owner turns
  away anyone joining over the network.
- **Invite:** `audiolink invite NAME` (owners) gives a one-use 6-digit code for NAME,
  as a user, or an owner with `--owner`. It lasts 5 minutes and 3 tries, and a new one
  replaces it; `audiolink setup` on the other computer redeems it (SPAKE2: the code
  never crosses the network). `audiolink pis PI` shows an owner the list.
- **Remove:** `audiolink remove NAME` takes NAME off every Pi this computer owns
  (`--pi PI`: just that one), at once; a Pi it can't reach is left as it was, and it
  says which. The last owner can't be removed. `audiolink forget PI` takes this
  computer off a Pi's list itself, and drops the Pi here.
- **The host:** over USB, a computer that isn't an owner can still be the Pi's host:
  it carries the link, the audio format and its exposed ports, and nothing else
  (setup says so).
- **Two gates:** the Pi's list lets a computer through to the host; the host's own
  ssh login (`authorized_keys`) decides who logs in. They don't know about each other:
  removing a computer from a Pi leaves its key in your hosts' `authorized_keys` for you
  to remove.
- **Routes:** the Pi's network, its setup network and the relay work the same, except
  that the Pi's Wi-Fi doesn't change over the relay (that would cut the connection the
  request came on).
- **The Pi's code** (`al2-...`, from `audiolink pis PI`) is its public key: it finds
  the Pi on its relay and proves it's that Pi. It lets nobody in, so it can be shown
  freely.

**Encryption:** every connection to the Pi over the network (its Wi-Fi network, the
setup network, the relay), the streams to exposed ports included, is TLS 1.3 to the
Pi, checked against the Pi's key; the relay only passes it on. Between the Pi and its
host the data goes over USB, unencrypted. Older audiolink versions on either side get
"update audiolink" instead; macOS's built-in `/usr/bin/python3` can't do it (use the
Mac zip, or Python from python.org or Homebrew).

Caveats:
- Owners can invite others, owners too: give the role to computers you'd trust with
  the Pi's root shell.
- On the host computer, `audiolink host`'s browse-as-the-Pi proxy (47801) and shell
  port (47802) answer any user of that computer: on a computer shared with other
  people, an owner host gives them the Pi's shell (`--proxy-port 0`, `--shell-port 0`
  turn them off). Its control port needs a token only the user running it can read.
- The service behind an exposed port sees every connection come from the host itself
  (127.0.0.1).
- A version that gets through to its computer or relay is kept even if something else
  is broken (USB, say): `audiolink update --back` over the network, or the card.

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
  Wi-Fi, identity and the list, invites, versions, flavors, the setup network.
- **Identity:** each Pi has an ed25519 key, made on its first start and kept on its
  card (a Pi without a card makes a new one at every start, and setup warns before
  claiming it); its code (`al2-...`) is the public key. Each computer proves its own
  key once per connection, or once per link-up over USB, and has that key's role on the
  Pi's list from then on.
- **Finding a Pi:** on this network by its last known address, mDNS or the setup
  network's address; from anywhere through its relay, which the Pi stays connected to.
  Each connection brings the Pi's current record (its addresses, network, exposed
  ports), which `audiolink pis` shows as where it was last seen.

Code layout: `audiolink/{wire, session.py, services, host, client, relay}`;
`config/` and `pi/` make the Pi's image (an app on pi-gadget-kit, see
*Development*). The wire details are in `docs/protocol.md`.

## Security notes

- Exposed ports are reachable by every computer on the Pi's list, users and owners,
  and by nobody else; past that they're protected by the service itself (e.g. SSH
  authentication).
- Claiming takes USB, and starting over takes the reset pins: whoever has the Pi in
  hand can make it theirs (not with its old key and list, though).
- The SD card holds the Pi's key and its saved Wi-Fi passwords, readable by anyone
  who has the card, and so do card backups; treat them like a key.
- The setup network is open Wi-Fi; invites over it are protected by SPAKE2, and
  everything else is inside TLS checked against the Pi's key.

## Development

`make help` lists the developer commands; each runs one of the scripts below.

```
make pyz            # dist/audiolink.pyz (python3 build_pyz.py)
make app            # dist/audiolink-OS-ARCH.zip for this OS (needs pyinstaller)
make test           # the tests in parallel (tools/test.py); serially: python3 -m unittest discover -s tests -t .
make card           # the dev card image, build/card-pi/
make release-card   # the release card image and its zip, as CI makes them
make deploy         # make card, then onto the running dev Pi
```

Make stops at images and the dev Pi: putting an image on a card is `audiolink card`.

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
for someone else: `make release-card`, then `audiolink card write dist/audiolink-pi-VER.zip`. `config/` describes the card:
`pi-release.toml` (no USB ethernet, no SSH, the debug console off) and `pi.toml`, the
dev image: it includes `pi-release.toml` and adds USB ethernet with the Pi at
`10.55.0.2` (the `audiolink` flavor's `dev = true` function, with `dev_functions`), SSH with
`keys/id_ed25519.pub` and the debug console. `flavors/*.toml` are the built-in USB
devices (flavors) for both, `flavors/audiolink.toml` the default (a new one's USB ID
goes in `wire/usb_audio.py`'s `USB_IDS`: a test checks); `flavors/local/` is for your
own (gitignored), added to a Pi with `audiolink flavor add`. `pi/app-start.sh` is the app's start: USB ethernet, the
daemon's respawn loop (`pi/start-audiolink.sh`), mDNS, Wi-Fi, the clock.
`pi-release.toml`'s `[app.settings]` are the card's `audiolink.*` settings (*The card's
settings*); `pi/card-settings.sh` checks them and turns them into `audiolink pi`
arguments, after `/etc/audiolink.args` (the build's).

```
tools/build_card.sh [config/pi.toml]  # -> build/card-pi/
tools/deploy.sh [build/card-pi]       # onto a running dev Pi's card over USB ethernet, then reboot
tools/hot.sh                          # just the daemon, no reboot (lost at the next boot)
tools/smoke.sh                        # end-to-end check of the host daemon and the Pi
```

A dev image onto a card: `audiolink card write build/card-pi` (a card reader, or
through the Pi; the card's `audiolink/` state and `logs/` stay), or over the link,
tried like a release: `audiolink update build/card-pi`. A running dev Pi takes it over
ssh instead: `make deploy`.
State (identity, saved networks, code updates) is in the card's `audiolink/`; logs
in `logs/last` and `logs/prev`.

**Code updates** (developers): `audiolink update --code [PYZ]` (hidden from `--help`)
sends just new code, a `.pyz` (by default the one running the command), to
`audiolink/audiolink.pyz` on the card, where the Pi runs it instead of its version's
own code; no trial. `audiolink update --code --back`, the reset pins (5 s), a new
version (`audiolink update`) or deleting that file from the card drop it. `audiolink
card write` removes it too, unless `--keep-update`.

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
  `PI=root@ADDRESS tools/hot.sh`. A code update on the card (`audiolink update --code`) runs
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
4. Check the release's files: the macOS and Windows zips, the `.pyz`, the card
   (`audiolink-pi-VER.zip`, with its `.manifest`: also what `audiolink update`
   sends to a Pi) and `SHA256SUMS`. Unzip the card onto a microSD card and boot a
   Pi from it.

## License

MIT. Bundles PortAudio (MIT-style license, `audiolink/wire/_lib/PORTAUDIO-LICENSE.txt`).
