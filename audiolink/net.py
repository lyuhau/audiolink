"""Listening sockets that act the same on every platform."""
import socket
import sys


def reuse_addr(sock):
    """Let a restarted listener take its port back while old connections linger
    (SO_REUSEADDR). Not on Windows: there SO_REUSEADDR lets a second listener share a
    port that's in use (two `audiolink host`s on one control port), and a port in
    TIME_WAIT can be bound without it anyway."""
    if sys.platform != "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
