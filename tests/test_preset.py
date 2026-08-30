"""End-to-end tests for the generated EasyEffects preset and its
companion `.irs` impulse-response file.

Each test runs `make_fir` + `make_preset` + `save_wav_stereo` on
synthetic, non-Dolby inputs (matching the *shape* parse_xml produces,
but with deliberately invented values) and asserts on the artifacts.
The file groups two kinds of checks:

  1. **Structural invariants** — preset has the right plugins in the
     right order, IRS file is a 4096-sample float32 stereo RIFF/WAVE,
     etc. Things any future maintainer should expect to remain true.

  2. **Trap regressions** — one assertion per shipped-bug "rabbit
     hole" in CLAUDE.md. Each section header names the trap and points
     to the CLAUDE.md bullet that motivates it. Re-introducing one of
     these bugs should turn the build red even if the math elsewhere
     stays correct.

The split between "structural" and "trap" is editorial only; both
classes of test run on the same fixture and live in the same file.
"""

import hashlib
import json
import math
import re
from datetime import date
from pathlib import Path

import numpy as np
import pytest

import dolby_to_easyeffects
from lib import console, doctor as doctor_module, ee_paths, ee_socket, packages
# Bound before the autouse `no_live_easyeffects_probe` fixture patches the
# module attribute, so the probe itself stays testable.
from lib.ee_socket import easyeffects_running as unpatched_ee_probe
from lib.dax import parse
from lib.doctor import (
    CheckResult,
    DOCTOR_FAIL,
    DOCTOR_PASS,
    DOCTOR_UNKNOWN,
    DOCTOR_WARN,
    emit_check,
    print_verdict,
    summarize,
)
from lib.hardware import sinks, speakers
from lib.pipewire import session
from lib.preset.fir import FIR_LENGTH, SAMPLE_RATE, make_fir
from lib.report import doctor_layout
from lib.report import doctor_run
from lib.report import speaker as report_speaker
from lib.report.doctor_run import _print_doctor_report, parse_ee_version
from lib.preset import autoload
from lib.preset.emit import save_wav_stereo, stereo_taps
from lib.report.profile import _report_parsed_profile
from lib.preset.autoload import (
    BYPASS_PRESET_NAME,
    _atomic_write,
    _atomic_write_text,
    read_autoload_entries,
    read_ee_rc,
    set_autoload_fallback,
    write_autoload,
    write_bypass_preset,
)
from lib.preset.bands import make_peq_eq
from lib.preset.build import make_preset
from lib.preset.plugins import (
    _disabled_band,
    decode_mbc_bands,
    make_autogain,
    make_bass_enhancer,
    make_dialog_enhancer,
    make_limiter,
    make_multiband_compressor,
    make_regulator,
)
from lib.report.environment import (
    DoctorReport,
    autostart_status,
    check_preset_kernel,
    ee_version_status,
    firmware_gate_status,
    install_status,
    kernel_age_status,
    loaded_preset_status,
    parse_kernel_series,
)
from tests.conftest import assert_rows_line_up
from tests.conftest import (
    SYNTHETIC_FREQS_20,
    is_minimum_phase,
    read_irs_file,
    synthetic_mb_comp,
    synthetic_peq_filters,
    synthetic_regulator,
    synthetic_virtual_bass,
    write_synthetic_tuning_xml,
)
from lib.report import environment
from lib.report import findings as report_findings

# For the one trap that has to drive the whole CLI (the impulse responses
# are written by main(), not by make_preset).
SCRIPT = Path(__file__).resolve().parent.parent / "dolby_to_easyeffects.py"


@pytest.fixture
def generated(tmp_path):
    """Build a full preset + IRS pair that exercises every plugin in
    the chain. Yields (preset_dict, irs_path) for assertions.

    Tests that need a deliberately-edge-case input (e.g. a single bell
    of a specific Q to exercise the PEQ output-gain compensation
    formula) build their own small fixtures inline rather than
    parameterising this one — keeps each trap test self-explanatory.
    """
    peq = synthetic_peq_filters([
        # (speaker, type, f0, gain, q, order, s)
        (0, 7, 90.0, 0.0, 0.707, 4, 1.0),    # HP left
        (1, 7, 90.0, 0.0, 0.707, 4, 1.0),    # HP right
        (0, 1, 1000.0, 4.0, 1.5, 0, 1.0),    # bell left
        (1, 1, 1000.0, 4.0, 1.5, 0, 1.0),    # bell right
    ])
    mb = synthetic_mb_comp(group_count=2, bands=[
        (10, -160, 16384, 30000, 32500, 0),
        (20, -160, 16384, 30000, 32500, 0),
    ])
    reg = synthetic_regulator([-6.0] * 20)

    fir, _ = make_fir(SYNTHETIC_FREQS_20, [0.0] * 20)
    irs_path = tmp_path / "Synthetic.irs"
    save_wav_stereo(irs_path, fir, fir)

    preset, _ = make_preset(
        kernel_name=irs_path.stem,
        peq_filters=peq,
        vol_leveler={"enable": True, "amount": 5, "out_target": -16.0},
        dialog_enhancer={"enable": True, "amount": 5, "boost": 4.0},
        mb_comp=mb,
        regulator=reg,
        freqs=SYNTHETIC_FREQS_20,
    )
    return preset, irs_path


# --- structural invariants ---

def test_preset_has_output_section(generated):
    preset, _ = generated
    assert "output" in preset
    assert "plugins_order" in preset["output"]


def test_preset_carries_generator_provenance(generated):
    """The preset is stamped with a `_generator` provenance string so a
    user (or an issue report) can tell which version produced it. It must
    sit alongside `output`, not inside the EasyEffects plugin tree."""
    preset, _ = generated
    assert "_generator" in preset
    assert preset["_generator"].startswith("dolby_to_easyeffects.py ")
    assert "_generator" not in preset["output"]
    # `output` and `_generator` are the only top-level keys.
    assert set(preset) == {"_generator", "output"}


def test_preset_round_trips_through_json(generated):
    """The preset must be JSON-serialisable in both directions — any
    non-serialisable value introduced by a future plugin builder breaks
    the user's load path.
    """
    preset, _ = generated
    reloaded = json.loads(json.dumps(preset))
    assert reloaded == preset


def test_irs_file_is_riff_wave_float32_stereo_48khz(generated):
    _, irs = generated
    sample_rate, n_samples, n_channels, _l, _r = read_irs_file(irs)
    assert sample_rate == SAMPLE_RATE
    assert n_samples == FIR_LENGTH
    assert n_channels == 2


def test_irs_peak_normalised_for_flat_target(generated):
    """A 0 dB target curve should yield a unit-peak FIR; the .irs file
    on disk should reflect that.
    """
    _, irs = generated
    _, _, _, left, right = read_irs_file(irs)
    peak = max(np.abs(left).max(), np.abs(right).max())
    assert peak == pytest.approx(1.0, abs=0.01)


# --- TRAP: convolver autogain (+50 dB clipping bug) ---
# CLAUDE.md: "Clipping or sudden level jumps on loud content — past
# traps include the convolver autogain +50 dB bug". The LSP convolver
# default applies +50 dB RMS re-normalisation, which clips loud content
# because the FIR is already peak-normalised in make_fir.

def test_convolver_autogain_disabled(generated):
    preset, _ = generated
    conv = preset["output"]["convolver#0"]
    assert conv["autogain"] is False, \
        "convolver autogain must be False — defaults re-introduce the +50 dB bug"


# --- TRAP: convolver kernel-name vs deprecated kernel-path ---
# CLAUDE.md: "EE 8.x convolver wants kernel-name (filename stem), not
# the deprecated kernel-path".

def test_convolver_uses_kernel_name_not_kernel_path(generated):
    preset, _ = generated
    conv = preset["output"]["convolver#0"]
    assert "kernel-name" in conv
    assert "kernel-path" not in conv


def test_convolver_instance_id_is_zero_suffix(generated):
    """EasyEffects 8.x identifies plugin instances with `#N` suffixes;
    the convolver must be `convolver#0`, not unsuffixed `convolver`.
    """
    preset, _ = generated
    assert "convolver#0" in preset["output"]
    assert "convolver" not in preset["output"]


def test_kernel_name_matches_irs_stem(generated):
    """The cleanest expression of the kernel-name rule: the JSON's
    kernel-name equals the .irs file's stem on disk — same name, no
    path, no extension.
    """
    preset, irs = generated
    assert preset["output"]["convolver#0"]["kernel-name"] == irs.stem


# --- LOCK-IN: the impulse's name carries a hash of its samples ---
# EasyEffects re-reads an .irs only when the convolver's kernel name changes
# (its preset loader skips the setter on an equal name), so a same-name
# rewrite left the old FIR playing through any reload. The name now follows
# the content. Why: docs/design-notes.md "Rejected approaches".

_HASHED = re.compile(r"\ADolby-Balanced-[0-9a-f]{8}\Z")


def _generate(xml, out, irs, *extra):
    """One in-process run into the given dirs; returns Dolby-Balanced's kernel-name."""
    rc = dolby_to_easyeffects.main(
        [str(xml), "--output-dir", str(out), "--irs-dir", str(irs),
         "--skip-ee-check", "--no-color", *extra])
    assert not rc, rc
    preset = json.loads((out / "Dolby-Balanced.json").read_text())
    return preset["output"]["convolver#0"]["kernel-name"]


def test_kernel_name_carries_a_content_hash(tmp_path):
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    out, irs = tmp_path / "out", tmp_path / "irs"
    name = _generate(xml, out, irs)
    assert _HASHED.match(name), name
    assert (irs / f"{name}.irs").exists()


def test_kernel_hash_is_over_the_taps_written(tmp_path):
    """The suffix is sha256 of the float32 stereo samples on disk — recomputed
    from the file, never a literal: the FIR's last bits move with the
    numpy/BLAS build (see test_golden_preset.py), so a pinned digest would
    fail on the next machine while proving nothing here."""
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    out, irs = tmp_path / "out", tmp_path / "irs"
    name = _generate(xml, out, irs)
    _, _, _, left, right = read_irs_file(irs / f"{name}.irs")
    digest = hashlib.sha256(stereo_taps(np.asarray(left), np.asarray(right))
                            .tobytes()).hexdigest()[:8]
    assert name == f"Dolby-Balanced-{digest}"


def test_a_changed_curve_changes_the_kernel_name(tmp_path):
    """Same tuning → same name (nothing to reload); a different audio-optimizer
    curve → different name, which is what makes EasyEffects re-read the file."""
    same = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    (tmp_path / "other").mkdir()
    other = write_synthetic_tuning_xml(
        tmp_path / "other" / "DEV_SYNTH_SUBSYS_TEST.xml",
        ao_right=",".join(str(16 * (i % 3)) for i in range(20)))
    a = _generate(same, tmp_path / "o1", tmp_path / "i1")
    b = _generate(same, tmp_path / "o2", tmp_path / "i2")
    c = _generate(other, tmp_path / "o3", tmp_path / "i3")
    assert a == b
    assert a != c


def _plant(irs, *names):
    irs.mkdir(parents=True, exist_ok=True)
    for name in names:
        (irs / name).write_bytes(b"RIFF")


@pytest.fixture
def hermetic_referrers(tmp_path, monkeypatch):
    """The stale-impulse cleanup reads four things outside the run's dirs —
    both PipeWire conf dirs, EasyEffects' own preset dir and its convolverrc
    — so point every one at tmp, or the dev machine's real files decide
    what a test keeps."""
    from lib.pipewire import checks
    monkeypatch.setattr(checks, "DEFAULT_OUTPUT_DIR", tmp_path / "pw")
    monkeypatch.setattr(checks, "_UNSCANNED_CONF_DIR", tmp_path / "pw_fc")
    monkeypatch.setattr(ee_paths, "DEFAULT_OUTPUT_DIR", tmp_path / "live_out")
    monkeypatch.setattr(ee_paths, "DEFAULT_EASYEFFECTS_RC", tmp_path / "db" / "easyeffectsrc")
    return tmp_path


def test_stale_hashed_impulses_are_dropped_but_foreign_files_survive(
        hermetic_referrers, tmp_path, capsys):
    """Ours only: an earlier hash of the same preset and the legacy unhashed
    name go; a user's own file and another preset's stay."""
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    out, irs = tmp_path / "out", tmp_path / "irs"
    _plant(irs, "Dolby-Balanced-deadbeef.irs", "Dolby-Balanced.irs",
           "Dolby-Balanced-mytest.irs", "Other-Balanced-12345678.irs")
    name = _generate(xml, out, irs)
    left = {p.name for p in irs.glob("*.irs")}
    assert "Dolby-Balanced-deadbeef.irs" not in left
    assert "Dolby-Balanced.irs" not in left
    assert {"Dolby-Balanced-mytest.irs", "Other-Balanced-12345678.irs"} <= left
    assert [n for n in left if _HASHED.match(n[:-4])] == [f"{name}.irs"]
    out_text = capsys.readouterr().out
    assert "Removed" in out_text and "Dolby-Balanced-deadbeef.irs" in out_text


def test_referenced_legacy_impulse_is_kept(hermetic_referrers, tmp_path, capsys):
    """A preset saved from EasyEffects' GUI keeps its parent's kernel name.
    Deleting that impulse would leave it convolving nothing — and EE loads a
    missing kernel silently — so a file another preset names stays, and the
    run says which preset now plays the older impulse."""
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    out, irs = tmp_path / "out", tmp_path / "irs"
    _plant(irs, "Dolby-Balanced.irs")
    out.mkdir()
    (out / "Dolby-RegV2.json").write_text(json.dumps(
        {"output": {"convolver#0": {"kernel-name": "Dolby-Balanced"}}}))
    _generate(xml, out, irs)
    assert (irs / "Dolby-Balanced.irs").exists()
    out_text = capsys.readouterr().out
    assert "Kept" in out_text and "Dolby-RegV2" in out_text


def test_impulse_named_by_a_pipewire_conf_is_kept(hermetic_referrers, tmp_path, capsys):
    """`ee_to_pipewire.py --no-copy-irs` pins the EE-side path in the conf, and
    a missing impulse there stops the whole conf loading."""
    pw = tmp_path / "pw"
    pw.mkdir()
    (pw / "dolby.conf").write_text('filename = "/x/irs/Dolby-Balanced.irs"\n')
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    out, irs = tmp_path / "out", tmp_path / "irs"
    _plant(irs, "Dolby-Balanced.irs")
    _generate(xml, out, irs)
    assert (irs / "Dolby-Balanced.irs").exists()
    assert "dolby.conf" in capsys.readouterr().out


def test_impulse_named_by_a_filter_chain_service_conf_is_kept(
        hermetic_referrers, tmp_path, capsys):
    """TRAP (code review 2026-08-27): filter-chain.conf.d/ is read by
    filter-chain.service, not the daemon, and docs/alternative-pipelines.md
    hands out a skeleton for exactly that directory naming the EE-side
    impulse. Live for whoever runs that unit — scan it too."""
    from lib.pipewire import checks
    fc = checks._UNSCANNED_CONF_DIR
    fc.mkdir()
    (fc / "dolby-speaker.conf").write_text(
        'config = { filename = "~/.local/share/easyeffects/irs/Dolby-Balanced.irs" }\n')
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    out, irs = tmp_path / "out", tmp_path / "irs"
    _plant(irs, "Dolby-Balanced.irs")
    _generate(xml, out, irs)
    assert (irs / "Dolby-Balanced.irs").exists()
    assert "dolby-speaker.conf" in capsys.readouterr().out


def test_live_tree_presets_keep_their_impulse_under_a_custom_output_dir(
        hermetic_referrers, tmp_path, capsys):
    """TRAP (code review 2026-08-27): --output-dir alone leaves --irs-dir at
    EasyEffects' own tree, so the file this run calls stale is the one the
    LIVE presets name — and this run never rewrote those. Scanning only the
    directory this run wrote deleted it and put every live preset into
    convolver passthrough."""
    live_out = ee_paths.DEFAULT_OUTPUT_DIR
    live_out.mkdir()
    (live_out / "Dolby-Balanced.json").write_text(json.dumps(
        {"output": {"convolver#0": {"kernel-name": "Dolby-Balanced"}}}))
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    out, irs = tmp_path / "out", tmp_path / "irs"
    _plant(irs, "Dolby-Balanced.irs")
    _generate(xml, out, irs)
    assert (irs / "Dolby-Balanced.irs").exists()
    out_text = capsys.readouterr().out
    assert "Kept" in out_text and "Dolby-Balanced in" in out_text


def test_impulse_named_by_easyeffects_saved_settings_is_kept(
        hermetic_referrers, tmp_path, capsys):
    """TRAP: EasyEffects restores the convolver's kernel NAME from its own
    convolverrc on start, not from the preset JSON. Remove the file that
    name points at and a fresh EasyEffects comes up in passthrough — "Kernel
    'Dolby-Balanced' not found" on the dev machine, 2026-08-27 — silent until
    something loads a preset, which without autoload is never."""
    db = tmp_path / "db"
    db.mkdir()
    (db / "convolverrc").write_text("[convolver#0]\nkernelName=Dolby-Balanced\n")
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    out, irs = tmp_path / "out", tmp_path / "irs"
    _plant(irs, "Dolby-Balanced.irs", "Dolby-Balanced-deadbeef.irs")
    _generate(xml, out, irs)
    assert (irs / "Dolby-Balanced.irs").exists()
    assert not (irs / "Dolby-Balanced-deadbeef.irs").exists()
    assert "EasyEffects' saved settings" in capsys.readouterr().out


def test_dry_run_deletes_no_impulse_file(hermetic_referrers, tmp_path):
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    out, irs = tmp_path / "out", tmp_path / "irs"
    _plant(irs, "Dolby-Balanced-deadbeef.irs", "Dolby-Balanced.irs")
    rc = dolby_to_easyeffects.main(
        [str(xml), "--output-dir", str(out), "--irs-dir", str(irs),
         "--skip-ee-check", "--no-color", "--dry-run"])
    assert not rc
    assert {p.name for p in irs.glob("*.irs")} == {
        "Dolby-Balanced-deadbeef.irs", "Dolby-Balanced.irs"}


# --- TRAP: enum parameters as integer indices (commit 91423b8) ---
# CLAUDE.md: "enum parameters must be string labels, not integer
# indices (commit 91423b8 was this exact bug)".

# Field names whose values must be string enums in EasyEffects 8.x.
# Anywhere these keys appear in the preset JSON, the value must be a string.
_STRING_ENUM_KEYS = {
    "type", "mode", "slope", "compressor-mode", "compression-mode",
    "envelope-boost", "sidechain-type", "sidechain-mode",
    "sidechain-source", "stereo-split-source", "reference",
}


def _walk_kv(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield (k, v, f"{path}.{k}")
            yield from _walk_kv(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _walk_kv(item, f"{path}[{i}]")


def test_enum_parameters_are_strings(generated):
    preset, _ = generated
    offenders = []
    for k, v, path in _walk_kv(preset):
        if k in _STRING_ENUM_KEYS and not isinstance(v, str):
            offenders.append(f"{path} = {v!r}")
    assert not offenders, \
        "enum-typed fields must be string labels, never integers:\n  " + \
        "\n  ".join(offenders)


# --- TRAP: .irs extension and minimum-phase FIR ---
# CLAUDE.md: "impulse-response files need the .irs extension";
# "FIR must be minimum-phase".

def test_irs_file_uses_irs_extension(generated):
    _, irs = generated
    assert irs.suffix == ".irs"
    assert irs.exists()


def test_generated_fir_is_minimum_phase(generated):
    """End-to-end: the FIR that lands on disk via the production path
    must still be minimum-phase. Linear-phase FIRs sound like pre-ringing.
    """
    _, irs = generated
    _, _, _, left, _ = read_irs_file(irs)
    assert is_minimum_phase(left, tol=1e-3)


# --- TRAP: LSP MBC defaults to upward compression ---
# CLAUDE.md: "Audible noise-floor boost during silence — the
# upward-compression trap on LSP MBC defaults". LSP's compression-mode
# defaults to "Upward" when omitted; we must explicitly set "Downward".

def test_mbc_compression_mode_is_downward_on_every_band(generated):
    preset, _ = generated
    mbc = preset["output"]["multiband_compressor#0"]
    for i in range(8):
        band = mbc[f"band{i}"]
        assert band["compression-mode"] == "Downward", \
            f"MBC band{i} compression-mode is {band['compression-mode']!r}; " \
            "LSP defaults to Upward and must be overridden explicitly"


def test_mbc_top_level_output_gain_is_zero(generated):
    """CLAUDE.md flags "MBC output-gain misconfiguration" as a past
    clipping trap. The MBC top-level output-gain feeds straight into
    the limiter; it must stay at 0 dB so per-band makeup is the only
    place gain rejoins the chain.
    """
    preset, _ = generated
    mbc = preset["output"]["multiband_compressor#0"]
    assert mbc["output-gain"] == 0.0


def test_regulator_compression_mode_is_downward_on_every_band(generated):
    preset, _ = generated
    reg = preset["output"]["multiband_compressor#1"]
    for i in range(8):
        band = reg[f"band{i}"]
        assert band["compression-mode"] == "Downward"


def test_disabled_band_trap_keys_across_both_builders():
    """The LSP "band off" dict is shared via _disabled_band() (finding S1).

    Exercise active AND disabled bands of both make_multiband_compressor and
    make_regulator, and assert the trap-fix values survive on every band:
    compression-mode "Downward" (LSP defaults to "Upward"), boost-amount 0.0
    (LSP's upward-compression default must stay off), and enable-band False on
    disabled bands. Disabled bands must match _disabled_band() exactly, so a
    dropped/reordered key in the shared helper turns the build red.
    """
    # group_count=2 -> bands 0-1 active, bands 2-7 disabled.
    mbc = make_multiband_compressor(
        synthetic_mb_comp(group_count=2, bands=[
            (10, -160, 16384, 30000, 32500, 0),
            (20, -160, 16384, 30000, 32500, 0),
        ]),
        SYNTHETIC_FREQS_20,
    )
    # One zone (all thresholds equal and < 0) -> band0 active, bands 1-7 off.
    reg = make_regulator(synthetic_regulator(threshold_high=[-6.0] * 20),
                         SYNTHETIC_FREQS_20)

    for label, comp in (("MBC", mbc), ("regulator", reg)):
        bands = [comp[f"band{i}"] for i in range(8)]
        assert any(b["compressor-enable"] for b in bands), f"{label}: no active band"
        assert any(not b["compressor-enable"] for b in bands), f"{label}: no disabled band"
        for i, b in enumerate(bands):
            assert b["compression-mode"] == "Downward", \
                f"{label} band{i}: compression-mode {b['compression-mode']!r} (LSP default Upward)"
            assert b["boost-amount"] == 0.0, \
                f"{label} band{i}: boost-amount {b['boost-amount']} (upward-comp must stay off)"
            if not b["compressor-enable"]:
                assert b["enable-band"] is False, \
                    f"{label} band{i}: disabled band enable-band is not False"
                assert b == _disabled_band(), \
                    f"{label} band{i}: disabled band drifted from _disabled_band()"


def test_decode_mbc_bands_is_single_source_for_builder():
    """M-DUP-PRINT: decode_mbc_bands is the one decode both the LSP builder
    and the main() diagnostics consume. Lock in that the values the builder
    emits (after its rounding) match decode_mbc_bands exactly, so the two
    can never silently drift apart again.

    The second band uses an out-of-range gain coeff (0) so the ratio-clamp
    path (decode -> 100.0) is covered too — the case where the old inline
    diagnostics re-decode printed inf instead of the emitted 100.
    """
    mb = synthetic_mb_comp(group_count=2, bands=[
        (10, -48, 16384, 30000, 32500, -32),   # in-range: ratio ~2:1
        (20, -96, 0, 30000, 32500, 16),         # gain coeff 0 -> ratio clamps to 100
    ])
    bands = decode_mbc_bands(mb)
    assert len(bands) == 2
    assert bands[1]["ratio"] == 100.0, \
        "out-of-range gain must clamp to 100.0, not inf"

    mbc = make_multiband_compressor(mb, SYNTHETIC_FREQS_20)
    for i, b in enumerate(bands):
        emitted = mbc[f"band{i}"]
        assert emitted["attack-threshold"] == round(b["threshold"], 4)
        assert emitted["attack-time"] == round(b["attack_ms"], 4)
        assert emitted["release-time"] == round(b["release_ms"], 4)
        assert emitted["ratio"] == round(b["ratio"], 4)
        assert emitted["makeup"] == round(b["makeup"], 4)


# --- TRAP: PEQ output-gain compensation for clipping/loudness ---
# CLAUDE.md: "Loss of loudness / over-conservative PEQ output-gain
# compensation"; "Clipping or sudden level jumps on loud content".

def test_peq_output_gain_compensates_highest_bell():
    """A +6 dB bell at Q=2 has effective broadband contribution of
    6 * min(1, 2/2) = 6 dB → output-gain must be -6 dB.
    """
    peq = synthetic_peq_filters([
        (0, 1, 1000.0, 6.0, 2.0, 0, 1.0),
        (1, 1, 1000.0, 6.0, 2.0, 0, 1.0),
    ])
    eq = make_peq_eq(peq)
    assert eq is not None
    assert eq["output-gain"] == pytest.approx(-6.0, abs=0.01)


def test_peq_output_gain_scales_narrowband_bell_down():
    """A narrow Q=4 bell raises broadband level by ~gain * 2/Q = gain/2,
    so output-gain compensates by half the bell gain (not full).
    """
    peq = synthetic_peq_filters([
        (0, 1, 3000.0, 8.0, 4.0, 0, 1.0),
        (1, 1, 3000.0, 8.0, 4.0, 0, 1.0),
    ])
    eq = make_peq_eq(peq)
    # 8 * (2/4) = 4 dB of broadband boost → -4 dB output-gain.
    assert eq["output-gain"] == pytest.approx(-4.0, abs=0.01)


def test_peq_output_gain_clamps_low_q_bell_to_full_gain():
    """Wide-Q bells (Q ≤ 2) raise the broadband level by their full
    gain — the `min(1, 2/Q)` clamp prevents the compensation from
    over-shooting. A Q=1 bell at +4 dB must compensate by exactly
    -4 dB (not -8 dB), so removing the clamp is caught here.
    """
    peq = synthetic_peq_filters([
        (0, 1, 1000.0, 4.0, 1.0, 0, 1.0),
        (1, 1, 1000.0, 4.0, 1.0, 0, 1.0),
    ])
    eq = make_peq_eq(peq)
    assert eq["output-gain"] == pytest.approx(-4.0, abs=0.01)


def test_peq_output_gain_zero_for_cut_only_chain():
    """HP/LP filters reduce headroom, so they don't enter the
    compensation sum: output-gain stays at 0 for a cut-only PEQ.
    """
    peq = synthetic_peq_filters([
        (0, 7, 90.0, 0.0, 0.707, 4, 1.0),
        (1, 7, 90.0, 0.0, 0.707, 4, 1.0),
    ])
    eq = make_peq_eq(peq)
    assert eq["output-gain"] == 0.0


def test_peq_output_gain_uses_global_max_across_asymmetric_channels():
    """Asymmetric L/R bells (the convertible/AIO per-speaker corrections,
    ~110 corpus profiles) share one output-gain — EE's equalizer has no
    per-channel output-gain. The single trim must be -max(L,R), applied
    equally to both channels, which preserves the Dolby-tuned L/R balance
    at every frequency. A per-channel trim would instead impose a broadband
    L-vs-R tilt and is what this test forbids: the left/right band gains
    stay the channel's own value, untouched by the compensation.
    """
    peq = synthetic_peq_filters([
        (0, 1, 1000.0, 6.0, 2.0, 0, 1.0),  # left  +6 dB
        (1, 1, 1000.0, 2.0, 2.0, 0, 1.0),  # right +2 dB
    ])
    eq = make_peq_eq(peq)
    # Global max of the two channels' effective boost (6 and 2) → -6 dB.
    assert eq["output-gain"] == pytest.approx(-6.0, abs=0.01)
    # The per-channel EQ curves are untouched — only the shared trim differs.
    assert eq["left"]["band0"]["gain"] == pytest.approx(6.0, abs=0.01)
    assert eq["right"]["band0"]["gain"] == pytest.approx(2.0, abs=0.01)


@pytest.mark.parametrize("shelf_type", [4, 3])   # low shelf, high shelf
def test_peq_output_gain_counts_a_shelf_at_its_full_gain(shelf_type):
    """Both shelf directions raise a whole half-band, so they enter the
    compensation at their full gain — no bandwidth scaling, unlike a bell.
    The golden digest can't see this: its mixed-shape fixture has a bell
    boosting harder than the shelf, so the shelf never sets the maximum.
    """
    peq = synthetic_peq_filters([
        (0, shelf_type, 4000.0, 5.0, 0.7, 0, 1.0),
        (1, shelf_type, 4000.0, 5.0, 0.7, 0, 1.0),
    ])
    eq = make_peq_eq(peq)
    assert eq["output-gain"] == pytest.approx(-5.0, abs=0.01)


# --- LOCK-IN: each Dolby filter type keeps its own shape, fillers included ---
# Dolby type code → filter shape drives bucketing, band building, the filler
# and the boost sum from one table. These pin the code→shape mapping (a code
# claimed by the wrong shape emits the wrong filter) and the neutral band that
# fills an unmatched slot on the shorter channel. The golden digest covers the
# shapes themselves, but only the high-pass filler — its asymmetric fixture is
# the one with an unmatched slot.

@pytest.mark.parametrize("type_code,expected", [
    (7, {"type": "Hi-pass", "frequency": 100.0, "slope": "x2"}),
    (9, {"type": "Hi-pass", "frequency": 100.0, "slope": "x2"}),
    (6, {"type": "Lo-pass", "frequency": 20000.0, "slope": "x2"}),
    (8, {"type": "Lo-pass", "frequency": 20000.0, "slope": "x2"}),
    (4, {"type": "Lo-shelf", "frequency": 100.0, "gain": 0.0}),
    (3, {"type": "Hi-shelf", "frequency": 10000.0, "gain": 0.0}),
    (1, {"type": "Bell", "frequency": 1000.0, "gain": 0.0}),
])
def test_peq_unmatched_slot_is_filled_with_its_own_shape(type_code, expected):
    """One filter on the left channel only: the right channel's band0 is a
    filler of the *same* shape, so the two channels stay matched band-for-band
    (EE has one band list per channel and no way to say "absent"). The shelf
    and bell fillers are 0 dB no-ops; HP/LP have no neutral setting, so they
    fill at an out-of-the-way corner — 100 Hz / 20 kHz, 4th order.
    """
    peq = synthetic_peq_filters([(0, type_code, 1234.0, 3.0, 1.5, 2, 1.0)])
    eq = make_peq_eq(peq)
    assert eq["num-bands"] == 1
    # The real band on the left carries the shape its type code selects...
    assert eq["left"]["band0"]["type"] == expected["type"]
    assert eq["left"]["band0"]["frequency"] == 1234.0
    # ...and the filler opposite it is the same shape, at its own defaults.
    for key, value in expected.items():
        assert eq["right"]["band0"][key] == value


# --- TRAP: HDA autogain bypass + crackle-safe gate ---
# The leveler stays bypassed by default on HDA: EE's autogain lacks
# Dolby's MI steering, so it boosts legitimate quiet content and loud
# onsets ride ~4 dB of overshoot into the downstream dynamics — audible
# saturation, measured independent of maximum-history (issue #25 flip
# attempt, design-notes). --enable autogain is the loudness opt-in.
# The stored silence gate is -50 dB (not EE's -70 default) so enabling —
# via flag or GUI — gets the #25 field-confirmed crackle fix.

def test_hda_autogain_is_bypassed(generated):
    """HDA preset (default) emits autogain#0 with bypass=True. Removing
    that bypass re-introduces saturation on quiet-background content.
    """
    preset, _ = generated
    autogain = preset["output"].get("autogain#0")
    assert autogain is not None, \
        "autogain#0 should be present (bypassed) so users can A/B with it"
    assert autogain["bypass"] is True, \
        "HDA autogain must stay bypassed by default — EE's leveler lacks " \
        "MI-style steering; loud onsets over a quiet background saturate " \
        "(measured in the issue #25 default-flip attempt)"
    assert autogain["silence-threshold"] == -50.0, \
        "the stored gate must stay -50 dB so manual/flag enabling gets the " \
        "issue #25 crackle fix; -70 dB crackles on sounds after silence"


# --- TRAP: plugin order, limiter as final stage ---
# A brickwall limiter only protects against clipping if it's the final
# stage. Reordering it earlier defeats the safety net.

def test_plugin_order_starts_with_convolver(generated):
    """Convolver is the first plugin: applies the IEQ + AO correction
    before any of the dynamic processing downstream.
    """
    preset, _ = generated
    assert preset["output"]["plugins_order"][0] == "convolver#0"


def test_limiter_is_last_in_plugin_order(generated):
    preset, _ = generated
    assert preset["output"]["plugins_order"][-1] == "limiter#0"


def test_limiter_threshold_is_minus_one_dbfs():
    """The chain-end brickwall sits at −1 dBFS for inter-sample-peak
    headroom (gain-staging budget table in docs/design-notes.md). A
    drift here changes the output ceiling on every preset."""
    lim = make_limiter()
    assert lim["threshold"] == -1.0
    # volmax fallback injects into input-gain, never the threshold
    assert make_limiter(input_gain=6.0)["threshold"] == -1.0
    assert make_limiter(input_gain=6.0)["input-gain"] == 6.0


# --- Atomic writes ---
# Output files are written via a same-dir temp + os.replace so a crash
# mid-write can't leave a truncated .json/.irs that EasyEffects would
# silently fail to load.

def test_atomic_write_text_creates_file(tmp_path):
    path = tmp_path / "preset.json"
    _atomic_write_text(path, "hello\n")
    assert path.read_text() == "hello\n"


def test_atomic_write_text_overwrites_existing(tmp_path):
    path = tmp_path / "preset.json"
    path.write_text("old content")
    _atomic_write_text(path, "new content")
    assert path.read_text() == "new content"


def test_atomic_write_text_leaves_no_temp_file(tmp_path):
    path = tmp_path / "preset.json"
    _atomic_write_text(path, "data")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "preset.json"]
    assert leftovers == [], f"unexpected leftover files: {leftovers}"


def test_atomic_write_aborts_cleanly_on_error(tmp_path):
    """A failure mid-write must leave neither a temp file nor a clobbered
    target — the existing file (if any) stays intact and os.replace never
    runs. This is the single-implementation guarantee _atomic_write owns."""
    path = tmp_path / "preset.json"
    path.write_text("original")
    with pytest.raises(RuntimeError):
        with _atomic_write(path) as tmp:
            tmp.write_text("partial")
            raise RuntimeError("write failed")
    assert path.read_text() == "original", "target must not be clobbered"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "preset.json"]
    assert leftovers == [], f"temp file left behind: {leftovers}"


# --- LOCK-IN: make_autogain SoundWire (conservative) branch ---
# Lock the conservative derivations (target = out_target - 6, history =
# max(40 - amount*4, 15), -50 dB silence gate) so an edit can't silently
# strip the SoundWire leveler's extra headroom. Conservative is the only
# default-active path; HDA activates only via --enable autogain.

_LEVELER = {"enable": True, "amount": 5, "out_target": -16.0}


def test_autogain_conservative_branch_active_with_derived_settings():
    ag = make_autogain(dict(_LEVELER), conservative=True)
    assert ag["bypass"] is False
    assert ag["target"] == -22.0             # out_target - 6.0
    assert ag["maximum-history"] == 20       # max(40 - amount*4, 15)
    assert ag["silence-threshold"] == -50.0
    assert ag["reference"] == "Geometric Mean (MSI)"


def test_autogain_conservative_history_floor_is_15():
    ag = make_autogain({"enable": True, "amount": 10, "out_target": -16.0},
                       conservative=True)
    assert ag["maximum-history"] == 15       # max(40 - 40, 15)


def test_autogain_disabled_leveler_returns_none():
    off = {"enable": False, "amount": 5, "out_target": -16.0}
    assert make_autogain(off, conservative=True) is None
    assert make_autogain(off) is None
    assert make_autogain(None) is None


def test_autogain_hda_vs_conservative():
    """Both branches share the -50 dB silence gate (issue #25); they
    differ in bypass (HDA ships bypassed), target (HDA unshifted,
    conservative -6 dB headroom) and history window (HDA shorter)."""
    hda = make_autogain(dict(_LEVELER))
    cons = make_autogain(dict(_LEVELER), conservative=True)
    assert hda["bypass"] is True
    assert hda["silence-threshold"] == -50.0
    assert hda["target"] == -16.0            # out_target unshifted
    assert hda["maximum-history"] == 10      # max(30 - amount*5, 10)
    assert cons["bypass"] is False
    assert cons["silence-threshold"] == -50.0


def test_autogain_enabled_flag_clears_hda_bypass():
    """--enable autogain (enabled=True) activates the HDA leveler with
    otherwise identical settings; it's a no-op on the conservative path,
    which is already active."""
    ag = make_autogain(dict(_LEVELER), enabled=True)
    assert ag["bypass"] is False
    assert ag["silence-threshold"] == -50.0
    assert ag["target"] == -16.0
    cons = make_autogain(dict(_LEVELER), conservative=True, enabled=False)
    assert cons["bypass"] is False


# --- LOCK-IN: make_dialog_enhancer gain derivations ---

def test_dialog_enhancer_gain_formula():
    """A single presence bell at 2.5 kHz, gain = amount/16 * 6 dB
    (rounded to 2 decimals -> 1.88 for amount=5)."""
    de = make_dialog_enhancer({"enable": True, "amount": 5, "boost": 4.0})
    assert de["num-bands"] == 1
    assert de["split-channels"] is False
    band = de["left"]["band0"]
    assert band["frequency"] == 2500.0
    assert band["gain"] == round(5 / 16 * 6.0, 2)  # 1.88
    assert de["left"] == de["right"]               # mirrored channels


def test_dialog_enhancer_has_no_soundwire_variant():
    """The SoundWire-only *8 mapping + 4 kHz clarity bell is removed
    (it compensated the pre-#13 over-applied-IEQ treble crush;
    design-notes unvalidated-scaling entry 1). One mapping for all
    device families, and no is_soundwire switch to reintroduce it."""
    import inspect
    assert "is_soundwire" not in inspect.signature(make_dialog_enhancer).parameters


def test_dialog_enhancer_zero_amount_returns_none():
    zero = {"enable": True, "amount": 0, "boost": 0.0}
    assert make_dialog_enhancer(zero) is None
    assert make_dialog_enhancer(None) is None


# --- LOCK-IN: make_regulator multi-zone behaviour ---
# The existing trap tests only exercise the single-zone ([-6]*20) path;
# these lock the zone grouping, the geometric-mean split frequencies, the
# >8-zone merge rule, and the slope/timbre/threshold>=0 mappings.

def test_regulator_three_zone_split_frequencies_geometric_mean():
    th = [-6.0] * 7 + [-12.0] * 7 + [-3.0] * 6
    reg = make_regulator(synthetic_regulator(th), SYNTHETIC_FREQS_20)
    active = [reg[f"band{i}"] for i in range(8)
              if reg[f"band{i}"]["compressor-enable"]]
    assert len(active) == 3
    assert reg["band0"]["attack-threshold"] == -6.0
    assert "split-frequency" not in reg["band0"]   # band 0 has no split
    assert reg["band1"]["attack-threshold"] == -12.0
    # split = geometric mean of the zone-boundary band frequencies:
    # sqrt(freqs[6] * freqs[7]) = sqrt(315 * 400) -> 355.0
    assert reg["band1"]["split-frequency"] == round(math.sqrt(315 * 400), 1)
    assert reg["band2"]["attack-threshold"] == -3.0
    # sqrt(freqs[13] * freqs[14]) = sqrt(2500 * 4000) -> 3162.3
    assert reg["band2"]["split-frequency"] == round(math.sqrt(2500 * 4000), 1)
    for i in range(3, 8):
        assert reg[f"band{i}"] == _disabled_band()


def test_regulator_merges_excess_zones_keeping_less_aggressive_threshold():
    """10 alternating 2-band runs -> 10 zones; two merges bring it down
    to the 8-band LSP limit. The merge rule keeps max(z1, z2): a -6 zone
    merged with a -12 zone must land on -6 (less aggressive), never -12.
    """
    th = ([-6.0] * 2 + [-12.0] * 2) * 5
    reg = make_regulator(synthetic_regulator(th), SYNTHETIC_FREQS_20)
    assert all(reg[f"band{i}"]["compressor-enable"] for i in range(8))
    # band0 absorbed indices 0-5 (the -6/-12/-6 runs) at max() = -6.0.
    assert reg["band0"]["attack-threshold"] == -6.0
    # Next zone starts at index 6: split at sqrt(freqs[5] * freqs[6]).
    assert reg["band1"]["attack-threshold"] == -12.0
    assert reg["band1"]["split-frequency"] == round(math.sqrt(250 * 315), 1)


@pytest.mark.parametrize("slope,expected_ratio", [
    (0.5, 2.0),     # ratio = 1 / (1 - slope)
    (1.0, 100.0),   # slope >= 1 -> hard-limiter cap
    (0.0, 1.0),     # slope <= 0 -> bypass ratio
])
def test_regulator_distortion_slope_maps_to_ratio(slope, expected_ratio):
    reg = make_regulator(
        synthetic_regulator([-6.0] * 20, distortion_slope=slope),
        SYNTHETIC_FREQS_20)
    assert reg["band0"]["ratio"] == expected_ratio


def test_regulator_timbre_maps_to_knee():
    reg = make_regulator(
        synthetic_regulator([-6.0] * 20, timbre_preservation=0.5),
        SYNTHETIC_FREQS_20)
    assert reg["band0"]["knee"] == -3.0   # knee = -6 * timbre


def test_regulator_nonnegative_threshold_disables_band():
    """A zone with threshold >= 0 dB can never trigger; its band slot is
    kept (with split point) but compressor-enable is False to save CPU."""
    th = [-6.0] * 10 + [0.0] * 10
    reg = make_regulator(synthetic_regulator(th), SYNTHETIC_FREQS_20)
    assert reg["band0"]["compressor-enable"] is True
    assert reg["band1"]["compressor-enable"] is False
    assert reg["band1"]["attack-threshold"] == 0.0
    assert reg["band1"]["enable-band"] is True


def test_regulator_all_zero_thresholds_yields_no_active_band():
    """A flat 0 dB threshold_high (issue #27 field tuning) collapses to a
    single zone whose band is disabled — the regulator emits but limits
    nothing, so any volmax boost riding it reaches the brickwall untamed."""
    reg = make_regulator(synthetic_regulator([0.0] * 20), SYNTHETIC_FREQS_20)
    assert not any(reg[f"band{i}"]["compressor-enable"] for i in range(8))


# --- Experimental --enable coupled-bands (isolated_band, issue #44) ---

def test_regulator_coupled_bands_activates_nonisolated_zero_zone():
    """With couple_bands on, a 0 dBFS zone whose bands are all
    isolated_band==0 becomes a live limiter at full scale; zones with a
    real (negative) threshold are untouched."""
    th = [-6.0] * 10 + [0.0] * 10
    reg_dict = synthetic_regulator(th, isolated_band=[1] * 10 + [0] * 10)
    off = make_regulator(reg_dict, SYNTHETIC_FREQS_20)
    assert off["band1"]["compressor-enable"] is False
    on = make_regulator(reg_dict, SYNTHETIC_FREQS_20, couple_bands=True)
    assert on["band1"]["compressor-enable"] is True
    assert on["band1"]["attack-threshold"] == 0.0
    assert on["band0"]["compressor-enable"] is True
    assert on["band0"]["attack-threshold"] == -6.0


def test_regulator_coupled_bands_requires_isolated_data():
    """No isolated_band in the XML -> the flag is a no-op (nothing to
    read, so nothing may change)."""
    th = [-6.0] * 10 + [0.0] * 10
    reg = make_regulator(synthetic_regulator(th), SYNTHETIC_FREQS_20,
                         couple_bands=True)
    assert reg["band1"]["compressor-enable"] is False


def test_regulator_coupled_bands_skips_isolated_marked_zone():
    """A 0 dBFS zone containing any isolated_band==1 band keeps the
    default disabled behaviour — the hypothesis only covers
    non-isolated bands."""
    th = [-6.0] * 10 + [0.0] * 10
    all_marked = synthetic_regulator(th, isolated_band=[1] * 20)
    reg = make_regulator(all_marked, SYNTHETIC_FREQS_20, couple_bands=True)
    assert reg["band1"]["compressor-enable"] is False
    one_marked = synthetic_regulator(
        th, isolated_band=[1] * 10 + [0] * 5 + [1] + [0] * 4)
    reg = make_regulator(one_marked, SYNTHETIC_FREQS_20, couple_bands=True)
    assert reg["band1"]["compressor-enable"] is False


def test_regulator_isolated_data_alone_changes_nothing():
    """isolated_band data without the flag must leave the default output
    byte-identical — the XML-only default path is the invariant."""
    th = [-6.0] * 10 + [0.0] * 10
    base = make_regulator(synthetic_regulator(th), SYNTHETIC_FREQS_20)
    with_iso = make_regulator(
        synthetic_regulator(th, isolated_band=[0] * 20), SYNTHETIC_FREQS_20)
    assert with_iso == base


def test_coupled_bands_eligibility_helper():
    """_coupled_bands_eligible gates the run's "Added a limit ..." line, so it
    must answer exactly what make_regulator does: true only when a whole
    ZONE sits at >= 0 dB with every one of its bands marked non-isolated."""
    from lib.preset.plugins import _coupled_bands_eligible
    th = [-6.0] * 10 + [0.0] * 10
    assert _coupled_bands_eligible(
        synthetic_regulator(th, isolated_band=[1] * 10 + [0] * 10))
    assert not _coupled_bands_eligible(synthetic_regulator(th))
    assert not _coupled_bands_eligible(
        synthetic_regulator(th, isolated_band=[1] * 20))
    assert not _coupled_bands_eligible(
        synthetic_regulator([-6.0] * 20, isolated_band=[0] * 20))
    assert not _coupled_bands_eligible(None)


def test_coupled_bands_eligibility_is_zone_level_not_band_level():
    """A non-isolated band inside a zone that also holds an isolated one does
    NOT qualify — make_regulator declines that zone, so a band-level `any()`
    would have the run announce a limit the preset never carries. It did, on
    274 of 37,949 otherwise-eligible corpus profiles (re-derived 2026-08-11).

    The 20 bands share one threshold here, so they form a single zone; one
    isolated band anywhere in it must sink the whole thing.
    """
    from lib.preset.plugins import _coupled_bands_eligible
    th = [0.0] * 20                            # one zone, at full scale
    iso = [0] * 20
    assert _coupled_bands_eligible(synthetic_regulator(th, isolated_band=iso))
    iso[7] = 1                                 # one isolated band in the zone
    assert not _coupled_bands_eligible(
        synthetic_regulator(th, isolated_band=iso))
    # Split it into two zones and the all-zero one qualifies again.
    split_th = [0.0] * 10 + [-3.0] * 10
    assert _coupled_bands_eligible(
        synthetic_regulator(split_th, isolated_band=[0] * 10 + [1] * 10))


def _report_tuning(regulator, volmax_boost, profile_used="dynamic",
                   default_profile=None, geq_max_range=192, ao_enabled=True,
                   ao_db=None):
    """Minimal tuning stand-in for _report_parsed_profile: every optional
    block is falsy so only the regulator/volmax sections print.

    ``ao_db`` is the audio-optimizer curve in dB — the unit these tests reason
    in — carried here in the 1/16-dB fixed point the XML uses, since that is
    what the report converts back."""
    from types import SimpleNamespace
    ao_raw = [v * parse.DB_FIXED_POINT_SCALE for v in (ao_db or [0.0] * 20)]
    return SimpleNamespace(
        ieq_amount=10, peq_filters=[], dialog_enhancer=None, surround=None,
        vol_leveler=None, mb_comp=None, regulator=regulator,
        volmax_boost=volmax_boost, freqs=SYNTHETIC_FREQS_20,
        profile_used=profile_used, default_profile=default_profile,
        geq_max_range=geq_max_range, ao_enabled=ao_enabled, findings=[],
        curves={}, ao_left=ao_raw, ao_right=ao_raw)


@pytest.mark.parametrize("threshold_high,volmax_boost,disabled,expect_warn", [
    ([0.0] * 20, 6.0, set(), True),            # inert regulator + boost
    ([-6.0] * 20, 6.0, set(), False),          # regulator actually limits
    ([0.0] * 20, 0.0, set(), False),           # no boost to warn about
    ([0.0] * 20, 6.0, {"volmax"}, False),      # boost disabled
    ([0.0] * 20, 6.0, {"regulator"}, False),   # limiter fallback path
])
def test_report_warns_only_when_volmax_rides_inert_regulator(
        silence_console, capsys, threshold_high, volmax_boost, disabled,
        expect_warn):
    """The 'regulator never engages' heads-up fires exactly when the volmax
    boost rides a regulator whose bands are all threshold >= 0 dB."""
    silence_console(console)
    _report_parsed_profile(
        _report_tuning(synthetic_regulator(threshold_high), volmax_boost),
        disabled)
    out = capsys.readouterr().out
    assert ("regulator never engages" in out) is expect_warn


def test_report_summarises_by_default_and_dumps_with_verbose(silence_console,
                                                             capsys):
    """Normal-verbosity output is what most reports paste, so the default
    summary lines must carry the triage payload (active-band count, floor);
    the raw arrays print only with -v."""
    silence_console(console)
    tuning = _report_tuning(synthetic_regulator([-6.0] * 20), 0.0)

    _report_parsed_profile(tuning, set())
    out = capsys.readouterr().out
    assert "limits 20 of 20 frequency bands" in out
    assert "full tables with -v" in out
    assert "threshold_high (dB):" not in out
    assert "Left:  [" not in out

    _report_parsed_profile(tuning, set(), verbose=True)
    out = capsys.readouterr().out
    assert "threshold_high (dB):" in out
    assert "zones, not per-band" in out
    assert "Left:  [" in out


@pytest.mark.parametrize("is_soundwire,disabled,expect", [
    (True, set(), True),
    (True, {"bass-enhancer"}, False),
    (False, set(), False),
])
def test_report_names_the_bass_enhancer_it_ships(silence_console, capsys,
                                                 is_soundwire, disabled,
                                                 expect):
    """The SoundWire build adds a converter-side bass enhancer with no XML
    source; the run must say so inline — it was the one active stage the
    --disable menu offered to drop that the output had never mentioned."""
    silence_console(console)
    _report_parsed_profile(
        _report_tuning(synthetic_regulator([-6.0] * 20), 0.0),
        disabled, is_soundwire=is_soundwire)
    assert ("Bass enhancer:" in capsys.readouterr().out) is expect


def _peaked_ao(peak_band, peak_db):
    """A 20-band AO curve that is flat at 0 dB except for one boosted band."""
    ao = [0.0] * 20
    ao[peak_band] = peak_db
    return ao


# `iso` is the isolated_band array, and since 2026-08-11 it is load-bearing
# here: coupled-bands is on by default, so a zone the tuning leaves at full
# scale AND marks non-isolated (iso 0) is now limited — which is exactly the
# exposure this finding warns about. All-1s means nothing qualifies, so the
# warning describes a band that really is left alone.
@pytest.mark.parametrize(
    "peak_db,peak_limited,volmax,iso,disabled,enabled,expect_warn", [
        (12.0, False, 9.0, 1, set(), set(), True),   # issue #46's T495 shape
        (12.0, True, 9.0, 1, set(), set(), False),   # regulator covers the peak
        (9.0, False, 9.0, 1, set(), set(), False),   # boost short of the range
        (12.0, False, 0.0, 1, set(), set(), False),  # no volmax riding on top
        (12.0, False, 9.0, 1, {"volmax"}, set(), False),
        # iso 0 -> the default coupled-bands mapping limits that zone, so the
        # boost is no longer unprotected and the warning must go quiet. This
        # is the row that used to pass enabled={"coupled-bands"}.
        (12.0, False, 9.0, 0, set(), set(), False),
        # ...and opting out of it puts the exposure back.
        (12.0, False, 9.0, 0, {"coupled-bands"}, set(), True),
        # --enable level-restore adds the peak back as gain, so a boost short of
        # the range now does reach the brickwall and has to warn (issue #50).
        (9.0, False, 9.0, 1, set(), {"level-restore"}, True),
        (9.0, True, 9.0, 1, set(), {"level-restore"}, False),  # still limited
    ])
def test_report_warns_when_biggest_boost_lands_on_an_unlimited_band(
        silence_console, capsys, peak_db, peak_limited, volmax, iso, disabled,
        enabled, expect_warn):
    """Issue #46: the regulator limits *somewhere*, so the all-inert warning
    stays quiet, but the band carrying the largest correction boost is one it
    leaves alone — that boost plus volmax hits the brickwall unprotected."""
    silence_console(console)
    peak_band = 1
    th = [-6.0 if peak_limited else 0.0] * 20
    th[10] = -6.0                              # limits somewhere regardless
    ao = _peaked_ao(peak_band, peak_db)
    _report_parsed_profile(
        _report_tuning(synthetic_regulator(th, isolated_band=[iso] * 20), volmax,
                       ao_db=ao),
        disabled, enabled=enabled)
    out = capsys.readouterr().out
    # Assert on the slug, not on prose: "leaves unlimited" also occurs in the
    # regulator section's coupled-bands line, and which of the two a wrap
    # breaks across lines is not a fact this test should depend on.
    assert ("[boost-unlimited]" in out) is expect_warn
    if expect_warn:                            # names the offending band
        assert f"{SYNTHETIC_FREQS_20[peak_band]} Hz" in out


def test_report_unlimited_boost_warning_uses_the_xml_declared_range(
        silence_console, capsys):
    """The gate is this XML's own stated per-band gain range, not a fixed
    +12 dB: a file declaring a wider range needs a bigger boost to trip it."""
    silence_console(console)
    th = [0.0] * 20
    th[10] = -6.0
    ao = _peaked_ao(1, 12.0)
    _report_parsed_profile(
        _report_tuning(synthetic_regulator(th, isolated_band=[1] * 20), 9.0,
                       geq_max_range=256,      # 16 dB range; +12 is mid-scale
                       ao_db=ao),
        set())
    assert "[boost-unlimited]" not in capsys.readouterr().out


@pytest.mark.parametrize("profile_used,declared,expect_note", [
    ("dynamic", "music", True),     # issue #46: XML ships on music, we build dynamic
    ("music", "music", False),      # already building what Dolby names
    ("dynamic", None, False),       # the common case — no declaration at all
])
def test_report_notes_dolby_declared_default_profile(
        silence_console, capsys, profile_used, declared, expect_note):
    """<setting><default_profile> names the profile the device ships on under
    Windows. We still build the first profile, so say when they differ."""
    silence_console(console)
    findings = _report_parsed_profile(
        _report_tuning(synthetic_regulator([-6.0] * 20), 6.0,
                       profile_used=profile_used, default_profile=declared),
        set())
    out = capsys.readouterr().out
    # The two halves: the mismatch is explained where it is detected, and the
    # rebuild to try is held back for the closing block.
    asks = [f.ask for f in findings if f.slug == "profile-mismatch"]
    assert bool(asks) is expect_note
    assert any("--profile music" in a for a in asks) is expect_note
    # Names what it built, not just what to try — otherwise there is nothing
    # to compare the rebuild against.
    assert any(f"'{profile_used}'" in a for a in asks) is expect_note
    if expect_note:
        assert declared in out, "the mismatch is explained where it is found"


def test_parse_xml_reads_declared_default_profile(tmp_path):
    """The field is read off <setting>, and its absence stays None rather than
    becoming a phantom mismatch."""
    from tests.conftest import write_synthetic_tuning_xml
    import dolby_to_easyeffects as d

    declared = write_synthetic_tuning_xml(tmp_path / "a.xml", default_profile="music")
    tuning = parse.parse_xml(declared)
    assert tuning.default_profile == "music"
    assert tuning.profile_used == "dynamic"

    plain = write_synthetic_tuning_xml(tmp_path / "b.xml")
    assert parse.parse_xml(plain).default_profile is None


# --- LOCK-IN: audio-optimizer-enable=0 must suppress the correction curve ---

def _xml_with_ao_enable(tmp_path, name, value):
    """Synthetic XML with <audio-optimizer-enable> set to `value` (or omitted
    when None), so the enable gate can be exercised on its own."""
    from tests.conftest import write_synthetic_tuning_xml

    path = write_synthetic_tuning_xml(tmp_path / name)
    text = path.read_text()
    if value is not None:
        text = text.replace(
            "<audio-optimizer-bands>",
            f'<audio-optimizer-enable value="{value}"/>\n        '
            "<audio-optimizer-bands>")
    path.write_text(text)
    return path


def test_audio_optimizer_curve_dropped_when_the_xml_disables_it(tmp_path):
    """A tuning can ship a correction curve and still declare the optimizer
    off (4091 corpus rows do). Applying it anyway emits a correction the
    tuning says not to apply, so the curve must go flat — and only the curve;
    the IEQ voicing is a separate stage."""
    import dolby_to_easyeffects as d

    off = parse.parse_xml(_xml_with_ao_enable(tmp_path, "off.xml", 0))
    assert off.ao_enabled is False
    assert off.ao_left == [0] * 20
    assert off.ao_right == [0] * 20
    # The IEQ curves this XML ships are untouched by the AO gate.
    assert any(v != 0 for v in off.curves["ieq_balanced"])


def test_audio_optimizer_curve_kept_when_enabled_or_absent(tmp_path):
    """Absent means enabled — the same convention speaker-peq-enable uses —
    so the gate can never silently flatten a tuning that never opted out."""
    import dolby_to_easyeffects as d

    for name, value in (("on.xml", 1), ("absent.xml", None)):
        tuning = parse.parse_xml(_xml_with_ao_enable(tmp_path, name, value))
        assert tuning.ao_enabled is True, name
        assert any(v != 0 for v in tuning.ao_left), name
        assert any(v != 0 for v in tuning.ao_right), name


def test_report_explains_a_flat_curve_caused_by_the_enable_gate(tmp_path, capsys):
    """Zeros in the printed curve would otherwise read as a flat tuning."""
    tuning = parse.parse_xml(_xml_with_ao_enable(tmp_path, "off.xml", 0))
    capsys.readouterr()
    _report_parsed_profile(tuning, set())
    assert "audio-optimizer-enable=0" in capsys.readouterr().out


# --- LOCK-IN: unreproducible stages are surfaced where users will see them ---

def test_leveler_substages_parsed_only_when_switched_on(tmp_path):
    """Dolby pairs sub-stages with its leveler that carry no parameters at
    all — only an on/off bit. They're recorded so the run can ask for the
    capture that would settle them."""
    from tests.conftest import write_synthetic_tuning_xml
    import dolby_to_easyeffects as d

    path = write_synthetic_tuning_xml(tmp_path / "sub.xml")
    path.write_text(path.read_text().replace(
        "<ieq-enable value=\"1\"/>",
        '<ieq-enable value="1"/>\n'
        '        <volume-leveler-compressor-enable value="1"/>\n'
        '        <volume-leveler-drc-enable value="0"/>'))
    assert parse.parse_xml(path).leveler_substages == ["volume-leveler-compressor"]

    plain = write_synthetic_tuning_xml(tmp_path / "plain.xml")
    assert parse.parse_xml(plain).leveler_substages == []


@pytest.mark.parametrize("autogain_on,expect", [
    (False, "only matters if you rebuild with --enable autogain"),
    (True, "swell then duck"),
])
def test_substage_summary_escalates_when_the_leveler_runs(autogain_on, expect):
    """Silent-but-present while the leveler is bypassed (it genuinely cannot
    be heard); the full evidence ask once the leveler actually runs.

    Keyed on the leveler running, not on the flag: SoundWire tunings ship it
    active without anyone passing --enable autogain, and the escalated
    wording must not tell those users they enabled something.
    """
    import dolby_to_easyeffects as d

    finding = report_findings._leveler_gap_finding(["volume-leveler-compressor"],
                                          autogain_on)
    assert expect in finding.detail
    assert "You enabled autogain" not in finding.detail
    # Carrying an ask is what marks a finding as worth acting on, and the
    # sub-stages only are once the leveler they hang off actually runs.
    assert bool(finding.ask) is autogain_on


def test_substage_summary_silent_when_there_is_nothing_to_say():
    import dolby_to_easyeffects as d

    assert report_findings._leveler_gap_finding([], autogain_on=True) is None


def test_findings_never_carry_a_url(capsys):
    """A URL inside wrapped prose gets broken mid-string and stops being
    clickable or copyable — which defeats the ask it belongs to.

    The previous version of this test only exercised the leveler sub-stage
    branch, which prints its URL on a line of its own and so was never at
    risk; the four watching-only notes embedded one in their prose and were
    silently being broken. The block now owns the single link, so the rule
    is simply that no finding may carry one, which this pins at the source.
    """
    import xml.etree.ElementTree as ET

    import dolby_to_easyeffects as d

    found = parse.collect_unmodeled_features(ET.fromstring("""
        <profile type="dynamic">
          <tuning-cp>
            <peak-level value="-3"/>
            <ieq-bands-set preset="ieq_warm"/>
            <regulator-overdrive value="5"/>
            <regulator-relaxation-amount value="80"/>
            <dynamic_speaker_optimization_enable value="1"/>
          </tuning-cp>
        </profile>
    """))
    assert len(found) == 5, "every watching-only row should have fired"
    for finding in found:
        assert "http" not in finding.detail + finding.ask, finding.slug

    # ...and the one link the block itself prints survives wrapping intact.
    report_findings.print_project_asks(found)
    lines = capsys.readouterr().out.splitlines()
    assert any(report_findings._REPORT_FORM_URL in line for line in lines)


# --- LOCK-IN: make_multiband_compressor split frequency from xover_idx ---

def test_mbc_band1_split_frequency_from_xover_idx():
    """A 2-band tuning with crossover idx 10 splits at freqs[10] (800 Hz
    on the synthetic grid); the sidechain low/highcut edges follow it."""
    mbc = make_multiband_compressor(
        synthetic_mb_comp(group_count=2, bands=[
            (10, -160, 16384, 30000, 32500, 0),
            (20, -160, 16384, 30000, 32500, 0),
        ]),
        SYNTHETIC_FREQS_20)
    assert mbc["band1"]["split-frequency"] == 800.0   # freqs[10]
    assert mbc["band1"]["enable-band"] is True
    assert mbc["band1"]["sidechain-lowcut-frequency"] == 800.0
    assert mbc["band0"]["sidechain-highcut-frequency"] == 800.0
    assert "split-frequency" not in mbc["band0"]


def test_mbc_out_of_range_xover_falls_back_to_500():
    mbc = make_multiband_compressor(
        synthetic_mb_comp(group_count=2, bands=[
            (25, -160, 16384, 30000, 32500, 0),   # 25 >= len(freqs) = 20
            (20, -160, 16384, 30000, 32500, 0),
        ]),
        SYNTHETIC_FREQS_20)
    assert mbc["band1"]["split-frequency"] == 500.0


# --- LOCK-IN: make_bass_enhancer scope derivation ---

@pytest.mark.parametrize("hp_freq,expected_scope", [
    (120.0, 240.0),   # scope = 2 * hp_freq
    (200.0, 300.0),   # capped at 300 Hz
])
def test_bass_enhancer_scope_tracks_hp_freq(hp_freq, expected_scope):
    be = make_bass_enhancer(hp_freq)
    assert be["scope"] == expected_scope
    assert be["amount"] == 12.0   # default drive


def test_preset_soundwire_no_hp_falls_back_to_100hz_scope():
    """make_preset's SoundWire path with no HP filter in the PEQ uses the
    100 Hz default -> scope 200."""
    preset, emitted = make_preset(kernel_name="X", peq_filters=[],
                                  is_soundwire=True)
    assert preset["output"]["bass_enhancer#0"]["scope"] == 200.0
    assert "bass-enhancer" in emitted


# --- LOCK-IN: no surround→stereo widening is emitted ---
# A 2026-06-13 DAX capture (design-notes entry 2) falsified the old
# surround-boost → stereo_tools widening: DAX applies no stereo widening on
# 2-ch content. The converter must never emit a stereo_tools stage, and
# `make_preset` must not accept a `surround` argument.

def test_no_stereo_tools_emitted(generated):
    preset, _ = generated
    order = preset["output"]["plugins_order"]
    assert not any("stereo_tools" in p for p in order), order
    assert "stereo_tools#0" not in preset["output"]


def test_make_preset_rejects_surround_kwarg():
    import inspect
    assert "surround" not in inspect.signature(make_preset).parameters


# --- LOCK-IN: convolver output-gain is always 0 ---
# The SoundWire 50%-headroom restore (peak_db * 0.5) is removed: it was
# calibrated against the pre-#13 chain whose 10x-over-applied IEQ inflated
# the FIR peak it compensated (design-notes unvalidated-scaling entry 3).
# The convolver must emit no gain of its own on any device family.

def test_convolver_output_gain_is_zero_for_all_devices():
    for is_soundwire in (False, True):
        preset, _ = make_preset(kernel_name="X", peq_filters=[],
                                is_soundwire=is_soundwire)
        assert preset["output"]["convolver#0"]["output-gain"] == 0.0


def test_make_preset_rejects_convolver_gain_kwarg():
    import inspect
    assert "convolver_gain" not in inspect.signature(make_preset).parameters


# --- --enable level-restore (issue #50) ---
# The convolver only ever attenuates: make_fir divides each channel by its
# own peak, so a tuning whose peak outruns its volmax-boost emits a preset
# quieter than bypass. The flag hands that measured peak back to the chain.
# It must stay strictly opt-in — fir_peak_db is passed on every run.

_RESTORE_SENTINEL = object()


def _restore_preset(fir_peak_db, volmax=6.0, enabled=None,
                    regulator=_RESTORE_SENTINEL, **kw):
    if regulator is _RESTORE_SENTINEL:
        regulator = synthetic_regulator([-6.0] * 20)
    return make_preset(kernel_name="X", peq_filters=[],
                       regulator=regulator,
                       freqs=SYNTHETIC_FREQS_20,
                       volmax_boost=volmax, fir_peak_db=fir_peak_db,
                       enabled=enabled or set(), **kw)


def test_level_restore_adds_the_fir_peak_to_the_static_boost():
    """The restored amount is exactly what make_fir divided out, and it
    rides the same slot as volmax-boost (issue #23 measured that placement
    at 0.06% THD against 11.6% straight into the brickwall)."""
    on, emitted = _restore_preset(9.2, enabled={"level-restore"})
    assert on["output"]["multiband_compressor#1"]["input-gain"] == 15.2
    assert on["output"]["multiband_compressor#1"]["output-gain"] == 0.0
    assert "level-restore-active" in emitted


def test_level_restore_is_inert_without_the_flag():
    """fir_peak_db reaches make_preset on every run, so a leak into the
    default path would ship silently."""
    off, emitted = _restore_preset(9.2)
    assert off["output"]["multiband_compressor#1"]["input-gain"] == 6.0
    assert "level-restore-active" not in emitted


def test_level_restore_asks_for_a_second_opinion_by_ear(tmp_path, capsys):
    """The flag has been heard on exactly one device (dev device,
    2026-08-18), where loud speech picked up artifacts. That is why it left
    EXPERIMENTAL_MARKERS: the run has to name what was heard and ask the
    next device the same question, not tell that reader nobody has heard it.
    """
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    dolby_to_easyeffects.main([str(xml), "--dry-run", "--skip-ee-check",
                               "--enable", "level-restore"])
    on = " ".join(capsys.readouterr().out.split())
    assert "loud speech picked up audible artifacts" in on
    assert "[level-restore]" in on

    # And it is gated on the flag: a default run neither warns nor asks.
    dolby_to_easyeffects.main([str(xml), "--dry-run", "--skip-ee-check"])
    off = " ".join(capsys.readouterr().out.split())
    assert "[level-restore]" not in off
    assert "picked up audible artifacts" not in off


def test_level_restore_falls_back_to_the_limiter_without_a_regulator():
    """Same fallback volmax-boost uses when the XML carries no regulator —
    otherwise the restore would silently vanish on those devices."""
    on, _ = _restore_preset(9.2, enabled={"level-restore"}, regulator=None)
    assert on["output"]["limiter#0"]["input-gain"] == 15.2


def test_level_restore_is_independent_of_disable_volmax():
    """The two terms are separate: dropping the tuning's loudness boost
    must not drop the level the impulse response was normalised by."""
    on, _ = _restore_preset(9.2, enabled={"level-restore"},
                            disabled={"volmax"})
    assert on["output"]["multiband_compressor#1"]["input-gain"] == 9.2


@pytest.mark.parametrize("fir_peak_db,volmax,expect_offer", [
    (9.2, 6.0, True),     # issue #50: the preset plays quieter than bypass
    (4.0, 6.0, False),    # volmax already covers what normalisation removed
    (6.0, 6.0, False),    # exactly covered — nothing to restore
])
def test_level_restore_offered_only_where_it_would_help(
        fir_peak_db, volmax, expect_offer):
    """Same contract as coupled-bands: the --enable menu names the flag
    only on the tunings where it does something."""
    _, emitted = _restore_preset(fir_peak_db, volmax=volmax)
    assert ("level-restore" in emitted) is expect_offer


def test_level_restore_re_references_both_channels(tmp_path, monkeypatch):
    """Normalising each channel to its own peak flattens the L/R level
    relationship the two AO curves ask for (7.2% of the corpus, up to
    5.56 dB). Under the flag both channels share the louder peak, so the
    relationship survives and neither channel exceeds full scale."""
    import subprocess
    import sys as _sys
    # ch_00 keeps the fixture's default curve; ch_01 is flat except for one
    # band, so the right channel's peak lands well below the left's.
    quiet_right = ",".join("32" if i == 5 else "0" for i in range(20))
    xml = write_synthetic_tuning_xml(
        tmp_path / "DEV_0287_SUBSYS_TESTTEST.xml", ao_right=quiet_right)

    def _peaks(*extra):
        out = tmp_path / f"o{len(extra)}"
        irs = tmp_path / f"i{len(extra)}"
        out.mkdir(); irs.mkdir()
        r = subprocess.run(
            [_sys.executable, str(SCRIPT), str(xml), "--output-dir", str(out),
             "--irs-dir", str(irs), "--skip-ee-check", "--no-color", *extra],
            capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        # Resolve the impulse through the preset: its name carries a hash.
        name = json.loads((out / "Dolby-Balanced.json").read_text()
                          )["output"]["convolver#0"]["kernel-name"]
        _, _, _, left, right = read_irs_file(irs / f"{name}.irs")
        # make_fir normalises the frequency response, not the time-domain
        # impulse, so the peak that matters is |H| — the time-domain sample
        # peak of a minimum-phase IR is unrelated.
        return tuple(float(np.max(np.abs(np.fft.rfft(ch, n=FIR_LENGTH))))
                     for ch in (left, right))

    off_l, off_r = _peaks()
    on_l, on_r = _peaks("--enable", "level-restore")
    # Default: both channels independently normalised to 0 dB, which is
    # what loses the relationship the two curves ask for.
    assert off_l == pytest.approx(1.0, rel=1e-4)
    assert off_r == pytest.approx(1.0, rel=1e-4)
    # Flag on: the louder channel still sits at 0 dB, the quieter one below
    # it by the difference between the two curves' peaks — and nothing
    # exceeds full scale, so the on-disk convention still holds.
    assert max(on_l, on_r) == pytest.approx(1.0, rel=1e-4)
    assert min(on_l, on_r) < 0.99


# --- --enable virtual-bass (issue #14) ---
# The stage is a parallel wet branch EasyEffects cannot express, so the flag
# only embeds a top-level `_vbe` metadata block (the `_generator` contract:
# EE ignores unknown top-level keys) that ee_to_pipewire.py builds from. The
# audio chain in the preset itself must be byte-identical either way.

def test_virtual_bass_flag_embeds_metadata_on_hda():
    """Scaled values trace the corpus-frozen XML fields exactly: src-freqs[0],
    mix-freqs, subgains[0..1]/16 with the -192 slot dropped as OFF, and
    overall-gain/16."""
    off, _ = make_preset(kernel_name="X", peq_filters=[],
                         virtual_bass=synthetic_virtual_bass())
    on, emitted = make_preset(kernel_name="X", peq_filters=[],
                              virtual_bass=synthetic_virtual_bass(),
                              enabled={"virtual-bass"})
    assert on["_vbe"] == {
        "src_lo_hz": 35.0,
        "mix_lo_hz": 94.0,
        "mix_hi_hz": 469.0,
        "arm_gains_db": [-2.0, -9.0],
        "overall_gain_db": 0.0,
    }
    assert "virtual-bass-active" in emitted
    assert on["output"] == off["output"]   # audio chain untouched by the flag


def test_virtual_bass_flag_is_a_noop_on_soundwire():
    """SoundWire tunings already ship bass_enhancer#0 for this gap; the flag
    must neither embed metadata nor offer itself in the menu there."""
    preset, emitted = make_preset(kernel_name="X", peq_filters=[],
                                  is_soundwire=True,
                                  virtual_bass=synthetic_virtual_bass(),
                                  enabled={"virtual-bass"})
    assert "_vbe" not in preset
    assert "virtual-bass-active" not in emitted
    assert "virtual-bass" not in emitted


def test_virtual_bass_offered_without_the_flag():
    """Same contract as level-restore: the --enable menu names the flag on
    tunings where it would do something, and only there."""
    _, emitted = make_preset(kernel_name="X", peq_filters=[],
                             virtual_bass=synthetic_virtual_bass())
    assert "virtual-bass" in emitted
    _, emitted = make_preset(kernel_name="X", peq_filters=[])
    assert "virtual-bass" not in emitted


def test_virtual_bass_all_floored_subgains_emit_nothing():
    """subgains of all -192 means every sub-band is off — no arms, no
    metadata, no menu row, so the had-no-effect warning can fire."""
    vb = synthetic_virtual_bass()
    vb["subgains"] = [-192, -192, -192]
    preset, emitted = make_preset(kernel_name="X", peq_filters=[],
                                  virtual_bass=vb,
                                  enabled={"virtual-bass"})
    assert "_vbe" not in preset
    assert "virtual-bass-active" not in emitted
    assert "virtual-bass" not in emitted


def test_parse_xml_reads_virtual_bass_family(tmp_path):
    """parse_xml carries the tuning-cp virtual-bass family raw (freqs in Hz,
    gains in 1/16 dB, the -192 floored slot intact)."""
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    tuning = parse.parse_xml(xml)
    assert tuning.virtual_bass == synthetic_virtual_bass()


# --- LOCK-IN: a run that bails out leaves the EasyEffects tree alone ---

def test_no_profiles_found_creates_no_directories(tmp_path, capsys):
    """`--all-profiles` on an endpoint the XML doesn't carry prints "No
    profiles found" and returns without generating anything — so it must
    not have planted the output and .irs directories on the way past. They
    were created above that return, which left two empty directories in the
    user's EasyEffects tree after a run that wrote nothing.
    """
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    # The fixture carries one endpoint, internal_speaker/normal.
    out = tmp_path / "out"
    irs = tmp_path / "irs"
    dolby_to_easyeffects.main(
        [str(xml), "--all-profiles", "--endpoint", "headphone",
         "--output-dir", str(out), "--irs-dir", str(irs)])
    # Confirms it is *this* early return that was taken, not another.
    assert "No profiles found" in capsys.readouterr().out
    assert not out.exists()
    assert not irs.exists()


# --- LOCK-IN: autoload artifacts (device binding + fallback preset/rc) ---

def test_write_autoload_filename_and_payload(tmp_path):
    path = write_autoload(
        tmp_path,
        device_name="alsa_output.pci-0000_00_1f.3/analog-stereo",
        device_description="Built-in Audio",
        device_profile="output:analog-stereo",
        preset_name="Dolby-Balanced")
    # '/' is path-unsafe: EE's AutoloadManager swaps it for '_' in the
    # filename only; the JSON payload keeps the original names.
    assert path.name == ("alsa_output.pci-0000_00_1f.3_analog-stereo"
                         ":output:analog-stereo.json")
    assert json.loads(path.read_text()) == {
        "device": "alsa_output.pci-0000_00_1f.3/analog-stereo",
        "device-description": "Built-in Audio",
        "device-profile": "output:analog-stereo",
        "preset-name": "Dolby-Balanced",
    }


def test_read_autoload_entries_round_trips_what_write_autoload_wrote(tmp_path):
    """The reader and the writer share the four key names; a round trip is
    what keeps them from drifting apart."""
    write_autoload(tmp_path, device_name="alsa_output.spk",
                   device_description="Speaker",
                   device_profile="Speaker", preset_name="Dolby-Balanced")
    assert read_autoload_entries(tmp_path) == [{
        "device": "alsa_output.spk",
        "device-description": "Speaker",
        "device-profile": "Speaker",
        "preset-name": "Dolby-Balanced",
    }]


def test_read_autoload_entries_skips_what_it_cannot_parse(tmp_path):
    """A diagnostic must not crash on a file EasyEffects itself tolerates.
    Malformed JSON, a non-object payload and a non-.json file are all
    'no mapping', and the good entry beside them still comes back."""
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "array.json").write_text("[1, 2]")
    (tmp_path / "notes.txt").write_text("ignored")
    write_autoload(tmp_path, device_name="alsa_output.spk",
                   device_description="Speaker", device_profile="Speaker",
                   preset_name="Dolby-Balanced")
    entries = read_autoload_entries(tmp_path)
    assert [e.get("device") for e in entries] == ["alsa_output.spk"]


def test_read_autoload_entries_missing_dir_is_empty_not_an_error(tmp_path):
    assert read_autoload_entries(tmp_path / "nope") == []


def test_write_bypass_preset_kept_when_existing(tmp_path):
    """A user's hand-built preset of the same name must never be
    clobbered — status 'kept', content untouched."""
    existing = tmp_path / "Bypass.json"
    existing.write_text('{"hand": "built"}')
    path, status = write_bypass_preset(tmp_path, "Bypass")
    assert status == "kept"
    assert path == existing
    assert path.read_text() == '{"hand": "built"}'


def test_write_bypass_preset_writes_minimal_preset(tmp_path):
    path, status = write_bypass_preset(tmp_path, "Bypass")
    assert status == "written"
    data = json.loads(path.read_text())
    assert data["output"] == {"blocklist": [], "plugins_order": []}
    assert data["_generator"].startswith("dolby_to_easyeffects.py ")


def test_set_autoload_fallback_already_configured_leaves_file_untouched(tmp_path):
    rc = tmp_path / "easyeffectsrc"
    original = ("[Window]\n"
                "outputAutoloadingFallbackPreset=HandPicked\n"
                "outputAutoloadingUsesFallback=true\n")
    rc.write_text(original)
    status, existing = set_autoload_fallback(rc, "Bypass")
    assert status == "already-configured"
    assert existing == "HandPicked"
    assert rc.read_text() == original   # file not rewritten


def test_set_autoload_fallback_patches_and_preserves_camelcase(tmp_path):
    rc = tmp_path / "easyeffectsrc"
    status, existing = set_autoload_fallback(rc, "Bypass")
    assert status == "patched"
    assert existing == ""
    text = rc.read_text()
    # optionxform=str: keys keep EE's camelCase (configparser's default
    # would lowercase them and EE would not see the setting). Written
    # with space_around_delimiters=False, matching EE's own format.
    assert "[Window]" in text
    assert "outputAutoloadingFallbackPreset=Bypass" in text
    assert "outputAutoloadingUsesFallback=true" in text


# --- doctor / self-diagnostic (issue #22) ---
#
# A generated preset can be flawless yet inaudible because of the environment
# it lands in. These lock in the verdict logic for each failure class. Several
# are trap regressions: the EE-7 detection and the missing-/legacy-kernel
# checks guard the exact #22 silent-convolver mechanism.

@pytest.mark.parametrize("text,expected", [
    ("easyeffects 8.2.1", (8, 2, 1)),     # verified native --version format
    ("easyeffects 7.1.5", (7, 1, 5)),     # EE 7
    ("easyeffects 7.0.0", (7, 0, 0)),
    ("EasyEffects 7.2", (7, 2, 0)),       # prefix/case drift; patch defaults 0
    ("7.1.4", (7, 1, 4)),                 # bare
    ("Version: 8.1.0", (8, 1, 0)),        # flatpak info Version: line
    ("", None),
    ("command not found", None),
])
def test_parse_ee_version(text, expected):
    assert parse_ee_version(text) == expected


def test_ee_version_7_is_loud_fail():
    """TRAP (#22): EE 7 can't read the v8 preset format — the convolver loads
    no kernel and the preset is silently inaudible. Every parsed 7.x must FAIL
    (the only loud banner), citing the v8 fix."""
    r = ee_version_status((7, 1, 5), found=True)
    assert r.status == DOCTOR_FAIL
    assert "EasyEffects 8" in r.detail


def test_ee_version_8_passes():
    assert ee_version_status((8, 2, 1), found=True).status == DOCTOR_PASS


def test_ee_version_not_found_warns_not_fails():
    """No EE at all is a valid 'generating for another machine' case — WARN,
    never the loud FAIL banner."""
    assert ee_version_status(None, found=False).status == DOCTOR_WARN


def test_ee_version_unparseable_is_unknown_not_fail():
    """Installed but version unreadable must not trigger the loud <8 banner."""
    assert ee_version_status(None, found=True).status == DOCTOR_UNKNOWN


def test_ee_version_7_keeps_the_install_command_out_of_the_prose():
    """The remedy has to reach the check as `steps`, not as a sentence.

    `emit_check` wraps a detail to the terminal width, and a command folded
    across two lines stops being runnable — which is the whole reason the
    caller builds the command and hands it over separately. The detail may
    lead up to it ("Install EasyEffects 8:") and must not contain it.
    """
    steps = (("cta", "  sudo apt install easyeffects"),)
    r = ee_version_status((7, 1, 5), found=True, install_steps=steps)
    assert r.status == DOCTOR_FAIL
    assert r.steps == steps
    assert "sudo apt" not in r.detail
    assert r.detail.rstrip().endswith("Install EasyEffects 8:")


# --- which EasyEffects 8 to install, per machine ---------------------------
#
# The offer used to be a hand-written sentence naming the releases that still
# shipped 7.x. These pin the thing that replaced it: ask the machine, and name
# its package only when the answer is 8.

def _distro_ships(monkeypatch, major, fam=None):
    """Pin what this machine's package manager would answer, and who it is.

    Both halves, or the assertions read the developer's own /etc/os-release
    and package lists and the suite starts passing or failing on where it ran
    — and on a Debian box it would shell out to `apt-cache` to do it.
    """
    monkeypatch.setattr(packages, "family",
                        lambda *a, **k: packages.DEBIAN if fam is None else fam)
    monkeypatch.setattr(doctor_run, "_distro_easyeffects_major",
                        lambda _fam: major)


# Every verb that would name a distribution's own package manager.
_PACKAGE_MANAGERS = ("apt", "dnf", "pacman", "zypper", "apk", "emerge")

# The one offer that is right on every machine, and the only line here that is
# matched whole: the captions around it are copy, but this is a command.
_FLATHUB_COMMAND = "flatpak install flathub com.github.wwmm.easyeffects"


def _step_commands(steps):
    """The offered lines, stripped of whatever margin the printer wants.

    Matched on content rather than on the exact indent, because the indent is
    the caller's to change — what these tests are about is which commands are
    offered, and in what order.
    """
    return [text.strip() for _style, text in steps]


def test_a_distro_that_still_ships_7_is_never_named(monkeypatch):
    """Naming a package that installs 7.x is worse than naming none.

    It installs cleanly, loads the preset, and leaves the speaker-correction
    convolver doing nothing — the exact silent failure this check exists to
    catch, now arrived at by following our own advice. So when the machine
    says 7, only the remedy that doesn't depend on the distribution is
    offered, and the reader is told why their package manager went unnamed —
    with the version, not "older than 8, or couldn't be checked", which left
    them unable to tell whether upgrading their distribution would help.
    """
    _distro_ships(monkeypatch, 7)
    steps = doctor_run.easyeffects_install_steps()
    assert _FLATHUB_COMMAND in _step_commands(steps), steps
    assert not any(m in t for _s, t in steps for m in _PACKAGE_MANAGERS), steps
    assert any(s == "dim" and "ships EasyEffects 7" in t
               for s, t in steps), steps

    # And the other way of not knowing says *that*, rather than sharing one
    # sentence with it.
    _distro_ships(monkeypatch, None)
    steps = doctor_run.easyeffects_install_steps()
    assert any(s == "dim" and "couldn't ask" in t for s, t in steps), steps


@pytest.mark.parametrize("fam,command", [
    (packages.DEBIAN, "sudo apt install easyeffects"),
    (packages.ARCH, "sudo pacman -S easyeffects"),
])
def test_a_distro_that_ships_8_is_offered_ahead_of_the_flatpak(
        fam, command, monkeypatch):
    """Both, never one — and in this order.

    The distro package is the shorter path for a reader who has one, so it
    leads; the Flathub line stays because it is the answer for a machine we
    couldn't place or couldn't ask, and dropping it here would mean the offer
    depended on a query that is allowed to fail.

    The bullet carries the version it found, because this is the one line that
    could be wrong: a reader whose distribution shipped 7 has to be able to
    see that the run asked rather than guessed.
    """
    _distro_ships(monkeypatch, 8, fam)
    steps = doctor_run.easyeffects_install_steps()
    offered = _step_commands(steps)
    assert command in offered and _FLATHUB_COMMAND in offered, steps
    assert offered.index(command) < offered.index(_FLATHUB_COMMAND), steps
    assert any("EasyEffects 8" in t for _s, t in steps), steps
    # The note explaining why no package manager was named belongs only to the
    # run where none was — the Flatpak-setup caveat below it is unconditional
    # and is not that note.
    assert not any("couldn't ask" in t or "ships EasyEffects" in t
                   for _s, t in steps
                   if "which has EasyEffects" not in t), steps


def test_nixos_is_offered_nixpkgs_when_nixpkgs_has_8(monkeypatch):
    """A family whose answer is a configuration edit still gets to answer.

    NixOS has no "install this" verb, so the bullet that names a distribution's
    own package was skipped for it and the Flatpak was all it ever saw — on the
    one platform where Flatpak is *least* likely to already work, and while
    nixpkgs itself ships a current EasyEffects. The offer is gated on the same
    query as everyone else's: nixpkgs is asked, and only an answer of 8 or
    newer puts the route on screen.
    """
    _distro_ships(monkeypatch, 8, packages.NIXOS)
    steps = doctor_run.easyeffects_install_steps()
    rendered = " ".join(t for _s, t in steps)
    assert "environment.systemPackages" in rendered, steps
    assert "pkgs.easyeffects" in rendered, steps
    assert "sudo nixos-rebuild switch" in _step_commands(steps), steps
    # And the Flatpak stays, because it is the answer when the query fails.
    assert _FLATHUB_COMMAND in _step_commands(steps), steps

    # Nothing to offer, nothing offered: a flakes-only machine has no
    # <nixpkgs> for the query to read, and guessing "8" there would send the
    # reader to a rebuild that changes nothing.
    _distro_ships(monkeypatch, None, packages.NIXOS)
    steps = doctor_run.easyeffects_install_steps()
    assert "environment.systemPackages" not in " ".join(t for _s, t in steps)
    assert _FLATHUB_COMMAND in _step_commands(steps), steps


@pytest.mark.parametrize("release,expected", [
    ("6.12.74+deb13+1-amd64", (6, 12)),   # Debian/LMDE style (#33 reporter)
    ("6.15.8-arch1-1", (6, 15)),          # Arch style
    ("5.10.0-35-generic", (5, 10)),       # Ubuntu style
    ("7.0.0", (7, 0)),
    ("", None),
    ("command not found", None),
])
def test_parse_kernel_series(release, expected):
    assert parse_kernel_series(release) == expected


# kernel_age_status tests pin `today` so they never go stale as wall-clock
# time passes. 2026-07-21 is the day #33 closed: 6.12 was 20 months old.
_KERNEL_TODAY = date(2026, 7, 21)


def test_kernel_age_old_lts_warns():
    """TRAP (#33): Debian 13's 6.12 at 20 months mis-drove the reporter's
    speaker amp — the preset was blameless. The verdict must WARN, name the
    release month, and give the confirm-symptom (bad even with EE off)."""
    r = kernel_age_status("6.12.74+deb13+1-amd64", today=_KERNEL_TODAY)
    assert r.status == DOCTOR_WARN
    assert "2024-11" in r.detail
    assert "EasyEffects off" in r.detail


def test_kernel_age_recent_passes():
    r = kernel_age_status("7.1.3-arch1-1", today=_KERNEL_TODAY)
    assert r.status == DOCTOR_PASS
    assert "2026-06" in r.detail


def test_kernel_age_boundary_18_months():
    """6.13 (2025-01) is exactly 18 months old in 2026-07 → still PASS; one
    month later → WARN."""
    assert kernel_age_status("6.13.0", today=_KERNEL_TODAY).status == DOCTOR_PASS
    assert kernel_age_status("6.13.0",
                             today=date(2026, 8, 1)).status == DOCTOR_WARN


def test_kernel_age_newer_than_table_passes():
    """A series this copy of the tool doesn't know is assumed recent — an
    aging table must never flag a brand-new kernel."""
    assert kernel_age_status("9.0.0-future", today=_KERNEL_TODAY).status == DOCTOR_PASS


def test_kernel_age_pre_table_warns():
    assert kernel_age_status("4.19.0-27-amd64",
                             today=_KERNEL_TODAY).status == DOCTOR_WARN


def test_kernel_age_unparseable_is_unknown():
    assert kernel_age_status("weird", today=_KERNEL_TODAY).status == DOCTOR_UNKNOWN


@pytest.mark.parametrize("flatpak,native,base_fp,ee_fp,expected", [
    (False, True, False, False, DOCTOR_PASS),    # native install, native run
    (True, False, True, True, DOCTOR_PASS),      # flatpak install, flatpak run
    (True, True, False, None, DOCTOR_WARN),      # both dirs present, run unknown
    (False, False, False, None, DOCTOR_WARN),    # no data dir yet
    (False, True, False, True, DOCTOR_WARN),     # write native, detected flatpak → mismatch (WARN, not FAIL)
])
def test_install_status(flatpak, native, base_fp, ee_fp, expected):
    r = install_status(flatpak, native, base_fp, "~/somewhere", ee_fp)
    assert r.status == expected


def test_install_mismatch_warns_not_fails():
    """Detected EE build differs from where we wrote. WARN (not FAIL): on a
    dual-install box the install that answered the probe isn't necessarily the
    one the user launches, so we can't assert 'never sees them' with certainty."""
    r = install_status(flatpak_exists=True, native_exists=True,
                       base_is_flatpak=False, base_display="~/.local/share/easyeffects",
                       ee_is_flatpak=True)
    assert r.status == DOCTOR_WARN
    assert "won't see them" in r.detail


def test_check_preset_kernel_resolves():
    preset = {"output": {"convolver#0": {"kernel-name": "Dolby-Balanced"}}}
    assert check_preset_kernel(preset, {"Dolby-Balanced"}, "Dolby-Balanced").status == DOCTOR_PASS


def test_check_preset_kernel_missing_irs_fails():
    """TRAP (#22): kernel-name with no matching .irs = silent passthrough."""
    preset = {"output": {"convolver#0": {"kernel-name": "Dolby-Balanced"}}}
    r = check_preset_kernel(preset, set(), "Dolby-Balanced")
    assert r.status == DOCTOR_FAIL
    assert "silent" in r.detail


def test_check_preset_kernel_no_kernel_name_fails():
    preset = {"output": {"convolver#0": {}}}
    assert check_preset_kernel(preset, set(), "X").status == DOCTOR_FAIL


def test_check_preset_kernel_legacy_kernel_path_fails():
    """TRAP: the deprecated EE-7 'kernel-path' key → FAIL (CLAUDE.md: EE 8.x
    wants kernel-name)."""
    preset = {"output": {"convolver#0": {"kernel-path": "/x.irs"}}}
    r = check_preset_kernel(preset, set(), "X")
    assert r.status == DOCTOR_FAIL
    assert "EasyEffects 8" in r.detail


def test_check_preset_kernel_bypassed_warns():
    preset = {"output": {"convolver#0": {"kernel-name": "K", "bypass": True}}}
    assert check_preset_kernel(preset, {"K"}, "K").status == DOCTOR_WARN


def test_check_preset_kernel_no_convolver_warns():
    assert check_preset_kernel({"output": {"equalizer#0": {}}}, set(), "X").status == DOCTOR_WARN


def test_check_preset_kernel_invalid_preset_fails():
    assert check_preset_kernel({"no_output": 1}, set(), "X").status == DOCTOR_FAIL


def test_check_real_generated_preset_resolves(generated):
    """Tie the doctor to the actual generator output: a real make_preset +
    save_wav_stereo pair must resolve. A future make_convolver key rename
    turns this red."""
    preset, irs_path = generated
    r = check_preset_kernel(preset, {irs_path.stem}, irs_path.stem)
    assert r.status == DOCTOR_PASS


def test_read_ee_rc_extracts_verified_keys():
    """The live EE 8.x rc: loaded preset under [Presets], fallback under
    [Window], sink + chain under [StreamOutputs]."""
    rc = ("[Presets]\n"
          "lastLoadedOutputPreset=Dolby-Balanced\n"
          "[Window]\n"
          "outputAutoloadingFallbackPreset=Nothing\n"
          "outputAutoloadingUsesFallback=true\n"
          "autostartOnLogin=true\n"
          "enableServiceMode=false\n"
          "[StreamOutputs]\n"
          "outputDevice=alsa_output.spk\n"
          "plugins=convolver#0,equalizer#0,limiter#0\n")
    d = read_ee_rc(rc)
    assert d["last_output_preset"] == "Dolby-Balanced"
    assert d["fallback_preset"] == "Nothing"
    assert d["uses_fallback"] is True
    assert d["autostart_on_login"] is True
    # enableServiceMode=false is written only when toggled off → service_mode False
    assert d["service_mode"] is False
    assert d["output_device"] == "alsa_output.spk"
    assert d["output_plugins"] == ["convolver#0", "equalizer#0", "limiter#0"]


def test_read_ee_rc_missing_sections_default_safely():
    d = read_ee_rc("")
    assert d["last_output_preset"] == ""
    assert d["uses_fallback"] is False
    assert d["autostart_on_login"] is False
    # opposite polarity to autostart: an absent enableServiceMode is the ON default
    assert d["service_mode"] is True
    assert d["output_plugins"] == []


def test_read_ee_rc_garbage_does_not_crash():
    assert read_ee_rc("}{ not ini ][").get("last_output_preset") == ""


def test_read_ee_rc_use_default_output_device_absent_is_true():
    """TRAP: written only when toggled OFF, so an absent key is the ON default
    — same polarity as enableServiceMode. Reading it as False would treat every
    default install as having pinned EE to a device, and report a stale sink."""
    assert read_ee_rc("")["use_default_output_device"] is True
    rc = "[StreamOutputs]\nuseDefaultOutputDevice=false\n"
    assert read_ee_rc(rc)["use_default_output_device"] is False


def test_read_ee_rc_bypass_absent_is_false():
    """[EffectsPipelines] bypass defaults to false and is only written when
    toggled on. (It does exist — this file used to claim global bypass was
    GUI-only state with no key at all.)"""
    assert read_ee_rc("")["bypass"] is False
    assert read_ee_rc("[EffectsPipelines]\nbypass=true\n")["bypass"] is True


def test_loaded_preset_generated_passes():
    rc = {"last_output_preset": "Dolby-Balanced", "fallback_preset": "", "uses_fallback": False}
    assert loaded_preset_status(rc, ["Dolby-Balanced", "Nothing"]).status == DOCTOR_PASS


def test_loaded_preset_non_generated_warns():
    rc = {"last_output_preset": "SomethingElse", "fallback_preset": "", "uses_fallback": False}
    assert loaded_preset_status(rc, ["Dolby-Balanced"]).status == DOCTOR_WARN


@pytest.mark.parametrize("kind", ["unknown", "speaker"],
                         ids=["no-answer", "on-the-speakers"])
def test_loaded_preset_bypass_selected_warns(kind):
    """TRAP: the silent 'Nothing' bypass preset is in the generated set but is
    itself a 'sounds like nothing' state — it must WARN, not PASS.

    Both halves are load-bearing now that a confident non-speaker output
    softens this (see below). 'speaker' is the case the WARN exists for;
    'unknown' is the default, and covers a failed probe, a disconnected sink
    and EasyEffects' own virtual one — none of which are evidence the
    speakers are fine."""
    rc = {"last_output_preset": BYPASS_PRESET_NAME, "fallback_preset": "",
          "uses_fallback": False}
    r = loaded_preset_status(rc, ["Dolby-Balanced", BYPASS_PRESET_NAME],
                             output_kind=kind)
    assert r.status == DOCTOR_WARN
    assert BYPASS_PRESET_NAME in r.detail


def test_loaded_preset_bypass_off_the_speakers_passes_naming_the_autoload():
    """'Nothing' on a non-speaker output is what --autoload deliberately
    installs, so it is not a fault — and the reader's real question is what
    happens when they switch back, which the autoload entry answers."""
    rc = {"last_output_preset": BYPASS_PRESET_NAME, "fallback_preset": "",
          "uses_fallback": False}
    r = loaded_preset_status(rc, ["Dolby-Balanced", BYPASS_PRESET_NAME],
                             output_kind="other",
                             speaker_preset="Dolby-Balanced")
    assert r.status == DOCTOR_PASS
    assert "Dolby-Balanced" in r.detail
    # No instruction to load a speaker tuning: the run itself refuses to put
    # one on a non-speaker output, so the doctor must not ask for it either.
    assert "Load a Dolby-* preset" not in r.detail


def test_loaded_preset_bypass_off_the_speakers_without_autoload_is_unknown():
    """Nothing points the speakers anywhere, so the check could not be made.
    UNKNOWN, not PASS: a green line would claim the speakers are fine when
    nothing looked."""
    rc = {"last_output_preset": BYPASS_PRESET_NAME, "fallback_preset": "",
          "uses_fallback": False}
    r = loaded_preset_status(rc, ["Dolby-Balanced", BYPASS_PRESET_NAME],
                             output_kind="other", speaker_preset="")
    assert r.status == DOCTOR_UNKNOWN


def test_loaded_preset_unknown_never_opens_a_sentence_with_the_preset_name():
    """TRAP (/user-review 2026-08-29): the detail said "Nothing autoloads a
    Dolby-* preset on the speakers" three words after quoting the 'Nothing'
    preset, and a first-time reader parsed it as a claim about that preset
    rather than as "no preset does". Any sentence here starting with the bare
    word is the same trap — it is a proper noun in this report."""
    rc = {"last_output_preset": BYPASS_PRESET_NAME, "fallback_preset": "",
          "uses_fallback": False}
    detail = loaded_preset_status(rc, ["Dolby-Balanced", BYPASS_PRESET_NAME],
                                  output_kind="other",
                                  speaker_preset="").detail
    sentences = [t.strip() for t in re.split(r"(?<=[.?!])\s+", detail)]
    assert not any(t.startswith(f"{BYPASS_PRESET_NAME} ") for t in sentences), \
        sentences
    # The reader is told what to do about it, not just that it failed.
    assert "--autoload" in detail


def test_loaded_preset_bypass_off_the_speakers_ignores_a_foreign_autoload():
    """TRAP: the autoload entry is EasyEffects', not ours — it can name a
    preset this script never generated. That is not a verified speaker
    tuning, so it must not buy a PASS."""
    rc = {"last_output_preset": BYPASS_PRESET_NAME, "fallback_preset": "",
          "uses_fallback": False}
    r = loaded_preset_status(rc, ["Dolby-Balanced", BYPASS_PRESET_NAME],
                             output_kind="other",
                             speaker_preset="SomebodyElses")
    assert r.status == DOCTOR_UNKNOWN
    assert "SomebodyElses" not in r.detail


@pytest.mark.parametrize("kind", ["unknown", "speaker", "other"])
def test_loaded_preset_other_branches_ignore_the_output_kind(kind):
    """The softening is scoped to the bypass preset. A Dolby preset still
    passes and a foreign one still warns wherever the audio is going."""
    good = {"last_output_preset": "Dolby-Balanced", "fallback_preset": "",
            "uses_fallback": False}
    bad = {"last_output_preset": "SomethingElse", "fallback_preset": "",
           "uses_fallback": False}
    names = ["Dolby-Balanced", BYPASS_PRESET_NAME]
    assert loaded_preset_status(
        good, names, output_kind=kind,
        speaker_preset="Dolby-Balanced").status == DOCTOR_PASS
    assert loaded_preset_status(
        bad, names, output_kind=kind,
        speaker_preset="Dolby-Balanced").status == DOCTOR_WARN


def test_loaded_preset_names_the_matched_fallback_not_the_loaded():
    """When the PASS is due to the fallback, the message names the fallback,
    not a non-generated last-loaded preset."""
    rc = {"last_output_preset": "SomethingElse", "fallback_preset": "Dolby-Balanced",
          "uses_fallback": True}
    r = loaded_preset_status(rc, ["Dolby-Balanced"])
    assert r.status == DOCTOR_PASS
    assert "Dolby-Balanced" in r.detail and "SomethingElse" not in r.detail


def test_loaded_preset_live_answer_beats_the_config_file():
    """TRAP: EasyEffects only writes its config from saveAll() — on quit and on
    an autosave timer that ticks only while its window is open. In service mode
    the file can name the silent bypass preset for hours while a Dolby one is
    loaded, which made this check WARN at a user whose audio was fine."""
    rc = {"last_output_preset": BYPASS_PRESET_NAME, "fallback_preset": "",
          "uses_fallback": False}
    r = loaded_preset_status(rc, ["Dolby-Balanced", BYPASS_PRESET_NAME],
                             live_preset="Dolby-Balanced")
    assert r.status == DOCTOR_PASS
    assert "Dolby-Balanced" in r.detail


def test_loaded_preset_live_answer_ignores_the_fallback_key():
    """A live reading is the outcome autoloading already reached, so the
    fallback key must not rescue a preset EE says isn't loaded."""
    rc = {"last_output_preset": "Dolby-Balanced", "fallback_preset": "Dolby-Balanced",
          "uses_fallback": True}
    r = loaded_preset_status(rc, ["Dolby-Balanced"], live_preset="SomethingElse")
    assert r.status == DOCTOR_WARN
    assert "SomethingElse" in r.detail


def test_background_service_both_on_passes():
    r = autostart_status({"autostart_on_login": True, "service_mode": True})
    assert r.status == DOCTOR_PASS


def test_background_service_service_off_warns_naming_only_service():
    """TRAP: autostart on but service mode off is a false-PASS risk — the preset
    stops once the window closes. WARN, and name service mode, NOT autostart."""
    r = autostart_status({"autostart_on_login": True, "service_mode": False})
    assert r.status == DOCTOR_WARN
    assert "Enable service mode" in r.detail
    assert "Autostart on login" not in r.detail


def test_background_service_autostart_off_warns_naming_only_autostart():
    r = autostart_status({"autostart_on_login": False, "service_mode": True})
    assert r.status == DOCTOR_WARN
    assert "Autostart on login" in r.detail
    assert "Enable service mode" not in r.detail


def test_background_service_both_off_warns_naming_both():
    r = autostart_status({"autostart_on_login": False, "service_mode": False})
    assert r.status == DOCTOR_WARN
    assert "Enable service mode" in r.detail and "Autostart on login" in r.detail


def test_background_service_missing_keys_warn_safely():
    """A partial/older dict must not KeyError; missing flags fall to the
    warn-safe direction."""
    assert autostart_status({}).status == DOCTOR_WARN


def test_doctor_summary_counts():
    checks = [CheckResult(DOCTOR_FAIL, "a", ""), CheckResult(DOCTOR_WARN, "b", ""),
              CheckResult(DOCTOR_PASS, "c", ""), CheckResult(DOCTOR_PASS, "d", ""),
              CheckResult(DOCTOR_UNKNOWN, "e", "")]
    assert summarize(checks) == (1, 1, 2, 1)


def _gate(on):
    import dolby_to_easyeffects as d
    return speakers.FirmwareGate("0", "sofhdadsp", "3", "CARD",
                          "Speaker Force Firmware Load", on=on)


@pytest.mark.parametrize("gates,expected", [
    ([], None),                                   # no such control: say nothing
    ([_gate(True)], DOCTOR_PASS),
    ([_gate(False)], DOCTOR_WARN),
    ([_gate(True), _gate(False)], DOCTOR_WARN),   # any gate off is a suspect
])
def test_firmware_gate_status(gates, expected):
    result = firmware_gate_status(gates)
    assert (result.status if result else None) == expected


def test_firmware_gate_check_carries_the_command_that_switches_it_on():
    """It used to name the section further down instead. One command per gate
    that is off, and none on a PASS — there is nothing to switch."""
    import dolby_to_easyeffects as d

    off, on = _gate(False), _gate(True)
    check = firmware_gate_status([off, on])
    assert check.steps == (("cta", speakers.amixer_enable_cmd(off)),)
    assert "section below" not in check.detail
    assert firmware_gate_status([on]).steps == ()


# --- an empty gate list means two opposite things (amixer absent) ----------
#
# `[]` is both "this machine has no such control" and "nothing looked", and
# the report rendered both as silence. Nothing in the PipeWire stack pulls
# `alsa-utils` in — on Debian it arrives only as a Recommends of the desktop
# task — so a minimal or container install genuinely has no amixer.

def test_an_empty_gate_list_is_only_silence_when_something_looked(monkeypatch):
    """Both halves, because they used to be the same `[]`.

    Staying quiet is right when the scan ran and found no gate: most machines
    have none, and a check about an absent control is noise. It is wrong when
    amixer is missing, because the reader then gets a report that says "no
    blocking problems" about the one thing most likely to explain silent or
    thin speakers — a claim nothing checked.
    """
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.DEBIAN)
    assert firmware_gate_status([], checked=True) is None

    unchecked = firmware_gate_status([], checked=False)
    assert unchecked.status == DOCTOR_UNKNOWN
    assert "amixer" in unchecked.detail
    # And it hands over the fix, unwrapped, like every other check that can.
    assert ("cta", "sudo apt install alsa-utils") in unchecked.steps


def test_a_gate_that_is_off_outranks_a_missing_amixer():
    """A found gate is proof the scan ran, so the missing tool stops mattering.

    Only the empty list is ambiguous. If `checked` ever started deciding the
    verdict on its own, a real off gate — the thing that mutes the woofers
    upstream of everything the preset does — would be reported as "couldn't
    check", and the command that switches it on would go with it.
    """
    off = _gate(False)
    check = firmware_gate_status([off], checked=False)
    assert check.status == DOCTOR_WARN
    assert check.steps == (("cta", speakers.amixer_enable_cmd(off)),)
    assert "isn't installed" not in check.detail


def test_amp_status_lines_say_why_the_gate_scan_found_nothing(monkeypatch):
    """The hardware section has the same empty-list problem as the check.

    It lists gates it found, so finding none printed nothing at all, which
    reads as "your amplifiers are fine". On a machine without amixer that is a
    claim nothing checked — and, unlike the check above, this section is what
    `--speaker-info` prints, so it is the only place that reader hears about
    it. Silent again the moment the scan actually ran, or every machine
    without a smart amp gets a line about a tool it never needed.
    """
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.DEBIAN)

    info = speakers.SpeakerInfo()
    info.firmware_gates_checked = False
    lines = report_speaker._amp_status_lines(info)
    assert any("not checked" in l and "amixer" in l for l in lines), lines
    assert any("sudo apt install alsa-utils" in l for l in lines), lines

    scanned = report_speaker._amp_status_lines(speakers.SpeakerInfo())
    assert not any("amixer" in l for l in scanned), scanned


def test_doctor_off_gate_is_never_summarised_as_clean(silence_console, capsys):
    """TRAP: --doctor is the output the issue form asks people to paste when
    something is wrong. A gate that mutes the speakers upstream of the whole
    preset must reach the verdict, not just the hardware dump it sits beside."""
    silence_console(console)
    report = DoctorReport(checks=[
        CheckResult(DOCTOR_PASS, "EasyEffects version", "8.1.0"),
        firmware_gate_status([_gate(False)]),
    ])
    _print_doctor_report(report)
    out = capsys.readouterr().out
    assert "No blocking problems detected." not in out
    assert "1 WARN" in out


def test_doctor_bypass_off_the_speakers_reads_as_clean(silence_console, capsys):
    """The whole point of the change: on a non-speaker output the report must
    stop pulling the 'what to fix first' verdict for a state --autoload
    installed on purpose."""
    silence_console(console)
    rc = {"last_output_preset": BYPASS_PRESET_NAME, "fallback_preset": "",
          "uses_fallback": False}
    report = DoctorReport(checks=[
        CheckResult(DOCTOR_PASS, "EasyEffects version", "8.2.8"),
        loaded_preset_status(rc, ["Dolby-Balanced", BYPASS_PRESET_NAME],
                             output_kind="other",
                             speaker_preset="Dolby-Balanced"),
    ])
    _print_doctor_report(report)
    out = capsys.readouterr().out
    assert "[WARN]" not in out
    assert "0 FAIL, 0 WARN, 2 PASS" in out
    assert "No blocking problems detected." in out
    assert "Dolby-Balanced" in out


def test_doctor_unverified_speaker_preset_ends_on_the_unknown_verdict(
        silence_console, capsys):
    """An UNKNOWN must not be summarised as clean either — it is a check that
    could not run, and the verdict says so rather than claiming an all-clear."""
    silence_console(console)
    rc = {"last_output_preset": BYPASS_PRESET_NAME, "fallback_preset": "",
          "uses_fallback": False}
    report = DoctorReport(checks=[
        CheckResult(DOCTOR_PASS, "EasyEffects version", "8.2.8"),
        loaded_preset_status(rc, ["Dolby-Balanced", BYPASS_PRESET_NAME],
                             output_kind="other", speaker_preset=""),
    ])
    _print_doctor_report(report)
    out = capsys.readouterr().out
    assert "No blocking problems detected." not in out
    assert "1 UNKNOWN" in out
    assert "couldn't be verified" in out


@pytest.mark.parametrize("is_speaker, wants_sink_check", [(True, False),
                                                          (False, True)])
def test_closing_drops_checks_the_report_already_answered(
        is_speaker, wants_sink_check, silence_console, capsys):
    """TRAP: the block prints which output PipeWire is using, then asked the
    reader to go confirm it — the same fault as telling them to check a bypass
    state we just read. The volume half always stays: no level we read tells us
    what the reader can hear."""
    silence_console(console)
    report = DoctorReport(checks=[CheckResult(DOCTOR_PASS, "x", "y")])
    report.facts = {"output_is_speaker": is_speaker, "bypass_is_live": True}
    _print_doctor_report(report)
    out = capsys.readouterr().out
    assert ("system output is the speaker sink" in out) is wants_sink_check
    assert "volume is up" in out


def test_verdict_says_something_on_a_fail(capsys):
    """TRAP: every branch below the all-clear was guarded on `not fail`, so the
    one state that most needs a closing instruction printed no verdict at all —
    the report just stopped after the summary counts."""
    checks = [CheckResult(DOCTOR_FAIL, "Broken thing", "detail")]
    print_verdict(checks, lambda _style, text: print(text))
    out = capsys.readouterr().out.strip()
    assert out, "a FAIL must still end on a verdict"
    assert doctor_module.tag(DOCTOR_FAIL) in out


def test_global_bypass_fails_rather_than_warns():
    """TRAP: as a WARN this sat under "0 FAIL" and "Nothing failed outright" —
    reassuring headlines above the one line saying none of the tool's output
    reaches the speakers."""
    assert environment.global_bypass_status().status == DOCTOR_FAIL


@pytest.mark.parametrize("status", [DOCTOR_WARN, DOCTOR_UNKNOWN])
def test_verdict_names_a_tag_the_report_actually_prints(status, capsys):
    """TRAP: the verdict points readers at "the [WARN] lines above", so the
    label it quotes must be the one the check printer emits. Both lines used
    to spell it by hand and both were wrong — WARN named a ⚠ that appears
    nowhere in either doctor, and UNKNOWN wrote `[ ? ]` where the centring
    yields `[ ?  ]`. Anyone hunting for the quoted string found nothing."""
    checks = [CheckResult(status, "Some check", "detail")]
    emit_check(checks[0], lambda _style, text: print(text), 80)
    print_verdict(checks, lambda _style, text: print(text))
    out = capsys.readouterr().out
    quoted = doctor_module.tag(status)
    assert quoted in out, f"verdict must quote {quoted!r}"
    # The same string appears on the check line above it, not just in prose.
    assert out.count(quoted) >= 2


def test_doctor_ends_on_the_diagnosis_not_the_inventory(monkeypatch,
                                                        silence_console, capsys):
    """Inventory leads, diagnosis trails.

    The report is longer than a terminal and the reader is here because
    something is already wrong, so the checks and what to do about them are
    what has to survive on screen — the hardware dump used to sit between the
    verdict and the closing link. Same principle as the generator's closing
    block (`.claude/rules/user-messages.md`).
    """
    silence_console(console)
    # Stubbed rather than probed: the sequence is the assertion, and the real
    # hardware block differs line for line per machine.
    monkeypatch.setattr(doctor_run.report_speaker, "_print_speaker_info",
                        lambda info: print("=== HARDWARE STUB ==="))
    report = DoctorReport(
        checks=[CheckResult(DOCTOR_WARN, "Background service", "not autostarted")],
        speaker_info=object(),
    )
    _print_doctor_report(report)
    out = capsys.readouterr().out

    assert (out.index("=== HARDWARE STUB ===")
            < out.index("=== EasyEffects setup ===")
            < out.index("=== EasyEffects doctor ===")
            < out.index("Background service")
            < out.index("Summary:")
            < out.index("what to fix first")
            < out.index(report_findings._REPORT_FORM_URL))
    assert out.rstrip().endswith(report_findings._REPORT_FORM_URL)


def test_doctor_report_unknown_not_summarised_as_clean(silence_console,
                                                       capsys):
    """TRAP: an UNKNOWN-only report must NOT print the green 'No blocking
    problems detected' line, and the summary must surface the UNKNOWN count."""
    silence_console(console)
    report = DoctorReport(checks=[
        CheckResult(DOCTOR_UNKNOWN, "EasyEffects version",
                    "installed but version unreadable"),
    ])
    _print_doctor_report(report)
    out = capsys.readouterr().out
    assert "1 UNKNOWN" in out
    assert "No blocking problems detected." not in out


def test_probe_ee_version_degrades_on_missing_binary(monkeypatch):
    """Graceful degradation: no EE binary anywhere → (None, found=False), no
    exception — and nothing claims EE is installed."""
    def boom(*a, **k):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(doctor_run.subprocess, "run", boom)
    monkeypatch.setattr(doctor_run.shutil, "which", lambda _name: None)
    probe = doctor_run._probe_ee_version()
    assert probe.version is None and probe.found is False
    assert probe.silent is None


def test_probe_ee_version_prefers_parseable_over_unreadable(monkeypatch):
    """#22 review: a found-but-unparseable install (e.g. a stale/shim native
    binary that exits 0 with no version) must NOT mask a healthy EE on the other
    install — keep probing for a parseable version."""
    class R:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(cmd, **k):
        if cmd[0] == "easyeffects":      # native answers, but no version token
            return R(0, "easyeffects shim\n")
        if cmd[0] == "flatpak":          # flatpak info has the real version
            return R(0, "ID: x\nVersion: 8.2.1\nInstalled: 458.6 MB\n")
        return R(1, "")

    monkeypatch.setattr(doctor_run.subprocess, "run", fake_run)
    probe = doctor_run._probe_ee_version()
    assert probe.version == (8, 2, 1) and probe.found is True
    assert probe.is_flatpak is True and probe.source == "flatpak info"


def test_probe_ee_version_degrades_on_timeout(monkeypatch):
    def slow(*a, **k):
        raise doctor_run.subprocess.TimeoutExpired(cmd="easyeffects", timeout=5)

    monkeypatch.setattr(doctor_run.subprocess, "run", slow)
    monkeypatch.setattr(doctor_run.shutil, "which", lambda _name: None)
    probe = doctor_run._probe_ee_version()
    assert probe.version is None and probe.found is False


def test_doctor_and_end_of_run_warning_share_their_wording(monkeypatch,
                                                           silence_console,
                                                           capsys):
    """Each end-of-run warning must render the same explanation its --doctor
    counterpart gives. They used to be two hand-maintained copies and had
    already drifted ("EasyEffects off" vs "EasyEffects disabled"), so assert
    the shared builders are what both sides actually emit."""
    from types import SimpleNamespace

    silence_console(console)
    flat = lambda s: " ".join(s.split())       # noqa: E731 — undo line wrapping

    # EasyEffects 7: doctor detail and the end-of-run banner
    assert flat(environment.ee_v7_message("7.1.5")) in flat(
        environment.ee_version_status((7, 1, 5), found=True).detail)
    monkeypatch.setattr(doctor_run, "_probe_ee_version",
                        lambda: doctor_run.EEProbe((7, 1, 5), True, "test", False))
    # The banner now ends on an install command built from what this machine's
    # package manager would answer, so pin that too — otherwise this shells out
    # to apt-cache on a Debian dev box and prints something else elsewhere.
    _distro_ships(monkeypatch, None)
    doctor_run.warn_ee_environment(
        SimpleNamespace(output_dir=ee_paths.DEFAULT_OUTPUT_DIR,
                        irs_dir=ee_paths.DEFAULT_IRS_DIR))
    assert flat(environment.ee_v7_message("7.1.5")) in flat(capsys.readouterr().out)

    # Old kernel: doctor detail and the end-of-run hint
    old = "6.12.74+deb13+1-amd64"
    assert flat(environment.kernel_old_message()) in flat(environment.kernel_age_status(old).detail)
    environment.warn_old_kernel(old)
    assert flat(environment.kernel_old_message()) in flat(capsys.readouterr().out)


def test_the_ee7_warning_names_no_distribution_release(monkeypatch,
                                                       silence_console, capsys):
    """It carried a list of which releases still shipped 7.x. That sentence was
    true when it was written and had no way of staying true — a distribution
    ships 8 the week after, and the tool goes on telling its users otherwise.

    The machine's own package manager answers the same question and can't go
    stale, so no release name may come back on any branch: not when the query
    says 7, not when it says 8, not when there is no query to run.
    """
    from types import SimpleNamespace

    silence_console(console)
    monkeypatch.setattr(doctor_run, "_probe_ee_version",
                        lambda: doctor_run.EEProbe((7, 1, 5), True, "test", False))
    for major in (7, 8, None):
        _distro_ships(monkeypatch, major)
        doctor_run.warn_ee_environment(
            SimpleNamespace(output_dir=ee_paths.DEFAULT_OUTPUT_DIR,
                            irs_dir=ee_paths.DEFAULT_IRS_DIR, dry_run=False))
        out = capsys.readouterr().out
        # Proof we reached the block that used to carry the sentence.
        assert "flatpak install flathub" in out, major
        assert "trixie" not in out and "24.04" not in out, major

    # Same for the --doctor half, which prints the offer through a CheckResult.
    _distro_ships(monkeypatch, 7)
    check = environment.ee_version_status(
        (7, 1, 5), found=True,
        install_steps=doctor_run.easyeffects_install_steps())
    spoken = check.detail + " " + " ".join(t for _s, t in check.steps)
    assert "trixie" not in spoken and "24.04" not in spoken, spoken


def test_probe_ee_version_installed_but_headless(monkeypatch):
    """Issue #46: EE 8's Qt binary needs a display to answer --version, so from
    a headless shell it exits non-zero. An installed EE must not be reported as
    missing — the probe records *why* it stayed silent instead."""
    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def fake_run(cmd, **k):
        if cmd[0] == "easyeffects":
            return R(1, "", "qt.qpa.plugin: could not connect to display\n")
        return R(1, "", "error: com.github.wwmm.easyeffects not installed\n")

    monkeypatch.setattr(doctor_run.subprocess, "run", fake_run)
    monkeypatch.setattr(doctor_run.shutil, "which", lambda name: "/usr/bin/easyeffects"
                        if name == "easyeffects" else None)
    probe = doctor_run._probe_ee_version()
    assert probe.found is False and probe.version is None
    assert "could not connect to display" in probe.silent

    # …and it surfaces as UNKNOWN with an accurate message, not "not found".
    status = environment.ee_version_status(probe.version, probe.found, probe.silent)
    assert status.status == DOCTOR_UNKNOWN
    assert "installed" in status.detail and "not found" not in status.detail


def test_probe_ee_version_absent_flatpak_is_not_silent(monkeypatch):
    """`flatpak info` exits non-zero exactly when the app isn't installed, so
    that failure means absence — it must not be reported as "installed but
    unreachable"."""
    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    monkeypatch.setattr(doctor_run.subprocess, "run",
                        lambda cmd, **k: R(1, "", "not installed\n"))
    monkeypatch.setattr(doctor_run.shutil, "which", lambda _name: None)
    probe = doctor_run._probe_ee_version()
    assert probe.found is False and probe.silent is None
    assert environment.ee_version_status(probe.version, probe.found,
                               probe.silent).status == DOCTOR_WARN


def test_easyeffects_running_is_unknown_on_missing_pgrep(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("no pgrep")

    monkeypatch.setattr(ee_socket.subprocess, "run", boom)
    assert unpatched_ee_probe() is None


# What each package manager actually prints when asked what it *would*
# install. Shapes, not versions: one parser reads all five, and no two of them
# agree on where the number goes — Debian prints two of them and only the
# second is the answer, `dnf --qf` prints the bare number with no label at
# all, and apk gives the version a line of its own with a trailing colon.
_AVAILABLE_VERSION_OUTPUT = {
    packages.DEBIAN: "easyeffects:\n"
                     "  Installed: 7.1.6\n"
                     "  Candidate: 8.2.8+ds-1\n"
                     "  Version table:\n",
    packages.FEDORA: "8.2.8\n",
    packages.SUSE: "Loading repository data...\n"
                   "Reading installed packages...\n\n\n"
                   "Information for package easyeffects:\n"
                   "------------------------------------\n"
                   "Repository     : Main Repository (OSS)\n"
                   "Name           : easyeffects\n"
                   "Version        : 8.2.8-1.2\n"
                   "Arch           : x86_64\n",
    packages.ARCH: "Repository      : extra\n"
                   "Name            : easyeffects\n"
                   "Version         : 8.2.8-1\n"
                   "Description     : Audio effects for PipeWire applications\n",
    packages.ALPINE: "easyeffects policy:\n"
                     "  8.2.8-r0:\n"
                     "    https://dl-cdn.alpinelinux.org/alpine/edge/community\n",
    # A Nix string literal, quotes and all — the parser reads the version out
    # of a line that is nothing but the answer.
    packages.NIXOS: '"8.2.8"\n',
}


class _Ran:
    """A finished `subprocess.run`, with the three attributes the code reads."""

    def __init__(self, returncode, stdout=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


@pytest.mark.parametrize("fam", [
    packages.DEBIAN,
    packages.FEDORA,
    packages.SUSE,
    # Alpine is the one that made the parser more than a partition on ':'.
    # `apk policy` heads each available version with the version itself, so
    # "  8.2.8-r0:" arrives looking like a label whose name is the answer, and
    # reading it as a label meant Alpine could never be offered its own
    # package however new the one it ships.
    packages.ARCH,
    packages.ALPINE,
    packages.NIXOS,
])
def test_the_available_version_query_reads_each_package_manager(fam, monkeypatch):
    """Five layouts, one parser, and a wrong read here names a wrong package.

    This is the whole point of asking the machine instead of tabulating which
    release ships what: an answer we misread is an answer, and it decides
    whether the reader is sent to a package that installs 8 or one that
    installs 7 and silently does nothing.
    """
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        return _Ran(0, _AVAILABLE_VERSION_OUTPUT[fam])

    monkeypatch.setattr(doctor_run.subprocess, "run", fake_run)
    assert doctor_run._distro_easyeffects_major(fam) == 8
    # …asked through this family's own tool, and about the package name this
    # family uses — the two must not drift apart.
    assert seen == [packages.available_version_cmd(packages.EASYEFFECTS, fam)]
    # Present in the argv, not necessarily last: `nix-instantiate` wants the
    # attribute after `-A` and the channel after that, which is the whole
    # reason the builder learned to place the name rather than append it.
    expected = ("easyeffects" if fam == packages.NIXOS
                else packages.names([packages.EASYEFFECTS], fam)[0])
    assert any(expected in arg for arg in seen[0]), seen


def test_apt_policy_answers_with_the_candidate_not_the_installed_version(
        monkeypatch):
    """`apt-cache policy` prints both, and only one of them is this question.

    `Installed:` is what the reader already has — on the machine this check
    fires for, that is the 7.x we are trying to get them off. `Candidate:` is
    what the command we are about to print would actually give them. Reading
    the first would make the tool refuse to name a package precisely on the
    machines where naming it is the fix.
    """
    monkeypatch.setattr(
        doctor_run.subprocess, "run",
        lambda *a, **k: _Ran(0, _AVAILABLE_VERSION_OUTPUT[packages.DEBIAN]))
    assert doctor_run._distro_easyeffects_major(packages.DEBIAN) == 8


def test_every_way_of_not_knowing_the_distro_version_is_none(monkeypatch):
    """Five different failures, one answer, on purpose.

    No query for the family, no tool to run it, a timeout, a non-zero exit, an
    answer with no version in it — they collapse to None because the remedy
    that doesn't depend on the distribution is right in every one of them, and
    a caller that had to tell them apart would be inventing a difference that
    changes nothing. What must never happen is any of them being mistaken for
    a version: that is how a stale answer becomes a named package.
    """
    # Gentoo has no cheap offline query, so the gate is that we return before
    # shelling out at all — not that we discard the result. NixOS is not in
    # this set: it gained one, and asserting it here again would quietly
    # un-test the family the query was added for.
    _forbid_subprocess(monkeypatch)
    assert packages.available_version_cmd(
        packages.EASYEFFECTS, packages.GENTOO) is None
    assert doctor_run._distro_easyeffects_major(packages.GENTOO) is None
    assert packages.available_version_cmd(
        packages.EASYEFFECTS, packages.NIXOS) is not None

    def raises(exc):
        def run(*a, **k):
            raise exc
        return run

    for failure in (OSError("no apt-cache"),                 # tool absent
                    doctor_run.subprocess.TimeoutExpired(cmd="apt-cache",
                                                         timeout=5)):
        monkeypatch.setattr(doctor_run.subprocess, "run", raises(failure))
        assert doctor_run._distro_easyeffects_major(packages.DEBIAN) is None

    for proc in (_Ran(1, "  Candidate: 8.2.8+ds-1\n"),   # exit code wins
                 _Ran(0, "N: Unable to locate package easyeffects\n"),
                 _Ran(0, "easyeffects:\n"),
                 _Ran(0, "")):
        monkeypatch.setattr(doctor_run.subprocess, "run",
                            lambda *a, _p=proc, **k: _p)
        assert doctor_run._distro_easyeffects_major(packages.DEBIAN) is None, \
            proc.stdout


def _forbid_subprocess(monkeypatch):
    """Fail loudly if anything shells out — the gates below must return before
    the subprocess, not merely discard its result."""
    def boom(*a, **k):
        raise AssertionError(f"must not run a subprocess: {a}")

    monkeypatch.setattr(doctor_run.subprocess, "run", boom)


def test_ee_query_refuses_a_request_that_is_not_read_only():
    """TRAP: the same socket accepts quit_app, hide_window and
    toggle_global_bypass. A diagnostic sending one of those would change the
    app it is diagnosing — which is exactly what the `easyeffects` CLI does
    (through EE 8.2.8 its parser emits onHideWindow for these very queries,
    hiding the running window; `-b 3` still does after upstream 8942fbc39),
    and why we speak to the socket ourselves instead."""
    for forbidden in ("quit_app\n", "hide_window\n", "toggle_global_bypass\n"):
        with pytest.raises(ValueError):
            doctor_run._ee_query(forbidden)


def test_ee_query_never_spawns_a_process(monkeypatch):
    """TRAP: invoking the `easyeffects` binary is the hazard, not the socket.
    With no daemon it becomes the PRIMARY instance and starts a whole second
    EasyEffects — new virtual sink, possibly a moved default sink."""
    _forbid_subprocess(monkeypatch)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert doctor_run._ee_query(ee_socket.PRESET_REQUEST).answered is False


def test_ee_query_absent_socket_is_not_reached(monkeypatch, tmp_path):
    """EasyEffects not running (or a Flatpak one whose socket lives inside the
    sandbox): nothing to connect to, so fall back to the config file quietly.
    Must NOT look like protocol drift — that would cry wolf on every machine
    where EE simply isn't up."""
    monkeypatch.setattr(ee_socket, "_socket_path",
                        lambda: tmp_path / "EasyEffectsServer")
    reply = doctor_run._ee_query(ee_socket.BYPASS_REQUEST)
    assert (reply.reached, reply.answered, reply.value) == (False, False, "")


def test_ee_query_contract_pins_the_request_strings():
    """The wire protocol we depend on, spelled out. EasyEffects documents its
    local socket (since 8.0.7) but promises nothing about compatibility, and
    get_global_bypass is not even on that page — only in upstream's
    tags_local_server.hpp — so if upstream renames a tag this test is where
    it is meant to be noticed: the request must keep matching
    `tags::local_server` (get_last_loaded_preset:(input|output)\\n and
    get_global_bypass\\n), and both must stay newline-terminated."""
    assert ee_socket.PRESET_REQUEST == "get_last_loaded_preset:output\n"
    assert ee_socket.BYPASS_REQUEST == "get_global_bypass\n"
    assert doctor_run._EE_READ_REQUESTS == {ee_socket.PRESET_REQUEST,
                                            ee_socket.BYPASS_REQUEST}


def _fake_socket(monkeypatch, *, reply=None, connect_error=None, timeout=False):
    """Stand in for the daemon: reply bytes, a refused connect, or silence.
    Returns the socket's ``sent`` list, so a test can assert what went on the
    wire — the read-only guarantee now lives at the wire, not in a wrapper."""
    sent = []

    class FakeSock:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def settimeout(self, _t): pass
        def connect(self, _p):
            if connect_error:
                raise connect_error
        def sendall(self, d): sent.append(d)
        def recv(self, _n):
            if timeout or reply is None:
                raise TimeoutError()
            return reply

    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/test")
    # The autouse fixture pins the path to None; a socket test puts it back.
    monkeypatch.setattr(ee_socket, "_socket_path",
                        lambda: Path("/run/user/test/EasyEffectsServer"))
    monkeypatch.setattr(ee_socket.socket, "socket", lambda *a, **k: FakeSock())
    return sent


def test_read_helpers_send_only_their_own_request(monkeypatch):
    """The transport takes typed calls, never a caller's string; each read
    puts exactly its own request on the wire and nothing else."""
    sent = _fake_socket(monkeypatch, reply=b"Dolby-Balanced\n")
    assert ee_socket.last_loaded_output_preset().value == "Dolby-Balanced"
    assert sent == [b"get_last_loaded_preset:output\n"]
    sent = _fake_socket(monkeypatch, reply=b"2")   # unframed, by design
    assert ee_socket.global_bypass().value == "2"
    assert sent == [b"get_global_bypass\n"]


def test_load_preset_refuses_a_name_the_daemon_would_drop(monkeypatch):
    """Upstream's regex is ^load_preset:(input|output):([^\\n]{1,100})\\n$, run
    as std::regex over the raw bytes — so 100 UTF-8 bytes, not characters —
    and a miss is dropped silently, which would reach the user as drift."""
    sent = _fake_socket(monkeypatch, reply=b"x\n")
    for bad in ("a\nb", "", "x" * 101, "é" * 60):
        with pytest.raises(ValueError):
            ee_socket.load_output_preset(bad)
    assert sent == []
    ee_socket.load_output_preset("é" * 50)    # exactly 100 bytes: accepted
    assert sent


def test_load_preset_pipelines_the_load_and_its_receipt(monkeypatch):
    """ONE write: load, then the two reads that are the only receipt (the
    daemon drains a write's complete lines in one synchronous pass, and
    load_preset itself answers nothing)."""
    sent = _fake_socket(monkeypatch, reply=b"Dolby-Balanced\nDolby-Balanced-0123abcd\n")
    result = ee_socket.load_output_preset("Dolby-Balanced", "Dolby-Balanced-0123abcd")
    assert sent == [b"load_preset:output:Dolby-Balanced\n"
                    b"get_last_loaded_preset:output\n"
                    b"get_property:output:convolver:0:kernelName\n"]
    assert (result.outcome, result.loaded, result.kernel) == (
        "loaded", "Dolby-Balanced", "Dolby-Balanced-0123abcd")


@pytest.mark.parametrize("reply, kwargs, outcome", [
    # EE still reports the previous preset: it could not find the file.
    (b"Other\nOther-1234abcd\n", {}, "mismatch"),
    # Empty name back: the JSON existed but failed to parse.
    (b"\n\n", {}, "mismatch"),
    # Right preset, stale kernel: the JSON applied but not the impulse.
    (b"Dolby-Balanced\nDolby-Balanced-deadbeef\n", {}, "mismatch"),
    # A listening daemon that answers nothing is drift, not absence.
    (None, {"timeout": True}, "silent"),
    (None, {"connect_error": ConnectionRefusedError()}, "unreachable"),
])
def test_load_preset_outcomes(monkeypatch, reply, kwargs, outcome):
    _fake_socket(monkeypatch, reply=reply, **kwargs)
    result = ee_socket.load_output_preset("Dolby-Balanced", "Dolby-Balanced-0123abcd")
    assert result.outcome == outcome


def test_load_preset_cut_off_mid_receipt_is_silent_not_mismatch(monkeypatch):
    """TRAP (code review 2026-08-27): the daemon closing after the first of
    two replies left buf=b"Dolby-Balanced\\n"; split() yielded a trailing ""
    that counted as the kernel line, and the run told the user EasyEffects
    "kept its previous impulse" about a load it never read."""
    replies = iter([b"Dolby-Balanced\n", b""])

    class FakeSock:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def settimeout(self, _t): pass
        def connect(self, _p): pass
        def sendall(self, _d): pass
        def recv(self, _n): return next(replies)

    monkeypatch.setattr(ee_socket, "_socket_path", lambda: Path("/run/user/test/EasyEffectsServer"))
    monkeypatch.setattr(ee_socket.socket, "socket", lambda *a, **k: FakeSock())
    result = ee_socket.load_output_preset("Dolby-Balanced", "Dolby-Balanced-0123abcd")
    assert result.outcome == "silent"


def test_load_preset_without_a_kernel_reads_one_line(monkeypatch):
    sent = _fake_socket(monkeypatch, reply=b"Dolby-Balanced\n")
    assert ee_socket.load_output_preset("Dolby-Balanced").outcome == "loaded"
    assert sent[0].count(b"\n") == 2


def test_ee_query_silence_from_a_live_daemon_is_drift(monkeypatch):
    """TRAP: the daemon replies only from the branch matching the request tag;
    an unrecognised tag falls through and writes nothing. So connect-then-
    timeout means EasyEffects is there but no longer understands us, which
    must be distinguishable from EE simply not running."""
    _fake_socket(monkeypatch, timeout=True)
    reply = doctor_run._ee_query(ee_socket.PRESET_REQUEST)
    assert (reply.reached, reply.answered) == (True, False)


def test_ee_query_empty_preset_reply_is_a_real_answer(monkeypatch):
    """TRAP: over the socket EE sends the raw preset name, so "\\n" means no
    preset is loaded — a live answer, not silence. (Its CLI substitutes the
    string "None" here; the socket does not.) Treating it as a failure would
    resurrect the stale config value this change exists to replace."""
    _fake_socket(monkeypatch, reply=b"\n")
    reply = doctor_run._ee_query(ee_socket.PRESET_REQUEST)
    assert (reply.reached, reply.answered, reply.value) == (True, True, "")


def test_ee_query_reads_a_normal_reply(monkeypatch):
    _fake_socket(monkeypatch, reply=b"Dolby-Balanced\n")
    assert doctor_run._ee_query(ee_socket.PRESET_REQUEST).value == "Dolby-Balanced"


# --- LOCK-IN: the end-of-run load into a running EasyEffects ---
# lib/preset/reload.py. A stand-in daemon answers the three requests the way
# upstream does — one synchronous pass per write, load_preset silent, the
# kernel name read from the preset on disk — so the receipt is exercised for
# real rather than against a pre-computed hash.

from types import SimpleNamespace


class _FakeDaemon:
    def __init__(self, out_dir, *, loaded="Podcast", bypass="2", mode="ok"):
        self.out_dir, self.loaded, self.bypass, self.mode = Path(out_dir), loaded, bypass, mode
        self.sent = []

    def _kernel(self, name):
        path = self.out_dir / f"{name}.json"
        if self.mode == "stale-kernel" or not path.exists():
            return "Stale-00000000" if path.exists() else "error_plugin_not_found"
        return json.loads(path.read_text())["output"]["convolver#0"]["kernel-name"]

    def handle(self, data: bytes) -> bytes:
        self.sent.append(data)
        out = b""
        for line in data.decode().split("\n")[:-1]:
            if line.startswith("load_preset:output:"):
                name = line.split(":", 2)[2]
                if self.mode == "parse-fail":
                    self.loaded = ""
                elif self.mode != "wrong-preset" and (self.out_dir / f"{name}.json").exists():
                    self.loaded = name
            elif line == "get_last_loaded_preset:output":
                if self.mode == "silent":
                    return b""
                out += self.loaded.encode() + b"\n"
            elif line.startswith("get_property:output:convolver:0:kernelName"):
                out += self._kernel(self.loaded).encode() + b"\n"
            elif line == "get_global_bypass":
                out += self.bypass.encode()      # unframed, as upstream
        return out


def _serve(monkeypatch, daemon):
    class FakeSock:
        pending = b""
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def settimeout(self, _t): pass
        def connect(self, _p): pass
        def sendall(self, d): self.pending = daemon.handle(d)
        def recv(self, _n):
            if not self.pending:
                raise TimeoutError()
            out, self.pending = self.pending, b""
            return out

    monkeypatch.setattr(ee_socket, "_socket_path",
                        lambda: Path("/run/user/test/EasyEffectsServer"))
    monkeypatch.setattr(ee_socket.socket, "socket", lambda *a, **k: FakeSock())
    return daemon


@pytest.fixture
def live_ee_tree(tmp_path, monkeypatch):
    """Point EasyEffects' own directories at tmp so a run with no
    --output-dir/--irs-dir counts as writing the live tree."""
    out, irs = tmp_path / "output", tmp_path / "irs"
    monkeypatch.setattr(ee_paths, "DEFAULT_OUTPUT_DIR", out)
    monkeypatch.setattr(ee_paths, "DEFAULT_IRS_DIR", irs)
    monkeypatch.setattr(sinks, "live_default_sink", lambda: "")
    return out, irs


def _run_live(tmp_path, *extra):
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    return dolby_to_easyeffects.main(
        [str(xml), "--skip-ee-check", "--no-color", *extra])


def _kernel_of(out, name):
    return json.loads((out / f"{name}.json").read_text())["output"]["convolver#0"]["kernel-name"]


def test_reload_pipelines_the_load_and_its_receipt(live_ee_tree, monkeypatch, capsys):
    """ONE write carries the load and its two reads; the kernel read back is
    the hashed name this very run wrote, which is what proves the convolver
    re-read the impulse."""
    out, _ = live_ee_tree
    daemon = _serve(monkeypatch, _FakeDaemon(out))
    assert not _run_live(out.parent)
    loads = [s for s in daemon.sent if s.startswith(b"load_preset:")]
    assert loads == [b"load_preset:output:Dolby-Balanced\n"
                     b"get_last_loaded_preset:output\n"
                     b"get_property:output:convolver:0:kernelName\n"]
    out_text = capsys.readouterr().out
    assert "EasyEffects is now playing 'Dolby-Balanced' — it was on 'Podcast'." in out_text
    collapsed = " ".join(out_text.split())
    assert "is playing 'Dolby-Balanced' now" in collapsed
    assert "reload the preset in EasyEffects" not in out_text
    assert "reload-refused" not in out_text


def test_reload_refreshes_the_preset_already_playing(live_ee_tree, monkeypatch, capsys):
    out, _ = live_ee_tree
    daemon = _serve(monkeypatch, _FakeDaemon(out, loaded="Dolby-Warm"))
    assert not _run_live(out.parent)
    assert any(s.startswith(b"load_preset:output:Dolby-Warm\n") for s in daemon.sent)
    assert "EasyEffects is playing 'Dolby-Warm' again" in capsys.readouterr().out


def test_reload_leaves_the_bypass_preset_alone(live_ee_tree, monkeypatch, capsys):
    """`Nothing` is --autoload's fallback for a non-speaker output: loading a
    speaker tuning over it would put speaker EQ on a headset."""
    out, _ = live_ee_tree
    daemon = _serve(monkeypatch, _FakeDaemon(out, loaded="Nothing"))
    assert not _run_live(out.parent)
    assert not any(s.startswith(b"load_preset:") for s in daemon.sent)
    out_text = " ".join(capsys.readouterr().out.split())
    # Said, not silent: under --autoload the closing is suppressed, and this
    # line is then the only sign the run left it alone.
    assert "EasyEffects is on 'Nothing', the bypass preset for non-speaker outputs" in out_text
    assert "To use them: open EasyEffects" in out_text


_BT_HEADSET = {"name": "bluez_output.AA.1", "description": "Buds",
               "profile": "", "route": "a2dp",
               "icon_name": "audio-headset-bluetooth", "bus": "bluetooth", "api": "bluez5"}
_EE_SINK = {"name": "easyeffects_sink", "description": "EasyEffects Sink", "profile": "",
            "icon_name": "", "bus": "", "api": ""}
_SPEAKER_SINK = {"name": "alsa_output.spk", "description": "Speaker",
                 "profile": "", "route": "Speaker",
                 "icon_name": "audio-speakers", "bus": "pci", "api": "alsa"}


def test_reload_declines_a_non_speaker_default_sink(live_ee_tree, monkeypatch, capsys):
    out, _ = live_ee_tree
    daemon = _serve(monkeypatch, _FakeDaemon(out))
    monkeypatch.setattr(sinks, "live_default_sink", lambda: _BT_HEADSET["name"])
    monkeypatch.setattr(sinks, "_enumerate_audio_sinks", lambda: [_BT_HEADSET])
    assert not _run_live(out.parent)
    assert not any(s.startswith(b"load_preset:") for s in daemon.sent)
    out_text = " ".join(capsys.readouterr().out.split())
    # PipeWire's default sink is the user's, not EasyEffects' device.
    assert "Your default output is 'bluez_output.AA.1' — not loading a speaker tuning onto it" in out_text


@pytest.mark.parametrize("enumerated", [[_EE_SINK], []],
                         ids=["virtual-sink", "probe-failed"])
def test_reload_loads_when_the_default_sink_is_unknown(
        live_ee_tree, monkeypatch, capsys, enumerated):
    """TRAP (code review 2026-08-27): the speaker classifier answers False
    for "don't know", and the gate read that as "not a speaker" — declining
    on EasyEffects' own virtual sink, or when the second pw-dump failed,
    and naming a sink the user never chose. Unknown loads."""
    out, _ = live_ee_tree
    daemon = _serve(monkeypatch, _FakeDaemon(out))
    monkeypatch.setattr(sinks, "live_default_sink", lambda: _EE_SINK["name"])
    monkeypatch.setattr(sinks, "_enumerate_audio_sinks", lambda: enumerated)
    assert not _run_live(out.parent)
    assert any(s.startswith(b"load_preset:") for s in daemon.sent)
    assert "not loading a speaker tuning" not in capsys.readouterr().out


def test_starting_preset_is_one_rule():
    """--autoload <name> wins; else the first built; empty when nothing was.
    Never the declared default profile's: <default_profile> is reported,
    not acted on, and --all-profiles must point where a bare run does
    (maintainer decision 2026-08-27)."""
    names = ["Dolby-Balanced", "Dolby-Detailed", "Dolby-Warm"]
    assert autoload.starting_preset("Dolby-Warm", names) == "Dolby-Warm"
    assert autoload.starting_preset(True, names) == "Dolby-Balanced"
    assert autoload.starting_preset(None, names) == "Dolby-Balanced"
    assert autoload.starting_preset(True, []) == ""


def _two_profile_xml(path, default_profile="music"):
    """The synthetic XML with its `dynamic` profile duplicated as `music`,
    and the declared default set — the case where "first in the file" and
    "declared default" name different presets."""
    xml = write_synthetic_tuning_xml(path, default_profile=default_profile)
    text = xml.read_text()
    block = text[text.index('    <profile type="dynamic">'):text.index("  </endpoint>")]
    xml.write_text(text.replace(
        block, block + block.replace('type="dynamic"', 'type="music"')))
    return xml


def test_autoload_and_the_reload_name_the_same_preset(live_ee_tree, monkeypatch):
    """TRAP (code review 2026-08-27): a bare --autoload wired the first
    profile in the file while the reload loaded the declared default's —
    two adjacent lines naming different presets, and the next sink
    re-activation flipping the user to the other one. One rule
    (autoload.starting_preset) feeds both, and it is the bare run's: the
    first profile, not the declared default (reported, not acted on)."""
    out, _ = live_ee_tree
    daemon = _serve(monkeypatch, _FakeDaemon(out))
    xml = _two_profile_xml(out.parent / "DEV_SYNTH_SUBSYS_TEST.xml")
    speaker = {"name": "alsa_output.pci-0000_00_1f.3.analog-stereo",
               "description": "Built-in Audio Analog Stereo", "route": "Speaker"}
    monkeypatch.setattr(sinks, "_resolve_autoload_sinks", lambda *_: [speaker])
    autoload_dir = out.parent / "autoload"
    rc = dolby_to_easyeffects.main(
        [str(xml), "--skip-ee-check", "--no-color", "--all-profiles",
         "--autoload", "--autoload-dir", str(autoload_dir)])
    assert not rc
    wired = {json.loads(p.read_text())["preset-name"] for p in autoload_dir.glob("*.json")}
    loaded = [s.split(b"\n")[0].split(b":")[2].decode()
              for s in daemon.sent if s.startswith(b"load_preset:")]
    assert wired == {"Dolby-Dynamic-Balanced"}
    assert loaded == ["Dolby-Dynamic-Balanced"]


@pytest.mark.parametrize("declared, note", [("music", True), ("dynamic", False)],
                         ids=["declared-differs", "declared-is-first"])
def test_all_profiles_closing_names_the_declared_default(tmp_path, capsys, declared, note):
    """A bare run says "Windows ships this device on 'music'; these voice
    'dynamic' — --profile music rebuilds". --all-profiles built it, so its
    closing names the preset instead — and still points at the first
    profile, as the bare run does. Silent when the declared default IS the
    first profile."""
    xml = _two_profile_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml", declared)
    rc = dolby_to_easyeffects.main(
        [str(xml), "--output-dir", str(tmp_path / "out"), "--irs-dir",
         str(tmp_path / "irs"), "--skip-ee-check", "--no-color", "--all-profiles"])
    assert not rc
    out_text = " ".join(capsys.readouterr().out.split())
    assert "pick 'Dolby-Dynamic-Balanced' from the Presets menu" in out_text
    assert ("Windows ships this device on 'music' — that's Dolby-Music-Balanced here."
            in out_text) is note


def test_reload_is_gated_off_by_dry_run(live_ee_tree, monkeypatch):
    out, _ = live_ee_tree
    daemon = _serve(monkeypatch, _FakeDaemon(out))
    assert not _run_live(out.parent, "--dry-run")
    assert daemon.sent == []


@pytest.mark.parametrize("loaded, autoload, phrase, restart", [
    ("Podcast", False, "keeps playing 'Podcast' — pick 'Dolby-Balanced' in its Presets menu", False),
    ("Podcast", True, "keeps playing 'Podcast' — pick 'Dolby-Balanced' in its Presets menu", True),
    ("", False, "is running — pick 'Dolby-Balanced'", False),
    ("Dolby-Warm", False, "keeps playing 'Dolby-Warm' as it was before this run — pick it again", False),
    ("Dolby-Warm", True, "keeps playing 'Dolby-Warm' as it was before this run — pick it again", True),
])
def test_no_reload_says_what_to_pick_but_never_loads(
        live_ee_tree, monkeypatch, capsys, loaded, autoload, phrase, restart):
    """The opt-out still owes the reader the state it leaves — under
    --autoload the closing block is silent, so without this line a
    --autoload --no-reload run said nothing about how to hear the change.
    A restart is offered only where something will load ours: autoload. On
    its own EasyEffects rebuilds from its settings db, not the preset file,
    and comes back as it was — even on the preset it was already playing
    (copy audit 2026-08-27; this test used to pin the opposite)."""
    out, _ = live_ee_tree
    daemon = _serve(monkeypatch, _FakeDaemon(out, loaded=loaded))
    extra = ["--no-reload"] + (["--autoload", "--autoload-sink", "alsa_output.spk"] if autoload else [])
    if autoload:
        monkeypatch.setattr(ee_paths, "DEFAULT_EASYEFFECTS_RC", out.parent / "easyeffectsrc")
        monkeypatch.setattr(ee_paths, "DEFAULT_AUTOLOAD_DIR", out.parent / "autoload")
    assert not _run_live(out.parent, *extra)
    assert not any(s.startswith(b"load_preset:") for s in daemon.sent)
    out_text = " ".join(capsys.readouterr().out.split())
    assert "--no-reload: EasyEffects " + phrase in out_text
    assert ("or restart it" in out_text) is restart


def test_no_reload_prints_nothing_when_easyeffects_is_not_running(live_ee_tree, capsys):
    out, _ = live_ee_tree
    assert not _run_live(out.parent, "--no-reload")
    assert "--no-reload:" not in capsys.readouterr().out


def test_reload_is_gated_off_by_custom_dirs(tmp_path, monkeypatch):
    """Presets written anywhere but EasyEffects' own tree are invisible to it."""
    daemon = _serve(monkeypatch, _FakeDaemon(tmp_path / "out"))
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    assert not dolby_to_easyeffects.main(
        [str(xml), "--output-dir", str(tmp_path / "out"), "--irs-dir",
         str(tmp_path / "irs"), "--skip-ee-check", "--no-color"])
    assert daemon.sent == []


def test_reload_is_gated_off_for_a_staged_run(live_ee_tree, monkeypatch):
    """The wrapper's staged presets are deleted when it returns."""
    out, _ = live_ee_tree
    daemon = _serve(monkeypatch, _FakeDaemon(out))
    xml = write_synthetic_tuning_xml(out.parent / "DEV_SYNTH_SUBSYS_TEST.xml")
    assert not dolby_to_easyeffects.main(
        [str(xml), "--skip-ee-check", "--no-color"], staged=True)
    assert daemon.sent == []


@pytest.mark.parametrize("mode, phrase", [
    ("wrong-preset", "still reports 'Podcast'"),
    ("parse-fail", "reports no preset loaded"),
    ("stale-kernel", "reports a different speaker-correction impulse"),
])
def test_reload_mismatch_raises_a_hint_and_the_run_still_succeeds(
        live_ee_tree, monkeypatch, capsys, mode, phrase):
    """Each refused cause carries a detail that fits it and an ask the
    reader can act on — "pick it from the menu" under a detail saying
    EasyEffects looks in another folder asked the impossible (review round
    2026-08-27), and "restart it" asked for a no-op: EasyEffects rebuilds
    from its settings db on start, not from the preset file (copy audit
    2026-08-27). The closing block must not re-offer the menu either."""
    out, _ = live_ee_tree
    _serve(monkeypatch, _FakeDaemon(out, mode=mode))
    assert not _run_live(out.parent)
    out_text = " ".join(capsys.readouterr().out.split())
    assert "[reload-refused]" in out_text and phrase in out_text
    assert "Run this script with --doctor" in out_text
    assert "Restart EasyEffects" not in out_text and "restart it" not in out_text
    assert "is now playing" not in out_text
    assert ("EasyEffects did not load 'Dolby-Balanced' this run — the "
            "[reload-refused] line above says what to do.") in out_text
    assert "To use them: open EasyEffects" not in out_text


def test_reload_silence_sends_no_load(live_ee_tree, monkeypatch, capsys):
    """A listening daemon that won't say what is playing gets no load — a
    load onto an unknown state is not a refresh — and the hint says why."""
    out, _ = live_ee_tree
    daemon = _serve(monkeypatch, _FakeDaemon(out, mode="silent"))
    assert not _run_live(out.parent)
    assert not any(s.startswith(b"load_preset:") for s in daemon.sent)
    out_text = " ".join(capsys.readouterr().out.split())
    assert "[reload-unanswered]" in out_text and "did not answer" in out_text
    # The unanswered question was "what is playing" — no load went out, and
    # the detail must not claim one did (copy audit 2026-08-27).
    assert "did not try to load 'Dolby-Balanced'" in out_text
    assert "asked to load" not in out_text
    assert "[reload-refused]" not in out_text
    assert "the [reload-unanswered] line above says what to do" in out_text


def test_reload_under_global_bypass_says_loaded_not_playing(live_ee_tree, monkeypatch, capsys):
    out, _ = live_ee_tree
    _serve(monkeypatch, _FakeDaemon(out, bypass="1"))
    assert not _run_live(out.parent)
    out_text = " ".join(capsys.readouterr().out.split())
    assert "EasyEffects loaded 'Dolby-Balanced'." in out_text
    assert "is playing" not in out_text and "is now playing" not in out_text
    assert "EasyEffects has 'Dolby-Balanced' loaded" in out_text
    assert "To use them: open EasyEffects" not in out_text
    assert "[ee-bypassed]" in out_text


def test_reload_with_no_bypass_answer_says_loaded_not_playing(live_ee_tree, monkeypatch, capsys):
    """get_global_bypass exists only since EasyEffects 8.1.3; an 8.0.9–8.1.2
    daemon loads fine and answers nothing to it. Unknown is not "off":
    "playing" would hide a bypass this run cannot see (copy audit
    2026-08-27)."""
    out, _ = live_ee_tree
    _serve(monkeypatch, _FakeDaemon(out, bypass=""))
    assert not _run_live(out.parent)
    out_text = " ".join(capsys.readouterr().out.split())
    assert ("EasyEffects loaded 'Dolby-Balanced' (this EasyEffects can't say "
            "whether its effects are switched on).") in out_text
    assert "is playing" not in out_text and "is now playing" not in out_text
    assert "[ee-bypassed]" not in out_text
    assert "EasyEffects has 'Dolby-Balanced' loaded" in out_text


def test_reload_not_reached_prints_nothing_and_keeps_the_manual_step(live_ee_tree, capsys):
    """No socket (EE down, Flatpak, EE < 8.0.9): today's copy, untouched."""
    out, _ = live_ee_tree
    assert not _run_live(out.parent)
    out_text = " ".join(capsys.readouterr().out.split())
    assert "EasyEffects is" not in out_text.replace("EasyEffects is currently", "")
    assert "To use them: open EasyEffects" in out_text
    assert "reload-refused" not in out_text


def test_demo_hook_never_opens_a_socket(live_ee_tree, monkeypatch, capsys):
    out, _ = live_ee_tree
    daemon = _serve(monkeypatch, _FakeDaemon(out))
    monkeypatch.setenv("DEMO_EE_RELOAD", "refreshed")
    assert not _run_live(out.parent)
    assert daemon.sent == []
    assert "EasyEffects is playing 'Dolby-Balanced' again" in capsys.readouterr().out


def test_a_misspelt_demo_hook_fails_closed(tmp_path, monkeypatch, capsys):
    """TRAP (code review 2026-08-27): any non-empty DEMO_EE_RELOAD waived the
    live-tree gate and fabricated a success — a real run printing "now
    playing" having sent nothing. A value the hook doesn't know is no hook:
    here the custom dirs gate the reload as they would without it."""
    monkeypatch.setenv("DEMO_EE_RELOAD", "refreshd")
    daemon = _serve(monkeypatch, _FakeDaemon(tmp_path / "out"))
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    _generate(xml, tmp_path / "out", tmp_path / "irs")
    assert daemon.sent == []
    assert "EasyEffects is" not in capsys.readouterr().out.replace(
        "EasyEffects is currently", "")


def test_resolve_live_state_reports_drift_but_still_shows_values(monkeypatch):
    """Drift must be loud AND non-fatal: the config values still print, marked
    as such, with an UNKNOWN check naming what went unanswered."""
    monkeypatch.setattr(doctor_run, "_ee_query",
                        lambda r: ee_socket.EEReply(reached=True))
    monkeypatch.setattr(sinks, "live_default_sink", lambda: "alsa_output.spk")
    s = doctor_run._resolve_live_state(
        {"last_output_preset": "Saved", "bypass": False,
         "use_default_output_device": True})
    assert s.unanswered == ["loaded preset", "global bypass"]
    assert s.preset == "Saved" and s.preset_is_live is False
    assert environment.ee_unanswered_status(s.unanswered).status == DOCTOR_UNKNOWN


def test_resolve_live_state_absent_daemon_is_not_drift(monkeypatch):
    """TRAP: EE not running is the ordinary case and must stay quiet."""
    monkeypatch.setattr(doctor_run, "_ee_query", lambda r: ee_socket.EEReply())
    monkeypatch.setattr(sinks, "live_default_sink", lambda: "alsa_output.spk")
    s = doctor_run._resolve_live_state({"last_output_preset": "Saved"})
    assert s.unanswered == []


def _resolve(monkeypatch, rc, *, preset="", bypass="", sink="", enumerated=()):
    monkeypatch.setattr(
        doctor_run, "_ee_query",
        lambda r: ee_socket.EEReply(
            value=preset if r == ee_socket.PRESET_REQUEST else bypass,
            reached=True, answered=True))
    from lib.pipewire import checks as pw_checks
    monkeypatch.setattr(sinks, "live_default",
                        lambda: pw_checks.DefaultSink(effective=sink))
    # Classifying a sink reaches the graph, so stub the one pw-dump boundary:
    # unpatched these would answer from the developer machine's own audio.
    # The default empty graph classifies everything "unknown".
    monkeypatch.setattr(sinks, "_enumerate_audio_sinks", lambda: list(enumerated))
    return doctor_run._resolve_live_state(rc)


def test_resolve_live_state_prefers_live_over_the_saved_copy(monkeypatch):
    rc = {"last_output_preset": "Nothing", "output_device": "bluez_output.x.1",
          "use_default_output_device": True, "bypass": True}
    s = _resolve(monkeypatch, rc, preset="Dolby-Balanced", bypass="2",
                 sink="alsa_output.spk")
    assert (s.preset, s.preset_is_live) == ("Dolby-Balanced", True)
    assert (s.sink, s.sink_source) == ("alsa_output.spk", "live")
    assert (s.bypass, s.bypass_is_live) == (False, True)


def test_resolve_live_state_empty_answer_beats_a_saved_name(monkeypatch):
    """TRAP: an answered request wins even when the preset name is empty —
    that is EasyEffects saying nothing is loaded. Falling back to the config
    name there would resurrect the stale value this change exists to replace,
    and would claim a preset is loaded when none is."""
    rc = {"last_output_preset": "Dolby-Balanced", "use_default_output_device": True}
    s = _resolve(monkeypatch, rc, preset="", sink="alsa_output.spk")
    assert s.preset == "" and s.preset_is_live is True


def test_resolve_live_state_falls_back_per_value_not_all_or_nothing(monkeypatch):
    """Each request is resolved on its own: a bypass reply we can't parse must
    not cost us the preset we did get. The two are separate requests on the
    same socket, so one going unrecognised says nothing about the other."""
    rc = {"last_output_preset": "Saved-Preset", "bypass": False,
          "use_default_output_device": True}
    s = _resolve(monkeypatch, rc, preset="Dolby-Balanced", bypass="",
                 sink="alsa_output.spk")
    assert s.preset_is_live is True
    assert s.bypass_is_live is False


def test_resolve_live_state_pinned_device_keeps_the_config_value(monkeypatch):
    """useDefaultOutputDevice off means the user pinned EE to a device, and
    only the GUI writes that key — so the config is authoritative and the live
    default sink is the wrong answer."""
    rc = {"output_device": "alsa_output.hdmi", "use_default_output_device": False}
    s = _resolve(monkeypatch, rc, sink="alsa_output.spk")
    assert (s.sink, s.sink_source) == ("alsa_output.hdmi", "pinned")


def test_resolve_live_state_falls_back_when_pipewire_is_silent(monkeypatch):
    rc = {"output_device": "alsa_output.saved", "use_default_output_device": True}
    s = _resolve(monkeypatch, rc, sink="")
    assert (s.sink, s.sink_source) == ("alsa_output.saved", "saved")
    # Never classified: this name is EasyEffects' cache of a default it may
    # have followed hours ago, so it earns no answer about today's output.
    assert s.sink_kind == "unknown"


def test_resolve_live_state_classifies_the_pinned_sink_too(monkeypatch):
    """A pinned name is the truth (only the GUI writes that key), so it gets
    classified the same way the live one does — otherwise a user pinned to a
    headset keeps the bypass-preset warning this gate exists to drop."""
    rc = {"output_device": _BT_HEADSET["name"], "use_default_output_device": False}
    s = _resolve(monkeypatch, rc, sink="alsa_output.spk",
                 enumerated=[_BT_HEADSET, _SPEAKER_SINK])
    assert (s.sink_source, s.sink_kind) == ("pinned", "other")


def test_resolve_live_state_pinned_sink_that_left_the_graph_is_unknown(monkeypatch):
    """TRAP: the pinned name outlives the device. A headset that has since
    disconnected isn't evidence of anything, so it must not read as a
    confident non-speaker and soften a check."""
    rc = {"output_device": _BT_HEADSET["name"], "use_default_output_device": False}
    s = _resolve(monkeypatch, rc, enumerated=[_SPEAKER_SINK])
    assert (s.sink_source, s.sink_kind) == ("pinned", "unknown")


def test_speaker_autoload_preset_matches_only_a_speaker_sink(tmp_path, monkeypatch):
    """The directory can hold an entry per device. Only the one pointing at a
    sink the classifier calls a speaker answers "what plays on the speakers?" —
    picking the headset's entry would report the bypass preset as the speaker
    tuning."""
    monkeypatch.setattr(sinks, "_enumerate_audio_sinks",
                        lambda: [_SPEAKER_SINK, _BT_HEADSET])
    write_autoload(tmp_path, device_name=_BT_HEADSET["name"],
                   device_description="Buds",
                   device_profile=_BT_HEADSET["route"],
                   preset_name=BYPASS_PRESET_NAME)
    write_autoload(tmp_path, device_name=_SPEAKER_SINK["name"],
                   device_description="Speaker",
                   device_profile=_SPEAKER_SINK["route"],
                   preset_name="Dolby-Balanced")
    assert doctor_run._speaker_autoload_preset(tmp_path) == "Dolby-Balanced"


def test_speaker_autoload_preset_ignores_an_entry_for_a_stale_route(
        tmp_path, monkeypatch):
    """TRAP (code review 2026-08-29): EasyEffects keys an autoload file on
    node.name *and* the active output route, so an entry written for a route
    the sink no longer has is inert. Matching on the name alone reported it
    as what the speakers autoload — a green line for a machine that has no
    working mapping at all."""
    monkeypatch.setattr(sinks, "_enumerate_audio_sinks",
                        lambda: [_SPEAKER_SINK])
    write_autoload(tmp_path, device_name=_SPEAKER_SINK["name"],
                   device_description="Speaker", device_profile="Analog Stereo",
                   preset_name="Dolby-Balanced")
    assert doctor_run._speaker_autoload_preset(tmp_path) == ""


def test_speaker_autoload_preset_looks_past_an_entry_naming_nothing(
        tmp_path, monkeypatch):
    """TRAP (code review 2026-08-29): returning on the first device match
    let one speaker sink mapped to nothing hide another's real mapping —
    glob order decided whether the report found the preset."""
    second = dict(_SPEAKER_SINK, name="alsa_output.spk2")
    monkeypatch.setattr(sinks, "_enumerate_audio_sinks",
                        lambda: [_SPEAKER_SINK, second])
    write_autoload(tmp_path, device_name=_SPEAKER_SINK["name"],
                   device_description="Speaker",
                   device_profile=_SPEAKER_SINK["route"], preset_name="")
    write_autoload(tmp_path, device_name=second["name"],
                   device_description="Speaker",
                   device_profile=second["route"], preset_name="Dolby-Balanced")
    assert doctor_run._speaker_autoload_preset(tmp_path) == "Dolby-Balanced"


def test_speaker_autoload_preset_is_empty_when_nothing_settles_it(tmp_path,
                                                                  monkeypatch):
    """No directory, no entry for a speaker, and no speaker sink to match
    against are all "couldn't check" — never a stray preset name."""
    monkeypatch.setattr(sinks, "_enumerate_audio_sinks",
                        lambda: [_SPEAKER_SINK, _BT_HEADSET])
    assert doctor_run._speaker_autoload_preset(None) == ""
    assert doctor_run._speaker_autoload_preset(tmp_path / "nope") == ""
    write_autoload(tmp_path, device_name=_BT_HEADSET["name"],
                   device_description="Buds",
                   device_profile=_BT_HEADSET["route"],
                   preset_name="Dolby-Balanced")
    assert doctor_run._speaker_autoload_preset(tmp_path) == ""
    # A speaker entry with no speaker in the graph settles nothing either.
    monkeypatch.setattr(sinks, "_enumerate_audio_sinks", lambda: [_BT_HEADSET])
    write_autoload(tmp_path, device_name=_SPEAKER_SINK["name"],
                   device_description="Speaker",
                   device_profile=_SPEAKER_SINK["route"],
                   preset_name="Dolby-Balanced")
    assert doctor_run._speaker_autoload_preset(tmp_path) == ""


def test_pinned_speaker_still_asks_about_the_system_output():
    """TRAP (code review 2026-08-29): making the pinned branch classify its
    sink turned `output_is_speaker` true for a pinned speaker, dropping the
    closing block's "confirm system output is the speaker sink" bullet. That
    is the case that needs it most — EasyEffects pinned to the speakers while
    the system default is HDMI means nothing you hear goes through the chain
    at all. The bullet is about the *system's* output; a pinned sink isn't."""
    speaker = doctor_run.LiveState(sink_kind="speaker", sink_source="live")
    pinned = doctor_run.LiveState(sink_kind="speaker", sink_source="pinned")
    assert speaker.system_output_is_speaker
    assert not pinned.system_output_is_speaker
    # The preset check still gets its answer for a pinned sink — the two read
    # `sink_kind` for different questions.
    assert pinned.sink_kind == "speaker"


def test_resolve_live_state_classifies_a_live_speaker(monkeypatch):
    rc = {"use_default_output_device": True}
    s = _resolve(monkeypatch, rc, sink=_SPEAKER_SINK["name"],
                 enumerated=[_SPEAKER_SINK, _BT_HEADSET])
    assert (s.sink_source, s.sink_kind) == ("live", "speaker")


def test_environment_lines_mark_only_the_rows_that_fell_back():
    f = {"ee_running": True, "rc_present": True,
         "selected_preset": "Dolby-Balanced", "selected_is_live": True,
         "output_device": "alsa_output.spk", "output_device_source": "live",
         "output_plugins": ["convolver#0"],
         "bypass": False, "bypass_is_live": False}
    lines = {ln.split(":")[0].strip(): ln for ln in doctor_run._environment_lines(f)}
    assert "saved config" not in lines["Selected preset"]
    assert "saved config" not in lines["Output sink"]
    # No live source exists for the chain, and bypass fell back here.
    assert "saved config" in lines["Active chain"]
    assert "saved config" in lines["Global bypass"]


def test_environment_lines_mark_the_source_even_with_no_daemon():
    """TRAP: a value that wasn't confirmed must say so on its own row, whether
    or not EasyEffects is running. Stating it once at the top instead left
    `Global bypass:` reading identically live and stale — while the closing
    drops its bypass reminder only on a live reading, which then looked
    arbitrary."""
    f = {"ee_running": False, "rc_present": True, "rc_path": "~/rc",
         "selected_preset": "Dolby-Balanced", "selected_is_live": False,
         "output_plugins": ["convolver#0"], "bypass": False}
    rows = {ln.split(":")[0].strip(): ln for ln in doctor_run._environment_lines(f)}
    assert "(from saved config)" in rows["Selected preset"]
    assert "(from saved config)" in rows["Global bypass"]
    assert "(from saved config)" in rows["Active chain"]


def test_environment_lines_count_the_files_in_the_folders():
    """TRAP: stated bare, the mismatched counts read as the tool failing to
    count — but "presets sharing impulse files" fixed that by asserting a
    relationship neither count establishes: both are folder contents, bypass
    preset, hand-made presets and stray .irs files included. So the line says
    what it counted, and claims nothing about why the two differ."""
    f = {"ee_running": True, "rc_present": True, "rc_path": "~/rc",
         "preset_count": 69, "irs_count": 64}
    lines = doctor_run._environment_lines(f)
    i = next(n for n, ln in enumerate(lines) if ln.startswith("  Install:"))
    row = " ".join(ln.strip() for ln in lines[i:i + 2])   # wraps at 80 columns
    assert "69 preset files and 64 impulse files in the folders" in row
    assert "sharing" not in row


def test_collapsed_preset_check_reconciles_the_two_counts():
    """TRAP: the folded line's denominator is one short of the preset count in
    the inventory (the bypass preset has nothing to check), and the summary's
    PASS total is mostly these checks, counted individually but shown as one
    line. Both gaps were read as the numbers not adding up. The bypass
    sentence only reconciles them where that file exists — on a folder
    without one it explained a gap that wasn't there."""
    checks = [CheckResult(DOCTOR_PASS, f"Preset P{i}", "") for i in range(3)]
    collapsed = doctor_run._collapse_preset_checks(checks,
                                                   bypass_present=True)
    assert len(collapsed) == 1
    assert collapsed[0].label == "Presets (3/3 checked out)"
    # A sentence, not the fragment "3 checks on one line." the #84 paste
    # carried — the reader could not tell what it was a fragment of.
    assert collapsed[0].detail.startswith(
        "Every one of them loads its speaker-correction impulse file; the "
        "summary below counts them one by one.")
    assert "checks on one line" not in collapsed[0].detail
    assert BYPASS_PRESET_NAME in collapsed[0].detail

    no_bypass = doctor_run._collapse_preset_checks(checks)
    assert BYPASS_PRESET_NAME not in no_bypass[0].detail
    assert "other preset" not in no_bypass[0].detail

    # The user's own presets in the folder are the third number to
    # reconcile — named as a count, singular and plural agreeing.
    two = doctor_run._collapse_preset_checks(checks, foreign=2)[0].detail
    assert two.endswith("2 other preset files in the folder aren't this tool's, "
                        "so nothing here checked them.")
    one = doctor_run._collapse_preset_checks(checks, foreign=1)[0].detail
    assert one.endswith("1 other preset file in the folder isn't this tool's, "
                        "so nothing here checked it.")


# --- Only presets this tool wrote are checked (issue #84) --------------------

AUTOEQ_PRESET = {"output": {"blocklist": [], "plugins_order": ["equalizer#0"],
                            "equalizer#0": {"bypass": False, "num-bands": 10}}}


def test_is_generated_preset_recognises_either_signal():
    ours = environment.is_generated_preset
    assert ours({"_generator": "dolby_to_easyeffects.py v2026.08", "output": {}},
                "Anything")
    # The hashed impulse name, and the legacy unhashed one, with no stamp
    # (an EasyEffects re-save may drop unknown keys).
    assert ours({"output": {"convolver#0": {"kernel-name": "Dolby-Balanced-0a1b2c3d"}}},
                "Dolby-Balanced")
    assert ours({"output": {"convolver#0": {"kernel-name": "Dolby-Balanced"}}},
                "Dolby-Balanced")
    # Matched without asking whether the .irs exists: a preset whose impulse
    # has gone must still reach the check that reports that.
    assert ours({"output": {"convolver#1": {"kernel-name": "Dolby-Warm-deadbeef"}}},
                "Dolby-Warm")


def test_is_generated_preset_leaves_the_users_presets_alone():
    ours = environment.is_generated_preset
    assert not ours(AUTOEQ_PRESET, "Edition_XS")
    # A copy the user saved under another name names a different preset's
    # impulse — theirs now, by design.
    assert not ours({"output": {"convolver#0": {"kernel-name": "Dolby-Balanced-0a1b2c3d"}}},
                    "My copy")
    assert not ours({"_generator": "someone-else 1.0", "output": {}}, "X")
    assert not ours([], "X")
    assert not ours({"output": "not a dict"}, "X")


def test_generated_presets_are_recognised_as_ours(generated, tmp_path):
    """DRIFT TRAP: the doctor recognises presets by the stamp and the impulse
    name the writers produce. Change either writer and this goes red before
    the doctor silently starts ignoring every preset this tool writes."""
    preset, _irs_path = generated
    assert environment.is_generated_preset(preset, "whatever-the-name")
    path, _state = autoload.write_bypass_preset(tmp_path, BYPASS_PRESET_NAME)
    assert environment.is_generated_preset(json.loads(path.read_text()),
                                           BYPASS_PRESET_NAME)


def test_read_presets_partitions_ours_from_the_users(tmp_path):
    """The #84 regression: two AutoEq headphone presets beside ours produced
    two [WARN] lines and a verdict pointing at them as what to fix first."""
    (tmp_path / "Dolby-Balanced.json").write_text(json.dumps(
        {"_generator": "dolby_to_easyeffects.py x",
         "output": {"convolver#0": {"kernel-name": "Dolby-Balanced-0a1b2c3d"}}}))
    (tmp_path / "Dolby-Warm.json").write_text(json.dumps(          # re-saved: no stamp
        {"output": {"convolver#0": {"kernel-name": "Dolby-Warm-0a1b2c3d"}}}))
    (tmp_path / "Edition_XS.json").write_text(json.dumps(AUTOEQ_PRESET))
    (tmp_path / "Soundcore_A40.json").write_text(json.dumps(AUTOEQ_PRESET))
    (tmp_path / "Broken.json").write_text("{not json")
    autoload.write_bypass_preset(tmp_path, BYPASS_PRESET_NAME)

    ours, foreign, unreadable = doctor_run._read_presets(tmp_path)
    assert [p.stem for p, _ in ours] == ["Dolby-Balanced", "Dolby-Warm",
                                         BYPASS_PRESET_NAME]
    assert [data is None for _, data in ours] == [False, False, True]
    assert foreign == 2          # the two AutoEq presets
    # A file that won't parse is neither ours nor theirs — nothing says
    # which — so it is reported on its own, not folded into either count.
    assert [p.stem for p in unreadable] == ["Broken"]

    # What the report then does with them: our two get the impulse check
    # (both FAIL here — no .irs dir), the others get no line at all, and the
    # folded PASS line is where they are mentioned.
    checks = [environment.check_preset_kernel(data, set(), p.stem)
              for p, data in ours if data is not None]
    assert [c.label for c in checks] == ["Preset Dolby-Balanced", "Preset Dolby-Warm"]
    assert all(c.status == DOCTOR_FAIL for c in checks)
    assert not any("Edition_XS" in c.label or "Soundcore" in c.label for c in checks)
    passing = [CheckResult(DOCTOR_PASS, "Preset Dolby-Balanced", "")]
    folded = doctor_run._collapse_preset_checks(passing, bypass_present=True, foreign=2)
    assert len(folded) == 1 and folded[0].status == DOCTOR_PASS
    assert "2 other preset files" in folded[0].detail


def test_presets_from_another_version_are_reported_once():
    """Two stale stamps fold into one sorted WARN with the counts; no stamp
    (a GUI re-save) is unknown, never stale; and the aggregate label's
    near-miss with "Preset " must stay out of the per-preset fold."""
    check = doctor_module.another_version_check(
        "Presets from another version", "preset",
        ["v1", "", "v0", "v2", "v1"], "v2",
        "re-run dolby_to_easyeffects.py on your tuning XML")
    assert check.status == DOCTOR_WARN
    assert check.detail.startswith(
        "3 of 5 presets were written by v0, v1 and this is v2.")
    assert check.detail.endswith(
        "a preset is a snapshot, it doesn't update itself.")
    assert doctor_module.another_version_check(
        "Presets from another version", "preset",
        ["", "v2"], "v2", "x") is None
    one = doctor_module.another_version_check(
        "Presets from another version", "preset", ["v1", "v2"], "v2", "x")
    assert "1 of 2 presets was written by v1" in one.detail
    collapsed = doctor_run._collapse_preset_checks(
        [one], bypass_present=False, foreign=0)
    assert collapsed == [one]


def test_generator_version_reads_only_our_own_stamp():
    """"" for anything that isn't this tool's stamp — and the stamp the
    writers produce round-trips to the running version, so the check
    compares like with like."""
    from lib import version as version_module
    assert autoload.generator_version(
        {"_generator": "dolby_to_easyeffects.py v1.2"}) == "v1.2"
    assert autoload.generator_version({"_generator": "someone-else 1.0"}) == ""
    assert autoload.generator_version({}) == ""
    assert autoload.generator_version([]) == ""
    assert autoload.generator_version({"_generator": 3}) == ""
    assert autoload.generator_version(
        {"_generator": autoload.generator_stamp()}) == version_module.get_version()


def test_no_presets_found_names_what_it_can_see():
    assert doctor_run._no_presets_found("~/o", 0, False) == (
        "no presets found in ~/o — run the script on your tuning XML first.")
    assert doctor_run._no_presets_found("~/o", 0, True).startswith(
        "no presets other than the bypass preset found in ~/o")
    only_theirs = doctor_run._no_presets_found("~/o", 2, False)
    assert only_theirs.startswith("no speaker presets from this tool found in ~/o "
                                  "(the 2 preset files there weren't written by it)")
    # With the bypass preset there too the count says "other", so it can be
    # reconciled with the Environment row's total.
    assert "(the 1 other preset file there wasn't written by it)" in \
        doctor_run._no_presets_found("~/o", 1, True)
    assert "(the 2 preset files there couldn't be read)" in \
        doctor_run._no_presets_found("~/o", 0, False, unreadable=2)
    assert "(the 3 other preset files there weren't written by it or couldn't be read)" \
        in doctor_run._no_presets_found("~/o", 2, True, unreadable=1)


def test_environment_lines_show_the_pipewire_clock_and_dropouts():
    """Issue #84: a crackling report whose paste could not say what quantum
    the chain ran at or whether the graph dropped buffers. Rows, not checks —
    no quantum is known to be too small and the xrun counter is cumulative."""
    ok = session.ClockSettings(rate="48000", quantum="1024", min_quantum="32",
                             max_quantum="2048", force_quantum="0", force_rate="0")
    quiet = session.Dropouts(sink=0, chain=0, chain_node="easyeffects_sink",
                           sink_recent=0, chain_recent=0, window_s=5.0, playing=False,
                           sink_is_driver=True, running_quantum=0)
    f = {"ee_running": True, "rc_present": True, "rc_path": "~/rc",
         "pw_clock": ok, "pw_xruns": quiet, "pw_age": 114403.0, "ee_age": 2028.9}
    lines = doctor_run._environment_lines(f)
    text = " ".join(ln.strip() for ln in lines)   # the rows wrap at 80 columns
    # Session defaults are named as such (a client can pull the graph lower
    # without touching them); what the output ran at during the check is its
    # own line, hung on the gutter, so standing state and observation never
    # read as one.
    assert ("48000 Hz, quantum 1024 samples per cycle (session defaults, min 32, "
            "max 2048), no session-wide override during the check: the output "
            "was idle") in text
    assert any(ln.startswith(" " * doctor_layout.GUTTER + "during the check:")
               for ln in lines)
    # Totals, their age (a bound — nodes are recreated), and the live window
    # — the three things that make a cumulative counter readable — with the
    # plain word leading.
    assert ("none (0 xruns) on the output sink or any of EasyEffects' nodes "
            "— since each node was created, at most PipeWire's 1 d 7 h / "
            "EasyEffects' 33 min uptime during the check: none in 5 s, nothing "
            "was playing into EasyEffects") in text
    # The server rows sit above the app that runs on them.
    order = [ln.split(":")[0].strip() for ln in lines]
    assert order.index("Clock") < order.index("EasyEffects")

    forced = session.ClockSettings(rate="48000", quantum="256", min_quantum="32",
                                 max_quantum="2048", force_quantum="256",
                                 force_rate="44100")
    f["pw_clock"] = forced
    f["pw_xruns"] = session.Dropouts(sink=42, chain=14, chain_node="ee_soe_convolver",
                                   sink_recent=3, chain_recent=0, window_s=5.0,
                                   playing=True, sink_is_driver=True,
                                   running_quantum=256, running_rate=48000)
    f["output_device"] = "bluez_output.80_99_E7_E0_8A_23.1"
    f["output_device_source"] = "live"
    lines = doctor_run._environment_lines(f)
    text = " ".join(ln.strip() for ln in lines)
    assert ("quantum 256 samples per cycle (session defaults, min 32, max 2048); "
            "forced session-wide: quantum 256, rate 44100 during the check: "
            "48000 Hz, 256-sample cycles (5.3 ms)") in text
    # Two counts, not a maximum over a set the reader has to reconstruct; a
    # driver's ERR is the whole graph's and says so; the sink is not named
    # twice — the Output sink row above carries its (redacted) name; and
    # "running", not "audible": a silent stream keeps the graph running.
    assert ("42 xruns on the output sink (it drives the clock, so any "
            "node's dropout counts there), 14 on the busiest EasyEffects node "
            "(ee_soe_convolver) — since each node was created, at most PipeWire's "
            "1 d 7 h / EasyEffects' 33 min uptime during the check: 3 on the sink, "
            "0 on EasyEffects' nodes in 5 s, a playback stream was running") in text
    assert "80_99" not in text
    assert all(len(ln) <= console._wrap_width() for ln in lines)
    # The sink row leads the PipeWire block, so "the output sink" two rows
    # down points at something on the same screen.
    order = [ln.split(":")[0].strip() for ln in lines]
    assert order.index("Output sink") < order.index("Clock") < order.index("Dropouts")
    # No age known, EasyEffects' nodes clean, only the sink counting, and the
    # sink following another driver.
    f["pw_xruns"] = session.Dropouts(sink=42, chain=0, chain_node="easyeffects_source",
                                   sink_recent=0, chain_recent=0, window_s=5.0)
    f.pop("pw_age"); f.pop("ee_age")
    text = " ".join(ln.strip() for ln in doctor_run._environment_lines(f))
    assert ("42 xruns on the output sink, none on EasyEffects' nodes — since each "
            "node was created during the check: none in 5 s, nothing was playing "
            "into EasyEffects") in text


def test_environment_lines_lead_with_the_daemon_versions(monkeypatch):
    """Which server the section describes comes first; a facts dict without
    the probes (every hand-built one in this file) renders no Versions row,
    so nothing else here had to change."""
    monkeypatch.setenv("COLUMNS", "80")
    f = {"ee_running": True, "rc_present": True,
         "pipewire_version": session.Version(text="1.6.8", parts=(1, 6, 8)),
         "wireplumber_version": session.Version(text="0.5.15",
                                                parts=(0, 5, 15))}
    lines = doctor_run._environment_lines(f)
    assert lines[0] == ("  Versions:        PipeWire 1.6.8 (running), "
                        "WirePlumber 0.5.15 (installed)")
    bare = doctor_run._environment_lines({"ee_running": True,
                                          "rc_present": True})
    assert not any("Versions:" in ln for ln in bare)


def test_environment_lines_say_when_the_pipewire_rows_could_not_be_read():
    """TRAP: an unread value must not vanish — in a pasted report an absent
    row and a zero are indistinguishable, and the reassuring one wins."""
    f = {"ee_running": False, "rc_present": True, "rc_path": "~/rc",
         "pw_clock": session.ClockSettings(reason="pw-metadata not found"),
         "pw_xruns": session.Dropouts(reason="pw-top didn't answer")}
    rows = {ln.split(":")[0].strip(): ln for ln in doctor_run._environment_lines(f)}
    assert rows["Clock"].endswith("pw-metadata not found — clock settings not read")
    assert rows["Dropouts"].endswith("not read (pw-top didn't answer)")
    f["pw_clock"] = session.ClockSettings(reason="no answer from pw-metadata")
    rows = {ln.split(":")[0].strip(): ln for ln in doctor_run._environment_lines(f)}
    assert rows["Clock"].endswith("no answer from pw-metadata — clock settings "
                                  "not read")


def test_the_ee_doctor_flags_an_unreadable_pipewire():
    """The filter-chain doctor's PipeWire check, mirrored: the verdict must
    not read "nothing failed" over a section that says "not read" four
    times. Both probes empty → one UNKNOWN carrying the daemon question;
    one probe missing → none (a package, not a dead server)."""
    check = doctor_run._pipewire_unread_check(
        session.ClockSettings(reason="no answer from pw-metadata"),
        session.Dropouts(reason="pw-top didn't answer"))
    assert check.status == DOCTOR_UNKNOWN and check.label == "PipeWire"
    assert "is the PipeWire daemon running?" in check.detail
    assert doctor_run._pipewire_unread_check(
        session.ClockSettings(reason="pw-metadata not found"),
        session.Dropouts(sink=0, chain=0)) is None
    assert doctor_run._pipewire_unread_check(None, None) is None


def test_environment_lines_wrap_the_chain_without_splitting_the_marker():
    """A full chain is seven plugin names — ~145 columns on one line. It wraps
    to the gutter, and the provenance marker is appended after wrapping: split
    across lines, "limiter#0 (from" reads as part of the plugin name."""
    f = {"ee_running": True, "rc_present": True, "rc_path": "~/rc",
         "output_plugins": ["convolver#0", "equalizer#0", "equalizer#1",
                            "autogain#0", "multiband_compressor#0",
                            "multiband_compressor#1", "limiter#0"]}
    lines = doctor_run._environment_lines(f)
    chain = [ln for ln in lines
             if "Active chain" in ln
             or ln.startswith(" " * doctor_layout.GUTTER)]
    assert len(chain) > 1, "a seven-plugin chain must wrap"
    assert all(len(ln) <= console._wrap_width() for ln in chain)
    assert any("(from saved config)" in ln for ln in chain), "marker kept whole"


def test_environment_lines_keep_the_gutter():
    """Values line up only if every label fits the gutter and every row pads
    to it. Driven by `doctor_layout.GUTTER` rather than a literal, so a label
    that outgrows it fails here instead of quietly stepping one column
    right."""
    f = {"ee_running": True, "rc_present": True, "rc_path": "~/rc",
         "selected_preset": "P", "selected_is_live": True,
         "output_device": "s", "output_device_source": "live",
         "output_label": "Speaker",
         "output_plugins": ["c"], "bypass": False, "bypass_is_live": True,
         "pw_clock": session.ClockSettings(rate="48000", quantum="1024"),
         "pw_xruns": session.Dropouts(sink=3, chain=1, chain_node="ee_soe_convolver",
                                    sink_recent=0, chain_recent=0, window_s=5.0)}
    assert_rows_line_up(doctor_run._environment_lines(f), doctor_layout.GUTTER)


def test_environment_lines_redact_a_bluetooth_default_sink():
    """The live default sink follows the headset on connect exactly as EE's
    own record did, so it needs the same redaction."""
    f = {"ee_running": True, "rc_present": True,
         "output_device": "bluez_output.80_99_E7_E0_8A_23.1",
         "output_device_source": "live"}
    line = [ln for ln in doctor_run._environment_lines(f) if "Output sink" in ln][0]
    assert "80_99_E7_E0_8A_23" not in line


def test_environment_lines_name_the_bypass_preset_as_a_preset():
    """TRAP (/user-review 2026-08-29): bare, the row read "Selected: Nothing"
    — which reads as "nothing is selected", not as the name of a preset, and
    gave no hint it was about an EasyEffects preset at all. Quoted like every
    other mention in the report, and the one name that doesn't explain itself
    says what it is. What to do about it stays with the check."""
    def row(preset):
        f = {"ee_running": True, "rc_present": True,
             "selected_preset": preset, "selected_is_live": True}
        return [ln for ln in doctor_run._environment_lines(f)
                if ln.startswith("  Selected preset:")][0]

    assert row(BYPASS_PRESET_NAME) == (
        f"  Selected preset: '{BYPASS_PRESET_NAME}'")
    assert row("Dolby-Balanced") == "  Selected preset: 'Dolby-Balanced'"
    # Not "the bypass preset": that word belongs to the `Global bypass:` row,
    # EasyEffects' own toggle, and the same word for two things reads as a
    # contradiction ("the bypass preset is selected" / "bypass: off").
    assert "bypass" not in row(BYPASS_PRESET_NAME).lower()


def _sink_lines(f) -> list[str]:
    """The Output-sink row and any continuation lines under it."""
    lines = doctor_run._environment_lines(f)
    i = next(n for n, ln in enumerate(lines) if "Output sink" in ln)
    out = [lines[i]]
    for ln in lines[i + 1:]:
        if not ln.startswith(" " * doctor_layout.GUTTER):
            break
        out.append(ln)
    return out


def test_environment_lines_never_print_a_bluetooth_devices_name():
    """TRAP: a Bluetooth description is user-set and routinely carries a
    person's name — "<Name>'s AirPods" is the stock spelling — and this block
    is what the issue form asks people to paste whole. The address is
    stripped for that reason; a name is worse, so every Bluetooth sink
    renders under one fixed label instead."""
    f = {"ee_running": True, "rc_present": True,
         "output_device": "bluez_output.80_99_E7_E0_8A_23.1",
         "output_device_source": "live",
         # What sink_kind_and_label refuses to hand over; here to prove the
         # renderer wouldn't print it even if something upstream did.
         "output_label": sinks.BT_SINK_LABEL}
    line = " ".join(_sink_lines(f))
    assert "80_99_E7_E0_8A_23" not in line
    assert sinks.BT_SINK_LABEL in line


def test_sink_label_is_the_description_but_never_a_bluetooth_one(monkeypatch):
    """The refusal lives at the resolver, so no caller can reach the name."""
    bt = dict(_BT_HEADSET, description="Someone's AirPods")
    monkeypatch.setattr(sinks, "_enumerate_audio_sinks",
                        lambda: [_SPEAKER_SINK, bt])
    assert sinks.sink_kind_and_label(_SPEAKER_SINK["name"]) == (
        "speaker", _SPEAKER_SINK["description"])
    assert sinks.sink_kind_and_label(bt["name"]) == (
        "other", sinks.BT_SINK_LABEL)
    # A sink the enumeration doesn't hold settles neither answer.
    assert sinks.sink_kind_and_label("alsa_output.gone") == ("unknown", "")


def test_environment_lines_lead_with_the_description_and_keep_the_node_name():
    """The description answers "what is my sound coming out of"; the node
    name is what --autoload-sink takes and what a report is triaged on, so
    losing either would cost something."""
    f = {"ee_running": True, "rc_present": True,
         "output_device": "alsa_output.spk", "output_device_source": "live",
         "output_label": "Built-in Audio Speaker"}
    line = " ".join(_sink_lines(f))
    assert line.index("Built-in Audio Speaker") < line.index("alsa_output.spk")


def test_environment_lines_never_split_a_long_node_name():
    """TRAP: a node name broken across lines stops being greppable and stops
    being copy-pasteable into --autoload-sink — the two things it is there
    for. Only the description wraps; the name overflows instead."""
    node = ("alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic"
            ".HiFi__Speaker__sink")
    f = {"ee_running": True, "rc_present": True,
         "output_device": node, "output_device_source": "live",
         "output_label": "Alder Lake PCH-P High Definition Audio Controller "
                         "HDMI / DisplayPort 1 Output"}
    lines = _sink_lines(f)
    assert any(node in ln for ln in lines), lines
    assert len(lines) > 1                      # the description did wrap
    assert all(ln.startswith(" " * doctor_layout.GUTTER) for ln in lines[1:])


def test_the_output_sink_row_survives_a_silent_pipewire(monkeypatch):
    """TRAP: with no name from either source the row used to vanish — and in
    a paste an absent row and a zero read alike, while the Dropouts row kept
    referring to "the output sink". Each of the three ways of having no name
    prints its own why."""
    monkeypatch.setenv("COLUMNS", "80")
    def text(f):
        f = {"ee_running": True, "rc_present": True, **f}
        return " ".join(ln.strip() for ln in _sink_lines(f))

    assert text({"output_device": "", "output_device_source": "saved",
                 "output_reason": "pw-dump didn't answer"}) == (
        "Output sink:     not read (pw-dump didn't answer), and EasyEffects "
        "has no saved output to fall back on")
    assert text({"output_device": "", "output_device_source": "saved"}) == (
        "Output sink:     none — PipeWire has no default output right now, "
        "and EasyEffects has no saved output to fall back on")
    # A parsed rc pinning no device is a none, not a failed probe.
    assert text({"output_device": "", "output_device_source": "pinned"}) == (
        "Output sink:     none — EasyEffects is pinned to an output but its "
        "config names no device")
    # Even a facts dict that never mentions the sink renders the row.
    assert "Output sink:" in text({})


def test_environment_lines_without_a_label_are_unchanged():
    """A probe that settled nothing must not cost the row its node name."""
    f = {"ee_running": True, "rc_present": True,
         "output_device": "alsa_output.spk", "output_device_source": "live",
         "output_label": ""}
    assert _sink_lines(f) == ["  Output sink:     alsa_output.spk"]


def test_easyeffects_running_is_unknown_on_permission_error(monkeypatch):
    """TRAP: a sandboxed/SELinux host where pgrep raises PermissionError (an
    OSError that is NOT FileNotFoundError/SubprocessError) must not crash the
    doctor's fact-gathering — and reads as unknown, never as "not running"."""
    def denied(*a, **k):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(ee_socket.subprocess, "run", denied)
    assert unpatched_ee_probe() is None


def test_the_running_row_says_unknown_when_nothing_could_ask():
    """TRAP: pgrep missing or denied is not "not running" — the row must not
    reassure in the direction of "no"."""
    def text(value):
        f = {"ee_running": value, "rc_present": True}
        return " ".join(ln.strip() for ln in doctor_run._environment_lines(f))

    assert "running: yes;" in text(True)
    assert "running: no;" in text(False)
    assert "running: unknown (pgrep couldn't be run);" in text(None)
