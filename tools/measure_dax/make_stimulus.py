#!/usr/bin/env python3
"""Generate the full stimulus suite for DAX3 measurement.

Outputs (written to the current working directory), each with a
matching `.json` sidecar:

  LTI / steady-state magnitude probes (-18 dBFS, with -42 dBFS quiet
  controls to bracket the leveler's level dependence):
    stimulus_sweep[_quiet]       exp sweep (Farina); inverse_sweep.npy
                                 is the shared matched inverse
    stimulus_pink[_quiet]        stationary pink noise
    stimulus_multitone           summed tones at the 20 Dolby band centers
                                 (per-band Goertzel readout, no leakage)

  Stereo M/S probes (-18 dBFS):
    stimulus_stereo_pink         decorrelated L/R (M≈S, max entropy)
    stimulus_stereo_correlated   music-like (M≫S) operating point

  Dynamics-engaging probes — loud enough to cross the MBC / regulator /
  brickwall thresholds the -18/-42 battery leaves dormant:
    stimulus_bass_burst[_quiet]  sustained 50/80/120/180 Hz bursts at
                                 -5 / -25 dBFS peak — the bass-band
                                 regulator / volmax low-end probe
    stimulus_stepped[_quiet]     one held tone per probe frequency, grid
                                 repeated asc/desc/shuffled (-18/-42 dBFS)
    stimulus_stepped_loud        same grid at -2 dBFS peak — wakes the MBC knee
    stimulus_speech              espeak (or LTASS-noise fallback) — the
                                 Media-Intelligence dialog-enhancer probe

  Built only with --speech-source WAV (a recording of a real reader) or
  --speech-source librivox (downloads the clip the dev device used):
    stimulus_speech_human        the recording, centred
    stimulus_speech_over_bed     centred speech over an independent pink bed
                                 per channel, 10 dB down (side = bed only)
    stimulus_speech_then_pink    20 s speech, then 20 s pink at the same level
    stimulus_pink_then_speech    the reverse: 20 s pink, then 20 s speech

  IEQ timing and bandwidth probes (also alone with --ieq-only):
    stimulus_pink_steps          one pink stream at -18, -30, -18 dBFS, 30 s each
    stimulus_pink_bump_2250      pink with a +9 dB, ~0.4-octave bump at 2.25 kHz

The non-LTI dynamics (compressor, regulator, brickwall) only engage on
the loud bass_burst / stepped_loud variants; the LTI battery leaves them
dormant (see research `r-dynamics-dormancy`).

Stereo stimuli are L=R (centered mono) except the stereo_* probes;
analyze.py keys off each sidecar's `stereo_mode`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly

SR = 48000
# Write to the current working directory so users can keep stimuli
# (and the matching inverse filter / sidecar JSONs) wherever fits their
# workflow. The script itself lives under tools/ and is location-agnostic.
OUT_DIR = Path.cwd()

# Sweep parameters (shared by sweep and sweep_quiet).
SWEEP_T = 10.0
SWEEP_TAIL = 1.0
SWEEP_F0 = 20.0
SWEEP_F1 = 22000.0
SWEEP_FADE_MS = 50.0

# Stationary stimuli are 12 s active + 1 s tail; 12 s gives the leveler
# 5+ s to settle and 5+ s of clean stationary signal to average.
STEADY_T = 12.0
STEADY_TAIL = 1.0
STEADY_FADE_MS = 50.0

# Dolby's 20-band center frequencies (Hz) — hardcoded from the XML's
# constant/band_20_freq[fs_48000]; identical across all DAX3 tunings.
BAND_CENTERS = (
    47, 141, 234, 328, 469, 656, 844, 1031, 1313, 1688,
    2250, 3000, 3750, 4688, 5813, 7125, 9000, 11250, 13875, 19688,
)


# ----- sweep -----

def _sweep_signal() -> np.ndarray:
    """Unit-peak exponential sweep, single channel, fade-in/out applied."""
    n = int(round(SWEEP_T * SR))
    t = np.arange(n) / SR
    L = SWEEP_T / np.log(SWEEP_F1 / SWEEP_F0)
    K = 2.0 * np.pi * SWEEP_F0 * L
    sweep = np.sin(K * (np.exp(t / L) - 1.0))
    fade_n = int(round(SWEEP_FADE_MS * 1e-3 * SR))
    fade = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_n) / fade_n))
    sweep[:fade_n] *= fade
    sweep[-fade_n:] *= fade[::-1]
    return sweep.astype(np.float32)


def _sweep_inverse(sweep: np.ndarray) -> np.ndarray:
    """Closed-form matched inverse: time-reversed sweep with exp envelope
    boosting the (now-leading) high-frequency portion. See the docstring
    on the prior version for the derivation."""
    n = sweep.size
    t = np.arange(n) / SR
    L = SWEEP_T / np.log(SWEEP_F1 / SWEEP_F0)
    envelope = np.exp((SWEEP_T - t) / L)
    inv = sweep[::-1] * envelope
    inv /= np.max(np.abs(inv))
    return inv.astype(np.float32)


def make_sweep(level_dbfs_peak: float) -> tuple[np.ndarray, dict]:
    sweep = _sweep_signal()
    sweep *= (10 ** (level_dbfs_peak / 20.0)) / np.max(np.abs(sweep))
    tail = np.zeros(int(round(SWEEP_TAIL * SR)), dtype=np.float32)
    mono = np.concatenate([sweep, tail])
    stereo = np.column_stack([mono, mono])
    meta = {
        "kind": "sweep",
        "sample_rate": SR,
        "duration_seconds": SWEEP_T + SWEEP_TAIL,
        "active_seconds": SWEEP_T,
        "tail_seconds": SWEEP_TAIL,
        "f0_hz": SWEEP_F0,
        "f1_hz": SWEEP_F1,
        "level_dbfs_peak": level_dbfs_peak,
        "fade_ms": SWEEP_FADE_MS,
        "stimulus_samples": int(stereo.shape[0]),
        "active_samples": int(sweep.size),
        "tail_samples": int(tail.size),
        "format": "float32 stereo L=R",
        "stereo_mode": "symmetric",
        "inverse_filter": "inverse_sweep.npy",
    }
    return stereo, meta


# ----- pink noise -----

def _pink_noise(n_samples: int, seed: int = 0) -> np.ndarray:
    """Pink noise via 1/sqrt(f) shaping of white Gaussian noise.

    Deterministic given the seed. The DC bin is zeroed; the Nyquist bin
    is preserved. The result is real-valued and has approximately unit
    RMS before scaling."""
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(n_samples).astype(np.float32)
    H = np.fft.rfft(white)
    n_bins = H.size
    # 1/sqrt(f) shaping. Skip the DC bin (k=0) to avoid div-by-zero.
    k = np.arange(n_bins, dtype=np.float64)
    shaping = np.zeros(n_bins)
    shaping[1:] = 1.0 / np.sqrt(k[1:])
    H_pink = H * shaping
    pink = np.fft.irfft(H_pink, n=n_samples).astype(np.float32)
    pink /= float(np.sqrt(np.mean(pink ** 2)) + 1e-12)
    return pink


def make_pink(level_dbfs_rms: float, seed: int = 0
              ) -> tuple[np.ndarray, dict]:
    n_active = int(round(STEADY_T * SR))
    pink = _pink_noise(n_active, seed=seed)
    pink *= 10 ** (level_dbfs_rms / 20.0)
    fade_n = int(round(STEADY_FADE_MS * 1e-3 * SR))
    fade = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_n) / fade_n))
    pink[:fade_n] *= fade
    pink[-fade_n:] *= fade[::-1]
    tail = np.zeros(int(round(STEADY_TAIL * SR)), dtype=np.float32)
    mono = np.concatenate([pink, tail])
    stereo = np.column_stack([mono, mono]).astype(np.float32)
    peak = float(np.max(np.abs(stereo)))
    if peak >= 1.0:
        # rare, but rescale to leave 0.5 dB headroom
        stereo *= (10 ** (-0.5 / 20.0)) / peak
    meta = {
        "kind": "pink",
        "sample_rate": SR,
        "duration_seconds": STEADY_T + STEADY_TAIL,
        "active_seconds": STEADY_T,
        "tail_seconds": STEADY_TAIL,
        "level_dbfs_rms": level_dbfs_rms,
        "fade_ms": STEADY_FADE_MS,
        "stimulus_samples": int(stereo.shape[0]),
        "active_samples": int(n_active),
        "tail_samples": int(tail.size),
        "seed": seed,
        "format": "float32 stereo L=R",
        "stereo_mode": "symmetric",
        # Reference window for the analyze step: skip the leveler's settling
        # transient at the start; analyze the last ~5 s of the stationary part.
        "analysis_window_start_seconds": 6.0,
        "analysis_window_end_seconds": 11.0,
    }
    return stereo, meta


# Level steps and a spectral bump, both for DAX's IEQ (research
# `r-ieq-adapts-to-content`). The steps change level with the spectrum fixed:
# an IEQ that tracks spectral shape should not move, and a fast-rise /
# slow-fall level tracker would show slow adaptation after the drop and fast
# after the rise. The bump reads how wide the IEQ's correction is around one
# spectral feature.
PINK_STEPS_DBFS = (-18.0, -30.0, -18.0)
PINK_STEP_S = 30.0
PINK_STEP_RAMP_MS = 50.0
PINK_BUMP_HZ = 2250.0
PINK_BUMP_DB = 9.0
PINK_BUMP_SIGMA_OCT = 1.0 / 6.0   # Gaussian in log-frequency, ~0.4 oct FWHM
PINK_BUMP_T = 20.0


def make_pink_steps(levels_dbfs: tuple = PINK_STEPS_DBFS,
                    step_s: float = PINK_STEP_S, seed: int = 0
                    ) -> tuple[np.ndarray, dict]:
    """One continuous pink stream whose level steps between segments.

    Each level is the gain on unit-RMS pink, as in make_pink."""
    n_step = int(round(step_s * SR))
    pink = _pink_noise(n_step * len(levels_dbfs), seed=seed)
    gain = np.concatenate([np.full(n_step, 10 ** (lv / 20.0)) for lv in levels_dbfs])
    ramp_n = int(round(PINK_STEP_RAMP_MS * 1e-3 * SR))
    if ramp_n > 1:   # short linear ramps at each step, so no click
        kernel = np.ones(ramp_n) / ramp_n
        gain = np.convolve(gain, kernel, mode="same")
    mono = pink * gain
    fade_n = int(round(STEADY_FADE_MS * 1e-3 * SR))
    fade = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_n) / fade_n))
    mono[:fade_n] *= fade
    mono[-fade_n:] *= fade[::-1]
    tail = np.zeros(int(round(STEADY_TAIL * SR)))
    mono = np.concatenate([mono, tail])
    peak = float(np.max(np.abs(mono)))
    if peak >= 1.0:
        mono *= (10 ** (-0.5 / 20.0)) / peak
    stereo = np.column_stack([mono, mono]).astype(np.float32)
    meta = {
        "kind": "pink_steps",
        "sample_rate": SR,
        "duration_seconds": stereo.shape[0] / SR,
        "tail_seconds": STEADY_TAIL,
        "segments": [[i * step_s, (i + 1) * step_s, lv]
                     for i, lv in enumerate(levels_dbfs)],
        "ramp_ms": PINK_STEP_RAMP_MS,
        "seed": seed,
        "stimulus_samples": int(stereo.shape[0]),
        "format": "float32 stereo L=R",
        "stereo_mode": "symmetric",
    }
    return stereo, meta


def make_pink_bump(level_dbfs_rms: float = -18.0, bump_hz: float = PINK_BUMP_HZ,
                   bump_db: float = PINK_BUMP_DB,
                   sigma_oct: float = PINK_BUMP_SIGMA_OCT,
                   duration_s: float = PINK_BUMP_T, seed: int = 0
                   ) -> tuple[np.ndarray, dict]:
    """Pink with a Gaussian (log-frequency) bump of bump_db at bump_hz."""
    n = int(round(duration_s * SR))
    pink = _pink_noise(n, seed=seed)
    spec = np.fft.rfft(pink)
    f = np.fft.rfftfreq(n, 1.0 / SR)
    octs = np.zeros_like(f)
    octs[1:] = np.log2(f[1:] / bump_hz)
    shape_db = bump_db * np.exp(-0.5 * (octs / sigma_oct) ** 2)
    shape_db[0] = 0.0
    mono = np.fft.irfft(spec * 10.0 ** (shape_db / 20.0), n)
    rms = float(np.sqrt(np.mean(mono ** 2))) + 1e-30
    mono *= 10.0 ** (level_dbfs_rms / 20.0) / rms
    fade_n = int(round(STEADY_FADE_MS * 1e-3 * SR))
    fade = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_n) / fade_n))
    mono[:fade_n] *= fade
    mono[-fade_n:] *= fade[::-1]
    mono = np.concatenate([mono, np.zeros(int(round(STEADY_TAIL * SR)))])
    peak = float(np.max(np.abs(mono)))
    if peak >= 1.0:
        mono *= (10 ** (-0.5 / 20.0)) / peak
    stereo = np.column_stack([mono, mono]).astype(np.float32)
    meta = {
        "kind": "pink_bump",
        "sample_rate": SR,
        "duration_seconds": stereo.shape[0] / SR,
        "tail_seconds": STEADY_TAIL,
        "level_dbfs_rms": level_dbfs_rms,
        "bump_hz": bump_hz,
        "bump_db": bump_db,
        "bump_sigma_octaves": sigma_oct,
        "seed": seed,
        "analysis_window_start_seconds": 10.0,
        "analysis_window_end_seconds": duration_s - 1.0,
        "stimulus_samples": int(stereo.shape[0]),
        "format": "float32 stereo L=R",
        "stereo_mode": "symmetric",
    }
    return stereo, meta


def make_stereo_pink(level_dbfs_rms: float, seed_l: int = 100,
                     seed_r: int = 101) -> tuple[np.ndarray, dict]:
    """Decorrelated pink-noise stereo: two independent pink streams on L
    and R. Used for stereo-aspect equivalence testing — symmetric stimuli
    (the rest of the battery) reduce to identity through any M/S widener
    because S = (L-R)/2 = 0, so a stereo_tools bug that affects only the
    Side component is invisible. Independent seeds guarantee a non-trivial
    M *and* S spectrum, so per-channel comparison can catch any asymmetric
    chain divergence.

    Caveat: M ≈ S ≈ 70% of either channel, which is *not* the operating
    point natural music sits at (real recordings have M ≫ S, with the
    widener designed for that regime). For a music-like test, see
    ``make_stereo_correlated_pink``.
    """
    n_active = int(round(STEADY_T * SR))
    pink_l = _pink_noise(n_active, seed=seed_l)
    pink_r = _pink_noise(n_active, seed=seed_r)
    pink_l *= 10 ** (level_dbfs_rms / 20.0)
    pink_r *= 10 ** (level_dbfs_rms / 20.0)
    fade_n = int(round(STEADY_FADE_MS * 1e-3 * SR))
    fade = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_n) / fade_n))
    for ch in (pink_l, pink_r):
        ch[:fade_n] *= fade
        ch[-fade_n:] *= fade[::-1]
    tail = np.zeros(int(round(STEADY_TAIL * SR)), dtype=np.float32)
    left = np.concatenate([pink_l, tail])
    right = np.concatenate([pink_r, tail])
    stereo = np.column_stack([left, right]).astype(np.float32)
    peak = float(np.max(np.abs(stereo)))
    if peak >= 1.0:
        stereo *= (10 ** (-0.5 / 20.0)) / peak
    meta = {
        "kind": "stereo_pink",
        "sample_rate": SR,
        "duration_seconds": STEADY_T + STEADY_TAIL,
        "active_seconds": STEADY_T,
        "tail_seconds": STEADY_TAIL,
        "level_dbfs_rms": level_dbfs_rms,
        "fade_ms": STEADY_FADE_MS,
        "stimulus_samples": int(stereo.shape[0]),
        "active_samples": int(n_active),
        "tail_samples": int(tail.size),
        "seed_left": seed_l,
        "seed_right": seed_r,
        "format": "float32 stereo L≠R",
        # Comparison drivers key off this field to switch from mono-sum
        # to per-channel diffing — the only way to detect a bug whose
        # signature lives in the M/S split (e.g. a wrong stereo-base
        # sign or an off-by-one channel swap in stereo_tools).
        "stereo_mode": "asymmetric",
        "analysis_window_start_seconds": 6.0,
        "analysis_window_end_seconds": 11.0,
    }
    return stereo, meta


def make_stereo_correlated_pink(
        level_dbfs_rms: float,
        side_ratio: float = 0.05,
        seed_main: int = 200,
        seed_side: int = 201) -> tuple[np.ndarray, dict]:
    """Music-like correlated stereo pink: a strong shared centre + a
    small per-channel decorrelation. With ``side_ratio = 0.05`` the
    Mid:Side amplitude ratio is ~32 dB, mimicking the operating point
    of natural music (instruments centred, modest stereo enrichment).

    Construction:

        L = main + side_ratio · side_l
        R = main − side_ratio · side_r

    where ``main`` is one pink stream and ``side_l/side_r`` are two
    independent pink streams. The decorrelated pink stimulus
    (``make_stereo_pink``) hits M ≈ S ≈ 70%, which is great for
    detecting LTI bugs but unrepresentative of where stereo_tools'
    widener is actually applied at runtime. This stimulus complements
    it: any bug whose visibility depends on signal correlation
    (e.g. nonlinear stages activating only at specific M/S ratios,
    or a widener whose error scales with |S|) shows up here in a way
    decorrelated pink would mask.
    """
    n_active = int(round(STEADY_T * SR))
    main = _pink_noise(n_active, seed=seed_main)
    side_l = _pink_noise(n_active, seed=seed_side)
    side_r = _pink_noise(n_active, seed=seed_side + 1)
    left = main + side_ratio * side_l
    right = main - side_ratio * side_r
    # Renormalise each channel to the requested RMS independently so the
    # added decorrelation noise doesn't shift the comparison level.
    target = 10 ** (level_dbfs_rms / 20.0)
    for ch in (left, right):
        rms = float(np.sqrt(np.mean(ch.astype(np.float64) ** 2)) + 1e-12)
        ch *= target / rms
    fade_n = int(round(STEADY_FADE_MS * 1e-3 * SR))
    fade = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_n) / fade_n))
    for ch in (left, right):
        ch[:fade_n] *= fade
        ch[-fade_n:] *= fade[::-1]
    tail = np.zeros(int(round(STEADY_TAIL * SR)), dtype=np.float32)
    left = np.concatenate([left.astype(np.float32), tail])
    right = np.concatenate([right.astype(np.float32), tail])
    stereo = np.column_stack([left, right]).astype(np.float32)
    peak = float(np.max(np.abs(stereo)))
    if peak >= 1.0:
        stereo *= (10 ** (-0.5 / 20.0)) / peak
    meta = {
        "kind": "stereo_correlated_pink",
        "sample_rate": SR,
        "duration_seconds": STEADY_T + STEADY_TAIL,
        "active_seconds": STEADY_T,
        "tail_seconds": STEADY_TAIL,
        "level_dbfs_rms": level_dbfs_rms,
        "fade_ms": STEADY_FADE_MS,
        "stimulus_samples": int(stereo.shape[0]),
        "active_samples": int(n_active),
        "tail_samples": int(tail.size),
        "side_ratio": side_ratio,
        "seed_main": seed_main,
        "seed_side": seed_side,
        "format": "float32 stereo L≠R (correlated, M≫S)",
        "stereo_mode": "asymmetric",
        "analysis_window_start_seconds": 6.0,
        "analysis_window_end_seconds": 11.0,
    }
    return stereo, meta


# ----- multitone -----

def make_multitone(level_dbfs_rms: float, seed: int = 1
                   ) -> tuple[np.ndarray, dict]:
    """Sum of equal-amplitude sinusoids at every Dolby band center, with
    Schroeder-style quasi-random phases to keep the crest factor low."""
    n_active = int(round(STEADY_T * SR))
    t = np.arange(n_active) / SR
    rng = np.random.default_rng(seed)
    # Schroeder phase formula for a flat-amplitude multitone yields a
    # crest factor close to sqrt(2) regardless of N. We approximate it
    # with deterministic random phases — close enough for a measurement
    # signal, and dead simple.
    n_tones = len(BAND_CENTERS)
    phases = rng.uniform(0.0, 2.0 * np.pi, size=n_tones)

    # Per-tone amplitude: equal contribution from each of the 20 bands.
    # Total RMS of the sum ≈ sqrt(N) * per_tone_rms, so per-tone RMS is
    # target_rms / sqrt(N) with each tone (peak = sqrt(2)*RMS).
    target_rms = 10 ** (level_dbfs_rms / 20.0)
    per_tone_amp = target_rms * np.sqrt(2.0 / n_tones)

    sig = np.zeros(n_active, dtype=np.float64)
    for f, phi in zip(BAND_CENTERS, phases):
        sig += per_tone_amp * np.sin(2 * np.pi * f * t + phi)
    sig = sig.astype(np.float32)

    fade_n = int(round(STEADY_FADE_MS * 1e-3 * SR))
    fade = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_n) / fade_n))
    sig[:fade_n] *= fade
    sig[-fade_n:] *= fade[::-1]

    tail = np.zeros(int(round(STEADY_TAIL * SR)), dtype=np.float32)
    mono = np.concatenate([sig, tail])
    stereo = np.column_stack([mono, mono]).astype(np.float32)

    peak = float(np.max(np.abs(stereo)))
    if peak >= 1.0:
        stereo *= (10 ** (-0.5 / 20.0)) / peak

    meta = {
        "kind": "multitone",
        "sample_rate": SR,
        "duration_seconds": STEADY_T + STEADY_TAIL,
        "active_seconds": STEADY_T,
        "tail_seconds": STEADY_TAIL,
        "level_dbfs_rms": level_dbfs_rms,
        "fade_ms": STEADY_FADE_MS,
        "stimulus_samples": int(stereo.shape[0]),
        "active_samples": int(n_active),
        "tail_samples": int(tail.size),
        "tone_frequencies_hz": list(BAND_CENTERS),
        "tone_phases_rad": phases.tolist(),
        "per_tone_amplitude": float(per_tone_amp),
        "seed": seed,
        "format": "float32 stereo L=R",
        "stereo_mode": "symmetric",
        "analysis_window_start_seconds": 6.0,
        "analysis_window_end_seconds": 11.0,
    }
    return stereo, meta


# ----- bass burst -----

def make_bass_burst(level_dbfs_peak: float,
                    tone_freqs_hz: tuple[float, ...] = (50.0, 80.0, 120.0, 180.0),
                    tone_duration_s: float = 3.0,
                    gap_duration_s: float = 0.5,
                    pre_silence_s: float = 0.3,
                    tail_silence_s: float = 0.5,
                    fade_ms: float = 5.0
                    ) -> tuple[np.ndarray, dict]:
    """Sustained bass-band sine bursts, stereo (identical L/R).

    Designed to drive the bass-band regulator at frequencies above the
    speaker's HP cutoff (the bursts that don't get attenuated to
    sub-threshold by upstream EQ are the diagnostic ones). Each burst
    is gated with a raised-cosine fade-in/out so band edges are clean.
    """
    n_pre = int(round(pre_silence_s * SR))
    n_tone = int(round(tone_duration_s * SR))
    n_gap = int(round(gap_duration_s * SR))
    n_tail = int(round(tail_silence_s * SR))

    fade = int(round(fade_ms * 1e-3 * SR))
    env = np.ones(n_tone, dtype=np.float64)
    env[:fade] = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade) / fade))
    env[-fade:] = env[fade - 1::-1]

    amp = 10.0 ** (level_dbfs_peak / 20.0)
    pieces: list[np.ndarray] = [np.zeros(n_pre, dtype=np.float32)]
    tone_starts: list[tuple[float, int]] = []
    cursor = n_pre
    for i, f in enumerate(tone_freqs_hz):
        t = np.arange(n_tone) / SR
        tone = (amp * env * np.sin(2 * np.pi * f * t)).astype(np.float32)
        tone_starts.append((float(f), cursor))
        pieces.append(tone)
        cursor += n_tone
        if i < len(tone_freqs_hz) - 1:
            pieces.append(np.zeros(n_gap, dtype=np.float32))
            cursor += n_gap
    pieces.append(np.zeros(n_tail, dtype=np.float32))

    mono = np.concatenate(pieces)
    stereo = np.column_stack([mono, mono]).astype(np.float32)

    duration_s = stereo.shape[0] / SR
    meta = {
        "kind": "bass_burst",
        "sample_rate": SR,
        "duration_seconds": duration_s,
        "tail_seconds": tail_silence_s,
        "level_dbfs_peak": level_dbfs_peak,
        "tone_freqs_hz": list(tone_freqs_hz),
        "tone_duration_s": tone_duration_s,
        "gap_duration_s": gap_duration_s,
        "pre_silence_s": pre_silence_s,
        "fade_ms": fade_ms,
        "tone_starts_samples": [(f, n) for f, n in tone_starts],
        "stimulus_samples": int(stereo.shape[0]),
        "format": "float32 stereo L=R",
        "stereo_mode": "symmetric",
    }
    return stereo, meta


# ----- speech (dialog-enhancer probe) -----

# DAX's dialog enhancer is speech-gated by Media Intelligence — a content
# classifier — so pink noise cannot excite it (research
# `r-dialog-enhancer-gain-ceiling`: the pink pre-screen came back
# null/confounded). This stimulus exists to trip that gate. Primary source
# is espeak-ng synthesis (clearly speech to any classifier); when espeak-ng
# isn't installed we fall back to LTASS-shaped noise with syllabic
# modulation, which may NOT register as speech — the capture protocol must
# verify the DE-on vs DE-off contrast is nonzero before drawing
# conclusions, whichever source was used. The wav is generated once and
# played on both capture sides, so cross-machine espeak determinism is not
# required; meta records the source and synthesizer version for provenance.
SPEECH_T = 12.0            # match the stationary stimuli: leveler settles ~5 s in
SPEECH_PAUSE_S = 0.35      # inter-sentence gap when tiling the clip
SPEECH_FADE_MS = 10.0
SPEECH_TEXT = (
    "The quick brown fox jumps over the lazy dog. "
    "She sells sea shells by the sea shore, and the shells she sells "
    "are surely sea shells. We promptly judged antique ivory buckles "
    "for the next prize. A loud voice carries clearly across the room."
)
SPEECH_FALLBACK_SEED = 11
SPEECH_SYLLABIC_HZ = 4.0   # fallback: amplitude-modulation rate of natural speech


def _synthesize_espeak(text: str) -> tuple[np.ndarray, str] | None:
    """Synthesize `text` with espeak-ng (or espeak); return (mono @ SR,
    version string), or None when no synthesizer is installed/working."""
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if exe is None:
        return None
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "speech.wav"
        proc = subprocess.run(
            [exe, "-v", "en-us", "-s", "165", "-w", str(wav), text],
            capture_output=True, text=True)
        if proc.returncode != 0 or not wav.is_file():
            return None
        sr_in, data = wavfile.read(str(wav))
    if data.dtype.kind == "i":
        mono = data.astype(np.float64) / float(np.iinfo(data.dtype).max)
    else:
        mono = data.astype(np.float64)
    if mono.ndim > 1:
        mono = mono.mean(axis=1)
    if sr_in != SR:
        from math import gcd
        g = gcd(SR, int(sr_in))
        mono = resample_poly(mono, SR // g, int(sr_in) // g)
    ver = subprocess.run([exe, "--version"], capture_output=True, text=True)
    version = (ver.stdout or ver.stderr).strip().splitlines()[0] if ver.returncode == 0 else "unknown"
    return mono, version


def _speech_shaped_noise(n: int, seed: int) -> np.ndarray:
    """LTASS-like shaped noise with syllabic AM and sentence pauses.

    Spectrum: ~flat 100–500 Hz, −9 dB/oct above 500 Hz, 4th-order
    rolloff below 100 Hz — a coarse long-term-average speech spectrum.
    Envelope: raised-cosine AM at the ~4 Hz syllabic rate plus a 350 ms
    pause every ~3 s. A statistical stand-in only; see the speech-gate
    caveat in the section comment.
    """
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(n)
    spec = np.fft.rfft(white)
    f = np.fft.rfftfreq(n, 1.0 / SR)
    mag_db = np.zeros_like(f)
    hi = f > 500.0
    mag_db[hi] = -9.0 * np.log2(f[hi] / 500.0)
    lo = (f > 0) & (f < 100.0)
    mag_db[lo] = -24.0 * np.log2(100.0 / f[lo])
    mag_db[f == 0] = -120.0
    sig = np.fft.irfft(spec * 10.0 ** (mag_db / 20.0), n)
    t = np.arange(n) / SR
    am = 0.55 + 0.45 * np.cos(2.0 * np.pi * SPEECH_SYLLABIC_HZ * t)
    pause = np.ones(n)
    n_pause = int(round(SPEECH_PAUSE_S * SR))
    for start_s in np.arange(2.8, n / SR - SPEECH_PAUSE_S, 3.0):
        i0 = int(round(start_s * SR))
        ramp = min(n_pause // 4, 480)
        win = np.ones(n_pause)
        win[:ramp] = 0.5 * (1.0 + np.cos(np.pi * np.arange(ramp) / ramp))
        win[-ramp:] = win[ramp - 1::-1]
        pause[i0:i0 + n_pause] *= 1.0 - win[: max(0, min(n_pause, n - i0))]
    return sig * am * pause


def _tile_speech(clip: np.ndarray, n_total: int) -> np.ndarray:
    """Repeat `clip` with a sentence pause between copies, cut to n_total."""
    n_pause = int(round(SPEECH_PAUSE_S * SR))
    reps: list[np.ndarray] = []
    filled = 0
    while filled < n_total:
        reps.append(clip)
        reps.append(np.zeros(n_pause))
        filled += clip.size + n_pause
    return np.concatenate(reps)[:n_total]


def _active_rms(mono: np.ndarray) -> float:
    """Linear RMS over active samples: those above −40 dB of the peak, so
    pauses do not pull the level down."""
    peak = float(np.max(np.abs(mono))) + 1e-30
    active = np.abs(mono) > peak * 10.0 ** (-40.0 / 20.0)
    return float(np.sqrt(np.mean(mono[active] ** 2))) if active.any() else peak


def _finish_speech(mono: np.ndarray, level_dbfs_rms: float) -> np.ndarray:
    """Fade the ends, set the active-segment RMS, guard the peak at −1 dBFS.

    Samples above −40 dB of the clip peak count as active, so pauses do not
    pull the level down."""
    fade_n = int(round(SPEECH_FADE_MS * 1e-3 * SR))
    fade = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_n) / fade_n))
    mono[:fade_n] *= fade
    mono[-fade_n:] *= fade[::-1]

    rms = _active_rms(mono)
    mono *= 10.0 ** (level_dbfs_rms / 20.0) / (rms + 1e-30)
    peak = float(np.max(np.abs(mono)))
    headroom = 10.0 ** (-1.0 / 20.0)
    if peak > headroom:
        mono *= headroom / peak
    return mono


def make_speech(level_dbfs_rms: float = -18.0) -> tuple[np.ndarray, dict]:
    """Speech stimulus, stereo L=R (dialog is center-panned content).

    Active-segment RMS is normalized to `level_dbfs_rms` (the pink-battery
    operating point) with a −1 dBFS peak guard.
    """
    n_total = int(round(SPEECH_T * SR))
    synth = _synthesize_espeak(SPEECH_TEXT)
    if synth is not None:
        clip, version = synth
        source = "espeak"
        mono = _tile_speech(clip, n_total)
    else:
        mono = _speech_shaped_noise(n_total, SPEECH_FALLBACK_SEED)
        source, version = "shaped_noise", None

    mono = _finish_speech(mono, level_dbfs_rms)

    tail = np.zeros(int(round(STEADY_TAIL * SR)))
    mono = np.concatenate([mono, tail]).astype(np.float32)
    stereo = np.column_stack([mono, mono]).astype(np.float32)
    meta = {
        "kind": "speech",
        "sample_rate": SR,
        "duration_seconds": stereo.shape[0] / SR,
        "tail_seconds": STEADY_TAIL,
        "level_dbfs_rms_active": level_dbfs_rms,
        "source": source,
        "synthesizer_version": version,
        "text": SPEECH_TEXT if source == "espeak" else None,
        "seed": None if source == "espeak" else SPEECH_FALLBACK_SEED,
        "analysis_window_start_seconds": 6.0,
        "analysis_window_end_seconds": 11.0,
        "stimulus_samples": int(stereo.shape[0]),
        "format": "float32 stereo L=R",
        "stereo_mode": "symmetric",
    }
    return stereo, meta


# ----- human speech (dialog-enhancer probe, built from a recording) -----

# espeak's voice reads as "fairly robotic", and on the dev device DAX showed
# no dialog-enhancer signature on it (research
# `r-dialog-enhancer-gain-ceiling`), so a classifier that ignores synthetic
# speech would explain the null. These probes are built from a recording of a
# real reader, passed with --speech-source; no recording ships in the repo.
# Each sidecar names the file and its SHA-256, so a capture can be traced to
# the exact clip.
# Pink bed level relative to the *target* speech level. The −1 dBFS peak guard
# can land the speech below its target, so the sidecar records the measured
# gap too: on the 40 s LibriVox clip it is about 9 dB, not 10.
SPEECH_BED_DB = -10.0
# The classifier's ramp and hold times are unmeasured, so these run far past
# the pink battery's 12 s. Analyse them in short windows across the whole
# capture: a plateau well before the end shows the length was enough.
HUMAN_SPEECH_T = 40.0
SPEECH_THEN_PINK_SPLIT_S = 20.0  # speech for the first 20 s, pink after
SPEECH_BED_SEEDS = (200, 201)


# A fixed public-domain reading, so anyone can rebuild the exact clip the
# dev device was captured with: `--speech-source librivox`.
LIBRIVOX = {
    "url": "https://archive.org/download/giftofmagi/"
           "gift_of_the_magi_henry_blb.mp3",
    "sha256": "63c035b3b8e24aca131364695ae7e57c9efed1396af142420183c727f8a08964",
    "start_s": 50.25,     # past the LibriVox intro, at a pause
    "duration_s": 40.0,
    "credit": "The Gift of the Magi (O. Henry), read by Betsie Bush; "
              "LibriVox, public domain",
}


def fetch_librivox_clip(out_dir: Path) -> tuple[Path, dict]:
    """Download (once), verify and cut the LibriVox clip into out_dir.

    The MP3 is checked against its pinned SHA-256. ffmpeg decodes the whole
    file to 48 kHz mono, and the 40 s are cut at an exact sample offset, the
    path the dev device's clip took. Different ffmpeg builds may decode a
    few samples differently, so each sidecar records its own clip's hash."""
    import urllib.request
    mp3 = out_dir / "speech_source_librivox.mp3"
    if (not mp3.is_file()
            or hashlib.sha256(mp3.read_bytes()).hexdigest() != LIBRIVOX["sha256"]):
        print(f"Downloading {LIBRIVOX['url']}")
        with urllib.request.urlopen(LIBRIVOX["url"], timeout=120) as r:
            data = r.read()
        got = hashlib.sha256(data).hexdigest()
        if got != LIBRIVOX["sha256"]:
            raise SystemExit(f"the LibriVox download's SHA-256 is {got}, not "
                             f"the pinned {LIBRIVOX['sha256']}; not using it")
        mp3.write_bytes(data)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("--speech-source librivox needs ffmpeg to decode the "
                         "MP3; install it, or pass a WAV of your own")
    with tempfile.TemporaryDirectory() as td:
        full = Path(td) / "full.wav"
        subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(mp3),
                        "-ac", "1", "-ar", str(SR), "-c:a", "pcm_f32le",
                        str(full)], check=True)
        _, data = wavfile.read(str(full))
    i0 = int(round(LIBRIVOX["start_s"] * SR))
    clip = data[i0:i0 + int(round(LIBRIVOX["duration_s"] * SR))]
    wav = out_dir / "speech_source_librivox_40s.wav"
    wavfile.write(str(wav), SR, clip.astype(np.float32))
    ver = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True)
    return wav, {"source_url": LIBRIVOX["url"],
                 "source_mp3_sha256": LIBRIVOX["sha256"],
                 "source_segment_seconds": [LIBRIVOX["start_s"],
                                            LIBRIVOX["duration_s"]],
                 "source_credit": LIBRIVOX["credit"],
                 "decoder": (ver.stdout.splitlines() or ["ffmpeg"])[0]}


def load_speech_source(path: Path) -> tuple[np.ndarray, dict]:
    """Read a speech recording as mono float at SR, with its provenance."""
    sr_in, data = wavfile.read(str(path))
    if data.dtype.kind == "i":
        mono = data.astype(np.float64) / float(np.iinfo(data.dtype).max)
    elif data.dtype.kind == "u":
        # 8-bit WAV PCM is unsigned, centred on 128.
        mono = (data.astype(np.float64) - 128.0) / 128.0
    else:
        mono = data.astype(np.float64)
    if mono.ndim > 1:
        mono = mono.mean(axis=1)
    if sr_in != SR:
        from math import gcd
        g = gcd(SR, int(sr_in))
        mono = resample_poly(mono, SR // g, int(sr_in) // g)
    sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return mono, {"source": "file", "source_name": Path(path).name,
                  "source_sha256": sha}


def _active_rms_db(mono: np.ndarray) -> float:
    return 20.0 * np.log10(_active_rms(mono) + 1e-30)


def _human_speech_meta(kind: str, stereo: np.ndarray, provenance: dict,
                       level_dbfs_rms: float, stereo_mode: str,
                       speech: np.ndarray) -> dict:
    return {
        "kind": kind,
        "sample_rate": SR,
        "duration_seconds": stereo.shape[0] / SR,
        "tail_seconds": STEADY_TAIL,
        "level_dbfs_rms_active": level_dbfs_rms,
        # the −1 dBFS peak guard can land a long clip below the target
        "speech_dbfs_rms_active_measured": round(_active_rms_db(speech), 2),
        **provenance,
        "stimulus_samples": int(stereo.shape[0]),
        "format": f"float32 stereo {'L=R' if stereo_mode == 'symmetric' else 'L!=R'}",
        "stereo_mode": stereo_mode,
    }


def make_speech_human(clip: np.ndarray, provenance: dict,
                      level_dbfs_rms: float = -18.0) -> tuple[np.ndarray, dict]:
    """The recording, centred (L=R), at the speech level; tiled if short."""
    n_total = int(round(HUMAN_SPEECH_T * SR))
    mono = _finish_speech(_tile_speech(clip, n_total), level_dbfs_rms)
    mono = np.concatenate([mono, np.zeros(int(round(STEADY_TAIL * SR)))])
    stereo = np.column_stack([mono, mono]).astype(np.float32)
    meta = _human_speech_meta("speech", stereo, provenance, level_dbfs_rms,
                              "symmetric", mono[:n_total])
    meta.update(analysis_window_start_seconds=10.0,
                analysis_window_end_seconds=HUMAN_SPEECH_T - 1.0)
    return stereo, meta


def make_speech_over_bed(clip: np.ndarray, provenance: dict,
                         level_dbfs_rms: float = -18.0,
                         bed_db: float = SPEECH_BED_DB
                         ) -> tuple[np.ndarray, dict]:
    """Centred speech over an independent pink bed on each channel.

    The side signal (L−R)/2 then carries bed only, so a boost there means the
    dialog enhancer acts on the whole signal, not just the centre. The bed's
    level during speech versus pauses shows any ducking."""
    n_total = int(round(HUMAN_SPEECH_T * SR))
    speech = _finish_speech(_tile_speech(clip, n_total), level_dbfs_rms)
    fade_n = int(round(STEADY_FADE_MS * 1e-3 * SR))
    fade = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_n) / fade_n))
    beds = []
    for seed in SPEECH_BED_SEEDS:
        bed = _pink_noise(n_total, seed=seed) * 10.0 ** ((level_dbfs_rms + bed_db) / 20.0)
        bed[:fade_n] *= fade
        bed[-fade_n:] *= fade[::-1]
        beds.append(bed)
    left, right = speech + beds[0], speech + beds[1]
    peak = max(float(np.max(np.abs(left))), float(np.max(np.abs(right))))
    headroom = 10.0 ** (-1.0 / 20.0)
    scale = headroom / peak if peak > headroom else 1.0
    tail = np.zeros(int(round(STEADY_TAIL * SR)))
    stereo = np.column_stack([np.concatenate([left * scale, tail]),
                              np.concatenate([right * scale, tail])]
                             ).astype(np.float32)
    meta = _human_speech_meta("speech_over_bed", stereo, provenance,
                              level_dbfs_rms, "asymmetric", speech * scale)
    bed_rms_db = level_dbfs_rms + bed_db + 20.0 * np.log10(scale)
    meta.update(bed_db_re_speech=bed_db,
                bed_db_re_speech_measured=round(
                    bed_rms_db - meta["speech_dbfs_rms_active_measured"], 2),
                bed_seeds=list(SPEECH_BED_SEEDS),
                peak_guard_scale_db=20.0 * np.log10(scale),
                analysis_window_start_seconds=10.0,
                analysis_window_end_seconds=HUMAN_SPEECH_T - 1.0)
    return stereo, meta


def make_speech_then_pink(clip: np.ndarray, provenance: dict,
                          level_dbfs_rms: float = -18.0
                          ) -> tuple[np.ndarray, dict]:
    """20 s of centred speech, then 20 s of centred pink at the target level.

    A boost that outlasts the speech onto the pink shows how long the
    dialog enhancer holds after speech stops."""
    n_split = int(round(SPEECH_THEN_PINK_SPLIT_S * SR))
    n_total = int(round(HUMAN_SPEECH_T * SR))
    speech = _finish_speech(_tile_speech(clip, n_split), level_dbfs_rms)
    pink = _pink_noise(n_total - n_split, seed=0) * 10.0 ** (level_dbfs_rms / 20.0)
    fade_n = int(round(SPEECH_FADE_MS * 1e-3 * SR))
    fade = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_n) / fade_n))
    pink[:fade_n] *= fade
    pink[-fade_n:] *= fade[::-1]
    mono = np.concatenate([speech, pink, np.zeros(int(round(STEADY_TAIL * SR)))])
    peak = float(np.max(np.abs(mono)))
    headroom = 10.0 ** (-1.0 / 20.0)
    if peak > headroom:
        mono *= headroom / peak
    stereo = np.column_stack([mono, mono]).astype(np.float32)
    meta = _human_speech_meta("speech_then_pink", stereo, provenance,
                              level_dbfs_rms, "symmetric", mono[:n_split])
    meta.update(speech_seconds=[0.0, SPEECH_THEN_PINK_SPLIT_S],
                pink_seconds=[SPEECH_THEN_PINK_SPLIT_S, HUMAN_SPEECH_T])
    return stereo, meta


def make_pink_then_speech(clip: np.ndarray, provenance: dict,
                          level_dbfs_rms: float = -18.0
                          ) -> tuple[np.ndarray, dict]:
    """20 s of centred pink, then 20 s of centred speech: the reverse of
    make_speech_then_pink, where the IEQ's level tracking should show the
    opposite asymmetry (research `r-ieq-adapts-to-content`)."""
    n_split = int(round(SPEECH_THEN_PINK_SPLIT_S * SR))
    n_total = int(round(HUMAN_SPEECH_T * SR))
    pink = _pink_noise(n_split, seed=0) * 10.0 ** (level_dbfs_rms / 20.0)
    fade_n = int(round(SPEECH_FADE_MS * 1e-3 * SR))
    fade = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_n) / fade_n))
    pink[:fade_n] *= fade
    pink[-fade_n:] *= fade[::-1]
    speech = _finish_speech(_tile_speech(clip, n_total - n_split), level_dbfs_rms)
    mono = np.concatenate([pink, speech, np.zeros(int(round(STEADY_TAIL * SR)))])
    peak = float(np.max(np.abs(mono)))
    headroom = 10.0 ** (-1.0 / 20.0)
    if peak > headroom:
        mono *= headroom / peak
    stereo = np.column_stack([mono, mono]).astype(np.float32)
    meta = _human_speech_meta("pink_then_speech", stereo, provenance,
                              level_dbfs_rms, "symmetric", speech)
    meta.update(pink_seconds=[0.0, SPEECH_THEN_PINK_SPLIT_S],
                speech_seconds=[SPEECH_THEN_PINK_SPLIT_S, HUMAN_SPEECH_T])
    return stereo, meta


# ----- stepped sine (per-frequency, multi-pass: static EQ vs adaptive) -----

# Probe grid: every Dolby band centre plus the geometric midpoint between each
# adjacent pair (39 points). Dense enough to sample the magnitude *between* the
# 20 band centres (the linear-vs-PCHIP interpolation question, issue #13) while
# still landing one probe exactly on every band centre.
STEPPED_TONE_S = 1.0       # held duration per tone (>= ~47 cycles at 47 Hz)
STEPPED_GAP_S = 0.4        # silence between tones — lets the dynamics relax
STEPPED_FADE_MS = 10.0
STEPPED_SETTLE_S = 0.4     # analyzer skips this much of each tone (attack) before
                           # reading the steady-state amplitude
# Each pass plays the whole grid once, in a different order. The response at a
# frequency that is invariant across passes is the static EQ; the part that
# shifts with arrival order is the leveler / regulator / MI steering (adaptive).
STEPPED_PASSES = ("ascending", "descending", "shuffled")


def _stepped_grid() -> np.ndarray:
    bands = np.array(BAND_CENTERS, dtype=float)
    mids = np.sqrt(bands[:-1] * bands[1:])
    return np.unique(np.concatenate([bands, mids]))


def make_stepped_sine(level_dbfs_peak: float, seed: int = 7
                      ) -> tuple[np.ndarray, dict]:
    """Stepped-sine probe: one held tone per probe frequency, the whole grid
    repeated in several orderings (ascending / descending / shuffled).

    Unlike the multitone (all tones at once) this isolates one frequency at a
    time, so the leveler/regulator act on a single tone; and unlike a one-shot
    stepped sweep, repeating the grid in different orders lets the analyzer
    separate the *static* EQ (invariant across passes) from the *adaptive*
    dynamics (response that depends on what tone preceded it). Pair the loud and
    quiet variants to map the level-dependent (dynamics-driven) gain — the
    dominant EE-vs-DAX gap in the treble (#11).

    Each tone is snapped to an exact FFT bin of its own held window so the
    steady-state single-bin DFT reads back a clean amplitude. Returns
    ``(stereo, meta)``; ``meta['segments']`` lists per-tone
    ``{freq_hz, pass, start_sample, len_sample}`` (start indices into the
    generated signal) for the analyzer.
    """
    grid = _stepped_grid()
    asc = np.argsort(grid)
    rng = np.random.default_rng(seed)
    shuffled = asc.copy()
    rng.shuffle(shuffled)
    orders = {"ascending": asc, "descending": asc[::-1], "shuffled": shuffled}

    n_tone = int(round(STEPPED_TONE_S * SR))
    n_gap = int(round(STEPPED_GAP_S * SR))
    fade_n = int(round(STEPPED_FADE_MS * 1e-3 * SR))
    fade = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_n) / fade_n))
    t = np.arange(n_tone) / SR
    amp = 10.0 ** (level_dbfs_peak / 20.0)

    pieces: list[np.ndarray] = [np.zeros(n_gap, dtype=np.float64)]  # lead-in
    segments: list[dict] = []
    cursor = n_gap
    for pass_name in STEPPED_PASSES:
        for idx in orders[pass_name]:
            f = float(grid[idx])
            # snap to an exact bin of the held window for a clean single-bin DFT
            bin_k = max(1, round(f * n_tone / SR))
            f_snap = bin_k * SR / n_tone
            tone = amp * np.sin(2.0 * np.pi * f_snap * t)
            tone[:fade_n] *= fade
            tone[-fade_n:] *= fade[::-1]
            segments.append({
                "freq_hz": f_snap, "pass": pass_name,
                "start_sample": cursor, "len_sample": n_tone,
            })
            pieces.append(tone)
            pieces.append(np.zeros(n_gap, dtype=np.float64))
            cursor += n_tone + n_gap

    mono = np.concatenate(pieces).astype(np.float32)
    stereo = np.column_stack([mono, mono]).astype(np.float32)
    peak = float(np.max(np.abs(stereo)))
    if peak >= 1.0:
        stereo *= (10 ** (-0.5 / 20.0)) / peak
    meta = {
        "kind": "stepped",
        "sample_rate": SR,
        "duration_seconds": stereo.shape[0] / SR,
        "tail_seconds": STEPPED_GAP_S,
        "level_dbfs_peak": level_dbfs_peak,
        "tone_s": STEPPED_TONE_S,
        "gap_s": STEPPED_GAP_S,
        "fade_ms": STEPPED_FADE_MS,
        "settle_s": STEPPED_SETTLE_S,
        "passes": list(STEPPED_PASSES),
        "n_probe_freqs": int(grid.size),
        "segments": segments,
        "seed": seed,
        "stimulus_samples": int(stereo.shape[0]),
        "format": "float32 stereo L=R",
        "stereo_mode": "symmetric",
    }
    return stereo, meta


# ----- level ladder and level steps (vendor dynamics, issue #140) -----

# One frequency where each kind of level-dependent loss was seen on loud
# stepped tones (HP B&O: 141–234 Hz and 2.2–2.6 kHz; its `off` state around
# 470 Hz), plus mids and treble.
LEVELS_FREQS_HZ = (150.0, 470.0, 1000.0, 2400.0, 5000.0, 10000.0)
LEVELS_DBFS_PEAK = tuple(range(-34, -1, 4))   # -34 … -2 dBFS in 4 dB steps
LEVELSTEP_FREQS_HZ = (150.0, 1000.0, 2400.0)
# (level dBFS peak, seconds): quiet long enough to settle, a loud step for
# the attack, then back down long enough to see the release.
LEVELSTEP_PLAN = ((-30.0, 2.0), (-4.0, 2.0), (-30.0, 3.0))
LEVELSTEP_GAP_S = 0.5
LEVELSTEP_RAMP_MS = 1.0


def _snap(f: float, n: int) -> float:
    """*f* moved to an exact bin of an *n*-sample window."""
    return max(1, round(f * n / SR)) * SR / n


def make_levels() -> tuple[np.ndarray, dict]:
    """Per-frequency level ladder: each of `LEVELS_FREQS_HZ` held at every
    level in `LEVELS_DBFS_PEAK`, quietest first. Read each tone's
    steady-state amplitude (after `settle_s`) against its own level: a
    linear chain gives the same gain on every rung, compression a falling
    one. Tone and gap lengths match the stepped sine, so the dynamics relax
    between tones as they do there."""
    n_tone = int(round(STEPPED_TONE_S * SR))
    n_gap = int(round(STEPPED_GAP_S * SR))
    fade_n = int(round(STEPPED_FADE_MS * 1e-3 * SR))
    fade = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_n) / fade_n))
    t = np.arange(n_tone) / SR
    pieces = [np.zeros(n_gap)]
    segments = []
    cursor = n_gap
    for f in LEVELS_FREQS_HZ:
        f_snap = _snap(f, n_tone)
        for level in LEVELS_DBFS_PEAK:
            tone = 10.0 ** (level / 20.0) * np.sin(2.0 * np.pi * f_snap * t)
            tone[:fade_n] *= fade
            tone[-fade_n:] *= fade[::-1]
            segments.append({"freq_hz": f_snap, "level_dbfs_peak": float(level),
                             "start_sample": cursor, "len_sample": n_tone})
            pieces += [tone, np.zeros(n_gap)]
            cursor += n_tone + n_gap
    mono = np.concatenate(pieces).astype(np.float32)
    stereo = np.column_stack([mono, mono])
    meta = {
        "kind": "levels",
        "sample_rate": SR,
        "duration_seconds": stereo.shape[0] / SR,
        "tail_seconds": STEPPED_GAP_S,
        "tone_s": STEPPED_TONE_S,
        "gap_s": STEPPED_GAP_S,
        "fade_ms": STEPPED_FADE_MS,
        "settle_s": STEPPED_SETTLE_S,
        "levels_dbfs_peak": [float(x) for x in LEVELS_DBFS_PEAK],
        "segments": segments,
        "stimulus_samples": int(stereo.shape[0]),
        "format": "float32 stereo L=R",
        "stereo_mode": "symmetric",
    }
    return stereo, meta


def make_levelstep() -> tuple[np.ndarray, dict]:
    """Level steps for attack and release: at each of `LEVELSTEP_FREQS_HZ`,
    one continuous tone stepping through `LEVELSTEP_PLAN` (1 ms ramps, so
    the step itself adds no click). A stage that acts on level shows as a
    gain that moves after each step; its time course is the attack (up
    step) and the release (down step)."""
    ramp_n = int(round(LEVELSTEP_RAMP_MS * 1e-3 * SR))
    n_gap = int(round(LEVELSTEP_GAP_S * SR))
    pieces = [np.zeros(n_gap)]
    steps = []
    cursor = n_gap
    total_s = sum(s for _, s in LEVELSTEP_PLAN)
    n_total = int(round(total_s * SR))
    for f in LEVELSTEP_FREQS_HZ:
        f_snap = _snap(f, int(round(STEPPED_TONE_S * SR)))
        gain = np.empty(n_total)
        o, prev = 0, None
        for level, secs in LEVELSTEP_PLAN:
            n = int(round(secs * SR))
            amp = 10.0 ** (level / 20.0)
            gain[o:o + n] = amp
            if prev is not None:
                gain[o:o + ramp_n] = np.linspace(prev, amp, ramp_n)
            steps.append({"freq_hz": f_snap, "level_dbfs_peak": level,
                          "start_sample": cursor + o, "len_sample": n})
            o, prev = o + n, amp
        ramp = 0.5 * (1.0 - np.cos(np.pi * np.arange(ramp_n) / ramp_n))
        gain[:ramp_n] *= ramp
        gain[-ramp_n:] *= ramp[::-1]
        t = np.arange(n_total) / SR
        pieces += [gain * np.sin(2.0 * np.pi * f_snap * t), np.zeros(n_gap)]
        cursor += n_total + n_gap
    mono = np.concatenate(pieces).astype(np.float32)
    stereo = np.column_stack([mono, mono])
    meta = {
        "kind": "levelstep",
        "sample_rate": SR,
        "duration_seconds": stereo.shape[0] / SR,
        "tail_seconds": LEVELSTEP_GAP_S,
        "ramp_ms": LEVELSTEP_RAMP_MS,
        "plan": [list(p) for p in LEVELSTEP_PLAN],
        "steps": steps,
        "stimulus_samples": int(stereo.shape[0]),
        "format": "float32 stereo L=R",
        "stereo_mode": "symmetric",
    }
    return stereo, meta


def _dynamics_suite() -> None:
    """The level ladder and level steps (issue #140)."""
    stereo, meta = make_levels()
    write_stimulus("stimulus_levels", stereo, meta)
    stereo, meta = make_levelstep()
    write_stimulus("stimulus_levelstep", stereo, meta)


# ----- entry point -----

def write_stimulus(name: str, stereo: np.ndarray, meta: dict,
                   inverse: np.ndarray | None = None) -> None:
    wav_path = OUT_DIR / f"{name}.wav"
    json_path = OUT_DIR / f"{name}.json"
    wavfile.write(str(wav_path), SR, stereo)
    json_path.write_text(json.dumps(meta, indent=2) + "\n")
    duration_ms = 1000.0 * stereo.shape[0] / SR
    peak_db = 20.0 * np.log10(float(np.max(np.abs(stereo))) + 1e-12)
    rms_db = 20.0 * np.log10(float(np.sqrt(np.mean(stereo ** 2))) + 1e-12)
    print(f"  {wav_path.name:<28} {stereo.shape[0]:>7} samples, "
          f"{duration_ms:>7.1f} ms, peak {peak_db:+6.2f} / RMS {rms_db:+6.2f} dBFS")
    if inverse is not None:
        np.save(OUT_DIR / "inverse_sweep.npy", inverse)


# Rungs added to the two pink levels the suite already had (−18 and −42), so
# the ladder reads −60/−48/−42/−30/−24/−18/−14 dBFS RMS.
#
# The loud end stops at −14 because pink noise cannot go louder: its crest
# factor is ~13 dB, so −12 dBFS RMS already peaks above full scale and
# `make_pink` quietly rescales it — asking for −12 and −6 produced two
# identical −13.4 dBFS files. For the loud end of the leveler curve use
# `stimulus_stepped_loud` (−2 dBFS peak) instead, which is built for it.
PINK_LADDER_DBFS = (-60, -48, -30, -24, -14)


def _ieq_suite() -> None:
    """The IEQ timing and bandwidth probes (research `r-ieq-adapts-to-content`)."""
    stereo, meta = make_pink_steps()
    write_stimulus("stimulus_pink_steps", stereo, meta)
    stereo, meta = make_pink_bump()
    write_stimulus(f"stimulus_pink_bump_{int(PINK_BUMP_HZ)}", stereo, meta)


def _speech_clip(source: str) -> tuple[np.ndarray, dict]:
    """The speech recording for the human-speech probes, with provenance."""
    extra = {}
    if source == "librivox":
        path, extra = fetch_librivox_clip(OUT_DIR)
    else:
        path = Path(source)
    clip, provenance = load_speech_source(path)
    provenance.update(extra)
    return clip, provenance


def _human_speech_suite(source: str) -> None:
    clip, provenance = _speech_clip(source)
    for name, build in (("stimulus_speech_human", make_speech_human),
                        ("stimulus_speech_over_bed", make_speech_over_bed),
                        ("stimulus_speech_then_pink", make_speech_then_pink),
                        ("stimulus_pink_then_speech", make_pink_then_speech)):
        stereo, meta = build(clip, provenance)
        write_stimulus(name, stereo, meta)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Write the DAX3 measurement stimulus suite (wav + json "
                    "sidecar each) into the current directory.")
    ap.add_argument("--speech-source", metavar="WAV|librivox",
                    help="a recording of a real reader, or 'librivox' to "
                         "download and cut the public-domain clip the dev "
                         "device used (needs ffmpeg); also builds the "
                         "speech_human, speech_over_bed, speech_then_pink and "
                         "pink_then_speech "
                         "dialog-enhancer probes from it")
    ap.add_argument("--speech-only", action="store_true",
                    help="build only the four --speech-source probes")
    ap.add_argument("--dynamics-only", action="store_true",
                    help="build only the level ladder and level steps "
                         "(stimulus_levels, stimulus_levelstep)")
    ap.add_argument("--ieq-only", action="store_true",
                    help="build only the IEQ probes: pink_steps and "
                         "pink_bump_2250, plus pink_then_speech with "
                         "--speech-source")
    args = ap.parse_args(argv)
    if args.speech_only and args.speech_source is None:
        ap.error("--speech-only needs --speech-source")
    if args.speech_only:
        print("Building human-speech probes:")
        _human_speech_suite(args.speech_source)
        return
    if args.dynamics_only:
        print("Building dynamics probes:")
        _dynamics_suite()
        return
    if args.ieq_only:
        print("Building IEQ probes:")
        _ieq_suite()
        if args.speech_source is not None:
            clip, provenance = _speech_clip(args.speech_source)
            stereo, meta = make_pink_then_speech(clip, provenance)
            write_stimulus("stimulus_pink_then_speech", stereo, meta)
        return

    print("Building stimulus suite:")

    # sweep — use a single shared inverse filter for both levels.
    sweep_unit = _sweep_signal()
    inverse = _sweep_inverse(sweep_unit)

    stereo, meta = make_sweep(level_dbfs_peak=-18.0)
    write_stimulus("stimulus_sweep", stereo, meta, inverse=inverse)
    stereo, meta = make_sweep(level_dbfs_peak=-42.0)
    write_stimulus("stimulus_sweep_quiet", stereo, meta)

    # pink
    stereo, meta = make_pink(level_dbfs_rms=-18.0)
    write_stimulus("stimulus_pink", stereo, meta)
    stereo, meta = make_pink(level_dbfs_rms=-42.0)
    write_stimulus("stimulus_pink_quiet", stereo, meta)

    # multitone
    stereo, meta = make_multitone(level_dbfs_rms=-18.0)
    write_stimulus("stimulus_multitone", stereo, meta)

    # stereo-asymmetric pink (decorrelated) — exercises the M/S split
    # at maximum entropy. Catches LTI divergences in stereo_tools
    # (and any other stage's) M/S processing.
    stereo, meta = make_stereo_pink(level_dbfs_rms=-18.0)
    write_stimulus("stimulus_stereo_pink", stereo, meta)

    # stereo-correlated pink — natural-music operating point (M ≫ S).
    # Catches divergences whose visibility depends on signal
    # correlation, which the decorrelated stimulus can mask.
    stereo, meta = make_stereo_correlated_pink(level_dbfs_rms=-18.0)
    write_stimulus("stimulus_stereo_correlated", stereo, meta)

    # bass-burst — sustained sine tones at 50/80/120/180 Hz at -5 and
    # -25 dBFS peak. Designed to expose bass-band regulator behaviour:
    # the loud variant pushes the post-FIR signal above any reasonable
    # regulator threshold (at frequencies the chain doesn't pre-
    # attenuate); the quiet variant is a same-frequency control with
    # the regulator dormant. Used to compare DAX-side bass dynamics
    # against the EE chain — see research `r-regulator-stress-amount`.
    stereo, meta = make_bass_burst(level_dbfs_peak=-5.0)
    write_stimulus("stimulus_bass_burst", stereo, meta)
    stereo, meta = make_bass_burst(level_dbfs_peak=-25.0)
    write_stimulus("stimulus_bass_burst_quiet", stereo, meta)

    # stepped sine — one held tone per probe frequency, the grid repeated in
    # ascending / descending / shuffled order. The cross-pass-invariant part of
    # each tone's response is the static EQ; the order-dependent part is the
    # adaptive dynamics. Loud (-18) + quiet (-42) bracket the level dependence.
    stereo, meta = make_stepped_sine(level_dbfs_peak=-18.0)
    write_stimulus("stimulus_stepped", stereo, meta)
    stereo, meta = make_stepped_sine(level_dbfs_peak=-42.0)
    write_stimulus("stimulus_stepped_quiet", stereo, meta)

    # pink level ladder — the volume leveler's gain-versus-input-level curve.
    # DAX rides a level-dependent gain that the two original pink levels only
    # bracket: measured on the dev device, DAX(dynamic) − DAX(off) is +7.1 dB
    # at −17.8 dBFS and +16.4 dB at −41.8 dBFS (research `r-level-restore`).
    # Two points make a line, and our autogain's target/window constants are
    # currently chosen rather than derived
    # (research `r-leveler-autogain-window`,
    # `r-conservative-autogain-offsets`) — these rungs turn that into a curve.
    #
    # The names carry the level with no separator on purpose: the analysis
    # tooling parses `<kind>_<label>_<channel>` and treats the first
    # underscore-free token as the stimulus tag, so "pink_m60" would be read
    # as tag "pink" with label "m60_dynamic" and silently compared against
    # the wrong capture.
    for level in PINK_LADDER_DBFS:
        stereo, meta = make_pink(level_dbfs_rms=float(level))
        # make_pink rescales rather than clip when the requested RMS implies
        # peaks over full scale, so a rung can come out at a level nobody
        # asked for — and two rungs can collapse onto the same file. A ladder
        # is only a ladder if each rung is where it says it is.
        got = 20.0 * np.log10(float(np.sqrt(np.mean(stereo ** 2))) + 1e-12)
        if abs(got - level) > 0.5:
            raise SystemExit(
                f"pink ladder rung {level} dBFS came out at {got:+.2f} dBFS "
                "(make_pink rescaled it for headroom). Pick a quieter rung, "
                "or use stimulus_stepped_loud for the loud end.")
        write_stimulus(f"stimulus_pink{abs(level):02d}", stereo, meta)

    _ieq_suite()

    # stepped sine, MBC-waking level. The dev-device XML decodes both MBC
    # band thresholds to ≈ −6.4 dBFS (see research `r-dynamics-dormancy`: the
    # −18/−42 batteries never cross them).
    # A −2 dBFS-peak tone (−5 dBFS RMS) crosses that knee by ~1.4 dB even at
    # unity chain gain, and engages the regulator/limiter at boosted bands
    # — intended: this variant exists to characterise the dynamics
    # constants (research `r-mbc-ratio-time-constants`,
    # `r-fixed-dynamics-constants`). With −18 and −42 it spans the
    # gain-reduction-vs-level curve.
    stereo, meta = make_stepped_sine(level_dbfs_peak=-2.0)
    write_stimulus("stimulus_stepped_loud", stereo, meta)

    # speech — dialog-enhancer probe (research
    # `r-dialog-enhancer-gain-ceiling`). DE is
    # speech-gated by Media Intelligence; pink can't excite it.
    stereo, meta = make_speech(level_dbfs_rms=-18.0)
    write_stimulus("stimulus_speech", stereo, meta)

    if args.speech_source is not None:
        _human_speech_suite(args.speech_source)

    _dynamics_suite()

    print(f"\ninverse_sweep.npy: written ({inverse.size} samples, "
          "shared by stimulus_sweep and stimulus_sweep_quiet)")


if __name__ == "__main__":
    main()
