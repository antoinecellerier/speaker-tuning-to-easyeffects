"""FIR generator and curve interpolation.

`make_fir` is the load-bearing DSP step: it turns a target dB curve into
a minimum-phase impulse response via cepstral processing. The naive
inverse-FFT-of-magnitude approach produces a linear-phase FIR with
audible pre-ringing, so the cepstral path here must be preserved.
"""

import numpy as np
import pytest

from dolby_to_easyeffects import (
    FIR_LENGTH,
    SAMPLE_RATE,
    interpolate_curve_db,
    make_fir,
)
from tests.conftest import (
    SYNTHETIC_FREQS_20,
    fir_freq_response_db,
    is_minimum_phase,
)


# --- interpolate_curve_db ---

def test_interpolate_flat_input_flat_output():
    band_freqs = np.array(SYNTHETIC_FREQS_20, dtype=float)
    gains = np.full_like(band_freqs, 3.5)
    out = interpolate_curve_db(band_freqs, gains, np.array([100.0, 1000.0, 8000.0]))
    np.testing.assert_allclose(out, 3.5)


def test_interpolate_extrapolates_flat_at_edges():
    band_freqs = np.array([100.0, 1000.0, 10000.0])
    gains = np.array([-3.0, 0.0, 6.0])
    # Below lowest band → first gain; above highest band → last gain
    below = interpolate_curve_db(band_freqs, gains, np.array([10.0]))[0]
    above = interpolate_curve_db(band_freqs, gains, np.array([20000.0]))[0]
    assert below == pytest.approx(-3.0)
    assert above == pytest.approx(6.0)


def test_interpolate_monotone_input_monotone_output():
    band_freqs = np.array(SYNTHETIC_FREQS_20, dtype=float)
    gains = np.linspace(-6.0, 6.0, len(band_freqs))
    fft_freqs = np.geomspace(50, 16000, 64)
    out = interpolate_curve_db(band_freqs, gains, fft_freqs)
    diffs = np.diff(out)
    assert (diffs >= -1e-9).all()


def test_interpolate_log_frequency_spacing():
    """Interpolation is in log-frequency: the geometric mean of two
    adjacent bands should sit at the arithmetic mean of their dB values.
    """
    band_freqs = np.array([1000.0, 2000.0])
    gains = np.array([0.0, 6.0])
    mid = np.sqrt(1000.0 * 2000.0)
    out = interpolate_curve_db(band_freqs, gains, np.array([mid]))[0]
    assert out == pytest.approx(3.0, abs=1e-6)


# --- make_fir ---

def test_make_fir_length_and_dtype():
    fir, peak_db = make_fir(SYNTHETIC_FREQS_20, [0.0] * 20)
    assert fir.shape == (FIR_LENGTH,)
    assert np.isfinite(fir).all()
    assert np.isfinite(peak_db)


def test_make_fir_flat_curve_is_unit_impulse():
    """A 0 dB target everywhere yields an impulse at n=0 with no other
    energy and a flat (0 dB) frequency response.
    """
    fir, peak_db = make_fir(SYNTHETIC_FREQS_20, [0.0] * 20)
    # impulse should sit at sample 0
    assert np.argmax(np.abs(fir)) == 0
    assert fir[0] == pytest.approx(1.0, abs=1e-3)
    other_energy = np.sum(np.abs(fir[1:]))
    assert other_energy < 1e-3
    assert peak_db == pytest.approx(0.0, abs=1e-6)


def test_make_fir_normalises_peak_to_zero_db():
    """Peak normalisation (when enabled) should bring the FFT magnitude
    peak to ~0 dBFS regardless of the target curve's level.
    """
    gains = [0, 0, 0, 0, 6, 8, 6, 4, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    fir, _peak_db = make_fir(SYNTHETIC_FREQS_20, gains)
    _freqs, mag_db = fir_freq_response_db(fir, fs=SAMPLE_RATE)
    assert mag_db.max() == pytest.approx(0.0, abs=0.05)


def test_make_fir_non_normalised_preserves_target_level():
    """With normalize=False the unity-curve FIR is still the impulse
    (level preserved), and a +6 dB target peak shows up as +6 dB.
    """
    fir_flat, peak_flat = make_fir(SYNTHETIC_FREQS_20, [0.0] * 20, normalize=False)
    assert peak_flat == pytest.approx(0.0, abs=1e-6)
    assert fir_flat[0] == pytest.approx(1.0, abs=1e-3)

    gains = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6, 6, 0, 0, 0, 0, 0, 0, 0, 0]
    _fir, peak_db = make_fir(SYNTHETIC_FREQS_20, gains, normalize=False)
    assert peak_db == pytest.approx(6.0, abs=0.5)


def test_make_fir_response_tracks_target_curve():
    """Sample the synthesised FIR at the band centres and check the
    magnitude lines up with the target dB values (after peak normalisation
    subtracts a constant offset).
    """
    gains = [0, 0, -3, -3, 0, 0, 4, 6, 4, 0, 0, -2, -2, 0, 0, 0, 0, 0, 0, 0]
    fir, _peak_db = make_fir(SYNTHETIC_FREQS_20, gains)
    freqs, mag_db = fir_freq_response_db(fir, fs=SAMPLE_RATE)

    # Peak-normalised, so subtract 0 dB peak (which is the max of `gains`).
    target_offset = max(gains)
    for f, expected in zip(SYNTHETIC_FREQS_20, gains):
        if f >= SAMPLE_RATE / 2:
            continue
        idx = int(np.argmin(np.abs(freqs - f)))
        # Interp on a log axis is inexact at the edges; tolerate ~1.5 dB.
        assert mag_db[idx] == pytest.approx(expected - target_offset, abs=1.5), \
            f"FIR magnitude at {f} Hz: got {mag_db[idx]:.2f} dB, expected {expected - target_offset:.2f} dB"


@pytest.mark.parametrize("gains", [
    [0.0] * 20,
    [0, 0, 4, 4, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],   # bass lift
    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6, 8, 6, 0, 0],   # treble lift
    [0, 0, -3, -2, 0, 2, 4, 2, 0, -2, -1, 0, 1, 2, 3, 2, 0, 0, 0, 0],
])
def test_make_fir_is_minimum_phase(gains):
    """Cepstral construction must produce a minimum-phase IR (causal
    cepstrum). A naive iFFT of the magnitude would fail this — that's
    the whole point of the cepstral processing.
    """
    fir, _ = make_fir(SYNTHETIC_FREQS_20, gains)
    assert is_minimum_phase(fir, tol=1e-3), \
        "make_fir produced a non-minimum-phase IR — has the cepstral processing been simplified out?"


# --- helper validation ---

def test_is_minimum_phase_helper_rejects_linear_phase_fir():
    """Sanity-check on the cepstral-causality helper used above: a
    deliberately linear-phase FIR (naive iFFT of a magnitude curve)
    must *not* pass is_minimum_phase. If this regresses, the rest of
    the minimum-phase tests are vacuously true.
    """
    fft_freqs = np.fft.rfftfreq(4096, d=1.0 / 48000)
    target = np.full_like(fft_freqs, 1.0, dtype=float)
    target[100:200] = 2.0  # arbitrary bump on a flat magnitude
    spectrum = target.astype(complex)
    fir_naive = np.fft.fftshift(np.fft.irfft(spectrum, n=4096))
    assert not is_minimum_phase(fir_naive, tol=1e-3)
