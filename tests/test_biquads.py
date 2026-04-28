"""Biquad / shelf / pass-band builders.

These return EasyEffects parameter dicts (LSP "RLC (BT)" mode), not raw
biquad coefficients, so we exercise them by:
  - validating the returned dict structure (enum types, key presence)
  - re-deriving the underlying RBJ biquad from the dict's f0/Q/gain and
    checking the FFT-domain response against expectations
"""

import math

import numpy as np
import pytest

from dolby_to_easyeffects import (
    _shelf_q_from_s,
    make_band,
    make_hishelf_band,
    make_hp_band,
    make_lp_band,
    make_shelf_band,
)
from tests.conftest import (
    biquad_response_db,
    rbj_bell,
    rbj_hishelf,
    rbj_loshelf,
)


# --- structural checks: type/mode strings and parameter passthrough ---
# These verify only the behavior-bearing fields (filter type/mode strings
# and that f0/gain/Q reach the output). Cosmetic defaults (mute, solo,
# width) intentionally aren't pinned — they can shift without affecting
# the rendered audio.

def test_make_band_passes_parameters_through():
    band = make_band(1000.0, 3.0, q=2.0)
    assert band["type"] == "Bell"
    assert band["mode"] == "RLC (BT)"
    assert band["frequency"] == 1000.0
    assert band["gain"] == 3.0
    assert band["q"] == 2.0


# Dolby `order=N` declares an N-th-order filter. LSP RLC (BT) HP/LP
# doubles the user-facing slope internally (para_equalizer.cpp:167) and
# builds nSlope/2 cascaded 2nd-order sections (Filter.cpp calc_rlc_filter),
# so the user-facing slope x1..x4 corresponds to filter orders 2, 4, 6, 8.
# Mapping: order N → slope x{N/2}. Corpus has order ∈ {2, 4, 8}.
@pytest.mark.parametrize("order,expected_slope", [
    (2, "x1"), (4, "x2"), (6, "x3"), (8, "x4"), (99, "x4"),
])
def test_make_hp_band_slope_mapping(order, expected_slope):
    band = make_hp_band(120.0, order)
    assert band["type"] == "Hi-pass"
    assert band["slope"] == expected_slope
    assert band["gain"] == 0.0  # HP/LP is cut-only — gain field is unused


@pytest.mark.parametrize("order,expected_slope", [
    (2, "x1"), (4, "x2"), (6, "x3"), (8, "x4"), (0, "x4"),
])
def test_make_lp_band_slope_mapping(order, expected_slope):
    band = make_lp_band(8000.0, order)
    assert band["type"] == "Lo-pass"
    assert band["slope"] == expected_slope
    assert band["gain"] == 0.0


def _lsp_rlc_hp_cascade_db(f0: float, order: int, q: float, freqs: np.ndarray,
                            fs: int = 48000) -> np.ndarray:
    """|H(f)| in dB for LSP RLC (BT) HP at order N, Q=q.

    Mirrors lsp-dsp-units Filter.cpp calc_rlc_filter for FLT_BT_RLC_HIPASS:
    nSlope/2 cascaded 2nd-order sections of analog prototype
    s² / (s² + k·s + 1) with k = 2/(1+Q), each bilinear-transformed with
    cutoff pre-warped to f0. nSlope = filter order. (For odd order: extra
    1st-order section; corpus has only even orders so we skip that branch.)
    """
    assert order % 2 == 0, "corpus has only even orders"
    n_sections = order // 2
    k = 2.0 / (1.0 + q)
    # Bilinear: s_norm = c·(1-z⁻¹)/(1+z⁻¹) with c = cot(π·f0/fs) so that
    # the digital cutoff lands at f0.
    c = 1.0 / math.tan(math.pi * f0 / fs)
    c2 = c * c
    a0 = c2 + k * c + 1.0
    b = np.array([c2, -2.0 * c2, c2]) / a0
    a = np.array([1.0, (2.0 - 2.0 * c2) / a0, (c2 - k * c + 1.0) / a0])
    section_db = biquad_response_db(b, a, freqs, fs=fs)
    return n_sections * section_db


def test_make_hp_band_order_4_has_4th_order_rolloff():
    """make_hp_band(100, 4) → LSP slope=x2 → 2 cascaded 2nd-order sections
    at Q=0.707 → 4th-order asymptotic rolloff (≈24 dB/oct deep in stopband).

    Regression guard: if order→slope mapping is wrong, this fails.
    """
    band = make_hp_band(100.0, 4)
    assert band["slope"] == "x2"  # 2 cascades = 4th-order
    # Two octaves below cutoff: |H|² ≈ (f/f0)^8, so |H| ≈ -48 dB
    # for an ideal 4th-order BW. LSP's same-Q cascade differs near fc but
    # asymptotic is the same: at f0/4 we should be deep in stopband.
    db = _lsp_rlc_hp_cascade_db(100.0, order=4, q=0.707,
                                 freqs=np.array([25.0, 50.0, 200.0]))
    # f=25 (two octaves below): expect ≈ −48 dB (within a few dB of ideal BW)
    assert db[0] < -40.0
    # f=50 (one octave below): expect ≈ −24 dB (LSP same-Q ≈ −25 dB)
    assert -32.0 < db[1] < -18.0
    # f=200 (one octave above): essentially passband (≥ −1 dB)
    assert db[2] > -1.0


def test_make_hp_band_order_4_was_8th_order_before_fix():
    """Regression guard: the previous (wrong) mapping order=4 → slope=x4
    produced 4 cascaded sections = 8th-order, attenuating ≈ −50 dB at
    f0/2. Make sure we don't drift back there.
    """
    band = make_hp_band(100.0, 4)
    # If someone reverts to slope=x4 (4 cascaded sections = 8th-order):
    assert band["slope"] != "x4", (
        "order=4 should be 4th-order (slope=x2), not 8th (slope=x4) — "
        "see para_equalizer.cpp:167 for LSP's slope-doubling"
    )


def test_make_shelf_band_emits_lo_shelf():
    band = make_shelf_band(150.0, 4.0)
    assert band["type"] == "Lo-shelf"
    assert band["frequency"] == 150.0
    assert band["gain"] == 4.0


def test_make_hishelf_band_emits_hi_shelf():
    band = make_hishelf_band(8000.0, 4.0)
    assert band["type"] == "Hi-shelf"
    assert band["frequency"] == 8000.0
    assert band["gain"] == 4.0


# --- shelf-Q formula checks ---

@pytest.mark.parametrize("gain", [-15.0, -6.0, -1.0, 0.0, 1.0, 6.0, 15.0])
def test_shelf_q_from_s_butterworth_at_s1(gain):
    """For S=1.0 the formula reduces to Q ≈ 0.707 (Butterworth)."""
    assert _shelf_q_from_s(gain, 1.0) == pytest.approx(0.7071, abs=1e-3)


def test_shelf_q_from_s_symmetric_in_gain():
    """Sign of gain must not change Q (formula is symmetric in A↔1/A)."""
    for g in (1.0, 4.0, 10.0, 14.0):
        assert _shelf_q_from_s(g, 0.7) == pytest.approx(_shelf_q_from_s(-g, 0.7), abs=1e-9)


def test_shelf_q_from_s_clamps_floor():
    """Extreme S/gain combinations must not produce NaN — the inner
    `max(denom, 0.01)` guards against negative argument to sqrt.
    """
    q = _shelf_q_from_s(40.0, 0.01)
    assert math.isfinite(q)
    assert q > 0


# --- FFT-domain response checks ---

@pytest.mark.parametrize("f0,gain", [(1000.0, 6.0), (200.0, -4.0), (3000.0, 10.0)])
def test_bell_peak_matches_at_centre(f0, gain):
    """RBJ peaking-EQ |H(f0)| = 10**(gain/20) → 20*log10|H(f0)| = gain."""
    band = make_band(f0, gain, q=4.0)
    b, a = rbj_bell(band["frequency"], band["gain"], band["q"])
    db = biquad_response_db(b, a, [f0])
    assert db[0] == pytest.approx(gain, abs=0.05)


@pytest.mark.parametrize("f0,gain", [(8000.0, 6.0), (3000.0, 12.0), (5000.0, -4.0)])
def test_hishelf_asymptotic_gains(f0, gain):
    """Hi-shelf: ~0 dB well below f0, ~gain dB well above f0."""
    band = make_hishelf_band(f0, gain)
    b, a = rbj_hishelf(band["frequency"], band["gain"], band["q"])
    lo = biquad_response_db(b, a, np.geomspace(20, max(f0 / 50, 21), 8)).mean()
    hi_top = min(f0 * 50, 23000)
    hi = biquad_response_db(b, a, np.geomspace(min(f0 * 5, hi_top), hi_top, 8)).mean()
    assert abs(lo) < 1.0
    assert hi == pytest.approx(gain, abs=1.0)


@pytest.mark.parametrize("f0,gain", [(150.0, 4.0), (300.0, 8.0), (500.0, -3.0)])
def test_loshelf_asymptotic_gains(f0, gain):
    """Lo-shelf: ~gain dB well below f0, ~0 dB well above f0."""
    band = make_shelf_band(f0, gain)
    b, a = rbj_loshelf(band["frequency"], band["gain"], band["q"])
    lo = biquad_response_db(b, a, np.geomspace(20, max(f0 / 5, 21), 8)).mean()
    hi = biquad_response_db(b, a, np.geomspace(f0 * 50, 23000, 8)).mean()
    assert lo == pytest.approx(gain, abs=1.0)
    assert abs(hi) < 1.0


def test_hishelf_monotone_with_positive_gain():
    """Strictly non-decreasing magnitude vs. frequency for non-negative
    gain — the corpus only contains gain >= 0 hi-shelf entries.
    """
    band = make_hishelf_band(2700.0, 5.0)
    b, a = rbj_hishelf(band["frequency"], band["gain"], band["q"])
    freqs = np.geomspace(20, 22000, 200)
    resp = biquad_response_db(b, a, freqs)
    diffs = np.diff(resp)
    # Allow a tiny epsilon for floating-point — but no real dips.
    assert (diffs >= -1e-3).all(), \
        f"hi-shelf with positive gain dipped by {-diffs.min():.4f} dB"
