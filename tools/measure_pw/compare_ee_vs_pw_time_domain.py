#!/usr/bin/env python3
"""Time-domain equivalence check between EE and PW captures.

Companion to compare_ee_vs_pw.py (frequency-domain). Two filter
chains can have identical magnitude responses but different phase
responses, in which case `compare_ee_vs_pw.py` would PASS while
transient behavior diverges audibly. This script subtracts the two
captures sample-by-sample after lag alignment and reports the
residual energy.

Method:
  1. Cross-correlate the two captures to find the integer-sample lag
     for which they're most similar.
  2. Shift PW by that lag and crop both to the overlapping region.
  3. Compute residual = ee_aligned - pw_aligned (per channel, then
     summed).
  4. Report: residual RMS in dBFS, residual / signal ratio in dB,
     residual peak in dBFS.

Outputs (in --out-dir):
  td_summary.json       — per-stimulus stats and PASS/FAIL
  td_<stim>.png         — time-aligned overlay + residual (matplotlib)

Equivalence target: residual RMS at least 30 dB below the signal RMS
(i.e. signal-to-residual ratio ≥ 30 dB) on every stimulus. The
threshold is a safety margin, not a ceiling: real measurements on
the development device with the full LSP+Calf chain run at
+70..+73 dB S/R on mono-symmetric stimuli (sweep / pink / multitone),
so a regression that drops below 30 dB is a clear signal of an
actual divergence rather than a metrology limit.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import correlate

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_EE_DIR = REPO_ROOT / "localresearch" / "measure_ee" / "captures_ee"
DEFAULT_PW_DIR = REPO_ROOT / "localresearch" / "measure_pw" / "captures"
DEFAULT_OUT_DIR = REPO_ROOT / "localresearch" / "measure_pw" / "ee_vs_pw"

SR = 48000


def _load(path: Path) -> np.ndarray:
    sr, x = wavfile.read(str(path))
    if sr != SR:
        raise SystemExit(f"{path}: sr={sr} != {SR}")
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


def _find_lag(a: np.ndarray, b: np.ndarray, max_lag: int = 4800) -> int:
    """Integer-sample lag (positive: b is delayed) that maximises
    cross-correlation between mono a and mono b."""
    if a.ndim > 1:
        a = a.mean(axis=1)
    if b.ndim > 1:
        b = b.mean(axis=1)
    # Use a 2-second window for speed
    n = min(96000, a.shape[0], b.shape[0])
    a = a[:n]
    b = b[:n]
    corr = correlate(b, a, mode="full")
    centre = len(corr) // 2
    lo = max(0, centre - max_lag)
    hi = min(len(corr), centre + max_lag + 1)
    sub = corr[lo:hi]
    peak = int(np.argmax(np.abs(sub)))
    return (lo + peak) - centre


def _fft_shift(x: np.ndarray, frac: float) -> np.ndarray:
    """Shift a signal by `frac` samples (can be fractional) using
    FFT phase rotation. Stereo-aware (column-wise)."""
    if x.ndim == 1:
        return _fft_shift_mono(x, frac)
    return np.column_stack([_fft_shift_mono(x[:, c], frac)
                            for c in range(x.shape[1])])


def _fft_shift_mono(x: np.ndarray, frac: float) -> np.ndarray:
    n = x.shape[0]
    # FFT, multiply by exp(-j*2*pi*k*frac/n), iFFT
    spec = np.fft.rfft(x.astype(np.float64))
    k = np.arange(spec.shape[0])
    phase = np.exp(-2j * np.pi * k * frac / n)
    spec *= phase
    return np.fft.irfft(spec, n=n).astype(x.dtype)


def _align_and_trim(ee: np.ndarray, pw: np.ndarray
                    ) -> tuple[np.ndarray, np.ndarray, float]:
    """Align PW to EE — integer lag from cross-correlation, then
    fractional refinement by minimising residual energy. Trim to
    overlapping range. Returns (ee', pw', lag_samples_float).
    """
    int_lag = _find_lag(ee, pw)
    if int_lag > 0:
        pw_int = pw[int_lag:]
        ee_int = ee
    elif int_lag < 0:
        ee_int = ee[-int_lag:]
        pw_int = pw
    else:
        ee_int, pw_int = ee, pw
    n = min(ee_int.shape[0], pw_int.shape[0])
    ee_int = ee_int[:n]
    pw_int = pw_int[:n]

    # Fractional refinement: ternary-ish search over sub-sample shifts
    # in [-0.5, +0.5]. Subsample alignment can lower the residual
    # 20-40 dB on signals dominated by mid/high content where phase
    # mismatch is most audible.
    def residual_rms(frac: float) -> float:
        shifted = _fft_shift(pw_int, frac)
        diff = ee_int - shifted
        return float(np.sqrt(np.mean(diff.astype(np.float64) ** 2)))

    # Coarse scan
    fracs = np.linspace(-1.0, 1.0, 41)
    rms = [residual_rms(f) for f in fracs]
    coarse_best = float(fracs[int(np.argmin(rms))])
    # Refine around the coarse minimum
    fine_fracs = np.linspace(coarse_best - 0.05, coarse_best + 0.05, 41)
    fine_rms = [residual_rms(f) for f in fine_fracs]
    best_frac = float(fine_fracs[int(np.argmin(fine_rms))])

    pw_aligned = _fft_shift(pw_int, best_frac)
    return ee_int, pw_aligned, int_lag + best_frac


@dataclass
class TDStats:
    stimulus: str
    lag_samples: float
    lag_ms: float
    signal_rms_db: float
    residual_rms_db: float
    residual_peak_db: float
    sr_db: float
    pass_tolerance: bool


def compare_capture_pair(ee_path: Path, pw_path: Path, sr_target_db: float,
                         out_plot: Path | None, tag: str,
                         per_channel: bool = False) -> TDStats:
    """Compute the EE vs PW time-domain residual for one capture pair.

    ``per_channel=True`` reports the worst-case S/R across L and R, so a
    divergence isolated to one channel (e.g. a stereo_tools sign error
    affecting only the Side path) isn't masked by the other channel
    matching cleanly. Sweeps and L=R steady stimuli use the default
    full-array residual, where the per-channel split would just shave
    3 dB off identical numbers.
    """
    ee = _load(ee_path)
    pw = _load(pw_path)
    ee_a, pw_a, lag = _align_and_trim(ee, pw)
    residual = ee_a - pw_a

    sig = ee_a  # use EE as the signal reference
    if per_channel and sig.ndim == 2 and sig.shape[1] >= 2:
        # Compute per-channel stats; report the worst (lower S/R) so the
        # PASS/FAIL decision matches what a careful listener would catch.
        sr_per_channel = []
        for ch in range(sig.shape[1]):
            srms = float(np.sqrt(np.mean(sig[:, ch].astype(np.float64) ** 2)))
            rrms = float(np.sqrt(np.mean(residual[:, ch].astype(np.float64) ** 2)))
            ratio = 20 * math.log10(max(srms, 1e-12) / max(rrms, 1e-12))
            sr_per_channel.append((ratio, srms, rrms,
                                   float(np.max(np.abs(residual[:, ch])))))
        # Pick the worst-S/R channel for the headline numbers.
        worst_idx = int(np.argmin([s[0] for s in sr_per_channel]))
        sr_db, sig_rms, res_rms, res_peak = sr_per_channel[worst_idx]
        ch_label = "L" if worst_idx == 0 else "R"
        report_tag = f"{tag} (worst={ch_label})"
    else:
        sig_rms = float(np.sqrt(np.mean(sig.astype(np.float64) ** 2)))
        res_rms = float(np.sqrt(np.mean(residual.astype(np.float64) ** 2)))
        res_peak = float(np.max(np.abs(residual)))
        sr_db = 20 * math.log10(max(sig_rms, 1e-12) /
                                max(res_rms, 1e-12))
        report_tag = tag

    if out_plot is not None:
        _maybe_plot(out_plot, report_tag, sig, residual)
    return TDStats(
        stimulus=report_tag,
        lag_samples=lag,
        lag_ms=lag / SR * 1000,
        signal_rms_db=20 * math.log10(max(sig_rms, 1e-12)),
        residual_rms_db=20 * math.log10(max(res_rms, 1e-12)),
        residual_peak_db=20 * math.log10(max(res_peak, 1e-12)),
        sr_db=sr_db,
        pass_tolerance=bool(sr_db >= sr_target_db),
    )


def _maybe_plot(out_path: Path, tag: str, sig: np.ndarray,
                residual: np.ndarray) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    # Take the post-pre-silence chunk: samples 4800..14400 (0.1s..0.3s)
    n0, n1 = 4800, 14400
    if sig.shape[0] < n1:
        n0, n1 = 0, min(9600, sig.shape[0])
    t = np.arange(n0, n1) / SR * 1000  # ms
    fig, axes = plt.subplots(2, 1, figsize=(10, 5), sharex=True)
    axes[0].plot(t, sig[n0:n1, 0], label="EE", linewidth=0.7)
    axes[0].plot(t, sig[n0:n1, 0] - residual[n0:n1, 0],
                 label="PW (aligned)", linewidth=0.7, linestyle="--")
    axes[0].set_ylabel("Amplitude")
    axes[0].set_title(f"EE vs PW (time domain) — {tag}")
    axes[0].legend()
    axes[0].grid(True, alpha=0.3)
    axes[1].plot(t, residual[n0:n1, 0], color="red", linewidth=0.6)
    axes[1].set_ylabel("Residual (EE - PW)")
    axes[1].set_xlabel("Time (ms)")
    axes[1].grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ee-dir", type=Path, default=DEFAULT_EE_DIR)
    ap.add_argument("--pw-dir", type=Path, default=DEFAULT_PW_DIR)
    ap.add_argument("--ee-label", default="ee_dolby_balanced")
    ap.add_argument("--pw-label", default="pw_dolby_balanced")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--target-sr-db", type=float, default=30.0,
                    help="minimum signal-to-residual ratio in dB to PASS "
                         "(default 30 dB safety margin; mono stimuli "
                         "typically measure +70 dB+ on the dev device, "
                         "so a sub-30 result is a real regression)")
    ap.add_argument("--stimulus-dir", type=Path,
                    default=DEFAULT_PW_DIR.parent.parent / "measure_ee" /
                            "stimuli",
                    help="where stimulus_*.json metadata lives — needed "
                         "to detect asymmetric stereo stimuli and switch "
                         "to per-channel residual comparison")
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    # Asymmetric stereo stimuli are diffed per-channel so a one-channel
    # divergence (e.g. a wrong stereo_tools side gain) isn't diluted by
    # the other channel's clean residual. Symmetric stimuli get the
    # default full-array residual.
    stims = ["sweep", "sweep_quiet", "pink", "pink_quiet", "multitone",
             "stereo_pink", "stereo_correlated"]
    all_stats: list[TDStats] = []
    for tag in stims:
        ee_path = args.ee_dir / f"loopback_{tag}_{args.ee_label}.wav"
        pw_path = args.pw_dir / f"loopback_{tag}_{args.pw_label}.wav"
        if not ee_path.is_file() or not pw_path.is_file():
            print(f"WARN: missing capture for {tag}; skipping")
            continue
        meta_path = args.stimulus_dir / f"stimulus_{tag}.json"
        per_channel = False
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text())
                per_channel = meta.get("stereo_mode") == "asymmetric"
            except (json.JSONDecodeError, OSError):
                pass
        plot = args.out_dir / f"td_{tag}.png"
        stats = compare_capture_pair(ee_path, pw_path, args.target_sr_db,
                                     plot, tag, per_channel=per_channel)
        all_stats.append(stats)
        verdict = "PASS" if stats.pass_tolerance else "FAIL"
        print(f"[{stats.stimulus:14}] {verdict}  "
              f"lag={stats.lag_samples:+.2f} ({stats.lag_ms:+.2f} ms)  "
              f"sig={stats.signal_rms_db:+.1f} dBFS  "
              f"res={stats.residual_rms_db:+.1f} dBFS  "
              f"S/R={stats.sr_db:+.1f} dB")

    summary = {
        "target_sr_db": args.target_sr_db,
        "ee_label": args.ee_label,
        "pw_label": args.pw_label,
        "results": [
            {k: getattr(s, k) for k in ("stimulus", "lag_samples", "lag_ms",
                                         "signal_rms_db", "residual_rms_db",
                                         "residual_peak_db", "sr_db",
                                         "pass_tolerance")}
            for s in all_stats
        ],
        "all_pass": all(s.pass_tolerance for s in all_stats) and len(all_stats) > 0,
    }
    (args.out_dir / "td_summary.json").write_text(
        json.dumps(summary, indent=2)
    )
    print()
    print(f"summary: {args.out_dir/'td_summary.json'}")
    if summary["all_pass"]:
        print("ALL PASS — EE and PW are time-domain equivalent")
        return 0
    print("FAIL — at least one stimulus has insufficient S/R")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
