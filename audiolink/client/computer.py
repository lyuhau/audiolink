"""This computer's audiolink key (docs/state.md "Principals"): one ed25519 key per OS user,
in the config folder as an OpenSSH key file, made on first use. It's what a Pi's list
holds, and what proves this computer to a Pi (auth.py).

  <config>/key        OpenSSH private key (openssh-key-v1, unencrypted), user-only
  <config>/key.pub    "ssh-ed25519 AAAA... audiolink@HOST"

Key also reads other OpenSSH ed25519 files: a passphrase-protected one, or a .pub
alone, signs through ssh-agent.
"""
import base64
import os
import re
import socket
import struct

from ..crypto import ed25519_public, ed25519_sign
from ..paths import config_dir

KIND = b"ssh-ed25519"


def _string(b):
    return struct.pack(">I", len(b)) + b


def _strings(blob, n):
    out, i = [], 0
    for _ in range(n):
        size = struct.unpack(">I", blob[i:i + 4])[0]
        out.append(blob[i + 4:i + 4 + size])
        i += 4 + size
    return out, blob[i:]


def _agent_sign(pub, msg):
    """Sign with ssh-agent (for passphrase-protected keys). -> signature or None."""
    path = os.environ.get("SSH_AUTH_SOCK")
    if not path or not hasattr(socket, "AF_UNIX"):
        return None
    blob = _string(KIND) + _string(pub)
    body = b"\x0d" + _string(blob) + _string(msg) + b"\0\0\0\0"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(path)
            s.sendall(_string(body))
            head = s.recv(4)
            size = struct.unpack(">I", head)[0]
            reply = b""
            while len(reply) < size:
                data = s.recv(size - len(reply))
                if not data:
                    return None
                reply += data
    except (OSError, struct.error):
        return None
    if reply[:1] != b"\x0e":
        return None
    (sig_blob,), _ = _strings(reply[1:], 1)
    (kind, sig), _ = _strings(sig_blob, 2)
    return sig if kind == KIND and len(sig) == 64 else None


def parse_public(text):
    """'ssh-ed25519 AAAA... [comment]' or 64 hex digits -> the 32-byte key."""
    text = text.strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return bytes.fromhex(text)
    parts = text.split()
    if len(parts) >= 2 and parts[0] == KIND.decode():
        try:
            (kind, pub), _ = _strings(base64.b64decode(parts[1]), 2)
        except (ValueError, struct.error):
            raise ValueError("not an ssh-ed25519 key")
        if kind == KIND and len(pub) == 32:
            return pub
    raise ValueError("not an ssh-ed25519 key")


class Key:
    """An ed25519 key from an OpenSSH private key file (unencrypted: signed here;
    passphrase-protected, or a .pub file: signed by ssh-agent)."""

    def __init__(self, path):
        try:
            with open(os.path.expanduser(str(path))) as f:
                text = f.read()
        except OSError as e:
            raise SystemExit("Can't read %s (%s)" % (path, e.strerror))
        self.path, self.seed = str(path), None
        if "BEGIN OPENSSH PRIVATE KEY" in text:
            self._private(text)
        else:
            try:
                self.pub = parse_public(text.splitlines()[0] if text.strip() else "")
            except ValueError as e:
                raise SystemExit("%s: %s" % (path, e))

    def _private(self, text):
        body = "".join(l for l in text.splitlines() if l and not l.startswith("-----"))
        try:
            blob = base64.b64decode(body)
            if not blob.startswith(b"openssh-key-v1\0"):
                raise ValueError
            (cipher, _, _), rest = _strings(blob[15:], 3)
            if struct.unpack(">I", rest[:4])[0] != 1:
                raise ValueError("one key per file")
            (pub_blob, priv), _ = _strings(rest[4:], 2)
            (kind, pub), _ = _strings(pub_blob, 2)
        except (ValueError, TypeError, struct.error):
            raise SystemExit("%s: not an OpenSSH private key" % self.path)
        if kind != KIND:
            raise SystemExit("%s: not an ed25519 key (make one: ssh-keygen -t ed25519)" % self.path)
        self.pub = pub
        if cipher == b"none":
            try:
                (kind2, pub2, secret), _ = _strings(priv[8:], 3)
            except struct.error:
                raise SystemExit("%s: damaged private key" % self.path)
            if kind2 == KIND and pub2 == pub and secret and len(secret) == 64:
                self.seed = secret[:32]

    def sign(self, msg):
        if self.seed:
            return ed25519_sign(self.seed, msg)
        sig = _agent_sign(self.pub, msg)
        if sig is None:
            raise SystemExit("%s is passphrase-protected: add it to ssh-agent (ssh-add %s)"
                             % (self.path, re.sub(r"\.pub$", "", self.path)))
        return sig


def openssh_private(seed, comment):
    """An unencrypted openssh-key-v1 file for an ed25519 seed (what ssh-keygen writes)."""
    pub = ed25519_public(seed)
    pub_blob = _string(KIND) + _string(pub)
    check = os.urandom(4)
    priv = check + check + _string(KIND) + _string(pub) + _string(seed + pub) + _string(comment.encode())
    priv += bytes(range(1, 1 + (-len(priv)) % 8))             # pad to the "none" cipher's block of 8
    blob = (b"openssh-key-v1\0" + _string(b"none") + _string(b"none") + _string(b"")
            + struct.pack(">I", 1) + _string(pub_blob) + _string(priv))
    b64 = base64.b64encode(blob).decode()
    lines = [b64[i:i + 70] for i in range(0, len(b64), 70)]
    return "-----BEGIN OPENSSH PRIVATE KEY-----\n%s\n-----END OPENSSH PRIVATE KEY-----\n" % "\n".join(lines)


def openssh_public(pub, comment):
    return "ssh-ed25519 %s %s\n" % (base64.b64encode(_string(KIND) + _string(pub)).decode(), comment)


def _write_private(path, text):
    """Write a file only this user can read: made 0600 before anything is in it."""
    tmp = "%s.tmp-%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def this_computer():
    """This computer's name on a Pi's list: the short hostname as a Pi name (lowercase,
    letters, digits and dashes: Yuhaus-MacBook-Air -> yuhaus-macbook-air)."""
    name = re.sub(r"[^a-z0-9-]+", "-", socket.gethostname().split(".")[0].lower()).strip("-")[:32]
    return name or "computer"


def path(folder=None):
    return os.path.join(str(folder or config_dir()), "key")


def key(folder=None):
    """This computer's Key, made the first time it's needed."""
    p = path(folder)
    if not os.path.exists(p):
        seed = os.urandom(32)
        comment = "audiolink@%s" % (socket.gethostname().split(".")[0] or "computer")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        _write_private(p, openssh_private(seed, comment))
        with open(p + ".pub", "w", newline="\n") as f:
            f.write(openssh_public(ed25519_public(seed), comment))
    return Key(p)
