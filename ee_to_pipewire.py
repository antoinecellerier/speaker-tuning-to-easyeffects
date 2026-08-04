#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""Convert an EasyEffects output preset (the JSON `dolby_to_easyeffects.py`
emits) into a PipeWire `filter-chain` `.conf`.

Scope (see docs/ee-to-pipewire.md for full detail):
  - convolver, equalizer (PEQ), equalizer (dialog), multiband_compressor
    (MBC + regulator), limiter, and autogain are translated (LSP-backed);
    bass_enhancer and stereo_tools are translated (Calf-backed).
  - autogain (EE-native libebur128 volume leveler) → LSP autogain_stereo,
    a K-weighted loudness AGC. Bypassed instances (the HDA default) are
    skipped silently; active ones (SoundWire, or HDA with
    --enable autogain) are translated.
  - Stereo only; no 4-channel upmix. By default the conf is a
    WirePlumber 0.5+ smart filter pinned to the auto-detected
    internal-speaker sink (--target-sink overrides; '' gives a plain
    v1 virtual sink).

Reads the EE preset JSON, walks `plugins_order`, dispatches each plugin
to a stage emitter, generates pair-wise stereo links, and writes a
PipeWire SPA-JSON conf. Stage parameters round-trip with the source
preset to 4 decimals (dB → linear conversions are explicit).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import _doctor
from _doctor import (
    DOCTOR_FAIL,
    DOCTOR_PASS,
    DOCTOR_UNKNOWN,
    DOCTOR_WARN,
    CheckResult,
)
from _ee_paths import easyeffects_base
from _version import get_version

# Colored terminal output (optional rich; mirrors dolby_to_easyeffects.py's
# setup). The console targets stderr so it never pollutes the --dry-run conf
# on stdout, and resolves the current sys.stderr lazily so capsys can capture
# it in tests.
# Which stream the no-rich path writes to, as a flag rather than a stream
# object: sys.stderr has to stay resolved at call time so capsys can capture
# it. Swapped alongside the console by _report_on_stdout.
_PLAIN_TO_STDOUT = False

_THEME_STYLES = {
    "err":  "bold red",
    "head": "bold cyan",
    "ok":   "green",
    "warn": "yellow",
    "cta":  "bold magenta",
    "dim":  "dim",
}
try:
    from rich.console import Console
    from rich.theme import Theme

    def _make_console(stderr: bool):
        return Console(stderr=stderr, theme=Theme(_THEME_STYLES),
                       markup=False, highlight=False)

    _CONSOLE = _make_console(stderr=True)
except ImportError:
    _CONSOLE = None

    def _make_console(stderr: bool):
        return None


@contextlib.contextmanager
def _report_on_stdout():
    """Put everything this block prints on stdout, for the duration.

    The console targets stderr so a --dry-run conf on stdout stays clean, but
    a diagnostic report is meant to be redirected to a file or pasted into an
    issue — on stderr, `--doctor > report.txt` would write an empty file. No
    conf is emitted in that mode, so the two streams swap while it runs.
    """
    global _CONSOLE, _PLAIN_TO_STDOUT
    previous, previous_plain = _CONSOLE, _PLAIN_TO_STDOUT
    if previous is not None:
        _CONSOLE = _make_console(stderr=False)
    _PLAIN_TO_STDOUT = True
    try:
        yield
    finally:
        _CONSOLE, _PLAIN_TO_STDOUT = previous, previous_plain

try:
    from rich_argparse import RichHelpFormatter as _HelpFormatter
except ImportError:
    _HelpFormatter = argparse.HelpFormatter

_MISSING_COLOR_DEPS = []
if _CONSOLE is None:
    _MISSING_COLOR_DEPS.append("rich")
if _HelpFormatter is argparse.HelpFormatter:
    _MISSING_COLOR_DEPS.append("rich-argparse")

# Optional tab-completion (README "Shell tab-completion"). Absent argcomplete, this
# module stays stdlib-only and behaves exactly as before.
try:
    import argcomplete
except ImportError:
    argcomplete = None


def cprint(style: str, text: str = "") -> None:
    """Print `text` to stderr in the given semantic style, or plain if rich is
    absent. ``soft_wrap=True`` keeps the text exactly as written (no 80-col
    reflow) so commands/paths stay on one line and substring-asserting tests
    hold."""
    if _CONSOLE is None:
        print(text, file=sys.stdout if _PLAIN_TO_STDOUT else sys.stderr)
        return
    _CONSOLE.print(text, style=style, soft_wrap=True)


def _disable_color() -> None:
    global _CONSOLE
    _CONSOLE = None

SCRIPT_DIR = Path(__file__).resolve().parent
VALIDATE_CONF_SCRIPT = SCRIPT_DIR / "tools" / "measure_pw" / "validate_conf.py"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LSP_PEQ_URI = "http://lsp-plug.in/plugins/lv2/para_equalizer_x16_lr"
LSP_MBC_URI = "http://lsp-plug.in/plugins/lv2/mb_compressor_stereo"
LSP_LIM_URI = "http://lsp-plug.in/plugins/lv2/limiter_stereo"
# autogain_stereo is a K-weighted (LUFS) loudness AGC — the LV2 equivalent of
# EE's native libebur128 autogain (volume leveler). See emit_autogain.
LSP_AUTOGAIN_URI = "http://lsp-plug.in/plugins/lv2/autogain_stereo"
# Calf plugins back EE's bass_enhancer and stereo_tools modules (verified
# against the EasyEffects sources in src/{bass_enhancer,stereo_tools}.cpp:
# both call `lv2_wrapper` with these URIs and bind EE preset keys to the
# LV2 control symbols listed in the per-emitter mapping below).
CALF_BE_URI = "http://calf.sourceforge.net/plugins/BassEnhancer"
CALF_ST_URI = "http://calf.sourceforge.net/plugins/StereoTools"

# Matches whichever EasyEffects install the generator writes to — native
# or Flatpak, which keep their presets in different trees. Hardcoding the
# native path sent the standalone two-step looking for the impulse
# response somewhere Flatpak users never had it.
DEFAULT_IRS_DIR = easyeffects_base() / "irs"
# PipeWire's stock pipewire.conf only auto-includes the
# pipewire.conf.d/*.conf overlay set; filter-chain.conf.d/ is *not*
# scanned by the daemon (it's the path for the standalone
# `pipewire -c filter-chain.conf` invocation pattern, used by the
# measurement rig). Drop the conf in pipewire.conf.d/ so the running
# daemon picks it up on the next restart.
DEFAULT_OUTPUT_DIR = Path.home() / ".config/pipewire/pipewire.conf.d"
# First line of every conf this script writes. --doctor keys on it to tell
# our confs from a user's own filter chains, so it must stay in step with the
# header format_conf() emits.
CONF_HEADER_MARK = "# Generated by ee_to_pipewire.py"
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
# mb_compressor.cpp:105 — mb_comp_modes[] (Down/Up/Boost). The generator pins
# "Downward" on every band because LSP's boost path (bth −72 dB / bsa +6 dB
# defaults) is live in the other two modes and amplifies the noise floor —
# the audible trap commit e454711 fixed on the EE side. Translate it
# explicitly so the conf never rides the LV2 default.
EE_MBC_CM = {"Downward": 0, "Upward": 1, "Boosting": 2}

# limiter.cpp:52 — limiter_oper_modes[]
EE_LIMITER_MODE = {
    "Herm Thin": 0, "Herm Wide": 1, "Herm Tail": 2, "Herm Duck": 3,
    "Exp Thin": 4, "Exp Wide": 5, "Exp Tail": 6, "Exp Duck": 7,
    "Line Thin": 8, "Line Wide": 9, "Line Tail": 10, "Line Duck": 11,
}

# Calf StereoTools `mode` scale points (lv2info "...StereoTools" → port "mode";
# label strings are the exact `mode` values EE writes into preset JSON, see
# stereo_tools_preset.cpp:54 → `defaultModeLabelsValue()`).
EE_ST_MODE = {
    "LR > LR (Stereo Default)":      0,
    "LR > MS (Stereo to Mid-Side)":  1,
    "MS > LR (Mid-Side to Stereo)":  2,
    "LR > LL (Mono Left Channel)":   3,
    "LR > RR (Mono Right Channel)":  4,
    "LR > L+R (Mono Sum L+R)":       5,
    "LR > RL (Stereo Flip Channels)": 6,
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
    # Real raises, not bare asserts — the contract must survive `python -O`.
    for first, role1, second, role2 in (
            ("equalizer#0", "PEQ", "equalizer#1", "dialog"),
            ("multiband_compressor#0", "MBC",
             "multiband_compressor#1", "regulator")):
        if first in plugins_order and second in plugins_order \
                and plugins_order.index(first) >= plugins_order.index(second):
            raise ValueError(
                f"{first} ({role1}) must precede {second} ({role2}) in "
                f"plugins_order; got {plugins_order!r}")


def _sanitize_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


# ---------------------------------------------------------------------------
# Stage emitters
# ---------------------------------------------------------------------------

def _enum(table: dict[str, int], label: str, fallback: int,
          warnings: list[str], ctx: str) -> int:
    """Enum-label → LSP integer lookup that surfaces unknown labels.

    A silent `.get(label, fallback)` would turn a new EE label into the
    fallback integer (often 0 = Off) with no trace; warn instead so the
    missing table entry gets added. Deduped so 16 PEQ bands × 2 sides
    don't repeat one message. Call sites must keep the
    `plugin.get("key", …)` literal inline — the coverage guard in
    tests/test_ee_to_pipewire.py scrapes those literals from the source.
    """
    if label in table:
        return table[label]
    msg = (f"{ctx}: unknown enum label {label!r}; using fallback "
           f"{fallback}. Update the matching EE_* table in "
           "ee_to_pipewire.py.")
    if msg not in warnings:
        warnings.append(msg)
    return fallback


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

    warns: list[str] = []
    input_gain_db = plugin.get("input-gain", 0.0)
    if input_gain_db:
        # Generated presets always carry 0.0 here; a hand-edited preset
        # with a real trim would otherwise silently change level.
        warns.append(
            f"convolver#0: input-gain {input_gain_db:g} dB has no "
            "builtin-convolver port; not translated — output level will "
            "differ from EasyEffects.")

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
        warnings=warns,
    )


def _emit_peq_node(plugin: dict, name: str, warnings: list[str]) -> dict:
    """Build the para_equalizer_x16_lr control dict from an EE PEQ plugin."""
    eq_mode = _enum(EE_EQMODE_TO_LSP, plugin.get("mode", "IIR"), 0,
                    warnings, f"{name}: mode")
    control: dict = {
        "mode": eq_mode,
        "g_in": db_to_lin(plugin.get("input-gain", 0.0)),
        "g_out": db_to_lin(plugin.get("output-gain", 0.0)),
    }

    num_bands = plugin.get("num-bands", 0)
    if num_bands > LSP_PEQ_BANDS:
        warnings.append(
            f"{name}: preset declares {num_bands} EQ bands; "
            f"para_equalizer_x16_lr caps at {LSP_PEQ_BANDS} — bands "
            f"{LSP_PEQ_BANDS}..{num_bands - 1} dropped.")
    left = plugin.get("left", {})
    right = plugin.get("right", {})

    for i in range(LSP_PEQ_BANDS):
        if i < num_bands:
            for side, bands in (("l", left), ("r", right)):
                band = bands.get(f"band{i}", {})
                control[f"ft{side}_{i}"] = _enum(
                    EE_FTYPE_TO_LSP, band.get("type", "Off"), 0,
                    warnings, f"{name}: band type")
                control[f"fm{side}_{i}"] = _enum(
                    EE_FMODE_TO_LSP, band.get("mode", "RLC (BT)"), 0,
                    warnings, f"{name}: band mode")
                control[f"s{side}_{i}"] = _enum(
                    EE_FSLOPE_TO_LSP, band.get("slope", "x1"), 0,
                    warnings, f"{name}: band slope")
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
    warns: list[str] = []
    node = _emit_peq_node(plugin, name, warns)
    return Stage(
        nodes=[node],
        in_l=(name, "in_l"), in_r=(name, "in_r"),
        out_l=(name, "out_l"), out_r=(name, "out_r"),
        warnings=warns,
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

    # LSP mb_compressor_stereo has no `bypass` control (bypassed plugins
    # are skipped before dispatch). Controls confirmed against
    # mb_compressor.cpp:185-230.
    warns: list[str] = []
    control: dict = {
        "mode": _enum(EE_MBC_GLOBAL_MODE,
                      plugin.get("compressor-mode", "Modern"), 1,
                      warns, f"{name}: compressor-mode"),
        "g_in": db_to_lin(plugin.get("input-gain", 0.0)),
        "g_out": db_to_lin(plugin.get("output-gain", 0.0)),
        "g_dry": db_to_lin(plugin.get("dry", -80.01)),
        "g_wet": db_to_lin(plugin.get("wet", 0.0)),
        "envb": _enum(EE_MBC_ENVB, plugin.get("envelope-boost", "None"), 0,
                      warns, f"{name}: envelope-boost"),
    }
    # `ssplit` only exists on lsp-plugins >= 1.2.3, and the generator
    # always emits stereo-split=False == the port default — write it only
    # when a hand-edited preset turns it on, so confs keep loading on
    # older installed LSP.
    if plugin.get("stereo-split", False):
        control["ssplit"] = 1

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
        control[f"scm_{i}"] = _enum(
            EE_MBC_SCMODE, band.get("sidechain-mode", "RMS"), 1,
            warns, f"{name}: sidechain-mode")
        control[f"sla_{i}"] = float(band.get("sidechain-lookahead", 0.0))
        control[f"scp_{i}"] = db_to_lin(band.get("sidechain-preamp", 0.0))
        # Boost cluster: LSP's defaults keep the below-threshold boost
        # primed (bth −72 dB, bsa +6 dB) behind cm alone — the EE side pins
        # all three per design-notes "MBC upward compression" (e454711), so
        # the conf must too rather than ride LV2 defaults.
        control[f"cm_{i}"] = _enum(
            EE_MBC_CM, band.get("compression-mode", "Downward"), 0,
            warns, f"{name}: compression-mode")
        # Missing-key fallbacks mirror the LSP/EE defaults (−72 dB / +6 dB),
        # not the generator's pinned −60/0 — a hand-edited preset that
        # omits the keys must render as EE would, not as our generator
        # happens to write them.
        control[f"bth_{i}"] = db_to_lin(band.get("boost-threshold", -72.0))
        control[f"bsa_{i}"] = db_to_lin(band.get("boost-amount", 6.0))
        # Custom sidechain filters: carry the toggles together with their
        # band-edge frequencies so the gate and its operands stay paired.
        control[f"sclc_{i}"] = 1 if band.get(
            "sidechain-custom-lowcut-filter", False) else 0
        control[f"schc_{i}"] = 1 if band.get(
            "sidechain-custom-highcut-filter", False) else 0
        control[f"sclf_{i}"] = float(
            band.get("sidechain-lowcut-frequency", 10.0))
        control[f"schf_{i}"] = float(
            band.get("sidechain-highcut-frequency", 20000.0))

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
        warnings=warns,
    )


def emit_limiter(plugin: dict, name: str = "limiter") -> Stage | None:
    """LSP limiter_stereo node."""
    # Defensive — build_chain handles bypass; kept for direct unit-test calls.
    if plugin.get("bypass", False):
        return None

    # No `bypass` control on LSP limiter_stereo (limiter.cpp:150-188).
    warns: list[str] = []
    control = {
        "mode": _enum(EE_LIMITER_MODE, plugin.get("mode", "Herm Thin"), 0,
                      warns, f"{name}: mode"),
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
        warnings=warns,
    )


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# EE's `maximum-history` (libebur128 integration window, seconds) has no
# 1:1 LSP port — autogain_stereo's loudness periods cap at 2 s. We instead
# steer the gain-ride *time-constants* (tgrow_l/tfall_l, 10..10000 ms) so a
# longer EE history yields a slower, gentler ride (monotonic). The two
# directions are asymmetric, matching EE's measured behaviour: it attenuates
# loud content quickly but boosts quiet content very slowly (anti-pumping).
# So `fall` (gain down toward target) is mapped fast and `grow` (gain up) slow.
# These scales are the load-bearing hypothesis tuned by the on-device EE-vs-PW
# comparison (docs/design-notes.md, autogain entry): at history=20 s, FALL→4 s
# matched EE's attenuation to 0.2 dB; GROW is pushed to the 10 s port ceiling
# to approach EE's much slower (~50 s effective) boost as closely as the port
# allows.
AUTOGAIN_FALL_MS_PER_S = 200.0
AUTOGAIN_GROW_MS_PER_S = 500.0
# The history the scales above were fitted and on-device-validated at.
# 20 s is the SoundWire value at volume-leveler-amount 5; the generator's
# SoundWire mapping is max(40 − amount·4, 15) s, so amounts > 5 land below
# it too, as does HDA with --enable autogain (max(30 − amount·5, 10) s).
# Shorter windows extrapolate; emit_autogain warns rather than claiming
# equivalence.
AUTOGAIN_VALIDATED_HISTORY_S = 20.0


def emit_autogain(plugin: dict, name: str = "autogain") -> Stage | None:
    """LSP autogain_stereo node — translates EE's autogain (volume leveler).

    EE's autogain is native libebur128 (EBU R 128, K-weighted). autogain_stereo
    is the LV2 equivalent: a K-weighted loudness AGC. The EE block is derived
    from Dolby's volume leveler and is emitted active on SoundWire devices
    and on HDA presets generated with --enable autogain (bypassed-by-default
    on HDA, where build_chain skips it).

    Port-unit note: `level`/`silence` are dB-domain (LUFS / dBFS) values passed
    **directly** — NOT linear gains, so no db_to_lin (contrast emit_limiter's
    `th`/`g_*`). EE `input-gain`/`output-gain` are always 0.0 here and have no
    main-path port on autogain_stereo (`preamp` is sidechain-only), so they are
    structurally identity and intentionally not written.
    """
    # Defensive — build_chain handles bypass; kept for direct unit-test calls.
    if plugin.get("bypass", False):
        return None

    history_s = float(plugin.get("maximum-history", 0.0))
    # The scales were fitted at a 20 s history. Shorter windows — HDA with
    # --enable autogain (10 s at volume-leveler-amount>=4) and SoundWire at
    # amount>5 (16 s and below) — extrapolate below the validated point:
    # the ride is then faster than at 20 s, and EE-vs-PW equivalence has
    # not been re-measured there. Recorded on the Stage.warnings channel
    # like every other emitter caveat so it reaches the conf header, not
    # just the terminal; the corpus tier's zero-warnings assertion exempts
    # this advisory by its "autogain: maximum-history" prefix — keep the
    # prefix stable.
    warns: list[str] = []
    if history_s < AUTOGAIN_VALIDATED_HISTORY_S:
        warns.append(
            f"autogain: maximum-history {history_s:g} s is below the "
            f"{AUTOGAIN_VALIDATED_HISTORY_S:g} s at which the gain-ride "
            "mapping was validated; the PipeWire leveler may ride faster "
            "than EasyEffects' on this preset. Compare with "
            "tools/measure_pw/ before relying on equivalence.")
    grow_ms = _clamp(history_s * AUTOGAIN_GROW_MS_PER_S, 10.0, 10000.0)
    fall_ms = _clamp(history_s * AUTOGAIN_FALL_MS_PER_S, 10.0, 10000.0)
    control = {
        # Desired loudness (LUFS), -60..0.
        "level": _clamp(float(plugin.get("target", -23.0)), -60.0, 0.0),
        # Silence gate (dBFS), -84..-36.
        "silence": _clamp(
            float(plugin.get("silence-threshold", -72.0)), -84.0, -36.0),
        # K-weighting (5) == EBU R 128, matching EE's libebur128 metering.
        "weight": 5,
        # Zero added latency: lookahead is the only latency source (port 41).
        "lkahead": 0.0,
        # Asymmetric long-window ride from EE maximum-history: slow boost
        # (grow), faster attenuation (fall) — see the constants above.
        "tgrow_l": grow_ms,
        "tfall_l": fall_ms,
    }
    node = {
        "type": "lv2",
        "name": name,
        "plugin": LSP_AUTOGAIN_URI,
        "control": control,
    }
    return Stage(
        nodes=[node],
        in_l=(name, "in_l"), in_r=(name, "in_r"),
        out_l=(name, "out_l"), out_r=(name, "out_r"),
        warnings=warns,
    )


def emit_bass_enhancer(plugin: dict, name: str = "bass") -> Stage | None:
    """Calf BassEnhancer node.

    EE's bass_enhancer wraps Calf BassEnhancer LV2; mapping verified
    against bass_enhancer.cpp:68-74 (BIND_LV2_PORT calls). Note `amount`
    is dB in the EE preset, linear in the Calf port (BIND_LV2_PORT_DB
    converts via util::db_to_linear). `harmonics`, `scope`, `floor` and
    `blend` are direct.
    """
    if plugin.get("bypass", False):
        return None
    control = {
        "level_in":     db_to_lin(plugin.get("input-gain", 0.0)),
        "level_out":    db_to_lin(plugin.get("output-gain", 0.0)),
        "amount":       db_to_lin(plugin.get("amount", 0.0)),
        "drive":        float(plugin.get("harmonics", 8.5)),
        "freq":         float(plugin.get("scope", 100.0)),
        "blend":        float(plugin.get("blend", 0.0)),
        "floor":        float(plugin.get("floor", 20.0)),
        "floor_active": 1 if plugin.get("floor-active", False) else 0,
        "listen":       1 if plugin.get("listen", False) else 0,
    }
    node = {
        "type": "lv2",
        "name": name,
        "plugin": CALF_BE_URI,
        "control": control,
    }
    return Stage(
        nodes=[node],
        in_l=(name, "in_l"), in_r=(name, "in_r"),
        out_l=(name, "out_l"), out_r=(name, "out_r"),
    )


def emit_stereo_tools(plugin: dict, name: str = "stereo") -> Stage | None:
    """Calf StereoTools node.

    Mapping verified against stereo_tools.cpp:65-80 — `slev` and `mlev`
    are the only ports that go through BIND_LV2_PORT_DB (dB → linear);
    every other named symbol is a direct linear/bool/enum bind. The
    `mode` enum string-label → integer table is `EE_ST_MODE`.
    """
    if plugin.get("bypass", False):
        return None
    warns: list[str] = []
    control = {
        "level_in":      db_to_lin(plugin.get("input-gain", 0.0)),
        "level_out":     db_to_lin(plugin.get("output-gain", 0.0)),
        "balance_in":    float(plugin.get("balance-in", 0.0)),
        "balance_out":   float(plugin.get("balance-out", 0.0)),
        "softclip":      1 if plugin.get("softclip", False) else 0,
        "mutel":         1 if plugin.get("mutel", False) else 0,
        "muter":         1 if plugin.get("muter", False) else 0,
        "phasel":        1 if plugin.get("phasel", False) else 0,
        "phaser":        1 if plugin.get("phaser", False) else 0,
        "mode":          _enum(
            EE_ST_MODE, plugin.get("mode", "LR > LR (Stereo Default)"), 0,
            warns, f"{name}: mode"),
        "slev":          db_to_lin(plugin.get("side-level", 0.0)),
        "sbal":          float(plugin.get("side-balance", 0.0)),
        "mlev":          db_to_lin(plugin.get("middle-level", 0.0)),
        "mpan":          float(plugin.get("middle-panorama", 0.0)),
        "stereo_base":   float(plugin.get("stereo-base", 0.0)),
        "delay":         float(plugin.get("delay", 0.0)),
        "sc_level":      float(plugin.get("sc-level", 1.0)),
        "stereo_phase":  float(plugin.get("stereo-phase", 0.0)),
    }
    node = {
        "type": "lv2",
        "name": name,
        "plugin": CALF_ST_URI,
        "control": control,
    }
    return Stage(
        nodes=[node],
        in_l=(name, "in_l"), in_r=(name, "in_r"),
        out_l=(name, "out_l"), out_r=(name, "out_r"),
        warnings=warns,
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

@dataclass
class PluginHandler:
    """Dispatch entry for one EE plugin key.

    `emitter=None` marks a key with no translation: it is skipped with
    `skip_warning`. `silent_if_bypassed=True` suppresses the bypass-skip
    warning when the source plugin is bypassed — used for autogain, whose
    bypassed state is the HDA default (the user shouldn't be nagged); when
    active (SoundWire, or --enable autogain) it is translated normally.
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
    "bass_enhancer#0":        PluginHandler(emit_bass_enhancer, ("bass",)),
    "stereo_tools#0":         PluginHandler(emit_stereo_tools, ("stereo",)),
    # EE's autogain (native libebur128 volume leveler) → LSP autogain_stereo,
    # a K-weighted loudness AGC (see emit_autogain). silent_if_bypassed keeps
    # the bypass-skip quiet on HDA, where bypass is the expected default;
    # active blocks (SoundWire, or --enable autogain) get translated rather
    # than dropped.
    "autogain#0": PluginHandler(emit_autogain, silent_if_bypassed=True),
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
            if handler.skip_warning is None:
                raise RuntimeError(
                    f"{key}: dispatch entry has neither emitter nor "
                    "skip_warning")
            warn(handler.skip_warning)
            continue

        if bypassed:
            # Surface bypass-skip uniformly so users notice if their EE
            # bypassed-plugin choice silently disappeared — unless
            # silent_if_bypassed marks bypass as the expected default
            # (autogain on HDA), where nagging would be noise.
            if not handler.silent_if_bypassed:
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

    # The loop above only visits plugins_order — a plugin object that never
    # made it into the order (hand-edited preset) would vanish without this.
    orphaned = [k for k, v in out.items()
                if k not in ("blocklist", "plugins_order")
                and isinstance(v, dict) and k not in plugins_order]
    for key in sorted(orphaned):
        warn(f"{key}: present in preset output but missing from "
             "plugins_order; not emitted.")

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
        f"{CONF_HEADER_MARK} — see\n"
        "# https://github.com/antoinecellerier/speaker-tuning-to-easyeffects\n"
        f"# version: {get_version()}\n"
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

    Uses ``dolby_to_easyeffects.select_speaker_sinks()`` (same probe as the
    EasyEffects autoload pathway): ``pw-dump`` filtered to ``Audio/Sink``
    nodes, preferring those tagged ``device.icon_name == audio-speakers`` (the
    "strict" tier, which excludes HDMI / Bluetooth / headsets). When nothing is
    tagged as a speaker — e.g. a laptop whose UCM2 profile omits the speaker
    icon (issue #18) — it falls back to a "relaxed" tier of internal analog
    sinks; a single relaxed candidate is used, with a warning. Returns
    ``(None, [reasons])`` when no unique sink can be chosen — the caller
    surfaces that, falls back to the v1 virtual-sink conf, and asks the user to
    pass ``--target-sink``.
    """
    try:
        from dolby_to_easyeffects import select_speaker_sinks, _sink_diag_line
    except Exception as e:  # pragma: no cover — defensive
        return None, [f"could not import speaker probe: {e}"]

    # Terse, description-less form of the shared diagnostic (stderr warnings).
    def _diag(sink: dict) -> str:
        return _sink_diag_line(sink, with_description=False)

    sel = select_speaker_sinks()
    tier, selected, all_sinks = sel["tier"], sel["selected"], sel["all_sinks"]

    if tier == "strict":
        if len(selected) == 1:
            return selected[0]["name"], []
        return None, [
            f"multiple speaker sinks found ({len(selected)}): "
            + ", ".join(s["name"] for s in selected)
            + "; pass --target-sink to pick one"
        ]

    if tier == "relaxed":
        if len(selected) == 1:
            sink = selected[0]
            return sink["name"], [
                "no sink tagged device.icon_name=audio-speakers; using the "
                f"only internal analog sink {_diag(sink)}; pass --target-sink "
                "to override"
            ]
        return None, [
            f"multiple internal analog sinks found ({len(selected)}): "
            + ", ".join(_diag(s) for s in selected)
            + "; pass --target-sink to pick one"
        ]

    # tier == "none"
    if not all_sinks:
        return None, [
            "no Audio/Sink nodes found via pw-dump (no PipeWire daemon running?)"
        ]
    return None, [
        "no internal-speaker sink found (none tagged "
        "device.icon_name=audio-speakers, and no internal analog output); "
        "sinks seen: " + ", ".join(_diag(s) for s in all_sinks)
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


def _print_results(conf_path: Path, irs_path: Path | None,
                   *, dry_run: bool) -> None:
    """Report where the conf (and copied IRS) landed — or, under --dry-run,
    where they *would* land. Always to stderr, so the dry-run conf on stdout
    stays clean."""
    # "impulse response (.irs)", not the bare acronym: it is never expanded
    # anywhere else a user reads (round 5).
    if dry_run:
        cprint("ok", f"Would write conf: {conf_path}")
        if irs_path is not None:
            cprint("ok", f"Would copy impulse response (.irs): {irs_path}")
    else:
        cprint("ok", f"Wrote conf: {conf_path}")
        if irs_path is not None:
            cprint("ok", f"Copied impulse response (.irs): {irs_path}")


def warn_if_stacked(output_path: Path, target_sink: str | None) -> None:
    """Warn when the conf just written joins another aimed at the same sink.

    Trying a second voicing or profile is the obvious next thing to do — the
    wrapper's own output suggests it — and nothing stopped the two coexisting:
    --force guards one output path, so a differently-named conf lands beside
    the first with no collision. WirePlumber then runs both in series rather
    than offering a choice, and neither the run nor the audio says so. Caught
    here, at the moment it happens, rather than left for --doctor to find
    after someone notices the sound is wrong.
    """
    if not target_sink:
        return   # not a smart filter; nothing chains
    others = [c.path.name for c in installed_confs(output_path.parent)
              if c.smart and c.target == target_sink
              and c.path.resolve() != output_path.resolve()]
    if not others:
        return
    cprint("warn", "")
    cprint("warn", "⚠  Another filter chain is already attached to the same "
                   "speakers:")
    for name in others:
        cprint("warn", f"     {name}")
    cprint("dim", "   PipeWire runs them one after another, not as "
                  "alternatives — every")
    cprint("dim", "   stage applies twice. If you were trying a different "
                  "voicing or profile,")
    cprint("dim", "   delete the one you don't want (and its .irs), then "
                  "restart PipeWire.")
    cprint("dim", "   --doctor lists what is installed and what it does.")


def _print_next_steps(node_name: str,
                      target_object: str | None = None) -> None:
    """The actions to take after a real (non-dry-run) write."""
    cprint("head", "Next steps:")
    cprint("cta", "  1. Restart PipeWire:        "
                  "systemctl --user restart pipewire pipewire-pulse")
    # Stays a numbered step here, unlike the wrapper's footnote: this script
    # converts an existing EasyEffects preset, so its caller almost certainly
    # runs EasyEffects. "Stop it starting again" because quitting the window
    # ends double-processing for this session only — the background service
    # and autostart entry bring it back at the next login.
    cprint("cta", "  2. Avoid double-processing: quit EasyEffects and stop "
                  "it starting again (its Background Service and autostart, "
                  "or remove its autoload for this device)")
    cprint("cta", "  3. Verify the sink:         pw-cli ls Node | grep "
                  f"{_sanitize_name(node_name)}")
    # What success looks like (round 6): with no expected output stated, an
    # empty grep couldn't be told apart from "this step doesn't matter".
    # Names the usual cause of an empty grep rather than blaming the restart:
    # a missing LSP/Calf plugin makes module-filter-chain drop the whole
    # conf, so no node appears and re-restarting never helps.
    cprint("dim", "     (it should print a line, showing node.name = \"...\"; "
                  "nothing usually means the LSP or Calf LV2 plugins are "
                  "missing, so the whole file failed to load)")
    if target_object:
        cprint("cta", "  4. Verify routing:          "
                      f"pw-link -l | grep {target_object}")


def _make_adder(container, only):
    """Shared-group plumbing: an ``add_argument`` wrapper that skips flags not
    selected by ``only`` (keyed by primary option string) and records the
    added actions so dolby_to_pipewire.py can rebuild a child argv from them.
    Deliberate double of dolby_to_easyeffects._make_adder — importing it here
    would drag that script's NumPy/SciPy into this one's dependency-free path.
    """
    added = []

    def add(*names, **kwargs):
        if only is None or names[0] in only:
            added.append(container.add_argument(*names, **kwargs))

    return add, added


# --- PipeWire-side diagnostics (--doctor) -----------------------------------
#
# The EasyEffects doctor checks the environment a preset lands in. This is the
# same idea for the PipeWire path, where there is more to get wrong and less
# to see: the chain lives in conf files nobody reads, nothing reports its own
# state, and the remedy is almost always "delete a file and restart PipeWire",
# which nobody would guess.
#
# Probing and judging are kept apart — every check below is a pure function
# over already-gathered data, so the states this machine can't produce (an old
# WirePlumber, a missing plugin, a target sink that vanished) are still
# testable.

# Both directories a conf can end up in. Only the first is loaded: the stock
# pipewire.conf auto-includes pipewire.conf.d/, while filter-chain.conf.d/ is
# for the standalone `pipewire -c filter-chain.conf` invocation and is never
# scanned by the running daemon (see DEFAULT_OUTPUT_DIR).
_UNSCANNED_CONF_DIR = Path.home() / ".config/pipewire/filter-chain.conf.d"

# effect_input.X and effect_output.X are the two halves of chain X. In
# smart-filter mode node.link-group joins them; the v1 virtual-sink conf sets
# no link group, so the name is the only thing that does.
_CHAIN_NODE_RE = re.compile(r"^effect_(input|output)\.(.+)$")


@dataclass
class InstalledConf:
    """A filter-chain conf found on disk, as far as we could read it."""
    path: Path
    version: str = ""        # from the "# version:" header
    node_name: str = ""      # capture node.name
    smart: bool = False
    target: str = ""         # filter.smart.target node.name
    pinned: str = ""         # playback target.object
    irs: list = field(default_factory=list)
    readable: bool = True    # False when spa-json-dump couldn't parse it


@dataclass
class LiveChain:
    """A filter chain as the running graph reports it."""
    name: str                # the X in effect_input.X
    smart: bool = False
    target: str = ""
    pinned: str = ""


def _pw_dump() -> list | None:
    """Every PipeWire object, or None when the daemon can't be reached.

    The doctor's single ``pw-dump`` boundary — tests monkeypatch it to feed
    synthetic graphs. Deliberately not dolby_to_easyeffects._enumerate_audio_sinks:
    that reduces to Audio/Sink nodes with a fixed field set, and these checks
    need filter.smart*, node.link-group and target.object on every node.
    """
    try:
        result = subprocess.run(["pw-dump"], capture_output=True, text=True,
                                timeout=5)
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, list) else None


def _wireplumber_version() -> tuple[int, ...] | None:
    """(major, minor) of the running WirePlumber, or None if it won't say."""
    try:
        out = subprocess.run(["wireplumber", "--version"], capture_output=True,
                             text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    m = re.search(r"(\d+)\.(\d+)", out or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_conf(path: Path) -> InstalledConf:
    """Read back one of our confs. Values come from spa-json-dump rather than
    a hand-rolled parser (CLAUDE.md: wrap the existing tool); without it the
    conf is marked unreadable and the checks that need its contents say so
    instead of guessing."""
    conf = InstalledConf(path=path)
    try:
        head = path.read_text(errors="replace")
    except OSError:
        conf.readable = False
        return conf
    m = re.search(r"^# version:\s*(\S+)", head, re.MULTILINE)
    conf.version = m.group(1) if m else ""
    if shutil.which("spa-json-dump") is None:
        conf.readable = False
        return conf
    try:
        dumped = subprocess.run(["spa-json-dump", str(path)],
                                capture_output=True, text=True, timeout=10)
        data = json.loads(dumped.stdout)
        args = data["context.modules"][0]["args"]
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError,
            KeyError, IndexError, TypeError):
        conf.readable = False
        return conf
    capture = args.get("capture.props", {})
    playback = args.get("playback.props", {})
    conf.node_name = capture.get("node.name", "")
    conf.smart = bool(capture.get("filter.smart"))
    conf.target = (capture.get("filter.smart.target") or {}).get("node.name", "")
    conf.pinned = playback.get("target.object", "")
    # Deduplicated: the stereo convolver is two nodes reading the same file,
    # so a missing IRS would otherwise be reported once per channel.
    conf.irs = list(dict.fromkeys(
        Path(n["config"]["filename"])
        for n in args.get("filter.graph", {}).get("nodes", [])
        if isinstance(n, dict) and "filename" in n.get("config", {})))
    return conf


def installed_confs(*dirs: Path) -> list[InstalledConf]:
    """Every conf this tool wrote, across the directories it might be in."""
    found = []
    for d in dirs:
        try:
            paths = sorted(d.glob("*.conf"))
        except OSError:
            continue
        for p in paths:
            try:
                if not p.read_text(errors="replace").startswith(CONF_HEADER_MARK):
                    continue
            except OSError:
                continue
            found.append(parse_conf(p))
    return found


def _target_node_name(target) -> str:
    """The node.name out of a filter.smart.target, whichever shape it arrives in.

    The conf declares it as a SPA-JSON object, and spa-json-dump hands it back
    as a dict — but pw-dump reports the *property* verbatim, so from the live
    graph the same value is the string `{ node.name = "..." }`. Taking that
    string as the name made every live smart filter look like it pointed at a
    sink that doesn't exist.
    """
    if isinstance(target, dict):
        return target.get("node.name", "")
    if isinstance(target, str):
        m = re.search(r'node\.name\s*=\s*"([^"]+)"', target)
        return m.group(1) if m else target.strip()
    return ""


def live_chains(dump) -> list[LiveChain]:
    """The filter chains present in a pw-dump, joined across their two nodes."""
    chains: dict[str, LiveChain] = {}
    for obj in dump or []:
        if not str(obj.get("type", "")).endswith("Node"):
            continue
        props = obj.get("info", {}).get("props", {})
        m = _CHAIN_NODE_RE.match(str(props.get("node.name", "")))
        if not m:
            continue
        half, name = m.group(1), m.group(2)
        chain = chains.setdefault(name, LiveChain(name=name))
        if half == "input":
            chain.smart = bool(props.get("filter.smart"))
            chain.target = _target_node_name(props.get("filter.smart.target"))
        else:
            chain.pinned = props.get("target.object", "") or ""
    return list(chains.values())


def sink_names(dump) -> set[str]:
    """node.name of every Audio/Sink in a pw-dump."""
    names = set()
    for obj in dump or []:
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") == "Audio/Sink":
            names.add(props.get("node.name", ""))
    return names - {""}


def _conf_for(chain_name: str, confs) -> str:
    """The conf file a live chain came from — the remedy is deleting one, and
    the file name is the part a reader can't derive from a node name. The
    basename only: check detail is wrapped, and a folded absolute path is
    worse than useless."""
    for c in confs:
        if c.node_name == f"effect_input.{chain_name}":
            return c.path.name
    return f"{chain_name}.conf"


def check_stacked_chains(chains, confs) -> CheckResult | None:
    """Chains sharing a filter.smart.target run in SERIES, not as alternatives.

    Measured on WirePlumber 0.5.15: get_filter_from_target returns the first
    filter matching a target and get_filter_target "the next filter with
    matching target", so two installed confs put both chains in the path —
    every stage twice, convolver included. The tool no longer creates this
    state, but a machine that reached it before the fix stays broken silently.
    """
    by_target: dict[str, list[str]] = {}
    for c in chains:
        if c.smart and c.target:
            by_target.setdefault(c.target, []).append(c.name)
    stacked = {t: n for t, n in by_target.items() if len(n) > 1}
    if not stacked:
        return None
    names = sorted(n for group in stacked.values() for n in group)
    files = ", ".join(_conf_for(n, confs) for n in names)
    return CheckResult(
        DOCTOR_FAIL, "Stacked filter chains",
        f"{len(names)} chains ({', '.join(names)}) attach to the same sink, so "
        "PipeWire runs them one after another instead of offering a choice — "
        f"every stage is applied that many times over. Keep one of {files} and "
        "delete the others, then restart PipeWire; the full paths are in the "
        "block below.")


def check_unpinned_siblings(chains) -> CheckResult | None:
    """Several virtual sinks with no playback target of their own.

    They follow the *default* sink, so selecting one of them in sound settings
    makes the others follow it and chain into it (measured: Balanced → Warm).
    One on its own is fine — there is nothing for it to chain into.
    """
    loose = [c.name for c in chains if not c.smart and not c.pinned]
    if len(loose) < 2:
        return None
    return CheckResult(
        DOCTOR_WARN, "Unpinned virtual sinks",
        f"{len(loose)} chains ({', '.join(sorted(loose))}) have no playback "
        "target of their own, so they follow whichever sink is default. Pick "
        "one of them as your output and the others feed into it. Re-run the "
        "converter with --target-object <your speaker sink> to pin them.")


def check_confs_loaded(confs, chains, dump) -> CheckResult | None:
    """A conf on disk with no node in the graph.

    module-filter-chain drops the *whole file* when a plugin it names is
    missing, so one absent LSP or Calf package silently costs the entire
    chain. Nothing else reports this outside the seconds after an activation.
    """
    if not confs:
        return None
    if dump is None:
        return CheckResult(DOCTOR_UNKNOWN, "Chains loaded",
                           "pw-dump didn't answer, so whether the confs "
                           "actually loaded couldn't be checked.")
    live = {f"effect_input.{c.name}" for c in chains}
    missing = [c for c in confs if c.node_name and c.node_name not in live]
    if not missing:
        return CheckResult(DOCTOR_PASS, "Chains loaded",
                           f"{len(confs)} conf(s), all present in the graph.")
    return CheckResult(
        DOCTOR_FAIL, "Chains loaded",
        f"{len(missing)} of {len(confs)} conf(s) are on disk but absent from "
        f"the graph ({', '.join(str(c.path.name) for c in missing)}). Usually "
        "an LSP or Calf LV2 plugin is missing, which makes PipeWire drop the "
        "whole file — see the README's plugin dependencies — or PipeWire "
        "hasn't been restarted since the file was written.")


def check_irs_present(confs) -> CheckResult | None:
    """An impulse response a conf names but that isn't on disk.

    The convolver then loads nothing and the speaker correction — the part
    that makes this device-specific at all — is simply absent, with the rest
    of the chain still running so it doesn't sound broken enough to notice.
    """
    missing = {irs.name: c.path.name for c in confs for irs in c.irs
               if not irs.exists()}
    if not missing:
        return None
    shown = ", ".join(sorted(missing)[:2])
    more = f" (+{len(missing) - 2} more)" if len(missing) > 2 else ""
    return CheckResult(
        DOCTOR_FAIL, "Impulse response missing",
        f"{len(missing)} impulse file(s) named by a conf aren't there: "
        f"{shown}{more}. The speaker correction is silently doing nothing — "
        "the rest of the chain still runs, so it won't sound broken enough to "
        "notice. Re-run the converter to copy it back beside the conf.")


def check_targets_exist(chains, sinks, dump) -> CheckResult | None:
    """A smart filter aimed at a sink that no longer exists.

    Nothing errors: WirePlumber simply never inserts the chain, so audio plays
    untreated and the conf looks fine. Sink names change when a card's UCM
    profile changes, or when a conf is copied from another machine.
    """
    if dump is None or not chains:
        return None
    orphans = sorted({c.target for c in chains
                      if c.smart and c.target and c.target not in sinks})
    if not orphans:
        return None
    return CheckResult(
        DOCTOR_FAIL, "Target sink missing",
        f"the chain is attached to {', '.join(orphans)}, which isn't among "
        "this machine's sinks, so it never joins the audio path. Re-run the "
        "converter to pick up the current speaker sink, or pass "
        "--target-sink with the right node.name.")


def check_conf_directory() -> CheckResult | None:
    """Confs in filter-chain.conf.d/, which the running daemon never reads."""
    try:
        strays = [p for p in sorted(_UNSCANNED_CONF_DIR.glob("*.conf"))
                  if p.read_text(errors="replace").startswith(CONF_HEADER_MARK)]
    except OSError:
        return None
    if not strays:
        return None
    return CheckResult(
        DOCTOR_WARN, "Conf in an unread directory",
        f"{len(strays)} conf(s) are in {_tilde(_UNSCANNED_CONF_DIR)}, which "
        "PipeWire's stock config does not load — only pipewire.conf.d/ is "
        f"auto-included. Move them to {_tilde(DEFAULT_OUTPUT_DIR)} and "
        "restart PipeWire.")


def check_wireplumber(version) -> CheckResult:
    """Smart-filter routing needs WirePlumber 0.5+."""
    if version is None:
        return CheckResult(DOCTOR_UNKNOWN, "WirePlumber",
                           "`wireplumber --version` didn't answer, so its "
                           "version wasn't checked.")
    vstr = ".".join(str(v) for v in version)
    if version < (0, 5):
        return CheckResult(
            DOCTOR_FAIL, "WirePlumber",
            f"{vstr} has no smart-filter support, so a chain written the "
            "default way never attaches to your speakers. Upgrade, or re-run "
            "the converter with --target-sink '' for a plain virtual sink you "
            "select as your output.")
    return CheckResult(DOCTOR_PASS, "WirePlumber",
                       f"{vstr} — smart-filter routing supported.")


def check_easyeffects_conflict(sinks, chains, dump) -> CheckResult | None:
    """EasyEffects processing the same audio as the chain.

    Both apply their own convolver, compressor and limiter, so the result is
    neither tuning. This is a conflict check, not a report on an EasyEffects
    install — on this path EasyEffects is only ever an intermediate format.

    Silent when no chain of ours is live: EasyEffects on its own is not a
    conflict, and saying "processed twice" when there is nothing to process it
    a second time would be plainly false.
    """
    if dump is None or "easyeffects_sink" not in sinks or not chains:
        return None
    return CheckResult(
        DOCTOR_WARN, "EasyEffects also running",
        "EasyEffects is running, and anything routed through it is processed "
        "twice — once by its own chain, once by this one. Quit it and stop it "
        "starting again (its Background Service and autostart, or remove its "
        "autoload).")


def check_conf_versions(confs, running: str) -> CheckResult | None:
    """A conf written by a different build than the one being run."""
    stale = sorted({c.version for c in confs if c.version and c.version != running})
    if not stale:
        return None
    return CheckResult(
        DOCTOR_WARN, "Conf from another version",
        f"the installed conf(s) were written by {', '.join(stale)} and this "
        f"is {running}. If a fix since then was meant to reach your audio, "
        "re-run the converter — a conf is a snapshot, it doesn't update "
        "itself.")


def gather_pw_doctor() -> tuple[list, list[InstalledConf], list[LiveChain], dict]:
    """Probe everything once, then judge. Returns (checks, confs, chains, facts)."""
    dump = _pw_dump()
    chains = live_chains(dump)
    sinks = sink_names(dump)
    # Only the directory the daemon reads. A conf in _UNSCANNED_CONF_DIR is
    # not installed in any meaningful sense — counting it inflated "Confs: N"
    # and, because it shares a node name with the real one, let "Chains
    # loaded" pass for a file that had loaded for nobody.
    confs = installed_confs(DEFAULT_OUTPUT_DIR.expanduser())
    running = get_version()

    checks = [c for c in (
        check_stacked_chains(chains, confs),
        check_unpinned_siblings(chains),
        check_confs_loaded(confs, chains, dump),
        check_irs_present(confs),
        check_targets_exist(chains, sinks, dump),
        check_conf_directory(),
        check_wireplumber(_wireplumber_version()),
        check_easyeffects_conflict(sinks, chains, dump),
        check_conf_versions(confs, running),
    ) if c is not None]

    if not confs:
        checks.insert(0, CheckResult(
            DOCTOR_WARN, "Installed confs",
            f"no filter-chain conf from this tool in "
            f"{_tilde(DEFAULT_OUTPUT_DIR)} — run dolby_to_pipewire.py on your "
            "tuning XML first."))
    if dump is None:
        checks.append(CheckResult(
            DOCTOR_UNKNOWN, "PipeWire",
            "pw-dump didn't answer — is the PipeWire daemon running? Most of "
            "the checks above need the live graph."))

    facts = {
        "confs": confs,
        "chains": chains,
        "sinks": sorted(sinks),
        "wireplumber": _wireplumber_version(),
        "version": running,
    }
    return checks, confs, chains, facts


def _plugin_presence() -> list[str]:
    """Which LV2 packages the chain needs are installed — the usual reason a
    conf loads nothing. Reported as facts, not judged: lv2info is the only
    way to ask, and it isn't always installed."""
    if shutil.which("lv2info") is None:
        return ["lv2info not installed — LV2 plugin presence unknown"]
    out = []
    for label, uri in (("LSP PEQ", LSP_PEQ_URI), ("LSP MBC", LSP_MBC_URI),
                       ("LSP limiter", LSP_LIM_URI),
                       ("Calf bass enhancer", CALF_BE_URI)):
        try:
            rc = subprocess.run(["lv2info", uri], capture_output=True,
                                text=True, timeout=10).returncode
        except (subprocess.SubprocessError, OSError):
            rc = 1
        out.append(f"{label}: {'present' if rc == 0 else 'MISSING'}")
    return out


def _tilde(path) -> str:
    """Render a path with $HOME collapsed to ~ — paste-safe (no username)."""
    s = str(path)
    home = str(Path.home())
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


def report_pw_doctor() -> int:
    """Print the PipeWire-side diagnostic report. Returns a process exit code."""
    with _report_on_stdout():
        return _report_pw_doctor()


def _report_pw_doctor() -> int:
    checks, confs, chains, facts = gather_pw_doctor()

    # The project name, not this module's: dolby_to_pipewire.py --doctor runs
    # the same report, and a header naming the other script reads as a
    # mis-invocation.
    cprint("head", f"speaker-tuning-to-easyeffects {facts['version']}")
    cprint("head", "=== PipeWire filter-chain doctor ===")
    print()
    for c in checks:
        _doctor.emit_check(c, cprint)
    print()
    _doctor.print_summary(checks, cprint)
    print()

    # Raw probed facts, always shown: a verdict can be wrong or UNKNOWN and
    # the report still has to be diagnosable by someone reading it remotely.
    cprint("head", "=== Environment (paste this into your issue) ===")
    wp = facts["wireplumber"]
    print(f"  Tool:         speaker-tuning-to-easyeffects {facts['version']}"
          " (PipeWire path)")
    print(f"  WirePlumber:  {'.'.join(map(str, wp)) if wp else 'unknown'}")
    print(f"  Confs:        {len(confs)} in {_tilde(DEFAULT_OUTPUT_DIR)}")
    for c in confs:
        state = "unreadable" if not c.readable else (
            f"smart→{c.target}" if c.smart
            else (f"pinned→{c.pinned}" if c.pinned else "virtual sink, unpinned"))
        print(f"                {_tilde(c.path)} [{c.version or '?'}] {state}")
    print(f"  Live chains:  {len(chains)}"
          + (": " + ", ".join(sorted(c.name for c in chains)) if chains else ""))
    print(f"  Sinks:        {len(facts['sinks'])}")
    for s in facts["sinks"]:
        print(f"                {s}")
    for line in _plugin_presence():
        print(f"  {line}")
    print()

    _doctor.print_verdict(checks, cprint)
    print()

    # Removing a conf and restarting is the answer to most of the above, and
    # it is the one step a reader can't derive from a diagnosis.
    cprint("dim", "To remove a chain: delete its .conf (and matching .irs), "
                  "then restart PipeWire:")
    cprint("cta", f"  systemctl --user restart pipewire pipewire-pulse")
    print()

    # Hardware sits under the whole chain, so the same questions apply here as
    # on the EasyEffects path. Imported lazily: this pulls the generator's
    # NumPy/SciPy (~0.4s), which a conversion run must not pay for.
    try:
        import dolby_to_easyeffects as gen
    except Exception as e:  # pragma: no cover — defensive
        cprint("dim", f"(hardware report unavailable: {e})")
        return 0
    info = gen._gather_speaker_info()
    gen._print_speaker_info(info)
    return 0


def add_routing_args(container, *, only=None):
    """Routing flags (dolby_to_pipewire.py shares --target-sink)."""
    add, added = _make_adder(container, only)
    add(
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
    add(
        "--target-object",
        default=None,
        help="bind the chain's playback to a specific downstream node "
             "(node.name) instead of letting WirePlumber choose. Useful "
             "for routing into a measurement null sink. End users "
             "usually want --target-sink instead, which is set by "
             "default and uses WirePlumber 0.5+ smart-filter routing "
             "so apps don't see the chain as a separate sink.",
    )
    return added


def add_output_args(container, *, only=None):
    """Output naming/location flags (dolby_to_pipewire.py shares --force)."""
    add, added = _make_adder(container, only)
    add(
        "--output",
        type=Path,
        default=None,
        help=f"output .conf path (default: "
             f"{DEFAULT_OUTPUT_DIR}/<node-name>.conf)",
    )
    add(
        "--node-name",
        default=None,
        help=f"PipeWire node-name suffix; sanitised to [A-Za-z0-9_]. "
             f"Default: derived from the preset filename stem "
             f"(e.g. Dolby-Balanced.json → Dolby_Balanced), so "
             f"converting multiple presets produces distinct sink "
             f"names without collision. Falls back to "
             f"{DEFAULT_NODE_NAME!r} if the stem is empty after "
             f"sanitisation.",
    )
    add(
        "--node-description",
        default=None,
        help=f"human-readable node description. Default: derived "
             f"from the preset filename stem (e.g. \"Dolby-Balanced\"), "
             f"falling back to {DEFAULT_NODE_DESCRIPTION!r}.",
    )
    add(
        "--force",
        action="store_true",
        help="overwrite the output file if it already exists",
    )
    return added


def add_impulse_response_args(container, *, only=None):
    """Impulse-response flags — never shared with the wrapper (it stages the
    .irs in a tempdir and must keep the default copy-beside-conf behavior)."""
    add, added = _make_adder(container, only)
    add(
        "--irs-dir",
        type=Path,
        default=DEFAULT_IRS_DIR,
        help=f"directory containing the .irs file referenced by the "
             f"preset's convolver (default: {DEFAULT_IRS_DIR})",
    )
    add(
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
    return added


def add_general_args(container, *, only=None):
    """General flags (dolby_to_pipewire.py shares --no-validate)."""
    add, added = _make_adder(container, only)
    add(
        "--no-validate",
        action="store_true",
        help="skip the schema self-check against lv2info port metadata. "
             "By default, after generating the conf, ee_to_pipewire shells "
             "out to tools/measure_pw/validate_conf.py to catch unknown "
             "port symbols and out-of-range values; pass this flag to "
             "skip it (e.g. on systems without lv2info installed).",
    )
    add(
        "--dry-run",
        action="store_true",
        help="print the generated conf to stdout instead of writing it; "
             "missing IRS files become warnings rather than errors",
    )
    add(
        "--skip-next-steps",
        action="store_true",
        help="drop the post-write next-steps checklist (restart PipeWire, "
             "verify the sink, quit EasyEffects) — for callers that handle "
             "activation themselves. Standalone, a one-line activation "
             "pointer replaces it; dolby_to_pipewire.py passes this "
             "automatically and prints its own steps instead",
    )
    add(
        "--no-color",
        action="store_true",
        help="disable colored terminal output",
    )
    add(
        "--version",
        action="version",
        version=f"%(prog)s {get_version()}",
        help="show version and exit",
    )
    return added


def build_parser(argv: list[str] | None = None) -> argparse.ArgumentParser:
    # --no-color must be honored before argparse renders --help, so pre-scan
    # argv to pick the help formatter (color itself is disabled after parsing).
    _argv = sys.argv[1:] if argv is None else argv
    formatter_class = (argparse.HelpFormatter
                       if "--no-color" in _argv else _HelpFormatter)
    epilog = None
    if _MISSING_COLOR_DEPS:
        epilog = ("Tip: install " + " and ".join(_MISSING_COLOR_DEPS)
                  + " for colored output (see README for distro packages).")
    parser = argparse.ArgumentParser(
        description="Convert an EasyEffects output preset to a PipeWire "
                    "filter-chain .conf (see docs/ee-to-pipewire.md).",
        formatter_class=formatter_class,
        epilog=epilog,
    )
    parser.add_argument(
        "preset",
        type=Path,
        nargs="?",
        help="path to the EasyEffects preset JSON (the output of "
             "dolby_to_easyeffects.py, e.g. ~/.local/share/easyeffects/output/"
             "Dolby-Balanced.json)",
    )
    group = parser.add_argument_group("inspection")
    group.add_argument(
        "--doctor",
        action="store_true",
        help="report the state of the installed filter chain — stacked "
             "chains, confs that didn't load, a missing impulse response, a "
             "target sink that no longer exists — then exit. Takes no preset; "
             "it inspects what is already installed.",
    )
    add_routing_args(parser.add_argument_group("routing"))
    add_output_args(parser.add_argument_group("output"))
    add_impulse_response_args(parser.add_argument_group("impulse response"))
    add_general_args(parser.add_argument_group("general"))
    return parser


def _complete_sink_names(prefix: str, **_kwargs) -> list[str]:
    """Tab-completion for --target-sink / --target-object: PipeWire node.name
    values, from the same pw-dump boundary _autodetect_speaker_sink() uses."""
    try:
        from dolby_to_easyeffects import _enumerate_audio_sinks
        names = [s.get("name", "") for s in _enumerate_audio_sinks()]
    except Exception:  # a wedged or absent PipeWire must never break TAB
        return []
    return [n for n in names if n.startswith(prefix)]


def _attach_completers(parser: argparse.ArgumentParser) -> None:
    """Tell argcomplete what each value-taking option means — argparse records
    `type=Path` for the preset JSON, the output conf and the IRS directory
    alike, and nothing at all for PipeWire node names."""
    from argcomplete.completers import DirectoriesCompleter, FilesCompleter

    completers = {
        "preset":        FilesCompleter(("json",)),
        "output":        FilesCompleter(("conf",)),
        "irs_dir":       DirectoriesCompleter(),
        "target_sink":   _complete_sink_names,
        "target_object": _complete_sink_names,
    }
    for action in parser._actions:
        completer = completers.get(action.dest)
        if completer is not None:
            action.completer = completer


def main(argv: list[str] | None = None, wrapped: bool = False) -> int:
    """``wrapped`` marks an in-process dolby_to_pipewire.py run: the wrapper
    owns all activation messaging ([3/3] activates, lists the steps, or says
    dry run), so the --skip-next-steps "To activate:" fallback is dropped —
    it printed the identical restart command two lines above [3/3]'s step 1
    (user-review round 5)."""
    parser = build_parser(argv)
    if argcomplete is not None:
        _attach_completers(parser)
        argcomplete.autocomplete(parser)
    args = parser.parse_args(argv)
    if args.no_color:
        _disable_color()

    # Inspection mode: reads the installed state, converts nothing, so the
    # preset positional is optional — and required for everything else.
    if args.doctor:
        return report_pw_doctor()
    if args.preset is None:
        parser.error("a preset path is required (or --doctor to inspect what "
                     "is already installed)")

    preset_path: Path = args.preset
    if not preset_path.is_file():
        cprint("err", f"error: preset not found: {preset_path}")
        return 2
    try:
        preset = json.loads(preset_path.read_text())
    except json.JSONDecodeError as e:
        cprint("err", f"error: preset JSON is malformed: {e}")
        return 2

    # Default node-name / -description are derived from the preset
    # filename stem so converting multiple presets produces distinct
    # sinks (e.g. Dolby-Balanced.json → Dolby_Balanced; Dolby-Detailed.json
    # → Dolby_Detailed). Without this, every conversion lands on the
    # same conf path and PW node name. The DEFAULT_NODE_NAME fallback
    # only kicks in for pathological stems (empty after sanitisation).
    if args.node_name is None:
        derived = _sanitize_name(preset_path.stem).strip("_")
        node_name = derived if derived else DEFAULT_NODE_NAME
    else:
        node_name = args.node_name
    if args.node_description is None:
        node_description = preset_path.stem or DEFAULT_NODE_DESCRIPTION
    else:
        node_description = args.node_description

    safe_node_name = _sanitize_name(node_name)
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
        cprint("err", f"error: {e}")
        return 2

    if not chain.stages:
        cprint("err", "error: no stages emitted (preset is empty or every "
                      "plugin was skipped)")
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
        cprint("dim", "[smart-filter] disabled by --target-sink ''; emitting "
                      "v1 virtual-sink conf (apps will target effect_input."
                      f"{safe_node_name} directly)")
    elif args.target_sink:
        target_sink = args.target_sink
        cprint("ok", f"[smart-filter] target sink: {target_sink} (from "
                     "--target-sink)")
    else:
        target_sink, detect_warnings = _autodetect_speaker_sink()
        # Warnings print on both paths: a relaxed-tier match returns a name
        # *and* a warning explaining the fallback.
        for w in detect_warnings:
            cprint("warn", f"[smart-filter] {w}")
        if target_sink:
            # Names the override (round 7): without it a reader whose
            # detection picked the wrong device assumed their only path
            # was filing a report. And how to find NAME (round 8): the
            # flag alone left them with no way to discover a value.
            cprint("ok", f"[smart-filter] your built-in speakers: "
                         f"{target_sink} "
                         "(autodetected — wrong device? --target-sink NAME "
                         "overrides)")
            cprint("dim", "  (list sink names with: pw-cli ls Node | grep "
                          "alsa_output)")
        else:
            cprint("warn", "[smart-filter] falling back to v1 virtual-sink "
                           "conf (apps will target effect_input."
                           f"{safe_node_name}); pass --target-sink "
                           "<node.name> to enable smart-filter routing.")

    links = emit_links(chain.stages)
    conf = format_conf(chain.stages, links, node_name,
                       node_description,
                       target_object=args.target_object,
                       target_sink=target_sink,
                       warnings=chain.warnings)

    for w in chain.warnings:
        cprint("warn", f"[warn] {w}")

    if not args.no_validate:
        rc, output = _validate_conf(conf)
        if rc == -1:
            cprint("dim", f"[validate] skipped: {output.strip()}")
            # Without lv2info the plugin set can't be checked, so a missing
            # runtime dependency would otherwise pass unnoticed — remind the
            # user what the chain needs.
            cprint("warn", "[validate] the plugin set wasn't checked — make "
                           "sure the LV2 plugins this conf uses are installed: "
                           "LSP (lsp-plugins-lv2) for the PEQ / MBC / limiter, "
                           "plus Calf (calf-plugins) if it includes "
                           "bass_enhancer / stereo_tools. Otherwise the chain "
                           "won't load.")
        elif rc == 2:
            # Setup error inside validate_conf.py — degraded gracefully.
            cprint("dim", f"[validate] skipped (setup): {output.strip()}")
        elif rc != 0:
            if output.strip():
                cprint("err", output.rstrip())
            cprint("err", "error: schema validation failed; conf not written")
            return 1
        elif output.strip():
            # Validation passed, but validate_conf still emits warnings — most
            # importantly "no lv2info schema available for <uri>" when a
            # referenced LSP/Calf plugin isn't installed, so its ports
            # couldn't be checked. Surface them; otherwise the conf writes
            # "successfully" while the chain silently fails to load for a
            # missing runtime dependency.
            for line in output.strip().splitlines():
                cprint("warn", f"[validate] {line}")

    if args.dry_run:
        # Only dump the conf when stdout is going somewhere — a pipe, a file,
        # a pager. On a terminal it is a few hundred lines of JSON between the
        # troubleshooting menu and the end of the run, which buries everything
        # either side of it; the paths below say what would have been written,
        # which is what a human running --dry-run is actually asking.
        if sys.stdout.isatty():
            # A paste-able instruction, not a flag fragment: "e.g. --dry-run
            # > preview.conf" made a reviewer guess what goes in front, and
            # they expected the whole log to land in the file — it won't,
            # these messages are on stderr and only the conf is on stdout.
            cprint("dim", "(conf not shown — add ' > preview.conf' to your "
                          "command to save it; only the conf lands in the "
                          "file, these messages stay on screen)")
        else:
            sys.stdout.write(conf)
        would_conf = (args.output.expanduser() if args.output is not None
                      else (DEFAULT_OUTPUT_DIR
                            / f"{safe_node_name}.conf").expanduser())
        would_irs = (target_irs if (src_irs is not None
                     and src_irs.resolve() != target_irs.resolve())
                     else None)
        _print_results(would_conf, would_irs, dry_run=True)
        return 0

    assert output_path is not None
    if output_path.exists() and not args.force:
        cprint("err", f"error: {output_path} exists; pass --force to overwrite")
        return 1

    # IRS copy: skip when source and target are the same path (no-op),
    # otherwise honour the same --force semantics as the conf write.
    copied_irs: Path | None = None
    if src_irs is not None and src_irs.resolve() != target_irs.resolve():
        if target_irs.exists() and not args.force:
            cprint("err", f"error: {target_irs} exists; pass --force to "
                          "overwrite")
            return 1
        target_irs.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_irs, target_irs)
        copied_irs = target_irs

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(conf)
    _print_results(output_path, copied_irs, dry_run=False)
    warn_if_stacked(output_path, target_sink)
    if args.skip_next_steps:
        # Checklist suppressed, but a freshly written conf must never go
        # unmentioned as inactive — keep the one action that makes it live.
        # Unless a wrapper is driving: its [3/3] owns activation.
        if not wrapped:
            cprint("cta", "To activate: systemctl --user restart pipewire "
                          "pipewire-pulse")
    else:
        _print_next_steps(node_name, target_object=args.target_object)
    return 0


if __name__ == "__main__":
    sys.exit(main())
