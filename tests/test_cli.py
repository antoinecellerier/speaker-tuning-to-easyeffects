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
    ENABLEABLE_FILTERS,
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
    write_synthetic_tuning_xml,
)


SCRIPT = Path(__file__).resolve().parent.parent / "dolby_to_easyeffects.py"


# --- --disable: each name should drop its plugin/filter from the preset ---

def _full_inputs():
    """A complete plugin set (PEQ with all relevant types, MBC,
    regulator, dialog, leveler) so every --disable target has
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
        "high-shelf", "lo-pass",
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


def test_enable_choices_match_documented_set():
    """Mirror of the --disable sanity check: --enable's argparse choices
    ARE ENABLEABLE_FILTERS."""
    assert set(ENABLEABLE_FILTERS) == {"autogain", "coupled-bands"}


def test_enable_autogain_activates_leveler():
    """--enable autogain (make_preset enabled={"autogain"}) activates
    the HDA leveler; default builds keep it bypassed with the -50 dB
    gate stored so manual GUI enabling gets the issue #25 crackle fix.
    A bypassed emission is marked in `emitted` (the --enable hint);
    an active one isn't."""
    preset, emitted = _build()
    assert preset["output"]["autogain#0"]["bypass"] is True
    assert preset["output"]["autogain#0"]["silence-threshold"] == -50.0
    assert "autogain" in emitted
    preset_on, emitted_on = _build(enabled={"autogain"})
    assert preset_on["output"]["autogain#0"]["bypass"] is False
    assert preset_on["output"]["autogain#0"]["silence-threshold"] == -50.0
    assert "autogain" not in emitted_on
    # The marker main() uses to tell "the flag worked" from "the XML's
    # leveler is disabled, so --enable autogain could not do anything".
    assert "autogain-active" in emitted_on


def test_enable_coupled_bands_hint_and_active_marker():
    """--enable coupled-bands mirrors the autogain emitted contract:
    an eligible-but-inactive regulator (0 dB zone marked non-isolated,
    flag off) lands "coupled-bands" in emitted for the end-of-run hint;
    with the flag on, the zone activates at 0 dBFS and the
    "coupled-bands-active" marker replaces it."""
    eligible = synthetic_regulator([-6.0] * 10 + [0.0] * 10,
                                   isolated_band=[1] * 10 + [0] * 10)
    preset, emitted = _build(regulator=eligible)
    reg = preset["output"]["multiband_compressor#1"]
    assert not any(reg[f"band{i}"]["compressor-enable"]
                   and reg[f"band{i}"]["attack-threshold"] >= 0
                   for i in range(8))
    assert "coupled-bands" in emitted
    assert "coupled-bands-active" not in emitted

    preset_on, emitted_on = _build(regulator=eligible,
                                   enabled={"coupled-bands"})
    reg_on = preset_on["output"]["multiband_compressor#1"]
    assert any(reg_on[f"band{i}"]["compressor-enable"]
               and reg_on[f"band{i}"]["attack-threshold"] == 0.0
               for i in range(8))
    assert "coupled-bands-active" in emitted_on
    assert "coupled-bands" not in emitted_on

    # No isolated data -> flag requested but nothing to couple: neither
    # the hint nor the active marker may appear (main() warns off this).
    plain = synthetic_regulator([-6.0] * 10 + [0.0] * 10)
    _, emitted_plain = _build(regulator=plain, enabled={"coupled-bands"})
    assert "coupled-bands" not in emitted_plain
    assert "coupled-bands-active" not in emitted_plain


def test_experimental_markers_cover_coupled_bands_activation():
    """The active marker doubles as an EXPERIMENTAL_MARKERS key, so an
    engaged --enable coupled-bands run triggers the end-of-run
    "please report back" prompt — the flag ships unheard on device and
    the feedback ask is the validation path. Locks the full marker set
    so drift in either direction is deliberate."""
    from dolby_to_easyeffects import EXPERIMENTAL_MARKERS
    assert set(EXPERIMENTAL_MARKERS) == {
        "high-shelf", "lo-pass", "mbc-1band", "coupled-bands-active",
    }
    eligible = synthetic_regulator([-6.0] * 10 + [0.0] * 10,
                                   isolated_band=[1] * 10 + [0] * 10)
    _, emitted = _build(regulator=eligible, enabled={"coupled-bands"})
    # (_full_inputs ships high-shelf/lo-pass PEQ types, so those markers
    # fire alongside — membership, not equality.)
    assert "coupled-bands-active" in emitted & set(EXPERIMENTAL_MARKERS)
    _, emitted_off = _build(regulator=eligible)
    assert "coupled-bands-active" not in emitted_off
    assert "autogain-active" not in emitted


def test_enable_autogain_marker_absent_when_leveler_disabled():
    """An XML whose volume leveler is disabled emits no autogain stage at
    all, so --enable can't activate anything — main() warns off the
    missing marker rather than leaving the flag silently inert."""
    off = {"enable": False, "amount": 5, "out_target": -16.0}
    preset, emitted = _build(enabled={"autogain"}, vol_leveler=off)
    assert "autogain#0" not in preset["output"]
    assert "autogain-active" not in emitted
    assert "autogain" not in emitted


def test_no_stereo_widener_ever_emitted():
    """The surround→stereo_tools widening was removed (design-notes entry
    2 — DAX applies no widening on 2-ch content). No invocation, with or
    without --disable, should ever produce a stereo_tools stage, and
    `stereo` is no longer a --disable choice.
    """
    preset, emitted = _build()
    assert "stereo_tools#0" not in preset["output"]
    assert "stereo" not in emitted
    assert "stereo" not in DISABLEABLE_FILTERS


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


# volmax-boost has two routing slots: regulator input-gain (default since
# issue #23 — applied pre-band-limiting so the per-band compression tames the
# boost; not a Dolby-documented topology) or limiter input-gain (fallback,
# when the regulator is disabled or absent). --disable volmax must zero both.
# --volmax-slot re-routes the regulator path (input-gain default vs output-gain).

def test_volmax_lands_on_regulator_when_present():
    """Sanity: with regulator enabled and volmax NOT disabled, the boost
    lands on regulator input-gain (the issue #23 default) and the limiter
    stays at 0.
    """
    preset, emitted = _build(volmax_boost=3.0)
    reg = preset["output"]["multiband_compressor#1"]
    assert reg["input-gain"] == pytest.approx(3.0)
    assert reg["output-gain"] == 0.0
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


# --volmax-slot (issue #23): input-gain is the default — the boost rides the
# regulator input so the per-band downward compression tames it before the
# brickwall. output-gain opts back into the pre-#23 post-band placement (kept
# for A/B). The flag only touches the regulator path, never the limiter
# fallback. (Default flipped to input-gain after a 2nd, aggressive-regulator
# device — X13, issue #23 — confirmed it stays clean and loud.)

def test_volmax_slot_default_is_input_gain():
    """Omitting volmax_slot routes the boost to the regulator input-gain
    (the issue #23 default), leaving output-gain at 0 and the limiter at 0,
    and matches an explicit input-gain — guards against the default regressing.
    """
    default, _ = _build(volmax_boost=3.0)
    explicit, _ = _build(volmax_boost=3.0, volmax_slot="input-gain")
    reg = "multiband_compressor#1"
    assert default["output"][reg]["input-gain"] == pytest.approx(3.0)
    assert default["output"][reg]["output-gain"] == 0.0
    assert default["output"]["limiter#0"]["input-gain"] == 0.0
    assert explicit["output"][reg] == default["output"][reg]


def test_volmax_slot_output_gain_opts_into_post_band_placement():
    """--volmax-slot output-gain restores the pre-#23 placement: the boost
    rides the regulator output-gain (post-band-limiting) with input-gain at 0.
    Kept as the A/B opt-out, so the routing must stay reachable.
    """
    preset, emitted = _build(volmax_boost=3.0, volmax_slot="output-gain")
    reg = preset["output"]["multiband_compressor#1"]
    assert reg["output-gain"] == pytest.approx(3.0)
    assert reg["input-gain"] == 0.0
    assert preset["output"]["limiter#0"]["input-gain"] == 0.0
    assert "volmax" in emitted


def test_volmax_slot_does_not_affect_limiter_fallback():
    """When the regulator is absent, the boost falls back to limiter
    input-gain regardless of volmax_slot — the flag only re-routes the
    regulator path.
    """
    preset, _ = _build(disabled={"regulator"}, volmax_boost=3.0,
                       volmax_slot="input-gain")
    assert "multiband_compressor#1" not in preset["output"]
    assert preset["output"]["limiter#0"]["input-gain"] == pytest.approx(3.0)


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


def test_doctor_runs_without_xml_and_exits_zero(tmp_path):
    """`--doctor` is an entry-point mode: it must run with no XML, degrade
    gracefully when the output/irs dirs are empty, and exit 0."""
    out = tmp_path / "out"
    irs = tmp_path / "irs"
    out.mkdir()
    irs.mkdir()
    result = _run_script("--doctor", "--no-color",
                         "--output-dir", str(out), "--irs-dir", str(irs))
    assert result.returncode == 0
    assert "EasyEffects doctor" in result.stdout
    # No presets in the empty dir → a WARN line, but never a crash.
    assert "no presets found" in result.stdout.lower()


def test_diagnose_alias_runs(tmp_path):
    """`--diagnose` is an alias for `--doctor` (same dest)."""
    out = tmp_path / "out"
    irs = tmp_path / "irs"
    out.mkdir()
    irs.mkdir()
    result = _run_script("--diagnose", "--no-color",
                         "--output-dir", str(out), "--irs-dir", str(irs))
    assert result.returncode == 0
    assert "EasyEffects doctor" in result.stdout


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


def test_skip_ee_check_gates_environment_warning(monkeypatch, tmp_path, capsys):
    """`--skip-ee-check` must suppress the end-of-run warn_ee_environment
    probe (and only that) — dolby_to_pipewire.py relies on it to keep the
    EasyEffects install hint out of PipeWire-only runs.
    """
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    calls = []
    monkeypatch.setattr(dolby_to_easyeffects, "warn_ee_environment",
                        lambda args: calls.append(args))
    base = [str(xml),
            "--output-dir", str(tmp_path / "out"),
            "--irs-dir", str(tmp_path / "irs")]
    dolby_to_easyeffects.main(base)
    assert len(calls) == 1
    dolby_to_easyeffects.main(base + ["--skip-ee-check"])
    assert len(calls) == 1


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


# --- find_tuning_xml: PCI-keyed (Apple Boot Camp / Intel Mac) match — issue #21 ---
# Apple's Boot Camp DAX tunings are named by the audio function's PCI subsystem
# in device-first order, e.g. PCI_DEV_1803_SUBSYS_1880106B_PCI_SUBSYS_72708086
# (106B = Apple). That token has the opposite byte order from an HDA codec
# subsystem, so it must match against the PCI subsystem id, not /proc/asound's
# vendor-first codec subsystem. A dummy non-matching HDA codec is supplied so the
# "no audio hardware" guard passes (mirrors a real Mac, which still enumerates a
# codec — just not one whose subsystem matches the filename).

_MAC_XML_NAME = "PCI_DEV_1803_SUBSYS_1880106B_PCI_SUBSYS_72708086.xml"


def _patch_mac_pci_match(monkeypatch, driver_store, pci_subsys):
    monkeypatch.setattr(
        dolby_to_easyeffects,
        "get_hda_codec_ids",
        lambda: [("8086FFFF", "DEADBEEF", "Dummy non-matching codec")],
    )
    monkeypatch.setattr(dolby_to_easyeffects, "get_soundwire_ids", lambda: [])
    monkeypatch.setattr(
        dolby_to_easyeffects, "get_pci_audio_subsystem", lambda: pci_subsys
    )
    monkeypatch.setattr(
        dolby_to_easyeffects, "_resolve_driver_store", lambda _p: driver_store
    )


def test_find_tuning_xml_matches_apple_pci_subsystem(monkeypatch, tmp_path):
    """PCI subsystem 106B:1880 → token 1880106B → matches the Mac filename."""
    xml = tmp_path / _MAC_XML_NAME
    xml.write_text("<tuning><tuning_version value='1'/></tuning>")
    _patch_mac_pci_match(monkeypatch, tmp_path, ("106B", "1880"))
    assert find_tuning_xml(tmp_path) == xml


def test_find_tuning_xml_apple_pci_subsystem_mismatch_does_not_match(monkeypatch, tmp_path):
    """A different PCI subsystem must not match the Mac filename (no false
    positive) — neither the HDA codec subsystem nor the PCI token line up."""
    (tmp_path / _MAC_XML_NAME).write_text("<tuning/>")
    _patch_mac_pci_match(monkeypatch, tmp_path, ("8086", "1234"))
    with pytest.raises(FileNotFoundError, match="No matching DAX3 tuning XML"):
        find_tuning_xml(tmp_path)


# --- find_tuning_xml: SoundWire match keys on PCI subsystem, not FUNC — issue #26 ---
# Samsung Galaxy Book6 Ultra (Cirrus cs35l56): the tuning filename is
# SOUNDWIRE_SDCAFUNCTION_10_MAN_01FA_FUNC_3556_SUBSYS_CA0A144D.xml, but sysfs
# reports SoundWire parts 3557 (amps) / 4245 (codec) — neither equals Dolby's
# FUNC_3556 (a device id, per the XML's own security-key). Matching must key on
# the PCI subsystem (CA0A144D, unique per SKU) + manufacturer present, NOT on
# (man, part). The only HDA codec on these systems is the Intel HDMI codec.

_GB6_XML_NAME = "SOUNDWIRE_SDCAFUNCTION_10_MAN_01FA_FUNC_3556_SUBSYS_CA0A144D.xml"


def _patch_soundwire_match(monkeypatch, driver_store, sdw_devices, pci_subsys):
    monkeypatch.setattr(
        dolby_to_easyeffects,
        "get_hda_codec_ids",
        lambda: [("80862822", "80860101", "Intel HDMI")],  # HDMI only, no match
    )
    monkeypatch.setattr(dolby_to_easyeffects, "get_soundwire_ids", lambda: sdw_devices)
    monkeypatch.setattr(
        dolby_to_easyeffects, "get_pci_audio_subsystem", lambda: pci_subsys
    )
    monkeypatch.setattr(
        dolby_to_easyeffects, "_resolve_driver_store", lambda _p: driver_store
    )


def test_find_tuning_xml_soundwire_matches_despite_func_not_a_part_id(monkeypatch, tmp_path):
    """Regression lock for #26: FUNC_3556 ∉ detected parts {3557, 4245}, but the
    PCI subsystem CA0A144D and manufacturer 01FA match → the file is selected."""
    xml = tmp_path / _GB6_XML_NAME
    xml.write_text("<tuning><tuning_version value='2'/></tuning>")
    _patch_soundwire_match(
        monkeypatch, tmp_path, [("01FA", "3557"), ("01FA", "4245")], ("144D", "CA0A")
    )
    assert find_tuning_xml(tmp_path) == xml


def test_find_tuning_xml_soundwire_wrong_pci_does_not_match(monkeypatch, tmp_path):
    """A sibling SKU's tuning (different PCI subsystem) must not match — the PCI
    subsystem is the per-device key, so F020144D ≠ CA0A144D rejects it."""
    (tmp_path / "SOUNDWIRE_MAN_01FA_FUNC_3556_SUBSYS_F020144D.xml").write_text("<x/>")
    _patch_soundwire_match(
        monkeypatch, tmp_path, [("01FA", "3557")], ("144D", "CA0A")
    )
    with pytest.raises(FileNotFoundError, match="No matching DAX3 tuning XML"):
        find_tuning_xml(tmp_path)


def test_find_tuning_xml_soundwire_wrong_manufacturer_does_not_match(monkeypatch, tmp_path):
    """The manufacturer guard still bites: a Qualcomm (025D) filename whose
    SUBSYS happens to equal our PCI token must not match a Cirrus-only machine."""
    (tmp_path / "SOUNDWIRE_MAN_025D_FUNC_1318_SUBSYS_CA0A144D.xml").write_text("<x/>")
    _patch_soundwire_match(
        monkeypatch, tmp_path, [("01FA", "3557")], ("144D", "CA0A")
    )
    with pytest.raises(FileNotFoundError, match="No matching DAX3 tuning XML"):
        find_tuning_xml(tmp_path)


def test_find_tuning_xml_soundwire_func_disambiguates_same_subsys(monkeypatch, tmp_path):
    """Regression guard for the relaxed match: some Lenovo SKUs ship two tunings
    sharing MAN+SUBSYS but differing in FUNC (e.g. SUBSYS_383917AA: FUNC_0721 vs
    FUNC_1320). The exact (man, part) tier must still pick the FUNC that equals
    the detected part — NOT fall through to highest-version selection, which here
    would wrongly pick the FUNC_0721 file. Locks that FUNC stays a preferred key
    where it equals a part id (the Qualcomm corpus), not dropped."""
    right = tmp_path / "SOUNDWIRE_MAN_025D_FUNC_1320_SUBSYS_383917AA.xml"
    right.write_text("<tuning><tuning_version value='5'/></tuning>")
    wrong = tmp_path / "SOUNDWIRE_MAN_025D_FUNC_0721_SUBSYS_383917AA.xml"
    wrong.write_text("<tuning><tuning_version value='99'/></tuning>")  # higher version
    _patch_soundwire_match(
        monkeypatch, tmp_path, [("025D", "1320")], ("17AA", "3839")
    )
    assert find_tuning_xml(tmp_path) == right


# --- find_tuning_xml: HDA DEV token disambiguates a shared subsystem (#33) ---
# Lenovo reuses codec subsystem ids across different Realtek codecs: the
# IdeaPad Pro 5 14APH8's ALC287 (SUBSYS 17AA38C5) shares its subsystem with an
# ALC257 SKU, and both tunings ship in the same driver store. The (DEV, SUBSYS)
# pair is the strong key; subsystem-only stays available as a fallback tier.


def _patch_idea_pad_pro5(monkeypatch, driver_store):
    monkeypatch.setattr(
        dolby_to_easyeffects,
        "get_hda_codec_ids",
        lambda: [("10EC0287", "17AA38C5", "Realtek ALC287")],
    )
    monkeypatch.setattr(dolby_to_easyeffects, "get_soundwire_ids", lambda: [])
    monkeypatch.setattr(
        dolby_to_easyeffects, "_resolve_driver_store", lambda _p: driver_store
    )


def test_find_tuning_xml_hda_dev_token_beats_tuning_version(monkeypatch, tmp_path):
    """Regression lock for #33: the ALC257 tuning shares SUBSYS_17AA38C5 and has
    the higher tuning_version, but the codec is an ALC287 — the DEV_0287 file
    must win. Before the fix, version tiebreak picked the wrong codec's tuning."""
    wrong = tmp_path / "DEV_0257_SUBSYS_17AA38C5_PCI_SUBSYS_382C17AA.xml"
    wrong.write_text("<tuning><tuning_version value='11'/></tuning>")
    right = tmp_path / "DEV_0287_SUBSYS_17AA38C5_PCI_SUBSYS_388117AA.xml"
    right.write_text("<tuning><tuning_version value='8'/></tuning>")
    _patch_idea_pad_pro5(monkeypatch, tmp_path)
    assert find_tuning_xml(tmp_path) == right


def test_find_tuning_xml_hda_subsys_only_fallback_still_matches(monkeypatch, tmp_path):
    """A subsystem match whose DEV token equals no detected codec device id must
    still be selected when nothing better exists (DEV preferred, not required)."""
    xml = tmp_path / "DEV_0257_SUBSYS_17AA38C5_PCI_SUBSYS_382C17AA.xml"
    xml.write_text("<tuning><tuning_version value='11'/></tuning>")
    _patch_idea_pad_pro5(monkeypatch, tmp_path)
    assert find_tuning_xml(tmp_path) == xml


# --- get_pci_audio_subsystem: prefer the analog controller over GPU HDMI (#33) ---
# On AMD dual-controller laptops card0 is the GPU audio function, so the old
# first-card walk reported the GPU's PCI subsystem — not the id kernel quirks
# and Dolby PCI-keyed filenames use (issue #33 diagnostics showed 17AA:3823
# where the analog controller was a different device; same pattern on #30).


def _make_sound_card(root, name, codec_names, subsys):
    """Build a fake /sys/class/sound card + /proc/asound codec files."""
    dev = root / "sys" / name / "device"
    dev.mkdir(parents=True)
    vendor, device = subsys
    (dev / "subsystem_vendor").write_text(f"0x{vendor.lower()}\n")
    (dev / "subsystem_device").write_text(f"0x{device.lower()}\n")
    proc_card = root / "proc" / name
    proc_card.mkdir(parents=True)
    for i, codec_name in enumerate(codec_names):
        (proc_card / f"codec#{i}").write_text(f"Codec: {codec_name}\n")


def _fake_pci_probe_roots(root):
    return dict(
        sound_class=root / "sys",
        proc_asound=root / "proc",
        sdw_bus=root / "no-soundwire",
    )


def test_get_pci_audio_subsystem_prefers_analog_over_hdmi(tmp_path):
    """card0 = GPU HDMI function, card1 = analog codec → the analog
    controller's subsystem must win despite sorting second."""
    _make_sound_card(tmp_path, "card0", ["ATI R6xx HDMI"], ("17aa", "3823"))
    _make_sound_card(tmp_path, "card1", ["Realtek ALC287"], ("17aa", "3881"))
    assert dolby_to_easyeffects.get_pci_audio_subsystem(
        **_fake_pci_probe_roots(tmp_path)
    ) == ("17AA", "3881")


def test_get_pci_audio_subsystem_hdmi_only_still_returns(tmp_path):
    """A machine with only a GPU HDMI card keeps the old behaviour — better a
    GPU subsystem than none."""
    _make_sound_card(tmp_path, "card0", ["ATI R6xx HDMI"], ("17aa", "3823"))
    assert dolby_to_easyeffects.get_pci_audio_subsystem(
        **_fake_pci_probe_roots(tmp_path)
    ) == ("17AA", "3823")


def test_get_pci_audio_subsystem_rank_order(tmp_path):
    """Full ranking: analog codec beats codec-less (e.g. USB) beats HDMI-only,
    regardless of card index order."""
    _make_sound_card(tmp_path, "card0", ["ATI R6xx HDMI"], ("17aa", "3823"))
    _make_sound_card(tmp_path, "card1", [], ("1912", "0014"))
    _make_sound_card(tmp_path, "card2", ["Realtek ALC287"], ("17aa", "3881"))
    assert dolby_to_easyeffects.get_pci_audio_subsystem(
        **_fake_pci_probe_roots(tmp_path)
    ) == ("17AA", "3881")
    import shutil

    shutil.rmtree(tmp_path / "sys" / "card2")
    shutil.rmtree(tmp_path / "proc" / "card2")
    assert dolby_to_easyeffects.get_pci_audio_subsystem(
        **_fake_pci_probe_roots(tmp_path)
    ) == ("1912", "0014")


# --- find_tuning_xml: content-validated best-guess fallback (issue #26) ---
# When no filename matches, parse each candidate's <endpoint type> and
# <security-key> and surface internal_speaker tunings whose manufacturer is
# present, so a user on an unmapped convention can self-unblock.


def _write_speaker_xml(directory, filename, man, subsys, endpoint_type="internal_speaker"):
    """Write a minimal DAX3 XML with a security-key encoding MAN/FUNC/SUBSYS.

    Pass man="" to emit an empty security-key (the generic untuned fallback).
    """
    if man:
        key = f"SOUNDWIRE\\SDCA_FUNCTION_10&amp;MAN_{man}&amp;FUNC_3556&amp;SUBSYS_{subsys}"
    else:
        key = ""
    path = directory / filename
    path.write_text(
        "<device_data><tuning_version value='1'/>"
        f"<setting><security-key value=\"{key}\"/></setting>"
        f"<endpoint type=\"{endpoint_type}\"/></device_data>"
    )
    return path


def _no_strict_match_dir(tmp_path):
    """A driver store with two Cirrus internal-speaker tunings (neither's
    SUBSYS equal to the detected PCI token), a headphone tuning, and a generic
    empty-security-key fallback. Returns the two device-tuning paths."""
    a = _write_speaker_xml(tmp_path, _GB6_XML_NAME, "01FA", "CA0A144D")
    b = _write_speaker_xml(
        tmp_path, "SOUNDWIRE_MAN_01FA_FUNC_3556_SUBSYS_F020144D.xml", "01FA", "F020144D"
    )
    _write_speaker_xml(
        tmp_path, "Headphone_Default.xml", "01FA", "CA0A144D", endpoint_type="headphone"
    )
    _write_speaker_xml(tmp_path, "Speaker_Default_Atmos3.10.xml", "", "")
    return a, b


def test_best_guess_lists_speaker_candidates_excludes_headphone_and_generic(monkeypatch, tmp_path):
    """No exact match (PCI token DEAD144D matches no file, incl. no security-key
    PCI match): the error lists both manufacturer-validated internal_speaker
    tunings as positional XML paths, and lists neither the headphone tuning nor
    the generic empty-security-key fallback."""
    a, b = _no_strict_match_dir(tmp_path)
    _patch_soundwire_match(monkeypatch, tmp_path, [("01FA", "3557")], ("144D", "DEAD"))
    with pytest.raises(FileNotFoundError) as exc:
        find_tuning_xml(tmp_path)
    msg = str(exc.value)
    assert str(a) in msg and str(b) in msg
    assert "--xml" not in msg  # hint is the positional path, not a flag
    assert "Speaker_Default_Atmos3.10.xml" not in msg  # generic skipped, not listed
    assert "Headphone_Default.xml" not in msg  # headphone excluded entirely


def test_find_tuning_xml_matches_security_key_pci_when_filename_unmapped(monkeypatch, tmp_path):
    """Authoritative content match: a tuning whose filename carries no
    recognizable token but whose <security-key> PCI subsystem equals this
    machine's is auto-selected — even without --best-guess (locks the SUBSYS
    use the review flagged as discarded)."""
    xml = _write_speaker_xml(tmp_path, "cirrus_speaker_tuning.xml", "01FA", "CA0A144D")
    # a sibling SKU whose security-key PCI subsystem does NOT match → ignored
    _write_speaker_xml(tmp_path, "cirrus_other_sku.xml", "01FA", "F020144D")
    _patch_soundwire_match(monkeypatch, tmp_path, [("01FA", "3557")], ("144D", "CA0A"))
    assert find_tuning_xml(tmp_path) == xml  # no best_guess needed


def test_best_guess_autoselects_single_candidate(monkeypatch, tmp_path):
    """Exactly one manufacturer-validated speaker tuning + --best-guess →
    auto-select it (warned, unverified)."""
    xml = _write_speaker_xml(tmp_path, _GB6_XML_NAME, "01FA", "CA0A144D")
    _write_speaker_xml(tmp_path, "Speaker_Default_Atmos3.10.xml", "", "")  # generic, ignored
    _patch_soundwire_match(monkeypatch, tmp_path, [("01FA", "3557")], ("144D", "DEAD"))
    assert find_tuning_xml(tmp_path, best_guess=True) == xml


def test_best_guess_multiple_candidates_does_not_autoselect(monkeypatch, tmp_path):
    """Several validated candidates + --best-guess → refuse to guess; list them."""
    _no_strict_match_dir(tmp_path)
    _patch_soundwire_match(monkeypatch, tmp_path, [("01FA", "3557")], ("144D", "DEAD"))
    with pytest.raises(FileNotFoundError, match="will not guess"):
        find_tuning_xml(tmp_path, best_guess=True)


def test_content_scan_skips_unreadable_xml_without_crashing(monkeypatch, tmp_path):
    """The no-match content scan opens every *.xml; an unreadable or
    directory-named one must be skipped, not raise an uncaught OSError that
    masks the clean 'no matching tuning' error."""
    good = _write_speaker_xml(tmp_path, _GB6_XML_NAME, "01FA", "CA0A144D")
    (tmp_path / "a_directory.xml").mkdir()  # ET.parse() → IsADirectoryError (OSError)
    _patch_soundwire_match(monkeypatch, tmp_path, [("01FA", "3557")], ("144D", "DEAD"))
    with pytest.raises(FileNotFoundError) as exc:  # not IsADirectoryError
        find_tuning_xml(tmp_path)
    assert str(good) in str(exc.value)


def test_best_guess_off_by_default_stays_strict(monkeypatch, tmp_path):
    """Default (best_guess=False) never auto-selects a manufacturer-only guess,
    even with a single candidate — it raises and lists it for explicit
    selection. (Uses a non-PCI-matching SUBSYS so the authoritative
    security-key path doesn't fire.)"""
    xml = _write_speaker_xml(tmp_path, _GB6_XML_NAME, "01FA", "CA0A144D")
    _patch_soundwire_match(monkeypatch, tmp_path, [("01FA", "3557")], ("144D", "DEAD"))
    with pytest.raises(FileNotFoundError) as exc:
        find_tuning_xml(tmp_path)
    assert str(xml) in str(exc.value)  # listed as the positional path to use


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
    # Apple Boot Camp / Intel Mac PCI-keyed name — see issue #21 (taprobane99).
    "PCI_DEV_1803_SUBSYS_1880106B_PCI_SUBSYS_72708086.xml",
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


# --- Device-report issue form (.github/ISSUE_TEMPLATE/device-report.yml) ---
# The end-of-run CTA points users at a GitHub issue form so "works on my
# hardware" reports arrive in a consistent shape (device + --speaker-info
# block). Two cheap locks: the CTA constant must target the form, and the
# form the URL names must exist, parse, and still require the two fields a
# report is useless without.

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_report_form_url_targets_device_report_template():
    assert "issues/new?template=device-report.yml" in dolby_to_easyeffects._REPORT_FORM_URL


def test_device_report_form_is_valid():
    yaml = pytest.importorskip("yaml")

    # Derive the filename from the CTA URL so a rename can't silently split
    # the constant from the file it names.
    template = dolby_to_easyeffects._REPORT_FORM_URL.split("template=", 1)[1]
    form_path = _REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / template
    assert form_path.is_file(), f"{form_path} (named by _REPORT_FORM_URL) is missing"

    form = yaml.safe_load(form_path.read_text())
    assert form.get("name")
    body = form.get("body")
    assert isinstance(body, list) and body

    required = {
        f["id"] for f in body
        if isinstance(f, dict) and f.get("validations", {}).get("required")
    }
    assert {"device", "speaker-info"} <= required


def test_speaker_info_output_is_version_stamped(monkeypatch, capsys):
    """`--speaker-info` prefixes a version line: users paste that block into
    the issue form, so the maintainer can tell which build was tested."""
    monkeypatch.setattr(dolby_to_easyeffects, "_CONSOLE", None)
    monkeypatch.setattr(dolby_to_easyeffects, "get_version", lambda: "vTEST-42")
    monkeypatch.setattr(dolby_to_easyeffects, "_gather_speaker_info", lambda: None)
    monkeypatch.setattr(dolby_to_easyeffects, "_print_speaker_info", lambda info: None)

    dolby_to_easyeffects.report_speaker_info()

    out = capsys.readouterr().out
    assert "speaker-tuning-to-easyeffects vTEST-42" in out


@pytest.mark.parametrize("content,expected", [
    ('PRETTY_NAME="Fedora Linux 44 (Workstation Edition)"\nID=fedora\n',
     "Fedora Linux 44 (Workstation Edition)"),
    ('NAME="Arch Linux"\nPRETTY_NAME=Arch Linux\n', "Arch Linux"),  # unquoted
    ('ID=void\n', ""),                                              # no PRETTY_NAME key
])
def test_get_distro_pretty_name_parses(tmp_path, content, expected):
    p = tmp_path / "os-release"
    p.write_text(content)
    assert dolby_to_easyeffects.get_distro_pretty_name(p) == expected


def test_get_distro_pretty_name_missing_file(tmp_path):
    assert dolby_to_easyeffects.get_distro_pretty_name(tmp_path / "nope") == ""
