"""`audiolink logs [FILE] [-n N] [--list] [--pi PI [--key FILE]]`: the Pi's logs, over the link (through
the running host) or over the network (services/logs.py)."""


def logs_command(a):
    if (a.key or a.at) and not a.pi:
        raise SystemExit("--key and --at go with --pi PI (over the network)")
    if a.pi:
        from ..client.remote import open_remote
        remote = open_remote(a.pi, a.at or [], a.key)

        def call(op, args=None, port=None):
            return remote.call("logs", dict(args or {}, action=op.split(".", 1)[1]))["result"]
    else:
        from .control import call
        if not call("peer", port=a.control_port).get("connected"):
            raise SystemExit("No Pi plugged in (or over the network: --pi PI)")
    if a.list:
        print("\n".join(call("logs.list", port=a.control_port)["files"]))
    else:
        print(call("logs.tail", {"file": a.file, "lines": a.lines}, port=a.control_port)["text"])
