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

import json
import math
from datetime import date

import numpy as np
import pytest

from dolby_to_easyeffects import (
    DOCTOR_FAIL,
    DOCTOR_PASS,
    DOCTOR_UNKNOWN,
    DOCTOR_WARN,
    BYPASS_PRESET_NAME,
    FIR_LENGTH,
    SAMPLE_RATE,
    CheckResult,
    DoctorReport,
    _atomic_write,
    _atomic_write_text,
    _disabled_band,
    _doctor_summary,
    _print_doctor_report,
    _report_parsed_profile,
    autostart_status,
    check_preset_kernel,
    decode_mbc_bands,
    ee_version_status,
    install_status,
    kernel_age_status,
    loaded_preset_status,
    make_autogain,
    make_bass_enhancer,
    make_dialog_enhancer,
    make_fir,
    make_limiter,
    make_multiband_compressor,
    make_peq_eq,
    make_preset,
    make_regulator,
    parse_ee_version,
    parse_kernel_series,
    read_ee_rc,
    save_wav_stereo,
    set_autoload_fallback,
    write_autoload,
    write_bypass_preset,
)
from tests.conftest import (
    SYNTHETIC_FREQS_20,
    is_minimum_phase,
    read_irs_file,
    synthetic_mb_comp,
    synthetic_peq_filters,
    synthetic_regulator,
)


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
    """_coupled_bands_eligible drives the end-of-run --enable hint: true
    only when a >= 0 dB band is marked non-isolated."""
    from dolby_to_easyeffects import _coupled_bands_eligible
    th = [-6.0] * 10 + [0.0] * 10
    assert _coupled_bands_eligible(
        synthetic_regulator(th, isolated_band=[1] * 10 + [0] * 10))
    assert not _coupled_bands_eligible(synthetic_regulator(th))
    assert not _coupled_bands_eligible(
        synthetic_regulator(th, isolated_band=[1] * 20))
    assert not _coupled_bands_eligible(
        synthetic_regulator([-6.0] * 20, isolated_band=[0] * 20))
    assert not _coupled_bands_eligible(None)


def _report_tuning(regulator, volmax_boost, profile_used="dynamic",
                   default_profile=None, geq_max_range=192, ao_enabled=True):
    """Minimal tuning stand-in for _report_parsed_profile: every optional
    block is falsy so only the regulator/volmax sections print."""
    from types import SimpleNamespace
    return SimpleNamespace(
        ieq_amount=10, peq_filters=[], dialog_enhancer=None, surround=None,
        vol_leveler=None, mb_comp=None, regulator=regulator,
        volmax_boost=volmax_boost, freqs=SYNTHETIC_FREQS_20,
        profile_used=profile_used, default_profile=default_profile,
        geq_max_range=geq_max_range, ao_enabled=ao_enabled)


@pytest.mark.parametrize("threshold_high,volmax_boost,disabled,expect_warn", [
    ([0.0] * 20, 6.0, set(), True),            # inert regulator + boost
    ([-6.0] * 20, 6.0, set(), False),          # regulator actually limits
    ([0.0] * 20, 0.0, set(), False),           # no boost to warn about
    ([0.0] * 20, 6.0, {"volmax"}, False),      # boost disabled
    ([0.0] * 20, 6.0, {"regulator"}, False),   # limiter fallback path
])
def test_report_warns_only_when_volmax_rides_inert_regulator(
        monkeypatch, capsys, threshold_high, volmax_boost, disabled,
        expect_warn):
    """The 'regulator never engages' heads-up fires exactly when the volmax
    boost rides a regulator whose bands are all threshold >= 0 dB."""
    import dolby_to_easyeffects as d
    monkeypatch.setattr(d, "_CONSOLE", None)   # plain print so capsys sees it
    _report_parsed_profile(
        _report_tuning(synthetic_regulator(threshold_high), volmax_boost),
        [0.0] * 20, [0.0] * 20, 0.1, disabled)
    out = capsys.readouterr().out
    assert ("regulator never engages" in out) is expect_warn


def _peaked_ao(peak_band, peak_db):
    """A 20-band AO curve that is flat at 0 dB except for one boosted band."""
    ao = [0.0] * 20
    ao[peak_band] = peak_db
    return ao


@pytest.mark.parametrize("peak_db,peak_limited,volmax,disabled,enabled,expect_warn", [
    (12.0, False, 9.0, set(), set(), True),      # issue #46's T495 shape
    (12.0, True, 9.0, set(), set(), False),      # regulator covers the peak band
    (9.0, False, 9.0, set(), set(), False),      # boost short of the full range
    (12.0, False, 0.0, set(), set(), False),     # no volmax riding on top
    (12.0, False, 9.0, {"volmax"}, set(), False),
    (12.0, False, 9.0, set(), {"coupled-bands"}, False),   # that band now limited
])
def test_report_warns_when_biggest_boost_lands_on_an_unlimited_band(
        monkeypatch, capsys, peak_db, peak_limited, volmax, disabled, enabled,
        expect_warn):
    """Issue #46: the regulator limits *somewhere*, so the all-inert warning
    stays quiet, but the band carrying the largest correction boost is one it
    leaves alone — that boost plus volmax hits the brickwall unprotected."""
    import dolby_to_easyeffects as d
    monkeypatch.setattr(d, "_CONSOLE", None)   # plain print so capsys sees it
    peak_band = 1
    th = [-6.0 if peak_limited else 0.0] * 20
    th[10] = -6.0                              # limits somewhere regardless
    ao = _peaked_ao(peak_band, peak_db)
    _report_parsed_profile(
        _report_tuning(synthetic_regulator(th, isolated_band=[0] * 20), volmax),
        ao, ao, 0.1, disabled, enabled=enabled)
    out = capsys.readouterr().out
    assert ("leaves unlimited" in out) is expect_warn
    if expect_warn:                            # names the offending band
        assert f"{SYNTHETIC_FREQS_20[peak_band]} Hz" in out


def test_report_unlimited_boost_warning_uses_the_xml_declared_range(
        monkeypatch, capsys):
    """The gate is this XML's own stated per-band gain range, not a fixed
    +12 dB: a file declaring a wider range needs a bigger boost to trip it."""
    import dolby_to_easyeffects as d
    monkeypatch.setattr(d, "_CONSOLE", None)
    th = [0.0] * 20
    th[10] = -6.0
    ao = _peaked_ao(1, 12.0)
    _report_parsed_profile(
        _report_tuning(synthetic_regulator(th, isolated_band=[0] * 20), 9.0,
                       geq_max_range=256),     # 16 dB range; +12 is mid-scale
        ao, ao, 0.1, set())
    assert "leaves unlimited" not in capsys.readouterr().out


@pytest.mark.parametrize("profile_used,declared,expect_note", [
    ("dynamic", "music", True),     # issue #46: XML ships on music, we build dynamic
    ("music", "music", False),      # already building what Dolby names
    ("dynamic", None, False),       # the common case — no declaration at all
])
def test_report_notes_dolby_declared_default_profile(
        monkeypatch, capsys, profile_used, declared, expect_note):
    """<setting><default_profile> names the profile the device ships on under
    Windows. We still build the first profile, so say when they differ."""
    import dolby_to_easyeffects as d
    monkeypatch.setattr(d, "_CONSOLE", None)   # plain print so capsys sees it
    _report_parsed_profile(
        _report_tuning(synthetic_regulator([-6.0] * 20), 6.0,
                       profile_used=profile_used, default_profile=declared),
        [0.0] * 20, [0.0] * 20, 0.1, set())
    out = capsys.readouterr().out
    assert ("--profile music" in out) is expect_note


def test_parse_xml_reads_declared_default_profile(tmp_path):
    """The field is read off <setting>, and its absence stays None rather than
    becoming a phantom mismatch."""
    from tests.conftest import write_synthetic_tuning_xml
    import dolby_to_easyeffects as d

    declared = write_synthetic_tuning_xml(tmp_path / "a.xml", default_profile="music")
    tuning = d.parse_xml(declared)
    assert tuning.default_profile == "music"
    assert tuning.profile_used == "dynamic"

    plain = write_synthetic_tuning_xml(tmp_path / "b.xml")
    assert d.parse_xml(plain).default_profile is None


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

    off = d.parse_xml(_xml_with_ao_enable(tmp_path, "off.xml", 0))
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
        tuning = d.parse_xml(_xml_with_ao_enable(tmp_path, name, value))
        assert tuning.ao_enabled is True, name
        assert any(v != 0 for v in tuning.ao_left), name
        assert any(v != 0 for v in tuning.ao_right), name


def test_report_explains_a_flat_curve_caused_by_the_enable_gate(tmp_path, capsys):
    """Zeros in the printed curve would otherwise read as a flat tuning."""
    import dolby_to_easyeffects as d

    tuning = d.parse_xml(_xml_with_ao_enable(tmp_path, "off.xml", 0))
    capsys.readouterr()
    d._report_parsed_profile(tuning, [0.0] * 20, [0.0] * 20, 0.1, set())
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
    assert d.parse_xml(path).leveler_substages == ["volume-leveler-compressor"]

    plain = write_synthetic_tuning_xml(tmp_path / "plain.xml")
    assert d.parse_xml(plain).leveler_substages == []


@pytest.mark.parametrize("autogain_on,expect", [
    (False, "only matters if you rebuild with --enable autogain"),
    (True, "pumps or overshoots"),
])
def test_substage_summary_escalates_under_autogain(capsys, autogain_on, expect):
    """Silent-but-present on a default run (the leveler is bypassed, so it
    genuinely cannot be heard); the full evidence ask once autogain is on."""
    import dolby_to_easyeffects as d

    d._unmodeled_summary([], ["volume-leveler-compressor"], autogain_on)
    out = capsys.readouterr().out
    assert expect in out
    # The ask is only worth making when it can be acted on.
    assert (d._REPORT_URL in out) is autogain_on


def test_substage_summary_silent_when_there_is_nothing_to_say(capsys):
    import dolby_to_easyeffects as d

    d._unmodeled_summary([], [], autogain_on=True)
    assert capsys.readouterr().out == ""


def test_report_url_is_never_split_across_lines(capsys):
    """A URL broken by wrapping can't be clicked or copied, which defeats
    the ask it belongs to."""
    import dolby_to_easyeffects as d

    d._unmodeled_summary([], ["volume-leveler-compressor"], autogain_on=True)
    lines = capsys.readouterr().out.splitlines()
    assert any(d._REPORT_URL in line for line in lines)


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


def test_loaded_preset_generated_passes():
    rc = {"last_output_preset": "Dolby-Balanced", "fallback_preset": "", "uses_fallback": False}
    assert loaded_preset_status(rc, ["Dolby-Balanced", "Nothing"]).status == DOCTOR_PASS


def test_loaded_preset_non_generated_warns():
    rc = {"last_output_preset": "SomethingElse", "fallback_preset": "", "uses_fallback": False}
    assert loaded_preset_status(rc, ["Dolby-Balanced"]).status == DOCTOR_WARN


def test_loaded_preset_bypass_selected_warns():
    """TRAP: the silent 'Nothing' bypass preset is in the generated set but is
    itself a 'sounds like nothing' state — it must WARN, not PASS."""
    rc = {"last_output_preset": BYPASS_PRESET_NAME, "fallback_preset": "",
          "uses_fallback": False}
    r = loaded_preset_status(rc, ["Dolby-Balanced", BYPASS_PRESET_NAME])
    assert r.status == DOCTOR_WARN
    assert BYPASS_PRESET_NAME in r.detail


def test_loaded_preset_names_the_matched_fallback_not_the_loaded():
    """When the PASS is due to the fallback, the message names the fallback,
    not a non-generated last-loaded preset."""
    rc = {"last_output_preset": "SomethingElse", "fallback_preset": "Dolby-Balanced",
          "uses_fallback": True}
    r = loaded_preset_status(rc, ["Dolby-Balanced"])
    assert r.status == DOCTOR_PASS
    assert "Dolby-Balanced" in r.detail and "SomethingElse" not in r.detail


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
    assert _doctor_summary(checks) == (1, 1, 2, 1)


def test_doctor_report_unknown_not_summarised_as_clean(monkeypatch, capsys):
    """TRAP: an UNKNOWN-only report must NOT print the green 'No blocking
    problems detected' line, and the summary must surface the UNKNOWN count."""
    import dolby_to_easyeffects as d
    monkeypatch.setattr(d, "_CONSOLE", None)   # plain print so capsys sees it
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
    import dolby_to_easyeffects as d

    def boom(*a, **k):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(d.subprocess, "run", boom)
    monkeypatch.setattr(d.shutil, "which", lambda _name: None)
    probe = d._probe_ee_version()
    assert probe.version is None and probe.found is False
    assert probe.silent is None


def test_probe_ee_version_prefers_parseable_over_unreadable(monkeypatch):
    """#22 review: a found-but-unparseable install (e.g. a stale/shim native
    binary that exits 0 with no version) must NOT mask a healthy EE on the other
    install — keep probing for a parseable version."""
    import dolby_to_easyeffects as d

    class R:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(cmd, **k):
        if cmd[0] == "easyeffects":      # native answers, but no version token
            return R(0, "easyeffects shim\n")
        if cmd[0] == "flatpak":          # flatpak info has the real version
            return R(0, "ID: x\nVersion: 8.2.1\nInstalled: 458.6 MB\n")
        return R(1, "")

    monkeypatch.setattr(d.subprocess, "run", fake_run)
    probe = d._probe_ee_version()
    assert probe.version == (8, 2, 1) and probe.found is True
    assert probe.is_flatpak is True and probe.source == "flatpak info"


def test_probe_ee_version_degrades_on_timeout(monkeypatch):
    import dolby_to_easyeffects as d

    def slow(*a, **k):
        raise d.subprocess.TimeoutExpired(cmd="easyeffects", timeout=5)

    monkeypatch.setattr(d.subprocess, "run", slow)
    monkeypatch.setattr(d.shutil, "which", lambda _name: None)
    probe = d._probe_ee_version()
    assert probe.version is None and probe.found is False


def test_doctor_and_end_of_run_warning_share_their_wording(monkeypatch, capsys):
    """Each end-of-run warning must render the same explanation its --doctor
    counterpart gives. They used to be two hand-maintained copies and had
    already drifted ("EasyEffects off" vs "EasyEffects disabled"), so assert
    the shared builders are what both sides actually emit."""
    from types import SimpleNamespace

    import dolby_to_easyeffects as d
    monkeypatch.setattr(d, "_CONSOLE", None)   # plain print so capsys sees it
    flat = lambda s: " ".join(s.split())       # noqa: E731 — undo line wrapping

    # EasyEffects 7: doctor detail and the end-of-run banner
    assert flat(d.ee_v7_message("7.1.5")) in flat(
        d.ee_version_status((7, 1, 5), found=True).detail)
    monkeypatch.setattr(d, "_probe_ee_version",
                        lambda: d.EEProbe((7, 1, 5), True, "test", False))
    d.warn_ee_environment(SimpleNamespace(output_dir=d.DEFAULT_OUTPUT_DIR,
                                          irs_dir=d.DEFAULT_IRS_DIR))
    assert flat(d.ee_v7_message("7.1.5")) in flat(capsys.readouterr().out)

    # Old kernel: doctor detail and the end-of-run hint
    old = "6.12.74+deb13+1-amd64"
    assert flat(d.kernel_old_message()) in flat(d.kernel_age_status(old).detail)
    d.warn_old_kernel(old)
    assert flat(d.kernel_old_message()) in flat(capsys.readouterr().out)


def test_probe_ee_version_installed_but_headless(monkeypatch):
    """Issue #46: EE 8's Qt binary needs a display to answer --version, so from
    a headless shell it exits non-zero. An installed EE must not be reported as
    missing — the probe records *why* it stayed silent instead."""
    import dolby_to_easyeffects as d

    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def fake_run(cmd, **k):
        if cmd[0] == "easyeffects":
            return R(1, "", "qt.qpa.plugin: could not connect to display\n")
        return R(1, "", "error: com.github.wwmm.easyeffects not installed\n")

    monkeypatch.setattr(d.subprocess, "run", fake_run)
    monkeypatch.setattr(d.shutil, "which", lambda name: "/usr/bin/easyeffects"
                        if name == "easyeffects" else None)
    probe = d._probe_ee_version()
    assert probe.found is False and probe.version is None
    assert "could not connect to display" in probe.silent

    # …and it surfaces as UNKNOWN with an accurate message, not "not found".
    status = d.ee_version_status(probe.version, probe.found, probe.silent)
    assert status.status == d.DOCTOR_UNKNOWN
    assert "installed" in status.detail and "not found" not in status.detail


def test_probe_ee_version_absent_flatpak_is_not_silent(monkeypatch):
    """`flatpak info` exits non-zero exactly when the app isn't installed, so
    that failure means absence — it must not be reported as "installed but
    unreachable"."""
    import dolby_to_easyeffects as d

    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    monkeypatch.setattr(d.subprocess, "run",
                        lambda cmd, **k: R(1, "", "not installed\n"))
    monkeypatch.setattr(d.shutil, "which", lambda _name: None)
    probe = d._probe_ee_version()
    assert probe.found is False and probe.silent is None
    assert d.ee_version_status(probe.version, probe.found,
                               probe.silent).status == d.DOCTOR_WARN


def test_easyeffects_is_running_degrades_on_missing_pgrep(monkeypatch):
    import dolby_to_easyeffects as d

    def boom(*a, **k):
        raise FileNotFoundError("no pgrep")

    monkeypatch.setattr(d.subprocess, "run", boom)
    assert d.easyeffects_is_running() is False


def test_easyeffects_is_running_degrades_on_permission_error(monkeypatch):
    """TRAP: a sandboxed/SELinux host where pgrep raises PermissionError (an
    OSError that is NOT FileNotFoundError/SubprocessError) must not crash the
    doctor's fact-gathering."""
    import dolby_to_easyeffects as d

    def denied(*a, **k):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(d.subprocess, "run", denied)
    assert d.easyeffects_is_running() is False
