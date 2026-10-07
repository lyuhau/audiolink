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
from ..ui import detail, done, step

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
        print("Sign-in page needed (run: audiolink wifi portal)")
        return False
    if not getattr(a, "portal", False):
        try:
            ans = input("Sign-in page needed. Open it now? [Y/n] ")
        except EOFError:
            ans = "n"
        if ans.strip().lower().startswith("n"):
            print("Later: audiolink wifi portal")
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
        raise SystemExit("%s: audiolink too old for sign-in pages (update it)" % pi)
    if not peer.get("proxy_port"):
        raise SystemExit("Off: audiolink host was started with --proxy-port 0")
    st = call("wifi.status", port=port)
    if not st["ssid"]:
        raise SystemExit("%s: not on Wi-Fi (pick a network: audiolink wifi)" % pi)
    r = call("wifi.check", port=port)
    if r["internet"] == "online":
        print("%s: already online (%s); nothing to sign in to" % (pi, st["ssid"]))
        return
    if r["internet"] == "offline":
        print("%s: no internet on %s; opening the sign-in page anyway" % (pi, st["ssid"]))
        detail("%s", r.get("detail", ""))
    url = r.get("portal_url") or "http://captive.apple.com/hotspot-detect.html"
    call("portal_active", port=port)        # the login window's own requests aren't "demand"
    browser = _open_browser(url, peer["proxy_port"])
    if browser:
        print("Opened a %s window browsing through %s; sign in to %s there" % (browser, pi, st["ssid"]))
        detail("a separate profile: your usual browser and this computer's network aren't affected")
        detail("page %s through the proxy at 127.0.0.1:%d", url, peer["proxy_port"])
    else:
        print("No Chrome, Edge or Brave found. In another browser, set HTTP proxy 127.0.0.1:%d" % peer["proxy_port"])
        print("(browses through %s) and open: %s" % (pi, url))
    if not wait:
        return
    step("Waiting for %s to get online (Ctrl-C to stop)" % pi)
    deadline = time.monotonic() + 15 * 60
    try:
        while time.monotonic() < deadline:
            time.sleep(3)
            call("portal_active", port=port)
            if call("wifi.check", port=port)["internet"] == "online":
                done("online")
                print("You can close that browser window")
                return
    except KeyboardInterrupt:
        done("stopped")
        raise SystemExit("Check later: audiolink wifi status")
    done("timed out")
    raise SystemExit("%s: still not online after 15 min" % pi)


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
            log.warning("%s: sign-in page on %s%s", pi, ssid, " (expired)" if expired else "")
        else:
            log.warning("%s: internet needed, but %s is waiting for sign-in (run: audiolink wifi portal)", pi, ssid)
        # Ask when it first happens; after a "no", ask again only on demand.
        if not self.interactive or (first and self.declined) or (not first and not self.declined):
            return
        if not self.prompt_lock.acquire(blocking=False):
            return
        self.gate.pause()
        try:
            try:
                ans = input("Open the sign-in page for %s? [Y/n] " % ssid)
            except EOFError:
                ans = "n"
            if ans.strip().lower().startswith("n"):
                self.declined = True
                print("Later: audiolink wifi portal (asking again when something needs %s's internet)" % pi,
                      flush=True)
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
