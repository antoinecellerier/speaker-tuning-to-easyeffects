#!/usr/bin/env python3
"""Harmonic tables for virtual-bass chain renders/captures (issue #14).

Committed port of the Finding 8 analysis: per-tone harmonic magnitudes,
Δ3/Δ5/Δ7 vs the fundamental, the 3rd-vs-2nd odd/even ratio, and crest
factor, on any set of WAVs that played the bass-burst stimulus. The
metric definitions (Hanning-windowed FFT, ±5 Hz band integration, 2.4 s
steady-state window skipping 0.3 s at each tone edge) are kept identical
to the 2026-05-06 investigation so numbers stay comparable across
sessions.

Inputs are labeled WAVs — offline chain renders (render_vbe_chain.py),
DAX loopback captures, live EE captures, summed mixes — e.g.:

    python3 tools/measure_ee/analyze_vbe_chain.py \\
        --wav final=.../vbe_final.wav --wav dax=.../loopback_bass_burst.wav \\
        --offset-align dax

Offline renders are a pre-screen; only measured on-device captures
(via the /audio-validate flow) validate a change.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
from _wavio import read as wav_read  # noqa: E402

HARMONICS = (1, 2, 3, 4, 5, 7, 9)
EDGE_SKIP_S = 0.3  # skipped at each end of a tone for the steady-state window


class Stimulus:
    def __init__(self, sidecar: Path):
        meta = json.loads(sidecar.read_text())
        self.sr = int(meta.get("sample_rate_hz",
                          meta.get("sample_rate")))
        self.tones = [float(f) for f in meta["tone_freqs_hz"]]
        self.tone_s = float(meta["tone_duration_s"])
        self.gap_s = float(meta["gap_duration_s"])
        self.pre_s = float(meta["pre_silence_s"])

    def window(self, signal: np.ndarray, idx: int) -> np.ndarray:
        start_s = self.pre_s + idx * (self.tone_s + self.gap_s) + EDGE_SKIP_S
        use_s = self.tone_s - 2 * EDGE_SKIP_S
        start = int(round(start_s * self.sr))
        return signal[start:start + int(round(use_s * self.sr))]


def load_mono(path: Path, sr: int) -> np.ndarray:
    rate, data = wav_read(str(path))
    if rate != sr:
        sys.exit(f"{path}: sample rate {rate} != stimulus {sr}")
    mono = (data[:, 0] if data.ndim == 2 else data).astype(np.float64)
    if data.dtype == np.int16:
        mono /= 32768.0
    elif data.dtype == np.int32:
        mono /= 2147483648.0
    return mono


def align(signal: np.ndarray, stim: Stimulus) -> np.ndarray:
    """Shift a live capture so its first onset lands at pre_silence_s.
    Sub-window accuracy is irrelevant: each tone contributes a long
    steady-state window."""
    peak = np.abs(signal).max()
    if peak == 0:
        return signal
    onset = int(np.argmax(np.abs(signal) > 0.05 * peak))
    want = int(round(stim.pre_s * stim.sr))
    return signal[onset - want:] if onset >= want \
        else np.concatenate([np.zeros(want - onset), signal])


def band_db(window: np.ndarray, freq: float, sr: int,
            half_bw_hz: float = 5.0) -> float:
    n = len(window)
    win = np.hanning(n)
    coh = win.mean()
    spec = np.fft.rfft(window * win)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    mask = (freqs >= freq - half_bw_hz) & (freqs <= freq + half_bw_hz)
    if not mask.any():
        return -np.inf
    amp = (2.0 / n / coh) * np.abs(spec[mask])
    rms_lin = np.sqrt(np.mean(amp ** 2)) / np.sqrt(2)
    return 20 * np.log10(rms_lin) if rms_lin > 0 else -np.inf


def table(title: str, stim: Stimulus, sigs: dict[str, np.ndarray],
          cell) -> None:
    print(title)
    print(f"  {'tone':>5} |" + "".join(f" {label:>9}" for label in sigs))
    for i, tone in enumerate(stim.tones):
        row = f"  {tone:>5.0f} |"
        for sig in sigs.values():
            row += f" {cell(stim.window(sig, i), tone):>+9.1f}"
        print(row)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Harmonic tables for bass-burst WAVs (issue #14). "
                    "Offline inputs are a pre-screen, not a validation.")
    parser.add_argument("--wav", action="append", required=True,
                        metavar="LABEL=PATH",
                        help="labeled input, repeatable; column order = "
                             "argument order")
    parser.add_argument(
        "--stimulus-json", type=Path,
        default=REPO_ROOT / "localresearch/measure_dax/stimulus_bass_burst.json",
        help="stimulus sidecar with the tone geometry")
    parser.add_argument("--offset-align", action="append", default=[],
                        metavar="LABEL",
                        help="onset-align this label (live captures), "
                             "repeatable")
    args = parser.parse_args()

    if not args.stimulus_json.is_file():
        sys.exit(f"stimulus sidecar not found: {args.stimulus_json} "
                 "(generate with tools/measure_dax/make_stimulus.py)")
    stim = Stimulus(args.stimulus_json)

    sigs: dict[str, np.ndarray] = {}
    for spec in args.wav:
        label, _, path = spec.partition("=")
        if not path:
            sys.exit(f"--wav wants LABEL=PATH, got: {spec}")
        sigs[label] = load_mono(Path(path), stim.sr)
    for label in args.offset_align:
        if label not in sigs:
            sys.exit(f"--offset-align {label}: no such --wav label")
        sigs[label] = align(sigs[label], stim)

    print("Per-tone harmonic magnitudes (dBFS), "
          f"channel L, harmonics {HARMONICS}:")
    head = f"  {'tone':>5} {'h':>2} |" + "".join(
        f" {label:>9}" for label in sigs)
    print(head)
    print("  " + "-" * (len(head) - 2))
    for i, tone in enumerate(stim.tones):
        for h in HARMONICS:
            row = f"  {tone:>5.0f} {h:>2} |"
            for sig in sigs.values():
                row += f" {band_db(stim.window(sig, i), h * tone, stim.sr):>+9.1f}"
            print(row)
        print()

    for h, nth in ((3, "3rd"), (5, "5th"), (7, "7th")):
        table(f"Δ{h} ({nth}-harmonic level relative to fundamental, dB):",
              stim, sigs,
              lambda w, tone, h=h: band_db(w, h * tone, stim.sr)
              - band_db(w, tone, stim.sr))

    table("3rd-vs-2nd ratio (dB, positive = odd-dominated):", stim, sigs,
          lambda w, tone: band_db(w, 3 * tone, stim.sr)
          - band_db(w, 2 * tone, stim.sr))

    table("Crest factor (peak/RMS, dB):", stim, sigs,
          lambda w, tone: 20 * np.log10(
              np.abs(w).max() / (np.sqrt(np.mean(w ** 2)) + 1e-12) + 1e-12))


if __name__ == "__main__":
    main()
