"""Find the vendor APO layer, if any, that belongs beside a DAX3 XML, or
the standalone vendor format that stands in for one.

One finder per supported format, tried in order; the first that binds the
device wins. The generator calls `find_for_xml` once it has picked the XML,
before its deferred DSP imports, so this stays stdlib-only.

A device that ships no Dolby file has its whole tuning in a standalone
format instead, listed in `STANDALONE` (`lib/apo/layer.py`
`StandaloneFormat`). `lib/tuning_source.py` asks it which, if any, a path
the run was given is.
"""

from __future__ import annotations

from pathlib import Path

from lib.apo import sonitude, surface
from lib.apo.layer import ApoLayer, StandaloneFormat

FINDERS = (surface.find,)

# What each format's finder reads out of a driver download, so
# `lib/driver_package.py` unpacks those files and no others.
_MEMBER_FILTERS = (surface.wanted_members,)


def wanted_members(paths: list[str]) -> set[str]:
    """The archive members any finder here reads, out of a file list."""
    return set().union(*(f(paths) for f in _MEMBER_FILTERS))


STANDALONE = (sonitude.FORMAT,)


def standalone_for(path: Path) -> StandaloneFormat | None:
    """The first standalone format *path* is a source of, or None."""
    return next((f for f in STANDALONE if f.is_source(Path(path))), None)


def stands_in_for_xml(name: str) -> bool:
    """Whether driver-download member *name* is a standalone format's
    tuning, which `lib/driver_package.py` keeps as it keeps a DAX3 XML."""
    return any(f.wanted_members([name]) for f in STANDALONE)


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
