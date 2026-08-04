#!/usr/bin/env python3
"""DAX's volume-leveler gain as a function of input level (read-only).

The converter's autogain stage approximates Dolby's volume leveler with
constants that were chosen, not decoded (docs/design-notes.md,
"Unvalidated converter scaling factors", entries 7 and 10). This reads a
pink-noise *level ladder* of DAX captures and reports the thing those
constants are approximating: how much gain DAX applies at each input level,
measured as DAX-on minus DAX-off at that same level.

It needs no capture of its own — point it at a directory of analyze.py
outputs. Rungs are discovered from the filenames, so a two-rung archive
reports two points and a seven-rung session reports seven.

    python3 tools/measure_ee/leveler_curve.py --dax-dir <analyze.py outputs>

Levels come from each stimulus's own sidecar where one is reachable, so the
x-axis is the real input RMS rather than the number in the filename.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

# The ladder's tags carry their level with no separator (stimulus_pink30 ->
# tag "pink30") because the analysis filename grammar treats the first
# underscore-free token as the stimulus tag. "pink" and "pink_quiet" are the
# two rungs that predate the ladder and keep their original names.
_LADDER_RE = re.compile(r"^pink(\d{2})?(_quiet)?$")


def _tag_level(tag: str, search_dirs: list[Path]) -> float | None:
    """Input RMS in dBFS for a ladder tag, preferring the stimulus sidecar."""
    for d in search_dirs:
        side = d / f"stimulus_{tag}.json"
        if side.exists():
            try:
                meta = json.loads(side.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            for key in ("level_dbfs_rms", "level_dbfs_peak"):
                if key in meta:
                    return float(meta[key])
    # Fall back to the name. Only the ladder rungs encode it; the two
    # original names do not, and guessing their level from the filename
    # would be inventing data.
    m = _LADDER_RE.match(tag)
    if m and m.group(1):
        return -float(m.group(1))
    return None


def _band_mean(on: Path, off: Path, lo: float, hi: float) -> tuple[float, float, float] | None:
    """(mean, median, span) of on-minus-off over a frequency window, in dB."""
    zon, zoff = np.load(on), np.load(off)
    if "eq_gain_db_raw" not in zon or "eq_gain_db_raw" not in zoff:
        return None                      # analysed before absolute levels
    f = zon["f"]
    d = zon["eq_gain_db_raw"] - zoff["eq_gain_db_raw"]
    band = (f > lo) & (f < hi)
    if not band.any():
        return None
    d = d[band]
    return float(np.mean(d)), float(np.median(d)), float(np.max(d) - np.min(d))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dax-dir", required=True, type=Path,
                    help="analyze.py output dir holding spectrum_*_{on,off}_*.npz")
    ap.add_argument("--profile", default="dynamic",
                    help="DAX profile label to treat as 'on' (default: dynamic)")
    ap.add_argument("--channel", default="L", choices=["L", "R"])
    ap.add_argument("--band", nargs=2, type=float, default=(100.0, 10000.0),
                    metavar=("LO", "HI"),
                    help="frequency window to average over (default 100 10000)")
    args = ap.parse_args()

    lo, hi = args.band
    search = [args.dax_dir, args.dax_dir.parent]
    rows = []
    for on in sorted(args.dax_dir.glob(f"spectrum_*_{args.profile}_{args.channel}.npz")):
        tag = on.name[len("spectrum_"):-len(f"_{args.profile}_{args.channel}.npz")]
        if not _LADDER_RE.match(tag):
            continue
        off = args.dax_dir / f"spectrum_{tag}_off_{args.channel}.npz"
        if not off.exists():
            print(f"  {tag}: no OFF baseline at this rung — skipped "
                  f"(capture --label off for stimulus_{tag}.wav)", file=sys.stderr)
            continue
        stats = _band_mean(on, off, lo, hi)
        if stats is None:
            print(f"  {tag}: no absolute data — re-run analyze.py on the "
                  "captures with the stimulus wav reachable", file=sys.stderr)
            continue
        level = _tag_level(tag, search)
        rows.append((level, tag, *stats))

    if not rows:
        print("no ladder rungs found", file=sys.stderr)
        return 1

    # Unknown levels sort last rather than crashing the sort.
    rows.sort(key=lambda r: (r[0] is None, r[0]))
    print(f"DAX '{args.profile}' minus 'off', {lo:.0f}-{hi:.0f} Hz, channel {args.channel}")
    print(f"{'input dBFS':>11} {'rung':>12} {'mean dB':>9} {'median dB':>10} {'span dB':>9}")
    print("-" * 55)
    for level, tag, mean, med, span in rows:
        lvl = f"{level:+11.1f}" if level is not None else f"{'?':>11}"
        print(f"{lvl} {tag:>12} {mean:+9.2f} {med:+10.2f} {span:9.2f}")

    known = [(r[0], r[2]) for r in rows if r[0] is not None]
    if len(known) >= 2:
        (x0, g0), (x1, g1) = known[0], known[-1]
        print(f"\n{len(known)} rungs spanning {x0:+.1f} to {x1:+.1f} dBFS: "
              f"gain falls {g0 - g1:+.2f} dB across it "
              f"({(g0 - g1) / (x1 - x0):+.2f} dB per dB of input).")
        if len(known) == 2:
            print("Two rungs is a line, not a curve — the ladder in "
                  "tools/measure_dax/CLAUDE_WINDOWS.md fills it in.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
