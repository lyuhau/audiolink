"""macOS: know the instant an audio device goes away.

When a device disappears, Core Audio moves streams that were playing to it over to
the default output, so the link's frames would play on the speakers (as rhythmic
pops), and our input to the microphone, so reads never stall either. PortAudio
doesn't report the removal, so we listen to Core Audio ourselves: DeviceIsAlive
on the devices, and the system's device list as a backup. Core Audio reroutes
~20 ms before it notifies, so a little can still reach the speakers; aborting at
once keeps that to a fraction of a second instead of indefinitely.
"""
import ctypes
import logging
import struct
import threading

log = logging.getLogger("audiolink")


def _fourcc(s):
    return struct.unpack(">I", s.encode())[0]


SYSTEM_OBJECT = 1
DEVICES = _fourcc("dev#")
IS_ALIVE = _fourcc("livn")
NOMINAL_RATE = _fourcc("nsrt")
SCOPE_GLOBAL = _fourcc("glob")
ELEMENT_MAIN = 0


class Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


LISTENER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.POINTER(Address), ctypes.c_void_p)
_ca = None


def _lib():
    global _ca
    if _ca is None:
        ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        for fn in ("AudioObjectAddPropertyListener", "AudioObjectRemovePropertyListener"):
            getattr(ca, fn).argtypes = [ctypes.c_uint32, ctypes.POINTER(Address), LISTENER, ctypes.c_void_p]
        ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(Address), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        _ca = ca
    return _ca


def is_alive(dev):
    alive = ctypes.c_uint32(0)
    size = ctypes.c_uint32(4)
    addr = Address(IS_ALIVE, SCOPE_GLOBAL, ELEMENT_MAIN)
    err = _lib().AudioObjectGetPropertyData(dev, ctypes.byref(addr), 0, None, ctypes.byref(size),
                                            ctypes.byref(alive))
    return err == 0 and alive.value != 0


class Watch:
    """Calls on_lost(reason) once, from a Core Audio thread, when any of the devices
    dies or changes its sample rate. close() stops watching."""

    def __init__(self, devices, on_lost):
        self.devices = [d for d in set(devices) if d]
        self.on_lost = on_lost
        self.fired = False
        self.lock = threading.Lock()
        self.cb = LISTENER(self._changed)           # must outlive the registration
        self.regs = [(dev, Address(sel, SCOPE_GLOBAL, ELEMENT_MAIN))
                     for dev in self.devices for sel in (IS_ALIVE, NOMINAL_RATE)]
        self.regs.append((SYSTEM_OBJECT, Address(DEVICES, SCOPE_GLOBAL, ELEMENT_MAIN)))
        ca = _lib()
        for obj, addr in self.regs:
            ca.AudioObjectAddPropertyListener(obj, ctypes.byref(addr), self.cb, None)

    def _changed(self, obj, n, addrs, _):
        try:
            selectors = {addrs[i].selector for i in range(n)}
            if NOMINAL_RATE in selectors and obj in self.devices:
                self._fire("sample rate changed")
            elif not all(is_alive(d) for d in self.devices):
                self._fire("device removed")
        except Exception as e:                      # never raise into Core Audio
            log.debug("coreaudio listener: %s", e)
        return 0

    def _fire(self, reason):
        with self.lock:
            if self.fired:
                return
            self.fired = True
        self.on_lost(reason)

    def alive(self):
        return all(is_alive(d) for d in self.devices)

    def close(self):
        ca = _lib()
        for obj, addr in self.regs:
            ca.AudioObjectRemovePropertyListener(obj, ctypes.byref(addr), self.cb, None)
