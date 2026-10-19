"""Host daemon side of the Pi's identity (docs/protocol.md "USB link").

When a Pi connects over USB (and whenever its identity changes, e.g. just claimed):
  - prove this computer's key to it once per link-up (`link.auth`, signed over the nonce
    in the Pi's HELLO): the link then has that key's role on the Pi's list (owner, user,
    or none: host jobs only), which `peer` on the control port reports;
  - if this computer knows the Pi (keyring), refresh what it knows: name, last seen, role.
"""
import logging
import threading
import time

from ..auth import auth_message
from ..client import keyring
from ..services import Service

log = logging.getLogger("audiolink")


class HostIdentity(Service):
    def __init__(self, key=None):
        self._key = key
        self.role = None                # this link's role on the Pi's list
        self.pi = None                  # the Pi's identity info, as it last said
        self.nonce = None

    @property
    def key(self):
        if self._key is None:
            from ..client.computer import key
            self._key = key()
        return self._key

    def on_peer_hello(self, info):
        self.role, self.pi = None, info.get("identity")
        self.nonce = info.get("nonce")
        if self.pi:
            threading.Thread(target=self._sync, daemon=True).start()

    def on_event(self, name, data):
        if name == "identity":          # renamed, claimed, a list change
            self.pi = data
            self._sync()

    def _sync(self):
        try:
            self._auth()
            self._refresh()
        except Exception as e:
            log.warning("identity: %s", e)

    def _auth(self):
        if not self.nonce or not self.pi or not self.pi.get("pub"):
            return
        pub = bytes.fromhex(self.pi["pub"])
        sig = self.key.sign(auth_message(bytes.fromhex(self.nonce), pub))
        r = self.session.request("link.auth", {"key": self.key.pub.hex(), "sig": sig.hex()})
        self.role = (r or {}).get("role")
        log.info("link: this computer is %s on %s", self.role or "not on the list", self.pi.get("name"))

    def _refresh(self):
        pub = self.pi["pub"]
        if pub in keyring.load():
            keyring.update(pub, name=self.pi.get("name"), role=self.role, last_seen=int(time.time()))

    def local_ops(self):
        """For `peer` on the control port: the link's role."""
        return {"role": self.role}
