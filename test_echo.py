#!/usr/bin/env python3
"""Phase 2 end-to-end test: TCP echo through the audio link.

  client --TCP--> Pi:9000 --audio--> Mac audiolink --TCP--> echo server (127.0.0.1:9000)
         <--TCP-- Pi      <--audio-- Mac audiolink <--TCP--

Every byte crosses the audio link in both directions; the client checks that
what comes back is identical to what it sent.
"""
import argparse
import hashlib
import os
import socket
import threading
import time


def echo_server(port):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)

    def handle(c):
        with c:
            while data := c.recv(65536):
                c.sendall(data)

    def loop():
        while True:
            c, _ = srv.accept()
            threading.Thread(target=handle, args=(c,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()


def run_client(host, port, size):
    payload = os.urandom(size)
    s = socket.create_connection((host, port), timeout=60)
    got = bytearray()
    t0 = time.time()

    def sender():
        view = memoryview(payload)
        for i in range(0, size, 16384):
            s.sendall(view[i:i + 16384])
        s.shutdown(socket.SHUT_WR)

    threading.Thread(target=sender, daemon=True).start()
    last = time.time()
    while data := s.recv(65536):
        got += data
        if time.time() - last > 2:
            last = time.time()
            print(f"  {len(got):,}/{size:,} bytes echoed ({len(got) / (last - t0) / 1e3:.1f} KB/s)")
    dt = time.time() - t0
    ok = bytes(got) == payload
    print(f"{'PASS' if ok else 'FAIL'}: {len(got):,}/{size:,} bytes echoed in {dt:.2f}s "
          f"= {size / dt / 1e3:.1f} KB/s each way  sha256={hashlib.sha256(got).hexdigest()[:16]}")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="10.55.0.2")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--size", type=int, default=256 * 1024)
    ap.add_argument("--server-only", action="store_true")
    ap.add_argument("--client-only", action="store_true")
    a = ap.parse_args()
    if not a.client_only:
        echo_server(a.port)
    if a.server_only:
        threading.Event().wait()
    raise SystemExit(0 if run_client(a.host, a.port, a.size) else 1)
