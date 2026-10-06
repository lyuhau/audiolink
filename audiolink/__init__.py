"""audiolink: reliable, multiplexed TCP streams over a bit-exact USB audio channel.

Pure standard-library Python (3.9+). The only native code is the bundled
PortAudio library, loaded with ctypes on the host side.
"""

__version__ = "0.9.0"
PROTOCOL_VERSION = 2
