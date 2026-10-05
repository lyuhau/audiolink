"""Framing and the reliable link layer.

Physical: 48 kHz / 16-bit / stereo PCM = a 192,000 B/s byte pipe each way, zeros when idle.

Frame:  SYNC(4) | session(4) | peer(4) | type(1) | stream(2) | seq(4) | ack(4) | len(2) | payload | CRC32(4)
  - SYNC's last byte is the protocol version, so peers speaking another version
    never parse each other's frames.
  - session is the sender's random id for this run; peer echoes the receiver's
    session as the sender knows it. A side only trusts a frame's ack and seq when
    the echo matches its own session, so frames sent to a previous incarnation
    (still in flight across a restart) can't corrupt the new link's state.
  - The receiver scans for SYNC and drops anything failing the CRC, so it
    resyncs on its own after dropped or corrupted samples.

Link: go-back-N ARQ. One sequence space for all reliable messages; cumulative
acks ride on every frame; on timeout, everything from the oldest unacked frame
is resent. Datagrams (T_DGRAM) bypass the ARQ: unsequenced, may be lost, for
traffic that does its own recovery (IP packets in phase 3).
"""
import collections
import logging
import random
import struct
import threading
import time
import zlib

from .. import PROTOCOL_VERSION

RATE, CHANNELS = 48000, 2
FRAME_BYTES = CHANNELS * 2           # one PCM frame
CHUNK_FRAMES = 480                   # 10 ms of audio
CHUNK = CHUNK_FRAMES * FRAME_BYTES

SYNC = b"\xa5\x5a\x96" + bytes([PROTOCOL_VERSION])
HDR = struct.Struct(">4sIIBHIIH")    # sync, session, peer, type, stream, seq, ack, length
CRC = struct.Struct(">I")
MAX_PAYLOAD = 1024
WINDOW = 48                          # reliable frames in flight (~50 KB)
RTO = 0.4                            # retransmit timeout, seconds
KEEPALIVE = 0.2                      # send an ack at least this often
DGRAM_QUEUE = 64                     # datagrams beyond this are dropped

T_ACK, T_DATA, T_OPEN, T_CLOSE, T_HELLO, T_DGRAM, T_CTRL = 0, 1, 2, 3, 4, 5, 6
RELIABLE = {T_DATA, T_OPEN, T_CLOSE, T_HELLO, T_CTRL}

log = logging.getLogger("audiolink")


def build_frame(session, peer, typ, sid, seq, ack, payload):
    body = HDR.pack(SYNC, session, peer, typ, sid, seq, ack, len(payload)) + payload
    return body + CRC.pack(zlib.crc32(body))


class Parser:
    """Finds valid frames in an arbitrary byte stream."""

    def __init__(self, on_frame, stats):
        self.buf = bytearray()
        self.on_frame = on_frame
        self.stats = stats

    def feed(self, data):
        buf = self.buf
        buf += data
        pos = 0
        while True:
            i = buf.find(SYNC, pos)
            if i < 0:
                pos = max(pos, len(buf) - (len(SYNC) - 1))  # SYNC may straddle chunks
                break
            if len(buf) - i < HDR.size:
                pos = i
                break
            _, sess, echo, typ, sid, seq, ack, length = HDR.unpack_from(buf, i)
            if length > MAX_PAYLOAD:
                pos = i + 1
                continue
            end = i + HDR.size + length + CRC.size
            if len(buf) < end:
                pos = i
                break
            body = bytes(buf[i:end - CRC.size])
            if zlib.crc32(body) != CRC.unpack_from(buf, end - CRC.size)[0]:
                self.stats["crc_fail"] += 1
                pos = i + 1
                continue
            self.stats["rx_frames"] += 1
            self.on_frame(sess, echo, typ, sid, seq, ack, body[HDR.size:])
            pos = end
        del buf[:pos]


class Link:
    """Reliable, ordered messages plus best-effort datagrams over the byte pipe.

    on_message(type, stream, payload) gets reliable messages in order;
    on_datagram(stream, payload) gets datagrams; on_up() runs when a peer is first
    seen or has restarted (all reliable state was reset).
    """

    def __init__(self, on_message, on_datagram, on_up):
        self.session = random.getrandbits(32) or 1
        self.peer = None
        self.cv = threading.Condition(threading.RLock())
        self.pending = collections.deque()      # (type, sid, payload) waiting for a seq
        self.unacked = collections.deque()      # [seq, sent_at, type, sid, payload]
        self.dgrams = collections.deque()       # (sid, payload)
        self.next_seq = 0
        self.resend_idx = None                  # go-back-N resend cursor into unacked
        self.expected = 0                       # next seq we accept from the peer
        self.ack_due = True
        self.last_tx = 0.0
        self.last_rx = 0.0
        self.out = bytearray()
        self.on_message = on_message
        self.on_datagram = on_datagram
        self.on_up = on_up
        self.stats = collections.Counter()
        self.parser = Parser(self._on_frame, self.stats)

    # ---- sending
    def send(self, typ, sid, payload=b""):
        """Queue a reliable message; blocks while the queue is full (backpressure)."""
        with self.cv:
            while len(self.pending) >= WINDOW:
                self.cv.wait(0.5)
            self.pending.append((typ, sid, payload))

    def send_datagram(self, sid, payload):
        """Queue a best-effort datagram; returns False if it was dropped."""
        with self.cv:
            if len(self.dgrams) >= DGRAM_QUEUE:
                self.stats["dgram_drop"] += 1
                return False
            self.dgrams.append((sid, payload))
            return True

    def _next_frame(self, now):
        if self.unacked and self.resend_idx is None and now - self.unacked[0][1] > RTO:
            self.resend_idx = 0
            self.stats["timeouts"] += 1
        if self.resend_idx is not None:
            if self.resend_idx < len(self.unacked):
                ent = self.unacked[self.resend_idx]
                self.resend_idx += 1
                ent[1] = now
                self.stats["retx"] += 1
                return build_frame(self.session, self.peer or 0, ent[2], ent[3], ent[0], self.expected, ent[4])
            self.resend_idx = None
        # Alternate between reliable and datagram traffic when both are waiting.
        want_dgram = self.dgrams and (self.stats["tx_frames"] + self.stats["tx_dgrams"]) % 2
        if self.pending and len(self.unacked) < WINDOW and not want_dgram:
            typ, sid, payload = self.pending.popleft()
            self.cv.notify_all()
            seq = self.next_seq
            self.next_seq += 1
            self.unacked.append([seq, now, typ, sid, payload])
            self.stats["tx_frames"] += 1
            return build_frame(self.session, self.peer or 0, typ, sid, seq, self.expected, payload)
        if self.dgrams:
            sid, payload = self.dgrams.popleft()
            self.stats["tx_dgrams"] += 1
            return build_frame(self.session, self.peer or 0, T_DGRAM, sid, 0, self.expected, payload)
        if self.ack_due or now - self.last_tx > KEEPALIVE:
            return build_frame(self.session, self.peer or 0, T_ACK, 0, 0, self.expected, b"")
        return None

    def pull(self, n):
        """Exactly n bytes for the audio output: queued frames, then zero padding."""
        with self.cv:
            now = time.monotonic()
            while len(self.out) < n:
                frame = self._next_frame(now)
                if frame is None:
                    break
                self.out += frame
                self.ack_due = False
                self.last_tx = now
            if len(self.out) >= n:
                chunk = bytes(self.out[:n])
                del self.out[:n]
            else:
                chunk = bytes(self.out) + bytes(n - len(self.out))
                self.out.clear()
            return chunk

    # ---- receiving
    def feed(self, data):
        self.parser.feed(data)

    def _on_frame(self, sess, echo, typ, sid, seq, ack, payload):
        deliver = dgram = None
        up = False
        with self.cv:
            self.last_rx = time.monotonic()
            if sess != self.peer:
                # New peer, or the peer restarted: start both directions from scratch.
                if self.peer is not None:
                    log.warning("peer restarted; resetting link")
                self._reset()
                self.peer = sess
                up = True
            if echo != self.session:
                # Addressed to an earlier run of ours (or sent before the peer knew us).
                self.stats["rx_stale"] += 1
                self.ack_due = True     # tell the peer who we are
            else:
                popped = 0
                while self.unacked and self.unacked[0][0] < ack:
                    self.unacked.popleft()
                    popped += 1
                if popped:
                    self.cv.notify_all()
                    if self.resend_idx is not None:
                        self.resend_idx = max(0, self.resend_idx - popped)
                if typ in RELIABLE:
                    if seq == self.expected:
                        self.expected += 1
                        deliver = (typ, sid, payload)
                    else:
                        self.stats["rx_out_of_order"] += 1
                    self.ack_due = True
                elif typ == T_DGRAM:
                    dgram = (sid, payload)
        if up:
            self.on_up()
        if deliver:
            self.on_message(*deliver)
        if dgram:
            self.stats["rx_dgrams"] += 1
            self.on_datagram(*dgram)

    def restart(self):
        """Forget the peer and start a new session (new id), so that when frames
        flow again both sides see a restart and resync from scratch."""
        with self.cv:
            self.session = random.getrandbits(32) or 1
            self._reset()
            self.peer = None

    def _reset(self):
        self.pending.clear()
        self.unacked.clear()
        self.dgrams.clear()
        self.out.clear()
        self.next_seq = self.expected = 0
        self.resend_idx = None
        self.cv.notify_all()
