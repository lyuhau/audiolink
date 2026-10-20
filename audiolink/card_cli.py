"""audiolink card backup|restore: the Pi's card to a zip and back (card.py).

The card is opened where it is: already mounted (a card reader, or the Pi already
showing it), or through the Pi (rpiboot), which needs the Pi started without its card.
"""
import datetime
import os
import sys
import time
from pathlib import Path

from . import card
from .system import Runner
from .ui import Prompter, done, step


def card_command(a, runner=None, platform=sys.platform, usb_state=None, sleep=time.sleep, clock=time,
                 now=datetime.datetime.now):
    runner = runner or Runner()
    usb_state = usb_state or (lambda: card.usb_state(runner, platform))
    ask = Prompter(a, "--yes")
    if platform != "darwin":
        raise SystemExit("audiolink card works on macOS only so far; copy the card's files by hand "
                         "(README: Back up a card)")
    if a.action == "backup":
        out = Path(a.file or "audiolink-card-%s.zip" % now().strftime("%Y-%m-%d-%H%M"))
        if out.exists():
            raise SystemExit("%s exists; nothing written" % out)
        disk, _, mount, via_pi = open_card(a, runner, usb_state, sleep, clock)
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
            # Backing up is usually before restoring or installing onto the same card,
            # and an ejected card means opening it all over again.
            print("Card still open: restore or install onto it now, or eject it before unplugging: "
                  "diskutil eject %s" % disk)
            return 0
        eject(runner, disk, sleep)
    else:
        image = card.check_image(a.file)
        disk, size, mount, via_pi = open_card(a, runner, usb_state, sleep, clock)
        what = "%s, %.1f GB%s" % (disk, size / 1e9, ", " + os.path.basename(mount) if mount else "")
        question = "Erase the card (%s) and restore %s?" % (what, image.name)
        if a.yes:
            print(question, "yes")
        elif not ask.yes(question, default=False):
            raise SystemExit("Nothing changed")
        step("Restoring the card")
        try:
            card.write(runner, disk, image, sleep=sleep)
        except SystemExit:
            done("failed")
            raise
        done("done")
    print("Unplug the Pi and plug it back in" if via_pi else "Put the card back in the Pi")
    return 0


def open_card(a, runner, usb_state, sleep, clock):
    """The card to work on -> (disk, bytes, mount point or None, through the Pi?)."""
    if a.disk:
        size = card.external_disks(runner).get(a.disk)
        if size is None:
            raise SystemExit("%s isn't an external disk (see: diskutil list external)" % a.disk)
        return checked(a.disk, size), size, card.mount_of(runner, a.disk, sleep), False
    cards = card.mounted_cards(runner)
    if len(cards) > 1:
        raise SystemExit("More than one Pi card here (%s): eject the others, or pick one with --disk"
                         % ", ".join(d for d, _, _ in cards))
    if cards:
        disk, size, mount = cards[0]
        print("Card: %s (%s)" % (mount, disk))
        return checked(disk, size), size, mount, usb_state() == "msd"
    state = usb_state()
    if state in ("pi", "msd", None):
        print({"pi": "The Pi is running from its card. To open the card through the Pi:",
               "msd": "The Pi is still in card mode from an earlier try. To open the card through the Pi:",
               None: "No card here. Put it in a card reader, or open it through the Pi:"}[state])
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
    return disk, size, card.mount_of(runner, disk, sleep), True


def checked(disk, size):
    if size > card.MAX_CARD:
        raise SystemExit("%s is too big to be the Pi's card; nothing changed" % disk)
    return disk


def eject(runner, disk, sleep):
    for _ in range(12):                 # Spotlight can hold a new disk for a while
        status, _ = runner.run(["diskutil", "eject", disk])
        if not status:
            return
        sleep(5)
    print("Couldn't eject %s; eject it before unplugging" % disk)
