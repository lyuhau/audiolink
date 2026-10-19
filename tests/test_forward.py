"""audiolink forward: a local listener whose connections each go to a Pi's port (a fake
Pi's stream: one end of a socket pair, the other end echoing)."""
import socket
import threading
import unittest
from unittest import mock

from audiolink.__main__ import build_parser
from audiolink.client import forward, keyring
from tests.test_cli import KeyringCase, new_code


def echo(sock):
    """The Pi's side: everything back, then a half-close when the client's side ends."""
    def run():
        while True:
            data = sock.recv(65536)
            if not data:
                break
            sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
    threading.Thread(target=run, daemon=True).start()


class Serving(unittest.TestCase):
    def setUp(self):
        self.srv = forward.listen(0)
        self.port = self.srv.getsockname()[1]
        self.addCleanup(self.srv.close)
        threading.Thread(target=forward.serve, args=(self.srv, "kitchen", 2222, []), daemon=True).start()
        for name, value in (("PAUSE", 0.05), ("RETRY", 0.5)):
            p = mock.patch.object(forward, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.conns = []

    def pi_stream(self, *args):
        mine, theirs = socket.socketpair()
        echo(theirs)
        conn = mock.Mock(sock=mine, ip="10.0.0.5")
        conn.close.side_effect = mine.close
        self.conns.append((conn, theirs))
        self.addCleanup(theirs.close)
        return conn

    def client(self):
        c = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(c.close)
        return c

    def read_all(self, c):
        got = b""
        while True:
            data = c.recv(65536)
            if not data:
                return got
            got += data

    def test_listens_on_localhost_only(self):
        self.assertEqual(self.srv.getsockname()[0], "127.0.0.1")

    def test_bytes_both_ways_then_both_closed(self):
        with mock.patch("audiolink.client.find._connect", side_effect=self.pi_stream) as connect:
            c = self.client()
            c.sendall(b"SSH-2.0-test\r\n" + b"x" * 200000)
            c.shutdown(socket.SHUT_WR)
            self.assertEqual(self.read_all(c), b"SSH-2.0-test\r\n" + b"x" * 200000)
        args = connect.call_args[0][0]
        self.assertEqual((args.pi, args.port, args.at), ("kitchen", 2222, []))
        conn, _ = self.conns[0]
        for _ in range(50):
            if conn.close.called:
                break
            threading.Event().wait(0.02)
        conn.close.assert_called_once()

    def test_each_connection_finds_the_pi_again(self):    # a host restart ends only the open ones
        with mock.patch("audiolink.client.find._connect", side_effect=self.pi_stream) as connect:
            for i in range(3):
                c = self.client()
                c.sendall(b"%d" % i)
                c.shutdown(socket.SHUT_WR)
                self.assertEqual(self.read_all(c), b"%d" % i)
        self.assertEqual(connect.call_count, 3)

    def test_keeps_trying_while_the_host_comes_back(self):
        tries = [SystemExit("kitchen isn't offering port 2222"), SystemExit("kitchen is offline"), None]

        def connect(a):
            t = tries.pop(0)
            if t:
                raise t
            return self.pi_stream()
        with mock.patch("audiolink.client.find._connect", side_effect=connect):
            c = self.client()
            c.sendall(b"hi")
            c.shutdown(socket.SHUT_WR)
            self.assertEqual(self.read_all(c), b"hi")
        self.assertEqual(tries, [])

    def test_gives_up_and_says_why(self):
        err = mock.Mock()
        with mock.patch("audiolink.client.find._connect", side_effect=SystemExit("kitchen is offline")), \
                mock.patch.object(forward, "say", err):
            c = self.client()
            self.assertEqual(self.read_all(c), b"")         # closed: ssh says so and sshfs reconnects
        said = " ".join(call.args[0] for call in err.call_args_list)
        self.assertIn("kitchen is offline (gave up after", said)

    def test_stops_trying_when_the_client_hangs_up(self):
        calls = []

        def connect(a):
            calls.append(1)
            raise SystemExit("kitchen is offline")
        with mock.patch("audiolink.client.find._connect", side_effect=connect), \
                mock.patch.object(forward, "RETRY", 30), mock.patch.object(forward, "say"):
            c = self.client()
            threading.Event().wait(0.1)
            c.close()
            threading.Event().wait(0.4)
            n = len(calls)
            threading.Event().wait(0.3)
        self.assertEqual(len(calls), n)
        self.assertLess(n, 6)


class Command(KeyringCase):
    def test_a_taken_port_says_so(self):
        taken = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(taken.close)
        with self.assertRaises(SystemExit) as e:
            forward.listen(taken.getsockname()[1])
        self.assertIn("Can't listen on 127.0.0.1:%d here" % taken.getsockname()[1], str(e.exception.code))

    def test_unknown_pi_ends_before_listening(self):
        with mock.patch.object(forward, "listen") as listen:
            _, _, msg = self.run_cmd(forward.forward_command, pi="nope", port=2222, local=None)
        self.assertEqual(msg, "No Pi named nope (see: audiolink pis)")
        listen.assert_not_called()

    def test_prints_the_ssh_line(self):
        code = new_code()
        keyring.upsert(code, name="kitchen")
        srv = mock.Mock()
        with mock.patch.object(forward, "listen", return_value=srv) as listen, \
                mock.patch.object(forward, "serve", side_effect=KeyboardInterrupt):
            out, _, msg = self.run_cmd(forward.forward_command, pi="kitchen", port=2222, local=2223)
        listen.assert_called_once_with(2223)
        srv.close.assert_called_once()
        self.assertIsNone(msg)
        self.assertIn("Forwarding 127.0.0.1:2223 to kitchen's port 2222", out)
        self.assertIn("ssh -p 2223 -o HostKeyAlias=audiolink-%s USER@127.0.0.1" % keyring.pub_of(code), out)

    def test_parses(self):
        a = build_parser().parse_args(["forward", "green-rowan", "2222", "--local", "2223"])
        self.assertEqual((a.cmd, a.pi, a.port, a.local, a.at), ("forward", "green-rowan", 2222, 2223, None))
        a = build_parser().parse_args(["forward", "green-rowan", "2222"])
        self.assertIsNone(a.local)


if __name__ == "__main__":
    unittest.main()
