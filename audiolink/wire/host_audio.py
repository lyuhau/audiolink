"""Host-side audio: find the AudioPi gadget's devices and stream them with PortAudio."""
import logging
import sys
import threading
import time

from .fmt import AudioFormat

log = logging.getLogger("audiolink")

OUT_FRAMES = 256               # macOS: PortAudio's output buffer, written a buffer at a time
LISTEN = 5.0                   # s to hear the Pi's link on a device's input before playing to it
NOT_THE_PI = 30.0              # s before listening again to a device that wasn't the Pi


def host_buffer_frames(fmt, output):
    """PortAudio's frames per buffer. On macOS its output latency follows it: a whole
    chunk (10 ms) gave ~53 ms (96 kHz x8), 256 frames ~14 ms, about 40 ms less on every
    round trip; written a buffer at a time, so the smaller ring doesn't run dry. Under a
    bulk load it still runs dry now and then (a retransmit; ~500 per 20 MB echoed); 512
    never did, at +20 ms. 128 gained little and dropped more; 384 (not a power of two)
    stalled streams. Input stays a chunk: smaller lost data, and
    its latency didn't change. Elsewhere (WASAPI's period, ALSA): a chunk, untested."""
    if sys.platform == "darwin" and output:
        return min(fmt.chunk_frames, OUT_FRAMES)
    return fmt.chunk_frames


def find_pairs(out_name=None, in_name=None):
    """[(output, input, key)] that may be the Pi: an output and an input of the same
    USB device (key: which), or by the given names. Real headphones a Pi was cloned
    from look the same (USB ID and names): each pair is its own candidate, and only
    the one whose input carries the link is played to (hears_the_link)."""
    from . import portaudio, usb_audio
    devs = portaudio.devices()
    order = lambda ds: sorted(ds, key=lambda d: d["hostapi_type"] != portaudio.paWASAPI)   # noqa: E731
    if out_name or in_name:
        pis = [] if out_name and in_name else usb_audio.pi_devices(usb_audio.annotate(devs))

        def pick(name, kind):
            key = "outputs" if kind == "output" else "inputs"
            cands = [d for d in devs if d[key] >= 1 and (name.lower() in d["name"].lower() if name else d in pis)]
            if not cands:
                raise LookupError("No %s audio device matching %r (see: audiolink list-devices)" % (kind, name)
                                  if name else "no Pi %s audio device found" % kind)
            return order(cands)[0]
        out, inp = pick(out_name, "output"), pick(in_name, "input")
        return [(out, inp, "%s/%s" % (out["name"], inp["name"]))]
    pis = usb_audio.pi_devices(usb_audio.annotate(devs))
    groups = {}                             # one USB device (or one name, unknown) -> its devices
    for d in pis:
        groups.setdefault(d.get("usb_dev") or "name:" + d["name"], []).append(d)
    pairs = []
    for key, ds in groups.items():
        outs = order([d for d in ds if d["outputs"] >= 1])     # a clone's microphone can be mono
        ins = order([d for d in ds if d["inputs"] >= 1])
        if outs and ins:
            pairs.append((outs[0], ins[0], key))
    if not pairs:
        raise LookupError("no Pi %s audio device found" % ("output" if not any(d["outputs"] for d in pis)
                                                            else "input"))
    return pairs


def find_devices(out_name=None, in_name=None):
    """The first candidate (output, input): find_pairs."""
    return find_pairs(out_name, in_name)[0][:2]


def device_formats(out_dev, in_dev):
    """(output, input) formats the gadget presents: the devices themselves say what the
    Pi runs. Usually the same both ways (audiolink's device); a device cloned from
    another can differ (stereo out, mono in), and each direction runs in its own."""
    return (AudioFormat(round(out_dev["rate"]), out_dev["outputs"]),
            AudioFormat(round(in_dev["rate"]), in_dev["inputs"]))


def hears_the_link(inp, fmt, seconds=LISTEN, clock=time.monotonic, sleep=time.sleep):
    """Listen before talking: does this input carry the link (one valid frame)? The Pi
    sends one at least every KEEPALIVE whether or not it hears us; any other device
    (a microphone, real headphones that share the Pi's USB ID or name) never does, and
    is never played to. The input is read and dropped here; the link resyncs."""
    import collections
    from .protocol import Parser
    heard = []
    parser = Parser(lambda *frame: heard.append(True), collections.Counter())
    end = clock() + seconds
    while not heard and clock() < end:
        if inp.available() < fmt.chunk_frames:
            sleep(0.005)
            continue
        data, _ = inp.read(fmt.chunk_frames)
        parser.feed(data)
    return bool(heard)


def run_portaudio(link, out_name=None, in_name=None, expected=lambda: False):
    """Host: blocking PortAudio streams on the gadget's devices.

    A supervisor reopens the devices whenever they error out or stall, e.g. when
    the Pi reboots, is unplugged or re-presents itself in another format, and waits
    for them if they aren't present yet. Streams open in the format the device reports.
    expected(): the device being away now is part of the plan (an audio format
    change), so waiting for it is -v detail.
    """
    from . import portaudio
    not_the_pi = {}                    # USB device (find_pairs' key) -> when it last wasn't

    def open_streams():
        portaudio.reinit()
        why = None
        for out_dev, in_dev, key in find_pairs(out_name, in_name):
            if time.monotonic() - not_the_pi.get(key, -NOT_THE_PI) < NOT_THE_PI:
                why = why or "%s isn't sending the link (listening again shortly)" % in_dev["name"]
                continue
            ofmt, ifmt = device_formats(out_dev, in_dev)
            # Input first, and only then output, once the Pi is heard on it (hears_the_link)
            inp = portaudio.Stream(in_dev, False, ifmt.rate, ifmt.channels, host_buffer_frames(ifmt, False))
            try:
                if not hears_the_link(inp, ifmt):
                    not_the_pi[key] = time.monotonic()
                    log.debug("audio: %s (%s) isn't sending the link: not the Pi, nothing played to it",
                              in_dev["name"], key)
                    why = "%s isn't sending the link: not the Pi, so nothing is played to it" % in_dev["name"]
                    inp.close()
                    continue
                out = portaudio.Stream(out_dev, True, ofmt.rate, ofmt.channels, host_buffer_frames(ofmt, True))
            except Exception:
                inp.close()
                raise
            break
        else:
            raise LookupError(why or "no Pi audio device found")
        link.set_format(ofmt)               # what this side sends in
        log.debug("audio out: %s [%s], %r", out_dev["name"], out_dev["hostapi"], ofmt)
        log.debug("audio in:  %s [%s], %r", in_dev["name"], in_dev["hostapi"], ifmt)
        return out, inp, (ofmt, ifmt)

    def run_session(out, inp, fmts):
        ofmt, ifmt = fmts
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
        piece = host_buffer_frames(ofmt, True)     # frames per write: PortAudio's buffer
        piece_bytes = piece * ofmt.frame_bytes

        def player():
            try:
                while state["ok"]:
                    if out.available() < piece:
                        time.sleep(0.001)
                        continue
                    if not out.write(link.pull(piece_bytes)):
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
                    if inp.available() < ifmt.chunk_frames:
                        time.sleep(0.002)
                        continue
                    data, overflowed = inp.read(ifmt.chunk_frames)
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
        # -v detail: an unplugged Pi is reported in words once the peer goes quiet (host/console.py)
        log.debug("audio device lost (%s); reopening", state["err"])
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
                out, inp, fmts = open_streams()
            except (LookupError, portaudio.PortAudioError) as e:
                if not waiting:
                    log.debug("audio: %s", e)
                    if out_name or in_name:
                        log.warning("%s; waiting for it", e)
                    elif not expected():
                        log.info("Waiting for the Pi (plug it in)")
                    waiting = True
                time.sleep(2)
                continue
            waiting = False
            run_session(out, inp, fmts)
            link.stats["audio_restarts"] += 1
            time.sleep(1)

    threading.Thread(target=supervisor, daemon=True).start()
