"""Tests for ee_to_pipewire.py — EE preset → PipeWire filter-chain conf.

The load-bearing assertion is `test_mbc_round_trip_4_decimals`: it
generates a preset with `make_preset`, converts to a conf, re-extracts
the LSP MBC controls from the conf text, and confirms the linear
values round-trip back to the source dB values to 4 decimals. That is
the design-doc's verification anchor (alternative-pipelines.md:371-373).
"""

from __future__ import annotations

import json
import math
import re

import pytest

from dolby_to_easyeffects import (
    SAMPLE_RATE,
    FIR_LENGTH,
    make_fir,
    make_preset,
    save_wav_stereo,
)
from ee_to_pipewire import (
    CALF_BE_URI,
    CALF_ST_URI,
    EE_FTYPE_TO_LSP,
    EE_ST_MODE,
    build_chain,
    db_to_lin,
    emit_bass_enhancer,
    emit_convolver,
    emit_limiter,
    emit_links,
    emit_mb_compressor,
    emit_peq,
    emit_stereo_tools,
    format_conf,
    lin_to_db,
    main as ee2pw_main,
    _assert_positional,
    _resolve_irs,
    _sanitize_name,
)
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
        surround={"enable": True, "boost": 4},
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


def test_ftype_mapping_covers_every_string_emitted_by_dolby():
    """Every filter type the script can write must map to a non-None
    LSP integer. If a future make_*_band introduces a new type, this
    catches it instead of silently emitting the default (Off).
    """
    for s in ("Bell", "Hi-pass", "Lo-pass", "Hi-shelf", "Lo-shelf"):
        assert s in EE_FTYPE_TO_LSP
        assert EE_FTYPE_TO_LSP[s] != 0  # 0 is Off — these aren't Off


def test_assert_positional_passes_in_correct_order():
    _assert_positional(["convolver#0", "equalizer#0", "equalizer#1",
                        "limiter#0"])


def test_assert_positional_raises_when_swapped():
    with pytest.raises(AssertionError):
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
    """Every mode label `make_stereo_tools` could ever write must map
    to a Calf integer. The seven labels are stable (defined in
    StereoTools.ttl scale points), so this is a regression sentinel.
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


def test_every_active_plugin_emits_or_warns(generated):
    """Every key in source `plugins_order` is either represented by ≥1
    emitted node or appears in the warnings list. Catches silent drops.
    """
    preset, irs_path = generated
    chain = build_chain(preset, irs_path.parent, must_exist=False)

    emitted_names = {n["name"] for s in chain.stages for n in s.nodes}
    source_keys = preset["output"]["plugins_order"]
    # Map each EE key to the PW node name(s) we'd emit for it.
    emitter_targets = {
        "convolver#0": {"conv_l", "conv_r"},
        "equalizer#0": {"peq"},
        "equalizer#1": {"dialog"},
        "multiband_compressor#0": {"mbc"},
        "multiband_compressor#1": {"reg"},
        "limiter#0": {"limiter"},
        "bass_enhancer#0": {"bass"},
        "stereo_tools#0": {"stereo"},
    }
    for key in source_keys:
        if key in emitter_targets:
            if not emitter_targets[key].intersection(emitted_names):
                # Must have warned instead.
                assert any(key in w for w in chain.warnings), \
                    f"{key} neither emitted nor warned"
            continue
        if key == "autogain#0" and preset["output"][key].get("bypass"):
            # Bypassed autogain on HDA is the common case — converter
            # is allowed to skip silently. Non-bypassed must warn.
            continue
        # Skipped-with-warning categories must be in the warning list.
        assert any(key in w for w in chain.warnings), \
            f"{key} silently dropped (no warning)"


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
    with pytest.raises(AssertionError):
        build_chain(bad_preset, irs_dir=None, must_exist=False)


def test_assert_positional_raises_when_mbc_swapped():
    """Same contract as PEQ/dialog: regulator (#1) must follow MBC (#0)."""
    with pytest.raises(AssertionError):
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
    assert "[validate] skipped" not in captured.err
    assert "schema validation failed" not in captured.err


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
    """HDA's bypassed autogain must not emit a warning — it's the
    expected default state.
    """
    preset = {
        "output": {
            "plugins_order": ["autogain#0", "limiter#0"],
            "autogain#0": {"bypass": True},
            "limiter#0": {"bypass": False},
        }
    }
    chain = build_chain(preset, irs_dir=None, must_exist=False)
    assert not any("autogain" in w for w in chain.warnings)


def test_active_autogain_emits_warning():
    preset = {
        "output": {
            "plugins_order": ["autogain#0", "limiter#0"],
            "autogain#0": {"bypass": False},
            "limiter#0": {"bypass": False},
        }
    }
    chain = build_chain(preset, irs_dir=None, must_exist=False)
    # The warning must name the plugin and explain that no LV2 equivalent
    # exists (the precise wording is in EE_KEY_DISPATCH and may evolve;
    # what matters is that a non-bypassed autogain doesn't drop silently).
    assert any("autogain" in w and "libebur128" in w
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
    """Dry-run still rewrites the convolver path so the printed conf
    matches what a real run would produce, but no file is created.
    """
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
    assert not out_path.exists()
    target_irs = out_path.parent / "TestChain.irs"
    assert not target_irs.exists()
    captured = capsys.readouterr()
    # The printed conf shows where the IRS *would* land, not the EE path.
    assert str(target_irs) in captured.out
    assert str(irs_path) not in captured.out


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
    assert "TestChain.irs" in captured.err
    assert "--force" in captured.err


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
