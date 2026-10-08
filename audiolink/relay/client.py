"""Client side: reach a Pi's port through a relay."""
import ssl

from . import DEFAULT, parse_relay
from .tunnel import open_port
from .wire import VERSION, Refused, answer, close, dial, send_line

class Denied(Refused):
    """The Pi itself said no (not paired, port not open), or wasn't the Pi."""


WAIT = 20           # s for the relay to get the Pi to dial in, and the Pi to answer


def connect(pub, secret, port, relays=()):
    """A socket carrying the Pi's `port`, through the first relay that has the Pi
    (relays first, then the default). Raises Refused: with the Pi's answer if it said
    no, else with the last relay's reason."""
    why = "no relay"
    for spec in dict.fromkeys(list(relays) + [DEFAULT]):
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
            try:
                open_port(sock, pub, secret, port)      # the Pi's answer is final
            except (OSError, ValueError):
                close(sock)
                raise Refused("relay connection dropped")
            except Refused as e:
                close(sock)
                raise Denied(str(e))
            sock.settimeout(None)
            return sock
        close(sock)
    raise Refused(why)
