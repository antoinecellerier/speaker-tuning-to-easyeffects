"""Every EE plugin block, turned into the LV2 node that stands in for it.

One ``emit_*`` per EasyEffects plugin key, each returning a ``Stage`` — the
node dict(s) plus the four port references that let ``conf.py`` link stages
without knowing which plugin produced them. ``EE_KEY_DISPATCH`` is the table
that says which emitter serves which key; ``build_chain`` walks it.

The ``EE_*`` tables are the other half of the translation: EasyEffects writes
enum parameters as string labels and LSP/Calf want integers, so every one of
them is a label→index map read through ``_enum``, which warns rather than
falling back silently when a label is missing.

Stdlib-only, and deliberately the deepest module of ``lib/pipewire`` — nothing
here reads the filesystem beyond resolving the impulse response, prints
anything, or knows where a conf is installed. That is what lets the converter
import it at startup without paying for numpy, and what lets the emitters be
unit-tested by calling them with a dict.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


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
