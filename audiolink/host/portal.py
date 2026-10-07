"""Captive portals, host side.

A portal authorizes the device that logs in, so the login page has to load *from
the Pi*: a separate browser instance uses the host daemon's local proxy, which the
Pi serves over the link (services/proxy.py). PortalWatcher reacts to the Pi's
connectivity events in the daemon.
"""
import logging
import os
import subprocess
import sys
import threading
import time

from ..services import Service
from ..paths import cache_dir
from .control import call
from ..ui import detail

log = logging.getLogger("audiolink")


def _pi(port=None, peer=None):
    """The plugged-in Pi's name, for messages."""
    try:
        peer = peer or call("peer", port=port)
        return ((peer.get("peer") or {}).get("identity") or {}).get("name") or "the Pi"
    except SystemExit:
        return "the Pi"


def offer_portal(a, port, wait=True):
    """A captive portal is in the way: open the login page if asked to (--portal) or
    if the user says yes; otherwise say how to do it later. Returns True if opened."""
    if getattr(a, "no_portal", False) or (not getattr(a, "portal", False) and not sys.stdin.isatty()):
        print("This network wants you to sign in before it gives %s internet.\n"
              "To sign in from this computer: audiolink wifi portal" % _pi(port))
        return False
    if not getattr(a, "portal", False):
        try:
            ans = input("This network wants you to sign in before it gives %s internet.\n"
                        "Open the sign-in page now? [Y/n] " % _pi(port))
        except EOFError:
            ans = "n"
        if ans.strip().lower().startswith("n"):
            print("OK. To sign in later: audiolink wifi portal")
            return False
    portal_flow(port, wait=wait)
    return True


def _find_browser():
    """A Chromium-family browser, which can run as a separate instance with its own
    proxy (no system or main-profile settings touched). Returns (kind, path)."""
    if sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            for base in ("/Applications", os.path.expanduser("~/Applications")):
                if os.path.isdir(os.path.join(base, app + ".app")):
                    return "mac-app", app
    elif sys.platform == "win32":
        roots = [os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe",
                    r"BraveSoftware\Brave-Browser\Application\brave.exe"):
            for root in roots:
                path = os.path.join(root, rel)
                if root and os.path.isfile(path):
                    return "exe", path
    return None, None


def _open_browser(url, proxy_port):
    profile = str(cache_dir() / "portal-browser")
    args = ["--user-data-dir=" + profile, "--proxy-server=http://127.0.0.1:%d" % proxy_port,
            "--no-first-run", "--no-default-browser-check", "--new-window", url]
    kind, browser = _find_browser()
    if kind == "mac-app":
        subprocess.Popen(["open", "-na", browser, "--args"] + args)
    elif kind == "exe":
        subprocess.Popen([browser] + args)
    else:
        return None
    return browser


def portal_flow(port, wait=True):
    """Open the portal's login page in a browser that browses as the Pi. wait=True
    polls until the Pi is online; the daemon uses wait=False (its connectivity
    events report when the Pi gets online)."""
    peer = call("peer", port=port)
    pi = _pi(peer=peer)
    if "proxy" not in (peer["peer"] or {}).get("caps", []):
        raise SystemExit("%s's audiolink is too old for signing in to networks from here; update it." % pi)
    if not peer.get("proxy_port"):
        raise SystemExit("audiolink host was started with --proxy-port 0, which turns this off. "
                         "Restart it without that.")
    st = call("wifi.status", port=port)
    if not st["ssid"]:
        raise SystemExit("%s isn't on a Wi-Fi network. Pick one first: audiolink wifi" % pi)
    r = call("wifi.check", port=port)
    if r["internet"] == "online":
        print("%s is already online on %s. There's nothing to sign in to." % (pi, st["ssid"]))
        return
    if r["internet"] == "offline":
        print("%s gets no answer from the internet on %s. Trying the sign-in page anyway." % (pi, st["ssid"]))
        detail("%s", r.get("detail", ""))
    url = r.get("portal_url") or "http://captive.apple.com/hotspot-detect.html"
    call("portal_active", port=port)        # the login window's own requests aren't "demand"
    browser = _open_browser(url, peer["proxy_port"])
    if browser:
        print("Opened a separate %s window that browses through %s." % (browser, pi))
        print("Sign in to %s there. Your usual browser and this computer's network aren't affected." % st["ssid"])
        detail("page %s through the proxy at 127.0.0.1:%d", url, peer["proxy_port"])
    else:
        print("I couldn't find Chrome, Edge or Brave. In another browser, set its HTTP proxy to")
        print("127.0.0.1:%d (that browses through %s), then open: %s" % (peer["proxy_port"], pi, url))
    if not wait:
        return
    print("Waiting for %s to get online (Ctrl-C to stop waiting)..." % pi, flush=True)
    deadline = time.monotonic() + 15 * 60
    try:
        while time.monotonic() < deadline:
            time.sleep(3)
            call("portal_active", port=port)
            if call("wifi.check", port=port)["internet"] == "online":
                print("%s is online. You can close that browser window." % pi)
                return
    except KeyboardInterrupt:
        print()
        raise SystemExit("Stopped waiting. To check later: audiolink wifi status")
    raise SystemExit("%s still isn't online after 15 minutes." % pi)


class PortalWatcher(Service):
    """In the host daemon. When the Pi lands behind a captive portal (joined one, or
    its login expired): warn, and if there's a terminal, offer to open the login
    page. If the user declines, stay quiet until something needs the Pi's internet,
    then ask again. Without a terminal, only log."""

    def __init__(self, port, gate):
        self.port, self.gate = port, gate
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty()
        self.state = None
        self.declined = False
        self.ready = False              # no prompts until startup (`--wifi`) is done
        self.prompt_lock = threading.Lock()
        self.portal_active_until = 0.0  # a login window is open: its requests aren't "demand"

    def local_ops(self):
        """Control-port ops answered by the daemon (see portal_flow)."""
        return {"portal_active": self._mark_active}

    def _mark_active(self, args):
        self.portal_active_until = time.monotonic() + 30

    def start(self, skip_initial=False):
        """skip_initial: startup (`--wifi`) already offered the login, so treat the
        answer as given and only ask again on demand."""
        self.ready = True
        if self.state and self.state["internet"] == "portal":
            if skip_initial:
                self.declined = True
            else:
                self._portal_blocking(first=True)

    def on_event(self, name, data):
        if name == "internet":
            prev, self.state = self.state, data
            if data["internet"] == "online":
                if prev and prev["internet"] != "online":
                    log.info("%s is online%s", self._name(), " on %s" % data["ssid"] if data.get("ssid") else "")
                self.declined = False
            elif data["internet"] == "portal" and self.ready and \
                    not (prev and prev["internet"] == "portal" and prev.get("ssid") == data.get("ssid")):
                self._portal_blocking(first=True, expired=bool(prev and prev["internet"] == "online"))
        elif name == "portal_needed" and self.ready:
            self.state = data
            if time.monotonic() < self.portal_active_until:
                return                          # it's the login window itself
            self._portal_blocking(first=False)

    def _name(self):
        return _pi(peer={"peer": self.session.peer_info})

    def _portal_blocking(self, first, expired=False):
        ssid = self.state.get("ssid")
        pi = self._name()
        if first:
            log.warning("%s needs you to sign in to %s%s before it gets internet", pi, ssid,
                        " again (the sign-in expired)" if expired else "")
        else:
            log.warning("something needs %s's internet, but %s is waiting for you to sign in: "
                        "audiolink wifi portal", pi, ssid)
        # Ask when it first happens; after a "no", ask again only on demand.
        if not self.interactive or (first and self.declined) or (not first and not self.declined):
            return
        if not self.prompt_lock.acquire(blocking=False):
            return
        self.gate.pause()
        try:
            try:
                ans = input("Open the sign-in page for %s now? [Y/n] " % ssid)
            except EOFError:
                ans = "n"
            if ans.strip().lower().startswith("n"):
                self.declined = True
                print("OK. To sign in later: audiolink wifi portal (I'll ask again when something "
                      "needs %s's internet)" % pi, flush=True)
            else:
                self.declined = False
                try:
                    portal_flow(self.port, wait=False)
                except SystemExit as e:
                    if isinstance(e.code, str):
                        print(e.code, flush=True)
        finally:
            self.gate.resume()
            self.prompt_lock.release()
