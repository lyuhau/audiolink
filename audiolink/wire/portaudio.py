"""Minimal ctypes binding to PortAudio's blocking API (host side).

Samples must pass through untouched. Output is int16 (PortAudio's int16->float
conversion divides by 32768, which is exact). Input is read as float32 and converted
here: PortAudio's float->int16 converter multiplies by 32767, which alters samples.
On Windows, WASAPI streams are opened in exclusive mode: shared mode resamples and
applies effects, which would corrupt the data.

The library is loaded from the bundled audiolink/_lib/ (extracted to a cache
directory when running from a .pyz), then from the system as a fallback.
"""
import ctypes
import ctypes.util
import hashlib
import os
import pkgutil
from array import array
import sys
from pathlib import Path

paFloat32 = 0x00000001
paInt16 = 0x00000008
paClipOff = 0x00000001
paDitherOff = 0x00000002
paInputOverflowed = -9981
paOutputUnderflowed = -9980
paWASAPI = 13
paWinWasapiExclusive = 1


class PaDeviceInfo(ctypes.Structure):
    _fields_ = [
        ("structVersion", ctypes.c_int),
        ("name", ctypes.c_char_p),
        ("hostApi", ctypes.c_int),
        ("maxInputChannels", ctypes.c_int),
        ("maxOutputChannels", ctypes.c_int),
        ("defaultLowInputLatency", ctypes.c_double),
        ("defaultLowOutputLatency", ctypes.c_double),
        ("defaultHighInputLatency", ctypes.c_double),
        ("defaultHighOutputLatency", ctypes.c_double),
        ("defaultSampleRate", ctypes.c_double),
    ]


class PaHostApiInfo(ctypes.Structure):
    _fields_ = [
        ("structVersion", ctypes.c_int),
        ("type", ctypes.c_int),
        ("name", ctypes.c_char_p),
        ("deviceCount", ctypes.c_int),
        ("defaultInputDevice", ctypes.c_int),
        ("defaultOutputDevice", ctypes.c_int),
    ]


class PaStreamParameters(ctypes.Structure):
    _fields_ = [
        ("device", ctypes.c_int),
        ("channelCount", ctypes.c_int),
        ("sampleFormat", ctypes.c_ulong),
        ("suggestedLatency", ctypes.c_double),
        ("hostApiSpecificStreamInfo", ctypes.c_void_p),
    ]


class PaWasapiStreamInfo(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_ulong),
        ("hostApiType", ctypes.c_int),
        ("version", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("channelMask", ctypes.c_ulong),
        ("hostProcessorOutput", ctypes.c_void_p),
        ("hostProcessorInput", ctypes.c_void_p),
        ("threadPriority", ctypes.c_int),
        ("streamCategory", ctypes.c_int),
        ("streamOption", ctypes.c_int),
    ]


_SCALE = 32768.0


class PortAudioError(RuntimeError):
    pass


def _bundled_name():
    if sys.platform == "darwin":
        return "libportaudio.dylib"
    if sys.platform == "win32":
        import platform
        return "libportaudioarm64.dll" if platform.machine().lower() in ("arm64", "aarch64") \
            else "libportaudio64bit.dll"
    return None


def cache_dir():
    """Per-user cache directory for audiolink (extracted libraries, browser profile)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "audiolink"


def _load():
    name = _bundled_name()
    if name:
        here = Path(__file__).resolve().parent / "_lib" / name
        if here.is_file():                         # running from a source checkout / folder
            return ctypes.CDLL(str(here))
        data = pkgutil.get_data(__package__, "_lib/" + name)  # running from a .pyz
        if data:
            dest = cache_dir() / hashlib.sha256(data).hexdigest()[:16] / name
            if not dest.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(dest)
            return ctypes.CDLL(str(dest))
    found = ctypes.util.find_library("portaudio")
    if found:
        return ctypes.CDLL(found)
    raise PortAudioError("PortAudio library not found")


_pa = None


def lib():
    global _pa
    if _pa is None:
        pa = _load()
        pa.Pa_GetErrorText.restype = ctypes.c_char_p
        pa.Pa_GetDeviceInfo.restype = ctypes.POINTER(PaDeviceInfo)
        pa.Pa_GetHostApiInfo.restype = ctypes.POINTER(PaHostApiInfo)
        pa.Pa_OpenStream.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(PaStreamParameters),
            ctypes.POINTER(PaStreamParameters), ctypes.c_double, ctypes.c_ulong,
            ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p]
        pa.Pa_ReadStream.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        pa.Pa_WriteStream.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ulong]
        for fn in ("Pa_StartStream", "Pa_StopStream", "Pa_AbortStream", "Pa_CloseStream"):
            getattr(pa, fn).argtypes = [ctypes.c_void_p]
        _check(pa.Pa_Initialize(), pa)
        _pa = pa
    return _pa


def reinit():
    """Re-scan devices. PortAudio only enumerates devices at initialization, so this
    is needed to see a device that was unplugged and plugged back in."""
    pa = lib()
    pa.Pa_Terminate()
    _check(pa.Pa_Initialize(), pa)


def _check(err, pa=None):
    if err < 0:
        pa = pa or _pa
        raise PortAudioError(pa.Pa_GetErrorText(err).decode(errors="replace"))
    return err


def devices():
    """[{index, name, hostapi, inputs, outputs, low_in, low_out}] for every device."""
    pa = lib()
    out = []
    for i in range(_check(pa.Pa_GetDeviceCount())):
        d = pa.Pa_GetDeviceInfo(i).contents
        api = pa.Pa_GetHostApiInfo(d.hostApi).contents
        out.append({
            "index": i, "name": d.name.decode(errors="replace"),
            "hostapi": api.name.decode(errors="replace"), "hostapi_type": api.type,
            "inputs": d.maxInputChannels, "outputs": d.maxOutputChannels,
            "low_in": d.defaultLowInputLatency, "low_out": d.defaultLowOutputLatency,
        })
    return out


class Stream:
    """One blocking int16 stream, input or output."""

    def __init__(self, device, output, rate, channels, frames_per_buffer):
        pa = lib()
        self.channels = channels
        self.output = output
        params = PaStreamParameters(
            device=device["index"], channelCount=channels,
            sampleFormat=paInt16 if output else paFloat32,
            suggestedLatency=device["low_out" if output else "low_in"],
            hostApiSpecificStreamInfo=None)
        if device["hostapi_type"] == paWASAPI:
            self._wasapi = PaWasapiStreamInfo(
                size=ctypes.sizeof(PaWasapiStreamInfo), hostApiType=paWASAPI,
                version=1, flags=paWinWasapiExclusive)
            params.hostApiSpecificStreamInfo = ctypes.cast(ctypes.pointer(self._wasapi), ctypes.c_void_p)
        self.handle = ctypes.c_void_p()
        p = ctypes.byref(params)
        _check(pa.Pa_OpenStream(ctypes.byref(self.handle), None if output else p, p if output else None,
                                rate, frames_per_buffer, paClipOff | paDitherOff, None, None))
        self.buf = ctypes.create_string_buffer(frames_per_buffer * channels * 4)
        _check(pa.Pa_StartStream(self.handle))

    def write(self, data):
        """Blocks until there is room. Returns False on an underflow (gap in output)."""
        err = _pa.Pa_WriteStream(self.handle, data, len(data) // (2 * self.channels))
        if err == paOutputUnderflowed:
            return False
        _check(err)
        return True

    def read(self, frames):
        """Blocks for `frames` frames. Returns (int16 little-endian bytes, overflowed)."""
        err = _pa.Pa_ReadStream(self.handle, self.buf, frames)
        if err not in (0, paInputOverflowed):
            _check(err)
        floats = array("f", self.buf.raw[:frames * 4 * self.channels])
        # Each sample is exactly k/32768, so this multiply is exact (no rounding needed).
        pcm = array("h", map(int, map(_SCALE.__mul__, floats)))
        if sys.byteorder != "little":
            pcm.byteswap()
        return pcm.tobytes(), err == paInputOverflowed

    def close(self):
        """Abort (don't drain) and close; safe to call on a stream whose device is gone."""
        _pa.Pa_AbortStream(self.handle)
        _pa.Pa_CloseStream(self.handle)
