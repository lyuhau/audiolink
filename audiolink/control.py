"""Local control port of a running `audiolink host`, and the commands that use it.

The host daemon owns the audio device, so other commands (e.g. `audiolink wifi`)
talk to it over 127.0.0.1 with one JSON request and one JSON reply per connection.
"""
import json
import logging
import os
import socket
import sys
import threading
import time

log = logging.getLogger("audiolink")

DEFAULT_PORT = int(os.environ.get("AUDIOLINK_CONTROL_PORT", "47800"))


def serve(node, port=DEFAULT_PORT):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", port))
    except OSError as e:
        log.warning("control port 127.0.0.1:%d unavailable (%s); `audiolink wifi` won't work", port, e)
        return
    srv.listen(4)

    def handle(conn):
        with conn:
            try:
                req = json.loads(conn.makefile("r").readline())
                if req["op"] == "portal_active":
                    node.portal_active_until = time.monotonic() + 30
                    reply = {"ok": True, "result": None}
                elif req["op"] == "peer":
                    reply = {"ok": True, "result": {"connected": node.peer_alive, "peer": node.peer_info,
                                                    "proxy_port": getattr(node, "proxy_port", None)}}
                else:
                    reply = {"ok": True, "result": node.request(req["op"], req.get("args"), timeout=90)}
            except Exception as e:
                reply = {"ok": False, "error": str(e)}
            conn.sendall((json.dumps(reply) + "\n").encode())

    def loop():
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()


def call(op, args=None, port=DEFAULT_PORT):
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=120)
    except OSError:
        raise SystemExit("can't reach a running `audiolink host` on 127.0.0.1:%d; start it first" % port)
    with conn:
        conn.sendall((json.dumps({"op": op, "args": args or {}}) + "\n").encode())
        reply = json.loads(conn.makefile("r").readline())
    if not reply.get("ok"):
        raise SystemExit("error: " + reply.get("error", "failed"))
    return reply["result"]


# ---- `audiolink wifi ...`
SEC_LABEL = {"open": "open", "owe": "open+", "wpa2": "WPA2", "wpa3": "WPA3", "wpa2/wpa3": "WPA2/3",
             "wep": "WEP", "enterprise": "802.1X"}
FIVE_GHZ_SUFFIXES = ("_5g", "-5g", "_5ghz", "-5ghz", " 5g", "_5", "-5", " 5ghz", ".5g")


def _band(freq):
    return "5GHz" if freq and freq > 4000 else "2.4GHz" if freq else ""


class Prompter:
    """Interactive questions, or clear errors when there's no terminal to ask on."""

    def __init__(self, a):
        self.a = a
        self.tty = sys.stdin.isatty()
        self.stdin_pw = None
        if getattr(a, "password_stdin", False):
            self.stdin_pw = sys.stdin.readline().rstrip("\r\n")

    @staticmethod
    def _input(prompt):
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit("cancelled")

    def need_tty(self, what):
        if not self.tty:
            raise SystemExit("%s (no terminal to ask on; see --security, --hidden, --password-stdin)"
                             % what.rstrip(" ?:"))

    def yes(self, question, default=False):
        self.need_tty(question)
        ans = self._input("%s [%s] " % (question, "Y/n" if default else "y/N")).strip().lower()
        return default if not ans else ans.startswith("y")

    def choose(self, question, options):
        self.need_tty(question)
        for i, (label, _) in enumerate(options, 1):
            print("  %d) %s" % (i, label))
        while True:
            ans = self._input("%s [1-%d] " % (question, len(options))).strip()
            if ans.isdigit() and 1 <= int(ans) <= len(options):
                return options[int(ans) - 1][1]

    def password(self, ssid, security, retry=False):
        """Returns (password, can_retry)."""
        if self.stdin_pw is not None:
            return self.stdin_pw, False
        env = os.environ.get("AUDIOLINK_WIFI_PASSWORD")
        if env and not retry:
            return env, False
        import getpass
        self.need_tty("%r needs a password" % ssid)
        while True:
            try:
                pw = getpass.getpass("%s password for %r: " % (SEC_LABEL.get(security, security), ssid))
            except (EOFError, KeyboardInterrupt):
                print()
                raise SystemExit("cancelled")
            if security in ("wpa2", "wpa2/wpa3") and not 8 <= len(pw) <= 63:
                print("WPA2 passwords are 8-63 characters; try again")
                continue
            if not pw:
                continue
            return pw, True


def _suggest(name, nets):
    """Near-misses for a name the Pi can't see: (message, suggested ssid or None)."""
    visible = {n["ssid"] for n in nets}
    low = name.lower()
    for suf in FIVE_GHZ_SUFFIXES:
        if low.endswith(suf):
            base = name[:-len(suf)]
            twin = next((v for v in visible if v.lower() == base.lower()), None)
            if twin:
                return ("%r looks like a 5 GHz network, and the Pi only sees 2.4 GHz. "
                        "Its 2.4 GHz twin %r is visible." % (name, twin), twin)
            return ("%r looks like a 5 GHz network, and the Pi only sees 2.4 GHz." % name, None)
    case = [v for v in visible if v.lower() == low]
    if len(case) == 1:
        return ("no network named %r, but %r is visible." % (name, case[0]), case[0])
    close = [v for v in visible if low in v.lower() or v.lower() in low]
    if close:
        return ("no network named %r. Similar: %s" % (name, ", ".join(repr(c) for c in close[:5])), None)
    return ("no network named %r is visible to the Pi (2.4 GHz only)." % name, None)


def join_flow(name, a, port, known_security=None):
    """Work out what kind of network NAME is, ask only for what's needed, then join."""
    ask = Prompter(a)
    saved = {n["ssid"]: n for n in call("wifi.list", port=port)}
    security = getattr(a, "security", None) or ("open" if getattr(a, "open", False) else None) \
        or known_security
    hidden = getattr(a, "hidden", False)

    def attempt(**args):
        print("joining %r..." % name, flush=True)
        return call("wifi.join", dict(args, ssid=name), port=port)

    # 1. Saved: just switch to it; if its key is now rejected, ask for the new password.
    if name in saved and not security and not getattr(a, "reenter", False):
        r = attempt(use_saved=True)
        if r["joined"]:
            return _joined(r, a, port)
        if r["code"] != "auth_failed":
            raise SystemExit("couldn't join %r: %s" % (name, r["reason"]))
        print("the saved password for %r was rejected (changed?)" % name)
        security = saved[name]["security"]
        hidden = saved[name]["hidden"]

    # 2. Not saved: look it up in the Pi's scan.
    if not security and not hidden:
        nets = call("wifi.scan", {"max_age": 10}, port=port)
        matches = [n for n in nets if n["ssid"] == name]
        if not matches:
            nets = call("wifi.scan", port=port)           # scans occasionally miss a network
            matches = [n for n in nets if n["ssid"] == name]
        if not matches:
            msg, suggestion = _suggest(name, nets)
            print(msg)
            if suggestion and ask.yes("Use %r instead?" % suggestion, default=True):
                return join_flow(suggestion, a, port)     # start over: it may be saved
            elif ask.yes("Is %r a hidden network (name not broadcast)?" % name):
                hidden = True
                security = ask.choose("Security type:", [("WPA2 (most networks)", "wpa2"),
                                                         ("WPA3 only", "wpa3"), ("open", "open")])
            else:
                raise SystemExit(1)
        if matches:
            kinds = sorted({m["security"] for m in matches})
            if len(kinds) > 1:
                print("there are %d networks named %r:" % (len(kinds), name))
                security = ask.choose("Which one?", [(SEC_LABEL[k], k) for k in kinds])
            else:
                security = kinds[0]

    if hidden and not security:
        # Hidden networks aren't in the scan, so their security can't be detected.
        security = ask.choose("Security type of hidden network %r:" % name,
                              [("WPA2 (most networks)", "wpa2"), ("WPA3 only", "wpa3"), ("open", "open")])

    if security == "wep":
        raise SystemExit("%r uses WEP, which is obsolete and insecure; not supported" % name)
    if security == "enterprise":
        raise SystemExit("%r uses 802.1X/enterprise login (username + password); not supported yet" % name)

    # 3. Join, re-prompting on a wrong password.
    needs_pw = security in ("wpa2", "wpa3", "wpa2/wpa3")
    if security == "wpa3":
        print("note: WPA3 can't use a derived key, so the Pi will save this password itself")
    retry = False
    for _ in range(3):
        pw, can_retry = ask.password(name, security, retry) if needs_pw else (None, False)
        r = attempt(password=pw, security=security, hidden=hidden)
        if r["joined"]:
            return _joined(r, a, port)
        if r["code"] == "auth_failed" and can_retry:
            print("password incorrect; try again")
            retry = True
            continue
        break
    raise SystemExit("couldn't join %r: %s" % (name, r["reason"]))


def startup_join(node, name, a, port):
    """`audiolink host --wifi NAME`: before the daemon's logs start, wait for the Pi
    and run the join flow. A failed join doesn't stop the daemon: the Pi stays on
    (or falls back to) its saved networks."""
    _line_buffered()
    if not node.peer_alive:
        print("waiting for the Pi to connect (plug it in)...", flush=True)
    while not (node.peer_alive and node.peer_info):
        time.sleep(0.25)
    if "wifi" not in node.peer_info.get("caps", []):
        print("this Pi's audiolink doesn't support Wi-Fi management (update it); ignoring --wifi")
        return
    try:
        st = call("wifi.status", port=port)
        if st["ssid"] == name:
            print("the Pi is already on %r (IP %s)" % (name, st["ip"]))
            if st.get("internet") == "portal":
                offer_portal(a, port)
            return
        join_flow(name, a, port)
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code)
        print("continuing; the Pi stays on its saved networks. Retry with: audiolink wifi join %r" % name)


def _joined(r, a, port):
    print("joined %r (%s), Pi IP %s; saved" % (r["ssid"], SEC_LABEL.get(r.get("security"), "?"), r["ip"]))
    if r.get("internet") == "portal":
        offer_portal(a, port)
    elif r.get("internet") == "offline":
        print("note: the Pi has no internet on this network (%s)" % r.get("detail", "no response"))


def offer_portal(a, port, wait=True):
    """A captive portal is in the way: open the login page if asked to (--portal) or
    if the user says yes; otherwise say how to do it later. Returns True if opened."""
    if getattr(a, "no_portal", False) or (not getattr(a, "portal", False) and not sys.stdin.isatty()):
        print("this network has a login page (captive portal); open it with: audiolink wifi portal")
        return False
    if not getattr(a, "portal", False):
        try:
            ans = input("this network has a login page (captive portal). Open it now? [Y/n] ")
        except EOFError:
            ans = "n"
        if ans.strip().lower().startswith("n"):
            print("ok; open it later with: audiolink wifi portal")
            return False
    portal_flow(port, wait=wait)
    return True


# ---- `audiolink wifi portal`
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
    import subprocess
    from .portaudio import _cache_dir
    profile = str(_cache_dir() / "portal-browser")
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
    if "proxy" not in (peer["peer"] or {}).get("caps", []):
        raise SystemExit("this Pi's audiolink can't proxy a browser (update it)")
    if not peer.get("proxy_port"):
        raise SystemExit("the host was started with --proxy-port 0; restart it with a proxy port")
    st = call("wifi.status", port=port)
    if not st["ssid"]:
        raise SystemExit("the Pi isn't on a Wi-Fi network; join one first (audiolink wifi)")
    r = call("wifi.check", port=port)
    if r["internet"] == "online":
        print("the Pi is already online on %r; no login needed" % st["ssid"])
        return
    if r["internet"] == "offline":
        print("the Pi gets no response from the internet on %r (%s); trying the login page anyway"
              % (st["ssid"], r.get("detail", "")))
    url = r.get("portal_url") or "http://captive.apple.com/hotspot-detect.html"
    call("portal_active", port=port)        # the login window's own requests aren't "demand"
    browser = _open_browser(url, peer["proxy_port"])
    if browser:
        print("opened a separate %s window that browses as the Pi." % browser)
        print("log in to %r there; your normal browser and network settings are untouched." % st["ssid"])
    else:
        print("no Chrome/Edge/Brave found. In any browser, set an HTTP proxy of 127.0.0.1:%d"
              % peer["proxy_port"])
        print("(that browses as the Pi), then open: %s" % url)
    if not wait:
        return
    print("waiting for the Pi to get online (Ctrl-C to stop waiting)...", flush=True)
    deadline = time.monotonic() + 15 * 60
    try:
        while time.monotonic() < deadline:
            time.sleep(3)
            call("portal_active", port=port)
            if call("wifi.check", port=port)["internet"] == "online":
                print("the Pi is online. You can close that browser window.")
                return
    except KeyboardInterrupt:
        print()
        raise SystemExit("stopped waiting; check later with: audiolink wifi status")
    raise SystemExit("still not online after 15 minutes")


def pick_flow(a, port):
    """`audiolink wifi` with no action: numbered list from a fresh scan."""
    print("scanning from the Pi (it only sees 2.4 GHz)...", flush=True)
    nets = call("wifi.scan", port=port)
    st = call("wifi.status", port=port)
    opts = []
    for n in nets:
        tags = [SEC_LABEL[n["security"]]]
        if n["saved"]:
            tags.append("saved")
        if n["ssid"] == st["ssid"]:
            tags.append("connected")
        opts.append(("%-32s %4d dBm  %s" % (n["ssid"][:32], n["signal"], ", ".join(tags)), n))
    opts.append(("(hidden network: type its name)", None))
    choice = Prompter(a).choose("Join which network?", opts)
    if choice is None:
        name = Prompter._input("Network name: ").strip()
        a.hidden = True
        return join_flow(name, a, port)
    if choice["ssid"] == st["ssid"]:
        print("already connected to %r" % choice["ssid"])
        return
    return join_flow(choice["ssid"], a, port, known_security=choice["security"])


def _line_buffered():
    try:
        sys.stdout.reconfigure(line_buffering=True)   # keep prints in order with logs/errors
    except AttributeError:
        pass


def wifi_command(a):
    _line_buffered()
    port = a.control_port
    peer = call("peer", port=port)
    if not peer["connected"]:
        raise SystemExit("the host is running but the Pi isn't connected yet")
    if "wifi" not in (peer["peer"] or {}).get("caps", []):
        raise SystemExit("this Pi's audiolink doesn't support Wi-Fi management (update it)")

    if a.action is None:
        return pick_flow(a, port)
    if a.action == "scan":
        print("scanning from the Pi (it only sees 2.4 GHz)...", flush=True)
        nets = call("wifi.scan", port=port)
        print("%-32s %8s %-7s %-7s %s" % ("NETWORK", "SIGNAL", "BAND", "SEC", ""))
        for n in nets:
            print("%-32s %5d dBm %-7s %-7s %s" % (n["ssid"][:32], n["signal"], _band(n["freq"]),
                                                  SEC_LABEL[n["security"]], "saved" if n["saved"] else ""))
    elif a.action == "status":
        st = call("wifi.status", port=port)
        if st["ssid"]:
            print("connected to %r (%s), IP %s, internet: %s" % (st["ssid"], _band(st["freq"]),
                  st["ip"] or "none yet", st.get("internet", "?")))
            if st.get("internet") == "portal":
                print("this network has a login page (captive portal); run: audiolink wifi portal")
        else:
            print("not connected (state: %s)" % st["state"])
        print("saved networks: %s" % (", ".join(st["saved"]) or "none"))
    elif a.action == "list":
        nets = call("wifi.list", port=port)
        if not nets:
            print("no saved networks")
        for n in nets:
            print("%-32s %-7s%s" % (n["ssid"], SEC_LABEL.get(n["security"], n["security"]),
                                    " hidden" if n["hidden"] else ""))
    elif a.action == "join":
        if not a.name:
            raise SystemExit("usage: audiolink wifi join NAME")
        join_flow(a.name, a, port)
    elif a.action == "portal":
        portal_flow(port)
    elif a.action == "check":
        r = call("wifi.check", port=port)
        print("internet: %s%s" % (r["internet"], " on %r" % r["ssid"] if r.get("ssid") else ""))
        raise SystemExit({"online": 0, "portal": 2}.get(r["internet"], 3))
    elif a.action == "forget":
        if not a.name:
            raise SystemExit("usage: audiolink wifi forget NAME")
        call("wifi.forget", {"ssid": a.name}, port=port)
        print("forgot %r" % a.name)


# ---- daemon side: react to the Pi's connectivity events
class PortalWatcher:
    """In the host daemon. When the Pi lands behind a captive portal (joined one, or
    its login expired): warn, and if there's a terminal, offer to open the login
    page. If the user declines, stay quiet until something needs the Pi's internet,
    then ask again. Without a terminal, only log."""

    def __init__(self, node, port, gate):
        self.node, self.port, self.gate = node, port, gate
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty()
        self.state = None
        self.declined = False
        self.ready = False              # no prompts until startup (`--wifi`) is done
        self.prompt_lock = threading.Lock()
        node.portal_active_until = 0.0
        node.on_event = self.on_event

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
                    log.info("the Pi is online%s", " on %r" % data["ssid"] if data.get("ssid") else "")
                self.declined = False
            elif data["internet"] == "portal" and self.ready and \
                    not (prev and prev["internet"] == "portal" and prev.get("ssid") == data.get("ssid")):
                self._portal_blocking(first=True, expired=bool(prev and prev["internet"] == "online"))
        elif name == "portal_needed" and self.ready:
            self.state = data
            if time.monotonic() < self.node.portal_active_until:
                return                          # it's the login window itself
            self._portal_blocking(first=False)

    def _portal_blocking(self, first, expired=False):
        ssid = self.state.get("ssid")
        if first:
            log.warning("the Pi's Wi-Fi %r %s a captive portal login", ssid,
                        "login expired; it needs" if expired else "needs")
        else:
            log.warning("something needs the Pi's internet, but %r is waiting for a captive portal "
                        "login; run: audiolink wifi portal", ssid)
        # Ask when it first happens; after a "no", ask again only on demand.
        if not self.interactive or (first and self.declined) or (not first and not self.declined):
            return
        if not self.prompt_lock.acquire(blocking=False):
            return
        self.gate.pause()
        try:
            try:
                ans = input("open the login page for %r now? [Y/n] " % ssid)
            except EOFError:
                ans = "n"
            if ans.strip().lower().startswith("n"):
                self.declined = True
                print("ok; open it later with: audiolink wifi portal (I'll ask again when "
                      "something needs the Pi's internet)", flush=True)
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
