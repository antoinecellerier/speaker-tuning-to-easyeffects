#!/usr/bin/env python3
"""Convert an EasyEffects output preset (the JSON `dolby_to_easyeffects.py`
emits) into a PipeWire `filter-chain` `.conf`.

v1 scope (deliberately tight; see docs/alternative-pipelines.md):
  - convolver, equalizer (PEQ), equalizer (dialog), multiband_compressor
    (MBC + regulator), and limiter are translated.
  - bass_enhancer, stereo_tools, and non-bypassed autogain are skipped
    with a warning.
  - Stereo only. No 4-channel upmix, no WirePlumber routing rules.

Reads the EE preset JSON, walks `plugins_order`, dispatches each plugin
to a stage emitter, generates pair-wise stereo links, and writes a
PipeWire SPA-JSON conf. Stage parameters round-trip with the source
preset to 4 decimals (dB → linear conversions are explicit).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

SCRIPT_DIR = Path(__file__).resolve().parent
VALIDATE_CONF_SCRIPT = SCRIPT_DIR / "tools" / "measure_pw" / "validate_conf.py"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LSP_PEQ_URI = "http://lsp-plug.in/plugins/lv2/para_equalizer_x16_lr"
LSP_MBC_URI = "http://lsp-plug.in/plugins/lv2/mb_compressor_stereo"
LSP_LIM_URI = "http://lsp-plug.in/plugins/lv2/limiter_stereo"

DEFAULT_IRS_DIR = Path.home() / ".local/share/easyeffects/irs"
# PipeWire's stock pipewire.conf only auto-includes the
# pipewire.conf.d/*.conf overlay set; filter-chain.conf.d/ is *not*
# scanned by the daemon (it's the path for the standalone
# `pipewire -c filter-chain.conf` invocation pattern, used by the
# measurement rig). Drop the conf in pipewire.conf.d/ so the running
# daemon picks it up on the next restart.
DEFAULT_OUTPUT_DIR = Path.home() / ".config/pipewire/pipewire.conf.d"
DEFAULT_NODE_NAME = "Dolby_Filter_Chain"
DEFAULT_NODE_DESCRIPTION = "Dolby DAX3 (filter-chain)"
DEFAULT_LINK_GROUP_SUFFIX = "_smart_filter"

LIN_AMP_FLOOR = 1e-30  # numerical floor for log10 in lin_to_db
LSP_PEQ_BANDS = 16     # para_equalizer_x16_lr — bump if URI changes
LSP_MBC_BANDS = 8      # mb_compressor_stereo

# All enum tables are extracted directly from the LSP plugin source under
# localresearch/measure_dax/lsp-plugins-1.2.27/modules/.

# para_equalizer.cpp:70 — filter_types[]
EE_FTYPE_TO_LSP = {
    "Off": 0, "Bell": 1, "Hi-pass": 2, "Hi-shelf": 3,
    "Lo-pass": 4, "Lo-shelf": 5, "Notch": 6, "Resonance": 7,
    "Allpass": 8, "Bandpass": 9, "Ladder-pass": 10, "Ladder-rej": 11,
}
# para_equalizer.cpp:94 — filter_modes[]
# EE only emits "RLC (BT)" in practice; other entries are kept for forward-
# compatibility if EE ever surfaces the mode picker for Dolby presets.
EE_FMODE_TO_LSP = {
    "RLC (BT)": 0, "RLC (MT)": 1, "BWC (BT)": 2, "BWC (MT)": 3,
    "LRX (BT)": 4, "LRX (MT)": 5, "APO (DR)": 6,
}
# para_equalizer.cpp:52 — filter_slopes[]
EE_FSLOPE_TO_LSP = {"x1": 0, "x2": 1, "x3": 2, "x4": 3}
# para_equalizer.cpp:61 — equalizer_eq_modes[]
EE_EQMODE_TO_LSP = {"IIR": 0, "FIR": 1, "FFT": 2, "SPM": 3}

# mb_compressor.cpp:113 — mb_global_comp_modes[]
EE_MBC_GLOBAL_MODE = {"Classic": 0, "Modern": 1, "Linear Phase": 2}
# mb_compressor.cpp:95 — mb_comp_sc_boost[]
EE_MBC_ENVB = {
    "None": 0, "Pink BT": 1, "Pink MT": 2, "Brown BT": 3, "Brown MT": 4,
}
# mb_compressor.cpp:64 — mb_comp_sc_modes[]
EE_MBC_SCMODE = {"Peak": 0, "RMS": 1, "LPF": 2, "SMA": 3}

# limiter.cpp:52 — limiter_oper_modes[]
EE_LIMITER_MODE = {
    "Herm Thin": 0, "Herm Wide": 1, "Herm Tail": 2, "Herm Duck": 3,
    "Exp Thin": 4, "Exp Wide": 5, "Exp Tail": 6, "Exp Duck": 7,
    "Line Thin": 8, "Line Wide": 9, "Line Tail": 10, "Line Duck": 11,
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Stage:
    """One EE plugin's PipeWire realisation.

    `nodes` may be a single stereo node (PEQ, MBC, limiter) or two mono
    nodes (the builtin convolver, one per channel). The four port
    references abstract that away so the link generator can pair stages
    without special-casing.
    """
    nodes: list[dict]
    in_l: tuple[str, str]
    in_r: tuple[str, str]
    out_l: tuple[str, str]
    out_r: tuple[str, str]
    warnings: list[str] = field(default_factory=list)


@dataclass
class ChainResult:
    stages: list[Stage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def db_to_lin(db: float) -> float:
    """Convert dB to linear gain. 0 dB → 1.0 exactly."""
    return 10.0 ** (db / 20.0)


def lin_to_db(lin: float) -> float:
    """Inverse of db_to_lin. Used by tests for the round-trip assertion."""
    return 20.0 * math.log10(max(lin, LIN_AMP_FLOOR))


def _resolve_irs(kernel_name: str, irs_dir: Path, must_exist: bool = True) -> Path:
    """Resolve <kernel-name>.irs against irs_dir to an absolute path."""
    # EE itself rejects path separators in kernel-name; mirror that here so
    # a malformed preset can't escape the IRS dir.
    if "/" in kernel_name or kernel_name.startswith("..") or "\0" in kernel_name:
        raise ValueError(
            f"invalid kernel-name {kernel_name!r}: must not contain path "
            "separators (EE forbids them too)"
        )
    path = (irs_dir / f"{kernel_name}.irs").expanduser().resolve()
    if must_exist and not path.is_file():
        raise FileNotFoundError(
            f"IRS file not found: {path}. Pass --irs-dir to point at the "
            "directory containing the .irs."
        )
    return path


def _retarget_convolver_irs(stages: list["Stage"],
                            target_irs: Path) -> Path | None:
    """Rewrite every convolver node's `filename` to ``target_irs`` and
    return the original source path (or ``None`` if the chain has no
    convolver). All convolver nodes share one source IRS, so we copy
    once and point both channels at the same destination.
    """
    src: Path | None = None
    for stage in stages:
        for node in stage.nodes:
            if node.get("label") != "convolver":
                continue
            if src is None:
                src = Path(node["config"]["filename"])
            node["config"]["filename"] = str(target_irs)
    return src


def _assert_positional(plugins_order: list[str]) -> None:
    """Lock the dialog/PEQ and MBC/regulator disambiguation contracts.

    Both `equalizer#0` (PEQ) and `equalizer#1` (dialog) are emitted by
    `dolby_to_easyeffects.py` with the same dict shape — only their
    position in `plugins_order` distinguishes them. The same is true of
    `multiband_compressor#0` (MBC) and `multiband_compressor#1`
    (regulator). If a future change reordered either pair, the
    converter's mapping would silently swap roles. Fail loudly here.
    """
    if "equalizer#0" in plugins_order and "equalizer#1" in plugins_order:
        i0 = plugins_order.index("equalizer#0")
        i1 = plugins_order.index("equalizer#1")
        assert i0 < i1, (
            "equalizer#0 (PEQ) must precede equalizer#1 (dialog) in "
            "plugins_order; got " + repr(plugins_order)
        )
    if ("multiband_compressor#0" in plugins_order
            and "multiband_compressor#1" in plugins_order):
        i0 = plugins_order.index("multiband_compressor#0")
        i1 = plugins_order.index("multiband_compressor#1")
        assert i0 < i1, (
            "multiband_compressor#0 (MBC) must precede "
            "multiband_compressor#1 (regulator) in plugins_order; got "
            + repr(plugins_order)
        )


def _sanitize_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


# ---------------------------------------------------------------------------
# Stage emitters
# ---------------------------------------------------------------------------

def emit_convolver(plugin: dict, irs_dir: Path,
                   must_exist: bool = True) -> Stage | None:
    """Two builtin `convolver` nodes, one per channel, in parallel.

    PipeWire's builtin convolver is mono — one node per output channel.
    EE's single `convolver#0` plugin therefore expands to two PW nodes
    (`conv_l`, `conv_r`) reading channels 0 and 1 of the same stereo IRS.
    """
    # Defensive — build_chain skips bypassed plugins before dispatch, but
    # the unit tests call emitters directly.
    if plugin.get("bypass", False):
        return None

    kernel_name = plugin.get("kernel-name")
    if not kernel_name:
        raise ValueError(
            "convolver#0 has no 'kernel-name' field; cannot resolve IRS path"
        )
    irs_path = _resolve_irs(kernel_name, irs_dir, must_exist=must_exist)
    output_gain_lin = db_to_lin(plugin.get("output-gain", 0.0))

    # The PW builtin convolver's `gain` config field scales the IR
    # samples on load (libpipewire-module-filter-chain(7)). EE's
    # convolver output_gain is universally 0.0 across the 1050-XML
    # corpus, so this is structurally identity in practice; passing it
    # through anyway keeps the chain faithful for any preset that does
    # set it.
    nodes = [
        {
            "type": "builtin",
            "name": "conv_l",
            "label": "convolver",
            "config": {"filename": str(irs_path), "channel": 0,
                       "gain": output_gain_lin},
        },
        {
            "type": "builtin",
            "name": "conv_r",
            "label": "convolver",
            "config": {"filename": str(irs_path), "channel": 1,
                       "gain": output_gain_lin},
        },
    ]
    return Stage(
        nodes=nodes,
        in_l=("conv_l", "In"), in_r=("conv_r", "In"),
        out_l=("conv_l", "Out"), out_r=("conv_r", "Out"),
    )


def _emit_peq_node(plugin: dict, name: str) -> dict:
    """Build the para_equalizer_x16_lr control dict from an EE PEQ plugin."""
    eq_mode = EE_EQMODE_TO_LSP.get(plugin.get("mode", "IIR"), 0)
    control: dict = {
        "mode": eq_mode,
        "g_in": db_to_lin(plugin.get("input-gain", 0.0)),
        "g_out": db_to_lin(plugin.get("output-gain", 0.0)),
    }

    num_bands = plugin.get("num-bands", 0)
    left = plugin.get("left", {})
    right = plugin.get("right", {})

    for i in range(LSP_PEQ_BANDS):
        if i < num_bands:
            for side, bands in (("l", left), ("r", right)):
                band = bands.get(f"band{i}", {})
                control[f"ft{side}_{i}"] = EE_FTYPE_TO_LSP.get(
                    band.get("type", "Off"), 0)
                control[f"fm{side}_{i}"] = EE_FMODE_TO_LSP.get(
                    band.get("mode", "RLC (BT)"), 0)
                control[f"s{side}_{i}"] = EE_FSLOPE_TO_LSP.get(
                    band.get("slope", "x1"), 0)
                control[f"f{side}_{i}"] = float(band.get("frequency", 1000.0))
                control[f"g{side}_{i}"] = db_to_lin(band.get("gain", 0.0))
                control[f"q{side}_{i}"] = float(band.get("q", 1.0))
                control[f"w{side}_{i}"] = float(band.get("width", 4.0))
                # xm = filter MUTE (default 0 = not muted; 1 = muted).
                # The control name is "xm" (mute), not enable — getting
                # this inverted mutes every band and the whole PEQ
                # silently passes through. para_equalizer.cpp:201:
                #   SWITCH("xm" id "_" #x, "Filter mute " ..., 0.0f)
                control[f"xm{side}_{i}"] = 1 if band.get("mute", False) else 0
                control[f"xs{side}_{i}"] = 1 if band.get("solo", False) else 0
        else:
            # Bands above num-bands are turned Off on both channels so
            # the LSP plugin doesn't process random defaults at runtime.
            control[f"ftl_{i}"] = 0
            control[f"ftr_{i}"] = 0

    return {
        "type": "lv2",
        "name": name,
        "plugin": LSP_PEQ_URI,
        "control": control,
    }


def emit_peq(plugin: dict, name: str) -> Stage | None:
    """Single LSP para_equalizer_x16_lr node.

    Used for both `equalizer#0` (speaker PEQ) and `equalizer#1` (dialog
    enhancer). The dialog enhancer's `make_dialog_enhancer` always sets
    `split-channels=False` and writes identical bands to left/right;
    the speaker PEQ always sets `split-channels=True`. The `_lr` plugin
    handles both — we just feed identical bands when not split.
    """
    # Defensive — build_chain handles bypass; kept for direct unit-test calls.
    if plugin.get("bypass", False):
        return None
    node = _emit_peq_node(plugin, name)
    return Stage(
        nodes=[node],
        in_l=(name, "in_l"), in_r=(name, "in_r"),
        out_l=(name, "out_l"), out_r=(name, "out_r"),
    )


def emit_mb_compressor(plugin: dict, name: str) -> Stage | None:
    """LSP mb_compressor_stereo node.

    Used for both `multiband_compressor#0` (the MBC) and
    `multiband_compressor#1` (the regulator). Identical shape on the EE
    side, identical mapping here.
    """
    # Defensive — build_chain handles bypass; kept for direct unit-test calls.
    if plugin.get("bypass", False):
        return None

    # LSP mb_compressor_stereo doesn't expose a `bypass` control or
    # `ssplit` (the stereo variant always processes L/R together).
    # Stick to controls confirmed against mb_compressor.cpp:185-194.
    control: dict = {
        "mode": EE_MBC_GLOBAL_MODE.get(plugin.get("compressor-mode", "Modern"), 1),
        "g_in": db_to_lin(plugin.get("input-gain", 0.0)),
        "g_out": db_to_lin(plugin.get("output-gain", 0.0)),
        "g_dry": db_to_lin(plugin.get("dry", -80.01)),
        "g_wet": db_to_lin(plugin.get("wet", 0.0)),
        "envb": EE_MBC_ENVB.get(plugin.get("envelope-boost", "None"), 0),
    }

    for i in range(LSP_MBC_BANDS):
        band = plugin.get(f"band{i}", {})
        # Band 0 always enabled with no split-frequency. Bands 1..7 carry
        # `enable-band` and `split-frequency`.
        if i > 0:
            control[f"cbe_{i}"] = 1 if band.get("enable-band", False) else 0
            control[f"sf_{i}"] = float(band.get("split-frequency", 100.0))
        control[f"ce_{i}"] = 1 if band.get("compressor-enable", False) else 0
        control[f"al_{i}"] = db_to_lin(band.get("attack-threshold", -12.0))
        control[f"at_{i}"] = float(band.get("attack-time", 20.0))
        control[f"rrl_{i}"] = db_to_lin(band.get("release-threshold", -80.01))
        control[f"rt_{i}"] = float(band.get("release-time", 100.0))
        control[f"cr_{i}"] = float(band.get("ratio", 1.0))
        control[f"kn_{i}"] = db_to_lin(band.get("knee", -6.0))
        control[f"mk_{i}"] = db_to_lin(band.get("makeup", 0.0))
        control[f"scm_{i}"] = EE_MBC_SCMODE.get(
            band.get("sidechain-mode", "RMS"), 1)
        control[f"sla_{i}"] = float(band.get("sidechain-lookahead", 0.0))
        control[f"scp_{i}"] = db_to_lin(band.get("sidechain-preamp", 0.0))

    node = {
        "type": "lv2",
        "name": name,
        "plugin": LSP_MBC_URI,
        "control": control,
    }
    return Stage(
        nodes=[node],
        in_l=(name, "in_l"), in_r=(name, "in_r"),
        out_l=(name, "out_l"), out_r=(name, "out_r"),
    )


def emit_limiter(plugin: dict, name: str = "limiter") -> Stage | None:
    """LSP limiter_stereo node."""
    # Defensive — build_chain handles bypass; kept for direct unit-test calls.
    if plugin.get("bypass", False):
        return None

    # No `bypass` control on LSP limiter_stereo (limiter.cpp:150-188).
    control = {
        "mode": EE_LIMITER_MODE.get(plugin.get("mode", "Herm Thin"), 0),
        "g_in": db_to_lin(plugin.get("input-gain", 0.0)),
        "g_out": db_to_lin(plugin.get("output-gain", 0.0)),
        "th": db_to_lin(plugin.get("threshold", -1.0)),
        "lk": float(plugin.get("lookahead", 1.0)),
        "at": float(plugin.get("attack", 1.0)),
        "rt": float(plugin.get("release", 5.0)),
        # slink is U_PERCENT — value range 0..100, not 0..1. Pass EE's
        # `stereo-link` (also percent) directly. Verified at limiter.cpp:179.
        "slink": float(plugin.get("stereo-link", 100.0)),
        "alr": 1 if plugin.get("alr", False) else 0,
        "boost": 1 if plugin.get("gain-boost", False) else 0,
    }
    node = {
        "type": "lv2",
        "name": name,
        "plugin": LSP_LIM_URI,
        "control": control,
    }
    return Stage(
        nodes=[node],
        in_l=(name, "in_l"), in_r=(name, "in_r"),
        out_l=(name, "out_l"), out_r=(name, "out_r"),
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

@dataclass
class PluginHandler:
    """Dispatch entry for one EE plugin key.

    `emitter=None` marks a key whose v1 implementation is to skip with
    `skip_warning`. `silent_if_bypassed=True` suppresses the warning
    when the source plugin is bypassed — used for autogain, where the
    bypassed state is the HDA default and the user shouldn't be nagged.
    """
    emitter: Callable[..., "Stage | None"] | None
    args: tuple = ()
    skip_warning: str | None = None
    silent_if_bypassed: bool = False


EE_KEY_DISPATCH: dict[str, PluginHandler] = {
    "convolver#0":            PluginHandler(emit_convolver),
    "equalizer#0":            PluginHandler(emit_peq, ("peq",)),
    "equalizer#1":            PluginHandler(emit_peq, ("dialog",)),
    "multiband_compressor#0": PluginHandler(emit_mb_compressor, ("mbc",)),
    "multiband_compressor#1": PluginHandler(emit_mb_compressor, ("reg",)),
    "limiter#0":              PluginHandler(emit_limiter),
    "autogain#0": PluginHandler(
        None,
        skip_warning=(
            "autogain#0: not bypassed but v1 doesn't translate autogain. "
            "The PW chain will lack volume-leveler behaviour. "
            "Mostly affects SoundWire devices."
        ),
        silent_if_bypassed=True,
    ),
    "bass_enhancer#0": PluginHandler(
        None,
        skip_warning=(
            "bass_enhancer#0: v1 doesn't translate bass_enhancer (plugin "
            "choice between Bankstown LV2 and Calf BassEnhancer is "
            "unresolved). Affects SoundWire devices with small drivers."
        ),
    ),
    "stereo_tools#0": PluginHandler(
        None,
        skip_warning=(
            "stereo_tools#0: v1 doesn't translate stereo_tools (Calf "
            "StereoTools mapping is non-trivial). Affects presets with "
            "surround virtualizer."
        ),
    ),
}


def build_chain(preset: dict, irs_dir: Path,
                must_exist: bool = True) -> ChainResult:
    """Walk plugins_order and build the PW stage list."""
    out = preset.get("output")
    if not isinstance(out, dict):
        raise ValueError("preset has no 'output' object")
    plugins_order = out.get("plugins_order", [])
    _assert_positional(plugins_order)

    result = ChainResult()

    def warn(msg: str) -> None:
        result.warnings.append(msg)

    for key in plugins_order:
        plugin = out.get(key)
        if not isinstance(plugin, dict):
            warn(f"{key}: missing from preset object; skipped.")
            continue

        handler = EE_KEY_DISPATCH.get(key)
        if handler is None:
            warn(f"{key}: unknown plugin key; skipped.")
            continue

        bypassed = plugin.get("bypass", False)
        if handler.emitter is None:
            if bypassed and handler.silent_if_bypassed:
                continue
            assert handler.skip_warning is not None
            warn(handler.skip_warning)
            continue

        if bypassed:
            # Surface bypass-skip uniformly so users notice if their EE
            # bypassed-plugin choice silently disappeared.
            warn(f"{key}: bypassed in source preset; not emitted.")
            continue

        if key == "convolver#0":
            stage = handler.emitter(plugin, irs_dir, *handler.args,
                                    must_exist=must_exist)
        else:
            stage = handler.emitter(plugin, *handler.args)
        if stage is not None:
            result.stages.append(stage)
            for w in stage.warnings:
                warn(w)

    return result


def emit_links(stages: list[Stage]) -> list[dict]:
    """Generate stereo pair-wise links between adjacent stages."""
    links = []
    for prev, nxt in zip(stages, stages[1:]):
        links.append({
            "output": f"{prev.out_l[0]}:{prev.out_l[1]}",
            "input": f"{nxt.in_l[0]}:{nxt.in_l[1]}",
        })
        links.append({
            "output": f"{prev.out_r[0]}:{prev.out_r[1]}",
            "input": f"{nxt.in_r[0]}:{nxt.in_r[1]}",
        })
    return links


# ---------------------------------------------------------------------------
# SPA-JSON formatter
# ---------------------------------------------------------------------------

# Bare-key tokens (PipeWire SPA-JSON allows these unquoted)
_BARE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")


def _fmt_num(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        # Ten significant figures preserves EE's 4-decimal precision on
        # values up to ~1e6 (e.g. attack-time 649.4292 ms) without
        # lapsing into scientific notation. Trailing zeros are dropped
        # by %g.
        s = f"{v:.10g}"
        # Avoid emitting `inf`/`nan` — clamp to a representable value
        # so the conf still parses.
        if s in ("inf", "-inf", "nan"):
            return "0.0"
        return s
    raise TypeError(f"_fmt_num: unsupported type {type(v).__name__}")


def _fmt_value(v, indent: int) -> str:
    if v is None:
        return "null"
    if isinstance(v, (bool, int, float)):
        return _fmt_num(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[ " + " ".join(_fmt_value(x, indent) for x in v) + " ]"
    if isinstance(v, dict):
        return _fmt_dict(v, indent)
    raise TypeError(f"_fmt_value: unsupported type {type(v).__name__}")


def _fmt_key(k: str) -> str:
    return k if _BARE_KEY_RE.match(k) else json.dumps(k)


def _fmt_dict(d: dict, indent: int) -> str:
    pad = "    " * (indent + 1)
    closing = "    " * indent
    lines = []
    for k, v in d.items():
        lines.append(f"{pad}{_fmt_key(k)} = {_fmt_value(v, indent + 1)}")
    return "{\n" + "\n".join(lines) + "\n" + closing + "}"


def format_conf(stages: list[Stage], links: list[dict],
                node_name: str, node_description: str,
                target_object: str | None = None,
                target_sink: str | None = None,
                warnings: list[str] | None = None) -> str:
    """Render the full PipeWire filter-chain conf as text.

    ``target_sink`` (the WirePlumber 0.5+ smart-filter target — typically
    the internal speaker sink's ``node.name``) makes the chain attach
    transparently to that hardware sink: apps keep targeting the
    speaker sink as the default, WirePlumber's link resolver routes
    them through the filter automatically, the chain auto-bypasses on
    HDMI/Bluetooth/USB, and there's no second volume layer. When unset,
    falls back to the v1 virtual-sink behaviour.

    ``target_object`` is the lower-level "pin playback to this node"
    used by the measurement rig to redirect into a null sink; it
    coexists with ``target_sink`` but the smart-filter pattern usually
    makes it unnecessary outside test rigs.
    """
    if not stages:
        msg = (
            "cannot format conf with empty stage list (every plugin was "
            "either bypassed in the source preset or in the v1 cuts list)"
        )
        if warnings:
            msg += "; warnings collected during build:\n  - " + \
                "\n  - ".join(warnings)
        raise ValueError(msg)
    nodes = [n for s in stages for n in s.nodes]
    # Without explicit `inputs` / `outputs`, the filter.graph has no
    # external endpoints and PipeWire silently passes nothing through —
    # audio reaches the capture sink but never enters the graph. The
    # first stage's input ports become the chain's audio inputs; the
    # last stage's output ports become its audio outputs.
    first, last = stages[0], stages[-1]
    graph_inputs = [f"{first.in_l[0]}:{first.in_l[1]}",
                    f"{first.in_r[0]}:{first.in_r[1]}"]
    graph_outputs = [f"{last.out_l[0]}:{last.out_l[1]}",
                     f"{last.out_r[0]}:{last.out_r[1]}"]
    safe_name = _sanitize_name(node_name)
    capture_props: dict = {
        "node.name": f"effect_input.{safe_name}",
        "media.class": "Audio/Sink",
    }
    if target_sink:
        # Smart-filter mode: WirePlumber 0.5+'s linking pipeline
        # (linking/find-filter-target.lua + linking/get-filter-from-target.lua)
        # detects nodes with `filter.smart = true` plus a matching
        # `filter.smart.target` and inserts them on links targeting that
        # node. `node.link-group` ties the capture and playback streams
        # together as one logical filter so WP routes them as a unit.
        # `filter.smart.targetable` defaults to false, which keeps apps
        # from picking the chain's capture sink directly — they target
        # the hardware speaker sink as usual, the chain inserts itself
        # in the path. No second volume layer; HDMI/BT outputs aren't
        # matched, so the chain bypasses automatically when audio
        # routes anywhere other than ``target_sink``.
        #
        # `priority.session = -1` keeps WirePlumber's
        # default-nodes/find-best-default-node.lua from picking the
        # chain as the default sink (it would otherwise win the
        # tiebreaker against the speaker sink — both have priority 0
        # by default, and the freshly-loaded chain sorts later). With
        # smart-filter routing, the user wants the speaker sink to
        # remain the default; the chain inserts itself transparently.
        capture_props.update({
            "node.link-group": f"{safe_name}{DEFAULT_LINK_GROUP_SUFFIX}",
            "filter.smart": True,
            "filter.smart.name": safe_name,
            "filter.smart.target": {"node.name": target_sink},
            "priority.session": -1,
        })
    playback_props: dict = {
        "node.name": f"effect_output.{safe_name}",
        "node.passive": True,
    }
    if target_sink:
        # Same link-group on the playback side so WirePlumber treats the
        # capture and playback streams as one filter for routing.
        playback_props["node.link-group"] = (
            f"{safe_name}{DEFAULT_LINK_GROUP_SUFFIX}"
        )
    args = {
        "node.description": node_description,
        "media.name": node_description,
        "filter.graph": {
            "nodes": nodes,
            "links": links,
            "inputs": graph_inputs,
            "outputs": graph_outputs,
        },
        "audio.channels": 2,
        "audio.position": ["FL", "FR"],
        "capture.props": capture_props,
        "playback.props": playback_props,
    }
    if target_object:
        # Bind the playback stream to a specific downstream sink. Used
        # by the measurement tooling to route the chain into a null
        # sink (e.g. ee_capture) instead of the system default — without
        # this, WirePlumber auto-links to the actual speakers.
        args["playback.props"]["target.object"] = target_object
    module = {"name": "libpipewire-module-filter-chain", "args": args}
    body = (
        "# Generated by ee_to_pipewire.py — see\n"
        "# https://github.com/antoinecellerier/speaker-tuning-to-easyeffects\n"
        "#\n"
        "# IRS file paths are absolute. By default the converter copies\n"
        "# the .irs next to this conf, so the chain is self-contained;\n"
        "# re-run ee_to_pipewire.py to refresh after updating the source\n"
        "# EasyEffects preset.\n\n"
        "context.modules = [\n"
    )
    body += "    " + _fmt_dict(module, 1) + "\n"
    body += "]\n"
    return body


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _autodetect_speaker_sink() -> tuple[str | None, list[str]]:
    """Return (chosen_sink_name, warnings) for the smart-filter target.

    Reuses ``dolby_to_easyeffects.find_speaker_sinks()`` (same probe as
    the EasyEffects autoload pathway: ``pw-dump`` filtered to
    ``Audio/Sink`` nodes whose ``device.icon_name`` is
    ``audio-speakers``, which excludes HDMI / Bluetooth / USB headsets).
    Returns ``(None, [reason])`` if no unique speaker sink can be
    chosen — the caller surfaces that as an error and prompts for
    ``--target-sink``.
    """
    try:
        from dolby_to_easyeffects import find_speaker_sinks
    except Exception as e:  # pragma: no cover — defensive
        return None, [f"could not import speaker probe: {e}"]
    sinks = find_speaker_sinks()
    if not sinks:
        return None, [
            "no internal-speaker sink found via pw-dump "
            "(no PipeWire daemon running, or no Audio/Sink with "
            "device.icon_name=audio-speakers)"
        ]
    names = [s["name"] for s in sinks if s.get("name")]
    if len(names) == 1:
        return names[0], []
    return None, [
        f"multiple speaker sinks found ({len(names)}): "
        + ", ".join(names)
        + "; pass --target-sink to pick one"
    ]


def _validate_conf(conf_text: str) -> tuple[int, str]:
    """Run validate_conf.py against `conf_text`.

    Returns (returncode, combined_output). Returncode -1 signals a setup
    skip (script absent, or `lv2info`/`spa-json-dump` missing); the
    caller treats that as a soft warning, not a hard failure. The
    `validate_conf.py` script's own contract: 0 = clean, 1 = errors,
    2 = setup error.
    """
    if not VALIDATE_CONF_SCRIPT.is_file():
        return -1, f"validate_conf.py not found at {VALIDATE_CONF_SCRIPT}"
    if not shutil.which("lv2info") or not shutil.which("spa-json-dump"):
        return -1, ("lv2info or spa-json-dump not in PATH "
                    "(install lilv-utils and pipewire)")
    rc = subprocess.run(
        [sys.executable, str(VALIDATE_CONF_SCRIPT), "-", "-q"],
        input=conf_text, capture_output=True, text=True, timeout=30,
    )
    return rc.returncode, (rc.stderr or "") + (rc.stdout or "")


def _print_next_steps(stream, output_path: Path | None,
                      node_name: str,
                      target_object: str | None = None,
                      irs_path: Path | None = None) -> None:
    pre = "[next] "
    print(f"{pre}systemctl --user restart pipewire pipewire-pulse",
          file=stream)
    print(f"{pre}To prevent double processing, quit EasyEffects or "
          "remove its autoload for this device.", file=stream)
    print(f"{pre}Verify the new sink: pw-cli ls Node | grep "
          f"{_sanitize_name(node_name)}", file=stream)
    if target_object:
        print(f"{pre}Verify routing: pw-link -l | grep {target_object}",
              file=stream)
    if output_path is not None:
        print(f"{pre}Conf written to: {output_path}", file=stream)
    if irs_path is not None:
        print(f"{pre}IRS copied to:   {irs_path}", file=stream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert an EasyEffects output preset to a PipeWire "
                    "filter-chain .conf (v1; see docs/alternative-pipelines.md).",
    )
    parser.add_argument(
        "preset",
        type=Path,
        help="path to the EasyEffects preset JSON (the output of "
             "dolby_to_easyeffects.py, e.g. ~/.config/easyeffects/output/"
             "Dolby-Balanced.json)",
    )
    parser.add_argument(
        "--irs-dir",
        type=Path,
        default=DEFAULT_IRS_DIR,
        help=f"directory containing the .irs file referenced by the "
             f"preset's convolver (default: {DEFAULT_IRS_DIR})",
    )
    parser.add_argument(
        "--node-name",
        default=DEFAULT_NODE_NAME,
        help=f"PipeWire node-name suffix; sanitised to [A-Za-z0-9_] "
             f"(default: {DEFAULT_NODE_NAME})",
    )
    parser.add_argument(
        "--node-description",
        default=DEFAULT_NODE_DESCRIPTION,
        help=f"human-readable node description (default: "
             f"{DEFAULT_NODE_DESCRIPTION!r})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"output .conf path (default: "
             f"{DEFAULT_OUTPUT_DIR}/<node-name>.conf)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite the output file if it already exists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the generated conf to stdout instead of writing it; "
             "missing IRS files become warnings rather than errors",
    )
    parser.add_argument(
        "--target-object",
        default=None,
        help="bind the chain's playback to a specific downstream node "
             "(node.name) instead of letting WirePlumber choose. Useful "
             "for routing into a measurement null sink. End users "
             "usually want --target-sink instead, which is set by "
             "default and uses WirePlumber 0.5+ smart-filter routing "
             "so apps don't see the chain as a separate sink.",
    )
    parser.add_argument(
        "--target-sink",
        default=None,
        help="hardware sink (node.name) the filter should attach to as "
             "a WirePlumber smart filter. When set, apps target this "
             "sink as usual and the filter inserts itself into the path; "
             "no virtual-sink stacking, automatic bypass on HDMI / "
             "Bluetooth / USB outputs. Default: auto-detect the "
             "internal-speaker sink via pw-dump (same probe "
             "dolby_to_easyeffects.py --autoload uses). Pass an empty "
             "string ('') to disable smart-filter mode and emit the "
             "v1 virtual-sink conf (apps target effect_input.<name> "
             "directly).",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="skip the schema self-check against lv2info port metadata. "
             "By default, after generating the conf, ee_to_pipewire shells "
             "out to tools/measure_pw/validate_conf.py to catch unknown "
             "port symbols and out-of-range values; pass this flag to "
             "skip it (e.g. on systems without lv2info installed).",
    )
    parser.add_argument(
        "--no-copy-irs",
        action="store_true",
        help="don't copy the .irs next to the generated conf. By default "
             "the converter copies the impulse response from --irs-dir "
             "into the conf's directory and rewrites the convolver "
             "filename, so the PipeWire chain has no runtime dependency "
             "on the EasyEffects path layout. Pass this flag to keep the "
             "conf pointing at the original EE-side .irs (which lets EE "
             "preset regenerations propagate automatically, at the cost "
             "of a brittle cross-tree dependency).",
    )
    args = parser.parse_args(argv)

    preset_path: Path = args.preset
    if not preset_path.is_file():
        print(f"error: preset not found: {preset_path}", file=sys.stderr)
        return 2
    try:
        preset = json.loads(preset_path.read_text())
    except json.JSONDecodeError as e:
        print(f"error: preset JSON is malformed: {e}", file=sys.stderr)
        return 2

    safe_node_name = _sanitize_name(args.node_name)
    output_path: Path | None
    if args.dry_run:
        output_path = None
    elif args.output is not None:
        output_path = args.output.expanduser()
    else:
        output_path = (DEFAULT_OUTPUT_DIR / f"{safe_node_name}.conf").expanduser()

    try:
        chain = build_chain(preset, args.irs_dir.expanduser(),
                            must_exist=not args.dry_run)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not chain.stages:
        print("error: no stages emitted (preset is empty or every plugin "
              "was skipped)", file=sys.stderr)
        return 1

    # Resolve where the IRS will live. By default we copy it next to the
    # conf so the PW chain is self-contained; --no-copy-irs keeps the
    # original EE-side absolute path baked into the conf. In dry-run we
    # still compute the destination so the printed conf reflects what a
    # real run would produce.
    if output_path is not None:
        target_irs_dir = output_path.parent
    elif args.output is not None:
        target_irs_dir = args.output.expanduser().parent
    else:
        target_irs_dir = DEFAULT_OUTPUT_DIR.expanduser()
    target_irs = target_irs_dir / f"{safe_node_name}.irs"
    src_irs: Path | None = None
    if not args.no_copy_irs:
        src_irs = _retarget_convolver_irs(chain.stages, target_irs)

    # Resolve the smart-filter target sink. ``--target-sink ''`` (empty
    # string) explicitly disables smart-filter mode; an unset flag falls
    # through to autodetection.
    if args.target_sink == "":
        target_sink: str | None = None
        print("[smart-filter] disabled by --target-sink ''; emitting "
              "v1 virtual-sink conf (apps will target effect_input."
              f"{safe_node_name} directly)", file=sys.stderr)
    elif args.target_sink:
        target_sink = args.target_sink
        print(f"[smart-filter] target sink: {target_sink} (from "
              "--target-sink)", file=sys.stderr)
    else:
        target_sink, detect_warnings = _autodetect_speaker_sink()
        if target_sink:
            print(f"[smart-filter] target sink: {target_sink} "
                  "(autodetected)", file=sys.stderr)
        else:
            for w in detect_warnings:
                print(f"[smart-filter] {w}", file=sys.stderr)
            print("[smart-filter] falling back to v1 virtual-sink conf "
                  "(apps will target effect_input."
                  f"{safe_node_name}); pass --target-sink "
                  "<node.name> to enable smart-filter routing.",
                  file=sys.stderr)

    links = emit_links(chain.stages)
    conf = format_conf(chain.stages, links, args.node_name,
                       args.node_description,
                       target_object=args.target_object,
                       target_sink=target_sink,
                       warnings=chain.warnings)

    for w in chain.warnings:
        print(f"[skip] {w}", file=sys.stderr)

    if not args.no_validate:
        rc, output = _validate_conf(conf)
        if rc == -1:
            print(f"[validate] skipped: {output.strip()}", file=sys.stderr)
        elif rc == 2:
            # Setup error inside validate_conf.py — degraded gracefully.
            print(f"[validate] skipped (setup): {output.strip()}",
                  file=sys.stderr)
        elif rc != 0:
            sys.stderr.write(output)
            print("error: schema validation failed; conf not written",
                  file=sys.stderr)
            return 1

    if args.dry_run:
        sys.stdout.write(conf)
        return 0

    assert output_path is not None
    if output_path.exists() and not args.force:
        print(f"error: {output_path} exists; pass --force to overwrite",
              file=sys.stderr)
        return 1

    # IRS copy: skip when source and target are the same path (no-op),
    # otherwise honour the same --force semantics as the conf write.
    copied_irs: Path | None = None
    if src_irs is not None and src_irs.resolve() != target_irs.resolve():
        if target_irs.exists() and not args.force:
            print(f"error: {target_irs} exists; pass --force to overwrite",
                  file=sys.stderr)
            return 1
        target_irs.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_irs, target_irs)
        copied_irs = target_irs

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(conf)
    _print_next_steps(sys.stderr, output_path, args.node_name,
                      target_object=args.target_object,
                      irs_path=copied_irs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
