# Plan: versions and flavors

Status: agreed (2026-10-21); being built in the order below. Decisions from the review
are folded in; "v2" marks what's deliberately later. Done: 1 (pi-gadget-kit c8880ae),
2 and 3 (`services/flavor.py`, `audiolink flavor`; not yet tried on hardware).

## The model

A Pi has two independent things that used to be one:

| | What it is | Where it lives | Changed with |
|---|---|---|---|
| **Version** | audiolink's code and the system it runs on (an *image*) | an image slot on the card (`a/`, `b/`): blue-green | `audiolink update` |
| **Flavor** | what the Pi presents on USB: its device description (IDs, strings, audio function, HID...) | a small TOML file: built into the image, or added to the card | `audiolink flavor` |

Today a flavor is compiled into the image at build time (the gadget plan is made on
the Mac), so every flavor needs its own image, and the two image slots can't hold
"two flavors, each with a fallback". Separating them: images are versions only, and
the Pi plans its gadget from the chosen flavor when it starts (or switches).

"Image" and "slot" become internal words (in `-v` output and the docs); everyday
commands say `update` and `flavor`.

## What users see

```
audiolink update                  the latest release onto the Pi (tried, kept if it comes back)
audiolink update build/card-pi    this build instead
audiolink update --back           back to the version it ran before
audiolink update --status         what it runs, and what it would go back to

audiolink flavor                  what the Pi is, and what it could be
audiolink flavor use NAME         become that (kept if it comes back)
audiolink flavor add FILE         add one (a .toml someone gave you, or one you made)
audiolink flavor try FILE         try one without keeping it (developers)
audiolink flavor remove NAME      remove one you added
```

```
$ audiolink flavor
Flavor: audiolink
  audiolink      audiolink's own sound device: the fastest link            (built in)
  usb-headset    a plain USB headset, for apps that want one              (built in)
  my-gamebuds    added from my-gamebuds.toml                               (yours)
Switch: audiolink flavor use NAME
```

- Every `flavor use` (not just the first) is confirm-or-revert, like audio formats and
  updates: the Pi presents the new flavor and keeps it once a computer's USB link
  comes back on it, else goes back. Only USB counts: a flavor changes only USB, so the
  relay or Wi-Fi coming back proves nothing about it. Switched over the network with
  no computer plugged in, nothing can confirm it: the command says so.
- Switching is live: the Pi re-presents its USB device (down, plan, up), no reboot,
  respecting the same gap between re-presentations as audio formats (RESET_GAP: the
  controller has wedged when rebound several times in a row at high rates).
- `flavor use` warns first that the computer may show "Allow accessory".
- Computers find the Pi in any flavor without being told: built-in flavors' USB IDs
  are known to every audiolink (`USB_IDS`); for added ones, the computer that adds or
  switches learns the ID, and the Pi's network record carries its current flavor's
  IDs so other computers learn them too.
- Without the link: `settings.txt` has a `flavor = NAME` line (any computer, card in
  hand), and the reset pins always bring the default flavor back.
- `audiolink setup` doesn't ask about flavors; it mentions a non-default one.
- `audiolink settings` shows `flavor` with the other Pi settings.

Built-in flavors are generic (audiolink's own device, a plain USB headset with its
own ID, ...). Clones of real products are never shipped: they stay user-made and
user-added (`capture_usb.py` → `capture_to_toml.py` → `flavor add`).

## What developers see

1. Describe a flavor: a TOML by hand, or `capture_to_toml.py capture.json -o x.toml`.
2. `audiolink flavor try x.toml`: sent to the Pi, planned there (its notes shown,
   e.g. "string 33 not reproduced"), presented, kept only if the link comes back.
3. Edit and `try` again; `flavor add` to keep it; `flavor use` to make it the default.
4. Built-in flavors: `config/flavors/NAME.toml` in the repo; the build packages them
   and runs the planner over each as a check (a flavor that doesn't plan fails the
   build). Your own: `config/flavors/local/` (gitignored), next to them, never committed
   (a clone of a real product, say).

One planner (pi-gadget-kit's `gadget_plan.py`), run on the Pi; the build only calls it
early as a check. Validation happens on the Pi, so what was validated is what runs.

Code-only updates (`.pyz` over the link) become a developer tool: `make hot`, or a
hidden `audiolink update --code PYZ`. The `.pyz` itself stays what computers run.

## Work, in order

### 1. pi-gadget-kit: plan at runtime

- The image carries `gadget_plan.py` (and needs `python3`; images without it keep
  today's build-time plan), the built-in flavors (`/etc/usb-gadget/flavors/*.toml`),
  and the default flavor's plan prebuilt, as the last-resort fallback.
- Image config: `[device] flavors = {audiolink = "device.toml", ...}` and
  `default = "audiolink"`, replacing `[device] description`. The build copies them and
  plans each one as a check.
- Card: added flavors in `flavors/` at the top (the Pi's own data, kept by every
  write, update and backup); the choice in `settings.txt` (`flavor = NAME`).
- Boot: read the choice, find the flavor (card first, then built in), plan it into
  `/run/usb-gadget/device.plan`, present it. Any failure (missing, doesn't plan, the
  kernel refuses it): the default flavor, with the reason in the log; if even that
  fails, the prebuilt plan.
- `gadget.sh use FLAVOR`: plan and re-present live (down, plan, up): how every switch
  is done (no reboot). Loads the kernel modules the flavor needs.
- Flavor files can say `extras = false`: no debug console and no dev functions in
  that flavor (a faithful clone), whatever the image's settings.
- The `usb.*` card settings go: a flavor owns its identity (rename the device by
  editing its flavor). Cards that set them get a note in `settings.log`.

### 2. audiolink on the Pi: the flavor service

- `flavor.list`, `add` (validated by planning it), `remove`, `use`, `try`: owners
  only, over USB or the network.
- `use` and `try`: present, then keep once a host or the relay reaches the Pi within
  a few minutes, else back to the previous flavor (the audio format's
  confirm-or-revert, reused).
- `wire/gadget.py` reads the plan that's running (`/run/usb-gadget/device.plan`).
- The Pi's network record carries its current flavor's USB IDs.
- The reset pins: back to the default flavor, too.

### 3. audiolink on the computer: `audiolink flavor`

- The commands above; `flavor add/use` remember the flavor's USB ID in `host.json`.
- Clients learn added flavors' IDs from the Pi's record.
- `audiolink settings` shows `flavor`.

### 4. `audiolink update` for versions

- `audiolink update [IMAGE]`: the image path from today's `update --image`; default:
  this version's release image.
- `--back`: today's `update --image --revert`. `--status`: version and the one to go
  back to, by role ("Running", "Previous") with the slot letters.
- `.pyz` updates: hidden `--code PYZ`, and `make hot`.
- `card write` on an audiolink card becomes slot-aware: the image goes into the spare
  slot and becomes the one that boots; the other stays (updating never deletes the
  other version). Firmware is updated too; no trial (nothing runs).
- `card restore` takes only backups (`card write --wipe` writes an image fresh);
  `setup --card-image` takes only images.

### 5. Docs and a release

- README: versions and flavors, the commands; the flavor TOML format (pi-gadget-kit
  README: Hand-written devices) and how to make one from a capture.
- Tag a release once the above is on hardware: update over the link from the
  previous release (whose Pis have slots but not flavors) is the migration test.

## Decided

- Flavors live in `config/flavors/` (built in, committed) and `config/flavors/local/`
  (yours, gitignored).
- Switching is live; every `use` is a trial, confirmed over USB only.
- Blue-green is for reliability; going back (`update --back`) is a developer tool for
  now, so state migrations stay parked. When state formats change: a version in each
  state file, and new code converts state only once its trial is committed (a failed
  trial must never leave state the old version can't read).

## Later (v2)

- Which built-in flavors to ship beyond `audiolink`, and their USB ID (a pid.codes ID
  for the project would cover audiolink's own flavors too): to be decided.
- Images without `python3` (other pi-gadget-kit projects): today's build-time plan.
