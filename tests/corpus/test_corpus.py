"""Corpus-driven invariants.

The test code ships in the repo; the corpus does not. By default these
tests **auto-discover** XMLs the same way ``dolby_to_easyeffects.py``
does — the union of every probed location, not just the one the script
picks for a single run:

  - NTFS-family mountpoints whose DriverStore holds ``dax3_ext_*.inf_*``
  - any directory under CWD (bounded depth) that directly contains a
    Dolby-shaped XML

If you want to point the suite at an explicit pile of XMLs, set
``ATMOS_CORPUS_DIR=/path/to/dax3/xmls`` — that overrides discovery.

These tests assert *invariants* the converter must hold for any DAX3
XML, not anything specific to a particular tuning. They catch
unknown-XML-shape regressions (new firmware variants, profile mixes)
that synthetic inputs cannot.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dolby_to_easyeffects import (
    DOLBY_FILENAME_RE,
    FIR_LENGTH,
    SAMPLE_RATE,
    _NON_DAX3_FILENAME_SUFFIXES,
    _ntfs_family_mountpoints,
    _resolve_driver_store,
    _walk_for_dolby_xml_dirs,
    make_fir,
    make_preset,
    parse_xml,
    save_wav_stereo,
)
from tests.conftest import is_minimum_phase, read_irs_file


def _is_dax3_xml(name: str) -> bool:
    """Filename matches the DAX3 sentinel and isn't a mic-AEC variant."""
    if name.lower().endswith(_NON_DAX3_FILENAME_SUFFIXES):
        return False
    return bool(DOLBY_FILENAME_RE.search(name))


def _xmls_under(directory: Path) -> list[Path]:
    """All DAX3-shaped XMLs directly under ``directory``."""
    out: list[Path] = []
    try:
        for entry in directory.iterdir():
            if entry.is_file() and _is_dax3_xml(entry.name):
                out.append(entry)
    except OSError:
        pass
    return out


def _autoprobe_corpus() -> list[Path]:
    """Union of every Dolby XML location the main script would consider.

    Mirrors the logic in ``autoprobe_dolby_source`` but, instead of
    picking a single winner, returns *every* XML found across all probed
    locations. Read-only; bounded by ``_walk_for_dolby_xml_dirs``'s depth.
    """
    seen: set[Path] = set()
    found: list[Path] = []

    def _add(p: Path) -> None:
        ap = p.resolve()
        if ap in seen:
            return
        seen.add(ap)
        found.append(ap)

    # 1. Mount-probe: every NTFS mountpoint whose DriverStore exists,
    #    walked the same way find_tuning_xml would walk it (dax3_ext_*
    #    wrappers, plus the driver-store dir itself for hand-extracted
    #    layouts).
    for mp in _ntfs_family_mountpoints():
        ds = _resolve_driver_store(mp)
        if ds is None:
            continue
        for wrapper in sorted(ds.glob("dax3_ext_*.inf_*")):
            for x in _xmls_under(wrapper):
                _add(x)
        for x in _xmls_under(ds):
            _add(x)

    # 2. CWD-probe: every directory under cwd (bounded depth, hidden
    #    pruned) that directly contains a Dolby XML.
    for d in _walk_for_dolby_xml_dirs(Path.cwd()):
        for x in _xmls_under(d):
            _add(x)

    return found


def _discover_corpus() -> list[Path]:
    """Resolve the corpus: explicit env var first, else auto-probe."""
    raw = os.environ.get("ATMOS_CORPUS_DIR")
    if raw:
        root = Path(raw).expanduser()
        if not root.exists():
            return []  # surfaces as "ATMOS_CORPUS_DIR is set but empty"
        return [
            p for p in sorted(root.rglob("*.xml")) if _is_dax3_xml(p.name)
        ]
    return _autoprobe_corpus()


CORPUS = _discover_corpus()
_EXPLICIT = "ATMOS_CORPUS_DIR" in os.environ


def _skip_if_no_corpus():
    if CORPUS:
        return
    if _EXPLICIT:
        pytest.skip(
            f"ATMOS_CORPUS_DIR={os.environ['ATMOS_CORPUS_DIR']!r} resolved "
            "to no DAX3 XMLs"
        )
    pytest.skip(
        "no Dolby XMLs auto-discovered (no NTFS mounts with DAX3 driver "
        "store, no DAX3 XMLs under CWD). Either run from a directory "
        "near your tuning files, or set ATMOS_CORPUS_DIR=/path/to/xmls."
    )


def test_corpus_is_configured():
    """When ATMOS_CORPUS_DIR is explicitly set, it must resolve to at
    least one DAX3 XML — otherwise the override is doing nothing.
    """
    if not _EXPLICIT:
        pytest.skip("auto-discovery mode; nothing to validate here")
    assert CORPUS, (
        f"ATMOS_CORPUS_DIR={os.environ['ATMOS_CORPUS_DIR']!r} resolved "
        "to no DAX3 XMLs"
    )


@pytest.mark.parametrize("xml_path", CORPUS, ids=lambda p: p.name)
def test_corpus_xml_parses_and_runs_pipeline(tmp_path, xml_path):
    """Single per-XML test: parse → invariants → make_fir → make_preset
    → save_wav_stereo → IRS shape. One parse per XML keeps the
    parametrized run honest on a corpus of thousands.

    A small fraction of corpus XMLs use simplified schema variants that
    the parser intentionally rejects (`ValueError`); those skip rather
    than fail so they can't mask real regressions in the rest.
    """
    _skip_if_no_corpus()

    try:
        result = parse_xml(xml_path)
    except ValueError as e:
        pytest.skip(f"{xml_path.name}: parser rejected by design: {e}")
    assert result is not None
    assert len(result) == 12

    (freqs, curves, _ieq_amount, ao_left, ao_right, peq_filters,
     vol_leveler, dialog_enhancer, surround, mb_comp, regulator,
     _volmax_boost) = result

    # --- shape invariants ---
    assert len(freqs) == 20
    assert freqs == sorted(freqs)
    assert 10 <= freqs[0] and freqs[-1] <= 24000
    assert len(ao_left) == 20
    assert len(ao_right) == 20
    if regulator is not None:
        assert len(regulator["threshold_high"]) == 20
        assert 0.0 <= regulator["timbre_preservation"] <= 1.0
    if mb_comp is not None:
        assert 1 <= mb_comp["group_count"] <= 4
        assert len(mb_comp["band_groups"]) >= mb_comp["group_count"]

    # --- full pipeline ---
    if not curves:
        pytest.skip(f"{xml_path.name}: no IEQ curves")
    ieq = next(iter(curves.values()))
    target_l = [(ieq[i] + ao_left[i]) / 16.0 for i in range(20)]
    target_r = [(ieq[i] + ao_right[i]) / 16.0 for i in range(20)]

    fir_l, _ = make_fir(freqs, target_l)
    fir_r, _ = make_fir(freqs, target_r)
    irs = tmp_path / f"{xml_path.stem}.irs"
    save_wav_stereo(irs, fir_l, fir_r)

    preset, _ = make_preset(
        kernel_name=xml_path.stem,
        peq_filters=peq_filters,
        vol_leveler=vol_leveler,
        dialog_enhancer=dialog_enhancer,
        surround=surround,
        mb_comp=mb_comp,
        regulator=regulator,
        freqs=freqs,
    )

    json.dumps(preset)  # catches non-serialisable values

    sr, n, ch, left, _ = read_irs_file(irs)
    assert sr == SAMPLE_RATE
    assert n == FIR_LENGTH
    assert ch == 2
    assert is_minimum_phase(left, tol=1e-2)
