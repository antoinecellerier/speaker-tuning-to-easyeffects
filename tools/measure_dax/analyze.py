#!/usr/bin/env python3
"""Unified analyzer for measure_dax captures.

Reads each `loopback_<kind>_<label>.wav` plus its `<...>.json` sidecar,
dispatches on `stimulus_kind`, and emits:

  - sweep      → Farina-deconvolved IR (`ir_sweep_<label>_{L,R}.wav`)
                 plus a magnitude / group-delay / phase summary.
  - pink       → averaged spectrum over the stationary window
                 (`spectrum_pink_<label>.npz`) + magnitude summary.
  - multitone  → per-tone amplitude readout via Goertzel
                 (`tones_multitone_<label>.npz`) + per-band table.

If `--xml`/`--profile`/`--curve` are given, also runs a comparison
against the FIR our converter would generate from that XML, saving
overlay plots (`compare_<basename>_<profile>_<curve>_<channel>.png`)
and a numerical residual summary.

Usage:
    python analyze.py captures/loopback_sweep_dynamic.wav  # analyze only
    python analyze.py captures/*.wav                       # batch analyze
    python analyze.py captures/loopback_pink_dynamic.wav \\
        --xml ../DEV_0287_SUBSYS_17AA22E6_PCI_SUBSYS_22E617AA.xml \\
        --profile dynamic --curve balanced
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import correlate, fftconvolve, group_delay

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from dolby_to_easyeffects import (  # noqa: E402
    parse_xml, make_fir, interpolate_curve_db, SAMPLE_RATE,
)

SCRIPT_DIR = Path(__file__).resolve().parent


def _resolve_resource(name: str, loopback_path: Path | None = None,
                      legacy_names: tuple[str, ...] = ()) -> Path | None:
    """Search for a generated artifact (stimulus / inverse filter) in
    common locations: cwd, the loopback file's parent dir, the script
    dir. Returns the first hit, or None."""
    search_roots: list[Path] = [Path.cwd()]
    if loopback_path is not None:
        search_roots.append(loopback_path.resolve().parent)
        search_roots.append(loopback_path.resolve().parent.parent)
    search_roots.append(SCRIPT_DIR)
    for root in search_roots:
        for n in (name, *legacy_names):
            p = root / n
            if p.is_file():
                return p
    return None


# Sweep deconvolution layout (matches the old deconvolve.py output):
# 8192 samples around the peak, 2048 pre-peak (enough for linear-phase
# precursor), 6144 post-peak.
SWEEP_IR_LENGTH = 8192
SWEEP_IR_PRE = 2048


# ----- shared loaders -----

def _read_wav_float(path: Path) -> tuple[int, np.ndarray]:
    sr, x = wavfile.read(str(path))
    if x.dtype == np.int16:
        x = x.astype(np.float32) / 32768.0
    elif x.dtype == np.int32:
        x = x.astype(np.float32) / 2147483648.0
    elif x.dtype != np.float32:
        x = x.astype(np.float32)
    if x.ndim == 1:
        x = np.column_stack([x, x])
    return int(sr), x


def _load_sidecar(loopback_path: Path) -> dict:
    sidecar = loopback_path.with_suffix(".json")
    if not sidecar.is_file():
        raise SystemExit(
            f"sidecar JSON not found for {loopback_path.name}: expected "
            f"{sidecar}. Run capture_dax.py to produce captures with "
            f"matching sidecars."
        )
    return json.loads(sidecar.read_text())


def _stim_kind(meta: dict) -> str:
    kind = meta.get("stimulus_kind")
    if kind:
        return kind
    return meta.get("stimulus", {}).get("stimulus_meta", {}).get("kind", "sweep")


def _stim_tag(meta: dict) -> str:
    """Per-variant tag for output filenames. Defaults to kind. The Windows
    side derives this from the stimulus filename (e.g. stimulus_sweep_quiet.wav
    → 'sweep_quiet') so regular and _quiet variants don't collide."""
    tag = meta.get("stimulus_tag")
    if tag:
        return tag
    return _stim_kind(meta)


def _stim_meta(meta: dict) -> dict:
    return meta.get("stimulus", {}).get("stimulus_meta", {})


# ----- sweep analysis (Farina deconvolution) -----

@dataclass
class SweepResult:
    f: np.ndarray              # frequency axis (Hz)
    mag_db_L: np.ndarray       # peak-normalized magnitude per channel (dB)
    mag_db_R: np.ndarray
    group_delay_ms_L: np.ndarray
    group_delay_ms_R: np.ndarray
    ir_L: np.ndarray           # 8192-sample IR around peak, peak at IR_PRE
    ir_R: np.ndarray
    peak_idx_L: int            # within trimmed IR (should be SWEEP_IR_PRE)
    peak_idx_R: int
    capture_offset_L: int      # in original capture
    capture_offset_R: int
    far_field_snr_db: float


def _farina_deconvolve(loopback: np.ndarray, inverse: np.ndarray
                       ) -> tuple[np.ndarray, int]:
    full = fftconvolve(loopback, inverse, mode="full")
    peak_idx = int(np.argmax(np.abs(full)))
    start = max(0, peak_idx - SWEEP_IR_PRE)
    end = min(full.size, start + SWEEP_IR_LENGTH)
    if end - start < SWEEP_IR_LENGTH:
        start = max(0, end - SWEEP_IR_LENGTH)
    ir = full[start:end].copy()
    return ir, peak_idx - start


def _far_field_snr_db(ir: np.ndarray, peak_off: int, sr: int) -> float:
    peak = float(np.max(np.abs(ir)))
    if peak <= 0:
        return float("-inf")
    far_start = peak_off + sr // 10
    if far_start >= ir.size - 100:
        return float("nan")
    far = ir[far_start:]
    rms = float(np.sqrt(np.mean(far ** 2))) / peak
    return -20.0 * np.log10(rms + 1e-30)


def analyze_sweep(loopback: np.ndarray, sr: int, inverse: np.ndarray,
                  n_fft: int = 16384) -> SweepResult:
    ir_L, off_L = _farina_deconvolve(loopback[:, 0], inverse)
    ir_R, off_R = _farina_deconvolve(loopback[:, 1], inverse)
    cap_off_L = int(np.argmax(np.abs(fftconvolve(loopback[:, 0], inverse,
                                                 mode="full"))))
    cap_off_R = int(np.argmax(np.abs(fftconvolve(loopback[:, 1], inverse,
                                                 mode="full"))))
    # Magnitude (windowed to reduce edge leakage)
    f, mag_L = _windowed_mag_db(ir_L, n_fft, sr)
    _, mag_R = _windowed_mag_db(ir_R, n_fft, sr)
    # Group delay
    f_gd, gd_L = _group_delay_ms(ir_L, n_fft, sr)
    _, gd_R = _group_delay_ms(ir_R, n_fft, sr)
    snr = min(_far_field_snr_db(ir_L, off_L, sr),
              _far_field_snr_db(ir_R, off_R, sr))
    return SweepResult(
        f=f, mag_db_L=mag_L, mag_db_R=mag_R,
        group_delay_ms_L=gd_L, group_delay_ms_R=gd_R,
        ir_L=ir_L, ir_R=ir_R,
        peak_idx_L=off_L, peak_idx_R=off_R,
        capture_offset_L=cap_off_L, capture_offset_R=cap_off_R,
        far_field_snr_db=snr,
    )


# ----- pink-noise analysis (averaged spectrum) -----

@dataclass
class PinkResult:
    f: np.ndarray
    mag_db_L: np.ndarray       # captured spectrum, peak-normalized (dB)
    mag_db_R: np.ndarray
    eq_gain_db_L: np.ndarray   # captured-vs-stimulus dB ratio per bin
    eq_gain_db_R: np.ndarray
    window_start_s: float
    window_end_s: float


def _averaged_psd(x: np.ndarray, sr: int, win_s: float = 0.5
                  ) -> tuple[np.ndarray, np.ndarray]:
    """Welch-style PSD using Hann windows with 50% overlap."""
    n_win = int(round(win_s * sr))
    n_win = max(1024, 1 << (n_win.bit_length() - 1))  # power of two
    hop = n_win // 2
    win = np.hanning(n_win).astype(np.float32)
    win_norm = float(np.sum(win ** 2))
    n_frames = max(1, (len(x) - n_win) // hop + 1)
    psd_acc = np.zeros(n_win // 2 + 1)
    for i in range(n_frames):
        seg = x[i * hop: i * hop + n_win]
        if len(seg) < n_win:
            break
        H = np.fft.rfft(seg * win)
        psd_acc += (np.abs(H) ** 2) / win_norm
    psd_acc /= n_frames
    f = np.fft.rfftfreq(n_win, d=1.0 / sr)
    return f, psd_acc


def analyze_pink(loopback: np.ndarray, sr: int,
                 stim_meta: dict, stimulus_path: Path | None
                 ) -> PinkResult:
    win_start = float(stim_meta.get("analysis_window_start_seconds", 6.0))
    win_end = float(stim_meta.get("analysis_window_end_seconds", 11.0))
    n0, n1 = int(win_start * sr), int(win_end * sr)
    if n1 > loopback.shape[0]:
        n1 = loopback.shape[0]
    seg = loopback[n0:n1]

    f, psd_L = _averaged_psd(seg[:, 0], sr)
    _, psd_R = _averaged_psd(seg[:, 1], sr)
    mag_db_L_raw = 10.0 * np.log10(psd_L + 1e-30)
    mag_db_R_raw = 10.0 * np.log10(psd_R + 1e-30)

    # If we have the stimulus, compute the captured-vs-stimulus dB ratio
    # *before* peak-normalizing — the ratio recovers the system EQ.
    eq_L = np.zeros_like(mag_db_L_raw)
    eq_R = np.zeros_like(mag_db_R_raw)
    if stimulus_path is not None and stimulus_path.is_file():
        sr_s, stim = _read_wav_float(stimulus_path)
        if sr_s == sr and stim.shape[0] >= n1:
            _, spsd_L = _averaged_psd(stim[n0:n1, 0], sr)
            _, spsd_R = _averaged_psd(stim[n0:n1, 1], sr)
            ref_L_raw = 10.0 * np.log10(spsd_L + 1e-30)
            ref_R_raw = 10.0 * np.log10(spsd_R + 1e-30)
            eq_L = mag_db_L_raw - ref_L_raw
            eq_R = mag_db_R_raw - ref_R_raw

    # Peak-normalize each curve over the in-band region for plotting.
    mask = (f >= 50) & (f <= 18000)
    mag_L = mag_db_L_raw - (np.max(mag_db_L_raw[mask]) if mask.any() else 0)
    mag_R = mag_db_R_raw - (np.max(mag_db_R_raw[mask]) if mask.any() else 0)
    if eq_L.any():
        eq_L = eq_L - (np.max(eq_L[mask]) if mask.any() else 0)
        eq_R = eq_R - (np.max(eq_R[mask]) if mask.any() else 0)

    return PinkResult(
        f=f, mag_db_L=mag_L, mag_db_R=mag_R,
        eq_gain_db_L=eq_L, eq_gain_db_R=eq_R,
        window_start_s=win_start, window_end_s=win_end,
    )


# ----- multitone analysis (Goertzel per tone) -----

@dataclass
class MultitoneResult:
    freqs_hz: np.ndarray
    amp_db_L: np.ndarray       # peak-normalized per-tone amplitude (dB)
    amp_db_R: np.ndarray
    phase_rad_L: np.ndarray    # per-tone phase
    phase_rad_R: np.ndarray
    window_start_s: float
    window_end_s: float


def _single_bin_dft(signal: np.ndarray, sr: int, freq: float
                    ) -> tuple[float, float]:
    """Direct single-bin DFT magnitude + phase. Exact for stationary
    tones at any frequency (no bin-center constraint). Equivalent to
    Goertzel but vectorized and unambiguous on phase convention.

    For x[n] = sin(omega*n + phi) over N samples with omega*N/(2π)
    integer, the bin's complex value is (N/2) * exp(j*(phi - π/2)).
    Magnitude is normalized to amplitude (×2/N); phase reported in
    radians, positive = leading."""
    n = signal.size
    k = np.arange(n, dtype=np.float64)
    omega = 2.0 * np.pi * freq / sr
    Y = np.sum(signal.astype(np.float64) * np.exp(-1j * omega * k))
    return float(np.abs(Y) * 2.0 / n), float(np.angle(Y))


def analyze_multitone(loopback: np.ndarray, sr: int,
                      stim_meta: dict) -> MultitoneResult:
    win_start = float(stim_meta.get("analysis_window_start_seconds", 6.0))
    win_end = float(stim_meta.get("analysis_window_end_seconds", 11.0))
    n0, n1 = int(win_start * sr), int(win_end * sr)
    if n1 > loopback.shape[0]:
        n1 = loopback.shape[0]
    seg = loopback[n0:n1].astype(np.float64)

    freqs = np.array(stim_meta.get("tone_frequencies_hz", []), dtype=float)
    stim_phases = np.array(stim_meta.get("tone_phases_rad", [0.0] * freqs.size),
                           dtype=float)
    # The Goertzel sample window starts at n0, but the underlying tones
    # were generated from t=0 of the stimulus. Phase at sample n0 of a
    # sinusoid with starting phase phi is (omega * n0 + phi). Compensate
    # so the recovered system phase doesn't wrap arbitrarily with the
    # analysis window choice.
    # The single-bin DFT of sin(omega*k + phi) returns phase = phi - π/2
    # (sine→cosine basis offset). For a passthrough capture, the segment
    # starts at sample n0 of the stimulus, so the in-window starting
    # phase is (omega*n0 + stim_phase). The corresponding bin phase is
    # then (omega*n0 + stim_phase) - π/2; subtracting it from the
    # captured bin phase yields just the system-induced phase shift.
    omega = 2.0 * np.pi * freqs / sr
    bin_phase_for_passthrough = (
        omega * n0 + stim_phases - np.pi / 2.0
    )

    amps_L = np.zeros_like(freqs)
    amps_R = np.zeros_like(freqs)
    cap_phase_L = np.zeros_like(freqs)
    cap_phase_R = np.zeros_like(freqs)
    for i, f in enumerate(freqs):
        amps_L[i], cap_phase_L[i] = _single_bin_dft(seg[:, 0], sr, f)
        amps_R[i], cap_phase_R[i] = _single_bin_dft(seg[:, 1], sr, f)

    sys_phase_L = ((cap_phase_L - bin_phase_for_passthrough + np.pi) %
                   (2.0 * np.pi)) - np.pi
    sys_phase_R = ((cap_phase_R - bin_phase_for_passthrough + np.pi) %
                   (2.0 * np.pi)) - np.pi

    eps = 1e-30
    amp_db_L = 20 * np.log10(amps_L + eps) - 20 * np.log10(np.max(amps_L) + eps)
    amp_db_R = 20 * np.log10(amps_R + eps) - 20 * np.log10(np.max(amps_R) + eps)
    return MultitoneResult(
        freqs_hz=freqs,
        amp_db_L=amp_db_L, amp_db_R=amp_db_R,
        phase_rad_L=sys_phase_L, phase_rad_R=sys_phase_R,
        window_start_s=win_start, window_end_s=win_end,
    )


# ----- helpers (FFT mag + group delay) -----

def _windowed_mag_db(ir: np.ndarray, n_fft: int, sr: int
                     ) -> tuple[np.ndarray, np.ndarray]:
    y = ir.astype(np.float64).copy()
    n_fade = max(16, len(y) // 10)
    fade = 0.5 * (1.0 + np.cos(np.pi * np.arange(n_fade) / n_fade))
    y[-n_fade:] *= fade
    H = np.fft.rfft(y, n=n_fft)
    f = np.fft.rfftfreq(n_fft, d=1.0 / sr)
    mag = np.abs(H)
    return f, 20.0 * np.log10(mag / np.max(mag) + 1e-12)


def _group_delay_ms(ir: np.ndarray, n_fft: int, sr: int
                    ) -> tuple[np.ndarray, np.ndarray]:
    f, gd = group_delay((ir, [1.0]), w=n_fft // 2 + 1, fs=sr)
    return f, gd / sr * 1000.0


def _phase_family(ir: np.ndarray, peak_idx: int, guard: int = 8
                  ) -> tuple[str, float]:
    n = ir.size
    pre_end = max(0, peak_idx - guard)
    post_start = min(n, peak_idx + guard + 1)
    if post_start >= n and pre_end <= 0:
        return "indeterminate", 0.0
    pre = ir[:pre_end]
    post = ir[post_start:]
    pre_e = float(np.sum(pre.astype(np.float64) ** 2))
    post_e = float(np.sum(post.astype(np.float64) ** 2))
    if pre_e <= 0 and post_e <= 0:
        return "indeterminate (zero energy)", 0.0
    if pre_e <= 0:
        return "minimum-phase (no pre-peak energy)", float("inf")
    if post_e <= 0:
        return "maximum-phase (no post-peak energy)", float("-inf")
    ratio = 10.0 * np.log10(post_e / pre_e)
    if ratio > 20.0:
        return "minimum-phase (post-peak energy dominates)", ratio
    if ratio < -20.0:
        return "maximum-phase (pre-peak energy dominates)", ratio
    if abs(ratio) < 3.0:
        return "linear-phase or symmetric (post ≈ pre)", ratio
    return f"hybrid (post {'>' if ratio > 0 else '<'} pre)", ratio


# ----- reference (our generated FIR / target curve) -----

@dataclass
class Reference:
    fir_L: np.ndarray
    fir_R: np.ndarray
    band_freqs: np.ndarray
    target_db_L: np.ndarray
    target_db_R: np.ndarray


def build_reference(xml_path: Path, profile: str, curve: str) -> Reference:
    (freqs, curves, ieq_amount, ao_left, ao_right, _peq, _vl,
     _de, _surr, _mbc, _reg, _vm) = parse_xml(
        xml_path, endpoint_type="internal_speaker",
        operating_mode="normal", profile_type=profile,
    )
    scale = ieq_amount / 10.0
    ao_L = np.array(ao_left, dtype=float) / 16.0
    ao_R = np.array(ao_right, dtype=float) / 16.0
    curve_key = f"ieq_{curve}"
    if curve_key not in curves:
        raise SystemExit(
            f"XML has no curve '{curve_key}'. Available: "
            f"{sorted(k for k in curves if k.startswith('ieq_'))}"
        )
    ieq = np.array(curves[curve_key], dtype=float) / 16.0 * scale
    target_L = ieq + ao_L
    target_R = ieq + ao_R
    float_freqs = np.array(freqs, dtype=float)
    fir_L, _ = make_fir(float_freqs, target_L, normalize=True)
    fir_R, _ = make_fir(float_freqs, target_R, normalize=True)
    return Reference(fir_L=fir_L, fir_R=fir_R, band_freqs=float_freqs,
                     target_db_L=target_L, target_db_R=target_R)


# ----- comparison (per-kind output) -----

def _band_table(captured_db: np.ndarray, our_fir_db: np.ndarray,
                target_db: np.ndarray, f: np.ndarray,
                band_freqs: np.ndarray) -> str:
    target_norm = target_db - np.max(target_db)
    lines = []
    lines.append(f"  {'freq':>7}  {'target':>7}  {'ourFIR':>7}  "
                 f"{'capture':>9}  {'cap-tgt':>7}  {'cap-our':>7}")
    for i, fc in enumerate(band_freqs):
        idx = int(np.argmin(np.abs(f - fc)))
        cap = float(captured_db[idx])
        ours = float(our_fir_db[idx])
        tgt = float(target_norm[i])
        lines.append(f"  {int(fc):>6} Hz  {tgt:+6.2f}  {ours:+6.2f}  "
                     f"{cap:+8.2f}  {cap-tgt:+6.2f}  {cap-ours:+6.2f}")
    return "\n".join(lines)


def _between_band_residual(captured_db: np.ndarray, f: np.ndarray,
                           band_freqs: np.ndarray, target_db: np.ndarray
                           ) -> str:
    f_grid = np.geomspace(band_freqs[0], band_freqs[-1], 200)
    target_at_grid = interpolate_curve_db(band_freqs, target_db, f_grid)
    target_at_grid -= np.max(target_at_grid)
    cap_at_grid = np.interp(f_grid, f, captured_db)
    delta = cap_at_grid - target_at_grid
    return (f"  between-band residual (200-pt log grid 47-19688 Hz): "
            f"max |Δ| = {np.max(np.abs(delta)):.2f} dB, "
            f"p95 = {np.percentile(np.abs(delta), 95):.2f} dB, "
            f"RMS = {np.sqrt(np.mean(delta**2)):.2f} dB")


def _maybe_plot_sweep(res: SweepResult, ref: Reference, channel: str,
                      out_png: Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False
    cap_mag = res.mag_db_L if channel == "L" else res.mag_db_R
    cap_gd = res.group_delay_ms_L if channel == "L" else res.group_delay_ms_R
    cap_ir = res.ir_L if channel == "L" else res.ir_R
    cap_peak = res.peak_idx_L if channel == "L" else res.peak_idx_R
    our_fir = ref.fir_L if channel == "L" else ref.fir_R
    target = ref.target_db_L if channel == "L" else ref.target_db_R
    n_fft = (len(res.f) - 1) * 2

    f_our, mag_our = _windowed_mag_db(our_fir, n_fft, SAMPLE_RATE)
    _, gd_our = _group_delay_ms(our_fir, n_fft, SAMPLE_RATE)
    target_at_f = interpolate_curve_db(ref.band_freqs, target, res.f)
    target_norm = target_at_f - np.max(target_at_f)

    fig, axes = plt.subplots(3, 1, figsize=(10, 11))
    ax = axes[0]
    ax.semilogx(res.f, cap_mag, label="captured DAX3 IR", color="C0")
    ax.semilogx(f_our, mag_our, label="our generated FIR",
                color="C1", linestyle="--")
    ax.semilogx(res.f, target_norm, label="target curve (peak-norm)",
                color="C2", linestyle=":")
    ax.scatter(ref.band_freqs, target - np.max(target),
               color="C2", s=20, zorder=5, label="band centers")
    ax.set_xlim(20, 22000)
    ax.set_ylim(np.min(target_norm) - 6, max(np.max(cap_mag), 0) + 3)
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Magnitude (dB, peak-norm)")
    ax.set_title("Magnitude response")
    ax.legend(loc="lower left")
    ax.grid(True, which="both", alpha=0.3)

    ax = axes[1]
    ax.semilogx(res.f, cap_gd, label="captured DAX3 IR", color="C0")
    ax.semilogx(res.f, gd_our, label="our FIR", color="C1", linestyle="--")
    ax.set_xlim(50, 20000); ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Group delay (ms)"); ax.set_title("Group delay")
    ax.legend(loc="upper right"); ax.grid(True, which="both", alpha=0.3)

    ax = axes[2]
    n_show = min(len(cap_ir), 4096)
    t_ms = np.arange(n_show) * 1000.0 / SAMPLE_RATE
    cap_n = cap_ir[:n_show] / max(float(np.max(np.abs(cap_ir))), 1e-12)
    our_n = our_fir[:n_show] / max(float(np.max(np.abs(our_fir))), 1e-12)
    ax.plot(t_ms, cap_n, label="captured", color="C0", alpha=0.7)
    ax.plot(t_ms, our_n, label="our FIR", color="C1", linestyle="--", alpha=0.7)
    ax.set_xlabel("Time (ms)"); ax.set_ylabel("Amplitude (peak-norm)")
    ax.set_title("Time-domain IR (first 85 ms)")
    ax.legend(loc="upper right"); ax.grid(True, alpha=0.3)
    ax.set_xlim(0, t_ms[-1])

    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    return True


def _maybe_plot_steady(f: np.ndarray, cap_db: np.ndarray,
                       ref: Reference | None, channel: str,
                       title: str, out_png: Path,
                       extra: tuple[np.ndarray, np.ndarray, str] | None = None
                       ) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.semilogx(f, cap_db, label="captured", color="C0")
    if ref is not None:
        target = ref.target_db_L if channel == "L" else ref.target_db_R
        target_at_f = interpolate_curve_db(ref.band_freqs, target, f)
        target_norm = target_at_f - np.max(target_at_f)
        ax.semilogx(f, target_norm, label="target curve",
                    color="C2", linestyle=":")
        ax.scatter(ref.band_freqs, target - np.max(target),
                   color="C2", s=20, zorder=5)
        # our FIR magnitude reference
        n_fft = (len(f) - 1) * 2
        our_f, mag_our = _windowed_mag_db(
            ref.fir_L if channel == "L" else ref.fir_R, n_fft, SAMPLE_RATE)
        ax.semilogx(our_f, mag_our, label="our generated FIR",
                    color="C1", linestyle="--")
    if extra is not None:
        ex_f, ex_db, ex_label = extra
        ax.semilogx(ex_f, ex_db, label=ex_label, color="C3", linestyle="-.")
    ax.set_xlim(20, 22000); ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Magnitude (dB, peak-norm)")
    ax.set_title(title); ax.legend(loc="lower left")
    ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout(); fig.savefig(out_png, dpi=120); plt.close(fig)
    return True


# ----- top-level dispatcher -----

def process(loopback_path: Path, xml_path: Path | None,
            profile: str, curve: str, channel_filter: str | None) -> None:
    sidecar = _load_sidecar(loopback_path)
    kind = _stim_kind(sidecar)
    tag = _stim_tag(sidecar)
    label = sidecar.get("label", "?")
    sm = _stim_meta(sidecar)

    sr, capture = _read_wav_float(loopback_path)
    if sr != SAMPLE_RATE:
        raise SystemExit(f"{loopback_path}: sr {sr} != {SAMPLE_RATE}")

    ref = build_reference(xml_path, profile, curve) if xml_path else None
    out_dir = loopback_path.parent
    base = loopback_path.stem
    summary = [f"=== {loopback_path.name}  kind={kind}  label={label} ==="]

    channels = ("L", "R") if channel_filter is None else (channel_filter,)

    if kind == "sweep":
        inv_path = _resolve_resource(
            "inverse_sweep.npy", loopback_path,
            legacy_names=("inverse_filter.npy",),
        )
        if inv_path is None:
            raise SystemExit(
                "inverse_sweep.npy not found — run make_stimulus.py first. "
                "(searches cwd, the capture's parent dir, and the script dir)"
            )
        inv = np.load(inv_path)
        res = analyze_sweep(capture, sr, inv)
        summary.append(f"  far-field SNR (>100 ms past peak): "
                       f"{res.far_field_snr_db:+.1f} dB")
        for ch in channels:
            ir = res.ir_L if ch == "L" else res.ir_R
            poff = res.peak_idx_L if ch == "L" else res.peak_idx_R
            cap_off = res.capture_offset_L if ch == "L" else res.capture_offset_R
            mag = res.mag_db_L if ch == "L" else res.mag_db_R
            phase_label, ratio_db = _phase_family(ir, poff)
            ir_path = out_dir / f"ir_{tag}_{label}_{ch}.wav"
            wavfile.write(str(ir_path), sr,
                          (ir / max(np.max(np.abs(ir)), 1e-12)).astype(np.float32))
            summary.append("")
            summary.append(f"  channel {ch}:")
            summary.append(f"    peak @ sample {poff}/{ir.size} "
                           f"({1000*poff/sr:.2f} ms; capture offset ~{cap_off})")
            summary.append(f"    phase character: {phase_label} "
                           f"(post/pre = {ratio_db:+.1f} dB)")
            summary.append(f"    wrote {ir_path.name}")
            if ref is not None:
                our_n_fft = (len(res.f) - 1) * 2
                our_f, mag_our = _windowed_mag_db(
                    ref.fir_L if ch == "L" else ref.fir_R, our_n_fft, sr)
                target = ref.target_db_L if ch == "L" else ref.target_db_R
                summary.append(_band_table(mag, mag_our, target, res.f,
                                           ref.band_freqs))
                summary.append(_between_band_residual(mag, res.f,
                                                      ref.band_freqs, target))
                png = out_dir / f"compare_{base}_{profile}_{curve}_{ch}.png"
                if _maybe_plot_sweep(res, ref, ch, png):
                    summary.append(f"    wrote {png.name}")

    elif kind == "pink":
        stim_path = Path(sidecar.get("stimulus", {}).get("path", ""))
        if not stim_path.is_file():
            located = _resolve_resource(stim_path.name, loopback_path)
            if located is not None:
                stim_path = located
        res = analyze_pink(capture, sr, sm,
                           stim_path if stim_path.is_file() else None)
        summary.append(f"  steady-state spectrum window: "
                       f"{res.window_start_s:.1f}-{res.window_end_s:.1f} s")
        for ch in channels:
            mag = res.mag_db_L if ch == "L" else res.mag_db_R
            eq = res.eq_gain_db_L if ch == "L" else res.eq_gain_db_R
            spec_path = out_dir / f"spectrum_{tag}_{label}_{ch}.npz"
            np.savez(spec_path, f=res.f, mag_db=mag, eq_gain_db=eq)
            summary.append("")
            summary.append(f"  channel {ch}:")
            summary.append(f"    wrote {spec_path.name}")
            if ref is not None:
                summary.append(_band_table(
                    eq if eq.any() else mag,
                    _windowed_mag_db(
                        ref.fir_L if ch == "L" else ref.fir_R,
                        (len(res.f) - 1) * 2, sr)[1],
                    ref.target_db_L if ch == "L" else ref.target_db_R,
                    res.f, ref.band_freqs,
                ))
                # Between-band residual against the EQ-recovered curve when
                # we have the stimulus, else against the raw spectrum.
                cap_for_residual = eq if eq.any() else mag
                summary.append(_between_band_residual(
                    cap_for_residual, res.f, ref.band_freqs,
                    ref.target_db_L if ch == "L" else ref.target_db_R,
                ))
                png = out_dir / f"compare_{base}_{profile}_{curve}_{ch}.png"
                title = (f"Pink-noise steady-state magnitude — "
                         f"{label} / {profile}/{curve} / ch {ch}")
                extra = (res.f, eq, "recovered EQ (cap−stim)") if eq.any() else None
                if _maybe_plot_steady(res.f, mag, ref, ch, title, png, extra):
                    summary.append(f"    wrote {png.name}")

    elif kind == "multitone":
        res = analyze_multitone(capture, sr, sm)
        summary.append(f"  steady-state window: "
                       f"{res.window_start_s:.1f}-{res.window_end_s:.1f} s")
        for ch in channels:
            amp_db = res.amp_db_L if ch == "L" else res.amp_db_R
            phases = res.phase_rad_L if ch == "L" else res.phase_rad_R
            tones_path = out_dir / f"tones_{tag}_{label}_{ch}.npz"
            np.savez(tones_path, freqs_hz=res.freqs_hz,
                     amp_db=amp_db, phase_rad=phases)
            summary.append("")
            summary.append(f"  channel {ch}:")
            summary.append(f"    wrote {tones_path.name}")
            # Per-band table (this one is exact: every probe frequency is a
            # band center, no interpolation).
            target = (ref.target_db_L if (ref and ch == "L")
                      else (ref.target_db_R if ref else None))
            tnorm = (target - np.max(target)) if target is not None else None
            our_f = (None if ref is None else (res.freqs_hz, _windowed_mag_db(
                ref.fir_L if ch == "L" else ref.fir_R, 16384, sr)[1]))
            summary.append(f"    {'freq':>7}  {'amp':>7}  "
                           f"{'phase°':>7}"
                           + ("  {:>7}  {:>7}".format("target", "Δ-tgt")
                              if tnorm is not None else ""))
            for i, f in enumerate(res.freqs_hz):
                row = f"    {int(f):>6} Hz  {amp_db[i]:+6.2f}  " \
                      f"{np.degrees(phases[i]):+7.1f}"
                if tnorm is not None:
                    row += f"  {tnorm[i]:+6.2f}  {amp_db[i]-tnorm[i]:+6.2f}"
                summary.append(row)
            if ref is not None:
                # Plot
                png = out_dir / f"compare_{base}_{profile}_{curve}_{ch}.png"
                title = (f"Multitone per-band amplitude — "
                         f"{label} / {profile}/{curve} / ch {ch}")
                # Build synthetic mag curve at tone frequencies + flatten between
                f_grid = np.geomspace(res.freqs_hz[0], res.freqs_hz[-1], 400)
                cap_at_grid = np.interp(np.log(f_grid),
                                        np.log(res.freqs_hz), amp_db)
                if _maybe_plot_steady(f_grid, cap_at_grid, ref, ch, title, png):
                    summary.append(f"    wrote {png.name}")

    else:
        summary.append(f"  unknown stimulus kind: {kind!r}, skipping.")

    summary_path = out_dir / f"analysis_{base}.txt"
    summary_path.write_text("\n".join(summary) + "\n")
    print("\n".join(summary))
    print(f"  wrote {summary_path.name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("captures", nargs="+", type=Path,
                    help="loopback_*.wav files (sidecar JSON must exist)")
    ap.add_argument("--xml", type=Path,
                    help="DAX3 XML for comparison (omit to skip the diff)")
    ap.add_argument("--profile", default="dynamic",
                    help="DAX3 profile to load from the XML")
    ap.add_argument("--curve", default="balanced",
                    choices=("balanced", "detailed", "warm"))
    ap.add_argument("--channel", choices=("L", "R"), default=None,
                    help="restrict analysis to one channel")
    args = ap.parse_args()

    for cap in args.captures:
        if not cap.is_file():
            print(f"skipping (not found): {cap}", file=sys.stderr)
            continue
        print()
        try:
            process(cap, args.xml, args.profile, args.curve, args.channel)
        except SystemExit:
            raise
        except Exception as e:
            import traceback
            print(f"FAIL on {cap}: {e}", file=sys.stderr)
            traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
