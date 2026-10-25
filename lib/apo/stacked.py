"""Find the vendor APO layer, if any, stacked on a DAX3 XML's Dolby chain.

One finder per supported format, tried in order; the first that binds the
device wins. A run from the XML (`lib/tuning_source.py` `DolbyXml`) calls
`find_for_xml` before the generator's deferred DSP imports, so this stays
stdlib-only.
"""

from __future__ import annotations

from pathlib import Path

from lib.apo import surface
from lib.apo.layer import ApoLayer

FINDERS = (surface.find,)

# What each format's finder reads out of a driver download, so
# `lib/driver_package.py` unpacks those files and no others.
_MEMBER_FILTERS = (surface.wanted_members,)


def wanted_members(paths: list[str]) -> set[str]:
    """The archive members any finder here reads, out of a file list."""
    return set().union(*(f(paths) for f in _MEMBER_FILTERS))


def find_for_xml(xml_path: Path) -> ApoLayer | None:
    """The first vendor APO layer bound to *xml_path*'s device, or None.

    A finder's `UnsupportedApoConfig` propagates: a config that binds this
    device but can't be read is worth telling the user about.
    """
    for finder in FINDERS:
        layer = finder(Path(xml_path))
        if layer is not None:
            return layer
    return None
