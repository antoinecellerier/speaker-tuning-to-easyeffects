#!/usr/bin/env python3
"""Score bass-burst captures against a device's measured DAX response
(issue #14 — the `r-dax-virtual-bass` phase-2 protocol, committed).

The exploration harness this ports scored the shipped `--enable
virtual-bass` chain; this tool re-runs the same arithmetic on any capture
set, so a contributor's device can be judged with the numbers the chain
shipped on. Inputs are loopback captures of the SAME bass-burst stimulus
(tools/measure_dax/make_stimulus.py):

    python3 tools/measure_ee/score_vbe_chain.py \\
        --dax-capture loopback_bass_burst_dax_dynamic.wav \\
        --dry-capture loopback_bass_burst_chain_off.wav \\
        --capture vbe=loopback_bass_burst_chain_on.wav

`--dax-capture` is the reference (their Windows DAX battery,
tools/measure_dax/); `--capture LABEL=PATH` (repeatable) are the chains to
score (e.g. the PipeWire conf with the virtual-bass branch built in,
captured via tools/measure_pw/capture_battery.py); `--dry-capture` is the
processing-off run — it anchors the score (S of doing nothing) and enables
the fundamental / mud-integral guards.

S = macro-averaged per-tone |error| in dB across the 12 scored harmonic
cells, both sides clamped at −80 dBFS, overshoot at or above 200 Hz
double-weighted (added mud is worse than missing sparkle). Calibration on
the dev X1 Yoga (research `r-dax-virtual-bass` phase 2): DAX scored
against its own table 0.07, doing nothing 10.01, the shipped chain ≈4.4,
the rejected Calf BassEnhancer approximation 18.4.

Guard subset: a live capture is the summed dry+wet signal, so the
wet-in-isolation half of G1 (wet fundamental ≤ dry −20 dB) cannot run
here — the exploration harness needed separately rendered arms for it.
What remains: G1 summed-fundamental preservation (vs --dry-capture),
G2 the 180 Hz source stays clean at 360 Hz, G3 mud cells ≤ DAX +6 dB and
the 200–469 Hz integral ≤ DAX +3 dB.

Timing, printed per capture but not scored: how many ms after its note
each odd product of the 50 and 80 Hz tones reaches half its steady level,
and how long it outlasts the note. S cannot see either, because its
windows skip each tone's edges.

Offline renders are a pre-screen; captures through a live chain are the
signal this scores (never adopt on offline numbers — CLAUDE.md).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT))
from tools.measure_ee.analyze_vbe_chain import (  # noqa: E402
    EDGE_SKIP_S, Stimulus, align, load_mono,
)

# The corpus-frozen XML mix band (`virtual-bass-mix-freqs` = "94,469", one
# value across every XML measured — research `r-dax-virtual-bass`).
MIX_LO, MIX_HI = 94.0, 469.0
MUD_LO = 200.0          # above here, overshoot double-weights and G3 patrols
FLOOR = -80.0           # capture floor clamp, both sides of every error

# The scored cells and the guard cell, as (source-tone Hz, harmonic). The
# schema is part of the protocol, not the device: cells where a harmonic
# product lands inside the mix band with room above the floor. (180, 2)
# is excluded from scoring and guarded instead — 360 Hz output from a
# 180 Hz source means the chain synthesized outside its own source band.
SCORE_CELLS = (
    (50, 2), (50, 3), (50, 4), (50, 5), (50, 7), (50, 9),
    (80, 2), (80, 3), (80, 4), (80, 5),
    (120, 2), (120, 3),
)
GUARD_CELL = (180, 2)
GUARD_TONES = (50.0, 80.0, 120.0, 180.0)


class CellBank:
    """Windowed complex spectra per tone, queried by frequency band.
    Reproduces analyze_vbe_chain.band_db exactly for a single signal
    (same Hanning window, ±5 Hz integration, steady-state windows)."""

    def __init__(self, signal: np.ndarray, stim: Stimulus):
        self.spec = {}
        self.scale = {}
        for i, tone in enumerate(stim.tones):
            w = stim.window(signal, i)
            n = len(w)
            win = np.hanning(n)
            self.spec[tone] = np.fft.rfft(w * win)
            self.scale[tone] = (2.0 / n / win.mean(),
                                np.fft.rfftfreq(n, 1 / stim.sr))

    def band_db(self, tone: float, lo: float, hi: float) -> float:
        scale, freqs = self.scale[tone]
        m = (freqs >= lo) & (freqs <= hi)
        amp = scale * np.abs(self.spec[tone][m])
        rms = np.sqrt(np.mean(amp ** 2)) / np.sqrt(2)
        return 20 * np.log10(rms) if rms > 0 else -np.inf

    def cell_db(self, tone: float, h: int) -> float:
        return self.band_db(tone, h * tone - 5, h * tone + 5)

    def mud_db(self, tone: float) -> float:
        return self.band_db(tone, MUD_LO, MIX_HI)


def score(bank: CellBank, dax: CellBank) -> tuple[float, float]:
    """(macro, flat): macro-averaged and flat-averaged |error| vs the DAX
    capture's own cell values — so the reference is the device under
    test's Windows behaviour, not the dev machine's table."""
    per_tone: dict[float, list[float]] = {}
    for tone, h in SCORE_CELLS:
        want = max(dax.cell_db(tone, h), FLOOR)
        got = max(bank.cell_db(tone, h), FLOOR)
        e = got - want
        err = abs(e) * (2.0 if (e > 0 and h * tone >= MUD_LO) else 1.0)
        per_tone.setdefault(tone, []).append(err)
    macro = float(np.mean([np.mean(v) for v in per_tone.values()]))
    flat = float(np.mean([e for v in per_tone.values() for e in v]))
    return macro, flat


def guards(bank: CellBank, dax: CellBank,
           dry: CellBank | None) -> list[str]:
    """The capture-runnable guard subset; returns failed guard names."""
    fails = []
    if dry is not None:
        for tone in GUARD_TONES:
            delta = bank.cell_db(tone, 1) - dry.cell_db(tone, 1)
            if abs(delta) > 1.0:
                fails.append(f"G1 fundamental moved @{tone:.0f} "
                             f"({delta:+.1f} dB)")
    if bank.cell_db(*GUARD_CELL) > FLOOR:
        fails.append("G2 180-source dirty (360 Hz)")
    for tone, h in SCORE_CELLS:
        if h * tone >= MUD_LO:
            want = max(dax.cell_db(tone, h), FLOOR)
            if bank.cell_db(tone, h) > want + 6:
                fails.append(f"G3 mud cell {h}x{tone:.0f}")
    for tone in GUARD_TONES:
        if bank.mud_db(tone) > dax.mud_db(tone) + 3:
            fails.append(f"G3 mud integral @{tone:.0f}")
    return fails


# Timing cells, reported beside S rather than scored: the steady-state
# windows above skip 0.3 s at each tone edge, so S is blind to when a
# product arrives. Odd harmonics of the in-source tones, where DAX's
# products clear the floor (research `r-vbe-dry-wet-timing`).
LAG_CELLS = ((50, 3), (50, 5), (80, 3), (80, 5))
LAG_BW_HZ = 10.0        # half-width of each envelope's band
LAG_HOLD_S = 0.15       # an onset counts once the envelope stays up this long
LAG_PRE_S = 0.2         # envelope context kept before each tone and
                        # before the next one


def _envelope(x: np.ndarray, freq: float, sr: int) -> np.ndarray:
    """Zero-phase band envelope, so the filter adds no lag of its own.
    The analytic-signal magnitude has no ripple at twice the band's
    frequency, which a short RMS window would at 50 Hz."""
    from scipy.signal import butter, hilbert, sosfiltfilt
    sos = butter(4, [freq - LAG_BW_HZ, freq + LAG_BW_HZ], "bandpass",
                 fs=sr, output="sos")
    return np.abs(hilbert(sosfiltfilt(sos, x)))


def _half_points(env: np.ndarray, stim: Stimulus,
                 pre: int) -> tuple[int, int] | None:
    """(onset, release) sample indices where the envelope crosses half its
    steady level: the first point it stays above for LAG_HOLD_S, and the
    last point it is above before falling for good. A dip mid-note is not a
    release. `pre` is where the tone starts in `env`. None under the floor,
    when it never holds above half, or when it outlasts the segment."""
    skip = int(EDGE_SKIP_S * stim.sr)
    tone = int(stim.tone_s * stim.sr)
    steady = float(np.median(env[pre + skip:pre + tone - skip]))
    if steady <= 0 or 20 * np.log10(steady) <= FLOOR:
        return None
    above = env > steady / 2
    hold = int(LAG_HOLD_S * stim.sr)
    held = np.flatnonzero(np.convolve(above.astype(int), np.ones(hold, int),
                                      "valid") == hold)
    if held.size == 0 or above[-1]:
        return None
    return int(held[0]), int(np.flatnonzero(above)[-1]) + 1


def timing_lags(signal: np.ndarray, stim: Stimulus, tone: float,
                h: int) -> tuple[float, float] | None:
    """(onset, release) lag in ms of harmonic `h` behind the fundamental
    of `tone`, both at their half-level points in the same aligned
    capture. Positive means the product arrives, or lingers, after the
    note. None when either envelope gives no clean reading."""
    idx = stim.tones.index(tone)
    slot = int(round((stim.pre_s + idx * (stim.tone_s + stim.gap_s))
                     * stim.sr))
    pre = min(int(LAG_PRE_S * stim.sr), slot)
    end = slot + int(round((stim.tone_s + stim.gap_s) * stim.sr))
    # The zero-phase filter smears both ways: it sags at its window's edge
    # and pulls the next tone's onset back into the gap. So read only up to
    # LAG_PRE_S before the next tone, and filter through to it.
    ext = signal[slot - pre:end]
    keep = len(ext) - int(LAG_PRE_S * stim.sr)

    def points(freq: float) -> tuple[int, int] | None:
        return _half_points(_envelope(ext, freq, stim.sr)[:keep], stim, pre)

    base, prod = points(tone), points(h * tone)
    if base is None or prod is None:
        return None
    to_ms = 1000.0 / stim.sr
    return (prod[0] - base[0]) * to_ms, (prod[1] - base[1]) * to_ms


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score bass-burst captures against a measured DAX "
                    "reference (issue #14; protocol in research "
                    "`r-dax-virtual-bass`). Lower S is closer; the "
                    "--dry-capture row is what doing nothing scores on "
                    "this device.")
    parser.add_argument("--capture", action="append", required=True,
                        metavar="LABEL=PATH",
                        help="chain capture to score, repeatable")
    parser.add_argument("--dax-capture", type=Path, required=True,
                        help="the device's Windows DAX bass-burst capture "
                             "(the reference)")
    parser.add_argument("--dry-capture", type=Path,
                        help="processing-off capture: prints the "
                             "doing-nothing anchor and enables the G1/G3 "
                             "integral guards")
    parser.add_argument(
        "--stimulus-json", type=Path,
        default=REPO_ROOT / "localresearch/measure_dax/stimulus_bass_burst.json",
        help="stimulus sidecar with the tone geometry")
    args = parser.parse_args()

    if not args.stimulus_json.is_file():
        sys.exit(f"stimulus sidecar not found: {args.stimulus_json} "
                 "(generate with tools/measure_dax/make_stimulus.py)")
    stim = Stimulus(args.stimulus_json)

    def aligned(path: Path) -> np.ndarray:
        return align(load_mono(path, stim.sr), stim)

    def bank_of(path: Path) -> CellBank:
        return CellBank(aligned(path), stim)

    def print_timing(label: str, signal: np.ndarray) -> None:
        cells = []
        for tone, h in LAG_CELLS:
            lags = timing_lags(signal, stim, float(tone), h)
            cells.append(f"{h}x{tone} " + ("no reading" if lags is None
                         else f"{lags[0]:+.0f}/{lags[1]:+.0f}"))
        print(f"  timing {label}, onset/release ms after the note: "
              + ", ".join(cells))

    dax_signal = aligned(args.dax_capture)
    dax = CellBank(dax_signal, stim)
    dry = bank_of(args.dry_capture) if args.dry_capture else None

    print("S = macro-averaged |error| vs the DAX capture over "
          f"{len(SCORE_CELLS)} harmonic cells (dB, floor {FLOOR:g}, "
          f"overshoot >={MUD_LO:g} Hz x2). Dev-device anchors: DAX-self "
          "0.07, doing nothing 10.01, shipped chain ~4.4.")
    print()
    print_timing("DAX", dax_signal)
    if dry is not None:
        s, flat = score(dry, dax)
        print(f"\ndry (doing nothing): S = {s:.2f}  (flat {flat:.2f})")

    for spec in args.capture:
        label, _, path = spec.partition("=")
        if not path:
            sys.exit(f"--capture wants LABEL=PATH, got: {spec}")
        signal = aligned(Path(path))
        bank = CellBank(signal, stim)
        s, flat = score(bank, dax)
        print(f"\n{label}: S = {s:.2f}  (flat {flat:.2f})")
        print_timing(label, signal)
        print(f"  {'cell':>7} {'Hz':>4} | {label:>8} {'DAX':>8} {'err':>6}")
        for tone, h in SCORE_CELLS:
            want = max(dax.cell_db(tone, h), FLOOR)
            got = max(bank.cell_db(tone, h), FLOOR)
            print(f"  {h:>2}x{tone:>4} {h * tone:>4} | {got:>+8.1f} "
                  f"{want:>+8.1f} {got - want:>+6.1f}")
        fails = guards(bank, dax, dry)
        if fails:
            for f in fails:
                print(f"  GUARD FAIL: {f}")
        else:
            missing = " (G1 skipped: no --dry-capture)" if dry is None else ""
            print(f"  guards: PASS{missing}")


if __name__ == "__main__":
    main()
