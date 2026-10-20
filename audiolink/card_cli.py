"""audiolink card write|backup|restore: the Pi's card (card.py). Plumbing: no questions;
what to do is in the flags, and a card it shouldn't touch without one stops it with the
flag to give (audiolink setup is the guided way, and asks).

The card is opened where it is: already mounted (a card reader, or the Pi already
showing it), or through the Pi (rpiboot), which needs the Pi started without its card.
"""
import datetime
import sys
import time
from pathlib import Path

from . import card
from .system import Runner
from .ui import done, step


def card_command(a, runner=None, platform=sys.platform, usb_state=None, sleep=time.sleep, clock=time,
                 now=datetime.datetime.now):
    runner = runner or Runner()
    usb_state = usb_state or (lambda: card.usb_state(runner, platform))
    if platform != "darwin":
        raise SystemExit("audiolink card works on macOS only so far; copy the card's files by hand "
                         "(README: Write, back up and restore the card)")
    if a.action == "backup":
        return backup(a, runner, usb_state, sleep, clock, now)
    return write(a, runner, usb_state, sleep, clock, wipe=a.wipe or a.action == "restore")


def backup(a, runner, usb_state, sleep, clock, now):
    out = Path(a.file or "audiolink-card-%s.zip" % now().strftime("%Y-%m-%d-%H%M"))
    if out.exists():
        raise SystemExit("%s exists; nothing written" % out)
    disk, size, mount, via_pi = open_card(a, runner, usb_state, sleep, clock)
    if not mount:
        raise SystemExit("The card in %s has nothing on it to back up" % disk)
    step("Backing up the card")
    try:
        n = card.backup(mount, out)
    except SystemExit:
        done("failed")
        raise
    done("%d files" % n)
    print("Saved %s" % out)
    print("It holds the Pi's keys and Wi-Fi passwords: keep it private")
    if not a.eject:
        # Backing up is usually before writing onto the same card, and an ejected card
        # means opening it all over again.
        print("Card still open: write or restore onto it now, or eject it before unplugging: "
              "diskutil eject %s" % disk)
        return 0
    card.eject(runner, disk, sleep)
    print("Unplug the Pi and plug it back in" if via_pi else "Put the card back in the Pi")
    return 0


def write(a, runner, usb_state, sleep, clock, wipe):
    if a.file:
        image = card.Image(a.file)
        label = image.name
    else:
        from .paths import cache_dir
        step("Downloading the card image (audiolink %s)" % card.__version__)
        try:
            image = card.Image(card.fetch_image(runner, cache_dir() / "cards"))
        except SystemExit:
            done("failed")
            raise
        done("checked")
        label = "audiolink %s" % card.__version__
    disk, size, mount, via_pi = open_card(a, runner, usb_state, sleep, clock)
    kind, what = card.classify(runner, disk, mount)
    print("Card: %s, %.1f GB, %s" % (disk, size / 1e9, what))
    if kind == "other" and not wipe:
        raise SystemExit("Not an audiolink card: nothing changed. To erase it and write %s: --wipe" % label)
    try:
        if wipe or kind == "blank":
            step("Erasing it and writing %s" % label if kind != "blank" else "Writing %s" % label)
            n, new, removed = card.write(runner, disk, image, sleep=sleep), [], False
        else:
            step("Writing %s, keeping the Pi's state" % label)
            n, new, removed = card.update(runner, disk, mount, image, sleep=sleep, keep_update=a.keep_update)
    except SystemExit:
        done("failed")
        raise
    done("done, %d files checked" % n)
    if new:
        print("New settings in its settings.txt (commented out): %s" % ", ".join(new))
    if removed:
        print("Removed an earlier update%s from the card, so the Pi runs this image's code "
              "(--keep-update: keep it)" % ("" if removed in (True, "unknown") else " (build %s)" % removed))
    print("Unplug the Pi and plug it back in" if via_pi else "Put the card back in the Pi")
    return 0


def open_card(a, runner, usb_state, sleep, clock):
    """The card to work on -> (disk, bytes, mount point or None, through the Pi?)."""
    if a.disk:
        size = card.external_disks(runner).get(a.disk)
        if size is None:
            raise SystemExit("%s isn't an external disk (see: diskutil list external)" % a.disk)
        return checked(a.disk, size), size, card.volume_of(runner, a.disk, sleep), False
    cards = card.mounted_cards(runner)
    if len(cards) > 1:
        raise SystemExit("More than one Pi card here (%s): eject the others, or pick one with --disk"
                         % ", ".join(d for d, _, _ in cards))
    if cards:
        disk, size, mount = cards[0]
        return checked(disk, size), size, mount, usb_state() == "msd"
    state = usb_state()
    if state in ("pi", "msd", None):
        print({"pi": "The Pi is running from its card. To open the card through the Pi:",
               "msd": "The Pi is still in card mode from an earlier try. To open the card through the Pi:",
               None: "No card here. Put it in a card reader (a blank one: --disk), or open it through the Pi:"}[state])
        print("  unplug the Pi, take its card out, plug the Pi back in, then put the card back in")
        step("Waiting for the Pi without its card")
        end = clock.monotonic() + 300
        while True:
            state = usb_state()
            cards = card.mounted_cards(runner) if state != "rom" else []
            if state == "rom" or cards:
                break
            if clock.monotonic() >= end:
                done("not yet")
                raise SystemExit("Nothing changed; run it again when the Pi or the card is ready")
            sleep(1)
        done("ready" if state == "rom" else "card found")
        if cards:
            return open_card(a, runner, usb_state, sleep, clock)
    disk, size = card.open_through_pi(runner, usb_state, "run it again", sleep=sleep, clock=clock)
    return disk, size, card.volume_of(runner, disk, sleep), True


def checked(disk, size):
    if size > card.MAX_CARD:
        raise SystemExit("%s is too big to be the Pi's card; nothing changed" % disk)
    return disk
