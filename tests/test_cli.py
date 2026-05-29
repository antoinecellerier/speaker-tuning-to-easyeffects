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

import dolby_to_easyeffects
from dolby_to_easyeffects import (
    DISABLEABLE_FILTERS,
    DOLBY_FILENAME_RE,
    autoprobe_dolby_source,
    find_tuning_xml,
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
    # Genuine CLI misuse keeps the usage banner and points at --help.
    assert "usage:" in result.stderr.lower()
    assert "--help" in result.stderr


def test_disable_rejects_unknown_filter():
    """`--disable NAME` is constrained by `choices=DISABLEABLE_FILTERS`
    — unknown values must be rejected at parse time.
    """
    result = _run_script("--disable", "nonexistent-filter")
    assert result.returncode == 2
    assert "nonexistent-filter" in result.stderr or "invalid choice" in result.stderr
    # Argparse errors should carry the --help pointer (_HelpHintParser).
    assert "--help" in result.stderr


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


# --- find_tuning_xml / autoprobe_dolby_source error-branch coverage ---
# These paths build human-readable diagnostics by joining over collections
# of hardware-detected tuples / Paths. PR #16 fixed a tuple-arity bug in
# the `if not candidates:` branch that shipped because no test exercised
# it. Pin each error branch directly so a future arity or formatting
# regression fails CI here rather than at runtime for end users.


def test_find_tuning_xml_raises_when_no_audio_hardware(monkeypatch, tmp_path):
    """No HDA codecs and no SoundWire devices → FileNotFoundError
    at dolby_to_easyeffects.py:761-765. The earliest bailout in
    find_tuning_xml; static message, but pinned so a future
    refactor doesn't silently turn it into a different exception type.
    """
    monkeypatch.setattr(dolby_to_easyeffects, "get_hda_codec_ids", lambda: [])
    monkeypatch.setattr(dolby_to_easyeffects, "get_soundwire_ids", lambda: [])
    with pytest.raises(FileNotFoundError, match="No HDA codecs or SoundWire"):
        find_tuning_xml(tmp_path)


def test_find_tuning_xml_raises_when_soundwire_without_pci(monkeypatch, tmp_path):
    """SoundWire devices detected but PCI subsystem ID is None →
    RuntimeError at dolby_to_easyeffects.py:773-777. Without the PCI
    subsystem we can't build the SDW SUBSYS match key, so refusing to
    guess is the right behavior; pin the exception type and message.
    """
    monkeypatch.setattr(dolby_to_easyeffects, "get_hda_codec_ids", lambda: [])
    monkeypatch.setattr(
        dolby_to_easyeffects, "get_soundwire_ids", lambda: [("025D", "1318")]
    )
    monkeypatch.setattr(dolby_to_easyeffects, "get_pci_audio_subsystem", lambda: None)
    with pytest.raises(RuntimeError, match="PCI subsystem"):
        find_tuning_xml(tmp_path)


def test_find_tuning_xml_raises_when_driver_store_missing(monkeypatch, tmp_path):
    """Hardware detected but _resolve_driver_store returns None →
    FileNotFoundError at dolby_to_easyeffects.py:787-793. The message
    interpolates Path objects; pin that the formatter survives.
    """
    monkeypatch.setattr(
        dolby_to_easyeffects,
        "get_hda_codec_ids",
        lambda: [("10EC0287", "17AA22E6", "Realtek ALC287")],
    )
    monkeypatch.setattr(dolby_to_easyeffects, "get_soundwire_ids", lambda: [])
    monkeypatch.setattr(dolby_to_easyeffects, "_resolve_driver_store", lambda _p: None)
    with pytest.raises(FileNotFoundError, match="DriverStore not found"):
        find_tuning_xml(tmp_path)


def test_find_tuning_xml_no_matching_xml_includes_hda_in_message(monkeypatch, tmp_path):
    """Regression sentinel for PR #16. The `if not candidates:` branch at
    dolby_to_easyeffects.py:835-843 unpacks hda_codecs via a generator
    expression inside an f-string; an arity mismatch (which is what
    PR #16 fixed) crashes the diagnostic with ValueError instead of
    reaching the FileNotFoundError. This test asserts both the exception
    type and that the codec identifiers reach the user-visible message,
    so reverting line 836 to the 2-tuple unpack fails here.
    """
    monkeypatch.setattr(
        dolby_to_easyeffects,
        "get_hda_codec_ids",
        lambda: [("10EC0287", "17AA22E6", "Realtek ALC287")],
    )
    monkeypatch.setattr(dolby_to_easyeffects, "get_soundwire_ids", lambda: [])
    monkeypatch.setattr(dolby_to_easyeffects, "_resolve_driver_store", lambda _p: tmp_path)
    with pytest.raises(
        FileNotFoundError, match=r"vendor=10EC0287 subsys=17AA22E6"
    ):
        find_tuning_xml(tmp_path)


def test_find_tuning_xml_no_matching_xml_includes_sdw_in_message(monkeypatch, tmp_path):
    """Parallel regression sentinel for the sdw_info formatter at
    dolby_to_easyeffects.py:837. If get_soundwire_ids ever grows a third
    tuple field, this test catches the same arity-bug class on the SDW
    side before it ships.
    """
    monkeypatch.setattr(dolby_to_easyeffects, "get_hda_codec_ids", lambda: [])
    monkeypatch.setattr(
        dolby_to_easyeffects, "get_soundwire_ids", lambda: [("025D", "1318")]
    )
    monkeypatch.setattr(
        dolby_to_easyeffects, "get_pci_audio_subsystem", lambda: ("17AA", "2339")
    )
    monkeypatch.setattr(dolby_to_easyeffects, "_resolve_driver_store", lambda _p: tmp_path)
    with pytest.raises(FileNotFoundError, match=r"man=025D part=1318"):
        find_tuning_xml(tmp_path)


# --- find_tuning_xml: multi-candidate version selection (R8) ---
# When several XMLs match the hardware, find_tuning_xml parses each once and
# keeps the highest tuning_version. A candidate whose version is malformed
# (bad/missing) must sort last AND list without crashing the display loop.


def _write_hda_candidate(directory, version_attr):
    """Create a DEV_*_SUBSYS_* XML matching the synthetic HDA codec below.

    version_attr is the raw `value` attribute string for <tuning_version>,
    or None to omit the attribute entirely (a malformed candidate).
    """
    attr = "" if version_attr is None else f' value="{version_attr}"'
    name = f"DEV_0287_SUBSYS_17AA22E6_v{version_attr or 'none'}.xml"
    path = directory / name
    path.write_text(f"<tuning><tuning_version{attr}/></tuning>")
    return path


def _patch_single_hda_match(monkeypatch, driver_store):
    """Wire find_tuning_xml so a single HDA codec drives candidate matching
    against XMLs in driver_store."""
    monkeypatch.setattr(
        dolby_to_easyeffects,
        "get_hda_codec_ids",
        lambda: [("10EC0287", "17AA22E6", "Realtek ALC287")],
    )
    monkeypatch.setattr(dolby_to_easyeffects, "get_soundwire_ids", lambda: [])
    monkeypatch.setattr(
        dolby_to_easyeffects, "_resolve_driver_store", lambda _p: driver_store
    )


def test_find_tuning_xml_selects_highest_version(monkeypatch, tmp_path):
    """Several matching XMLs → the highest tuning_version is returned."""
    _patch_single_hda_match(monkeypatch, tmp_path)
    _write_hda_candidate(tmp_path, "3")
    winner = _write_hda_candidate(tmp_path, "12")
    _write_hda_candidate(tmp_path, "7")
    assert find_tuning_xml(tmp_path) == winner


def test_find_tuning_xml_malformed_version_does_not_crash(monkeypatch, tmp_path):
    """A malformed-version candidate (missing `value`) must not raise in the
    display loop and must still lose to a valid higher-version candidate.
    """
    _patch_single_hda_match(monkeypatch, tmp_path)
    _write_hda_candidate(tmp_path, None)  # missing value → sorts last
    winner = _write_hda_candidate(tmp_path, "9")
    # Should not raise (display path is now robust to the bad version).
    assert find_tuning_xml(tmp_path) == winner


def test_autoprobe_raises_when_no_candidates_anywhere(monkeypatch, tmp_path):
    """No NTFS mounts and an empty CWD → FileNotFoundError at
    dolby_to_easyeffects.py:711-727. Pins the "no candidates" diagnostic
    text and the FileNotFoundError type.
    """
    monkeypatch.setattr(dolby_to_easyeffects, "_ntfs_family_mountpoints", lambda: [])
    monkeypatch.setattr(
        dolby_to_easyeffects, "_walk_for_dolby_xml_dirs", lambda _root: []
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(
        FileNotFoundError, match=r"Auto-detection failed.*no NTFS-family"
    ):
        autoprobe_dolby_source()


def test_autoprobe_raises_when_multiple_candidates_no_hardware_match(monkeypatch, tmp_path):
    """Two mount candidates, neither matching this machine's hardware →
    FileNotFoundError at dolby_to_easyeffects.py:732-746. Exercises the
    `.join(f"  - {p}" for p in candidates)` listing formatter on a
    non-trivial list, and pins that both candidate paths appear in the
    user-visible message.
    """
    mount_a = tmp_path / "win_a"
    mount_b = tmp_path / "win_b"
    (mount_a / "dax3_ext_foo.inf_001").mkdir(parents=True)
    (mount_b / "dax3_ext_bar.inf_002").mkdir(parents=True)
    monkeypatch.setattr(
        dolby_to_easyeffects,
        "_ntfs_family_mountpoints",
        lambda: [mount_a, mount_b],
    )
    monkeypatch.setattr(dolby_to_easyeffects, "_resolve_driver_store", lambda p: p)
    monkeypatch.setattr(dolby_to_easyeffects, "_detect_expected_subsys_ids", set)
    with pytest.raises(FileNotFoundError, match=r"none of which match") as excinfo:
        autoprobe_dolby_source()
    msg = str(excinfo.value)
    assert str(mount_a) in msg
    assert str(mount_b) in msg


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
    # Lenovo IdeaPad text-vendor SUBSYS — see issue #4 (taprobane99).
    "AUCD_DEV_0C29_SUBSYS_IDEA4002_ADCM_SUBSYS_IDEA4002.xml",
    "AUCD_DEV_0C29_SUBSYS_idea4002_ADCM_SUBSYS_idea4002.xml",  # case-insensitive
])
def test_dolby_filename_regex_matches_dax3_filenames(filename):
    assert DOLBY_FILENAME_RE.search(filename) is not None


@pytest.mark.parametrize("filename", [
    "settings.xml",                        # no SUBSYS_ token
    "DEV_0287_SUBSYS_17AA22E6.txt",        # not .xml
    "SUBSYS_SHORT.xml",                    # 5 chars, fails 8-char width
    "SUBSYS_17AA22E6_NO_DOT_XML",          # no .xml
    "DEV_0287.xml",                        # no SUBSYS_ token
    "SUBSYS_BAD-CHAR.xml",                 # non-alphanumeric in the 8-char window
])
def test_dolby_filename_regex_rejects_non_dax3(filename):
    assert DOLBY_FILENAME_RE.search(filename) is None
