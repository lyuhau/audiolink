"""The internet relay: reach a Pi from anywhere, without opening anything on its network.

The Pi holds one outgoing connection to the relay (the control connection). A client
asks the relay for a Pi by public key; the relay tells the Pi, the Pi dials a fresh
data connection, and the relay joins the two. The relay is untrusted: the client and
the Pi authenticate each other end to end over the joined connection (see tunnel.py),
and the Pi decides which of its ports are reachable.

All relay connections are TLS to a self-signed certificate pinned by hash.
"""

DEFAULT = "relay.lyuhau.com:443"
# sha256 of the relay certificate (DER); keys/relay.crt, made once and reused on rebuilds
PIN = "b063132443c34f3074c07ffd69b4a13b74804f867a0104cb0dd2c1de8a26b2a3"


def parse_relay(spec):
    """'host:port' or 'host:port#pin' -> ((host, port), pin); the default pin when none."""
    addr, _, pin = spec.partition("#")
    return parse_addr(addr), pin or PIN


def parse_addr(spec, default_port=443):
    """'host:443' or 'host' -> ('host', 443); IPv6 as '[::1]:443', '[::1]' or '::1'."""
    if spec.startswith("["):
        host, _, rest = spec[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
        return host, int(port) if port.isdigit() else default_port
    host, sep, port = spec.rpartition(":")
    if not sep or not port.isdigit() or ":" in host:
        return spec, default_port
    return host, int(port)
