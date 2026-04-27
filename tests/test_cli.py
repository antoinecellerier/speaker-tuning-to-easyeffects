"""CLI option coverage.

Argparse plumbing is exercised by:
  - calling `make_preset(disabled=...)` directly with the same names the
    CLI's `--disable NAME` produces (`DISABLEABLE_FILTERS`)
  - subprocess-running the script for the entry-point options that
    short-circuit before any I/O (`--help`, `--speaker-info`, mutually
    exclusive flags)

`--all-profiles`, `--profile`, `--endpoint`, `--mode`, `--prefix`,
`--output-dir`, `--irs-dir`, `--autoload`, `--dry-run`, and `--windows`
are end-to-end-only paths through `parse_xml`/`find_tuning_xml` and
filesystem writes; they are exercised by the corpus tests
(`tests/corpus/`) when a corpus is reachable, or by manual run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from dolby_to_easyeffects import (
    DISABLEABLE_FILTERS,
    DOLBY_FILENAME_RE,
    make_preset,
)
from tests.conftest import (
    SYNTHETIC_FREQS_20,
    synthetic_mb_comp,
    synthetic_peq_filters,
    synthetic_regulator,
)


SCRIPT = Path(__file__).resolve().parent.parent / "dolby_to_easyeffects.py"


# --- --disable: each name should drop its plugin/filter from the preset ---

def _full_inputs():
    """A complete plugin set (PEQ with all relevant types, MBC,
    regulator, dialog, surround, leveler) so every --disable target has
    something to drop.
    """
    peq = synthetic_peq_filters([
        (0, 1, 1000.0, 4.0, 1.5, 0, 1.0),    # bell
        (1, 1, 1000.0, 4.0, 1.5, 0, 1.0),
        (0, 7, 90.0, 0.0, 0.707, 4, 1.0),    # HP
        (1, 7, 90.0, 0.0, 0.707, 4, 1.0),
        (0, 3, 5000.0, 3.0, 1.0, 0, 1.0),    # high-shelf (experimental)
        (1, 3, 5000.0, 3.0, 1.0, 0, 1.0),
        (0, 6, 8000.0, 0.0, 0.707, 4, 1.0),  # low-pass (experimental)
        (1, 6, 8000.0, 0.0, 0.707, 4, 1.0),
    ])
    mb = synthetic_mb_comp(group_count=2, bands=[
        (10, -160, 16384, 30000, 32500, 0),
        (20, -160, 16384, 30000, 32500, 0),
    ])
    reg = synthetic_regulator([-6.0] * 20)
    return dict(
        peq_filters=peq,
        vol_leveler={"enable": True, "amount": 5, "out_target": -16.0},
        dialog_enhancer={"enable": True, "amount": 5, "boost": 4.0},
        surround={"enable": True, "boost": 4},
        mb_comp=mb,
        regulator=reg,
        freqs=SYNTHETIC_FREQS_20,
    )


def _build(disabled=None, **overrides):
    inputs = _full_inputs()
    inputs.update(overrides)
    return make_preset(
        kernel_name="CLI-Test",
        disabled=disabled or set(),
        **inputs,
    )


def test_disable_choices_match_documented_set():
    """Sanity: the `--disable` argparse `choices=` list IS
    DISABLEABLE_FILTERS — if the constant grows or shrinks, the CLI's
    valid choices follow. Pinning this prevents a quiet drift between
    the documented choices and the actual code paths.
    """
    expected = {
        "volmax", "mbc", "regulator", "bass-enhancer", "dialog",
        "stereo", "high-shelf", "lo-pass",
    }
    assert set(DISABLEABLE_FILTERS) == expected


def test_disable_mbc_drops_multiband_compressor():
    preset, emitted = _build(disabled={"mbc"})
    assert "multiband_compressor#0" not in preset["output"]
    assert "mbc" not in emitted
    # The regulator (multiband_compressor#1) must still be present —
    # they share a plugin type and naming scheme; only #0 is the MBC.
    assert "multiband_compressor#1" in preset["output"]


def test_disable_regulator_drops_per_band_limiter():
    preset, emitted = _build(disabled={"regulator"})
    assert "multiband_compressor#1" not in preset["output"]
    assert "regulator" not in emitted


def test_disable_dialog_drops_dialog_enhancer():
    preset, emitted = _build(disabled={"dialog"})
    # equalizer#1 is the dialog enhancer; equalizer#0 is the speaker PEQ.
    assert "equalizer#1" not in preset["output"]
    assert "equalizer#0" in preset["output"]
    assert "dialog" not in emitted


def test_disable_stereo_drops_stereo_widener():
    preset, emitted = _build(disabled={"stereo"})
    assert "stereo_tools#0" not in preset["output"]
    assert "stereo" not in emitted


def test_disable_high_shelf_drops_type3_filters():
    """High-shelf is type 3 in the PEQ filter list — disabling it must
    leave the PEQ without any Hi-shelf bands.
    """
    preset, emitted = _build(disabled={"high-shelf"})
    eq = preset["output"].get("equalizer#0")
    assert eq is not None
    for side in ("left", "right"):
        for band in eq[side].values():
            assert band["type"] != "Hi-shelf"
    assert "high-shelf" not in emitted


def test_disable_lo_pass_drops_type6_8_filters():
    preset, emitted = _build(disabled={"lo-pass"})
    eq = preset["output"].get("equalizer#0")
    assert eq is not None
    for side in ("left", "right"):
        for band in eq[side].values():
            assert band["type"] != "Lo-pass"
    assert "lo-pass" not in emitted


# volmax-boost has two routing slots: regulator output-gain (primary,
# matches Dolby's VolMax topology) or limiter input-gain (fallback, when
# regulator is disabled or absent). --disable volmax must zero both.

def test_volmax_lands_on_regulator_when_present():
    """Sanity: with regulator enabled and volmax NOT disabled, the
    boost lands on regulator output-gain and the limiter stays at 0.
    """
    preset, emitted = _build(volmax_boost=3.0)
    assert preset["output"]["multiband_compressor#1"]["output-gain"] == pytest.approx(3.0)
    assert preset["output"]["limiter#0"]["input-gain"] == 0.0
    assert "volmax" in emitted


def test_volmax_falls_back_to_limiter_when_regulator_disabled():
    """When regulator is dropped, the boost has to land somewhere or the
    user loses the loudness uplift. The fallback slot is limiter#0
    input-gain.
    """
    preset, emitted = _build(disabled={"regulator"}, volmax_boost=3.0)
    assert "multiband_compressor#1" not in preset["output"]
    assert preset["output"]["limiter#0"]["input-gain"] == pytest.approx(3.0)
    assert "volmax" in emitted


def test_disable_volmax_zeroes_regulator_slot():
    """--disable volmax with regulator present: regulator output-gain
    drops to 0 regardless of volmax_boost value.
    """
    preset, emitted = _build(disabled={"volmax"}, volmax_boost=3.0)
    assert preset["output"]["multiband_compressor#1"]["output-gain"] == 0.0
    assert preset["output"]["limiter#0"]["input-gain"] == 0.0
    assert "volmax" not in emitted


def test_disable_volmax_zeroes_limiter_slot_too():
    """--disable volmax + --disable regulator: neither slot gets the
    boost. The fallback path must respect --disable volmax.
    """
    preset, emitted = _build(disabled={"volmax", "regulator"}, volmax_boost=3.0)
    assert preset["output"]["limiter#0"]["input-gain"] == 0.0
    assert "volmax" not in emitted
    assert "regulator" not in emitted


def test_disable_bass_enhancer_drops_harmonic_generator():
    """Bass enhancer only emits for SoundWire presets in the first place,
    so we need is_soundwire=True to test the disable. Without --disable
    bass-enhancer, soundwire mode emits bass_enhancer#0; with it, the
    plugin is dropped.
    """
    preset_on, emitted_on = _build(is_soundwire=True)
    preset_off, emitted_off = _build(is_soundwire=True, disabled={"bass-enhancer"})
    assert "bass_enhancer#0" in preset_on["output"]
    assert "bass-enhancer" in emitted_on
    assert "bass_enhancer#0" not in preset_off["output"]
    assert "bass-enhancer" not in emitted_off


# --- argparse smoke tests (subprocess) ---

def _run_script(*args, timeout=10):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_help_exits_cleanly():
    """`--help` prints argparse's auto-generated text and exits 0; this
    smokes the entire argparse setup (no missing imports, no
    duplicate-flag errors, no choices=... referencing an undefined name).
    """
    result = _run_script("--help")
    assert result.returncode == 0
    assert "Convert Dolby DAX3" in result.stdout


def test_xml_and_windows_are_mutually_exclusive(tmp_path):
    """Passing both a positional XML and `--windows` is a usage error.
    Argparse exits with code 2 by convention.
    """
    fake_xml = tmp_path / "fake.xml"
    fake_xml.write_text("<root/>")
    fake_dir = tmp_path / "winroot"
    fake_dir.mkdir()
    result = _run_script(str(fake_xml), "--windows", str(fake_dir))
    assert result.returncode == 2
    assert "specify either" in result.stderr.lower() or "not both" in result.stderr.lower()


def test_disable_rejects_unknown_filter():
    """`--disable NAME` is constrained by `choices=DISABLEABLE_FILTERS`
    — unknown values must be rejected at parse time.
    """
    result = _run_script("--disable", "nonexistent-filter")
    assert result.returncode == 2
    assert "nonexistent-filter" in result.stderr or "invalid choice" in result.stderr


def test_nonexistent_xml_path_fails_cleanly(tmp_path):
    """Pointing at a missing XML must exit with code 1 (the
    `(FileNotFoundError, RuntimeError, ValueError)` branch in
    __main__), not raise an uncaught exception. Catches a regression
    where the entry-point exception handler is removed or narrowed.
    """
    fake = tmp_path / "definitely-does-not-exist.xml"
    result = _run_script(str(fake))
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "error" in combined.lower() or "no such" in combined.lower()


# --- Dolby filename auto-discovery regex ---
# DOLBY_FILENAME_RE drives both `find_tuning_xml` and the test-corpus
# auto-discovery. A broken regex causes silent miss/over-include —
# corpus tests would either skip XMLs or feed parse_xml junk that
# raises ValueError and gets skipped. Worth a direct test.

@pytest.mark.parametrize("filename", [
    "DEV_0287_SUBSYS_17AA22E6.xml",
    "DEV_0287_SUBSYS_17AA22E6_PCI_SUBSYS_22E617AA.xml",
    "SOUNDWIRE_DEV_0123_SUBSYS_17AA22E6_PCI_SUBSYS_22E617AA.xml",
    "SDW_DEV_0123_SUBSYS_17AA22E6.xml",
    "dev_0287_subsys_17aa22e6.xml",  # case-insensitive
])
def test_dolby_filename_regex_matches_dax3_filenames(filename):
    assert DOLBY_FILENAME_RE.search(filename) is not None


@pytest.mark.parametrize("filename", [
    "settings.xml",                        # no SUBSYS_ token
    "DEV_0287_SUBSYS_17AA22E6.txt",        # not .xml
    "SUBSYS_TOOSHORT.xml",                 # 8-hex-char requirement
    "SUBSYS_17AA22E6_NO_DOT_XML",          # no .xml
    "DEV_0287.xml",                        # no SUBSYS_ token
])
def test_dolby_filename_regex_rejects_non_dax3(filename):
    assert DOLBY_FILENAME_RE.search(filename) is None
