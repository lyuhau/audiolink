"""The wire: bytes over the USB sound card.

protocol    framing (sync word + CRC) and the reliable link (go-back-N ARQ)
alsa        Pi side: aplay/arecord on the UAC2 gadget
host_audio  host side: find the gadget's devices, stream them, reopen when lost
portaudio   ctypes binding to the bundled PortAudio (host side)
"""
