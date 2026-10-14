"""What `audiolink host` tells its user about the Pi: connected (with the ports it
exposes for this computer), refused exposes, lost. Link restarts while the audio
format changes (busy()) aren't news: they stay -v detail."""
import logging

from ..services import Service

log = logging.getLogger("audiolink")


def _target(host, port):
    return "this computer's port %d" % port if host in ("127.0.0.1", "localhost") else "%s:%d" % (host, port)


class HostConsole(Service):
    def __init__(self, expose=(), busy=lambda: False):
        self.expose = list(expose)      # what we asked for: [(pi_port, (host, port))]
        self.busy = busy
        self.said = None                # the last "Connected ..." line, until the Pi is lost

    def _name(self):
        return ((self.session.peer_info or {}).get("identity") or {}).get("name") or "the Pi"

    def _say(self, ok, refused=()):
        name = self._name()
        line = "Connected to %s" % name
        if ok:
            line += ". " + "; ".join("Pi port %d → %s" % (p, _target(h, tp)) for p, h, tp in ok)
        for port, why in refused:
            line += "\n%s refused to expose port %d: %s" % (name, port, why)
        if line != self.said:
            self.said = line
            for i, text in enumerate(line.split("\n")):
                (log.info if not i else log.warning)("%s", text)
        elif self.busy():
            log.debug("%s (again)", line)

    def on_peer_hello(self, info):
        if "expose_report" not in info.get("caps", []):     # an older Pi: assume it took them
            self._say([[p, h, tp] for p, (h, tp) in self.expose])

    def on_event(self, name, data):
        if name == "exposed" and isinstance(data, dict):
            self._say(data.get("ok") or [], data.get("refused") or [])

    def on_down(self):
        if self.busy():
            log.debug("link down during an audio format change")
            return
        if self.said:
            log.warning("Lost the Pi; waiting for it")
        self.said = None
