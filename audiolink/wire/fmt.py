"""Audio formats the link can run in.

Samples are always 16-bit: hosts convert through float32, which carries 16-bit
values exactly (24-bit would also survive Core Audio, but PortAudio's float->int
conversions aren't exact, and packing 3-byte samples in pure Python is slow).
Throughput scales with rate x channels instead: 192 kHz x 8 channels is 16x the
baseline.

BASELINE is where every link starts and what both sides fall back to: the Pi
always boots into it, and a format switch that isn't confirmed reverts to it.
"""


class AudioFormat:
    __slots__ = ("rate", "channels")

    def __init__(self, rate, channels):
        self.rate, self.channels = int(rate), int(channels)

    @property
    def frame_bytes(self):              # one sample on every channel
        return 2 * self.channels

    @property
    def bytes_per_sec(self):
        return self.rate * self.frame_bytes

    @property
    def chunk_frames(self):             # 10 ms of audio per read/write
        return self.rate // 100

    @property
    def chunk(self):
        return self.chunk_frames * self.frame_bytes

    def link_params(self):
        """(max frame payload, frames in flight): enough in flight to cover ~250 ms
        at this rate (round trips are ~100 ms), without bloating latency at low rates."""
        payload = 1024 if self.bytes_per_sec < 500_000 else 4096
        return payload, max(48, int(self.bytes_per_sec * 0.25 / payload))

    def to_json(self):
        return {"rate": self.rate, "channels": self.channels}

    @classmethod
    def from_json(cls, d):
        return cls(d["rate"], d["channels"])

    def __eq__(self, other):
        return isinstance(other, AudioFormat) and (self.rate, self.channels) == (other.rate, other.channels)

    def __hash__(self):
        return hash((self.rate, self.channels))

    def __repr__(self):
        return "%dHz x%d (%.0f KB/s)" % (self.rate, self.channels, self.bytes_per_sec / 1000)


BASELINE = AudioFormat(48000, 2)

# Tried best-first by the host (see host/audio_format.py).
CANDIDATES = [AudioFormat(r, c) for r, c in
              ((192000, 8), (96000, 8), (192000, 4), (96000, 4), (192000, 2), (96000, 2), (48000, 8))]
