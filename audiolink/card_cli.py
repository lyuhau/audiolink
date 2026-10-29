"""audiolink card write|backup|restore: the Pi's card (card.py). Plumbing: no questions;
what to do is in the flags, and a card it shouldn't touch without one stops it with the
flag to give (audiolink setup is the guided way, and asks).

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
from .ui import detail, done, step


def card_command(a, runner=None, platform=sys.platform, usb_state=None, sleep=time.sleep, clock=time,
                 now=datetime.datetime.now):
    runner = runner or Runner()
    usb_state = usb_state or (lambda: card.usb_state(runner, platform))
    if a.action == "backup" and getattr(a, "pi", None):
        return backup_from_pi(a, sleep)
    if a.action == "backup" and (platform != "darwin" or not card.mounted_cards(runner)):
        from .host.pi_call import pi_connected
        if pi_connected(a, "card"):                 # the running Pi backs itself up: no card swap
            return backup_from_pi(a, sleep)
    if platform != "darwin":
        raise SystemExit("audiolink card works on macOS only so far; copy the card's files by hand "
                         "(README: Write, back up and restore the card)")
    if a.action == "backup":
        return backup(a, runner, usb_state, sleep, clock, now)
    return write(a, runner, usb_state, sleep, clock, wipe=a.wipe or a.action == "restore")


def backup_out(a, now=datetime.datetime.now):
    out = Path(a.file or "audiolink-card-%s.zip" % now().strftime("%Y-%m-%d-%H%M"))
    if out.exists():
        raise SystemExit("%s exists; nothing written" % out)
    return out


READ = 1024 * 1024
IN_FLIGHT = 4


def backup_from_pi(a, sleep=time.sleep, now=datetime.datetime.now):
    """The Pi backs up its own card (services/card.py) and this reads it, over the link or
    the network (--pi): the same zip as from the card itself."""
    import base64
    import hashlib
    import os
    from .host.pi_call import pi_caller
    out = backup_out(a, now)
    call, over_network = pi_caller(a, "card")
    step("Backing up the Pi's card, on the Pi")
    st = call("card.backup", {"all_images": bool(getattr(a, "all_images", False))})
    while st.get("state") == "running":
        sleep(2)
        st = call("card.progress")
    if st.get("state") != "done":
        done("failed")
        raise SystemExit("The Pi couldn't back up its card: %s" % st.get("error", st))
    done("%d files, %.1f MB" % (st["files"], st["size"] / 1e6))
    step("Copying it here")
    start = time.monotonic()
    part = out.with_name(out.name + ".part")
    fd = os.open(str(part), os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "r+b") as f:
            def get(off):
                return off, base64.b64decode(call("card.read", {"offset": off, "size": READ})["data"])
            offsets = range(0, st["size"], READ)
            if over_network:
                pieces = map(get, offsets)
            else:
                from concurrent.futures import ThreadPoolExecutor
                pool = ThreadPoolExecutor(IN_FLIGHT)
                pieces = pool.map(get, offsets)
            for off, data in pieces:
                f.seek(off)
                f.write(data)
            f.truncate(st["size"])
            f.flush()
            f.seek(0)
            h = hashlib.sha256()
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest() != st["sha256"]:
            raise SystemExit("The backup arrived damaged; nothing kept (run it again)")
        os.replace(str(part), str(out))
    except BaseException:
        done("failed")
        raise
    finally:
        if part.exists():
            part.unlink()
    secs = time.monotonic() - start
    done("%ds (%.1f MB/s)" % (secs, st["size"] / 1e6 / max(secs, 0.1)))
    try:
        call("card.done")                       # the Pi removes its copy
    except (SystemExit, OSError, ValueError):
        pass
    print("Saved %s" % out)
    print("It holds the Pi's keys and Wi-Fi passwords: keep it private")
    return 0


def backup(a, runner, usb_state, sleep, clock, now):
    out = backup_out(a, now)
    disk, size, mount, via_pi = open_card(a, runner, usb_state, sleep, clock)
    if not mount:
        raise SystemExit("The card in %s has nothing on it to back up" % disk)
    step("Backing up the card")
    try:
        n = card.backup(mount, out, bool(getattr(a, "all_images", False)))
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
    if via_pi:
        card.replug()
    else:
        print("Put the card back in the Pi")
    return 0


def write(a, runner, usb_state, sleep, clock, wipe):
    if a.file:
        image = card.Image(a.file)
        label = image.name
        if a.action == "restore" and not image.is_backup:
            raise SystemExit("%s is an image, not a card backup: write it with audiolink card write %s "
                             "(--wipe: erasing the card first)" % (image.name, a.file))
        if a.action != "restore" and image.is_backup:
            raise SystemExit("%s is a card backup (a Pi's keys, its images), not an image: put it back with "
                             "audiolink card restore %s" % (image.name, a.file))
    elif a.action == "restore":
        raise SystemExit("Which backup? audiolink card restore FILE")
    else:
        from .paths import cache_dir
        step("Downloading the card image (audiolink %s)" % card.__version__)
        try:
            image = card.Image(card.fetch_image(runner, cache_dir() / "cards", give="audiolink card write FILE"))
        except SystemExit:
            done("failed")
            raise
        done("checked")
        label = "audiolink %s" % card.__version__
    disk, size, mount, via_pi = open_card(a, runner, usb_state, sleep, clock)
    usb = False
    kind, what = card.classify(runner, disk, mount)
    print("Card: %s, %.1f GB, %s" % (disk, size / 1e9, what))
    if kind == "other" and not wipe:
        raise SystemExit("Not an audiolink card: nothing changed. To erase it and write %s: --wipe" % label)
    try:
        if wipe or kind == "blank":
            step("Erasing it and writing %s" % label if kind != "blank" else "Writing %s" % label)
            n, new, removed, into = card.write(runner, disk, image, sleep=sleep), [], False, None
        else:
            try:
                with open(os.path.join(mount, "settings.txt"), encoding="utf-8", errors="replace") as f:
                    usb = card.sets_usb(f.read())
            except OSError:
                pass
            step("Writing %s, keeping the Pi's state" % label)
            n, new, removed, into = card.update(runner, disk, mount, image, sleep=sleep, keep_update=a.keep_update)
    except SystemExit:
        done("failed")
        raise
    done("done, %d files checked" % n)
    if into:
        print("The Pi starts this one from now on; the one it had stays on the card (back to it: audiolink update --back)")
        detail("into %s, kept %s", *into)
    if new:
        print("New settings in its settings.txt: %s" % ", ".join(new))
    if usb:
        print(card.USB_GONE)
    if removed:
        print("Removed an earlier update%s from the card, so the Pi runs this image's code "
              "(--keep-update: keep it)" % ("" if removed in (True, "unknown") else " (build %s)" % removed))
    if via_pi:
        card.replug()
    else:
        print("Put the card back in the Pi")
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
