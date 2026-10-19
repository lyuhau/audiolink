"""Pi side: who may do what, decided in one place (docs/protocol.md).

Roles, from the Pi's list (services/identity.py): a computer proves its key once per
connection (endpoint `auth`) or link-up (`link.auth`), and from then on has that key's
role:
  user     through the Pi to whatever its current host exposes (`open`)
  owner    everything: managing the Pi, its list, its logs, updates, the shell
A request that needs no role (hello, auth, pair; over USB the host jobs) is NONE.

Routes, from the connection's addresses (route_of; anything it can't place counts as
the relay, the most restricted). They only narrow (context rules): over the relay, no
Wi-Fi changes.
  setup    the setup network (open Wi-Fi)
  lan      the Wi-Fi network the Pi is on
  relay    through the relay (from RELAY_SOURCE)

Over USB, LinkAccess holds the host's role: the host jobs (audio, link.auth,
identity.info, identity.claim, `expose` in the HELLO) need none, everything else an
owner's key, and so do the host's streams to the Pi's proxy and shell.
"""
import logging
import os

from .. import auth
from . import Service
from .setup_ap import AP_ADDR

log = logging.getLogger("audiolink")

NONE, USER, OWNER = None, "user", "owner"
ROLES = (USER, OWNER)
RANK = {NONE: 0, USER: 1, OWNER: 2}
ROUTES = ("setup", "lan", "relay")
RELAY_SOURCE = "127.0.0.2"          # where relayed connections to local services come from
HOST_JOBS = ("link.auth", "identity.info", "identity.claim")    # and audio.*


class Refused(Exception):
    """A request refused for a reason the asker can act on: `code` goes in the reply."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def allows(have, need):
    """Does role `have` cover what `need` asks for?"""
    return RANK.get(have, 0) >= RANK.get(need, 0)


def route_of(local, remote, wifi_ip=None):
    """The route of a connection to the Pi, from its local and remote addresses."""
    if remote == RELAY_SOURCE:
        return "relay"
    if local == AP_ADDR:
        return "setup"
    if wifi_ip and local == wifi_ip:
        return "lan"
    return "relay"                  # fail closed


def not_owner(identity, who="this computer"):
    """The refusal for a request only an owner may make. who: the asker as the list
    names it, if it's on the list."""
    name = identity.pi_name()
    if not identity.claimed():
        return Refused("not_owner", "%s has no owner yet: claim it with audiolink setup on the computer "
                                    "it's plugged into" % name)
    return Refused("not_owner", "%s isn't an owner of %s: on an owner computer, audiolink invite %s --owner"
                   % (who, name, "NAME" if who == "this computer" else who))


def parse_key(text):
    """64 hex digits (or the 32 bytes themselves) -> 32 bytes. Raises ValueError."""
    if isinstance(text, bytes) and len(text) == 32:
        return text
    try:
        key = bytes.fromhex(str(text))
    except ValueError:
        key = b""
    if len(key) != 32:
        raise ValueError("a computer key is 64 hex digits")
    return key


class LinkAccess(Service):
    """The USB link's role: link.auth proves the host's key against a nonce fresh at
    every link-up (sent in the HELLO). The role is looked up on every request, so a key
    taken off the list loses it at once; a successful identity.claim makes the
    claimer's key the link's."""
    name = "link"
    caps = ("roles",)

    def __init__(self, identity):
        self.identity = identity
        self.nonce = os.urandom(auth.NONCE_BYTES)
        self.key = None                         # proven on this link-up
        identity.on_claim.append(self._claimed)

    def role(self):
        return self.identity.role_of(self.key) if self.key else NONE

    def on_up(self):
        self.nonce = os.urandom(auth.NONCE_BYTES)
        self.key = None

    def on_down(self):
        self.key = None

    def hello(self):
        return {"nonce": self.nonce.hex()}

    def handle(self, op, args):
        if op == "auth":
            key = parse_key(args.get("key"))
            try:
                sig = bytes.fromhex(str(args.get("sig")))
            except ValueError:
                sig = b""
            if not auth.verify(key, self.nonce, self.identity.public(), sig):
                log.warning("link: a key proof that doesn't check out (%s...)", key.hex()[:8])
                raise ValueError("that key's signature doesn't check out (link.auth)")
            self.key = key
            role = self.role()
            log.info("link: host is %s", "%s (%s)" % (self.identity.member(key)["name"], role) if role
                     else "not on the list (%s...)" % key.hex()[:8])
            return {"role": role}
        return super().handle(op, args)

    def _claimed(self, key):
        self.key = key

    def check(self, op):
        """Session gate: raises Refused unless the link's role may make this request."""
        if op in HOST_JOBS or op.startswith("audio."):
            return
        if self.role() != OWNER:
            raise not_owner(self.identity)

    def may_open(self):
        """The host's streams to the Pi's proxy and shell."""
        return self.role() == OWNER
