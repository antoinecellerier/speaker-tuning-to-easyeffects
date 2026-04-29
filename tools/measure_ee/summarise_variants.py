#!/usr/bin/env python3
"""Summarise a per-variant capture matrix produced by sweep_variants.sh.

For each (variant, profile) cell:
  1. Run analyze.py on the variant's loopback wavs to produce
     spectrum / tones / ir npz / wav alongside.
  2. Compute two residuals at the pink-noise tag:
       - **EE − XML target** (the "is our chain faithful to the
         published XML?" metric — uses the same `build_reference`
         path analyze.py uses).
       - **EE − DAX captured** (the "do we match Windows DAX?"
         metric — Finding 6 already established this is floored
         by fixed-DAX behavior outside the XML, but worth tracking
         as a tie-breaker).
  3. Print a residual table covering the variant × profile matrix.

Usage:
    python tools/measure_ee/summarise_variants.py \\
        --xml localresearch/<DEV...>.xml \\
        --variant-base localresearch/measure_ee/variants \\
        --variants <label1> <label2> ... \\
        [--dax-dir localresearch/measure_dax/captures]
        [--profiles dynamic [movie ...]]
        [--curve balanced]
        [--channels L]
        [--norm-hz 1000]
        [--skip-analyze]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]


def _normalize(curve: np.ndarray, f: np.ndarray, ref_hz: float = 1000.0
               ) -> np.ndarray:
    return curve - float(np.interp(ref_hz, f, curve))


def _read_pink_npz(path: Path) -> tuple[np.ndarray, np.ndarray]:
    z = np.load(str(path))
    f = z["f"].astype(float)
    if "eq_gain_db" in z.files and np.any(z["eq_gain_db"]):
        eq = z["eq_gain_db"].astype(float)
    else:
        eq = z["mag_db"].astype(float)
    return f, eq


def _residual_stats(f: np.ndarray, ee: np.ndarray, ref: np.ndarray,
                    lo: float = 200.0, hi: float = 18000.0
                    ) -> tuple[float, float, float]:
    """Return (rms, p95, max) of |EE-ref| in dB, in [lo, hi] Hz."""
    band = (f >= lo) & (f <= hi)
    diff = ee[band] - ref[band]
    return (float(np.sqrt(np.mean(diff ** 2))),
            float(np.percentile(np.abs(diff), 95)),
            float(np.max(np.abs(diff))))


def _run_analyze(wavs: list[Path], xml: Path, profile: str, curve: str
                 ) -> None:
    """Run analyze.py on a batch of wavs (writes spectrum/tones/ir
    alongside)."""
    if not wavs:
        return
    cmd = [
        sys.executable,
        str(REPO_ROOT / "tools" / "measure_dax" / "analyze.py"),
        *[str(p) for p in wavs],
        "--xml", str(xml),
        "--profile", profile,
        "--curve", curve,
    ]
    subprocess.run(cmd, check=False, capture_output=True)


def _xml_target(xml: Path, profile: str, curve: str
                ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (band_freqs, target_db_L, target_db_R) via build_reference."""
    sys.path.insert(0, str(REPO_ROOT / "tools" / "measure_dax"))
    from analyze import build_reference  # noqa: E402
    ref = build_reference(xml, profile, curve)
    return ref.band_freqs, ref.target_db_L, ref.target_db_R


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xml", type=Path, required=True)
    ap.add_argument("--variant-base", type=Path, required=True,
                    help="parent of <label>/ subdirs")
    ap.add_argument("--variants", nargs="+", required=True,
                    help="variant labels to summarise (must match the "
                         "subdir names under --variant-base)")
    ap.add_argument("--profiles", nargs="+", default=["dynamic"])
    ap.add_argument("--curve", default="balanced")
    ap.add_argument("--dax-dir", type=Path, default=None,
                    help="DAX captures dir (analyze.py outputs). When "
                         "given, also computes the EE-vs-DAX residual.")
    ap.add_argument("--channels", nargs="+", default=["L"],
                    help="Channels to summarise (DAX captures are L-only "
                         "via WASAPI loopback on the test rig).")
    ap.add_argument("--norm-hz", type=float, default=1000.0)
    ap.add_argument("--skip-analyze", action="store_true",
                    help="Skip analyze.py; assume spectrum_*.npz already "
                         "exists alongside the loopback wavs.")
    args = ap.parse_args()

    # Phase 1: run analyze.py on every variant/profile so spectrum_*.npz
    # and ir_*.wav exist
    if not args.skip_analyze:
        for variant in args.variants:
            vdir = args.variant_base / variant
            if not vdir.is_dir():
                print(f"missing variant dir: {vdir}", file=sys.stderr)
                continue
            for profile in args.profiles:
                wavs = sorted(vdir.glob(f"loopback_*_{variant}.wav"))
                # Filter to only the wavs whose label matches our (profile,
                # variant) pair. The capture_battery script writes
                # loopback_<tag>_<label>.wav where label is the same as
                # variant when sweep_variants.sh is used. For multi-profile
                # variant labels we'd need different behaviour; for now,
                # assume one profile per variant subdir (the sweep_variants
                # contract).
                if not wavs:
                    continue
                print(f"  analyzing {variant}/{profile} "
                      f"({len(wavs)} wavs)", file=sys.stderr)
                _run_analyze(wavs, args.xml, profile, args.curve)

    # Phase 2: compute EE-vs-XML and (optionally) EE-vs-DAX residuals
    rows = []
    for profile in args.profiles:
        bf, tgt_L, tgt_R = _xml_target(args.xml, profile, args.curve)
        for variant in args.variants:
            vdir = args.variant_base / variant
            for ch in args.channels:
                ee_npz = vdir / f"spectrum_pink_{variant}_{ch}.npz"
                if not ee_npz.is_file():
                    rows.append((profile, variant, ch, None, None,
                                 None, None))
                    continue
                f_ee, ee = _read_pink_npz(ee_npz)
                ee_n = _normalize(ee, f_ee, args.norm_hz)

                # EE vs XML target
                tgt = tgt_L if ch == "L" else tgt_R
                tgt_on_ee = np.interp(np.log(np.maximum(f_ee, 1.0)),
                                      np.log(bf), tgt,
                                      left=tgt[0], right=tgt[-1])
                tgt_n = _normalize(tgt_on_ee, f_ee, args.norm_hz)
                xml_rms, _, xml_max = _residual_stats(f_ee, ee_n, tgt_n)

                # EE vs DAX (optional)
                dax_rms = dax_max = None
                if args.dax_dir is not None:
                    dax_npz = (args.dax_dir
                               / f"spectrum_pink_{profile}_{ch}.npz")
                    if dax_npz.is_file():
                        f_dax, dax = _read_pink_npz(dax_npz)
                        dax_n = _normalize(dax, f_dax, args.norm_hz)
                        dax_on_ee = np.interp(f_ee, f_dax, dax_n)
                        dax_rms, _, dax_max = _residual_stats(
                            f_ee, ee_n, dax_on_ee)

                rows.append((profile, variant, ch,
                             xml_rms, xml_max, dax_rms, dax_max))

    # Wide table
    print()
    print(f"Pink-noise residual, normalized at {args.norm_hz:.0f} Hz, "
          "in-band 200–18000 Hz, dB:")
    print()
    if args.dax_dir is None:
        print(f"{'profile':<8} {'variant':<14} {'ch':<2} "
              f"{'vsXML rms':>10} {'vsXML max':>10}")
        print("-" * 50)
    else:
        print(f"{'profile':<8} {'variant':<14} {'ch':<2} "
              f"{'vsXML rms':>10} {'vsXML max':>10} "
              f"{'vsDAX rms':>10} {'vsDAX max':>10}")
        print("-" * 70)
    for row in rows:
        profile, variant, ch, x_rms, x_max, d_rms, d_max = row
        if x_rms is None:
            print(f"{profile:<8} {variant:<14} {ch:<2}  (missing)")
            continue
        line = (f"{profile:<8} {variant:<14} {ch:<2} "
                f"{x_rms:>10.2f} {x_max:>10.2f}")
        if args.dax_dir is not None:
            if d_rms is None:
                line += f" {'n/a':>10} {'n/a':>10}"
            else:
                line += f" {d_rms:>10.2f} {d_max:>10.2f}"
        print(line)

    # Compact "winner per profile" view, EE-vs-XML only
    print()
    chans_label = "/".join(args.channels)
    print(f"By profile, EE vs XML target RMS in dB "
          f"(lower = more faithful to XML), averaged over {chans_label}:")
    print()
    print(f"{'profile':<8} " +
          " ".join(f"{v:>14}" for v in args.variants))
    for profile in args.profiles:
        cells = []
        for variant in args.variants:
            vals = [r[3] for r in rows
                    if r[0] == profile and r[1] == variant
                    and r[3] is not None]
            cells.append(f"{np.mean(vals):>14.2f}" if vals
                         else f"{'n/a':>14}")
        print(f"{profile:<8} " + " ".join(cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
