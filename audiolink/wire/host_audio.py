"""Host-side audio: find the AudioPi gadget's devices and stream them with PortAudio."""
import logging
import sys
import threading
import time

from .fmt import AudioFormat

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
        cands = [d for d in devs if d[key] >= 2]
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


def device_format(out_dev, in_dev):
    """The format the gadget presents. It offers exactly one rate and channel count,
    the same both ways, so the devices themselves say what the Pi is running."""
    fmt = AudioFormat(round(out_dev["rate"]), out_dev["outputs"])
    if (round(in_dev["rate"]), in_dev["inputs"]) != (fmt.rate, fmt.channels):
        raise LookupError("AudioPi input (%d Hz x%d) and output (%r) disagree"
                          % (in_dev["rate"], in_dev["inputs"], fmt))
    return fmt


def run_portaudio(link, out_name=None, in_name=None):
    """Host: blocking PortAudio streams on the gadget's devices.

    A supervisor reopens the devices whenever they error out or stall, e.g. when
    the Pi reboots, is unplugged or re-presents itself in another format, and waits
    for them if they aren't present yet. Streams open in the format the device reports.
    """
    from . import portaudio

    def open_streams():
        portaudio.reinit()
        out_dev, in_dev = find_devices(out_name, in_name)
        fmt = device_format(out_dev, in_dev)
        # Input first: the gadget's capture side only carries data while the host streams.
        inp = portaudio.Stream(in_dev, False, fmt.rate, fmt.channels, fmt.chunk_frames)
        try:
            out = portaudio.Stream(out_dev, True, fmt.rate, fmt.channels, fmt.chunk_frames)
        except Exception:
            inp.close()
            raise
        link.set_format(fmt)
        log.info("audio out: %s [%s]", out_dev["name"], out_dev["hostapi"])
        log.info("audio in:  %s [%s], %r", in_dev["name"], in_dev["hostapi"], fmt)
        return out, inp, fmt

    def run_session(out, inp, fmt):
        state = {"ok": True, "last_read": time.monotonic(), "err": "stalled"}

        def lost(reason):
            state["err"] = reason
            state["ok"] = False

        watch = None
        if sys.platform == "darwin":
            # Know about a removal at once: Core Audio would move our output to the speakers.
            from .coreaudio import Watch
            try:
                watch = Watch([out.coreaudio_device(), inp.coreaudio_device()], lost)
            except Exception as e:
                log.debug("coreaudio watch unavailable: %s", e)

        # Neither thread blocks inside PortAudio: they wait for room/data here, so they
        # notice state["ok"] going false and exit before the streams are closed (closing
        # or re-initializing under a blocked read corrupts memory).
        def player():
            try:
                while state["ok"]:
                    if out.available() < fmt.chunk_frames:
                        time.sleep(0.002)
                        continue
                    if not out.write(link.pull(fmt.chunk)):
                        link.stats["out_underflows"] += 1
            except Exception as e:
                state["err"] = e
            state["ok"] = False
            try:
                out.abort()             # drop what's queued rather than let it play anywhere
            except Exception:
                pass

        def recorder():
            try:
                while state["ok"]:
                    if inp.available() < fmt.chunk_frames:
                        time.sleep(0.002)
                        continue
                    data, overflowed = inp.read(fmt.chunk_frames)
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
            if watch and not watch.alive():     # backstop for a missed notification
                lost("device removed")
        state["ok"] = False
        if watch:
            watch.close()
        log.warning("audio device lost (%s); reopening", state["err"])
        for t in threads:
            t.join(10)
            if t.is_alive():
                log.warning("audio thread stuck in PortAudio; reopening anyway")
        for st in (out, inp):
            try:
                st.close()
            except Exception:
                pass

    def supervisor():
        waiting = False
        while True:
            try:
                out, inp, fmt = open_streams()
            except (LookupError, portaudio.PortAudioError) as e:
                if not waiting:
                    log.warning("%s; waiting for the AudioPi to appear", e)
                    waiting = True
                time.sleep(2)
                continue
            waiting = False
            run_session(out, inp, fmt)
            link.stats["audio_restarts"] += 1
            time.sleep(1)

    threading.Thread(target=supervisor, daemon=True).start()
