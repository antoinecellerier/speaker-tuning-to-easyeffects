"""Minimum-phase FIR design: the convolver kernel, and the rates it uses.

The IEQ target curve and the audio-optimizer correction are realised as one
impulse response rather than a stack of bell filters. That is what lets the
response be exact instead of solved for. `make_fir`'s cepstral section is
load-bearing and is under a hard **zero-added-latency** invariant (CLAUDE.md,
"Core invariants"). A naive inverse FFT of the target magnitude gives a
*linear*-phase filter with audible pre-ringing and half the kernel's length
in group delay. Change the peak position, not the design.

**This module imports numpy at the top, and that is why the generator does
not import it at the top.** `dolby_to_easyeffects.py` defers the whole DSP
stack into function-local imports inside `main()`. Numpy is ~0.35 s of a
~0.5 s start-up, and every path that returns before the emit loop reaches
none of it: `--version`, `--list`, `--doctor`, `--speaker-info`, an argparse
error, and a tab completion, which argcomplete re-runs the whole script for
on every TAB press. The generator reaches this module only through those
imports, so none of those paths costs any numpy
(`tests/test_layout.py::test_the_dsp_import_is_deferred_past_every_early_return`).
Importing this module at the top of the generator would break that trap
outright.

`FIR_LENGTH` lives here because `make_fir` reads it, and a constant sits with
its user (CLAUDE.md, "Co-locate definitions with use"). `SAMPLE_RATE` is the
pipeline's rate, not the FIR's. It lives in `lib/preset/bands.py`, beside
`make_convolver`, the plugin whose kernel carries the rate. `--doctor`
compares it against the running PipeWire graph rate, and the doctor path must
not pay the DSP import. It is imported back here for `make_fir`'s own use, so
every `fir.SAMPLE_RATE` reader keeps resolving.
"""

import numpy as np

from lib.preset.bands import SAMPLE_RATE

FIR_LENGTH = 4096  # ~85ms, plenty for EQ


def interpolate_curve_db(band_freqs: np.ndarray, band_gains_db: np.ndarray,
                         fft_freqs: np.ndarray) -> np.ndarray:
    """Interpolate a gain curve (in dB) to FFT frequency bins.

    Uses log-frequency interpolation with linear dB values.
    Extrapolates flat beyond the band edges.
    """
    log_bands = np.log(np.maximum(band_freqs, 1.0))
    log_fft = np.log(np.maximum(fft_freqs, 1.0))
    return np.interp(log_fft, log_bands, band_gains_db,
                     left=band_gains_db[0], right=band_gains_db[-1])


# Floor added to a linear magnitude before 20*log10 so a true zero maps to a
# large finite negative dB instead of -inf. That keeps the FIR peak and the
# verification finite.
LOG_MAG_FLOOR = 1e-12


def biquad_cascade_db(sections, freqs: np.ndarray) -> np.ndarray:
    """|H| in dB of a biquad cascade at *freqs*, at the pipeline's rate.

    Each section is (b0, b1, b2, a1, a2), with the denominator
    1 + a1·z⁻¹ + a2·z⁻², the layout a vendor APO config ships.
    """
    z1 = np.exp(-2j * np.pi * np.asarray(freqs, dtype=float) / SAMPLE_RATE)
    h = np.ones_like(z1)
    for b0, b1, b2, a1, a2 in sections:
        h *= (b0 + b1 * z1 + b2 * z1 * z1) / (1.0 + a1 * z1 + a2 * z1 * z1)
    return 20.0 * np.log10(np.abs(h) + LOG_MAG_FLOOR)


# How far below its own peak a target folding in a vendor EQ may reach. A
# high-pass section has a null at DC, and asking the cepstral design for one
# aliases in time: 14.8 dB of error at 53 Hz on the #113 Surface MainEQ. A
# smooth floor 40 dB down leaves that fold within 0.32 dB of its target from
# 20 Hz up. A hard clamp gave 1.0, and floors of 50 or 60 dB did worse at
# this FIR length. Research `r-surface-apo-efx` has the sweep.
FIR_TARGET_RANGE_DB = 40.0


def design_target_db(band_freqs, gains_db, freqs: np.ndarray,
                     eq_sections=(), eq_curve=None) -> np.ndarray:
    """The dB curve `make_fir` designs from, evaluated at *freqs*.

    The band curve is interpolated as `interpolate_curve_db` does. A vendor
    EQ, when given, is added at each frequency: its biquads exactly, its
    *eq_curve* ``(hz, db)`` interpolated the same way as the band curve. The
    sum is then floored smoothly, in power, `FIR_TARGET_RANGE_DB` below its
    peak. The peak is always taken over the FFT bins `make_fir` designs on,
    so a check graded on any other grid sees the floor the filter was
    designed with.
    """
    def unfloored(at):
        at = np.asarray(at, dtype=float)
        db = interpolate_curve_db(np.array(band_freqs, dtype=float),
                                  np.array(gains_db, dtype=float), at)
        if eq_sections:
            db = db + biquad_cascade_db(eq_sections, at)
        if eq_curve is not None:
            db = db + interpolate_curve_db(np.array(eq_curve[0], dtype=float),
                                           np.array(eq_curve[1], dtype=float),
                                           at)
        return db
    target = unfloored(freqs)
    if eq_sections or eq_curve is not None:
        bins = np.fft.rfftfreq(FIR_LENGTH, d=1.0 / SAMPLE_RATE)
        floor_db = unfloored(bins).max() - FIR_TARGET_RANGE_DB
        target = 10.0 * np.log10(10.0 ** (target / 10.0) +
                                 10.0 ** (floor_db / 10.0))
    return target


def make_fir(band_freqs: np.ndarray, gains_db: np.ndarray,
             normalize: bool = True,
             eq_sections=(), eq_curve=None) -> tuple[np.ndarray, float]:
    """Generate a minimum-phase FIR filter from a target dB curve.

    Uses homomorphic processing: the minimum-phase impulse response
    is constructed from the log-magnitude spectrum via the cepstrum.
    *eq_sections* and *eq_curve* fold a vendor APO's biquad cascade and dB
    curve into the same filter (`design_target_db`). Without either the
    design is the band curve alone, bit for bit.
    """
    n = FIR_LENGTH
    fft_freqs = np.fft.rfftfreq(n, d=1.0 / SAMPLE_RATE)

    # Interpolate target curve to FFT bins
    if eq_sections or eq_curve is not None:
        gains_at_bins = design_target_db(band_freqs, gains_db, fft_freqs,
                                         eq_sections, eq_curve)
    else:
        gains_at_bins = interpolate_curve_db(
            np.array(band_freqs, dtype=float),
            np.array(gains_db, dtype=float),
            fft_freqs
        )

    # Log magnitude (natural log for cepstral processing)
    log_mag = gains_at_bins * (np.log(10.0) / 20.0)  # dB to ln(linear)

    # Minimum-phase via cepstrum:
    # 1. IFFT of log-magnitude gives the real cepstrum
    # 2. Causal windowing (double positive-time, zero negative-time)
    # 3. FFT back gives log(H_min) = log|H| + j*phase_min
    # 4. exp() gives H_min, IFFT gives impulse response
    cepstrum = np.fft.irfft(log_mag, n=n)
    # Causal window: keep n=0, double n=1..N/2-1, zero n=N/2..N-1
    cepstrum[1:n // 2] *= 2.0
    cepstrum[n // 2 + 1:] = 0.0
    # Reconstruct minimum-phase spectrum
    log_H_min = np.fft.rfft(cepstrum, n=n)
    H_min = np.exp(log_H_min)
    fir = np.fft.irfft(H_min, n=n)

    peak_mag = np.max(np.abs(H_min))
    peak_db = 20.0 * np.log10(peak_mag + LOG_MAG_FLOOR)

    if normalize:
        if peak_mag > 0:
            fir /= peak_mag

    return fir, peak_db
