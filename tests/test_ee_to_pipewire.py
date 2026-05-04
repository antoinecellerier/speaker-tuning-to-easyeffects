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
    EE_FTYPE_TO_LSP,
    build_chain,
    db_to_lin,
    emit_convolver,
    emit_limiter,
    emit_links,
    emit_mb_compressor,
    emit_peq,
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
    assert any("autogain" in w and "v1 doesn't translate" in w
               for w in chain.warnings)
