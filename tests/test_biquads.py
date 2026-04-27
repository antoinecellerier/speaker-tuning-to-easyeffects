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


@pytest.mark.parametrize("order,expected_slope", [
    (1, "x1"), (2, "x2"), (3, "x3"), (4, "x4"), (99, "x4"),
])
def test_make_hp_band_slope_mapping(order, expected_slope):
    band = make_hp_band(120.0, order)
    assert band["type"] == "Hi-pass"
    assert band["slope"] == expected_slope
    assert band["gain"] == 0.0  # HP/LP is cut-only — gain field is unused


@pytest.mark.parametrize("order,expected_slope", [
    (1, "x1"), (2, "x2"), (3, "x3"), (4, "x4"), (0, "x4"),
])
def test_make_lp_band_slope_mapping(order, expected_slope):
    band = make_lp_band(8000.0, order)
    assert band["type"] == "Lo-pass"
    assert band["slope"] == expected_slope
    assert band["gain"] == 0.0


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
