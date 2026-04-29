#!/usr/bin/env python3
"""Compare a captured EE-pipeline magnitude response against an
analytical model of the same preset (FIR + parametric EQ biquads +
static gains).

Inputs:

    --capture spectrum_<kind>_<label>_<channel>.npz   (from analyze.py;
        contains 'eq_gain_db' which is the EE chain's recovered EQ shape
        relative to flat input)
    --preset Dolby-Balanced.json                      (EE output preset)
    --irs Dolby-Balanced.irs                          (the matching FIR)

Outputs:

    compare_ee_vs_analytical_<basename>.png   (overlay + residual)
    compare_ee_vs_analytical_<basename>.txt   (numeric summary)

The analytical model multiplies:

    H_analytical(f) = |FIR_FFT(f)| × ∏ biquad(equalizer#k bands)
                       × 10**((sum of output_gain_db)/20)

and ignores the dynamic plugins (autogain, MBC, limiter): on the pink-
noise stationary capture they're essentially unity, and any residual
between this model and the capture is exactly the "what these dynamic
plugins are doing" we want to surface.

Usage:

    python tools/measure_ee/compare_ee_analytical.py \\
        --capture /tmp/ee_battery_test/spectrum_pink_ee_dolby_balanced_L.npz \\
        --preset ~/.local/share/easyeffects/output/Dolby-Balanced.json \\
        --irs ~/.local/share/easyeffects/irs/Dolby-Balanced.irs
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import freqz

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests"))

# Reuse the verified RBJ + LSP RLC math from the test suite.
from conftest import (  # noqa: E402  type: ignore[import-not-found]
    rbj_bell, rbj_hishelf, rbj_loshelf, biquad_response_db,
)

SR = 48000


# ----- LSP RLC (BT) HP/LP — mirrors lsp-dsp-units calc_rlc_filter ----

def _rlc_hp_section_ba(f0: float, q: float, fs: int = SR
                       ) -> tuple[np.ndarray, np.ndarray]:
    k = 2.0 / (1.0 + q)
    c = 1.0 / math.tan(math.pi * f0 / fs)
    c2 = c * c
    a0 = c2 + k * c + 1.0
    b = np.array([c2, -2.0 * c2, c2]) / a0
    a = np.array([1.0, (2.0 - 2.0 * c2) / a0, (c2 - k * c + 1.0) / a0])
    return b, a


def _rlc_lp_section_ba(f0: float, q: float, fs: int = SR
                       ) -> tuple[np.ndarray, np.ndarray]:
    k = 2.0 / (1.0 + q)
    c = math.tan(math.pi * f0 / fs)
    c2 = c * c
    a0 = 1.0 + k * c + c2
    b = np.array([c2, 2.0 * c2, c2]) / a0
    a = np.array([1.0, (2.0 * c2 - 2.0) / a0, (1.0 - k * c + c2) / a0])
    return b, a


_SLOPE_TO_DOUBLINGS = {"x1": 1, "x2": 2, "x3": 3, "x4": 4}


def lsp_rlc_hp_db(f0: float, slope: str, q: float,
                  freqs: np.ndarray, fs: int = SR) -> np.ndarray:
    """LSP RLC (BT) HP at user-facing slope ∈ {x1..x4}.

    LSP cascades N 2nd-order sections at slope xN: x1 → 2nd order,
    x2 → 4th order, x4 → 8th order. Note that LSP also internally doubles
    the slope when *constructing* the filter from a Dolby-XML order
    (`para_equalizer.cpp:167`: `*slope = 2 * *slope`); the converter's
    `make_hp_band` accounts for that, so by the time we read the band
    out of an EE preset the user-facing slope is the cascade count we
    apply here.
    """
    n_sections = _SLOPE_TO_DOUBLINGS[slope]
    b, a = _rlc_hp_section_ba(f0, q, fs)
    section_db = biquad_response_db(b, a, freqs, fs=fs)
    return n_sections * section_db


def lsp_rlc_lp_db(f0: float, slope: str, q: float,
                  freqs: np.ndarray, fs: int = SR) -> np.ndarray:
    """LP counterpart to lsp_rlc_hp_db. NOTE: not exercised by any
    current test or preset — the Dolby-Balanced chain only uses HP. If
    you start using LP bands here, validate against an EE-captured LP
    sweep before trusting the residual."""
    n_sections = _SLOPE_TO_DOUBLINGS[slope]
    b, a = _rlc_lp_section_ba(f0, q, fs)
    section_db = biquad_response_db(b, a, freqs, fs=fs)
    return n_sections * section_db


# ----- preset → analytical model -------------------------------------

def _band_response_db(band: dict, freqs: np.ndarray, fs: int = SR
                      ) -> np.ndarray:
    """Magnitude (dB) for a single equalizer band."""
    btype = band.get("type", "Bell")
    f0 = float(band["frequency"])
    q = float(band.get("q", 0.707))
    gain = float(band.get("gain", 0.0))
    slope = band.get("slope", "x1")
    mode = band.get("mode", "RLC (BT)")

    if btype == "Off":
        return np.zeros_like(freqs, dtype=float)
    if btype == "Bell":
        b, a = rbj_bell(f0, gain, q)
        return biquad_response_db(b, a, freqs, fs=fs)
    if btype == "Hi-shelf":
        b, a = rbj_hishelf(f0, gain, q)
        return biquad_response_db(b, a, freqs, fs=fs)
    if btype == "Lo-shelf":
        b, a = rbj_loshelf(f0, gain, q)
        return biquad_response_db(b, a, freqs, fs=fs)
    if btype == "Hi-pass":
        if mode != "RLC (BT)":
            raise NotImplementedError(f"HP mode {mode!r} not modeled here")
        return lsp_rlc_hp_db(f0, slope, q, freqs, fs=fs)
    if btype == "Lo-pass":
        if mode != "RLC (BT)":
            raise NotImplementedError(f"LP mode {mode!r} not modeled here")
        return lsp_rlc_lp_db(f0, slope, q, freqs, fs=fs)
    raise NotImplementedError(f"band type {btype!r} not modeled")


def _equalizer_response_db(eq: dict, freqs: np.ndarray, channel: str,
                           ) -> np.ndarray:
    """Sum response of one equalizer plugin instance over its bands."""
    if eq.get("bypass"):
        return np.zeros_like(freqs, dtype=float)
    in_gain = float(eq.get("input-gain", 0.0))
    out_gain = float(eq.get("output-gain", 0.0))
    n_bands = int(eq.get("num-bands", 0))
    side = eq.get(channel, {})
    total = np.full_like(freqs, in_gain + out_gain, dtype=float)
    for k in range(n_bands):
        band = side.get(f"band{k}")
        if band is None:
            continue
        if band.get("type") == "Off":
            continue
        total = total + _band_response_db(band, freqs)
    return total


def _fir_magnitude_db(irs_path: Path, freqs: np.ndarray) -> np.ndarray:
    """Read an .irs (32-bit float wav) and return |FFT| magnitude (dB)
    interpolated to `freqs`."""
    sr, x = wavfile.read(str(irs_path))
    if sr != SR:
        raise SystemExit(f"FIR sr={sr} != {SR}")
    if x.dtype == np.int16:
        x = x.astype(np.float32) / 32768.0
    elif x.dtype == np.int32:
        x = x.astype(np.float32) / 2147483648.0
    elif x.dtype != np.float32:
        x = x.astype(np.float32)
    if x.ndim == 1:
        ch = x
    else:
        ch = x[:, 0]
    n = max(len(ch), 16384)
    spectrum = np.fft.rfft(ch, n=n)
    f_native = np.fft.rfftfreq(n, 1.0 / SR)
    mag_db = 20 * np.log10(np.maximum(np.abs(spectrum), 1e-12))
    return np.interp(freqs, f_native, mag_db)


@dataclass
class AnalyticalModel:
    f: np.ndarray
    convolver_db: np.ndarray
    eqs_db: list[np.ndarray]   # per-plugin contribution
    eq_names: list[str]
    static_gain_db: float


def build_analytical_model(preset: dict, irs_path: Path, channel: str,
                            freqs: np.ndarray) -> AnalyticalModel:
    output = preset.get("output", preset)
    plugin_order = output.get("plugins_order", [])

    # FIR
    convolver = output.get("convolver#0") or {}
    convolver_db = _fir_magnitude_db(irs_path, freqs)
    convolver_db += float(convolver.get("input-gain", 0.0))
    convolver_db += float(convolver.get("output-gain", 0.0))
    if convolver.get("bypass"):
        convolver_db = np.zeros_like(freqs, dtype=float)

    eq_names: list[str] = []
    eqs_db: list[np.ndarray] = []
    static_gain_db = 0.0
    for name in plugin_order:
        plugin = output.get(name) or {}
        if name.startswith("equalizer"):
            eqs_db.append(_equalizer_response_db(plugin, freqs, channel))
            eq_names.append(name)
        elif name.startswith("stereo_tools"):
            # Stereo_tools is transparent for L=R input on M/S decode;
            # input/output gains contribute statically.
            static_gain_db += float(plugin.get("input-gain", 0.0))
            static_gain_db += float(plugin.get("output-gain", 0.0))
        elif name.startswith("autogain"):
            if not plugin.get("bypass"):
                # Bypass is the default in the user preset; on stationary
                # input autogain settles to a near-static gain. We can't
                # model it without target-loudness info, so we surface the
                # discrepancy as residual rather than guess.
                pass
        elif name.startswith("multiband_compressor"):
            if not plugin.get("bypass"):
                static_gain_db += float(plugin.get("output-gain", 0.0))
        elif name.startswith("limiter"):
            if not plugin.get("bypass"):
                static_gain_db += float(plugin.get("input-gain", 0.0))
                static_gain_db += float(plugin.get("output-gain", 0.0))
    return AnalyticalModel(
        f=freqs, convolver_db=convolver_db,
        eqs_db=eqs_db, eq_names=eq_names,
        static_gain_db=static_gain_db,
    )


def model_total_db(model: AnalyticalModel) -> np.ndarray:
    total = model.convolver_db.copy()
    for eq in model.eqs_db:
        total = total + eq
    total = total + model.static_gain_db
    return total


# ----- main -----------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture", type=Path, required=True,
                    help="spectrum_<kind>_<label>_<channel>.npz from analyze.py")
    ap.add_argument("--preset", type=Path, required=True,
                    help="EE preset JSON")
    ap.add_argument("--irs", type=Path, required=True,
                    help=".irs FIR file referenced by the preset")
    ap.add_argument("--channel", default=None,
                    help="L/R (default: parsed from capture filename)")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Output directory (default capture's dir)")
    ap.add_argument("--align", default="median",
                    choices=("median", "1k", "none"),
                    help="How to align analytical to capture: subtract "
                         "the median residual (default), the residual at "
                         "1 kHz, or no alignment")
    args = ap.parse_args()

    cap = np.load(str(args.capture))
    if "eq_gain_db" not in cap.files:
        raise SystemExit(
            "capture npz must contain 'eq_gain_db' — re-run analyze.py "
            "to regenerate"
        )
    f = cap["f"].astype(float)
    cap_db = cap["eq_gain_db"].astype(float)

    channel = args.channel
    if channel is None:
        stem = args.capture.stem
        if stem.endswith("_L"):
            channel = "left"
        elif stem.endswith("_R"):
            channel = "right"
        else:
            channel = "left"
    elif channel.upper() == "L":
        channel = "left"
    elif channel.upper() == "R":
        channel = "right"

    preset = json.loads(args.preset.read_text())
    model = build_analytical_model(preset, args.irs, channel, f)
    model_db = model_total_db(model)

    band_mask = (f >= 200) & (f <= 18000)
    diff = cap_db - model_db
    if args.align == "median":
        offset = float(np.median(diff[band_mask]))
    elif args.align == "1k":
        offset = float(np.interp(1000.0, f, diff))
    else:
        offset = 0.0
    model_db_aligned = model_db + offset
    resid = cap_db - model_db_aligned

    max_resid = float(np.max(np.abs(resid[band_mask])))
    p95_resid = float(np.percentile(np.abs(resid[band_mask]), 95))
    rms_resid = float(np.sqrt(np.mean(resid[band_mask] ** 2)))

    out_dir = args.out_dir or args.capture.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"compare_ee_vs_analytical_{args.capture.stem}"

    txt_lines = [
        f"capture: {args.capture}",
        f"preset:  {args.preset}",
        f"irs:     {args.irs}",
        f"channel: {channel}",
        f"alignment: {args.align}  (offset = {offset:+.3f} dB)",
        f"static_gain_db (model): {model.static_gain_db:+.2f}",
        f"plugins in model: convolver, " + ", ".join(model.eq_names),
        "",
        f"residual (200 Hz – 18 kHz) of (capture − model_aligned):",
        f"  max |Δ| = {max_resid:.2f} dB",
        f"  p95 |Δ| = {p95_resid:.2f} dB",
        f"  RMS Δ   = {rms_resid:.2f} dB",
    ]
    txt_path = out_dir / f"{base}.txt"
    txt_path.write_text("\n".join(txt_lines) + "\n")
    for line in txt_lines:
        print(line)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(matplotlib unavailable — skipping plot)", file=sys.stderr)
        return 0

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 7), sharex=True)
    ax1.semilogx(f, cap_db, label="EE capture", color="C0")
    ax1.semilogx(f, model_db_aligned, "--", label="analytical model", color="C3")
    ax1.semilogx(f, model.convolver_db, ":", label="FIR-only", color="0.5", lw=0.8)
    ax1.axvspan(200, 18000, color="0.95", alpha=0.5, zorder=-1)
    ax1.set_ylabel("dB")
    ax1.set_title(f"EE pipeline ({channel}): capture vs analytical model")
    ax1.legend(loc="lower left", fontsize=8)
    ax1.grid(True, which="both", alpha=0.3)

    ax2.semilogx(f, resid, color="C2")
    ax2.axhline(0, lw=0.5, color="k")
    ax2.axvspan(200, 18000, color="0.95", alpha=0.5, zorder=-1)
    ax2.set_xlim(20, 24000)
    ax2.set_ylim(-6, 6)
    ax2.set_xlabel("Hz")
    ax2.set_ylabel("residual (dB)")
    ax2.set_title(
        f"residual = capture − model_aligned  "
        f"(max |Δ| = {max_resid:.2f} dB, RMS = {rms_resid:.2f} dB, "
        f"in 200 Hz–18 kHz)"
    )
    ax2.grid(True, which="both", alpha=0.3)

    fig.tight_layout()
    png_path = out_dir / f"{base}.png"
    fig.savefig(str(png_path), dpi=110)
    plt.close(fig)
    print(f"\nwrote {png_path}")
    print(f"wrote {txt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
