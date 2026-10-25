"""Dolby DAX2 tunings in the corpus's driver `.inf` files (`lib/apo/dax2.py`).

Walks every `.inf` under `ATMOS_CORPUS_DIR` that carries the DAX2 key, by
content, once per distinct file. Skips cleanly without it, or when it holds
none: DAX2 tunings live in driver packages, not beside any DAX3 XML, so the
XML discovery never reaches them. The trees are read without the
DolbyDAX2APO prefilter, since a pruned research tree keeps the `.inf` and
drops the binaries.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest

from lib.apo import dax2, inf


def _infs() -> list[Path]:
    raw = os.environ.get("ATMOS_CORPUS_DIR")
    if not raw:
        return []
    seen, out = set(), []
    for p in dax2.infs_below(Path(raw), beside_apo=False):
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if digest not in seen:
            seen.add(digest)
            out.append(p)
    return out


INFS = _infs()
_HWID_RE = re.compile(r"VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})&SUBSYS_([0-9A-F]{8})",
                      re.IGNORECASE)


def _codecs(path: Path) -> set[tuple[str, str]]:
    """Every (vendor-device, subsystem) an HD-Audio hardware ID in the model
    sections Windows reads from *path* on x64 names."""
    sections = inf.read(path)
    out = set()
    for line in sections.get("manufacturer", []):
        base, *decorations = inf.fields(line.partition("=")[2])
        for model in inf.models_applied(base, decorations):
            for entry in sections.get(model.lower(), []):
                for hwid in inf.fields(entry.partition("=")[2])[1:]:
                    m = _HWID_RE.search(hwid)
                    if m:
                        out.add((m[1] + m[2], m[3]))
    return out


@pytest.mark.skipif(not INFS, reason="no .inf with a DAX2 tuning under "
                    "ATMOS_CORPUS_DIR")
@pytest.mark.parametrize("path", INFS, ids=[f"{p.parent.name}/{p.name}"
                                            for p in INFS])
def test_every_dax2_preset_an_inf_binds_reads_as_a_tuning(path):
    """Each codec a DAX2 preset binds converts to a Dolby tuning on the
    audio-optimizer's 20-band grid; a DAX3 registry-form preset is refused
    by name, never misread."""
    read = refused = 0
    for vendor_device, subsys in sorted(_codecs(path)):
        try:
            preset = dax2.bind(path, vendor_device, subsys)
        except dax2.UnsupportedApoConfig as exc:
            assert "DAX3's registry form" in exc.reason, exc.reason
            refused += 1
            continue
        if preset is None:
            continue
        tuning = dax2.tuning(preset)
        assert len(tuning.freqs) == 20
        assert len(tuning.ao_left) == len(tuning.ao_right) == 20
        assert len(tuning.curves["ieq_balanced"]) == 20
        if tuning.regulator is not None:
            assert len(tuning.regulator["threshold_high"]) == 20
        read += 1
    assert read or refused, f"{path.name}: carries the key, binds nothing"
