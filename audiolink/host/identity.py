"""Host daemon side of the Pi's identity.

When a Pi connects over USB (and whenever its identity changes):
  - if this machine knows it (keyring), refresh what we know (name, last seen) and,
    while the Pi has no persistent storage, keep the custody copy current;
  - if the Pi has a fresh identity (it lost its own at a RAM-only reboot, or just
    got an SD card) and we hold custody for its board serial, restore it, so its
    code and name survive;
  - one-time migration: before Pis owned their identity, the host kept one in
    discovery.json; hand that to the Pi so existing connect codes keep working.
"""
import json
import logging
import threading
import time

from ..client import keyring
from ..discovery.record import code_for
from ..paths import config_dir
from ..services import Service

log = logging.getLogger("audiolink")

LEGACY = config_dir() / "discovery.json"


class HostIdentity(Service):
    def on_peer_hello(self, info):
        if info.get("identity"):
            threading.Thread(target=self._sync, args=(info["identity"],), daemon=True).start()

    def on_event(self, name, data):
        if name == "identity":          # renamed, (un)discoverable, claimed, restored
            self._sync(data)

    def _sync(self, ident):
        try:
            self._sync_inner(ident)
        except Exception as e:
            log.warning("identity: %s", e)

    def _sync_inner(self, ident):
        req = self.session.request
        pub, entry = keyring.by_serial(ident["serial"])

        if entry is None and LEGACY.exists():
            legacy = json.loads(LEGACY.read_text())
            data = dict(legacy, name=ident["name"], claimed=True, discoverable=True)
            ident = req("identity.restore", data)
            code = code_for(data)
            keyring.upsert(code, name=ident["name"], serial=ident["serial"],
                           custody=None if ident["persistent"] else data)
            LEGACY.rename(LEGACY.with_suffix(".json.migrated"))
            log.info("identity: moved this host's old discovery identity to %s; existing codes keep working",
                     ident["name"])
            return

        if entry is None:
            return                      # not added on this machine: nothing to do
        custody = entry.get("custody")
        if custody and ident["pub"] != pub and (not ident["persistent"] or ident.get("fresh")):
            ident = req("identity.restore", custody)
            log.info("identity: restored %s's identity from this host's custody copy", ident["name"])
        if ident["pub"] != pub:
            return                      # a different identity on a known board (e.g. reflashed)
        fields = {"name": ident["name"]}
        if custody:
            fields["custody"] = req("identity.export")      # keep name/flags current (also a backup)
        keyring.update(pub, last_seen=int(time.time()), **fields)
