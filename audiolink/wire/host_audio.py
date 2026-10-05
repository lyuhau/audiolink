"""Host-side audio: find the AudioPi gadget's devices and stream them with PortAudio."""
import logging
import threading
import time

from .protocol import CHANNELS, CHUNK, CHUNK_FRAMES, RATE

log = logging.getLogger("audiolink")

# Substrings that identify the AudioPi gadget's devices on different hosts.
# macOS names them after the gadget's alt-setting strings; Windows includes the product name.
DEVICE_HINTS = ("audiopi", "playback inactive", "capture inactive", "uac2")


def find_devices(out_name=None, in_name=None):
    """Pick the gadget's output and input devices, preferring WASAPI on Windows."""
    from . import portaudio
    devs = portaudio.devices()

    def pick(name, kind):
        key = "outputs" if kind == "output" else "inputs"
        cands = [d for d in devs if d[key] >= CHANNELS]
        if name:
            cands = [d for d in cands if name.lower() in d["name"].lower()]
        else:
            cands = [d for d in cands if any(h in d["name"].lower() for h in DEVICE_HINTS)]
        if not cands:
            raise LookupError("no AudioPi %s device found%s"
                              % (kind, " matching %r" % name if name else ""))
        cands.sort(key=lambda d: d["hostapi_type"] != portaudio.paWASAPI)
        return cands[0]

    return pick(out_name, "output"), pick(in_name, "input")


def run_portaudio(link, out_name=None, in_name=None):
    """Host: blocking PortAudio streams on the gadget's devices.

    A supervisor reopens the devices whenever they error out or stall, e.g. when
    the Pi reboots or is unplugged, and waits for them if they aren't present yet.
    """
    from . import portaudio

    def open_streams():
        portaudio.reinit()
        out_dev, in_dev = find_devices(out_name, in_name)
        # Input first: the gadget's capture side only carries data while the host streams.
        inp = portaudio.Stream(in_dev, False, RATE, CHANNELS, CHUNK_FRAMES)
        try:
            out = portaudio.Stream(out_dev, True, RATE, CHANNELS, CHUNK_FRAMES)
        except Exception:
            inp.close()
            raise
        log.info("audio out: %s [%s]", out_dev["name"], out_dev["hostapi"])
        log.info("audio in:  %s [%s]", in_dev["name"], in_dev["hostapi"])
        return out, inp

    def run_session(out, inp):
        state = {"ok": True, "last_read": time.monotonic(), "err": "stalled"}

        def player():
            try:
                while state["ok"]:
                    if not out.write(link.pull(CHUNK)):
                        link.stats["out_underflows"] += 1
            except Exception as e:
                state["err"] = e
            state["ok"] = False

        def recorder():
            try:
                while state["ok"]:
                    data, overflowed = inp.read(CHUNK_FRAMES)
                    state["last_read"] = time.monotonic()
                    if overflowed:
                        link.stats["in_overflows"] += 1
                    link.feed(data)
            except Exception as e:
                state["err"] = e
            state["ok"] = False

        threads = [threading.Thread(target=f, daemon=True) for f in (player, recorder)]
        for t in threads:
            t.start()
        while state["ok"] and time.monotonic() - state["last_read"] < 2.0:
            time.sleep(0.25)
        state["ok"] = False
        log.warning("audio device lost (%s); reopening", state["err"])
        for st in (out, inp):
            try:
                st.close()
            except Exception:
                pass
        for t in threads:
            t.join(2)

    def supervisor():
        waiting = False
        while True:
            try:
                out, inp = open_streams()
            except (LookupError, portaudio.PortAudioError) as e:
                if not waiting:
                    log.warning("%s; waiting for the AudioPi to appear", e)
                    waiting = True
                time.sleep(2)
                continue
            waiting = False
            run_session(out, inp)
            link.stats["audio_restarts"] += 1
            time.sleep(1)

    threading.Thread(target=supervisor, daemon=True).start()
