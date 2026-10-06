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
import json
import os
import time

from ..discovery.record import parse_code
from ..paths import config_dir

PATH = config_dir() / "pis.json"


def load():
    try:
        return json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}


def save(pis):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(pis, indent=1, sort_keys=True))
    try:
        os.chmod(tmp, 0o600)            # custody entries hold the Pi's private key
    except OSError:
        pass
    tmp.replace(PATH)


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


def by_serial(serial):
    for pub, e in load().items():
        if e.get("serial") == serial:
            return pub, e
    return None, None


def update(pub, **fields):
    pis = load()
    if pub in pis:
        pis[pub].update(fields)
        save(pis)


def remove(pub):
    pis = load()
    e = pis.pop(pub, None)
    save(pis)
    return e
