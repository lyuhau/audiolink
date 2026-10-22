"""Client side: reach a Pi's endpoint through a relay (docs/protocol.md "Relay").

The relay joins this connection to one the Pi dials in, and the Pi splices that to its
own endpoint: what comes back is a plain byte stream to the endpoint, over which the
client runs TLS (pinned to the Pi's key), hello and auth, as on the LAN.
"""
import ssl

from . import DEFAULT, parse_relay
from .wire import VERSION, Refused, answer, close, dial, send_line

WAIT = 20           # s for the relay to get the Pi to dial in


def connect(pub, relays=()):
    """A socket carrying the Pi's endpoint, through the first relay that has the Pi
    (relays first, then the default, unless it's off). Raises Refused with the last relay's reason
    ("offline": the Pi isn't on it)."""
    why = "no relay"
    for spec in dict.fromkeys(list(relays) + [DEFAULT]):
        if spec == "off":
            continue
        addr, pin = parse_relay(spec)
        try:
            sock = dial(addr, pin, timeout=WAIT)
        except (OSError, ssl.SSLError):
            why = "relay unreachable"
            continue
        try:
            send_line(sock, {"v": VERSION, "role": "client", "pub": pub.hex()})
            answer(sock)
        except Refused as e:
            why = str(e)
        except (OSError, ValueError):
            why = "relay connection dropped"
        else:
            sock.settimeout(None)
            return sock
        close(sock)
    raise Refused(why)
