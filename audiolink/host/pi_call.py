"""A caller for the Pi's services: over USB through the running host (control.call), or
over the network to PI (--pi; update_cli.NetCaller). call(op, args) -> its result."""


def pi_caller(a, cap):
    """(call, over the network?) for commands with --pi/--at and --control-port; SystemExit
    if there's no Pi to ask, or its audiolink doesn't have `cap` yet."""
    if getattr(a, "pi", None):
        from ..client.remote import open_remote
        from .update_cli import NetCaller
        return NetCaller(open_remote(a.pi, a.at or [])), True
    from . import control
    port = getattr(a, "control_port", None) or control.DEFAULT_PORT
    peer = control.call("peer", port=port)
    if not peer.get("connected"):
        raise SystemExit("No Pi plugged in (or over the network: --pi PI)")
    if cap not in (peer.get("peer") or {}).get("caps", []):
        raise SystemExit("The Pi's audiolink is older than this: audiolink update (new code) first")
    return (lambda op, args=None, port=port: control.call(op, args, port=port)), False


def pi_connected(a, cap):
    """Is a Pi with `cap` connected through the running host? (No questions, no errors.)"""
    try:
        pi_caller(a, cap)
        return True
    except (SystemExit, OSError, ValueError):
        return False
