"""Shared test helpers: a fake clock, and two link ends joined by an in-memory wire."""
import os
import random
import re
import sys
import tempfile
import time as _time
import unittest
from unittest import mock

from audiolink import tls

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _built_in_usb():
    with open(os.path.join(ROOT, "config", "flavors", "audiolink.toml")) as f:
        text = f.read()
    return tuple(int(re.search(r"^%s = (0x[0-9a-fA-F]+)" % k, text, re.M).group(1), 16) for k in ("idVendor", "idProduct"))


# The built-in flavor's USB ID (config/flavors/audiolink.toml; usb_audio.USB_IDS has it
# too, test_usb_audio checks), read rather than copied so a fork can change it.
PI_USB = _built_in_usb()
PI_USB_ID = "%04x:%04x" % PI_USB

# For tests that connect to a Pi's endpoint (TLS 1.3 with an Ed25519 key): macOS's
# /usr/bin/python3 (LibreSSL) can't, and audiolink says so to its users instead.
needs_tls = unittest.skipIf(tls.unsupported(), "this Python's SSL library can't do TLS 1.3 with ed25519")


def _can_symlink():
    with tempfile.TemporaryDirectory() as d:
        try:
            os.symlink("x", os.path.join(d, "link"))
        except OSError:
            return False
    return True


# For tests whose stand-ins are made of symlinks: Windows makes them only in Developer Mode
# or as an administrator.
needs_symlinks = unittest.skipUnless(_can_symlink(), "can't make symlinks here (Windows: only in Developer Mode)")

# For tests that check a file's mode bits: Windows has none to set beyond read-only (a file
# in the user's profile is private by the folder's permissions).
needs_modes = unittest.skipIf(sys.platform == "win32", "Windows files have no mode bits to make private")


class FakeClock:
    """Stands in for the `time` module where code reads monotonic() (and sleeps)."""

    def __init__(self, start=1000.0):
        self.t = start

    def monotonic(self):
        return self.t

    def time(self):
        return 1_750_000_000 + self.t

    def sleep(self, s):
        self.t += s

    def advance(self, s):
        self.t += s


def patch_time(module, clock):
    """Make `module` see `clock` as its `time` module."""
    return mock.patch.object(module, "time", clock)


class Wire:
    """Moves bytes between two Links, as the audio would: in fixed-size chunks, with
    optional corruption and dropped chunks, and time passing per chunk."""

    def __init__(self, a, b, clock, chunk=1920, corrupt=0.0, drop=0.0, seed=1):
        self.a, self.b, self.clock, self.chunk = a, b, clock, chunk
        self.corrupt, self.drop = corrupt, drop
        self.rng = random.Random(seed)

    def _carry(self, src, dst):
        data = bytearray(src.pull(self.chunk))
        if self.rng.random() < self.drop:
            return
        if self.rng.random() < self.corrupt:
            data[self.rng.randrange(len(data))] ^= 0xFF
        dst.feed(bytes(data))

    def run(self, seconds, until=None):
        """Run for up to `seconds` of fake time (10 ms per round), or until until()."""
        end = self.clock.t + seconds
        while self.clock.t < end:
            self._carry(self.a, self.b)
            self._carry(self.b, self.a)
            self.clock.advance(0.01)
            if until and until():
                return True
        return bool(until and until())


def wait(pred, timeout=5.0):
    """For code that runs on real threads: poll pred() for up to timeout real seconds."""
    end = _time.monotonic() + timeout
    while _time.monotonic() < end:
        if pred():
            return True
        _time.sleep(0.01)
    return pred()


class EndpointClient:
    """A bare client of a Pi's endpoint (services/endpoint.py) for Pi-side tests: TLS,
    hello, auth with a test key, then JSON lines; after `open`, the socket is the stream."""

    def __init__(self, port, host="127.0.0.1", sock=None):
        import socket
        raw = sock or socket.create_connection((host, port), timeout=10)
        raw.settimeout(10)
        self.sock, self.pub = tls.client_wrap(raw)
        self.buf = b""

    def ask(self, obj):
        self.send(obj)
        return self.line()

    def send(self, obj):
        import json
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def line(self):
        import json
        while b"\n" not in self.buf:
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("the Pi closed the connection")
            self.buf += data
        line, _, self.buf = self.buf.partition(b"\n")
        return json.loads(line)

    def hello(self):
        self.hi = self.ask({"op": "hello"})
        return self.hi

    def auth(self, seed):
        """hello (if not yet), then auth signed with this ed25519 seed. -> the reply."""
        from audiolink.auth import auth_message
        from audiolink.crypto import ed25519_public, ed25519_sign
        if not hasattr(self, "hi"):
            self.hello()
        sig = ed25519_sign(seed, auth_message(bytes.fromhex(self.hi["nonce"]), self.pub))
        return self.ask({"op": "auth", "key": ed25519_public(seed).hex(), "sig": sig.hex()})

    def recv_exactly(self, n):
        data, self.buf = self.buf, b""
        while len(data) < n:
            more = self.sock.recv(65536)
            if not more:
                break
            data += more
        return data

    def close(self):
        import socket
        try:
            socket.socket.shutdown(self.sock, socket.SHUT_RDWR)     # the Pi sees the end now
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
