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

import numpy as np
import pytest

from dolby_to_easyeffects import (
    FIR_LENGTH,
    SAMPLE_RATE,
    make_fir,
    make_peq_eq,
    make_preset,
    save_wav_stereo,
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
        surround={"enable": True, "boost": 4},
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


# --- TRAP: HDA autogain bypass ---
# CLAUDE.md: "Pumping or saturation on quiet → loud transitions — the
# reason autogain is bypassed by default; re-enabling or moving it
# will likely reintroduce it unless Media Intelligence steering is
# somehow approximated."

def test_hda_autogain_is_bypassed(generated):
    """HDA preset (default) emits autogain#0 with bypass=True. Removing
    that bypass re-introduces the pumping/saturation trap.
    """
    preset, _ = generated
    autogain = preset["output"].get("autogain#0")
    assert autogain is not None, \
        "autogain#0 should be present (bypassed) so users can A/B with it"
    assert autogain["bypass"] is True, \
        "HDA autogain must be bypassed by default — re-enabling it without " \
        "MI-style steering reintroduces pumping on quiet→loud transitions"


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
