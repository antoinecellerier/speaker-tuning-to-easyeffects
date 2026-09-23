"""The one way ``lib/`` reads the machine's own state off the filesystem.

What these tools report about the hardware — codecs and speaker pins, the
PCI subsystem, SoundWire devices, DMI, loaded modules and firmware files, the
distro, the kernel — is read from ``/proc``, ``/sys``, ``/etc`` and
``/lib/firmware``. A test that reaches the real ones depends on the machine
running it, and an HDA codec's ``/proc/asound/card*/codec#*`` is not even a
cheap read: each goes to the hardware, and the kernel serialises them, so a
dozen test workers reading the same codec queued behind each other for most of
a fast-tier run (2026-09-23: 456 of 518 s spent waiting).

With ``ATMOS_HOST_ROOT`` set, ``path()`` re-roots every such location under
it. The test suite points it at a directory holding only what a test put
there (``tests/conftest.py``), and the scripts a test starts inherit it. A
path that is not a host location — a test's own tree, passed in as a
parameter or patched into a module constant — comes back unchanged, which is
what keeps those seams working. ``tests/conftest.py`` fails a test that opens
a real host location without ``@pytest.mark.live_machine``.

Stdlib-only: the converter imports it at startup through ``lib.packages``.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

HOST_ROOT = "ATMOS_HOST_ROOT"

# Where lib/ reads machine state. Anything else handed to `path` is the
# caller's own and passes through.
HOST_PREFIXES = ("/proc/", "/sys/", "/etc/", "/lib/firmware",
                 "/var/lib/flatpak/")


def path(location: str | os.PathLike) -> Path:
    """``location`` as this process should read it: re-rooted under
    ``ATMOS_HOST_ROOT`` when that is set and ``location`` is a host path."""
    location = Path(location)
    root = os.environ.get(HOST_ROOT)
    if root and str(location).startswith(HOST_PREFIXES):
        return Path(root, *location.parts[1:])
    return location


def kernel_release() -> str:
    """The running kernel's release, as ``uname -r`` prints it.

    Read from ``/proc`` rather than ``platform.release()`` so it re-roots with
    everything else; under a root that doesn't carry it, "" — never the real
    kernel's.
    """
    try:
        return path("/proc/sys/kernel/osrelease").read_text().strip()
    except OSError:
        return "" if os.environ.get(HOST_ROOT) else platform.release()
