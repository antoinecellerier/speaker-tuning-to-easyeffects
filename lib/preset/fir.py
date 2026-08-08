"""Minimum-phase FIR design — the convolver kernel, and the rates it uses.

The IEQ target curve and the audio-optimizer correction are realised as one
impulse response rather than a stack of bell filters, which is what lets the
response be exact instead of solved for. `make_fir`'s cepstral section is
load-bearing and is under a hard **zero-added-latency** invariant (CLAUDE.md,
"Core invariants"): a naive inverse FFT of the target magnitude gives a
*linear*-phase filter with audible pre-ringing and half the kernel's length
in group delay. Change the peak position, not the design.

**This module imports numpy at the top, and that is why the generator does
not import it at the top.** `dolby_to_easyeffects.py` defers the whole DSP
stack into function-local imports inside `main()` — numpy is ~0.35 s of a
~0.5 s start-up, and every path that returns before the emit loop (`--version`,
`--list`, `--doctor`, `--speaker-info`, an argparse error, and a tab
completion, which argcomplete re-runs the whole script for on every TAB press)
reaches none of it. It reaches this module only through those imports, so none
of those paths costs any numpy
(`tests/test_completions.py::test_the_dsp_import_is_deferred_past_every_early_return`).
The alternative was worse: importing this module at the top of the generator
breaks that trap outright.

`SAMPLE_RATE` and `FIR_LENGTH` live here rather than in a constants module
because `make_fir` reads both, and a constant sits with its user (CLAUDE.md,
"Co-locate definitions with use"). `SAMPLE_RATE` is the arguable one — it is
the pipeline's rate, not the FIR's, and its other readers (the MBC time
constants, the Nyquist line in the profile report) will end up in modules
that want no numpy. If that coupling ever bites, it earns a stdlib-only home
then; today every reader is in a process that has already paid for numpy.
"""

import numpy as np

SAMPLE_RATE = 48000
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
# large finite negative dB instead of -inf (keeps FIR peak/verification finite).
LOG_MAG_FLOOR = 1e-12


def make_fir(band_freqs: np.ndarray, gains_db: np.ndarray,
             normalize: bool = True) -> tuple[np.ndarray, float]:
    """Generate a minimum-phase FIR filter from a target dB curve.

    Uses homomorphic processing: the minimum-phase impulse response
    is constructed from the log-magnitude spectrum via the cepstrum.
    """
    n = FIR_LENGTH
    fft_freqs = np.fft.rfftfreq(n, d=1.0 / SAMPLE_RATE)

    # Interpolate target curve to FFT bins
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
