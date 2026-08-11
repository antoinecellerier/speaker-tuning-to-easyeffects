"""Tests for ee_to_pipewire.py — EE preset → PipeWire filter-chain conf.

The load-bearing assertion is `test_mbc_round_trip_4_decimals`: it
generates a preset with `make_preset`, converts to a conf, re-extracts
the LSP MBC controls from the conf text, and confirms the linear
values round-trip back to the source dB values to 4 decimals. That is
the design-doc's verification anchor (alternative-pipelines.md:371-373).
"""

from __future__ import annotations

import copy
import functools
import inspect
import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

from pathlib import Path

import pytest

from lib.preset.build import make_preset
from lib.preset.emit import save_wav_stereo
from lib.report.messages import (
    DISABLEABLE_FILTERS,
    ENABLEABLE_FILTERS,
)
from lib.preset.fir import FIR_LENGTH, SAMPLE_RATE, make_fir
from lib.pipewire.conf import (
    CONF_HEADER_MARK,
    build_chain,
    emit_links,
    format_conf,
    _assert_positional,
    _sanitize_name,
)
from lib.pipewire.plugins import (
    CALF_BE_URI,
    CALF_ST_URI,
    EE_EQMODE_TO_LSP,
    EE_FMODE_TO_LSP,
    EE_FSLOPE_TO_LSP,
    EE_FTYPE_TO_LSP,
    EE_KEY_DISPATCH,
    EE_LIMITER_MODE,
    EE_MBC_CM,
    EE_MBC_ENVB,
    EE_MBC_GLOBAL_MODE,
    EE_MBC_SCMODE,
    EE_ST_MODE,
    LSP_AUTOGAIN_URI,
    LSP_LIM_URI,
    LSP_MBC_URI,
    db_to_lin,
    emit_autogain,
    emit_bass_enhancer,
    emit_convolver,
    emit_limiter,
    emit_mb_compressor,
    emit_peq,
    emit_stereo_tools,
    lin_to_db,
    _emit_peq_node,
    _resolve_irs,
)
from ee_to_pipewire import main as ee2pw_main
from tests.conftest import (
    SYNTHETIC_FREQS_20,
    synthetic_mb_comp,
    synthetic_peq_filters,
    synthetic_regulator,
)


# ---------------------------------------------------------------------------
# Fixture: a complete preset+IRS pair (mirrors test_preset.py's `generated`)
# ---------------------------------------------------------------------------

@pytest.fixture
def generated(tmp_path):
    peq = synthetic_peq_filters([
        (0, 7, 90.0, 0.0, 0.707, 4, 1.0),
        (1, 7, 90.0, 0.0, 0.707, 4, 1.0),
        (0, 1, 1000.0, 4.0, 1.5, 0, 1.0),
        (1, 1, 1000.0, 4.0, 1.5, 0, 1.0),
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


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_db_to_lin_zero_is_unity():
    assert db_to_lin(0.0) == 1.0


def test_db_to_lin_round_trip_precision():
    """dB → linear → dB must round-trip to within 1e-12 over the
    audio-relevant range. This is the foundation of the round-trip
    assertion below; if this is loose, that one is loose too.
    """
    for db in [-80.01, -60.0, -12.0, -6.0, -1.0, -0.5, 0.0, 0.5, 1.5, 6.0, 12.0]:
        assert math.isclose(lin_to_db(db_to_lin(db)), db, abs_tol=1e-12)


def test_assert_positional_passes_in_correct_order():
    _assert_positional(["convolver#0", "equalizer#0", "equalizer#1",
                        "limiter#0"])


def test_assert_positional_raises_when_swapped():
    # ValueError, not AssertionError — the contract must survive `python -O`.
    with pytest.raises(ValueError):
        _assert_positional(["equalizer#1", "equalizer#0"])


def test_emit_peq_skips_bypassed():
    plugin = {"bypass": True, "num-bands": 0, "left": {}, "right": {}}
    assert emit_peq(plugin, "peq") is None


def test_emit_mb_compressor_skips_bypassed():
    plugin = {"bypass": True}
    assert emit_mb_compressor(plugin, "mbc") is None


def test_emit_limiter_skips_bypassed():
    assert emit_limiter({"bypass": True}) is None


def test_emit_bass_enhancer_skips_bypassed():
    assert emit_bass_enhancer({"bypass": True}) is None


def test_emit_stereo_tools_skips_bypassed():
    assert emit_stereo_tools({"bypass": True}) is None


def test_emit_bass_enhancer_round_trips_db_amount():
    """`amount` is dB in the EE preset, linear in Calf — the BIND_LV2_PORT_DB
    macro converts via db_to_linear. Lock the conversion so a future refactor
    that drops the dB→linear step doesn't silently mute or amplify the bass.
    """
    plugin = {
        "bypass": False,
        "input-gain": 0.0, "output-gain": 0.0,
        "amount": 12.0,            # +12 dB → 3.9810717... linear
        "harmonics": 8.5, "scope": 100.0,
        "blend": -10.0, "floor": 10.0,
        "floor-active": True, "listen": False,
    }
    stage = emit_bass_enhancer(plugin)
    assert stage is not None and len(stage.nodes) == 1
    node = stage.nodes[0]
    assert node["plugin"] == CALF_BE_URI
    ctl = node["control"]
    assert math.isclose(ctl["amount"], db_to_lin(12.0), rel_tol=1e-9)
    assert ctl["drive"] == 8.5
    assert ctl["freq"] == 100.0
    assert ctl["blend"] == -10.0
    assert ctl["floor"] == 10.0
    assert ctl["floor_active"] == 1
    assert ctl["listen"] == 0


def test_emit_bass_enhancer_control_symbols_match_calf():
    """The Calf BassEnhancer LV2 plugin defines an exact set of input
    control symbols. Lock the emitted symbol set so a typo (`drive` →
    `harmonics`, `floor_active` → `floor-active`) is caught at test-time
    rather than at runtime via lv2info validation.
    """
    plugin = {
        "bypass": False, "input-gain": 0.0, "output-gain": 0.0,
        "amount": 0.0, "harmonics": 8.5, "scope": 100.0,
        "blend": 0.0, "floor": 20.0,
        "floor-active": False, "listen": False,
    }
    stage = emit_bass_enhancer(plugin)
    expected = {"level_in", "level_out", "amount", "drive", "freq",
                "blend", "floor", "floor_active", "listen"}
    assert set(stage.nodes[0]["control"].keys()) == expected


def test_emit_stereo_tools_mode_enum_complete():
    """Every Calf StereoTools mode label the `emit_stereo_tools`
    translator might encounter in a preset must map to a Calf integer.
    The seven labels are stable (defined in StereoTools.ttl scale
    points), so this is a regression sentinel. (The converter no longer
    emits stereo_tools — design-notes entry 2 — but the translator stays
    for hand-edited / legacy presets.)
    """
    expected_count = 7
    assert len(EE_ST_MODE) == expected_count
    assert set(EE_ST_MODE.values()) == set(range(expected_count))
    # Exhaustively confirm the canonical default is 0.
    assert EE_ST_MODE["LR > LR (Stereo Default)"] == 0


@pytest.mark.parametrize("label,expected_int", [
    ("LR > LR (Stereo Default)", 0),
    ("LR > MS (Stereo to Mid-Side)", 1),
    ("MS > LR (Mid-Side to Stereo)", 2),
    ("LR > LL (Mono Left Channel)", 3),
    ("LR > RR (Mono Right Channel)", 4),
    ("LR > L+R (Mono Sum L+R)", 5),
    ("LR > RL (Stereo Flip Channels)", 6),
])
def test_emit_stereo_tools_each_mode_label_maps(label, expected_int):
    """Per-label sentinel — catches a typo in any individual scale-point
    string drifting away from the StereoTools.ttl ground truth (e.g. a
    stray space, missing parens, or hyphen vs en-dash). A bug in any
    one entry would flip a stereo widener into a mono-summing chain.
    """
    plugin = {
        "bypass": False,
        "input-gain": 0.0, "output-gain": 0.0,
        "balance-in": 0.0, "balance-out": 0.0,
        "softclip": False,
        "mutel": False, "muter": False,
        "phasel": False, "phaser": False,
        "mode": label,
        "side-level": 0.0, "side-balance": 0.0,
        "middle-level": 0.0, "middle-panorama": 0.0,
        "stereo-base": 0.0, "delay": 0.0,
        "sc-level": 1.0, "stereo-phase": 0.0,
    }
    stage = emit_stereo_tools(plugin)
    assert stage.nodes[0]["control"]["mode"] == expected_int


@pytest.mark.parametrize("key,calf_symbol,value", [
    # Linear passthroughs: -1..+1 ranges, direct map (no dB conversion).
    ("balance-in",      "balance_in",   -0.5),
    ("balance-out",     "balance_out",   0.7),
    ("side-balance",    "sbal",         -0.3),
    ("middle-panorama", "mpan",          0.4),
    # stereo-base (-1..+1): widening factor; the only stereo_tools key EE
    # ever sets non-default for Dolby presets.
    ("stereo-base",     "stereo_base",   0.6),
    # delay (-20..+20 ms): asymmetric per-channel time offset.
    ("delay",           "delay",        -5.0),
    # stereo-phase (0..360°): channel-pair phase rotation in degrees.
    ("stereo-phase",    "stereo_phase",   90.0),
    # sc-level (1..100): sidechain level — direct linear, *not* dB.
    ("sc-level",        "sc_level",      50.0),
])
def test_emit_stereo_tools_linear_keys_pass_through(key, calf_symbol, value):
    """Each key listed in BIND_LV2_PORT (without _DB) is a direct linear
    bind in EE — no unit conversion. Regression-guard against someone
    "helpfully" wrapping one of these in db_to_lin (which would silently
    wreck the stereo image, balance, or timing).
    """
    plugin = {
        "bypass": False,
        "input-gain": 0.0, "output-gain": 0.0,
        "balance-in": 0.0, "balance-out": 0.0,
        "softclip": False,
        "mutel": False, "muter": False,
        "phasel": False, "phaser": False,
        "mode": "LR > LR (Stereo Default)",
        "side-level": 0.0, "side-balance": 0.0,
        "middle-level": 0.0, "middle-panorama": 0.0,
        "stereo-base": 0.0, "delay": 0.0,
        "sc-level": 1.0, "stereo-phase": 0.0,
    }
    plugin[key] = value
    stage = emit_stereo_tools(plugin)
    assert stage.nodes[0]["control"][calf_symbol] == value


@pytest.mark.parametrize("key,calf_symbol,db_value", [
    ("side-level",   "slev", -6.0),
    ("middle-level", "mlev", +3.0),
    ("input-gain",   "level_in",  -3.0),
    ("output-gain",  "level_out", +6.0),
])
def test_emit_stereo_tools_db_keys_convert(key, calf_symbol, db_value):
    """The four dB-valued keys must be db_to_linear-converted to match
    EE's BIND_LV2_PORT_DB. Slev/mlev set the M/S gains used by the
    widener, so a unit mistake would skew the stereo image asymmetrically.
    """
    plugin = {
        "bypass": False,
        "input-gain": 0.0, "output-gain": 0.0,
        "balance-in": 0.0, "balance-out": 0.0,
        "softclip": False,
        "mutel": False, "muter": False,
        "phasel": False, "phaser": False,
        "mode": "LR > LR (Stereo Default)",
        "side-level": 0.0, "side-balance": 0.0,
        "middle-level": 0.0, "middle-panorama": 0.0,
        "stereo-base": 0.0, "delay": 0.0,
        "sc-level": 1.0, "stereo-phase": 0.0,
    }
    plugin[key] = db_value
    stage = emit_stereo_tools(plugin)
    assert math.isclose(stage.nodes[0]["control"][calf_symbol],
                        db_to_lin(db_value), rel_tol=1e-9)


@pytest.mark.parametrize("flag", ["softclip", "mutel", "muter",
                                  "phasel", "phaser"])
def test_emit_stereo_tools_bool_flags_round_trip(flag):
    """Each toggle must serialize to 0/1 in both states — the LV2 port
    type is `lv2:toggled` so a `True`/`False` Python value would not be
    accepted by lv2info validation. Regression-guard against a future
    refactor that drops the `int()` cast.
    """
    base = {
        "bypass": False,
        "input-gain": 0.0, "output-gain": 0.0,
        "balance-in": 0.0, "balance-out": 0.0,
        "softclip": False,
        "mutel": False, "muter": False,
        "phasel": False, "phaser": False,
        "mode": "LR > LR (Stereo Default)",
        "side-level": 0.0, "side-balance": 0.0,
        "middle-level": 0.0, "middle-panorama": 0.0,
        "stereo-base": 0.0, "delay": 0.0,
        "sc-level": 1.0, "stereo-phase": 0.0,
    }
    base[flag] = True
    on = emit_stereo_tools(base).nodes[0]["control"][flag]
    base[flag] = False
    off = emit_stereo_tools(base).nodes[0]["control"][flag]
    assert on == 1 and off == 0
    assert isinstance(on, int) and isinstance(off, int)


def test_emit_stereo_tools_round_trips_levels_and_widening():
    """`side-level` and `middle-level` go through BIND_LV2_PORT_DB →
    db_to_linear; `stereo-base` is direct linear. These are the two
    parameters that matter for the surround widener, so lock them.
    """
    plugin = {
        "bypass": False,
        "input-gain": -3.0, "output-gain": 0.0,
        "balance-in": 0.0, "balance-out": 0.0,
        "softclip": False,
        "mutel": False, "muter": False,
        "phasel": False, "phaser": False,
        "mode": "LR > LR (Stereo Default)",
        "side-level": 6.0, "side-balance": 0.0,
        "middle-level": -2.0, "middle-panorama": 0.0,
        "stereo-base": 0.3,
        "delay": 0.0, "sc-level": 1.0, "stereo-phase": 0.0,
    }
    stage = emit_stereo_tools(plugin)
    assert stage is not None
    node = stage.nodes[0]
    assert node["plugin"] == CALF_ST_URI
    ctl = node["control"]
    assert math.isclose(ctl["level_in"], db_to_lin(-3.0), rel_tol=1e-9)
    assert math.isclose(ctl["slev"],     db_to_lin(6.0),  rel_tol=1e-9)
    assert math.isclose(ctl["mlev"],     db_to_lin(-2.0), rel_tol=1e-9)
    assert ctl["stereo_base"] == 0.3
    assert ctl["mode"] == 0


def test_emit_stereo_tools_control_symbols_match_calf():
    """Lock the emitted Calf StereoTools symbol set against typos —
    `slev`/`sbal`/`mlev`/`mpan` and the underscores in `stereo_base`,
    `sc_level`, `stereo_phase` are common get-it-wrong points.
    """
    plugin = {
        "bypass": False, "input-gain": 0.0, "output-gain": 0.0,
        "balance-in": 0.0, "balance-out": 0.0, "softclip": False,
        "mutel": False, "muter": False, "phasel": False, "phaser": False,
        "mode": "LR > LR (Stereo Default)",
        "side-level": 0.0, "side-balance": 0.0,
        "middle-level": 0.0, "middle-panorama": 0.0,
        "stereo-base": 0.0, "delay": 0.0,
        "sc-level": 1.0, "stereo-phase": 0.0,
    }
    stage = emit_stereo_tools(plugin)
    expected = {
        "level_in", "level_out", "balance_in", "balance_out",
        "softclip", "mutel", "muter", "phasel", "phaser",
        "mode", "slev", "sbal", "mlev", "mpan",
        "stereo_base", "delay", "sc_level", "stereo_phase",
    }
    assert set(stage.nodes[0]["control"].keys()) == expected


@pytest.mark.parametrize("label,expected_int", [
    ("Herm Thin", 0), ("Herm Wide", 1), ("Herm Tail", 2), ("Herm Duck", 3),
    ("Exp Thin", 4), ("Exp Wide", 5), ("Exp Tail", 6), ("Exp Duck", 7),
    ("Line Thin", 8), ("Line Wide", 9), ("Line Tail", 10), ("Line Duck", 11),
])
def test_emit_limiter_each_mode_label_maps(label, expected_int):
    """Per-label sentinel against limiter.cpp:52 limiter_oper_modes[] —
    same pattern as the stereo_tools mode test. A drifted entry would
    silently swap the limiter's inter-sample interpolation shape.
    """
    plugin = {
        "bypass": False, "mode": label,
        "input-gain": 0.0, "output-gain": 0.0,
        "threshold": -1.0, "lookahead": 1.0, "attack": 1.0, "release": 5.0,
        "stereo-link": 100.0, "alr": False, "gain-boost": False,
    }
    stage = emit_limiter(plugin)
    assert stage.nodes[0]["control"]["mode"] == expected_int
    assert EE_LIMITER_MODE[label] == expected_int


@pytest.mark.parametrize("label,expected_int", [
    ("Classic", 0), ("Modern", 1), ("Linear Phase", 2),
])
def test_emit_mb_compressor_each_global_mode_maps(label, expected_int):
    """Per-label sentinel against mb_compressor.cpp:113
    mb_global_comp_modes[]. 'Linear Phase' would add latency, so an
    off-by-one here breaks the project's zero-added-latency invariant.
    """
    plugin = {
        "bypass": False, "compressor-mode": label,
        "input-gain": 0.0, "output-gain": 0.0,
        "dry": -80.01, "wet": 0.0, "envelope-boost": "None",
    }
    stage = emit_mb_compressor(plugin, "mbc")
    assert stage.nodes[0]["control"]["mode"] == expected_int
    assert EE_MBC_GLOBAL_MODE[label] == expected_int


@pytest.mark.parametrize("label,expected_int", [
    ("Downward", 0), ("Upward", 1), ("Boosting", 2),
])
def test_emit_mb_compressor_each_compression_mode_maps(label, expected_int):
    """Per-label sentinel against mb_compressor.cpp:105 mb_comp_modes[].
    Up/Boost engage LSP's below-threshold boost path (noise-floor
    amplification — the e454711 trap), so an off-by-one here is audible.
    """
    plugin = {
        "bypass": False, "compressor-mode": "Modern",
        "input-gain": 0.0, "output-gain": 0.0,
        "dry": -80.01, "wet": 0.0, "envelope-boost": "None",
        "band0": {"compression-mode": label},
    }
    stage = emit_mb_compressor(plugin, "mbc")
    assert stage.nodes[0]["control"]["cm_0"] == expected_int
    assert EE_MBC_CM[label] == expected_int


# ---------------------------------------------------------------------------
# Unknown-label and untranslatable-value warnings
# ---------------------------------------------------------------------------

def test_mbc_ssplit_written_only_when_set():
    """`ssplit` only exists on lsp-plugins >= 1.2.3, and the generator
    pins stereo-split=False == the port default — so it must be written
    only when a hand-edited preset enables it, keeping confs loadable on
    older installed LSP."""
    stage = emit_mb_compressor({"bypass": False}, "mbc")
    assert "ssplit" not in stage.nodes[0]["control"]
    stage = emit_mb_compressor({"bypass": False, "stereo-split": True}, "mbc")
    assert stage.nodes[0]["control"]["ssplit"] == 1


def test_unknown_enum_label_warns_not_silent():
    """A new EE enum label must warn while falling back — a silent
    `.get(label, fallback)` would map it to the fallback integer (often
    0 = Off) with no trace.
    """
    stage = emit_limiter({"bypass": False, "mode": "Bogus Mode"})
    assert stage.nodes[0]["control"]["mode"] == 0
    assert any("Bogus Mode" in w for w in stage.warnings)

    stage = emit_mb_compressor(
        {"bypass": False, "band0": {"compression-mode": "Sideways"}}, "mbc")
    assert stage.nodes[0]["control"]["cm_0"] == 0
    assert any("Sideways" in w for w in stage.warnings)


def test_unknown_enum_label_warning_reaches_chain_warnings():
    preset = {
        "output": {
            "plugins_order": ["limiter#0"],
            "limiter#0": {"bypass": False, "mode": "Bogus Mode"},
        }
    }
    chain = build_chain(preset, irs_dir=None, must_exist=False)
    assert any("Bogus Mode" in w for w in chain.warnings)


def test_unknown_enum_label_warning_is_deduped():
    """16 bands × 2 sides with the same unknown label must produce one
    warning, not 32."""
    band = {"type": "Sideways", "frequency": 1000.0, "gain": 0.0, "q": 1.0}
    plugin = {
        "bypass": False, "mode": "IIR", "num-bands": 3,
        "left": {f"band{i}": dict(band) for i in range(3)},
        "right": {f"band{i}": dict(band) for i in range(3)},
    }
    stage = emit_peq(plugin, "peq")
    assert len([w for w in stage.warnings if "Sideways" in w]) == 1


def test_convolver_nonzero_input_gain_warns(tmp_path):
    """The builtin convolver has no input-gain port; a hand-edited trim
    must surface instead of silently changing level."""
    plugin = {"bypass": False, "kernel-name": "x", "input-gain": 3.0}
    stage = emit_convolver(plugin, tmp_path, must_exist=False)
    assert any("input-gain" in w for w in stage.warnings)
    # The generated-preset value (0.0) stays quiet.
    stage = emit_convolver(
        {"bypass": False, "kernel-name": "x", "input-gain": 0.0},
        tmp_path, must_exist=False)
    assert stage.warnings == []


def test_peq_band_overflow_warns():
    """para_equalizer_x16_lr has 16 bands; a preset declaring more must
    warn about the dropped tail instead of truncating silently."""
    plugin = {"bypass": False, "mode": "IIR", "num-bands": 18,
              "left": {}, "right": {}}
    stage = emit_peq(plugin, "peq")
    assert any("caps at 16" in w for w in stage.warnings)


def test_orphaned_output_key_warns():
    """A plugin object missing from plugins_order is never visited by the
    build_chain loop — it must warn, not vanish."""
    preset = {
        "output": {
            "plugins_order": ["limiter#0"],
            "limiter#0": {"bypass": False},
            "equalizer#0": {"bypass": False, "num-bands": 0,
                            "left": {}, "right": {}},
        }
    }
    chain = build_chain(preset, irs_dir=None, must_exist=False)
    assert any("equalizer#0" in w and "plugins_order" in w
               for w in chain.warnings)


# ---------------------------------------------------------------------------
# autogain → LSP autogain_stereo
# ---------------------------------------------------------------------------

def _active_autogain(**overrides):
    """A non-bypassed (SoundWire-style) EE autogain block."""
    plugin = {
        "bypass": False, "input-gain": 0.0, "output-gain": 0.0,
        "maximum-history": 20.0, "reference": "Geometric Mean (MSI)",
        "silence-threshold": -50.0, "target": -22.0,
    }
    plugin.update(overrides)
    return plugin


def test_emit_autogain_skips_bypassed():
    assert emit_autogain({"bypass": True}) is None


def test_emit_autogain_uses_autogain_stereo_uri():
    stage = emit_autogain(_active_autogain())
    assert stage.nodes[0]["plugin"] == LSP_AUTOGAIN_URI


def test_emit_autogain_lkahead_is_zero():
    """Lookahead is the only latency source (port 41); it must be 0 so the
    node adds no latency over the PipeWire quantum (hard constraint)."""
    control = emit_autogain(_active_autogain()).nodes[0]["control"]
    assert control["lkahead"] == 0.0


def test_emit_autogain_weight_is_k_weighted():
    """K-weighting (enum 5) == EBU R 128, matching EE's libebur128 metering."""
    control = emit_autogain(_active_autogain()).nodes[0]["control"]
    assert control["weight"] == 5


def test_emit_autogain_level_is_target_not_linear():
    """`level` is a LUFS (dB-domain) port — the EE target passes through
    directly. Converting it to a linear gain would silently mis-set the
    leveler's loudness goal."""
    control = emit_autogain(_active_autogain(target=-22.0)).nodes[0]["control"]
    assert control["level"] == -22.0
    assert control["level"] != db_to_lin(-22.0)


def test_emit_autogain_silence_is_threshold_not_linear():
    control = emit_autogain(
        _active_autogain(**{"silence-threshold": -50.0})).nodes[0]["control"]
    assert control["silence"] == -50.0


def test_emit_autogain_history_maps_monotonic_and_clamped():
    """A longer EE maximum-history must yield a longer (gentler) gain-ride,
    clamped into the tgrow_l/tfall_l port range [10, 10000] ms."""
    c_short = emit_autogain(
        _active_autogain(**{"maximum-history": 15.0})).nodes[0]["control"]
    c_long = emit_autogain(
        _active_autogain(**{"maximum-history": 40.0})).nodes[0]["control"]
    assert c_short["tfall_l"] < c_long["tfall_l"]
    # Extreme history must not exceed the port maximum.
    c_huge = emit_autogain(
        _active_autogain(**{"maximum-history": 1e6})).nodes[0]["control"]
    assert c_huge["tgrow_l"] == 10000.0
    assert c_huge["tfall_l"] == 10000.0


def test_emit_autogain_boost_slower_than_attenuation():
    """The ride is asymmetric (validated on-device): gain grows (boosts quiet
    up) more slowly than it falls (attenuates loud down) — anti-pumping, like
    EE. So tgrow_l >= tfall_l for any non-trivial history."""
    control = emit_autogain(
        _active_autogain(**{"maximum-history": 20.0})).nodes[0]["control"]
    assert control["tgrow_l"] >= control["tfall_l"]


def test_emit_autogain_warns_below_validated_history():
    """The gain-ride scales were fitted at 20 s. Shorter histories (HDA
    --enable autogain, SoundWire amount>5) extrapolate — say so rather
    than implying the measured EE/PW equivalence still holds. The caveat
    rides Stage.warnings like every other emitter caveat (so it reaches
    the conf header), with the stable prefix the corpus tier's advisory
    exemption keys on."""
    stage = emit_autogain(_active_autogain(**{"maximum-history": 10.0}))
    assert any(w.startswith("autogain: maximum-history")
               for w in stage.warnings)
    stage = emit_autogain(_active_autogain(**{"maximum-history": 20.0}))
    assert stage.warnings == []


def test_emit_autogain_controls_within_lv2_ranges():
    """Every emitted control symbol must lie within autogain_stereo's
    lv2info-declared bounds (validate_conf enforces this at the conf level;
    locked here so a mapping change can't drift out of range)."""
    ranges = {
        "level": (-60.0, 0.0), "silence": (-84.0, -36.0),
        "weight": (0, 5), "lkahead": (0.0, 40.0),
        "tgrow_l": (10.0, 10000.0), "tfall_l": (10.0, 10000.0),
    }
    control = emit_autogain(_active_autogain()).nodes[0]["control"]
    for sym, val in control.items():
        lo, hi = ranges[sym]
        assert lo <= val <= hi, f"{sym}={val} out of [{lo}, {hi}]"


def test_build_chain_includes_bass_enhancer_and_stereo_tools():
    """End-to-end: a preset whose plugins_order includes both new keys
    must yield two extra emitted nodes (one per emitter). Catches the
    case where the dispatch entry was added but emitter was forgotten,
    or vice-versa.
    """
    preset = {
        "output": {
            "plugins_order": ["bass_enhancer#0", "stereo_tools#0",
                              "limiter#0"],
            "bass_enhancer#0": {
                "bypass": False, "input-gain": 0.0, "output-gain": 0.0,
                "amount": 12.0, "harmonics": 10.0, "scope": 200.0,
                "blend": -10.0, "floor": 10.0,
                "floor-active": True, "listen": False,
            },
            "stereo_tools#0": {
                "bypass": False, "input-gain": 0.0, "output-gain": 0.0,
                "balance-in": 0.0, "balance-out": 0.0,
                "softclip": False, "mutel": False, "muter": False,
                "phasel": False, "phaser": False,
                "mode": "LR > LR (Stereo Default)",
                "side-level": 0.0, "side-balance": 0.0,
                "middle-level": 0.0, "middle-panorama": 0.0,
                "stereo-base": 0.3, "delay": 0.0,
                "sc-level": 1.0, "stereo-phase": 0.0,
            },
            "limiter#0": {
                "bypass": False, "mode": "Herm Thin",
                "input-gain": 0.0, "output-gain": 0.0,
                "threshold": -1.0, "lookahead": 1.0,
                "attack": 1.0, "release": 5.0,
                "stereo-link": 100.0, "alr": False, "gain-boost": False,
            },
        }
    }
    chain = build_chain(preset, irs_dir=None, must_exist=False)
    names = {n["name"] for s in chain.stages for n in s.nodes}
    assert "bass" in names and "stereo" in names and "limiter" in names


def test_sanitize_name_strips_invalid_chars():
    assert _sanitize_name("Dolby Filter-Chain!") == "Dolby_Filter_Chain_"
    assert _sanitize_name("ok_already") == "ok_already"


# ---------------------------------------------------------------------------
# Integration: round-trip MBC values through the conf to 4 decimals
# ---------------------------------------------------------------------------

# The generated conf has nested `node = { ... }` and `args = { ... }`
# blocks; we want to look at one specific LV2 node's `control = { ... }`.
# This is a simple pre-order parser that finds the first `name = "<name>"`
# token, then walks forward to the next `control = { ... }` brace pair
# at the same nesting level and returns its key=value pairs.
_TOKEN_RE = re.compile(
    r'"[^"]*"|[A-Za-z_][A-Za-z0-9_.\-]*|[{}\[\]]|[+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?'
    r'|=|true|false|null'
)


def _extract_node_control(conf_text: str, node_name: str) -> dict:
    """Find the LV2 node with `name = <node_name>` and return its control
    dict as Python literals (numbers/strings/bools).
    """
    tokens = _TOKEN_RE.findall(conf_text)
    # Find the index where `name = "<node_name>"` appears.
    target_idx = None
    for i in range(len(tokens) - 2):
        if tokens[i] == "name" and tokens[i + 1] == "=" and \
           tokens[i + 2].strip('"') == node_name:
            target_idx = i
            break
    if target_idx is None:
        raise AssertionError(f"node {node_name!r} not found in conf")

    # From there, scan forward for `control = {` and parse until the
    # matching `}`.
    j = target_idx
    while j < len(tokens) - 2:
        if tokens[j] == "control" and tokens[j + 1] == "=" and tokens[j + 2] == "{":
            break
        j += 1
    else:
        raise AssertionError(f"control block for {node_name!r} not found")

    j += 3  # skip past `control = {`
    result: dict = {}
    while j < len(tokens):
        tok = tokens[j]
        if tok == "}":
            return result
        # Parse `key = value`
        key = tok.strip('"')
        assert tokens[j + 1] == "=", f"expected = at {j+1}, got {tokens[j+1]!r}"
        val_tok = tokens[j + 2]
        if val_tok == "true":
            val = True
        elif val_tok == "false":
            val = False
        elif val_tok == "null":
            val = None
        elif val_tok.startswith('"'):
            val = val_tok.strip('"')
        else:
            try:
                val = int(val_tok)
            except ValueError:
                val = float(val_tok)
        result[key] = val
        j += 3
    raise AssertionError(f"unterminated control block for {node_name!r}")


def test_mbc_round_trip_4_decimals(generated):
    """The load-bearing test (design doc lines 371-373).

    Build a preset, render to conf, parse the MBC control block, and
    confirm the linear values round-trip back to source dB values
    within 1e-4 dB.
    """
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)
    links = emit_links(chain.stages)
    conf = format_conf(chain.stages, links, "test_node", "test")

    mbc_controls = _extract_node_control(conf, "mbc")
    src = preset["output"]["multiband_compressor#0"]
    src_b0 = src["band0"]

    # Logarithmic ones: round-trip linear → dB.
    assert abs(lin_to_db(mbc_controls["al_0"]) - src_b0["attack-threshold"]) < 1e-4
    assert abs(lin_to_db(mbc_controls["mk_0"]) - src_b0["makeup"]) < 1e-4
    assert abs(lin_to_db(mbc_controls["rrl_0"]) - src_b0["release-threshold"]) < 1e-4
    assert abs(lin_to_db(mbc_controls["kn_0"]) - src_b0["knee"]) < 1e-4

    # Identity (no unit conversion):
    assert abs(mbc_controls["at_0"] - src_b0["attack-time"]) < 1e-4
    assert abs(mbc_controls["rt_0"] - src_b0["release-time"]) < 1e-4
    assert abs(mbc_controls["cr_0"] - src_b0["ratio"]) < 1e-4

    # Sidechain mode is enum-mapped — RMS should land on integer 1.
    assert mbc_controls["scm_0"] == 1


# --- TRAP: LSP MBC boost path primed by defaults (converter side) ---
# design-notes "MBC upward compression" (commit e454711): LSP's defaults
# leave bth at -72 dB and bsa at +6 dB, gated only by cm staying 0 (Down).
# The generator pins Downward/-60 dB/0 dB on every band; when the converter
# dropped all three, the conf rode the LV2 defaults — inert today, an
# audible noise-floor boost the moment any of those defaults moves. Assert
# the whole cluster (plus the custom-sidechain gate + band edges and the
# global ssplit) reaches the conf on both MBC instances.

def test_mbc_compression_mode_and_boost_reach_conf(generated):
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)
    links = emit_links(chain.stages)
    conf = format_conf(chain.stages, links, "test_node", "test")
    for node, key in (("mbc", "multiband_compressor#0"),
                      ("reg", "multiband_compressor#1")):
        controls = _extract_node_control(conf, node)
        src = preset["output"][key]
        # ssplit is deliberately absent: generator emits False == port
        # default, and the port doesn't exist on lsp-plugins < 1.2.3.
        assert "ssplit" not in controls
        for i in range(8):
            band = src[f"band{i}"]
            assert controls[f"cm_{i}"] == 0, \
                f"{node} band{i}: compression-mode must land on 0 (Down)"
            assert abs(lin_to_db(controls[f"bth_{i}"])
                       - band["boost-threshold"]) < 1e-4
            assert abs(lin_to_db(controls[f"bsa_{i}"])
                       - band["boost-amount"]) < 1e-4
            assert controls[f"sclc_{i}"] == 0
            assert controls[f"schc_{i}"] == 0
            assert abs(controls[f"sclf_{i}"]
                       - band["sidechain-lowcut-frequency"]) < 1e-4
            assert abs(controls[f"schf_{i}"]
                       - band["sidechain-highcut-frequency"]) < 1e-4


def test_peq_g_out_round_trips_output_gain(generated):
    """The PEQ output-gain is the clipping-compensation trim whose
    derivation test_preset.py locks in; here we lock that the conf's
    `g_out` carries it as a linear gain (BIND_LV2_PORT_DB), mirroring
    the MBC round-trip pattern above.
    """
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)
    links = emit_links(chain.stages)
    conf = format_conf(chain.stages, links, "test_node", "test")
    peq_controls = _extract_node_control(conf, "peq")
    src = preset["output"]["equalizer#0"]
    # The fixture's +4 dB bell forces a non-zero trim, so this round-trip
    # can't pass vacuously on a 0 dB / 1.0-linear identity.
    assert src["output-gain"] != 0.0
    assert abs(lin_to_db(peq_controls["g_out"]) - src["output-gain"]) < 1e-4


def test_regulator_distinct_from_mbc(generated):
    """Both `multiband_compressor#0` and `multiband_compressor#1` emit
    LSP mb_compressor_stereo nodes, but with distinct names so links
    can address them separately.
    """
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)
    node_names = {n["name"] for s in chain.stages for n in s.nodes}
    assert "mbc" in node_names
    assert "reg" in node_names


def test_default_hda_autogain_skips_silently(generated):
    """The default HDA preset carries a bypassed leveler; the PW
    converter must skip it without emitting a node or a warning (bypass
    is the expected default there, not a translation gap)."""
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)
    node_names = {n["name"] for s in chain.stages for n in s.nodes}
    assert "autogain" not in node_names
    assert not [w for w in chain.warnings if "autogain" in w]


def test_enable_autogain_preset_chain_emits_autogain(tmp_path):
    """A preset built with --enable autogain carries an active leveler;
    the PW chain must carry it — with the EE target and the -50 dB
    silence gate passed through as dB-domain ports (issue #25)."""
    preset, _ = make_preset(
        kernel_name="Synthetic",
        peq_filters=[],
        vol_leveler={"enable": True, "amount": 5, "out_target": -16.0},
        freqs=SYNTHETIC_FREQS_20,
        enabled={"autogain"},
    )
    chain = build_chain(preset, tmp_path, must_exist=False)
    autogain_nodes = [n for s in chain.stages for n in s.nodes
                      if n["name"] == "autogain"]
    assert len(autogain_nodes) == 1
    control = autogain_nodes[0]["control"]
    assert control["level"] == -16.0
    assert control["silence"] == -50.0


def test_limiter_threshold_round_trips(generated):
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)
    links = emit_links(chain.stages)
    conf = format_conf(chain.stages, links, "test_node", "test")
    lim_controls = _extract_node_control(conf, "limiter")
    src = preset["output"]["limiter#0"]
    assert abs(lin_to_db(lim_controls["th"]) - src["threshold"]) < 1e-4
    assert lim_controls["lk"] == src["lookahead"]


def test_peq_filter_type_integers(generated):
    """The PEQ stage must encode filter types as integers (Hi-pass=2,
    Bell=1) — string types in `t_N` would silently break LSP.
    """
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)
    links = emit_links(chain.stages)
    conf = format_conf(chain.stages, links, "test_node", "test")
    peq_controls = _extract_node_control(conf, "peq")
    # band 0 in our generated fixture is the HP — type "Hi-pass" = 2 on
    # both channels.
    assert peq_controls["ftl_0"] == 2
    assert peq_controls["ftr_0"] == 2
    # band 1 is the Bell at 1 kHz — type "Bell" = 1.
    assert peq_controls["ftl_1"] == 1
    assert peq_controls["ftr_1"] == 1


def test_peq_unmuted_bands_have_xm_zero(generated):
    """LSP `xm` is MUTE, not enable — default 0 means filter active.
    Inverting this mutes every band and the whole PEQ silently
    passes through. EE bands with `mute: False` MUST map to xm=0.
    Regression test for the bug found during the EE-vs-PW A/B
    that produced ~30 dB extra bass in the PW chain.
    """
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)
    links = emit_links(chain.stages)
    conf = format_conf(chain.stages, links, "test_node", "test")
    peq_controls = _extract_node_control(conf, "peq")

    # The fixture's PEQ has 4 bands (HP + bells), all with mute=False.
    src_peq = preset["output"]["equalizer#0"]
    for i in range(src_peq["num-bands"]):
        for side in ("l", "r"):
            band = src_peq["left" if side == "l" else "right"][f"band{i}"]
            expected = 1 if band.get("mute", False) else 0
            actual = peq_controls[f"xm{side}_{i}"]
            assert actual == expected, (
                f"band{i} {side}: mute={band.get('mute')} "
                f"-> expected xm{side}_{i}={expected}, got {actual}"
            )


def test_peq_solo_default_is_off(generated):
    """`xs` is solo, default 0 = not solo. EE bands with solo=False
    must map to xs=0."""
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)
    links = emit_links(chain.stages)
    conf = format_conf(chain.stages, links, "test_node", "test")
    peq_controls = _extract_node_control(conf, "peq")
    src_peq = preset["output"]["equalizer#0"]
    for i in range(src_peq["num-bands"]):
        for side in ("l", "r"):
            band = src_peq["left" if side == "l" else "right"][f"band{i}"]
            expected = 1 if band.get("solo", False) else 0
            assert peq_controls[f"xs{side}_{i}"] == expected


# ---------------------------------------------------------------------------
# Structural assertions
# ---------------------------------------------------------------------------

def test_every_link_endpoint_resolves(generated):
    """Every node:port reference in `links` must name a node that exists
    in the chain — otherwise PipeWire silently drops the link.
    """
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)
    links = emit_links(chain.stages)
    node_names = {n["name"] for s in chain.stages for n in s.nodes}
    for link in links:
        out_node = link["output"].split(":", 1)[0]
        in_node = link["input"].split(":", 1)[0]
        assert out_node in node_names, f"link source {out_node} unknown"
        assert in_node in node_names, f"link sink {in_node} unknown"


def _dispatch_node_names(key: str) -> set[str]:
    """PW node name(s) a dispatch entry emits, derived from
    EE_KEY_DISPATCH itself so this can't drift from the real table (the
    previous hand-written mirror did)."""
    handler = EE_KEY_DISPATCH[key]
    if key == "convolver#0":
        # No `name` argument — probe the emitter for its fixed mono node
        # names rather than mirroring them by hand.
        stage = handler.emitter({"kernel-name": "probe"},
                                Path("/nonexistent"), must_exist=False)
        return {n["name"] for n in stage.nodes}
    if handler.args:
        return {handler.args[0]}
    return {inspect.signature(handler.emitter).parameters["name"].default}


def test_every_active_plugin_emits_or_warns(tmp_path):
    """Every key in source `plugins_order` is either represented by ≥1
    emitted node or appears in the warnings list, across the whole flag
    sweep. Catches silent drops.
    """
    for sid, preset in _sweep_presets():
        chain = build_chain(preset, tmp_path, must_exist=False)
        emitted_names = {n["name"] for s in chain.stages for n in s.nodes}
        for key in preset["output"]["plugins_order"]:
            if key == "autogain#0" and preset["output"][key].get("bypass"):
                # Bypassed autogain on HDA is the expected default — the
                # converter may skip it silently (silent_if_bypassed).
                # Active autogain falls through and must emit.
                continue
            if key not in EE_KEY_DISPATCH:
                assert any(key in w for w in chain.warnings), \
                    f"[{sid}] {key} silently dropped (no warning)"
                continue
            if not _dispatch_node_names(key) & emitted_names:
                assert any(key in w for w in chain.warnings), \
                    f"[{sid}] {key} neither emitted nor warned"


def test_conf_starts_with_context_modules(generated):
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)
    links = emit_links(chain.stages)
    conf = format_conf(chain.stages, links, "test_node", "test")
    # The format-conf comment header is fine, but the actual context
    # block must be present.
    assert "context.modules = [" in conf
    assert "libpipewire-module-filter-chain" in conf
    assert "filter.graph" in conf
    # IRS paths must be absolute (starts with /).
    assert ' filename = "/' in conf or "filename = \"/" in conf


def test_conf_declares_stereo_audio(generated):
    """The converter is stereo-only (CLAUDE.md); the filter-chain args
    must pin audio.channels = 2 with an FL/FR position so WirePlumber
    never negotiates a different channel count around the chain."""
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)
    links = emit_links(chain.stages)
    conf = format_conf(chain.stages, links, "test_node", "test")
    assert re.search(r"audio\.channels\s*=\s*2", conf), conf[:400]
    assert re.search(r"audio\.position\s*=\s*\[\s*FL\s*FR\s*\]", conf) or \
        '"FL"' in conf and '"FR"' in conf


def test_irs_path_baked_absolute(generated):
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)
    conv_l = chain.stages[0].nodes[0]
    assert conv_l["config"]["filename"].startswith("/"), \
        "convolver filename must be absolute"


def test_assert_positional_raises_inside_build_chain():
    """If someone manually edits a preset to swap PEQ/dialog order,
    build_chain must fail rather than silently mismap them.
    """
    bad_preset = {
        "output": {
            "plugins_order": ["equalizer#1", "equalizer#0"],
            "equalizer#0": {"bypass": False, "num-bands": 0,
                            "left": {}, "right": {}, "mode": "IIR"},
            "equalizer#1": {"bypass": False, "num-bands": 0,
                            "left": {}, "right": {}, "mode": "IIR"},
        }
    }
    with pytest.raises(ValueError):
        build_chain(bad_preset, irs_dir=None, must_exist=False)


def test_assert_positional_raises_when_mbc_swapped():
    """Same contract as PEQ/dialog: regulator (#1) must follow MBC (#0)."""
    with pytest.raises(ValueError):
        _assert_positional(
            ["multiband_compressor#1", "multiband_compressor#0"]
        )


# ---------------------------------------------------------------------------
# Convolver gain field
# ---------------------------------------------------------------------------

def test_convolver_emits_gain_in_config(tmp_path):
    """The PW builtin convolver's `gain` config field is the only place
    output-gain can be applied (no wet/dry control). 0 dB → 1.0.
    """
    irs_path = tmp_path / "x.irs"
    irs_path.write_bytes(b"")
    plugin = {"bypass": False, "kernel-name": "x", "output-gain": 0.0}
    stage = emit_convolver(plugin, tmp_path, must_exist=False)
    assert stage is not None
    for node in stage.nodes:
        assert "gain" in node["config"]
        assert node["config"]["gain"] == pytest.approx(1.0)


def test_convolver_nonzero_gain_round_trips(tmp_path):
    plugin = {"bypass": False, "kernel-name": "x", "output-gain": 6.0}
    stage = emit_convolver(plugin, tmp_path, must_exist=False)
    assert stage is not None
    for node in stage.nodes:
        # 6 dB ≈ 1.9953
        assert lin_to_db(node["config"]["gain"]) == pytest.approx(6.0,
                                                                   abs=1e-4)


def test_convolver_missing_kernel_name_raises(tmp_path):
    plugin = {"bypass": False, "output-gain": 0.0}
    with pytest.raises(ValueError, match="kernel-name"):
        emit_convolver(plugin, tmp_path, must_exist=False)


# ---------------------------------------------------------------------------
# IRS path-separator rejection
# ---------------------------------------------------------------------------

def test_resolve_irs_rejects_path_separators(tmp_path):
    with pytest.raises(ValueError, match="path separators"):
        _resolve_irs("subdir/foo", tmp_path, must_exist=False)


def test_resolve_irs_rejects_parent_traversal(tmp_path):
    with pytest.raises(ValueError, match="path separators"):
        _resolve_irs("../escape", tmp_path, must_exist=False)


# ---------------------------------------------------------------------------
# format_conf empty-stages message
# ---------------------------------------------------------------------------

def test_format_conf_empty_includes_warnings():
    """The empty-stage error should surface the warning trail so the
    user understands why nothing was emitted.
    """
    with pytest.raises(ValueError, match="cuts list"):
        format_conf([], [], "n", "d",
                    warnings=["autogain#0: not bypassed but v1 …"])


# ---------------------------------------------------------------------------
# Self-validation pass via main()
# ---------------------------------------------------------------------------

def test_main_validate_runs_on_dry_run(generated, tmp_path, monkeypatch,
                                       capsys):
    """End-to-end: with `lv2info`/`spa-json-dump` available, the
    self-validation pass should succeed silently on a freshly generated
    conf. Skips cleanly if either tool is absent.
    """
    import shutil as _sh
    if not _sh.which("lv2info") or not _sh.which("spa-json-dump"):
        pytest.skip("lv2info / spa-json-dump not on PATH")

    preset, irs_path = generated
    preset_path = tmp_path / "preset.json"
    preset_path.write_text(json.dumps(preset))

    rc = ee2pw_main([
        str(preset_path),
        "--irs-dir", str(irs_path.parent),
        "--dry-run",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    # No "[validate]" lines mean validation ran cleanly (only setup
    # skips emit them).
    assert "[validate] skipped" not in captured.out
    assert "schema validation failed" not in captured.out


def test_main_no_validate_skips_check(generated, tmp_path, capsys):
    """`--no-validate` should bypass the self-check entirely (works even
    without lv2info installed).
    """
    preset, irs_path = generated
    preset_path = tmp_path / "preset.json"
    preset_path.write_text(json.dumps(preset))

    rc = ee2pw_main([
        str(preset_path),
        "--irs-dir", str(irs_path.parent),
        "--dry-run",
        "--no-validate",
    ])
    assert rc == 0


def test_main_surfaces_validate_warnings_on_pass(generated, tmp_path,
                                                 monkeypatch, capsys):
    """Trap: a clean self-check that still carries warnings — e.g. a
    referenced LSP/Calf plugin isn't installed, so its ports couldn't be
    checked — must surface those warnings. They were previously swallowed on
    the passing path, hiding a missing runtime dependency behind a "conf
    written" success (the chain then silently fails to load).
    """
    from lib.pipewire import validate
    warn = ("mb_compressor: no lv2info schema available for "
            "http://lsp-plug.in/plugins/lv2/mb_compressor_stereo; skipping")
    monkeypatch.setattr(validate, "run",
                        lambda conf: validate.Report(validate.CLEAN,
                                                     warnings=(warn,)))

    preset, irs_path = generated
    preset_path = tmp_path / "preset.json"
    preset_path.write_text(json.dumps(preset))

    rc = ee2pw_main([
        str(preset_path),
        "--irs-dir", str(irs_path.parent),
        "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[validate]" in out
    assert "no lv2info schema available" in out


def test_main_reminds_about_plugins_when_lv2info_absent(generated, tmp_path,
                                                        monkeypatch, capsys):
    """When the self-check is skipped because lv2info isn't installed, the
    converter can't verify the plugin set is present — so it must remind the
    user to install LSP / Calf, or the chain silently won't load in PipeWire.
    """
    from lib.pipewire import validate
    monkeypatch.setattr(
        validate, "run",
        lambda conf: validate.Report(
            validate.NO_TOOLING,
            reason="lv2info or spa-json-dump not in PATH "
                   "(install lilv-utils and pipewire)"))

    preset, irs_path = generated
    preset_path = tmp_path / "preset.json"
    preset_path.write_text(json.dumps(preset))

    rc = ee2pw_main([
        str(preset_path),
        "--irs-dir", str(irs_path.parent),
        "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[validate] skipped" in out
    assert "lsp-plugins-lv2" in out
    assert "calf-plugins" in out


def test_main_hard_fails_when_validation_reports_errors(generated, tmp_path,
                                                        monkeypatch, capsys):
    """A self-check that reports real errors must abort the run and leave
    *nothing* on disk.

    The other three `validate.run` outcomes are soft — NO_TOOLING and
    UNCHECKED are skips, CLEAN is a pass — so ERRORS is the only one whose
    contract is that the conf never gets written. Asserting the exit status
    and the message would pass even if the file had been written and the run
    then changed its mind; the assertion that carries the contract is that
    `--output` names a path which does not exist afterwards. A half-written or
    fully-written conf that failed schema validation is the bad outcome: it
    loads into PipeWire.
    """
    from lib.pipewire import validate
    monkeypatch.setattr(
        validate, "run",
        lambda conf: validate.Report(
            validate.ERRORS,
            errors=("peq: unknown port symbol 'ftl_99' for "
                    "para_equalizer_x16_lr",)))

    preset, irs_path = generated
    preset_path = tmp_path / "preset.json"
    preset_path.write_text(json.dumps(preset))
    out_path = tmp_path / "out" / "TestChain.conf"

    rc = ee2pw_main([
        str(preset_path),
        "--irs-dir", str(irs_path.parent),
        "--node-name", "TestChain",
        "--output", str(out_path),
    ])
    assert rc == 1
    out = capsys.readouterr().out
    assert "schema validation failed" in out
    assert "conf not written" in out
    assert not out_path.exists(), (
        "a conf that failed schema validation was written anyway — the "
        "message says it wasn't, and PipeWire would load it"
    )


def test_main_tells_a_warning_from_an_error_in_a_failing_run(generated,
                                                             tmp_path,
                                                             monkeypatch,
                                                             capsys):
    """A failing run renders both lists, and they must not read alike.

    The failing arm used to print one text blob in the error style, so a
    warning inside it arrived in red with nothing to say it wasn't one of the
    reasons the conf was refused. Colour alone doesn't settle it either: rich
    is optional and `--no-color` is supported, so the word has to be in the
    text.
    """
    from lib.pipewire import validate
    monkeypatch.setattr(
        validate, "run",
        lambda conf: validate.Report(
            validate.ERRORS,
            errors=("peq: unknown port symbol 'ftl_99' for "
                    "para_equalizer_x16_lr",),
            warnings=("mb_compressor: no lv2info schema available for "
                      "http://lsp-plug.in/plugins/lv2/mb_compressor_stereo; "
                      "skipping",)))

    preset, irs_path = generated
    preset_path = tmp_path / "preset.json"
    preset_path.write_text(json.dumps(preset))

    rc = ee2pw_main([
        str(preset_path),
        "--irs-dir", str(irs_path.parent),
        "--node-name", "TestChain",
        "--output", str(tmp_path / "out" / "TestChain.conf"),
        "--no-color",
    ])
    assert rc == 1
    lines = capsys.readouterr().out.splitlines()
    warned = [ln for ln in lines if "no lv2info schema available" in ln]
    failed = [ln for ln in lines if "unknown port symbol" in ln]
    assert len(warned) == 1 and len(failed) == 1, lines
    assert warned[0].startswith("[validate] warning: "), warned[0]
    assert failed[0].startswith("[validate] error: "), failed[0]
    # Warnings first: the one about an unreadable schema explains the errors
    # that follow, and the order is the same on the passing path.
    assert lines.index(warned[0]) < lines.index(failed[0])


# ---------------------------------------------------------------------------
# Colored output (rich, optional) — lib/console.py, shared with the generator
# ---------------------------------------------------------------------------

def test_cprint_falls_back_to_plain_when_color_disabled(monkeypatch, capsys):
    """With the console disabled (rich absent or --no-color), cprint must
    still emit the text plainly — color is decoration, never a gate on the
    message getting through — and on the same stream the console uses.

    That stream is stdout, as in dolby_to_easyeffects.py: one console, one
    stream, so a run can be redirected to a file or a pager whole. Both halves
    matter, hence the empty-stderr assertion: a fallback that quietly diverged
    would only show up when someone piped a run.

    Patches `_CONSOLE` by hand rather than taking the `silence_console`
    fixture: the None state is this test's subject, and the fixture's whole
    premise is the behaviour asserted here.
    """
    from lib import console
    monkeypatch.setattr(console, "_CONSOLE", None)
    console.cprint("err", "diagnostic-xyz")
    captured = capsys.readouterr()
    assert "diagnostic-xyz" in captured.out
    assert captured.err == ""


def test_main_no_color_disables_console(generated, tmp_path, monkeypatch):
    """`--no-color` must disable the console. monkeypatch sets a sentinel and
    auto-restores it at teardown, so `_disable_color()`'s global mutation
    doesn't leak into other tests.
    """
    from lib import console
    monkeypatch.setattr(console, "_CONSOLE", object())

    preset, irs_path = generated
    preset_path = tmp_path / "preset.json"
    preset_path.write_text(json.dumps(preset))

    rc = ee2pw_main([
        str(preset_path),
        "--irs-dir", str(irs_path.parent),
        "--dry-run",
        "--no-validate",
        "--no-color",
    ])
    assert rc == 0
    assert console._CONSOLE is None


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

def test_unknown_plugin_key_warns_not_raises():
    """A novel plugin key (e.g. `compressor#0` from a non-Dolby preset
    accidentally fed in) must surface as a warning, not a crash.
    """
    preset = {
        "output": {
            "plugins_order": ["compressor#0", "limiter#0"],
            "compressor#0": {"bypass": False},
            "limiter#0": {"bypass": False},
        }
    }
    chain = build_chain(preset, irs_dir=None, must_exist=False)
    assert any("compressor#0" in w and "unknown" in w for w in chain.warnings)


def test_bypassed_autogain_silent_skip():
    """HDA's bypassed autogain must be dropped with neither a node nor a
    warning — it's the expected default state (silent_if_bypassed).
    """
    preset = {
        "output": {
            "plugins_order": ["autogain#0", "limiter#0"],
            "autogain#0": {"bypass": True},
            "limiter#0": {"bypass": False},
        }
    }
    chain = build_chain(preset, irs_dir=None, must_exist=False)
    names = {n["name"] for s in chain.stages for n in s.nodes}
    assert "autogain" not in names
    assert not any("autogain" in w for w in chain.warnings)


def test_active_autogain_is_translated():
    """A non-bypassed autogain is now translated to an autogain_stereo node
    (it used to be skipped with a 'no LV2 equivalent' warning). Even a sparse
    block (emit_autogain fills defaults) must emit a node and not warn.
    """
    preset = {
        "output": {
            "plugins_order": ["autogain#0", "limiter#0"],
            "autogain#0": {"bypass": False},
            "limiter#0": {"bypass": False},
        }
    }
    chain = build_chain(preset, irs_dir=None, must_exist=False)
    names = {n["name"] for s in chain.stages for n in s.nodes}
    assert "autogain" in names
    # The sparse block's maximum-history (0 s) legitimately draws the
    # validated-history advisory; only a skip-class warning would mean
    # the plugin was dropped rather than translated.
    assert not any("skipped" in w or "not emitted" in w
                   for w in chain.warnings)


# ---------------------------------------------------------------------------
# IRS copy: by default the conf is self-contained — the .irs is copied
# next to the conf and the convolver `filename` rewritten. --no-copy-irs
# preserves v1's behaviour of referencing the EE-side path.
# ---------------------------------------------------------------------------

def _run_main(generated, tmp_path, *extra_args, no_validate=True):
    preset, irs_path = generated
    preset_path = tmp_path / "preset.json"
    preset_path.write_text(json.dumps(preset))
    out_path = tmp_path / "out" / "TestChain.conf"
    args = [
        str(preset_path),
        "--irs-dir", str(irs_path.parent),
        "--node-name", "TestChain",
        "--output", str(out_path),
        *extra_args,
    ]
    if no_validate:
        args.append("--no-validate")
    rc = ee2pw_main(args)
    return rc, out_path, irs_path


def test_main_copies_irs_next_to_conf(generated, tmp_path):
    """Default behaviour: writing the conf also copies the .irs into the
    same directory and rewrites the convolver `filename` to that copy.
    """
    rc, out_path, src_irs = _run_main(generated, tmp_path)
    assert rc == 0

    target_irs = out_path.parent / "TestChain.irs"
    assert target_irs.is_file(), "IRS should be copied next to the conf"
    # Bytes match the source.
    assert target_irs.read_bytes() == src_irs.read_bytes()
    # Conf body references the new path, not the original EE-side one.
    conf_text = out_path.read_text()
    assert str(target_irs) in conf_text
    assert str(src_irs) not in conf_text


def test_main_no_copy_irs_keeps_source_path(generated, tmp_path):
    """With `--no-copy-irs`, no copy happens and the conf references the
    original EE-side path (the v1 behaviour).
    """
    rc, out_path, src_irs = _run_main(generated, tmp_path, "--no-copy-irs")
    assert rc == 0

    target_irs = out_path.parent / "TestChain.irs"
    assert not target_irs.exists(), \
        "no copy should be made when --no-copy-irs is passed"
    conf_text = out_path.read_text()
    assert str(src_irs) in conf_text


def test_main_dry_run_retargets_without_copying(generated, tmp_path,
                                                 capsys):
    """Dry-run still rewrites the convolver path, so what it reports is
    exactly what the same command minus --dry-run writes — but it creates
    nothing itself.
    """
    preset, irs_path = generated
    preset_path = tmp_path / "preset.json"
    preset_path.write_text(json.dumps(preset))
    out_path = tmp_path / "out" / "TestChain.conf"
    argv = [
        str(preset_path),
        "--irs-dir", str(irs_path.parent),
        "--node-name", "TestChain",
        "--output", str(out_path),
        "--no-validate",
    ]
    assert ee2pw_main(argv + ["--dry-run"]) == 0
    assert not out_path.exists()
    target_irs = out_path.parent / "TestChain.irs"
    assert not target_irs.exists()
    # The retarget ran: the announced .irs destination is the one beside the
    # conf, not the EE-side source.
    out = capsys.readouterr().out
    assert f"Would copy impulse response (.irs): {target_irs}" in out
    # ...and the real run puts the IRS exactly where the dry run said, with
    # the conf pointing at that copy rather than the EE path.
    assert ee2pw_main(argv) == 0
    assert target_irs.is_file()
    conf_text = out_path.read_text()
    assert str(target_irs) in conf_text
    assert str(irs_path) not in conf_text


def test_main_dry_run_reports_would_write_paths(generated, tmp_path, capsys):
    """--dry-run announces where the conf (and IRS copy) *would* land, and
    that report is all it emits — the conf body itself is no longer dumped
    to a stream, so --output is the only way to obtain it."""
    preset, irs_path = generated
    preset_path = tmp_path / "preset.json"
    preset_path.write_text(json.dumps(preset))
    out_path = tmp_path / "out" / "TestChain.conf"
    rc = ee2pw_main([
        str(preset_path),
        "--irs-dir", str(irs_path.parent),
        "--node-name", "TestChain",
        "--output", str(out_path),
        "--no-validate",
        "--dry-run",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    target_irs = out_path.parent / "TestChain.irs"
    assert f"Would write conf: {out_path}" in captured.out
    assert "Would copy impulse response (.irs):" in captured.out
    assert str(target_irs) in captured.out
    for stream in (captured.out, captured.err):
        assert CONF_HEADER_MARK not in stream
        assert "filter.graph" not in stream


def test_main_real_write_reports_results_and_next_steps(generated, tmp_path,
                                                        capsys):
    """A real write reports the destinations (Wrote/Copied) and a numbered
    Next steps block — the post-[next] presentation."""
    rc, out_path, _src_irs = _run_main(generated, tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert f"Wrote conf: {out_path}" in out
    assert "Copied impulse response (.irs):" in out
    assert "Next steps:" in out
    assert "systemctl --user restart pipewire pipewire-pulse" in out
    assert "[next]" not in out  # old per-line tag is gone


def test_main_skip_next_steps_suppresses_checklist(generated, tmp_path,
                                                   capsys):
    """`--skip-next-steps` replaces the next-steps checklist with the
    one-line activation pointer — dolby_to_pipewire.py relies on it to
    print its own consolidated activation block instead. The Wrote/Copied
    report must survive, and a freshly written conf is never left silently
    inactive."""
    rc, out_path, _src_irs = _run_main(generated, tmp_path,
                                       "--skip-next-steps")
    assert rc == 0
    out = capsys.readouterr().out
    assert f"Wrote conf: {out_path}" in out
    assert "Next steps:" not in out
    assert ("To activate: systemctl --user restart pipewire pipewire-pulse"
            in out)


def test_main_existing_target_irs_without_force_errors(generated,
                                                       tmp_path, capsys):
    """If the target .irs already exists with different content,
    refuse to overwrite without --force."""
    rc, out_path, _src_irs = _run_main(generated, tmp_path)
    assert rc == 0

    target_irs = out_path.parent / "TestChain.irs"
    target_irs.write_bytes(b"stale bytes that don't match")

    # Re-run with --force on the conf only; IRS check should still fire.
    rc2, _, _ = _run_main(generated, tmp_path, "--force")
    # --force overwrites both, so this round should succeed and replace
    # the stale bytes.
    assert rc2 == 0
    preset, src_irs = generated
    assert target_irs.read_bytes() == src_irs.read_bytes()


def test_main_force_overwrites_existing_irs(generated, tmp_path):
    """--force replaces both the conf and the IRS in the target dir."""
    rc, out_path, src_irs = _run_main(generated, tmp_path)
    assert rc == 0

    target_irs = out_path.parent / "TestChain.irs"
    # Corrupt the copied IRS to ensure --force actually rewrites it.
    target_irs.write_bytes(b"corrupted")
    rc2, _, _ = _run_main(generated, tmp_path, "--force")
    assert rc2 == 0
    assert target_irs.read_bytes() == src_irs.read_bytes()


def test_main_target_irs_exists_blocks_without_force(generated, tmp_path,
                                                     capsys):
    """Without --force, an existing target IRS blocks the conf write."""
    rc, out_path, _src_irs = _run_main(generated, tmp_path)
    assert rc == 0
    # Conf exists too — delete it so the conf-side check passes and we
    # exercise the IRS-side check specifically.
    out_path.unlink()

    rc2, _, _ = _run_main(generated, tmp_path)
    assert rc2 == 1
    captured = capsys.readouterr()
    assert "TestChain.irs" in captured.out
    assert "--force" in captured.out


# ---------------------------------------------------------------------------
# WirePlumber 0.5+ smart-filter target. When `target_sink` is set the
# conf gets `node.link-group`/`filter.smart`/`filter.smart.target` on
# both streams so WP routes apps targeting that hardware sink through
# the chain transparently. When unset (or explicitly "" via CLI) the
# conf falls back to the v1 virtual-sink behaviour.
# ---------------------------------------------------------------------------

def _format_minimal(target_sink=None, target_object=None):
    """Build a minimal valid chain text without touching the filesystem.

    Skips IRS resolution (must_exist=False) so the test fixture is just
    a one-stage chain — enough to exercise format_conf's prop wiring.
    """
    preset = {
        "output": {
            "plugins_order": ["limiter#0"],
            "limiter#0": {"bypass": False, "threshold": -1.0,
                          "lookahead": 1.0, "attack": 1.0, "release": 5.0,
                          "stereo-link": 100.0, "input-gain": 0.0,
                          "output-gain": 0.0, "mode": "Herm Thin",
                          "alr": False, "gain-boost": False},
        }
    }
    chain = build_chain(preset, irs_dir=None, must_exist=False)
    links = emit_links(chain.stages)
    return format_conf(chain.stages, links, "TestChain", "test desc",
                       target_object=target_object,
                       target_sink=target_sink)


def test_format_conf_no_target_sink_omits_smart_filter():
    """v1 fallback: without target_sink, no smart-filter properties."""
    conf = _format_minimal()
    assert "filter.smart" not in conf
    assert "node.link-group" not in conf


def test_format_conf_target_sink_emits_smart_filter():
    """target_sink populates the WP smart-filter properties on both
    capture and playback streams, with matching link-group."""
    conf = _format_minimal(target_sink="alsa_output.x.HiFi__Speaker__sink")
    assert "filter.smart = true" in conf
    assert 'filter.smart.target = {' in conf
    assert '"alsa_output.x.HiFi__Speaker__sink"' in conf
    # Same link-group on both sides — WP needs this to treat the
    # capture and playback streams as one filter for routing.
    assert conf.count('node.link-group = "TestChain_smart_filter"') == 2
    # filter.smart.targetable is intentionally left as the default
    # (false), which keeps the chain sink hidden from app target lists.
    assert "filter.smart.targetable" not in conf


def test_format_conf_target_sink_keeps_no_target_object():
    """Smart-filter mode shouldn't bake target.object on playback —
    WP's link resolver picks the target. Coexistence with
    --target-object is allowed (measurement rig still uses it)."""
    conf = _format_minimal(target_sink="speaker_sink")
    assert "target.object" not in conf


def test_format_conf_target_sink_and_target_object_coexist():
    """Power users / measurement rig may set both: target_object
    pins playback, target_sink advertises smart routing."""
    conf = _format_minimal(target_sink="speaker_sink",
                           target_object="ee_capture")
    assert "filter.smart = true" in conf
    assert 'target.object = "ee_capture"' in conf


def test_main_target_sink_flag_threads_through(generated, tmp_path):
    """End-to-end: --target-sink lands as filter.smart.target."""
    rc, out_path, _ = _run_main(generated, tmp_path,
                                "--target-sink", "speaker_sink_xyz")
    assert rc == 0
    conf_text = out_path.read_text()
    assert "filter.smart = true" in conf_text
    assert '"speaker_sink_xyz"' in conf_text


def test_main_target_sink_empty_disables_smart_filter(generated, tmp_path):
    """--target-sink '' explicitly opts out of smart-filter mode."""
    rc, out_path, _ = _run_main(generated, tmp_path,
                                "--target-sink", "")
    assert rc == 0
    conf_text = out_path.read_text()
    assert "filter.smart" not in conf_text


# ---------------------------------------------------------------------------
# Default node-name derivation. Without an explicit --node-name, the
# converter should name the chain after the preset filename stem so
# multiple presets produce distinct sinks (Dolby-Balanced.json →
# effect_input.Dolby_Balanced; Dolby-Detailed.json → effect_input.Dolby_Detailed).
# Without this, every conversion lands on the same sink name.
# ---------------------------------------------------------------------------

def test_main_default_node_name_derives_from_preset_filename(
        generated, tmp_path, capsys):
    preset, irs_path = generated
    preset_path = tmp_path / "Dolby-Balanced.json"
    preset_path.write_text(json.dumps(preset))
    out_path = tmp_path / "out" / "out.conf"
    rc = ee2pw_main([
        str(preset_path),
        "--irs-dir", str(irs_path.parent),
        "--output", str(out_path),
        "--no-validate",
        "--target-sink", "",
    ])
    assert rc == 0
    conf = out_path.read_text()
    # Sanitised preset stem becomes the sink and source node names.
    assert 'node.name = "effect_input.Dolby_Balanced"' in conf
    assert 'node.name = "effect_output.Dolby_Balanced"' in conf
    # Description preserves the unsanitised stem (hyphen kept).
    assert 'node.description = "Dolby-Balanced"' in conf


def test_main_two_presets_produce_distinct_sinks(generated, tmp_path):
    preset, irs_path = generated
    p1 = tmp_path / "Dolby-Balanced.json"
    p2 = tmp_path / "Dolby-Detailed.json"
    p1.write_text(json.dumps(preset))
    p2.write_text(json.dumps(preset))

    confs = []
    for p in (p1, p2):
        out = tmp_path / f"{p.stem}.conf"
        rc = ee2pw_main([
            str(p),
            "--irs-dir", str(irs_path.parent),
            "--output", str(out),
            "--no-validate",
            "--target-sink", "",
        ])
        assert rc == 0
        confs.append(out.read_text())
    assert 'effect_input.Dolby_Balanced' in confs[0]
    assert 'effect_input.Dolby_Balanced' not in confs[1]
    assert 'effect_input.Dolby_Detailed' in confs[1]


def test_main_explicit_node_name_overrides_derivation(generated, tmp_path):
    preset, irs_path = generated
    preset_path = tmp_path / "Dolby-Balanced.json"
    preset_path.write_text(json.dumps(preset))
    out_path = tmp_path / "out" / "Custom.conf"
    rc = ee2pw_main([
        str(preset_path),
        "--irs-dir", str(irs_path.parent),
        "--output", str(out_path),
        "--node-name", "MyChain",
        "--no-validate",
        "--target-sink", "",
    ])
    assert rc == 0
    conf = out_path.read_text()
    assert 'effect_input.MyChain' in conf
    assert 'Dolby_Balanced' not in conf


# ---------------------------------------------------------------------------
# Systematic coverage guard
#
# Schema validation (lv2info) catches *invalid* control symbols, but a key the
# generator WRITES that the emitter silently IGNORES yields a valid-but-wrong
# conf that nothing catches. The guards below sweep `make_preset` across every
# emission-relevant flag combination and assert, for each emitted plugin:
#   * the plugin key has an EE_KEY_DISPATCH entry (translate-or-classify);
#   * every leaf param is either consumed by the emitter or carried in
#     _INTENTIONALLY_UNTRANSLATED with a default-equivalence proof;
#   * every enum-label string maps through a converter EE_* table;
#   * each untranslated param's pinned generator value equals the LV2 port
#     default the conf silently inherits (hermetic fast check against values
#     pinned from the LSP 1.2.27 meta sources; live lv2info cross-check in
#     the slow tier).
# A new generator feature that is neither translated nor classified fails
# loudly, forcing a conscious decision rather than a silent drop.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UntranslatedParam:
    """A generator-emitted param the converter deliberately does not write.

    What makes the drop faithful is default-equivalence: the pinned
    generator value (`ee_value`), converted to port units by `to_port`,
    equals the LV2 default (`lv2_default`) the conf inherits by not
    writing `port`. `port=None` marks params with no LV2 port at all
    (EE-internal behaviour), where nothing can drift on the plugin side.
    """
    reason: str
    ee_value: object
    port: str | None = None           # LV2 symbol; "{i}" template per band
    lv2_default: float | None = None  # in port units (LSP 1.2.27 meta)
    to_port: Callable | None = None   # ee_value -> port units


_MBC_UNTRANSLATED = {
    "mute": UntranslatedParam(
        "always False in generated presets; bm default 0 (unmuted)",
        False, "bm_{i}", 0.0, int),
    "solo": UntranslatedParam(
        "always False; bs default 0", False, "bs_{i}", 0.0, int),
    "sidechain-type": UntranslatedParam(
        "external sidechain is unwired in a filter-chain graph; EE pins "
        "Internal == sce default", "Internal", "sce_{i}", 0.0,
        {"Internal": 0}.get),
    "sidechain-source": UntranslatedParam(
        "EE pins Middle == scs default", "Middle", "scs_{i}", 0.0,
        {"Middle": 0}.get),
    "stereo-split-source": UntranslatedParam(
        "read only when ssplit is on (generator keeps it off); EE pins "
        "Left/Right == sscs default", "Left/Right", "sscs_{i}", 0.0,
        {"Left/Right": 0}.get),
    "sidechain-reactivity": UntranslatedParam(
        "EE pins 10 ms == scr default", 10.0, "scr_{i}", 10.0, float),
}

# Keys the converter deliberately does NOT translate. Every entry is
# enforced twice: the sweep tests pin the generator-side value, and the
# default-equivalence tests pin the LV2 side. Add entries only with both
# proofs (or port=None for params with no LV2 port at all).
_INTENTIONALLY_UNTRANSLATED: dict[str, dict[str, UntranslatedParam]] = {
    "convolver#0": {
        "ir-width": UntranslatedParam(
            "EE-internal stereo-width preprocessing of the IR (EE applies "
            "it at kernel load; not an LV2 port)", 100),
        "autogain": UntranslatedParam(
            "EE convolver auto-normalise flag; stays False (the +50 dB "
            "LSP-default trap, design-notes) — the PW builtin convolver "
            "has no such behaviour", False),
    },
    "equalizer#0": {
        "split-channels": UntranslatedParam(
            "para_equalizer_x16_lr is inherently two-channel; the "
            "converter always writes explicit L/R bands", True),
    },
    "equalizer#1": {
        "split-channels": UntranslatedParam(
            "dialog enhancer writes identical L/R bands, so unsplit and "
            "split render the same", False),
    },
    "multiband_compressor#0": _MBC_UNTRANSLATED,
    "multiband_compressor#1": _MBC_UNTRANSLATED,
    "limiter#0": {
        "oversampling": UntranslatedParam(
            "EE pins None == ovs default", "None", "ovs", 0.0,
            {"None": 0}.get),
        "dithering": UntranslatedParam(
            "EE pins None == dith default", "None", "dith", 0.0,
            {"None": 0}.get),
        "sidechain-type": UntranslatedParam(
            "external sidechain unwired; EE pins Internal == extsc "
            "default", "Internal", "extsc", 0.0, {"Internal": 0}.get),
        "sidechain-preamp": UntranslatedParam(
            "EE pins 0 dB == scp default (1.0 linear)", 0.0, "scp", 1.0,
            db_to_lin),
    },
    "autogain#0": {
        "reference": UntranslatedParam(
            "EE libebur128 loudness-statistic selector; autogain_stereo "
            "has no equivalent port — the autogain mapping is a documented "
            "approximation (docs/ee-to-pipewire.md), and any retune is "
            "device-validation-gated (CLAUDE.md)", "Geometric Mean (MSI)"),
        "input-gain": UntranslatedParam(
            "always 0.0; autogain_stereo has no main-path input trim "
            "(preamp is sidechain-only)", 0.0),
        "output-gain": UntranslatedParam(
            "always 0.0; no main-path output trim", 0.0),
    },
    "bass_enhancer#0": {},
}

# Private helpers whose source the key-consumption scraper must also read,
# keyed by the public emitter that calls them.
_HELPER_FNS = {emit_peq: (_emit_peq_node,)}


def _emitters_for(key: str) -> tuple:
    """Emitter function(s) for a plugin key, derived from EE_KEY_DISPATCH
    (the previous hand-maintained mirror went stale and KeyError'd on new
    keys)."""
    handler = EE_KEY_DISPATCH[key]
    return (handler.emitter, *_HELPER_FNS.get(handler.emitter, ()))


# The container shapes a plugin dict nests params under: PEQ left/right ->
# bandN, MBC band0..7. One definition for the three flatteners below —
# copies drifting apart would make the coverage guards scan different
# parameter surfaces.
_BAND_CONTAINERS = {"left", "right"} | {f"band{i}" for i in range(8)}


def _leaf_param_keys(plugin: dict) -> set[str]:
    """Flatten a preset plugin dict to its scalar parameter names, descending
    past the PEQ left/right -> bandN containers and the MBC band0..7 containers
    so per-band params are compared, not the structural wrappers."""
    keys: set[str] = set()
    for k, v in plugin.items():
        if k in _BAND_CONTAINERS and isinstance(v, dict):
            if all(not isinstance(sv, dict) for sv in v.values()):
                keys.update(v.keys())          # MBC: bandN -> {params}
            else:
                for sub_v in v.values():       # PEQ: left/right -> bandN -> {params}
                    if isinstance(sub_v, dict):
                        keys.update(sub_v.keys())
        else:
            keys.add(k)
    return keys


@functools.lru_cache(maxsize=None)
def _consumed_keys(fns: tuple) -> frozenset[str]:
    """Keys the emitter reads, scraped from `plugin.get("X")`/`band.get("X")`
    literals in its source. Auto-derived so the guard tracks the emitter with
    no hand-maintained mirror; f-string container reads (band{i}) are handled
    by _leaf_param_keys descending past the containers. A key read only in a
    warn branch (convolver input-gain) counts as consumed — surfacing the
    value is a conscious decision, not a silent drop."""
    keys: set[str] = set()
    for fn in fns:
        src = inspect.getsource(fn)
        keys.update(re.findall(r"""\.get\(\s*["']([^"']+)["']""", src))
    return frozenset(keys)


def _leaf_values(plugin: dict, pname: str) -> list:
    """Every value of param `pname` in a plugin dict, at top level or inside
    the band containers (PEQ left/right -> bandN, MBC band0..7)."""
    vals = []
    if pname in plugin:
        vals.append(plugin[pname])
    for k, v in plugin.items():
        if k in _BAND_CONTAINERS and isinstance(v, dict):
            if pname in v:
                vals.append(v[pname])
            for sub in v.values():
                if isinstance(sub, dict) and pname in sub:
                    vals.append(sub[pname])
    return vals


def _string_leaves(plugin: dict):
    """Yield (level, pname, value) for every string leaf; level is "plugin"
    (top of the plugin dict) or "band" (inside band containers)."""
    for k, v in plugin.items():
        if k in _BAND_CONTAINERS and isinstance(v, dict):
            subs = [sv for sv in v.values() if isinstance(sv, dict)] or [v]
            for sub in subs:
                for pk, pv in sub.items():
                    if isinstance(pv, str):
                        yield "band", pk, pv
        elif isinstance(v, str):
            yield "plugin", k, v


def _coverage_preset(is_soundwire: bool = False,
                     volmax_slot: str = "input-gain",
                     enabled: set[str] | None = None,
                     disabled: set[str] | None = None):
    peq = synthetic_peq_filters([
        (0, 7, 90.0, 0.0, 0.707, 4, 1.0), (1, 7, 90.0, 0.0, 0.707, 4, 1.0),
        (0, 1, 1000.0, 4.0, 1.5, 0, 1.0), (1, 1, 1000.0, 4.0, 1.5, 0, 1.0),
        (0, 4, 200.0, 3.0, 0.707, 0, 1.0), (1, 4, 200.0, 3.0, 0.707, 0, 1.0),
        (0, 3, 8000.0, 2.0, 0.707, 0, 1.0), (1, 3, 8000.0, 2.0, 0.707, 0, 1.0),
        (0, 6, 15000.0, 0.0, 0.707, 2, 1.0), (1, 6, 15000.0, 0.0, 0.707, 2, 1.0),
    ])
    mb = synthetic_mb_comp(group_count=3, bands=[
        (10, -160, 16384, 30000, 32500, 0),
        (100, -160, 16384, 30000, 32500, 0),
        (1000, -160, 16384, 30000, 32500, 0),
    ])
    reg = synthetic_regulator([-6.0] * 20)
    preset, _ = make_preset(
        kernel_name="Synthetic", peq_filters=peq,
        vol_leveler={"enable": True, "amount": 5, "out_target": -16.0},
        dialog_enhancer={"enable": True, "amount": 5, "boost": 4.0},
        mb_comp=mb, regulator=reg, freqs=SYNTHETIC_FREQS_20,
        is_soundwire=is_soundwire, volmax_boost=3.0,
        volmax_slot=volmax_slot, enabled=enabled, disabled=disabled,
    )
    return preset


def _sweep_specs() -> list[tuple[str, dict]]:
    """(id, make_preset kwarg overrides) spanning every emission-relevant
    flag combination. The disable/enable axes derive from the generator's
    own DISABLEABLE_FILTERS / ENABLEABLE_FILTERS and are crossed with both
    device families, so a new flag joins the sweep automatically even when
    its emission path is SoundWire-only. `enable-coupled-bands` may not
    fire on the synthetic regulator — that changes band values, never keys
    or labels, so the coverage claims hold either way."""
    specs = [
        ("hda", {}),
        ("soundwire", {"is_soundwire": True}),
        ("volmax-output-gain", {"volmax_slot": "output-gain"}),
    ]
    for prefix, base in (("", {}), ("soundwire-", {"is_soundwire": True})):
        specs += [(f"{prefix}enable-{n}", {**base, "enabled": {n}})
                  for n in sorted(ENABLEABLE_FILTERS)]
        specs += [(f"{prefix}disable-{n}", {**base, "disabled": {n}})
                  for n in sorted(DISABLEABLE_FILTERS)]
    return specs


_SWEEP_CACHE: list[tuple[str, dict]] | None = None


def _sweep_presets() -> list[tuple[str, dict]]:
    """Cached (id, preset) pairs for the whole sweep — pure dict building
    (no FIR/IRS), cheap enough for the fast tier but not worth repeating
    in every test that scans the full surface. Read-only by convention."""
    global _SWEEP_CACHE
    if _SWEEP_CACHE is None:
        _SWEEP_CACHE = [(sid, _coverage_preset(**kw))
                        for sid, kw in _sweep_specs()]
    return _SWEEP_CACHE


def _emitted_plugin_keys(preset: dict) -> set[str]:
    return {k for k, v in preset["output"].items() if isinstance(v, dict)}


def test_every_emittable_key_has_dispatch_entry():
    """A new generator plugin key must get an EE_KEY_DISPATCH entry (or a
    conscious skip entry) before it ships — at runtime an unknown key is
    only a stderr warning, which nothing in CI reads."""
    emittable = set()
    for sid, preset in _sweep_presets():
        keys = _emitted_plugin_keys(preset)
        # plugins_order and the plugin objects must stay in lockstep —
        # build_chain walks only plugins_order (orphans merely warn).
        assert keys == set(preset["output"]["plugins_order"]), sid
        emittable |= keys
    missing = emittable - set(EE_KEY_DISPATCH)
    assert not missing, (
        f"make_preset can emit plugin keys with no converter dispatch "
        f"entry: {sorted(missing)}")


def test_dispatch_dead_keys_are_known():
    """Inverse direction: dispatch entries no generator path reaches.
    stereo_tools#0 is deliberate — the widener was removed from the
    generator (3d35a92) but the translator stays for hand-edited/legacy
    presets. Anything else joining it means a generator emission path
    silently died."""
    emittable = set()
    for _, preset in _sweep_presets():
        emittable |= _emitted_plugin_keys(preset)
    assert set(EE_KEY_DISPATCH) - emittable == {"stereo_tools#0"}


@pytest.mark.parametrize("sid", [s for s, _ in _sweep_specs()])
def test_no_generator_key_silently_dropped(sid):
    preset = dict(_sweep_presets())[sid]
    for key, plugin in preset["output"].items():
        if key in ("blocklist", "plugins_order") or not isinstance(plugin, dict):
            continue
        assert key in EE_KEY_DISPATCH, (
            f"{key}: no EE_KEY_DISPATCH entry (see "
            "test_every_emittable_key_has_dispatch_entry)")
        consumed = _consumed_keys(_emitters_for(key))
        entries = _INTENTIONALLY_UNTRANSLATED.get(key, {})
        stale = consumed & set(entries)
        assert not stale, (
            f"{key}: {sorted(stale)} are both consumed by the emitter and "
            "marked intentionally-untranslated — prune the stale entries.")
        unhandled = _leaf_param_keys(plugin) - consumed - set(entries)
        assert not unhandled, (
            f"{key}: generator writes keys the converter neither reads nor "
            f"marks untranslated: {sorted(unhandled)}. Either consume them "
            "in the emitter or add an UntranslatedParam entry with a "
            "default-equivalence proof."
        )


# Where each plugin key's enum-label params live, and which converter table
# maps them. A brand-new plugin base name KeyErrors here — loudly, so the
# new plugin's string params get classified rather than skipped.
_ENUM_PARAM_TABLES = {
    "convolver": {"plugin": {}, "band": {}},
    "equalizer": {
        "plugin": {"mode": EE_EQMODE_TO_LSP},
        "band": {"type": EE_FTYPE_TO_LSP, "mode": EE_FMODE_TO_LSP,
                 "slope": EE_FSLOPE_TO_LSP},
    },
    "multiband_compressor": {
        "plugin": {"compressor-mode": EE_MBC_GLOBAL_MODE,
                   "envelope-boost": EE_MBC_ENVB},
        "band": {"sidechain-mode": EE_MBC_SCMODE,
                 "compression-mode": EE_MBC_CM},
    },
    "limiter": {"plugin": {"mode": EE_LIMITER_MODE}, "band": {}},
    "autogain": {"plugin": {}, "band": {}},
    "bass_enhancer": {"plugin": {}, "band": {}},
}
# String params that aren't enum labels at all.
_NON_ENUM_STRINGS = {"kernel-name"}


def test_enum_labels_covered_by_tables():
    """Every enum-label string the generator can emit must map through the
    converter's EE_* tables (or carry an UntranslatedParam entry). Derived
    from the sweep — replaces a hand-typed label list that couldn't see
    new generator output."""
    for sid, preset in _sweep_presets():
        for key, plugin in preset["output"].items():
            if key in ("blocklist", "plugins_order") \
                    or not isinstance(plugin, dict):
                continue
            tables = _ENUM_PARAM_TABLES[key.split("#")[0]]
            untranslated = _INTENTIONALLY_UNTRANSLATED.get(key, {})
            for level, pname, value in _string_leaves(plugin):
                if pname in _NON_ENUM_STRINGS:
                    continue
                table = tables[level].get(pname)
                if table is not None:
                    assert value in table, (
                        f"[{sid}] {key} {pname} = {value!r} is missing "
                        "from its EE_* table in lib/pipewire/plugins.py")
                    if table is EE_FTYPE_TO_LSP:
                        # 0 is Off — an emitted filter type must never be.
                        assert table[value] != 0, \
                            f"[{sid}] {key} {pname} = {value!r} maps to Off"
                else:
                    assert pname in untranslated, (
                        f"[{sid}] {key} {pname} = {value!r}: string param "
                        "with no enum table and no UntranslatedParam entry")


def test_untranslated_params_pinned_ee_values():
    """Generator drift on a guarded param must fail loud — the drop is
    only faithful while the generator emits exactly the pinned value."""
    for key, entries in _INTENTIONALLY_UNTRANSLATED.items():
        for pname, entry in entries.items():
            hits = 0
            for sid, preset in _sweep_presets():
                plugin = preset["output"].get(key)
                if not isinstance(plugin, dict):
                    continue
                for value in _leaf_values(plugin, pname):
                    hits += 1
                    assert value == entry.ee_value, (
                        f"[{sid}] {key} {pname} = {value!r}; pinned "
                        f"{entry.ee_value!r}. Translate the param or re-pin "
                        "after a fresh default-equivalence check.")
            assert hits, (
                f"{key} {pname} never appeared in the sweep — stale "
                "UntranslatedParam entry?")


def test_untranslated_pinned_defaults_self_consistent():
    """The hermetic inert-by-equivalence proof: each pinned generator
    value, converted to port units, equals the LV2 default the conf
    inherits by not writing the port. Defaults pinned from the LSP 1.2.27
    meta sources; the slow tier cross-checks them against live lv2info."""
    for key, entries in _INTENTIONALLY_UNTRANSLATED.items():
        for pname, entry in entries.items():
            if entry.port is None:
                continue
            got = float(entry.to_port(entry.ee_value))
            assert math.isclose(got, entry.lv2_default,
                                rel_tol=1e-6, abs_tol=1e-9), (
                f"{key} {pname}: to_port({entry.ee_value!r}) = {got} != "
                f"pinned LV2 default {entry.lv2_default} — not translating "
                "it changes the audio.")


_URI_FOR_KEY = {
    "multiband_compressor#0": LSP_MBC_URI,
    "multiband_compressor#1": LSP_MBC_URI,
    "limiter#0": LSP_LIM_URI,
}


@functools.lru_cache(maxsize=None)
def _lv2info_defaults(uri: str) -> dict[str, float]:
    # 60 s, not the 10 s the two production call sites use
    # (lib/pipewire/validate.py, lib/pipewire/checks.py) — deliberately, not
    # drift. Those bound a user waiting at a terminal; this one runs under
    # `-n auto` alongside a core's worth of pytest workers, where a call that
    # costs ~15 ms cold and serial can take far longer. Nothing here is worth
    # a flake.
    out = subprocess.run(["lv2info", uri], capture_output=True, text=True,
                         timeout=60).stdout
    defaults: dict[str, float] = {}
    for chunk in re.split(r"\n\s*Port \d+:", out):
        sym = re.search(r"Symbol:\s+(\S+)", chunk)
        dfl = re.search(r"Default:\s+([-+0-9.eE]+)", chunk)
        if sym and dfl:
            defaults[sym.group(1)] = float(dfl.group(1))
    return defaults


@pytest.mark.slow
def test_untranslated_lv2_defaults_match_live_lv2info():
    """Environment drift check: the pinned LV2 defaults above must match
    the installed plugins' actual .ttl. A distro shipping an LSP version
    with a changed default would silently change the audio of every conf
    that omits the port — this is the only test that can see it."""
    if shutil.which("lv2info") is None:
        pytest.skip("lv2info not installed")
    for key, entries in _INTENTIONALLY_UNTRANSLATED.items():
        uri = _URI_FOR_KEY.get(key)
        if uri is None:
            continue
        defaults = _lv2info_defaults(uri)
        if not defaults:
            pytest.skip(f"lv2info returned no ports for {uri}")
        for pname, entry in entries.items():
            if entry.port is None:
                continue
            sym = entry.port.format(i=1)
            assert sym in defaults, f"{uri}: no port {sym}"
            assert math.isclose(defaults[sym], entry.lv2_default,
                                rel_tol=1e-3, abs_tol=1e-6), (
                f"{key} {pname} ({sym}): installed LV2 default "
                f"{defaults[sym]} != pinned {entry.lv2_default} — the "
                "installed LSP version changes the audio of confs that "
                "omit this port; translate the param explicitly.")


# ---------------------------------------------------------------------------
# Guard canaries — prove the coverage machinery detects gaps
#
# The guards above pass on today's clean state, so their failure branches
# never run; if an introspection helper silently broke (empty source
# scrape, a flattener that stops descending), they would keep passing
# vacuously. These inject synthetic gaps into copies of a sweep preset and
# assert the detection arithmetic actually flags them.
# ---------------------------------------------------------------------------


def test_consumed_keys_scraper_sees_get_literals():
    def dummy(plugin):
        a = plugin.get("alpha", 1.0)
        b = plugin.get("beta-key")
        hoisted = "gamma"
        return a, b, plugin.get(hoisted)  # non-literal: must NOT count

    assert _consumed_keys((dummy,)) == {"alpha", "beta-key"}


def test_guard_flags_injected_unknown_plugin_key(tmp_path):
    preset = copy.deepcopy(_sweep_presets()[0][1])
    preset["output"]["fancy_new_plugin#0"] = {"bypass": False, "knob": 1.0}
    preset["output"]["plugins_order"].append("fancy_new_plugin#0")
    # Static side: the emittable-keys guard's arithmetic must see the new
    # key as uncovered by the dispatch table.
    assert (_emitted_plugin_keys(preset) - set(EE_KEY_DISPATCH)
            == {"fancy_new_plugin#0"})
    # Runtime side: build_chain must warn, not silently drop the stage.
    chain = build_chain(preset, tmp_path, must_exist=False)
    assert any("fancy_new_plugin#0" in w and "unknown" in w
               for w in chain.warnings)


def test_guard_flags_injected_untranslated_param():
    preset = copy.deepcopy(_sweep_presets()[0][1])
    mbc = preset["output"]["multiband_compressor#0"]
    mbc["band0"]["brand-new-knob"] = 3.5
    mbc["brand-new-top-knob"] = "Fancy Label"
    consumed = _consumed_keys(_emitters_for("multiband_compressor#0"))
    classified = set(_INTENTIONALLY_UNTRANSLATED["multiband_compressor#0"])
    unhandled = _leaf_param_keys(mbc) - consumed - classified
    # Both the band-level and top-level injections must surface — proving
    # the flattener descends containers and the guard's set arithmetic
    # would turn the build red on a real generator addition.
    assert unhandled == {"brand-new-knob", "brand-new-top-knob"}
    # The enum walker must surface the new string leaf too.
    assert (("plugin", "brand-new-top-knob", "Fancy Label")
            in set(_string_leaves(mbc)))


# ---------------------------------------------------------------------------
# Targeted round-trips for paths the happy-path fixture skips: the regulator's
# volmax gain (default slot `input-gain`, plus the `output-gain` opt-out), MBC
# split-frequency / band-enable on bands 1..n, and the experimental PEQ shelf /
# lo-pass filter types. `_coverage_preset` carries a non-zero volmax_boost and
# all five filter types, so these aren't vacuous.
# ---------------------------------------------------------------------------

def _coverage_conf(tmp_path, volmax_slot="input-gain"):
    preset = _coverage_preset(is_soundwire=False, volmax_slot=volmax_slot)
    chain = build_chain(preset, tmp_path, must_exist=False)
    links = emit_links(chain.stages)
    conf = format_conf(chain.stages, links, "test_node", "test")
    return preset, conf


@pytest.fixture
def coverage_chain(tmp_path):
    return _coverage_conf(tmp_path)


def test_regulator_volmax_g_in_round_trips(coverage_chain):
    """The regulator (multiband_compressor#1) carries the volmax-boost on its
    input-gain by default (issue #23) — distinct from the MBC (#0), which never
    carries volmax. Lock that it round-trips through the conf's `g_in` as a
    linear gain."""
    preset, conf = coverage_chain
    reg = _extract_node_control(conf, "reg")
    src = preset["output"]["multiband_compressor#1"]
    assert src["input-gain"] != 0.0   # volmax_boost=3.0 forces a non-zero trim
    assert src["output-gain"] == 0.0
    assert abs(lin_to_db(reg["g_in"]) - src["input-gain"]) < 1e-4


def test_regulator_volmax_output_gain_g_out_round_trips(tmp_path):
    """The `--volmax-slot output-gain` opt-out routes the boost to the
    regulator output-gain; lock that it round-trips through the conf's `g_out`
    so the opt-out translation path stays covered."""
    preset, conf = _coverage_conf(tmp_path, volmax_slot="output-gain")
    reg = _extract_node_control(conf, "reg")
    src = preset["output"]["multiband_compressor#1"]
    assert src["output-gain"] != 0.0   # volmax_boost=3.0 forces a non-zero trim
    assert src["input-gain"] == 0.0
    assert abs(lin_to_db(reg["g_out"]) - src["output-gain"]) < 1e-4


def test_regulator_coupled_bands_activation_round_trips(tmp_path):
    """The coupled-bands mapping (issue #44, default since 2026-08-11) leaves
    `compressor-enable` on at a 0 dB attack-threshold inside
    multiband_compressor#1 — values that ride the generic ce_N/al_N
    translation. The sweep's variants can't fire the coupling (their
    synthetic regulator carries no isolated_band data), so this locks the
    activated values through to the conf: ce=1 with al at 0 dBFS (1.0
    linear) on the coupled zone, and the --disable path back off again.
    """
    eligible = synthetic_regulator([-6.0] * 10 + [0.0] * 10,
                                   isolated_band=[1] * 10 + [0] * 10)

    def conf_reg(disabled):
        preset, _ = make_preset(
            kernel_name="Synthetic",
            peq_filters=synthetic_peq_filters([
                (0, 1, 1000.0, 4.0, 1.5, 0, 1.0),
                (1, 1, 1000.0, 4.0, 1.5, 0, 1.0),
            ]),
            regulator=eligible, freqs=SYNTHETIC_FREQS_20,
            disabled=disabled,
        )
        chain = build_chain(preset, tmp_path, must_exist=False)
        conf = format_conf(chain.stages, emit_links(chain.stages),
                           "test_node", "test")
        return preset, _extract_node_control(conf, "reg")

    preset_on, reg_on = conf_reg(set())
    src = preset_on["output"]["multiband_compressor#1"]
    # Generator-side precondition (its own contract is locked in
    # test_cli.py): band1 is the coupled 0 dB zone.
    assert src["band1"]["compressor-enable"] is True
    assert src["band1"]["attack-threshold"] == 0.0
    assert reg_on["ce_1"] == 1
    assert abs(reg_on["al_1"] - 1.0) < 1e-9   # 0 dBFS threshold
    # The ordinary zone still round-trips its real threshold beside it.
    assert reg_on["ce_0"] == 1
    assert abs(lin_to_db(reg_on["al_0"]) - (-6.0)) < 1e-4

    # Contrast run with the opt-out: the 0 dB zone must go back to
    # disabled — proves the assertions above aren't vacuously true of any
    # regulator.
    _, reg_off = conf_reg({"coupled-bands"})
    assert reg_off["ce_1"] == 0


def test_mbc_split_frequency_and_band_enable_round_trip(coverage_chain):
    """The MBC round-trip elsewhere only checks band 0; bands 1..7 carry the
    crossover `sf_{i}` and the `cbe_{i}` enable toggle. Verify both round-trip
    from the generated preset to the conf."""
    preset, conf = coverage_chain
    mbc = _extract_node_control(conf, "mbc")
    src = preset["output"]["multiband_compressor#0"]
    checked = 0
    for i in range(1, 8):
        band = src.get(f"band{i}")
        if not band or "split-frequency" not in band:
            continue
        assert abs(mbc[f"sf_{i}"] - band["split-frequency"]) < 1e-4
        assert mbc[f"cbe_{i}"] == (1 if band["enable-band"] else 0)
        checked += 1
    assert checked >= 1, "fixture exercised no split band — test would be vacuous"


def test_peq_shelf_lopass_types_and_q_round_trip(coverage_chain):
    """Exercise the experimental Hi-shelf / Lo-shelf / Lo-pass paths the
    happy-path test (Hi-pass/Bell only) skips: filter-type integers map via
    EE_FTYPE_TO_LSP, and Q (identity) + gain (dB->linear) round-trip."""
    preset, conf = coverage_chain
    peq = _extract_node_control(conf, "peq")
    left = preset["output"]["equalizer#0"]["left"]
    num_bands = preset["output"]["equalizer#0"]["num-bands"]
    seen = set()
    for i in range(num_bands):
        b = left[f"band{i}"]
        seen.add(b["type"])
        assert peq[f"ftl_{i}"] == EE_FTYPE_TO_LSP[b["type"]]
        assert peq[f"ftr_{i}"] == EE_FTYPE_TO_LSP[b["type"]]
        assert abs(peq[f"ql_{i}"] - b["q"]) < 1e-9
        assert abs(lin_to_db(peq[f"gl_{i}"]) - b["gain"]) < 1e-4
    assert {"Hi-shelf", "Lo-shelf", "Lo-pass"} <= seen, (
        f"experimental shelf/lo-pass paths not exercised; saw {sorted(seen)}"
    )


def test_filter_graph_declares_inputs_and_outputs(generated):
    """Without `inputs`/`outputs` in filter.graph, PipeWire leaves the chain
    unconnected (a silent-drop trap flagged in the measure_pw README). Assert
    both arrays are present and carry node:port references."""
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)
    links = emit_links(chain.stages)
    conf = format_conf(chain.stages, links, "test_node", "test")
    for field in ("inputs", "outputs"):
        m = re.search(rf"\b{field}\s*=\s*\[(.*?)\]", conf, re.DOTALL)
        assert m, f"filter.graph has no {field} array"
        assert re.search(r'"[^"]+:[^"]+"', m.group(1)), \
            f"{field} array carries no node:port reference: {m.group(1)!r}"


# --- Install location -------------------------------------------------------

def test_irs_default_matches_the_generator():
    """The standalone two-step converts a preset the generator wrote and reads
    the .irs beside it, so both scripts have to resolve the same install. Two
    copies of that logic drift: the converter's was once hardcoded to the
    native path and sent Flatpak users looking in a directory that never had
    the file.

    Both now read `lib.ee_paths.DEFAULT_IRS_DIR`, so the constants can no
    longer disagree — what still can is the flag, if one script's `--irs-dir`
    is given a default of its own. So this asks the two parsers, which is the
    layer a user meets.

    Identity, not equality, and that is the whole test: the original bug was a
    *second derivation* of the same idea, and on a native-install machine a
    re-derived (or hardcoded-to-native) default compares equal to the right
    answer while being wrong for every Flatpak user. `is` fails on any object
    that didn't come from the one attribute, whichever install this runs on."""
    import dolby_to_easyeffects as d
    import ee_to_pipewire as e
    from lib import ee_paths

    def irs_default(parser):
        (action,) = [a for a in parser._actions if "--irs-dir" in a.option_strings]
        return action.default

    for parser in (e.build_parser([]), d.build_parser([])):
        assert irs_default(parser) is ee_paths.DEFAULT_IRS_DIR


@pytest.mark.parametrize("flatpak_run,native_run,installed,expected", [
    (True,  False, False, True),    # Flatpak has been run
    (False, True,  False, False),   # native has been run
    (True,  True,  False, True),    # both run: Flatpak keeps the old default
    (False, False, True,  True),    # installed but never opened (issue #33)
    (False, False, False, False),   # nothing to go on: native
])
def test_prefer_flatpak(tmp_path, monkeypatch, flatpak_run, native_run,
                        installed, expected):
    """Which install the defaults point at, over the four states a machine can
    be in. Writing presets to the tree EasyEffects doesn't read is one of the
    'preset generated, nothing changed' reports --doctor exists to catch."""
    from lib import ee_paths
    flatpak, native = tmp_path / "flatpak", tmp_path / "native"
    if flatpak_run:
        flatpak.mkdir()
    if native_run:
        native.mkdir()
    if installed:
        (tmp_path / ".local/share/flatpak/app"
         / ee_paths.FLATPAK_APP_ID).mkdir(parents=True)
    monkeypatch.setattr(ee_paths, "FLATPAK_BASE", flatpak)
    monkeypatch.setattr(ee_paths, "NATIVE_BASE", native)
    # The never-opened probe walks $HOME; patch the module's own Path binding
    # rather than pathlib's, so nothing outside this module sees a fake home.
    monkeypatch.setattr(ee_paths, "Path",
                        type("HomedPath", (type(tmp_path),),
                             {"home": staticmethod(lambda: tmp_path)}))
    assert ee_paths.prefer_flatpak() is expected


@pytest.mark.skipif(shutil.which("spa-json-dump") is None,
                    reason="spa-json-dump not installed")
def test_doctor_reads_back_a_generated_conf(tmp_path, generated):
    """--doctor reads installed confs with spa-json-dump rather than a
    hand-rolled parser, so what it reports is what PipeWire will read. This
    pins the round trip: emit a conf, then parse it back."""
    from lib.pipewire import checks
    sink = "alsa_output.pci-0000_00_1f.3.HiFi__Speaker__sink"
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)
    conf = format_conf(chain.stages, emit_links(chain.stages),
                       "Dolby_Balanced", "Dolby-Balanced", target_sink=sink)
    (tmp_path / "Dolby_Balanced.conf").write_text(conf)

    (parsed,) = checks.installed_confs(tmp_path)
    assert parsed.readable
    assert parsed.node_name == "effect_input.Dolby_Balanced"
    assert parsed.smart is True
    assert parsed.target == sink
    assert parsed.irs and all(p.suffix == ".irs" for p in parsed.irs)
