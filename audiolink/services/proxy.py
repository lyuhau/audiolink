"""Small HTTP proxy that runs on the Pi, for browsing *as the Pi*.

Used for captive portals: a portal authorizes the device that logs in, so the
login page has to be loaded from the Pi. The host reaches this proxy through the
audio link; it listens only on the Pi's loopback interface.

Supports CONNECT (HTTPS) and absolute-URI requests (plain HTTP). Every request
gets its own upstream connection (Connection: close), which keeps it simple.
"""
import logging
import socket
import threading
import time
from urllib.parse import urlsplit

from . import Service

log = logging.getLogger("audiolink")

PROXY_PORT = 3128


def _splice(a, b):
    """Copy bytes both ways until either side closes."""
    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    t = threading.Thread(target=pump, args=(b, a), daemon=True)
    t.start()
    pump(a, b)
    t.join()


def _handle(client, on_request):
    on_request()
    with client:
        try:
            client.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(65536)
                if not chunk:
                    return
                head += chunk
                if len(head) > 65536:
                    return
            head, _, rest = head.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                upstream = socket.create_connection((host, int(port)), timeout=15)
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
            else:
                url = urlsplit(target)
                if url.scheme != "http" or not url.hostname:
                    client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                    return
                upstream = socket.create_connection((url.hostname, url.port or 80), timeout=15)
                path = (url.path or "/") + ("?" + url.query if url.query else "")
                headers = [h for h in lines[1:] if h.split(":", 1)[0].strip().lower()
                           not in ("proxy-connection", "connection", "keep-alive")]
                req = "%s %s %s\r\n%s\r\nConnection: close\r\n\r\n" % (
                    method, path, version, "\r\n".join(headers))
                upstream.sendall(req.encode("latin-1") + rest)
            client.settimeout(None)
            upstream.settimeout(None)
            with upstream:
                _splice(client, upstream)
        except (OSError, ValueError) as e:
            log.debug("proxy: %s", e)
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass


def serve(port=PROXY_PORT, on_request=lambda: None):
    """on_request() runs for every proxied connection (used to notice demand for
    the Pi's internet while a captive portal is blocking it)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(32)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
                threading.Thread(target=_handle, args=(conn, on_request), daemon=True).start()
            except (OSError, RuntimeError) as e:     # e.g. out of file descriptors or threads
                log.warning("proxy: %s", e)
                time.sleep(1)

    threading.Thread(target=loop, daemon=True).start()
    log.info("proxy for browsing as the Pi on 127.0.0.1:%d", port)


class ProxyService(Service):
    """Pi side: runs the proxy; the host reaches it with an ordinary stream to
    127.0.0.1:PROXY_PORT (which the Pi's stream policy has to allow)."""
    caps = ("proxy",)
    target = ("127.0.0.1", PROXY_PORT)

    def __init__(self, on_request=lambda: None):
        serve(on_request=on_request)
