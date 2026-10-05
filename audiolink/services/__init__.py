"""Services: features carried over a session (stream forwarding, Wi-Fi, proxy, ...).

A service is registered with the Session explicitly (see __main__) and can:
  caps            capability names advertised in the HELLO
  msg_types       link message types it owns (e.g. streams: OPEN/DATA/CLOSE)
  hello()         extra fields for our HELLO
  on_peer_hello   react to the peer's HELLO
  on_up/on_down   the link came up (new or restarted peer) / the peer went silent
  handle(op, a)   serve control requests addressed to "<name>.<op>"
  on_event        one-way events from the peer
  on_message      link messages of its msg_types
Every hook is optional.
"""


class Service:
    name = ""               # control-request area, e.g. "wifi" for "wifi.scan"
    caps = ()
    msg_types = ()

    def attach(self, session):
        self.session = session

    def hello(self):
        return {}

    def on_peer_hello(self, info):
        pass

    def on_up(self):
        pass

    def on_down(self):
        pass

    def handle(self, op, args):
        raise ValueError("%s does not support %r" % (self.name or type(self).__name__, op))

    def on_event(self, name, data):
        pass

    def on_message(self, typ, sid, payload):
        pass
