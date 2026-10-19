"""The Pis this machine knows about (config dir / pis.json).

Each entry is keyed by the Pi's public key, which never changes; names are labels:
  alias       what *this machine* calls it (`connect ALIAS`, ssh config); user-editable
  name        what the Pi calls itself (its advertised name), as last seen
  code        the Pi code, al2- + its public key: finds the Pi and pins TLS to it; grants
              nothing (access is this computer's key on the Pi's list). An al1- code from
              before (public key + read secret) is read for its public key only.
  serial      the board's serial, when learned over USB
  added       when this machine added it
  role        this computer's role on the Pi's list (user, owner), as it last said
  last_seen   the last real contact: over USB, or a connection to it
  last_record its record (name, ts, lan, ssid, ports) from the last connection or pairing

The host daemon and commands both change the file: each load-modify-save holds a lock
file (other processes) and _changing (this process's threads), and save_json replaces
the file atomically.
"""
import base64
import binascii
import contextlib
import os
import threading
import time

from ..paths import config_dir, load_json, save_json

PATH = config_dir() / "pis.json"
NOT_A_CODE = "That's not a Pi code (al2-...; audiolink pis PI shows it)"
PREFIXES = ("al2-", "al1-")             # al1-: before keys (public key + read secret)


class Ambiguous(KeyError):
    """More than one Pi goes by that name (args[0]: how many)."""


_changing = threading.RLock()           # one load-modify-save at a time (the host daemon's threads)
_depth = 0                              # changing() nesting, in the thread holding _changing


def _lock_file(f):
    """An exclusive lock on an open file, across processes; best effort where there's none."""
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return
    try:
        import msvcrt
    except ImportError:
        return
    for _ in range(30):                 # LK_LOCK itself gives up after 10 s
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            return
        except OSError:
            continue


@contextlib.contextmanager
def changing():
    """Hold around a load-modify-save, so that a change another process (or thread) makes
    between the load and the save isn't lost. Re-entrant within a thread."""
    global _depth
    with _changing:
        if _depth:
            _depth += 1
            try:
                yield
            finally:
                _depth -= 1
            return
        f = None
        try:
            os.makedirs(str(PATH.parent), exist_ok=True)
            f = open(str(PATH) + ".lock", "a+")
            _lock_file(f)
        except OSError:
            pass                        # can't lock (read-only?): the save says what's wrong
        _depth = 1
        try:
            yield
        finally:
            _depth = 0
            if f:
                f.close()               # releases the lock


def load():
    pis = load_json(PATH, {})
    for pub, e in pis.items():          # entries from before keys: their code held the read secret,
        if str(e.get("code", "")).startswith("al1-"):  # and custody the Pi's private key
            e["code"] = code_for(pub)
        e.pop("custody", None)
    return pis


def save(pis):
    save_json(PATH, pis, indent=1, sort_keys=True)


def code_for(pub):
    """The Pi code for a public key (bytes or hex)."""
    if isinstance(pub, str):
        pub = bytes.fromhex(pub)
    return "al2-" + base64.urlsafe_b64encode(pub).decode().rstrip("=")


def is_code(s):
    return str(s).startswith(PREFIXES)


def parse(code):
    """The public key (32 bytes) of a Pi code; anything else ends the command with a message."""
    try:
        if not is_code(code):
            raise ValueError
        raw = base64.urlsafe_b64decode(code[4:] + "=" * (-len(code[4:]) % 4))
        if len(raw) != (32 if code.startswith("al2-") else 48):
            raise ValueError
    except (ValueError, binascii.Error):
        raise SystemExit(NOT_A_CODE)
    return raw[:32]


def pub_of(code):
    return parse(code).hex()


def unique_alias(pis, wanted, pub):
    taken = {e["alias"] for p, e in pis.items() if p != pub}
    alias, n = wanted, 2
    while alias in taken:
        alias, n = "%s-%d" % (wanted, n), n + 1
    return alias


def upsert(code, name=None, alias=None, serial=None, seen=False):
    """Add or update a Pi; seen: it was just reached (sets last_seen). Returns (entry, created)."""
    with changing():
        return _upsert(code, name, alias, serial, seen)


def _upsert(code, name, alias, serial, seen):
    pis = load()
    pub = pub_of(code)
    e = pis.get(pub)
    created = e is None
    if created:
        e = {"added": int(time.time())}
    e["code"] = code_for(pub)
    if name:
        e["name"] = name
    if serial:
        e["serial"] = serial
    if alias or created:
        e["alias"] = unique_alias(pis, alias or name or "pi-" + pub[:4], pub)
    if seen:
        e["last_seen"] = int(time.time())
    pis[pub] = e
    save(pis)
    return e, created


def resolve(name_or_code, known=False):
    """A keyring entry by alias, advertised name, or code (an unknown code gets a
    throwaway entry so it still works, unless known). Raises KeyError if nothing
    matches, Ambiguous if several Pis call themselves that, SystemExit for a broken code."""
    pis = load()
    if is_code(name_or_code):
        pub = pub_of(name_or_code)
        if pub in pis:
            return pis[pub]
        if known:
            raise KeyError(name_or_code)
        return {"code": code_for(pub), "alias": None, "name": None}
    for field in ("alias", "name"):
        hits = [e for e in pis.values() if e.get(field) == name_or_code]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise Ambiguous(len(hits))
    raise KeyError(name_or_code)


def lookup(name_or_code, known=False):
    """resolve() for commands: a missing Pi ends the command with a message. known: only
    a Pi added here (a code this machine hasn't added is an error too)."""
    try:
        return resolve(name_or_code, known)
    except Ambiguous as e:
        raise SystemExit("%d Pis call themselves %s; use the name in the first column (audiolink pis)"
                         % (e.args[0], name_or_code))
    except KeyError:
        if is_code(name_or_code):
            raise SystemExit("No Pi with that code here (to be let in: audiolink invite NAME on an owner "
                             "computer, then audiolink setup here)")
        raise SystemExit("No Pi named %s (see: audiolink pis)" % name_or_code)


def by_serial(serial):
    for pub, e in load().items():
        if e.get("serial") == serial:
            return pub, e
    return None, None


def update(pub, **fields):
    with changing():
        pis = load()
        if pub in pis:
            pis[pub].update(fields)
            save(pis)


def seen(pub, at=None):
    """A real contact with a known Pi (pub: hex): now, or at `at` (e.g. when it published
    a record) if that's later than the last one kept."""
    with changing():
        pis = load()
        ts = int(time.time() if at is None else at)
        if pub in pis and ts > (pis[pub].get("last_seen") or 0):
            pis[pub]["last_seen"] = ts
            save(pis)


def remove(pub):
    with changing():
        pis = load()
        e = pis.pop(pub, None)
        save(pis)
        return e
