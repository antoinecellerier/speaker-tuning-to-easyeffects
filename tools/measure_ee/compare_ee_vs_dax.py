#!/usr/bin/env python3
"""Overlay EasyEffects-captured response on top of Windows DAX3 capture
of the same XML / profile, both already processed by
tools/measure_dax/analyze.py.

Inputs (the analyze.py outputs):

    --ee-dir <dir>       contains spectrum_*.npz / tones_*.npz / ir_*.wav
                         from `analyze.py` run on EE captures.
    --dax-dir <dir>      same, from DAX captures.

For each stimulus tag (sweep / pink / multitone, etc.) and channel
(L / R) common to both directories, draws an overlay of the two
recovered responses with a residual subplot (EE − DAX, dB).

Usage:

    python tools/measure_ee/compare_ee_vs_dax.py \\
        --ee-dir ~/dax-measure/ee_captures \\
        --dax-dir ~/dax-measure/captures \\
        --out-dir ~/dax-measure/three_way

Per-tag outputs:

    compare_ee_vs_dax_<tag>_<channel>.png
    compare_ee_vs_dax_<tag>_<channel>.txt   (per-band residual table)

If --xml/--profile/--curve are also passed, the analytical FIR target is
plotted as a third reference line (re-uses the same code paths
analyze.py uses to derive it from the XML).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _scan_dir(d: Path) -> dict[tuple[str, str, str], Path]:
    """Map (kind, tag, channel) -> file_path, for analyze.py's outputs."""
    out: dict[tuple[str, str, str], Path] = {}
    if not d.is_dir():
        return out
    for path in d.iterdir():
        m = re.match(r"^(spectrum|tones|ir)_([^_]+(?:_quiet)?)_(.+)_([LR])"
                     r"\.(npz|wav)$", path.name)
        if not m:
            continue
        kind, tag, _label, ch, _ext = m.groups()
        out[(kind, tag, ch)] = path
    return out


def _band_freqs() -> np.ndarray:
    return np.array([
        47, 141, 234, 328, 469, 656, 844, 1031, 1313, 1688,
        2250, 3000, 3750, 4688, 5813, 7125, 9000, 11250, 13875, 19688,
    ], dtype=float)


def _normalize(curve: np.ndarray, f: np.ndarray, ref_hz: float = 1000.0
               ) -> np.ndarray:
    """Subtract the value at ref_hz so both curves share a common zero."""
    return curve - float(np.interp(ref_hz, f, curve))


def _read_pink(npz_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Returns (f, eq_gain_db). Falls back to mag_db if eq_gain_db is empty."""
    z = np.load(str(npz_path))
    f = z["f"].astype(float)
    eq = z["eq_gain_db"].astype(float) if "eq_gain_db" in z.files else None
    if eq is None or not np.any(eq):
        eq = z["mag_db"].astype(float)
    return f, eq


def _read_tones(npz_path: Path) -> tuple[np.ndarray, np.ndarray]:
    z = np.load(str(npz_path))
    return z["freqs_hz"].astype(float), z["amp_db"].astype(float)


def _read_ir_mag(wav_path: Path, n_fft: int = 16384, sr: int = 48000
                 ) -> tuple[np.ndarray, np.ndarray]:
    sr_, ir = wavfile.read(str(wav_path))
    if ir.dtype != np.float32:
        if ir.dtype == np.int16:
            ir = ir.astype(np.float32) / 32768.0
        elif ir.dtype == np.int32:
            ir = ir.astype(np.float32) / 2147483648.0
        else:
            ir = ir.astype(np.float32)
    if ir.ndim > 1:
        ir = ir[:, 0]
    spectrum = np.fft.rfft(ir, n=n_fft)
    f = np.fft.rfftfreq(n_fft, 1.0 / sr_)
    mag = 20 * np.log10(np.maximum(np.abs(spectrum), 1e-12))
    return f, mag


def _band_table(f_ee: np.ndarray, c_ee: np.ndarray,
                f_dax: np.ndarray, c_dax: np.ndarray,
                bands: np.ndarray) -> str:
    rows = ["    {:>7}  {:>7}  {:>7}  {:>7}".format(
        "freq", "ee", "dax", "Δ"
    )]
    for fb in bands:
        ee = float(np.interp(fb, f_ee, c_ee))
        dax = float(np.interp(fb, f_dax, c_dax))
        rows.append(f"    {int(fb):>6} Hz  {ee:+6.2f}  {dax:+6.2f}  "
                    f"{ee - dax:+6.2f}")
    return "\n".join(rows)


def _residual_stats(f: np.ndarray, c_ee: np.ndarray, c_dax: np.ndarray
                    ) -> dict[str, float]:
    band = (f >= 200) & (f <= 18000)
    diff = c_ee[band] - c_dax[band]
    return {
        "max_abs": float(np.max(np.abs(diff))),
        "p95_abs": float(np.percentile(np.abs(diff), 95)),
        "rms": float(np.sqrt(np.mean(diff ** 2))),
        "median": float(np.median(diff)),
    }


def _maybe_plot(f: np.ndarray, c_ee: np.ndarray, c_dax: np.ndarray,
                title: str, png_path: Path, extra: tuple | None = None
                ) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(matplotlib unavailable — skipping plot)", file=sys.stderr)
        return False
    diff = c_ee - c_dax
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 7), sharex=True,
                                    gridspec_kw=dict(height_ratios=[2.5, 1]))
    ax1.semilogx(f, c_ee, label="EasyEffects capture", color="C0")
    ax1.semilogx(f, c_dax, label="DAX3 capture", color="C3")
    if extra is not None:
        f_x, c_x, lbl = extra
        ax1.semilogx(f_x, c_x, ":", color="0.4", lw=0.9, label=lbl)
    ax1.axvspan(200, 18000, color="0.95", alpha=0.5, zorder=-1)
    ax1.set_ylabel("dB (normalized at 1 kHz)")
    ax1.set_title(title)
    ax1.legend(loc="lower left", fontsize=8)
    ax1.grid(True, which="both", alpha=0.3)

    ax2.semilogx(f, diff, color="C2")
    ax2.axhline(0, lw=0.5, color="k")
    ax2.axvspan(200, 18000, color="0.95", alpha=0.5, zorder=-1)
    ax2.set_xlim(20, 24000)
    ax2.set_ylim(-6, 6)
    ax2.set_xlabel("Hz")
    ax2.set_ylabel("EE − DAX (dB)")
    ax2.grid(True, which="both", alpha=0.3)

    fig.tight_layout()
    fig.savefig(str(png_path), dpi=110)
    plt.close(fig)
    return True


def _ref_target(xml_path: Path, profile: str, curve: str, sr: int = 48000
                ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (band_freqs, target_db_L, target_db_R) using the same code
    path analyze.py uses (parse_xml + interpolate_curve_db)."""
    from dolby_to_easyeffects import (parse_xml, interpolate_curve_db,
                                      SAMPLE_RATE)
    if sr != SAMPLE_RATE:
        raise SystemExit(f"sr {sr} != converter SAMPLE_RATE {SAMPLE_RATE}")
    res = parse_xml(str(xml_path))
    freqs, curves, ieq_amount, ao_l, ao_r = res[:5]
    target_l = interpolate_curve_db(freqs, curves, ieq_amount, ao_l,
                                     profile, curve, channel="left")
    target_r = interpolate_curve_db(freqs, curves, ieq_amount, ao_r,
                                     profile, curve, channel="right")
    return np.asarray(freqs, dtype=float), target_l, target_r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ee-dir", type=Path, required=True,
                    help="analyze.py output dir for EE captures")
    ap.add_argument("--dax-dir", type=Path, required=True,
                    help="analyze.py output dir for DAX captures")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Output directory (default ee-dir)")
    ap.add_argument("--xml", type=Path, default=None,
                    help="Optional XML for analytical target overlay")
    ap.add_argument("--profile", default="dynamic")
    ap.add_argument("--curve", default="balanced")
    ap.add_argument("--norm-hz", type=float, default=1000.0,
                    help="Reference frequency at which both curves are "
                         "set to 0 dB (default 1000 Hz)")
    args = ap.parse_args()

    out_dir = args.out_dir or args.ee_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    ee_idx = _scan_dir(args.ee_dir)
    dax_idx = _scan_dir(args.dax_dir)
    if not ee_idx:
        print(f"no analyze.py outputs found in {args.ee_dir}", file=sys.stderr)
        return 2
    if not dax_idx:
        print(f"no analyze.py outputs found in {args.dax_dir}", file=sys.stderr)
        return 2

    common = sorted(set(ee_idx) & set(dax_idx))
    if not common:
        print("no overlapping (kind, tag, channel) tuples between dirs",
              file=sys.stderr)
        print(f"  ee:  {sorted(ee_idx)}")
        print(f"  dax: {sorted(dax_idx)}")
        return 2

    extra: tuple | None = None
    if args.xml is not None:
        bf, tgt_l, tgt_r = _ref_target(args.xml, args.profile, args.curve)

    bands = _band_freqs()
    summary_lines = [
        f"EE dir:  {args.ee_dir}",
        f"DAX dir: {args.dax_dir}",
        f"normalized at: {args.norm_hz:.0f} Hz",
        "",
    ]
    for kind, tag, ch in common:
        ee_path = ee_idx[(kind, tag, ch)]
        dax_path = dax_idx[(kind, tag, ch)]
        title = f"{kind}/{tag}, channel {ch}: EE vs DAX3"
        png = out_dir / f"compare_ee_vs_dax_{kind}_{tag}_{ch}.png"
        txt = out_dir / f"compare_ee_vs_dax_{kind}_{tag}_{ch}.txt"
        if kind == "spectrum":
            f_ee, c_ee = _read_pink(ee_path)
            f_dax, c_dax = _read_pink(dax_path)
        elif kind == "ir":
            f_ee, c_ee = _read_ir_mag(ee_path)
            f_dax, c_dax = _read_ir_mag(dax_path)
        elif kind == "tones":
            # Multitone: per-band amplitude — already discrete, plot as
            # markers on the same band axis.
            f_ee, c_ee = _read_tones(ee_path)
            f_dax, c_dax = _read_tones(dax_path)
        else:
            continue

        c_ee_n = _normalize(c_ee, f_ee, args.norm_hz)
        c_dax_n = _normalize(c_dax, f_dax, args.norm_hz)

        # Resample DAX to EE's grid for residual computation
        if kind != "tones":
            c_dax_on_ee = np.interp(f_ee, f_dax, c_dax_n)
        else:
            c_dax_on_ee = c_dax_n  # already same band axis
        stats = _residual_stats(f_ee, c_ee_n, c_dax_on_ee)

        ex_for_plot: tuple | None = None
        if args.xml is not None and kind != "tones":
            tgt = tgt_l if ch == "L" else tgt_r
            tgt_norm = tgt - float(np.interp(args.norm_hz, bf, tgt))
            ex_for_plot = (bf, tgt_norm, "XML target")

        _maybe_plot(f_ee, c_ee_n, c_dax_on_ee, title, png, ex_for_plot)
        if kind == "tones":
            band_table = _band_table(f_ee, c_ee_n, f_dax, c_dax_n,
                                      bands=f_ee)
        else:
            band_table = _band_table(f_ee, c_ee_n, f_dax, c_dax_n, bands)

        summary_lines.append(f"=== {kind}/{tag} ch {ch} ===")
        summary_lines.append(f"  EE:  {ee_path.name}")
        summary_lines.append(f"  DAX: {dax_path.name}")
        summary_lines.append("")
        summary_lines.append(band_table)
        summary_lines.append("")
        summary_lines.append(
            f"  residual (200 Hz – 18 kHz): max |Δ| = {stats['max_abs']:.2f} "
            f"dB, p95 = {stats['p95_abs']:.2f} dB, RMS = {stats['rms']:.2f} dB, "
            f"median = {stats['median']:+.2f} dB"
        )
        summary_lines.append("")
        txt.write_text("\n".join([
            f"=== {kind}/{tag} ch {ch} ===",
            f"  EE:  {ee_path}",
            f"  DAX: {dax_path}",
            band_table,
            "",
            f"  residual (200 Hz – 18 kHz):",
            f"    max |Δ| = {stats['max_abs']:.2f} dB",
            f"    p95 |Δ| = {stats['p95_abs']:.2f} dB",
            f"    RMS Δ   = {stats['rms']:.2f} dB",
            f"    median  = {stats['median']:+.2f} dB",
        ]) + "\n")
        print(f"wrote {png}")

    summary_path = out_dir / "compare_ee_vs_dax_summary.txt"
    summary_path.write_text("\n".join(summary_lines) + "\n")
    print(f"\nwrote summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
