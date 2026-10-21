"""HP's Sonitude tuning archives found in the corpus (`lib/apo/sonitude.py`).

Walks every `SEAPODAT.*.zip` under `ATMOS_CORPUS_DIR`. Skips cleanly without
it, or when it holds none: the archives ship inside HP's driver downloads,
not beside any DAX3 XML, so the XML discovery never reaches them.
"""

from __future__ import annotations

import math
import os
import zipfile
from pathlib import Path

import pytest

from lib.apo import sonitude
from lib.apo.layer import UnsupportedApoConfig


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


@pytest.mark.skipif(not ARCHIVES, reason="no SEAPODAT archive under "
                    "ATMOS_CORPUS_DIR")
@pytest.mark.parametrize("archive", ARCHIVES, ids=[a.name for a in ARCHIVES])
def test_every_mapped_id_binds_through_the_lookup_the_run_uses(archive):
    """`bindings` then `load_profiles`, the converter's path, reads every
    subsystem the map names into a cuts-only layer per profile, or refuses
    it as unsupported; never a crash."""
    bus, ven, dev = sonitude._ARCHIVE_RE.fullmatch(archive.name).groups()
    bus = bus.upper()
    with zipfile.ZipFile(archive) as z:
        names = {n.replace("\\", "/").lower(): n for n in z.namelist()}
        mapping = sonitude.read_ssidmap(z.read(names["ssidmap.bin"]))
    built = 0
    for subsys in mapping:
        found = sonitude.bindings(archive, ven + dev, subsys)
        assert found.unreadable == () and found.codec_seen
        assert list(found.hits) == [bus]
        try:
            layers = sonitude.load_profiles(*found.hits[bus], bus,
                                            ven + dev, subsys)
        except UnsupportedApoConfig:
            continue
        assert next(iter(layers)) == "music"
        for layer in layers.values():
            for db in (layer.curve_left_db, layer.curve_right_db):
                assert all(math.isfinite(v) and v <= 0 for v in db)
            assert len(layer.curve_hz) == sonitude.CURVE_POINTS
        built += 1
    assert built, f"{archive.name}: no mapped ID built a layer"


@pytest.mark.skipif(not ARCHIVES, reason="no SEAPODAT archive under "
                    "ATMOS_CORPUS_DIR")
def test_real_speaker_curves_take_both_low_end_readings():
    """Every real preset walks to its end in the stage/key layout, and real
    presets take both low-end readings: a Butterworth-4 below the corner of
    a high-pass that is on, and the stored curve where it is off; a rule
    that sent every curve down one path would pass the unit tests."""
    high_passed = kept = 0
    for archive in ARCHIVES:
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                if not name.replace("\\", "/").lower().endswith(
                        sonitude.SPEAKER_PRESET.lower()):
                    continue
                data = z.read(name)
                curves = sonitude.read_curves(data)
                values = sonitude.stage_values(data)
                assert values, f"{archive.name}: {name} doesn't walk to its end"
                if not curves:
                    continue
                c = curves[0]
                corner = sonitude._high_pass_hz(values, 19)
                if sonitude._apply_high_pass(c, corner) == tuple(c):
                    kept += 1
                else:
                    high_passed += 1
    assert high_passed and kept, (high_passed, kept)
