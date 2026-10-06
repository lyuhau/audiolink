"""Minimal BitTorrent Mainline DHT client: just enough to store and fetch signed records.

BEP 5   KRPC: bencoded queries over UDP (ping, find_node), XOR-distance lookups
BEP 43  we flag ourselves read-only ("ro": 1): we query the DHT but don't serve it,
        so other nodes don't put us in their routing tables
BEP 44  mutable items: a value of up to 1000 bytes stored under SHA1(public key + salt),
        signed with ed25519; storing nodes keep the highest sequence number

Lookups are iterative: ask the closest nodes we know for nodes closer to the target,
keeping ALPHA queries in flight and sending the next as soon as any reply arrives,
until no unqueried node is closer than the K-th closest node that answered.
"""
import hashlib
import logging
import os
import queue
import socket
import statistics
import threading
import time

from ..crypto import ed25519_sign, ed25519_verify

log = logging.getLogger("audiolink")

BOOTSTRAP = [("router.bittorrent.com", 6881), ("router.utorrent.com", 6881),
             ("dht.transmissionbt.com", 6881), ("dht.libtorrent.org", 25401)]
K = 8               # nodes that store a key
ALPHA = 8           # parallel queries per round
QUERY_TIMEOUT = 1.5 # seconds; slow or gone nodes are common
BOOTSTRAP_TIMEOUT = 4.0  # bootstrap routers can be slower; they're all we have at first
MAX_V = 1000        # BEP 44 value size limit


# ---------------------------------------------------------------- bencode
def bencode(x):
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, str):
        x = x.encode()
    if isinstance(x, (bytes, bytearray)):
        return b"%d:%s" % (len(x), x)
    if isinstance(x, (list, tuple)):
        return b"l" + b"".join(bencode(i) for i in x) + b"e"
    if isinstance(x, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, v) for k, v in x.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError("can't bencode %r" % type(x))


def bdecode(data):
    def dec(i):
        c = data[i:i + 1]
        if c == b"i":
            j = data.index(b"e", i)
            return int(data[i + 1:j]), j + 1
        if c == b"l":
            out, i = [], i + 1
            while data[i:i + 1] != b"e":
                v, i = dec(i)
                out.append(v)
            return out, i + 1
        if c == b"d":
            out, i = {}, i + 1
            while data[i:i + 1] != b"e":
                k, i = dec(i)
                out[k], i = dec(i)
            return out, i + 1
        j = data.index(b":", i)
        n = int(data[i:j])
        return data[j + 1:j + 1 + n], j + 1 + n
    value, end = dec(0)
    return value


def _nodes(blob):
    """Compact node info: 20-byte id + 4-byte IPv4 + 2-byte port, repeated."""
    out = []
    for i in range(0, len(blob) - 25, 26):
        nid, ip, port = blob[i:i + 20], blob[i + 20:i + 24], blob[i + 24:i + 26]
        port = int.from_bytes(port, "big")
        if port:
            out.append((nid, (socket.inet_ntoa(ip), port)))
    return out


def _dist(a, b):
    return int.from_bytes(a, "big") ^ int.from_bytes(b, "big")


def target_for(public, salt=b""):
    return hashlib.sha1(public + salt).digest()


def sig_buffer(salt, seq, v):
    """The bytes BEP 44 signs: [4:salt<salt>]3:seqi<seq>e1:v<bencoded v>."""
    buf = b""
    if salt:
        buf += b"4:salt" + bencode(salt)
    return buf + b"3:seqi%de1:v" % seq + bencode(v)


# ---------------------------------------------------------------- client
class DHT:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.id = os.urandom(20)
        self.pending = {}
        self.lock = threading.Lock()
        self.known = {}                 # id -> addr of nodes that answered (warm start)
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def close(self):
        self.sock.close()

    def _recv_loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(65536)
                msg = bdecode(data)
            except OSError:
                return
            except Exception:
                continue
            if not isinstance(msg, dict) or msg.get(b"y") not in (b"r", b"e"):
                continue                # queries to us: we're read-only, ignore
            with self.lock:
                ent = self.pending.get(msg.get(b"t"))
            if isinstance(ent, queue.Queue):
                ent.put((msg.get(b"t"), addr, msg))
            elif ent:
                ent[1] = msg
                ent[0].set()

    def _query_many(self, reqs, timeout=3.0):
        """reqs: [(addr, method, args)] -> [(addr, response dict or None)], in parallel."""
        ents = []
        for addr, q, a in reqs:
            t = os.urandom(4)
            ent = [threading.Event(), None]
            with self.lock:
                self.pending[t] = ent
            msg = {"t": t, "y": "q", "q": q, "a": dict(a, id=self.id), "ro": 1}
            try:
                self.sock.sendto(bencode(msg), addr)
            except OSError:
                ent[0].set()
            ents.append((t, addr, ent))
        deadline = time.monotonic() + timeout
        out = []
        for t, addr, ent in ents:
            ent[0].wait(max(0, deadline - time.monotonic()))
            with self.lock:
                self.pending.pop(t, None)
            r = ent[1]
            out.append((addr, r.get(b"r") if r and r.get(b"y") == b"r" else None))
        return out

    def _bootstrap(self):
        addrs = []
        for host, port in BOOTSTRAP:
            try:
                addrs.append((socket.gethostbyname(host), port))
            except OSError:
                pass
        return addrs

    def lookup(self, target, method="find_node", deadline=15.0):
        """Iterative lookup. Returns [(node id, addr, response)] for the nodes that
        answered, closest to target first."""
        end = time.monotonic() + deadline
        arg = {"target": target}
        replies = queue.Queue()
        inflight = {}                   # transaction id -> (node id or None, give-up time)
        inflight_q = {}                 # transaction id -> query method
        seen, queried, answered = {}, set(), {}

        def send(nid, addr, q=method, timeout=QUERY_TIMEOUT):
            t = os.urandom(4)
            with self.lock:
                self.pending[t] = replies
            msg = {"t": t, "y": "q", "q": q, "a": dict(arg, id=self.id), "ro": 1}
            try:
                self.sock.sendto(bencode(msg), addr)
                inflight[t] = (nid, time.monotonic() + timeout)
                inflight_q[t] = q
            except OSError:
                with self.lock:
                    self.pending.pop(t, None)

        def seed():
            # Bootstrap routers only answer find_node (not BEP 44 get), so seed with that.
            for addr in self._bootstrap():
                send(None, addr, "find_node", BOOTSTRAP_TIMEOUT)
            for addr in list(self.known.values())[:16]:
                send(None, addr)

        seed()
        reseeds = 2
        try:
            while time.monotonic() < end:
                now = time.monotonic()
                for t, (nid, give_up) in list(inflight.items()):
                    if now > give_up:                       # give up on slow nodes
                        del inflight[t]
                        with self.lock:
                            self.pending.pop(t, None)
                # Fill the pipe with the closest unqueried nodes that could still matter:
                # nothing farther than the K-th closest node that has answered.
                best = sorted(answered, key=lambda n: _dist(n, target))[:K]
                bound = _dist(best[-1], target) if len(best) == K else None
                todo = sorted((n for n in seen if n not in queried and
                               (bound is None or _dist(n, target) < bound)),
                              key=lambda n: _dist(n, target))
                for n in todo[:ALPHA - len(inflight)]:
                    queried.add(n)
                    send(n, seen[n])
                if not inflight:
                    if not answered and reseeds:            # nobody useful answered: ask again
                        reseeds -= 1
                        seed()
                        continue
                    break                                   # converged (or nothing to ask)
                try:
                    t, addr, msg = replies.get(timeout=0.1)
                except queue.Empty:
                    continue
                inflight.pop(t, None)
                with self.lock:
                    self.pending.pop(t, None)
                r = msg.get(b"r") if msg.get(b"y") == b"r" else None
                if r and b"id" in r:
                    if inflight_q.pop(t, method) == method:     # bootstrap replies aren't answers
                        answered[r[b"id"]] = (addr, r)
                    queried.add(r[b"id"])
                    for nid, naddr in _nodes(r.get(b"nodes", b"")):
                        seen.setdefault(nid, naddr)
        finally:
            with self.lock:
                for t in inflight:
                    self.pending.pop(t, None)
        for nid, (addr, _) in list(answered.items())[:200]:
            self.known[nid] = addr
        return sorted(((n, a, r) for n, (a, r) in answered.items()), key=lambda x: _dist(x[0], target))

    # ---- BEP 44 mutable items
    def get(self, public, salt=b"", deadline=15.0):
        """The newest validly signed value under (public, salt): (seq, v) or None."""
        target = target_for(public, salt)
        best = None
        for _, _, r in self.lookup(target, "get", deadline):
            v, seq, sig = r.get(b"v"), r.get(b"seq"), r.get(b"sig")
            if v is None or seq is None or sig is None or r.get(b"k", public) != public:
                continue
            if (best is None or seq > best[0]) and ed25519_verify(public, sig_buffer(salt, seq, v), sig):
                best = (seq, v)
        return best

    def put(self, seed, public, v, seq, salt=b"", deadline=15.0):
        """Store a signed value on the K closest nodes. Returns how many accepted it."""
        if len(bencode(v)) > MAX_V:
            raise ValueError("value too large for the DHT (%d bytes)" % len(bencode(v)))
        target = target_for(public, salt)
        sig = ed25519_sign(seed, sig_buffer(salt, seq, v))
        nodes = [(a, r[b"token"]) for _, a, r in self.lookup(target, "get", deadline) if b"token" in r][:K]
        args = {"k": public, "seq": seq, "sig": sig, "v": v}
        if salt:
            args["salt"] = salt
        results = self._query_many([(a, "put", dict(args, token=tok)) for a, tok in nodes])
        return sum(1 for _, r in results if r is not None)

    # ---- bonus: how big is the DHT?
    def estimate_size(self, samples=5):
        """Node IDs are uniform over 2^160, so the i-th closest node to a random target
        sits at distance ~ i * 2^160 / N. Estimate N from the closest K of a few lookups."""
        estimates = []
        for _ in range(samples):
            target = os.urandom(20)
            found = self.lookup(target, deadline=10.0)[:K]
            for i, (nid, _, _) in enumerate(found, 1):
                d = _dist(nid, target)
                if d:
                    estimates.append(i * 2 ** 160 / d)
        return (statistics.median(estimates), len(estimates)) if estimates else (None, 0)
