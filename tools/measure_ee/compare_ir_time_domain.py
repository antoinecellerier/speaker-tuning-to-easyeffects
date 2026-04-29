#!/usr/bin/env python3
"""Time-domain comparison of impulse responses.

Plots up to three IRs aligned at their peak with both linear-amplitude
(top) and log-envelope (bottom) views, and prints a numeric summary
(peak position, time-to-decay, cumulative-energy times). Used to
answer "why is my converter-generated FIR shorter than the loopback
IR" type questions: most often the captured IR's tail is below
−60 dB and looks long only on a linear amplitude scale.

Inputs (any combination, all optional but at least one):

    --converter-irs <path>   the .irs the converter writes (e.g.
                             ~/.local/share/easyeffects/irs/Dolby-Balanced.irs)
    --ee-ir <path>           an EE-captured IR from analyze.py
                             (e.g. ir_sweep_<label>_L.wav)
    --dax-ir <path>          a DAX-captured IR from analyze.py
                             (same naming convention)

Output:

    --out <path>             figure file (PNG). If omitted, only prints
                             the numeric summary.

Example:

    python tools/measure_ee/compare_ir_time_domain.py \\
        --converter-irs ~/.local/share/easyeffects/irs/Dolby-Balanced.irs \\
        --ee-ir ~/dax-measure/ee_captures/ir_sweep_ee_dynamic_balanced_L.wav \\
        --dax-ir ~/dax-measure/captures/ir_sweep_dynamic_L.wav \\
        --out ~/dax-measure/three_way/compare_ir_time_domain_L.png
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.io import wavfile


@dataclass
class IRStats:
    name: str
    sr: int
    n_samples: int
    peak_idx: int
    peak_value: float
    decay_60_ms: float       # peak → first envelope < −60 dB
    decay_80_ms: float       # peak → first envelope < −80 dB
    energy_95_ms: float      # peak → 95% cumulative energy
    energy_99_ms: float
    energy_999_ms: float
    pre_peak_rms_db: float   # RMS dB of pre-peak window relative to peak


def load_ir(path: Path) -> tuple[int, np.ndarray]:
    sr, x = wavfile.read(str(path))
    if x.dtype == np.int16:
        x = x.astype(np.float32) / 32768.0
    elif x.dtype == np.int32:
        x = x.astype(np.float32) / 2147483648.0
    elif x.dtype != np.float32:
        x = x.astype(np.float32)
    if x.ndim > 1:
        x = x[:, 0]
    return int(sr), x


def _envelope_db(xn: np.ndarray, sr: int, win_ms: float = 1.0) -> np.ndarray:
    win = max(1, int(round(win_ms * 1e-3 * sr)))
    rms = np.sqrt(np.convolve(xn * xn, np.ones(win) / win, mode="same"))
    return 20 * np.log10(np.maximum(rms, 1e-10))


def _first_drop_below(env_db: np.ndarray, peak_idx: int,
                      threshold_db: float) -> int:
    """First sample index ≥ peak_idx where envelope is below threshold.
    Returns len(env_db) if it never drops there."""
    tail = env_db[peak_idx:]
    below = np.where(tail < threshold_db)[0]
    return int(peak_idx + below[0]) if below.size else len(env_db)


def _cumulative_energy_time_ms(xn: np.ndarray, peak_idx: int, sr: int,
                                pct: float) -> float:
    e = xn * xn
    cumul = np.cumsum(e)
    total = float(cumul[-1])
    if total <= 0:
        return float("nan")
    idx = int(np.searchsorted(cumul, total * pct))
    return 1000.0 * (idx - peak_idx) / sr


def analyze_ir(name: str, sr: int, x: np.ndarray) -> IRStats:
    peak_idx = int(np.argmax(np.abs(x)))
    peak = float(np.abs(x[peak_idx]))
    xn = x / max(peak, 1e-12)
    env_db = _envelope_db(xn, sr)
    decay_60 = (_first_drop_below(env_db, peak_idx, -60) - peak_idx) * 1000.0 / sr
    decay_80 = (_first_drop_below(env_db, peak_idx, -80) - peak_idx) * 1000.0 / sr
    pre = xn[:peak_idx]
    pre_rms = float(np.sqrt(np.mean(pre * pre))) if pre.size else 0.0
    pre_db = 20 * np.log10(max(pre_rms, 1e-12))
    return IRStats(
        name=name, sr=sr, n_samples=len(x),
        peak_idx=peak_idx, peak_value=peak,
        decay_60_ms=decay_60, decay_80_ms=decay_80,
        energy_95_ms=_cumulative_energy_time_ms(xn, peak_idx, sr, 0.95),
        energy_99_ms=_cumulative_energy_time_ms(xn, peak_idx, sr, 0.99),
        energy_999_ms=_cumulative_energy_time_ms(xn, peak_idx, sr, 0.999),
        pre_peak_rms_db=pre_db,
    )


def print_summary(stats: list[IRStats]) -> None:
    for s in stats:
        peak_t_ms = 1000.0 * s.peak_idx / s.sr
        print(f"\n--- {s.name} ---")
        print(f"  sr={s.sr}  length={s.n_samples} samples "
              f"({1000.0 * s.n_samples / s.sr:.1f} ms)")
        print(f"  peak @ sample {s.peak_idx} ({peak_t_ms:.2f} ms from start, "
              f"|peak| = {s.peak_value:.4f})")
        print(f"  pre-peak RMS: {s.pre_peak_rms_db:+.1f} dB (relative to peak)")
        print(f"  envelope drops below −60 dB at peak + {s.decay_60_ms:.2f} ms")
        print(f"  envelope drops below −80 dB at peak + {s.decay_80_ms:.2f} ms")
        print(f"  cumulative energy: 95% by peak+{s.energy_95_ms:.2f} ms, "
              f"99% by peak+{s.energy_99_ms:.2f} ms, "
              f"99.9% by peak+{s.energy_999_ms:.2f} ms")


def make_figure(items: list[tuple[IRStats, np.ndarray, str]], out_path: Path,
                xlim_ms: tuple[float, float] = (-10.0, 90.0)) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(10, 7), sharex=True,
        gridspec_kw=dict(height_ratios=[1, 2]),
    )
    for stats, x, color in items:
        peak = float(np.abs(x[stats.peak_idx]))
        xn = x / max(peak, 1e-12)
        t_ms = (np.arange(len(x)) - stats.peak_idx) * 1000.0 / stats.sr
        ax1.plot(t_ms, xn, color=color, lw=0.6, alpha=0.8, label=stats.name)
        env_db = _envelope_db(xn, stats.sr)
        ax2.plot(t_ms, env_db, color=color, lw=1.0, label=stats.name)

    for y, lbl in [(-60, "−60 dB"), (-80, "−80 dB")]:
        ax2.axhline(y, ls=":", color="0.4", lw=0.7)
        ax2.text(xlim_ms[1] - 12, y + 1.5, lbl, color="0.4", fontsize=8)

    ax1.set_xlim(*xlim_ms)
    ax1.set_ylim(-1.05, 1.05)
    ax1.set_ylabel("amplitude (peak-norm)")
    ax1.set_title("Impulse-response time-domain comparison "
                  "(t = 0 at IR peak)")
    ax1.grid(True, alpha=0.3)
    ax1.legend(loc="upper right", fontsize=8)

    ax2.set_xlim(*xlim_ms)
    ax2.set_ylim(-100, 5)
    ax2.set_xlabel("time relative to peak (ms)")
    ax2.set_ylabel("1 ms RMS envelope (dB, peak-norm)")
    ax2.grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(str(out_path), dpi=110)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--converter-irs", type=Path, default=None,
                    help="Path to converter-generated .irs (FIR file)")
    ap.add_argument("--ee-ir", type=Path, default=None,
                    help="EE-captured IR from analyze.py (ir_sweep_<label>_L.wav)")
    ap.add_argument("--dax-ir", type=Path, default=None,
                    help="DAX-captured IR from analyze.py")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output PNG path (omit for numeric summary only)")
    ap.add_argument("--xlim", nargs=2, type=float, default=(-10.0, 90.0),
                    metavar=("LO_MS", "HI_MS"),
                    help="X-axis range in ms (default −10 to +90)")
    args = ap.parse_args()

    inputs: list[tuple[Path, str, str]] = []
    if args.converter_irs is not None:
        inputs.append((args.converter_irs,
                       f"converter FIR ({args.converter_irs.name})", "C0"))
    if args.ee_ir is not None:
        inputs.append((args.ee_ir,
                       f"EE-captured IR ({args.ee_ir.name})", "C2"))
    if args.dax_ir is not None:
        inputs.append((args.dax_ir,
                       f"DAX-captured IR ({args.dax_ir.name})", "C3"))
    if not inputs:
        print("ERROR: pass at least one of --converter-irs / --ee-ir / --dax-ir",
              file=sys.stderr)
        return 2

    items: list[tuple[IRStats, np.ndarray, str]] = []
    stats: list[IRStats] = []
    for path, label, color in inputs:
        sr, x = load_ir(path)
        s = analyze_ir(label, sr, x)
        items.append((s, x, color))
        stats.append(s)

    print_summary(stats)

    if args.out is not None:
        make_figure(items, args.out, xlim_ms=tuple(args.xlim))
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
