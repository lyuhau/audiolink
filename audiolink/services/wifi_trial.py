"""Pi side: try a Wi-Fi network without leaving the current one.

The Wi-Fi chip can be on two networks at once (on different channels it time-slices
the radio, so this is for a short check, not for staying). A trial brings up a second
interface with its own wpa_supplicant, joins the network, gets a DHCP lease and checks
for internet through it, then tears everything down. The Pi's own connection, routes
and DNS are never touched: the lease is recorded, not applied; the trial address gets
a private routing table (TABLE) used only by traffic from that address, and names are
resolved by asking the trial network's DNS server directly.

Used by services/mode.py before switching networks on request from the network, so a
wrong password, a captive portal or a network without DHCP is found out while the Pi
is still reachable where it is. Not possible while the setup network is up (the chip
allows an access point plus one client connection, on one channel).
"""
import logging
import os
import random
import shutil
import socket
import struct
import subprocess
import time
from pathlib import Path

from .wifi import IFACE, Supplicant, connectivity, security_of

log = logging.getLogger("audiolink")

TRIAL_IFACE = "wlan1"
RUN = Path("/run/audiolink-trial")
TABLE, RULE_PRIO = "47", "470"
DHCP_SCRIPT = """#!/bin/sh
case "$1" in bound|renew) echo "ip=$ip router=$router dns=$dns" > %s/lease ;; esac
""" % RUN


def _run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _mac():
    """A fresh, random, locally administered address for each trial: some access points
    (phone hotspots) ignore DHCP from an address that just left, for a while."""
    b = bytearray(os.urandom(6))
    b[0] = (b[0] | 0x02) & 0xFE
    return ":".join("%02x" % x for x in b)


def resolve_a(name, server, source, timeout=3):
    """One DNS A lookup sent from `source` to `server` (no system resolver involved)."""
    qid = random.getrandbits(16)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack(">HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.bind((source, 0))
        s.sendto(q, (server, 53))
        data, _ = s.recvfrom(2048)
    rid, flags, qd, an = struct.unpack(">HHHH", data[:8])
    if rid != qid or flags & 0x000F:
        raise OSError("DNS lookup of %s failed (rcode %d)" % (name, flags & 0x000F))

    def skip_name(i):
        while True:
            n = data[i]
            if n == 0:
                return i + 1
            if n & 0xC0 == 0xC0:
                return i + 2
            i += n + 1

    i = 12
    for _ in range(qd):
        i = skip_name(i) + 4
    for _ in range(an):
        i = skip_name(i)
        typ, _, _, rdlen = struct.unpack(">HHIH", data[i:i + 10])
        i += 10
        if typ == 1 and rdlen == 4:
            return socket.inet_ntoa(data[i:i + 4])
        i += rdlen
    raise OSError("no address for %s" % name)


def trial(ssid, ent, timeout=20):
    """Join `ssid` (saved-entry fields `ent`) on a second interface and check it.
    Returns {"ok": True|False|None, "code", "reason", "internet", "freq"}. ok False only
    when the network is certainly unusable (auth_failed, error); None when the trial
    couldn't tell (inconclusive, or no_ip: the password works but DHCP didn't answer
    in time); True: code ok."""
    log.info("wifi trial: trying %r without leaving the current network", ssid)
    _teardown()
    RUN.mkdir(parents=True, exist_ok=True)
    lease = None
    try:
        r = _run("iw", "dev", IFACE, "interface", "add", TRIAL_IFACE, "type", "managed")
        if r.returncode != 0:
            raise RuntimeError("can't add a second Wi-Fi interface: %s" % (r.stderr.strip() or r.stdout.strip()))
        _run("ip", "link", "set", TRIAL_IFACE, "address", _mac())
        _run("ip", "link", "set", TRIAL_IFACE, "up")
        conf = RUN / "wpa.conf"
        conf.write_text("ctrl_interface=%s\nupdate_config=0\n" % RUN)
        os.chmod(conf, 0o600)
        r = _run("wpa_supplicant", "-B", "-i", TRIAL_IFACE, "-c", str(conf), "-P", str(RUN / "wpa.pid"))
        if r.returncode != 0:
            raise RuntimeError("wpa_supplicant for the trial didn't start: %s" % r.stdout.strip()[-200:])
        sup = Supplicant(TRIAL_IFACE, str(RUN))
        for _ in range(20):
            try:
                sup.wpa("ping")
                break
            except Exception:
                time.sleep(0.2)
        nid = sup.add(ssid, ent, 1)
        sup.wpa("select_network", nid)
        outcome, seen = sup.associate(nid, ssid, security_of(ent) not in ("open", "owe"), timeout)
        if outcome == "bad_key" or (outcome != "ok" and "4WAY_HANDSHAKE" in seen):
            return _result(False, "auth_failed", "authentication failed (wrong password?)")
        if outcome != "ok":
            # Never got as far as a handshake on the second interface (e.g. the firmware
            # won't connect twice to the router the Pi is already on): no verdict.
            return _result(None, "inconclusive", "couldn't try it alongside the current network")
        freq = int(sup.status().get("freq") or 0)

        script = RUN / "dhcp.sh"
        script.write_text(DHCP_SCRIPT)
        os.chmod(script, 0o700)
        for _ in range(2):              # DHCP answers do go missing (phone hotspots)
            _run("udhcpc", "-i", TRIAL_IFACE, "-f", "-n", "-q", "-t", "5", "-T", "2", "-s", str(script),
                 "-x", "hostname:audiopi")
            lease = _lease()
            if lease.get("ip"):
                break
        if not lease.get("ip"):
            # Not proof the network is broken: the real switch (with its fallback) decides.
            return _result(None, "no_ip", "the password works, but no address came back in time", freq=freq)
        ip, router = lease["ip"], (lease.get("router") or "").split()[:1]
        dns = (lease.get("dns") or "").split()[:1] or router
        own = Supplicant().ip()
        if not router or ip == own:
            # Can't route a check through it without disturbing the Pi's own connection.
            return _result(True, "ok", "joined and got an address; internet not checked", freq=freq,
                           internet="unknown")
        _run("ip", "addr", "add", ip + "/32", "dev", TRIAL_IFACE)
        _run("ip", "route", "add", router[0], "dev", TRIAL_IFACE, "table", TABLE)
        _run("ip", "route", "add", "default", "via", router[0], "dev", TRIAL_IFACE, "table", TABLE)
        _run("ip", "rule", "add", "from", ip, "lookup", TABLE, "priority", RULE_PRIO)
        net = _check(ip, dns[0])
        return _result(True, "ok", "joined, got %s, internet %s" % (ip, net["internet"]), freq=freq,
                       internet=net["internet"], portal_url=net.get("portal_url"))
    except Exception as e:
        log.warning("wifi trial: %s", e)
        return _result(False, "error", str(e))
    finally:
        _teardown(lease)


def _check(source, dns):
    """Internet check through the trial address. Right after joining, the network's
    DNS (a phone's hotspot relaying to mobile data, say) can be slow to answer the
    first query: retry, then fall back to a public resolver, then check once more."""
    def resolve(host):
        for server, timeout in ((dns, 3), (dns, 3), ("1.1.1.1", 3)):
            try:
                return resolve_a(host, server, source, timeout=timeout)
            except OSError as e:
                err = e
        raise err
    net = connectivity(source=source, resolve=resolve)
    if net["internet"] == "offline":
        time.sleep(2)
        net = connectivity(source=source, resolve=resolve)
    return net


def _lease():
    try:
        return dict(kv.split("=", 1) for kv in (RUN / "lease").read_text().split() if "=" in kv)
    except OSError:
        return {}


def _result(ok, code, reason, freq=None, internet=None, portal_url=None):
    r = {"ok": ok, "code": code, "reason": reason, "freq": freq, "internet": internet}
    if portal_url:
        r["portal_url"] = portal_url
    log.info("wifi trial: %s", reason)
    return r


def _teardown(lease=None):
    ip = (lease or _lease()).get("ip")
    if ip:
        _run("ip", "rule", "del", "from", ip, "lookup", TABLE, "priority", RULE_PRIO)
    _run("ip", "route", "flush", "table", TABLE)
    # Leave properly: an access point that still thinks we're connected can ignore the
    # next trial from the same address (no DHCP answer) for a while.
    if (RUN / "wpa.pid").exists():
        _run("wpa_cli", "-p", str(RUN), "-i", TRIAL_IFACE, "disconnect")
        time.sleep(0.5)
    try:
        pid = int((RUN / "wpa.pid").read_text())
        os.kill(pid, 15)
        for _ in range(30):
            os.kill(pid, 0)
            time.sleep(0.1)
    except (OSError, ValueError):
        pass
    _run("iw", "dev", TRIAL_IFACE, "del")
    shutil.rmtree(RUN, ignore_errors=True)
