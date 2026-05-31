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
    # against the EE chain — see docs/design-notes.md "Follow-ups".
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

    print(f"\ninverse_sweep.npy: written ({inverse.size} samples, "
          "shared by stimulus_sweep and stimulus_sweep_quiet)")


if __name__ == "__main__":
    main()
