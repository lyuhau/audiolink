"""macOS: know the instant an audio device goes away, and which USB device a
sound device belongs to.

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
import re
import struct
import threading

log = logging.getLogger("audiolink")


def _fourcc(s):
    return struct.unpack(">I", s.encode())[0]


SYSTEM_OBJECT = 1
DEVICES = _fourcc("dev#")
IS_ALIVE = _fourcc("livn")
NOMINAL_RATE = _fourcc("nsrt")
NAME = _fourcc("lnam")
TRANSPORT = _fourcc("tran")
MODEL_UID = _fourcc("muid")
USB = _fourcc("usb ")
SCOPE_GLOBAL = _fourcc("glob")
ELEMENT_MAIN = 0


class Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


LISTENER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.POINTER(Address), ctypes.c_void_p)
_ca = None
_cf = None


def _lib():
    global _ca
    if _ca is None:
        ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        for fn in ("AudioObjectAddPropertyListener", "AudioObjectRemovePropertyListener"):
            getattr(ca, fn).argtypes = [ctypes.c_uint32, ctypes.POINTER(Address), LISTENER, ctypes.c_void_p]
        ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(Address), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(Address), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32)]
        _ca = ca
    return _ca


def _get(obj, selector, buf):
    size = ctypes.c_uint32(ctypes.sizeof(buf))
    addr = Address(selector, SCOPE_GLOBAL, ELEMENT_MAIN)
    return _lib().AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size),
                                             ctypes.byref(buf)) == 0


def _string(obj, selector):
    """A CFString property as str, or None."""
    global _cf
    if _cf is None:
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        _cf = cf
    ref = ctypes.c_void_p()
    if not _get(obj, selector, ref) or not ref.value:
        return None
    try:
        buf = ctypes.create_string_buffer(1024)
        ok = _cf.CFStringGetCString(ref, buf, len(buf), 0x08000100)     # UTF-8
        return buf.value.decode() if ok else None
    finally:
        _cf.CFRelease(ref)


def devices():
    """Core Audio's device ids."""
    size = ctypes.c_uint32(0)
    addr = Address(DEVICES, SCOPE_GLOBAL, ELEMENT_MAIN)
    if _lib().AudioObjectGetPropertyDataSize(SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size)):
        return []
    ids = (ctypes.c_uint32 * (size.value // 4))()
    return list(ids) if _get(SYSTEM_OBJECT, DEVICES, ids) else []


# A USB sound device's model UID ends in its vendor and product ID: "Topology Control:1D6B:0104"
MODEL_IDS = re.compile(r":([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})$")


def usb_id(dev):
    """(vendor, product) of the USB device behind Core Audio device `dev`, or None."""
    transport = ctypes.c_uint32(0)
    if not _get(dev, TRANSPORT, transport) or transport.value != USB:
        return None
    m = MODEL_IDS.search(_string(dev, MODEL_UID) or "")
    return (int(m.group(1), 16), int(m.group(2), 16)) if m else None


UID = _fourcc("uid ")


def usb_ids():
    """lookup(portaudio device, its position among PortAudio's Core Audio devices) ->
    ((vendor, product) or None, the USB device it's part of or None). PortAudio lists
    Core Audio's devices in Core Audio's order (two USB devices can share a name, real
    headphones and a Pi cloned from them): by position, its name checked; else by name,
    and a name that devices with different IDs share answers None (names decide then).
    The USB device: a USB sound device's UID less its last part
    ("AppleUSBAudioEngine:SteelSeries:Arctis GameBuds:1100000:1": its location)."""
    listed = []
    for d in devices():
        uid, uid_usb = _string(d, UID) or "", usb_id(d)
        part = uid.rsplit(":", 1)[0] if uid_usb and uid.startswith("AppleUSBAudioEngine:") else None
        listed.append((_string(d, NAME), uid_usb, part))
    by_name = {}
    for name, uid_usb, _ in listed:
        by_name.setdefault(name, set()).add(uid_usb)

    def lookup(dev, pos=None):
        if pos is not None and pos < len(listed) and listed[pos][0] == dev["name"]:
            return listed[pos][1], listed[pos][2]
        ids = by_name.get(dev["name"], set())
        return (next(iter(ids)) if len(ids) == 1 else None), None
    return lookup


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
