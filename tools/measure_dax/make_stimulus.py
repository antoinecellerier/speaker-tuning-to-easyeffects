#!/usr/bin/env python3
"""Generate the full stimulus suite for DAX3 measurement.

Outputs (in this directory):
    stimulus_sweep.wav        + .json   exp sweep, -18 dBFS peak
    stimulus_sweep_quiet.wav  + .json   exp sweep, -42 dBFS peak
    stimulus_pink.wav         + .json   pink noise, -18 dBFS RMS
    stimulus_pink_quiet.wav   + .json   pink noise, -42 dBFS RMS
    stimulus_multitone.wav    + .json   summed band-center tones, -18 dBFS RMS
    inverse_sweep.npy                   matched inverse for both sweep stimuli

The five stimuli probe DAX3 from different angles:

  - sweep (-18 dBFS): the original Farina test. Recovers a true LTI IR
    if the system is LTI, otherwise produces an artifact-laden estimate.
    Already shown to push DAX3's leveler/regulator into non-LTI behavior.

  - sweep_quiet (-42 dBFS): same sweep at a much lower input level.
    Tests whether the leveler is *less* aggressive when the input is
    quiet (it might also be more aggressive — leveler's job is to bring
    quiet up).

  - pink (-18 dBFS RMS): stationary pink noise. After the leveler has
    settled (~5 s), average the loopback spectrum. Recovers steady-state
    magnitude only — no phase, no IR.

  - pink_quiet (-42 dBFS RMS): pink at low level, again to bracket
    leveler behavior.

  - multitone (-18 dBFS RMS): sum of 20 pure tones at the Dolby band
    centers. Per-band magnitude readout via Goertzel — no spectral
    leakage between bands. The cleanest steady-state magnitude probe.

Stereo stimuli are L=R (centered mono content) — recovers DAX3's
diagonal response (L→L, R→R), which is what compare.py expects.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy.io import wavfile

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
        # Reference window for the analyze step: skip the leveler's settling
        # transient at the start; analyze the last ~5 s of the stationary part.
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
        "analysis_window_start_seconds": 6.0,
        "analysis_window_end_seconds": 11.0,
    }
    return stereo, meta


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


def main() -> None:
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

    print(f"\ninverse_sweep.npy: written ({inverse.size} samples, "
          "shared by stimulus_sweep and stimulus_sweep_quiet)")


if __name__ == "__main__":
    main()
