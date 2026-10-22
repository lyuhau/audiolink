"""audiolink: reliable, multiplexed TCP streams over a bit-exact USB audio channel.

Pure standard-library Python (3.9+). The only native code is the bundled
PortAudio library, loaded with ctypes on the host side.
"""

__version__ = "0.14.0"
PROTOCOL_VERSION = 3

# Which build: a hash of the package's files, which build_pyz.py writes into the .pyz
# (_build.py) so two builds of one version can be told apart. None: run from source.
try:
    from ._build import BUILD
except ImportError:
    BUILD = None


def version_text():
    return "%s (build %s)" % (__version__, BUILD) if BUILD else "%s (from source)" % __version__
