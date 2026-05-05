#!/usr/bin/env python3
"""Overlay an EE-chain capture against a PipeWire-filter-chain capture
generated from the same EE preset.

Used to prove equivalence between `dolby_to_easyeffects.py`'s EE preset
output and `ee_to_pipewire.py`'s converted PW filter-chain. Both chains
are fed the same stimulus battery (sweep, pink, multitone) and captured
into the same `ee_capture` null sink, so the only difference between
the two captures is the host (EE vs PW filter-chain).

Inputs:

    <ee-dir>/loopback_<stim>_<ee-label>.wav + .json
    <pw-dir>/loopback_<stim>_<pw-label>.wav + .json

Outputs (in --out-dir):

    summary.json     — per-stimulus magnitude diff stats and verdict
    diff_<stim>.png  — magnitude overlay + difference (if matplotlib)

Usage (defaults assume the standard ./localresearch/measure_{ee,pw}/
layout):

    python3 tools/measure_pw/compare_ee_vs_pw.py

Or with explicit paths:

    python3 tools/measure_pw/compare_ee_vs_pw.py \\
        --ee-dir localresearch/measure_ee/captures_ee \\
        --pw-dir localresearch/measure_pw/captures \\
        --ee-label ee_dolby_balanced \\
        --pw-label pw_dolby_balanced

Equivalence verdict: PASS when |dB diff| stays under --tolerance-db
(default 0.5 dB) across the band 50 Hz–18 kHz on every stimulus.
For multitone stimuli we restrict the comparison to ±2 bins around
each tone frequency (inter-tone bins are noise vs noise).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import fftconvolve

SR = 48000

# Comparison band — below 50 Hz the speaker HP and FFT bin spacing make
# the diff noisy; above 18 kHz the analysis sweep tapers and content is
# inaudible.
BAND_LO_HZ = 50.0
BAND_HI_HZ = 18000.0

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_EE_DIR = REPO_ROOT / "localresearch" / "measure_ee" / "captures_ee"
DEFAULT_PW_DIR = REPO_ROOT / "localresearch" / "measure_pw" / "captures"
DEFAULT_STIMULUS_DIR = REPO_ROOT / "localresearch" / "measure_ee" / "stimuli"
DEFAULT_OUT_DIR = REPO_ROOT / "localresearch" / "measure_pw" / "ee_vs_pw"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_wav_f32(path: Path) -> np.ndarray:
    sr, x = wavfile.read(str(path))
    if sr != SR:
        raise SystemExit(f"{path}: sample rate {sr} != expected {SR}")
    if x.dtype == np.int16:
        x = x.astype(np.float32) / 32768.0
    elif x.dtype == np.int32:
        x = x.astype(np.float32) / 2147483648.0
    elif x.dtype == np.float32:
        pass
    else:
        x = x.astype(np.float32)
    if x.ndim == 1:
        x = np.column_stack([x, x])
    return x


def _find_capture(cap_dir: Path, stim_tag: str, label: str) -> Path:
    path = cap_dir / f"loopback_{stim_tag}_{label}.wav"
    if not path.is_file():
        raise SystemExit(f"missing capture: {path}")
    return path


# ---------------------------------------------------------------------------
# Sweep deconvolution (Farina)
# ---------------------------------------------------------------------------

def _deconvolve_sweep(loopback: np.ndarray, inverse: np.ndarray
                      ) -> tuple[np.ndarray, np.ndarray]:
    """Return per-channel IRs from the Farina-deconvolved capture."""
    irs = []
    for ch in range(loopback.shape[1]):
        full = fftconvolve(loopback[:, ch], inverse, mode="full")
        peak = int(np.argmax(np.abs(full)))
        # Take a 4096-sample tail starting at the peak — enough to
        # capture the chain's group delay + minimum-phase decay.
        tail_len = 4096
        ir = full[peak:peak + tail_len]
        if ir.size < tail_len:
            ir = np.pad(ir, (0, tail_len - ir.size))
        irs.append(ir)
    return np.column_stack(irs), np.array([0, 0])


def _ir_magnitude_db(ir: np.ndarray, n_fft: int = 8192
                     ) -> tuple[np.ndarray, np.ndarray]:
    """Return (freqs, mag_db) for an IR. Stereo IRs are averaged."""
    if ir.ndim == 2:
        ir = ir.mean(axis=1)
    spectrum = np.fft.rfft(ir, n=n_fft)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / SR)
    mag_db = 20 * np.log10(np.maximum(np.abs(spectrum), 1e-12))
    return freqs, mag_db


# ---------------------------------------------------------------------------
# Pink-noise spectrum
# ---------------------------------------------------------------------------

def _windowed_spectrum_db(loopback: np.ndarray, win_start: float,
                          win_end: float, n_fft: int = 16384,
                          channel: int | None = None
                          ) -> tuple[np.ndarray, np.ndarray]:
    """Average power spectrum over [win_start, win_end] seconds, in dB.

    ``channel`` selects a single column when set (0=L, 1=R); leaving it
    ``None`` mono-sums L+R, which is lossless for symmetric stimuli but
    nulls the Side component for asymmetric content. Asymmetric stimuli
    must be diffed per-channel — see compare_steady's stereo branch.
    """
    n0 = int(win_start * SR)
    n1 = min(int(win_end * SR), loopback.shape[0])
    if n1 - n0 < n_fft:
        n_fft = max(1024, (n1 - n0) // 2)
    if channel is None:
        sig = loopback[n0:n1].mean(axis=1)  # mono-sum L+R
    else:
        sig = loopback[n0:n1, channel]
    # Welch-style averaging
    hop = n_fft // 2
    window = np.hanning(n_fft).astype(np.float32)
    accum = np.zeros(n_fft // 2 + 1, dtype=np.float64)
    n_segs = 0
    pos = 0
    while pos + n_fft <= sig.shape[0]:
        seg = sig[pos:pos + n_fft] * window
        spectrum = np.fft.rfft(seg)
        accum += np.abs(spectrum) ** 2
        n_segs += 1
        pos += hop
    if n_segs == 0:
        spectrum = np.fft.rfft(sig[:n_fft] * window[:sig.shape[0]])
        accum = np.abs(spectrum) ** 2
        n_segs = 1
    psd = accum / n_segs
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / SR)
    mag_db = 10 * np.log10(np.maximum(psd, 1e-30))
    return freqs, mag_db


# ---------------------------------------------------------------------------
# Diff stats over the comparison band
# ---------------------------------------------------------------------------

@dataclass
class DiffStats:
    stimulus: str
    band_max_db: float
    band_max_freq_hz: float
    band_rms_db: float
    band_99p_db: float
    pass_tolerance: bool


def _band_stats(freqs: np.ndarray, ee_db: np.ndarray, pw_db: np.ndarray,
                lo: float, hi: float, tol_db: float, stim: str) -> DiffStats:
    mask = (freqs >= lo) & (freqs <= hi)
    diff = ee_db[mask] - pw_db[mask]
    abs_diff = np.abs(diff)
    max_idx = int(np.argmax(abs_diff))
    return DiffStats(
        stimulus=stim,
        band_max_db=float(abs_diff[max_idx]),
        band_max_freq_hz=float(freqs[mask][max_idx]),
        band_rms_db=float(np.sqrt(np.mean(diff ** 2))),
        band_99p_db=float(np.quantile(abs_diff, 0.99)),
        pass_tolerance=bool(abs_diff.max() < tol_db),
    )


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------

def _maybe_plot(out_path: Path, stim: str, freqs: np.ndarray,
                ee_db: np.ndarray | None = None,
                pw_db: np.ndarray | None = None,
                ee_l: np.ndarray | None = None,
                ee_r: np.ndarray | None = None,
                pw_l: np.ndarray | None = None,
                pw_r: np.ndarray | None = None) -> None:
    """Plot the EE vs PW magnitude diff. When per-channel arrays are
    supplied, the top panel shows L and R separately for both EE and
    PW (4 traces) and the bottom panel overlays the L and R diffs in
    different colours — making any asymmetric divergence visually
    obvious. Without per-channel arrays, falls back to the original
    two-trace plot for symmetric / mono-summed stimuli.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    mask = (freqs >= 20) & (freqs <= 22000)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    has_per_channel = (ee_l is not None and ee_r is not None
                       and pw_l is not None and pw_r is not None)
    if has_per_channel:
        ax1.semilogx(freqs[mask], ee_l[mask], label="EE L",
                     linewidth=1.0, color="C0")
        ax1.semilogx(freqs[mask], ee_r[mask], label="EE R",
                     linewidth=1.0, color="C2")
        ax1.semilogx(freqs[mask], pw_l[mask], label="PW L",
                     linewidth=1.0, color="C0", linestyle="--")
        ax1.semilogx(freqs[mask], pw_r[mask], label="PW R",
                     linewidth=1.0, color="C2", linestyle="--")
    else:
        ax1.semilogx(freqs[mask], ee_db[mask], label="EasyEffects",
                     linewidth=1.0)
        ax1.semilogx(freqs[mask], pw_db[mask], label="PipeWire filter-chain",
                     linewidth=1.0, linestyle="--")
    ax1.set_ylabel("Magnitude (dB)")
    ax1.set_title(f"EE vs PW — {stim}")
    ax1.legend()
    ax1.grid(True, which="both", alpha=0.3)
    if has_per_channel:
        diff_l = ee_l[mask] - pw_l[mask]
        diff_r = ee_r[mask] - pw_r[mask]
        ax2.semilogx(freqs[mask], diff_l, color="C0", linewidth=0.8,
                     label="EE − PW (L)")
        ax2.semilogx(freqs[mask], diff_r, color="C2", linewidth=0.8,
                     label="EE − PW (R)")
        ax2.legend(loc="upper right", fontsize=8)
    else:
        diff = ee_db[mask] - pw_db[mask]
        ax2.semilogx(freqs[mask], diff, color="red", linewidth=0.8)
    ax2.set_ylabel("EE − PW (dB)")
    ax2.set_xlabel("Frequency (Hz)")
    ax2.axhline(0, color="black", linewidth=0.5)
    ax2.axhline(0.5, color="gray", linewidth=0.5, linestyle=":")
    ax2.axhline(-0.5, color="gray", linewidth=0.5, linestyle=":")
    ax2.set_ylim(-3, 3)
    ax2.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def compare_sweep(ee_path: Path, pw_path: Path, inverse: np.ndarray,
                  out_plot: Path | None, tol_db: float, tag: str) -> DiffStats:
    ee_lb = _load_wav_f32(ee_path)
    pw_lb = _load_wav_f32(pw_path)
    ee_ir, _ = _deconvolve_sweep(ee_lb, inverse)
    pw_ir, _ = _deconvolve_sweep(pw_lb, inverse)
    freqs, ee_db = _ir_magnitude_db(ee_ir)
    _, pw_db = _ir_magnitude_db(pw_ir)
    stats = _band_stats(freqs, ee_db, pw_db, BAND_LO_HZ, BAND_HI_HZ,
                        tol_db, tag)
    if out_plot is not None:
        _maybe_plot(out_plot, tag, freqs, ee_db, pw_db)
    return stats


def compare_steady(ee_path: Path, pw_path: Path, stim_meta: dict,
                   out_plot: Path | None, tol_db: float, tag: str
                   ) -> DiffStats:
    """Compare a steady-state stimulus, with stereo branching.

    For ``stereo_mode == "asymmetric"`` stimuli (L≠R), comparing the
    mono-sum nulls the Side component, so we run the analysis per
    channel and surface the worst-case stats — that's the only way to
    catch a stereo_tools (or other stage's) M/S divergence.
    """
    ee_lb = _load_wav_f32(ee_path)
    pw_lb = _load_wav_f32(pw_path)
    win_start = float(stim_meta.get("analysis_window_start_seconds", 6.0))
    win_end = float(stim_meta.get("analysis_window_end_seconds", 11.0))
    if stim_meta.get("stereo_mode") == "asymmetric":
        # Per-channel diff: take the worst stats across L and R so the
        # PASS/FAIL decision matches what a careful listener would
        # notice — a divergence on either channel is a real divergence.
        freqs, ee_l = _windowed_spectrum_db(ee_lb, win_start, win_end,
                                            channel=0)
        _, ee_r = _windowed_spectrum_db(ee_lb, win_start, win_end,
                                        channel=1)
        _, pw_l = _windowed_spectrum_db(pw_lb, win_start, win_end,
                                        channel=0)
        _, pw_r = _windowed_spectrum_db(pw_lb, win_start, win_end,
                                        channel=1)
        stats_l = _band_stats(freqs, ee_l, pw_l, BAND_LO_HZ, BAND_HI_HZ,
                              tol_db, f"{tag}/L")
        stats_r = _band_stats(freqs, ee_r, pw_r, BAND_LO_HZ, BAND_HI_HZ,
                              tol_db, f"{tag}/R")
        worst = stats_l if stats_l.band_max_db >= stats_r.band_max_db \
            else stats_r
        # Re-tag so the summary shows the user-facing stimulus name and
        # the worst channel's stats — informative without doubling the
        # summary row count.
        stats = DiffStats(
            stimulus=f"{tag} (worst={worst.stimulus[-1]})",
            band_max_db=worst.band_max_db,
            band_max_freq_hz=worst.band_max_freq_hz,
            band_rms_db=worst.band_rms_db,
            band_99p_db=worst.band_99p_db,
            pass_tolerance=stats_l.pass_tolerance and stats_r.pass_tolerance,
        )
        if out_plot is not None:
            # Pass the per-channel arrays so the plot shows L and R
            # separately on both panels; the diff panel overlays the
            # two channel diffs in different colours so any asymmetric
            # divergence is visually obvious.
            _maybe_plot(out_plot, f"{tag} (per-channel)",
                        freqs, ee_db=None, pw_db=None,
                        ee_l=ee_l, ee_r=ee_r, pw_l=pw_l, pw_r=pw_r)
        return stats
    freqs, ee_db = _windowed_spectrum_db(ee_lb, win_start, win_end)
    _, pw_db = _windowed_spectrum_db(pw_lb, win_start, win_end)

    # For a multitone stimulus we only have meaningful signal at the
    # tone frequencies. Comparing the inter-tone noise floor is just
    # noise vs noise (residuals up to 50+ dB of meaningless ratio).
    tone_freqs = stim_meta.get("tone_frequencies_hz")
    if tone_freqs:
        # Restrict to ±2 bins around each tone frequency (multitone-only)
        bin_hz = freqs[1] - freqs[0] if len(freqs) > 1 else 1.0
        mask = np.zeros_like(freqs, dtype=bool)
        for tone in tone_freqs:
            if BAND_LO_HZ <= tone <= BAND_HI_HZ:
                idx = int(round(tone / bin_hz))
                lo = max(0, idx - 2)
                hi = min(len(freqs), idx + 3)
                mask[lo:hi] = True
        if mask.any():
            stats = _band_stats(freqs[mask], ee_db[mask], pw_db[mask],
                                BAND_LO_HZ, BAND_HI_HZ, tol_db, tag)
        else:
            stats = _band_stats(freqs, ee_db, pw_db, BAND_LO_HZ, BAND_HI_HZ,
                                tol_db, tag)
    else:
        stats = _band_stats(freqs, ee_db, pw_db, BAND_LO_HZ, BAND_HI_HZ,
                            tol_db, tag)

    if out_plot is not None:
        _maybe_plot(out_plot, tag, freqs, ee_db, pw_db)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ee-dir", type=Path, default=DEFAULT_EE_DIR,
                    help=f"directory of EE captures "
                         f"(default: {DEFAULT_EE_DIR})")
    ap.add_argument("--pw-dir", type=Path, default=DEFAULT_PW_DIR,
                    help=f"directory of PW captures "
                         f"(default: {DEFAULT_PW_DIR})")
    ap.add_argument("--ee-label", default="ee_dolby_balanced")
    ap.add_argument("--pw-label", default="pw_dolby_balanced")
    ap.add_argument("--stimulus-dir", type=Path, default=DEFAULT_STIMULUS_DIR,
                    help=f"where stimulus_*.wav and inverse_sweep.npy live "
                         f"(default: {DEFAULT_STIMULUS_DIR})")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help=f"output directory (default: {DEFAULT_OUT_DIR})")
    ap.add_argument("--tolerance-db", type=float, default=0.5,
                    help="Max |dB diff| in the band 50 Hz–18 kHz to PASS "
                         "(default 0.5 dB)")
    args = ap.parse_args()

    inverse_path = args.stimulus_dir / "inverse_sweep.npy"
    if not inverse_path.is_file():
        raise SystemExit(f"missing inverse sweep: {inverse_path} — re-run "
                         "tools/measure_dax/make_stimulus.py")
    inverse = np.load(inverse_path).astype(np.float32)

    args.out_dir.mkdir(parents=True, exist_ok=True)

    stims = [
        ("sweep", "sweep"),
        ("sweep_quiet", "sweep"),
        ("pink", "steady"),
        ("pink_quiet", "steady"),
        ("multitone", "steady"),
        # Asymmetric pink — stim_meta's stereo_mode==asymmetric flips
        # compare_steady into per-channel mode automatically. The
        # decorrelated variant maxes out the S component; the
        # correlated one matches the natural-music operating point
        # (M ≫ S) where stereo_tools' widener is actually applied.
        ("stereo_pink", "steady"),
        ("stereo_correlated", "steady"),
    ]

    all_stats: list[DiffStats] = []
    for tag, kind in stims:
        ee_path = _find_capture(args.ee_dir, tag, args.ee_label)
        pw_path = _find_capture(args.pw_dir, tag, args.pw_label)
        plot = args.out_dir / f"diff_{tag}.png"
        if kind == "sweep":
            stats = compare_sweep(ee_path, pw_path, inverse, plot,
                                  args.tolerance_db, tag)
        else:
            stim_meta_path = args.stimulus_dir / f"stimulus_{tag}.json"
            stim_meta = (json.loads(stim_meta_path.read_text())
                         if stim_meta_path.is_file() else {})
            stats = compare_steady(ee_path, pw_path, stim_meta, plot,
                                   args.tolerance_db, tag)
        all_stats.append(stats)
        verdict = "PASS" if stats.pass_tolerance else "FAIL"
        print(f"[{tag:14}] {verdict}  max={stats.band_max_db:6.2f} dB "
              f"@ {stats.band_max_freq_hz:>6.0f} Hz  "
              f"rms={stats.band_rms_db:5.2f} dB  "
              f"99p={stats.band_99p_db:5.2f} dB")

    summary = {
        "tolerance_db": args.tolerance_db,
        "band_lo_hz": BAND_LO_HZ,
        "band_hi_hz": BAND_HI_HZ,
        "ee_label": args.ee_label,
        "pw_label": args.pw_label,
        "results": [
            {
                "stimulus": s.stimulus,
                "band_max_db": s.band_max_db,
                "band_max_freq_hz": s.band_max_freq_hz,
                "band_rms_db": s.band_rms_db,
                "band_99p_db": s.band_99p_db,
                "pass_tolerance": s.pass_tolerance,
            }
            for s in all_stats
        ],
        "all_pass": all(s.pass_tolerance for s in all_stats),
    }
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2)
    )
    print()
    print(f"summary written to {args.out_dir/'summary.json'}")
    if summary["all_pass"]:
        print("ALL PASS — EE and PW filter-chain are equivalent within tolerance")
        return 0
    print("FAIL — some stimulus diffs exceed tolerance; inspect plots")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
