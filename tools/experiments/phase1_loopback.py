#!/usr/bin/env python3
"""Phase 1 (an early experiment, kept for reference; needs numpy, sounddevice): is
the USB audio channel bit-exact?

Sends a known pseudo-random payload through the AudioPi sound card and compares
what comes out the other side, byte for byte.

  mac2pi : Mac plays into "Playback Inactive", Pi records with arecord
  pi2mac : Pi plays with aplay, Mac records from "Capture Inactive"

Audio is exchanged with Core Audio as float32 and converted to/from int16 here,
because PortAudio's own int16 conversion dithers (flips LSBs).
"""
import argparse
import hashlib
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parents[2]     # the repo: keys/, and where the .raw files go
PI = "root@10.55.0.2"
SSH = ["ssh", "-i", str(ROOT / "keys/id_ed25519"), "-o", "StrictHostKeyChecking=no",
       "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
RATE, CHANNELS = 48000, 2
BYTES_PER_SEC = RATE * CHANNELS * 2
MARKER_LEN = 64
LEAD_S, TAIL_S = 3.0, 1.0
ALSA_FMT = ["-D", "hw:0,0", "-f", "S16_LE", "-r", str(RATE), "-c", str(CHANNELS), "-t", "raw"]


def make_signal(seconds, seed):
    rng = np.random.default_rng(seed)
    marker = rng.bytes(MARKER_LEN)
    payload = rng.bytes(int(seconds * BYTES_PER_SEC) // 4 * 4)
    lead = bytes(int(LEAD_S * BYTES_PER_SEC))
    tail = bytes(int(TAIL_S * BYTES_PER_SEC))
    return marker, payload, lead + marker + payload + tail


def bytes_to_float(b):
    return (np.frombuffer(b, dtype="<i2").astype(np.float32) / 32768.0).reshape(-1, CHANNELS)


def float_to_bytes(x):
    return np.round(x.reshape(-1) * 32768.0).clip(-32768, 32767).astype("<i2").tobytes()


def device(name):
    for i, d in enumerate(sd.query_devices()):
        if d["name"] == name:
            return i
    sys.exit(f"audio device not found: {name}")


def analyze(rx, marker, payload):
    start = rx.find(marker)
    if start < 0:
        print("FAIL: sync marker not found in received stream")
        nz = np.count_nonzero(np.frombuffer(rx, dtype=np.uint8))
        print(f"  received {len(rx)} bytes, {nz} non-zero")
        return False
    got = rx[start + MARKER_LEN:start + MARKER_LEN + len(payload)]
    print(f"marker found at byte {start} (frame {start / 4:.2f})")
    if got == payload:
        print(f"PASS: {len(payload):,} bytes bit-exact  sha256={hashlib.sha256(got).hexdigest()[:16]}")
        return True

    a = np.frombuffer(payload[:len(got)], dtype=np.uint8)
    b = np.frombuffer(got, dtype=np.uint8)
    diff = np.nonzero(a != b)[0]
    print(f"FAIL: {len(got):,}/{len(payload):,} bytes received, {len(diff):,} differ")
    if len(diff):
        first = diff[0]
        print(f"  first mismatch at payload byte {first} ({first / BYTES_PER_SEC:.3f}s in)")
        # Did the stream slip (dropped/duplicated frames) or get altered in place?
        probe = payload[first + 64:first + 128]
        j = got.find(probe, max(0, first - 4096))
        if j >= 0:
            print(f"  stream resumes {j - (first + 64):+d} bytes later -> frames dropped/duplicated")
        else:
            xor = np.bitwise_xor(a[diff[:1000]], b[diff[:1000]])
            print(f"  no resync found; sample values altered (xor of first diffs: {np.unique(xor)[:8]})")
    return False


def mac2pi(seconds, seed):
    marker, payload, sig = make_signal(seconds, seed)
    rec_s = int(len(sig) / BYTES_PER_SEC)
    print(f"mac2pi: {len(payload):,} bytes payload, recording {rec_s}s on Pi")
    # The gadget's capture side only has data while the Mac is streaming, so start
    # playback first (it opens with LEAD_S of silence) and record once it's running.
    t0 = time.time()
    sd.play(bytes_to_float(sig), RATE, device=device("Playback Inactive"))
    time.sleep(1.0)
    rec = subprocess.run(SSH + [PI, f"arecord -q {' '.join(ALSA_FMT)} -d {rec_s} /tmp/rx.raw"])
    sd.wait()
    print(f"played {len(sig) / BYTES_PER_SEC:.2f}s of audio in {time.time() - t0:.2f}s, arecord exit {rec.returncode}")
    rx = subprocess.run(SSH + [PI, "cat /tmp/rx.raw"], check=True, capture_output=True).stdout
    (ROOT / "rx_mac2pi.raw").write_bytes(rx)
    return analyze(rx, marker, payload)


def pi2mac(seconds, seed):
    marker, payload, sig = make_signal(seconds, seed)
    (ROOT / "tx_pi2mac.raw").write_bytes(sig)
    subprocess.run(SSH + [PI, "cat > /tmp/tx.raw"], input=sig, check=True)
    rec_s = len(sig) / BYTES_PER_SEC + 3
    print(f"pi2mac: {len(payload):,} bytes payload, recording {rec_s:.1f}s on Mac")
    frames = sd.rec(int(rec_s * RATE), RATE, CHANNELS, dtype="float32",
                    device=device("Capture Inactive"))
    time.sleep(1.0)
    subprocess.run(SSH + [PI, f"aplay -q {' '.join(ALSA_FMT)} /tmp/tx.raw"], check=True)
    sd.wait()
    rx = float_to_bytes(frames)
    (ROOT / "rx_pi2mac.raw").write_bytes(rx)
    return analyze(rx, marker, payload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("direction", choices=["mac2pi", "pi2mac", "both"])
    ap.add_argument("--seconds", type=float, default=5)
    ap.add_argument("--seed", type=int, default=1234)
    a = ap.parse_args()
    ok = True
    if a.direction in ("mac2pi", "both"):
        ok &= mac2pi(a.seconds, a.seed)
    if a.direction in ("pi2mac", "both"):
        ok &= pi2mac(a.seconds, a.seed + 1)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
