"""The Pis this machine knows about (config dir / pis.json).

Each entry is keyed by the Pi's public key, which never changes; names are labels:
  alias       what *this machine* calls it (`connect ALIAS`, ssh config); user-editable
  name        what the Pi calls itself (its advertised name), as last seen
  code        al1- connect code: public key + read secret (find and read, not forge)
  serial      the board's serial, when learned over USB
  custody     the Pi's full identity, kept while the Pi has no persistent storage, so the
              host can restore it after a reboot
  added, last_seen, last_record
"""
import threading
import time

from ..discovery.record import parse_code
from ..paths import config_dir, load_json, save_json

PATH = config_dir() / "pis.json"


_changing = threading.RLock()           # one load-modify-save at a time (the host daemon's threads)


def load():
    return load_json(PATH, {})


def save(pis):
    save_json(PATH, pis, indent=1, sort_keys=True)     # 0600: custody entries hold the Pi's private key


def pub_of(code):
    return parse_code(code)[0].hex()


def unique_alias(pis, wanted, pub):
    taken = {e["alias"] for p, e in pis.items() if p != pub}
    alias, n = wanted, 2
    while alias in taken:
        alias, n = "%s-%d" % (wanted, n), n + 1
    return alias


def upsert(code, name=None, alias=None, serial=None, custody=None):
    """Add or update a Pi. Returns (entry, created)."""
    with _changing:
        return _upsert(code, name, alias, serial, custody)


def _upsert(code, name, alias, serial, custody):
    pis = load()
    pub = pub_of(code)
    e = pis.get(pub)
    created = e is None
    if created:
        e = {"added": int(time.time())}
    e["code"] = code
    if name:
        e["name"] = name
    if serial:
        e["serial"] = serial
    if custody is not None:
        e["custody"] = custody
    if alias or created:
        e["alias"] = unique_alias(pis, alias or name or "pi-" + pub[:4], pub)
    e["last_seen"] = int(time.time())
    pis[pub] = e
    save(pis)
    return e, created


def resolve(name_or_code):
    """A keyring entry by alias, advertised name, or code (an unknown code gets a
    throwaway entry so it still works). Raises KeyError if nothing matches."""
    pis = load()
    if name_or_code.startswith("al1-"):
        pub = pub_of(name_or_code)
        return pis.get(pub) or {"code": name_or_code, "alias": None, "name": None}
    for field in ("alias", "name"):
        hits = [e for e in pis.values() if e.get(field) == name_or_code]
        if len(hits) == 1:
            return hits[0]
    raise KeyError(name_or_code)


def lookup(name_or_code):
    """resolve() for commands: a missing Pi ends the command with a message."""
    try:
        return resolve(name_or_code)
    except KeyError:
        raise SystemExit("No Pi named %s (see: audiolink pis)" % name_or_code)


def by_serial(serial):
    for pub, e in load().items():
        if e.get("serial") == serial:
            return pub, e
    return None, None


def update(pub, **fields):
    with _changing:
        pis = load()
        if pub in pis:
            pis[pub].update(fields)
            save(pis)


def remove(pub):
    with _changing:
        pis = load()
        e = pis.pop(pub, None)
        save(pis)
        return e
