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

import re
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
        "volmax", "mbc", "regulator", "autogain", "bass-enhancer", "dialog",
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


def test_disable_autogain_drops_leveler():
    # SoundWire is the case that matters: there the leveler ships active
    # (bypass=False), and before this flag existed nothing could turn it off.
    preset, emitted = _build(disabled={"autogain"}, is_soundwire=True)
    assert "autogain#0" not in preset["output"]
    assert "autogain" not in emitted
    assert "autogain-active" not in emitted


def test_disable_autogain_menu_row_needs_the_active_marker(monkeypatch,
                                                          capsys):
    """The --disable menu offers autogain only where it actually runs.

    The bare "autogain" marker means "present but bypassed" and feeds the
    --enable menu; a --disable row keyed off it would offer to switch off a
    stage that is already off — so the row keys off "autogain-active".
    """
    import dolby_to_easyeffects
    monkeypatch.setattr(dolby_to_easyeffects, "_CONSOLE", None)

    dolby_to_easyeffects.print_troubleshooting(
        [], {"autogain-active": {"default"}})
    assert "--disable autogain" in capsys.readouterr().out

    dolby_to_easyeffects.print_troubleshooting([], {"autogain": {"default"}})
    out = capsys.readouterr().out
    assert "--disable autogain" not in out
    # The bypassed state still reaches the user, via the --enable menu.
    assert "--enable autogain" in out


def test_same_name_in_disable_and_enable_errors(tmp_path, monkeypatch,
                                                capsys):
    """A name in both directions is a contradiction — silently picking a
    winner would leave the user believing whichever flag they meant, so the
    run must stop before building anything."""
    monkeypatch.setattr(dolby_to_easyeffects, "_CONSOLE", None)
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    with pytest.raises(SystemExit) as exc:
        dolby_to_easyeffects.main([
            str(xml), "--dry-run", "--skip-ee-check",
            "--enable", "autogain", "--disable", "autogain"])
    assert exc.value.code == 2
    assert ("autogain given to both --disable and --enable"
            in capsys.readouterr().err)


def test_disable_menu_never_offers_to_revert_an_enable_flag(monkeypatch,
                                                            capsys):
    """A stage the user switched on with --enable gets no --disable row: the
    undo for a flag you typed is removing it, and offering the opposite flag
    would steer the next command straight into the both-directions error."""
    monkeypatch.setattr(dolby_to_easyeffects, "_CONSOLE", None)
    dolby_to_easyeffects.print_troubleshooting(
        [], {"autogain-active": {"default"}, "mbc": {"default"}},
        enabled_by_flag=frozenset({"autogain"}))
    out = capsys.readouterr().out
    assert "--disable autogain" not in out
    assert "--disable mbc" in out


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


def test_report_url_is_not_folded_by_the_console(capsys):
    """A link broken across lines can't be clicked or copied, which defeats
    the whole point of the ask.

    rich reflows at the console width unless told not to, and this URL is 103
    characters — so on an ordinary 80-column terminal the run's main call to
    action was being folded mid-string. It also meant the output differed
    depending on whether rich was installed, since the no-rich fallback never
    wraps. cprint pins soft_wrap; this holds it there.
    """
    url = dolby_to_easyeffects._REPORT_FORM_URL
    dolby_to_easyeffects.cprint("cta", f"  {url}")
    first = capsys.readouterr().out.splitlines()[0]
    assert url in first, "the URL must survive on a single line"


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


# --- Closing-block copy contract ---
# The end-of-run block is the one part of the output most people read, and
# most people run this script once — so it has to stay short enough to scan.
# Conciseness erodes one well-meaning entry at a time, so the contract in
# .claude/rules/user-messages.md gets traps rather than good intentions.
#
# Enumerating every raiser is the structural cost: table-driven ones walk
# themselves, the handful of literal sites are listed here.

def _every_finding():
    """One Finding per site that can raise one, for the contract checks.

    Every one is reached through the factory that defines it, so this list
    cannot drift from the wording the run actually prints.
    """
    import xml.etree.ElementTree as ET

    profile = ET.fromstring("""
        <profile type="dynamic">
          <tuning-cp>
            <peak-level value="-3"/>
            <ieq-bands-set preset="ieq_warm"/>
            <regulator-overdrive value="5"/>
            <regulator-relaxation-amount value="80"/>
            <dynamic_speaker_optimization_enable value="1"/>
            <advanced-speaker-virtualizer-rendering-config/>
          </tuning-cp>
        </profile>
    """)
    found = list(dolby_to_easyeffects.collect_unmodeled_features(profile))
    # Only the escalated strength here: the bypassed one shares this slug by
    # design (same site, two wordings) and carries no ask, so it has nothing
    # for these checks to bite on. tests/test_preset.py covers both.
    found.append(dolby_to_easyeffects._leveler_gap_finding(
        ["volume-leveler-compressor"], autogain_on=True))
    found.append(dolby_to_easyeffects._firmware_gate_finding())
    # Raised inside _report_parsed_profile / main(), which would need a whole
    # run to reach. Called through their factories rather than restated here:
    # this fixture used to carry its own copy of each sentence, which is two
    # definitions of one string and exactly the drift these checks exist to
    # stop.
    found += [
        dolby_to_easyeffects._profile_mismatch_finding("music", "dynamic"),
        dolby_to_easyeffects._profile_unknown_finding(),
        dolby_to_easyeffects._loudness_untamed_finding(),
        dolby_to_easyeffects._boost_unlimited_finding(12.0, 120),
        dolby_to_easyeffects._experimental_finding("type-3 high-shelf",
                                                   ["high-shelf"]),
    ]
    return [f for f in found if f is not None]


@pytest.mark.parametrize("finding", _every_finding(),
                         ids=lambda f: f.slug)
def test_ask_stays_one_short_sentence(finding, capsys, monkeypatch):
    """An entry that needs three lines is an explanation, and explanations
    belong at the detection site where they have context — not in the block
    a user skims once.

    Pinned to the narrow end of the wrap range rather than the runner's own
    terminal, so the budget means "fits on a small window" and the result
    doesn't change with whoever runs it.
    """
    if not finding.ask:
        return
    monkeypatch.setattr(dolby_to_easyeffects, "_wrap_width", lambda: 72)
    dolby_to_easyeffects._print_ask("cta", finding)
    rendered = capsys.readouterr().out.rstrip("\n").splitlines()
    assert len(rendered) <= 2, f"{finding.slug} renders {len(rendered)} lines"
    assert finding.ask.count(". ") == 0, f"{finding.slug} is multi-sentence"


@pytest.mark.parametrize("finding", _every_finding(), ids=lambda f: f.slug)
def test_findings_carry_no_link_and_no_empty_action(finding):
    """The block owns the single link, so nothing it renders may carry one —
    a URL inside wrapped prose gets folded and stops being clickable. And an
    entry with nothing to do carries no ask at all: saying "nothing for you
    to do" in a block that exists to prompt action teaches people to skip it.
    """
    assert "http" not in finding.detail + finding.ask, finding.slug
    assert not re.search(r"nothing (for you )?to do|no action",
                         finding.ask, re.I), finding.slug


def test_finding_slugs_are_unique():
    """Two sites sharing a handle would send a reader to the wrong detail,
    and would collapse into one another in main()'s slug-keyed de-dup."""
    slugs = [f.slug for f in _every_finding()]
    assert len(slugs) == len(set(slugs)), sorted(slugs)


def test_leveler_ask_does_not_contradict_the_no_effect_warning(
        monkeypatch, tmp_path, capsys):
    """--enable autogain on a tuning whose leveler is disabled prints "had no
    effect"; the sub-stage finding must not then claim the leveler is running.

    The escalation gates on the leveler actually engaging, not on the flag
    being passed — gating on the flag put the two statements four lines
    apart, contradicting each other.
    """
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    # Leveler off, but the sub-stage bits set: the shape that contradicted.
    text = xml.read_text().replace(
        '<ieq-enable value="1"/>',
        '<ieq-enable value="1"/>\n'
        '        <volume-leveler-compressor-enable value="1"/>')
    xml.write_text(text.replace('<volume-leveler-enable value="1"/>',
                                '<volume-leveler-enable value="0"/>'))

    dolby_to_easyeffects.main(
        [str(xml), "--skip-ee-check", "--enable", "autogain",
         "--output-dir", str(tmp_path / "out"),
         "--irs-dir", str(tmp_path / "irs")])
    out = capsys.readouterr().out
    if "had no effect" in out:
        assert "You enabled autogain, and this tuning pairs" not in out


def test_skip_closing_gates_only_the_ask_block(monkeypatch, tmp_path,
                                                  capsys):
    """`--skip-closing` suppresses the closing block and nothing else —
    dolby_to_pipewire.py relies on that to hold the block back from [1/3]
    while still getting the troubleshooting hints for the chain it built."""
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    base = [str(xml), "--skip-ee-check",
            "--output-dir", str(tmp_path / "out"),
            "--irs-dir", str(tmp_path / "irs")]

    dolby_to_easyeffects.main(base)
    assert dolby_to_easyeffects._REPORT_FORM_URL in capsys.readouterr().out

    dolby_to_easyeffects.main(base + ["--skip-closing"])
    out = capsys.readouterr().out
    assert dolby_to_easyeffects._REPORT_FORM_URL not in out
    # Both closing blocks go, not just the ask. dolby_to_pipewire.py stages
    # into a tempdir it then deletes, so "wrote 3 presets to /tmp/…, open
    # EasyEffects and pick one" pointed at a directory that no longer existed.
    assert "Done — wrote" not in out
    assert "open EasyEffects" not in out
    # The run still happened and still reported itself.
    assert "ieq-amount" in out


def test_skip_closing_still_hands_back_the_findings(tmp_path):
    """The flag governs printing, not collecting. A wrapper that suppressed
    the block would otherwise silently drop every finding the run raised."""
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    collected = []
    dolby_to_easyeffects.main(
        [str(xml), "--skip-ee-check", "--skip-closing",
         "--output-dir", str(tmp_path / "out"),
         "--irs-dir", str(tmp_path / "irs")],
        closing=collected)
    assert isinstance(collected, list)


def test_troubleshooting_menu_renders_every_emitted_filter(monkeypatch,
                                                          capsys):
    """The closing troubleshooting block must actually render.

    Nothing covered this path, so deleting a helper it called left the block
    raising NameError mid-print — after the presets were already written —
    and the whole suite stayed green. It is the most-seen block in the output.
    """
    monkeypatch.setattr(dolby_to_easyeffects, "_CONSOLE", None)
    findings = [dolby_to_easyeffects._loudness_untamed_finding()]
    by_profile = {name: {"default"} for name in
                  ("volmax", "mbc", "regulator", "dialog")}
    by_profile["autogain"] = {"default"}
    dolby_to_easyeffects.print_troubleshooting(findings, by_profile)
    # Collapsed, because the advice wraps to the terminal and the phrases
    # asserted below would otherwise straddle a line break.
    out = " ".join(capsys.readouterr().out.split())

    # Every emitted filter is offered, including the one the hint named: a
    # hint saying "--disable volmax" over a list without volmax in it reads
    # as a bug in the tool.
    for name in ("volmax", "mbc", "dialog"):
        assert f"--disable {name}" in out
    # ...except the regulator, which this hint just said never engages.
    # Offering to switch off a stage the same screen calls inert is a
    # contradiction the reader can't resolve.
    assert "--disable regulator" not in out
    assert "--enable autogain" in out
    # The hint itself, and how to apply any of it.
    assert "--disable volmax" in out
    assert "reload the preset in EasyEffects" in out


def test_regulator_stays_on_the_menu_without_the_inert_hint(monkeypatch,
                                                            capsys):
    """The suppression is specific to the contradiction, not a blanket drop —
    a run that never claimed the regulator was inert still offers it."""
    monkeypatch.setattr(dolby_to_easyeffects, "_CONSOLE", None)
    by_profile = {"regulator": {"default"}, "mbc": {"default"}}
    dolby_to_easyeffects.print_troubleshooting([], by_profile)
    assert "--disable regulator" in capsys.readouterr().out


def test_apply_hint_skips_easyeffects_for_a_wrapper(monkeypatch, capsys):
    """The line telling you how to apply a fix must not end in an action the
    reader can't perform. dolby_to_pipewire.py users chose that path to avoid
    EasyEffects, and its staged presets are deleted anyway."""
    monkeypatch.setattr(dolby_to_easyeffects, "_CONSOLE", None)
    by_profile = {"mbc": {"default"}}

    # Collapsed: the advice wraps to the terminal, so these phrases would
    # otherwise straddle a line break.
    dolby_to_easyeffects.print_troubleshooting([], by_profile)
    assert "reload the preset in EasyEffects" in " ".join(
        capsys.readouterr().out.split())

    dolby_to_easyeffects.print_troubleshooting([], by_profile,
                                               installs_presets=False)
    out = " ".join(capsys.readouterr().out.split())
    assert "EasyEffects" not in out
    # ...but the rest of the advice survives; it's about the chain either way.
    assert "they combine" in out
    assert "--disable mbc" in out


def test_every_shown_tag_is_quotable(monkeypatch, capsys):
    """A hint tag is often the only finding that actually fired for the
    device. Listing only ask-tags under "quote the tag in brackets" sent
    reporters to quote the speculative one and never mention the real
    finding."""
    monkeypatch.setattr(dolby_to_easyeffects, "_CONSOLE", None)
    hint = dolby_to_easyeffects._loudness_untamed_finding()
    ask = dolby_to_easyeffects._experimental_finding("type-3 high-shelf",
                                                     ["high-shelf"])

    # With an ask, the header must not imply only the listed tags count —
    # "which line you mean" leaves it open to any tagged line in the run.
    dolby_to_easyeffects.print_project_asks([hint, ask])
    out = " ".join(capsys.readouterr().out.split())
    assert "quote the [tag] so we know which line you mean" in out

    # With a hint and no ask there is no list at all, so the tag would
    # otherwise go unexplained.
    dolby_to_easyeffects.print_project_asks([hint])
    out = " ".join(capsys.readouterr().out.split())
    assert "Saw a [tag] above?" in out

    # A clean run mentions tags not at all.
    dolby_to_easyeffects.print_project_asks([])
    assert "[tag]" not in capsys.readouterr().out


def test_autogain_entry_warns_when_it_enables_an_unreproduced_stage(
        monkeypatch, capsys):
    """The run warns that a leveler sub-stage "only matters if you rebuild
    with --enable autogain", then offers exactly that flag in the menu. The
    two have to meet, or the menu quietly recommends the thing the warning
    was about."""
    monkeypatch.setattr(dolby_to_easyeffects, "_CONSOLE", None)
    by_profile = {"autogain": {"default"}}
    gap = dolby_to_easyeffects._leveler_gap_finding(
        ["volume-leveler-compressor"], autogain_on=False)

    dolby_to_easyeffects.print_troubleshooting([gap], by_profile)
    assert "leveler-gap" in " ".join(capsys.readouterr().out.split())

    dolby_to_easyeffects.print_troubleshooting([], by_profile)
    assert "leveler-gap" not in capsys.readouterr().out


def test_xml_path_prints_for_any_ask_that_wants_the_xml(monkeypatch, capsys):
    """An ask that requests the XML is unactionable without its path — the
    tool found that file, the user never went looking for it. Gating this on
    one ask's exact phrasing meant a reword silently switched it off."""
    monkeypatch.setattr(dolby_to_easyeffects, "_CONSOLE", None)
    wants = dolby_to_easyeffects.Finding(
        slug="peak-level", kind="ask", detail="x",
        ask="Send us the XML and we can confirm it.")

    dolby_to_easyeffects.print_project_asks([wants], xml_path="/tmp/DEV_X.xml")
    assert "'/tmp/DEV_X.xml'" in capsys.readouterr().out

    # An ask that doesn't want it doesn't get a path dumped at it.
    other = dolby_to_easyeffects._experimental_finding("type-3 high-shelf",
                                                       ["high-shelf"])
    dolby_to_easyeffects.print_project_asks([other], xml_path="/tmp/DEV_X.xml")
    assert "/tmp/DEV_X.xml" not in capsys.readouterr().out


def test_dropped_stages_reach_the_closing_block(monkeypatch, capsys):
    """A stage we drop has no ask — nobody can act on it — but it printed
    hundreds of lines earlier and never again, so the closing block read as
    the whole story while a piece of the tuning was missing from it."""
    monkeypatch.setattr(dolby_to_easyeffects, "_CONSOLE", None)
    import xml.etree.ElementTree as ET

    dropped = dolby_to_easyeffects.collect_unmodeled_features(ET.fromstring("""
        <profile type="dynamic"><tuning-cp>
          <dynamic_speaker_optimization_enable value="1"/>
        </tuning-cp></profile>"""))
    assert dropped and not dropped[0].ask

    dolby_to_easyeffects.print_project_asks(dropped)
    out = " ".join(capsys.readouterr().out.split())
    assert ("doesn't rebuild: [speaker-optimizer]" in out)
    # The mention carries its reason — an entry with nothing to do and no
    # reason to mention it teaches readers to skip the block.
    assert "so we know which devices have them" in out

    # It stays one line of context, not another bulleted thing to action.
    dolby_to_easyeffects.print_project_asks(dropped)
    assert not any(line.lstrip().startswith("•")
                   for line in capsys.readouterr().out.splitlines())


def test_flag_menu_lead_is_dry_run_aware(monkeypatch, capsys):
    """Under --dry-run "the same command you ran" rebuilds nothing, and the
    reload instruction pointed at a preset the very next block says was
    never written."""
    monkeypatch.setattr(dolby_to_easyeffects, "_CONSOLE", None)
    by_profile = {"mbc": {"default"}}

    dolby_to_easyeffects.print_troubleshooting([], by_profile, dry_run=True)
    out = " ".join(capsys.readouterr().out.split())
    assert "re-run without --dry-run" in out

    dolby_to_easyeffects.print_troubleshooting([], by_profile)
    out = " ".join(capsys.readouterr().out.split())
    assert "the same command you ran" in out


def test_fir_verdict_prints_and_tables_hide_without_verbose(tmp_path,
                                                            monkeypatch,
                                                            capsys):
    """The tables were the bulk of the output and buried the findings even
    when marked skippable; the verdict line is what a default reader needs.
    -v restores the full tables (and reports are asked to include a -v
    log)."""
    monkeypatch.setattr(dolby_to_easyeffects, "_CONSOLE", None)
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")

    dolby_to_easyeffects.main([str(xml), "--dry-run", "--skip-ee-check"])
    out = capsys.readouterr().out
    assert "Correction check passed" in out
    assert "re-run with -v" in out
    assert out.count("frequency tables hidden") == 1
    assert "FIR verification (left" not in out
    assert "combined IEQ+AO curve" not in out

    dolby_to_easyeffects.main([str(xml), "--dry-run", "--skip-ee-check",
                               "-v"])
    out = capsys.readouterr().out
    assert "Correction check passed" in out
    assert "FIR verification (left" in out
    assert "combined IEQ+AO curve" in out
    assert "frequency tables hidden" not in out


def test_tag_convention_prints_once_with_the_first_finding(monkeypatch,
                                                           capsys):
    """The first bracketed token a reader meets looked like an error code —
    the convention was only explained in the closing block. One orientation
    line rides the first finding; repeating it per finding would be a nag."""
    monkeypatch.setattr(dolby_to_easyeffects, "_CONSOLE", None)
    monkeypatch.setattr(dolby_to_easyeffects, "_TAG_CONVENTION_SHOWN", False)
    finding = dolby_to_easyeffects._loudness_untamed_finding()
    dolby_to_easyeffects._print_finding_detail(finding)
    dolby_to_easyeffects._print_finding_detail(finding)
    out = " ".join(capsys.readouterr().out.split())
    assert out.count("quote one if you report") == 1


def test_disable_symptoms_do_not_overlap():
    """Two filters describing the same symptom is the same as describing
    none — the reader gets several candidates and no way to choose. Three of
    these used to share "pumping"/"squashed" between them."""
    words = {}
    for name, (symptom, _effect) in DISABLEABLE_FILTERS.items():
        for word in re.findall(r"[a-z]{5,}", symptom.lower()):
            words.setdefault(word, []).append(name)
    shared = {w: n for w, n in words.items() if len(n) > 1
              and w not in {"sounds", "audibly"}}
    assert not shared, f"symptom words shared between filters: {shared}"


# A finding's ask may legitimately speak the vocabulary of the stage it is
# about — the leveler-gap ask and the autogain menu row describe the same
# stage on purpose. Sharing with any OTHER filter's symptom is the round-2
# bug: the ask said "surges" while the regulator row owned "surges", so one
# heard symptom had two competing remedies.
_ASK_STAGE_VOCAB_OK = {"leveler-gap": {"autogain"}}


def test_finding_asks_do_not_borrow_other_filters_symptoms():
    for finding in _every_finding():
        if not finding.ask:
            continue
        named = set(re.findall(r"--(?:disable|enable) ([a-z-]+)",
                               finding.ask))
        named |= _ASK_STAGE_VOCAB_OK.get(finding.slug, set())
        # Quoted 'values' and flag arguments are data (profile names, XML
        # fields), not symptom vocabulary — 'music' the profile must not
        # match "music sounds…", quoted or as "--profile music".
        ask_text = re.sub(r"'[^']*'|--profile [a-z-]+", "",
                          finding.ask.lower())
        ask_words = set(re.findall(r"[a-z]{5,}", ask_text))
        for name, (symptom, _effect) in DISABLEABLE_FILTERS.items():
            if name in named:
                continue
            shared = ask_words & set(
                re.findall(r"[a-z]{5,}", symptom.lower()))
            # "sound(s)" is copy glue, not symptom vocabulary.
            shared -= {"sound", "sounds", "audibly"}
            assert not shared, (
                f"[{finding.slug}] ask shares {shared} with the "
                f"'{name}' menu symptom — one heard symptom, two remedies")


def test_clean_run_closing_block_is_just_the_ask(monkeypatch, capsys):
    """The common case by a wide margin. A rule and a "Help the project"
    heading over a bare report-back line would be noise on every clean run,
    which is how a block earns being skipped."""
    monkeypatch.setattr(dolby_to_easyeffects, "_CONSOLE", None)
    dolby_to_easyeffects.print_project_asks([])
    out = capsys.readouterr().out.strip().splitlines()
    # Two or three lines depending on how wide the sentence wraps; what
    # matters is that there is no rule, no heading and no bullet list.
    assert len(out) <= 3, out
    assert "=====" not in "".join(out)
    assert "Help the project" not in "".join(out)
    assert not any(line.lstrip().startswith("•") for line in out)
    assert out[-1].strip() == dolby_to_easyeffects._REPORT_FORM_URL
