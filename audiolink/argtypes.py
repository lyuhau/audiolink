"""argparse types for the command line: a bad value is a one-line usage error that
says what's wanted, not a traceback later on."""
import argparse


def _port(text, zero_ok=False):
    try:
        port = int(text)
    except ValueError:
        return None
    return port if (0 if zero_ok else 1) <= port <= 65535 else None


def _target(text):
    """'22' -> ('127.0.0.1', 22); 'host:22' -> ('host', 22); None if malformed."""
    host, sep, port = text.rpartition(":")
    port = _port(port)
    if port is None or (sep and not host):
        return None
    return (host if sep else "127.0.0.1"), port


def positive_int(text):
    try:
        n = int(text)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError("wants a whole number above 0, e.g. 50")
    return n


def port(text):
    """A local port to listen on; 0 = off."""
    if _port(text, zero_ok=True) is None:
        raise argparse.ArgumentTypeError("wants a port number (1-65535), or 0 for off")
    return int(text)


def allow(text):
    """--allow [HOST:]PORT[,...]: kept as text (services/streams.py parses it)."""
    for part in text.split(","):
        if part and _target(part) is None:
            raise argparse.ArgumentTypeError("wants [HOST:]PORT, e.g. 22 or 192.168.1.5:80")
    return text


def audio_format(text):
    """--audio-format auto|baseline|RATExCH: kept as text; only formats the link can run."""
    if text in ("auto", "baseline"):
        return text
    from .host.audio_format import parse_format
    try:
        parse_format(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError("%s (or auto)" % e)
    return text


def _pair(text, wants):
    lport, sep, target = text.partition(":")
    if not sep or _port(lport) is None or _target(target) is None:
        raise argparse.ArgumentTypeError("wants " + wants)
    return text


def forward(text):
    """--forward LPORT:[HOST:]PORT: kept as text (services/streams.py parse_forward parses it)."""
    return _pair(text, "LPORT:[HOST:]PORT, e.g. 8080:22 or 8080:192.168.1.5:80")


def expose(text):
    """--expose PIPORT:[HOST:]PORT, likewise."""
    return _pair(text, "PIPORT:[HOST:]PORT, e.g. 2222:22")
