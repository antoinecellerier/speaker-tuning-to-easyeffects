"""HP's Sonitude tuning archives found in the corpus (`lib/apo/sonitude.py`).

Walks every `SEAPODAT.*.zip` under `ATMOS_CORPUS_DIR`. Skips cleanly without
it, or when it holds none: the archives ship inside HP's driver downloads,
not beside any DAX3 XML, so the XML discovery never reaches them.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from lib.apo import sonitude


def _archives() -> list[Path]:
    raw = os.environ.get("ATMOS_CORPUS_DIR")
    if not raw:
        return []
    seen, out = set(), []
    for p in sorted(sonitude.archives_in(Path(raw))):
        key = (p.name, p.stat().st_size)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


ARCHIVES = _archives()


@pytest.mark.skipif(not ARCHIVES, reason="no SEAPODAT archive under "
                    "ATMOS_CORPUS_DIR")
@pytest.mark.parametrize("archive", ARCHIVES, ids=[a.name for a in ARCHIVES])
def test_every_mapped_speaker_preset_yields_its_curves(archive):
    """Each subsystem the map names leads to a speaker preset with two
    265-point curves, or to none at all; never to an unreadable one."""
    with zipfile.ZipFile(archive) as z:
        names = {n.replace("\\", "/").lower(): n for n in z.namelist()}
        mapping = sonitude.read_ssidmap(z.read(names["ssidmap.bin"]))
        assert mapping
        read = 0
        for number in set(mapping.values()):
            key = f"{number}/{sonitude.SPEAKER_PRESET}".lower()
            if key not in names:
                continue
            assert len(sonitude.read_curves(z.read(names[key]))) == 2, key
            read += 1
    assert read, f"{archive.name}: no mapped preset has a speaker file"
