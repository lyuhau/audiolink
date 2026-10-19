"""`audiolink host` startup: every port before the Pi's audio device; what the Pi
says about --expose and what the host tells its user (host/console.py)."""
import argparse
import json
import socket
import threading
import unittest
from unittest import mock

from audiolink import argtypes
from audiolink.host.console import HostConsole
from audiolink.services.streams import Streams
from audiolink.ui import LogGate


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def taken_port():
    """A listening socket (kept open by the test) and its port."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


class Startup(unittest.TestCase):
    def run_host(self, control_port, **kw):
        from audiolink.__main__ import run_host
        a = argparse.Namespace(**dict(dict(wifi=None, expose=[], forward=[], audio_format="auto",
                                           proxy_port=0, shell_port=0, verbose=True, out_device=None,
                                           in_device=None), **kw))
        opened = []
        with mock.patch("audiolink.wire.host_audio.run_portaudio", lambda *x, **k: opened.append(1)), \
                self.assertRaises(SystemExit) as e:
            run_host(a, [], control_port, LogGate(mock.Mock()))
        self.assertEqual(opened, [])            # stopped before the audio device
        return str(e.exception.code)

    def test_second_host_stops_before_the_audio_device(self):
        from audiolink.host import control
        port = free_port()
        srv = control.listen(port)              # as the first host took it (on Windows its socket
                                                # options once let a second host share the port)
        self.addCleanup(srv.close)

        def answer():                           # a running host's control port
            conn, _ = srv.accept()
            with conn:
                conn.makefile("r").readline()
                conn.sendall((json.dumps({"ok": True, "result": {"connected": True}}) + "\n").encode())
        threading.Thread(target=answer, daemon=True).start()
        self.assertEqual(self.run_host(port), "audiolink host is already running")

    def test_control_port_taken_by_something_else(self):
        srv, port = taken_port()
        self.addCleanup(srv.close)
        threading.Thread(target=lambda: srv.accept()[0].close(), daemon=True).start()
        self.assertIn("taken by another program", self.run_host(port))

    def test_proxy_port_in_use(self):
        srv, port = taken_port()
        self.addCleanup(srv.close)
        msg = self.run_host(free_port(), proxy_port=port)
        self.assertEqual(msg, "Port %d (the browse-as-the-Pi proxy) is in use; pick another: "
                              "--proxy-port PORT, or turn it off: --proxy-port 0" % port)


class Args(unittest.TestCase):
    def test_values(self):
        self.assertEqual(argtypes.expose("2222:22"), "2222:22")
        self.assertEqual(argtypes.forward("8080:10.0.0.5:80"), "8080:10.0.0.5:80")
        self.assertEqual(argtypes.allow("22,10.0.0.5:80"), "22,10.0.0.5:80")
        for fn, bad in ((argtypes.expose, "2222"), (argtypes.forward, "99999:22"), (argtypes.allow, "x"),
                        (argtypes.expose, "2222:host:"), (argtypes.port_or_off, "-1"), (argtypes.port, "0"), (argtypes.port, "x"), (argtypes.positive_int, "0"),
                        (argtypes.audio_format, "0x0")):
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                fn(bad)


class ExposeReport(unittest.TestCase):
    def test_pi_says_what_it_took(self):
        srv, busy = taken_port()
        self.addCleanup(srv.close)
        s = Streams("pi", expose_ports="1024-65535")
        s.session = mock.Mock()
        r = s._apply_expose([[80, "127.0.0.1", 22], [2222, "127.0.0.1", 22]])
        self.assertEqual(r["ok"], [[2222, "127.0.0.1", 22]])
        self.assertEqual(r["refused"], [[80, "outside the ports it allows (1024-65535)"]])
        # Nothing listens on the Pi any more (streams go through its endpoint): a port in use is fine.
        self.assertEqual(s._apply_expose([[busy, "127.0.0.1", 22]])["ok"], [[busy, "127.0.0.1", 22]])
        self.assertEqual((s.ports(), s.listeners), ([busy], {}))


class Console(unittest.TestCase):
    def console(self, caps=("expose_report",), busy=False):
        c = HostConsole([(2222, ("127.0.0.1", 22))], busy=lambda: busy)
        c.session = mock.Mock(peer_info={"identity": {"name": "green-rowan"}, "caps": list(caps)})
        return c

    def lines(self, fn):
        with self.assertLogs("audiolink", "DEBUG") as cm:
            fn()
        return [r.getMessage() for r in cm.records if r.levelname != "DEBUG"]

    def test_connected_with_what_the_pi_took(self):
        c = self.console()
        c.on_peer_hello(c.session.peer_info)    # waits for the Pi's report
        out = self.lines(lambda: c.on_event("exposed", {"ok": [[2222, "127.0.0.1", 22]], "refused": [[80, "why"]]}))
        self.assertEqual(out, ["Connected to green-rowan. Pi port 2222 → this computer's port 22",
                               "green-rowan refused to expose port 80: why"])
        self.assertEqual(self.lines(c.on_down), ["Lost the Pi; waiting for it"])

    def test_older_pi_and_format_changes(self):
        c = self.console(caps=())
        out = self.lines(lambda: c.on_peer_hello(c.session.peer_info))
        self.assertEqual(out, ["Connected to green-rowan. Pi port 2222 → this computer's port 22"])
        c.busy = lambda: True                   # a format change: link restarts aren't news
        self.assertEqual(self.lines(c.on_down), [])
        self.assertEqual(self.lines(lambda: c.on_peer_hello(c.session.peer_info)), [])


if __name__ == "__main__":
    unittest.main()
