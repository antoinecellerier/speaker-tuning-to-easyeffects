"""Q15-coefficient decoders + parse-time XML warning hooks.

The Dolby tuning ships exponential-smoothing time constants and gain
coefficients as Q15 fixed-point. The decode formulas are closed-form,
but the tests pin numeric reference values rather than re-deriving the
formula — that way a refactor changing the formula can't silently stay
green by replicating the same change in both production and test.

The warning-hook tests at the bottom guard the "watching-only" XML
fields where the corpus is constant today (peak-level≈0,
ieq-bands-set=ieq_balanced); we want a clear user-facing prompt if a
future device breaks the assumption.
"""

import math
import xml.etree.ElementTree as ET

import pytest

import dolby_to_easyeffects
from dolby_to_easyeffects import (
    decode_mbc_time_constant,
    make_multiband_compressor,
    warn_unmodeled_features,
)
from tests.conftest import SYNTHETIC_FREQS_20, synthetic_mb_comp


# Reference values computed offline from the documented spec
#   tau_ms = -1000 / ((fs/block_size) * ln(coeff/32768))
# Pin concrete numbers so a refactor that *changes* the formula (rather
# than re-expressing it equivalently) cannot quietly stay green by
# replicating the change in both production and test.
@pytest.mark.parametrize("coeff,block_size,expected_ms", [
    (16384, 256, 7.694374),    # ~half — moderate smoothing
    (32000, 256, 224.878348),  # near-unity — slow smoothing (long tau)
    (1000, 256, 1.528416),     # tiny — fast smoothing (short tau)
    (16384, 128, 3.847187),    # different block size halves the time
])
def test_decode_mbc_time_constant_reference_values(coeff, block_size, expected_ms):
    got_ms = decode_mbc_time_constant(coeff, block_size=block_size)
    assert got_ms == pytest.approx(expected_ms, abs=1e-5)


def test_decode_mbc_time_constant_clamps_invalid():
    """coeff at the bounds where one_minus_alpha is 0 or >=1 should
    fall back rather than blow up — this matches the production guard.
    """
    assert decode_mbc_time_constant(0) == 100.0
    assert decode_mbc_time_constant(32768) == 100.0
    assert decode_mbc_time_constant(40000) == 100.0


def test_decode_mbc_time_constant_monotone_in_coeff():
    """Larger coeff (closer to 32768) → slower smoothing → longer tau."""
    taus = [decode_mbc_time_constant(c) for c in (1000, 5000, 16384, 30000, 32500)]
    assert taus == sorted(taus)


def test_decode_mbc_time_constant_returns_finite_for_realistic_range():
    """All coefficients in the corpus span ~10 to ~32700; results
    must be finite, positive, and within ~0.1 ms to 10 s.
    """
    for coeff in range(100, 32700, 500):
        ms = decode_mbc_time_constant(coeff)
        assert math.isfinite(ms)
        assert 0.05 < ms < 10000.0


@pytest.mark.parametrize("gain_raw,expected_ratio", [
    (32767, 1.0),       # ~unity Q15 → 1:1 (no compression)
    (32000, 1.024),     # 32768/32000
    (16384, 2.0),       # exactly half Q15 → 2:1
    (8192, 4.0),        # quarter Q15 → 4:1
])
def test_make_multiband_compressor_decodes_ratio_from_q15(gain_raw, expected_ratio):
    """The Q15 gain coefficient inside an mb-comp band group must
    produce the expected compression ratio in the emitted preset.

    Tests through make_multiband_compressor (the only production caller)
    rather than re-implementing the formula in the test.
    """
    # Single-band MBC: only band0 is active, so band0.ratio reflects gain_raw.
    mb = synthetic_mb_comp(group_count=1, bands=[
        # (xover_idx, threshold_q4, gain_raw, attack_q15, release_q15, makeup_q4)
        (20, -160, gain_raw, 30000, 32500, 0),
    ])
    out = make_multiband_compressor(mb, SYNTHETIC_FREQS_20)
    assert out["band0"]["ratio"] == pytest.approx(expected_ratio, abs=0.01)


def test_make_multiband_compressor_clamps_extreme_ratio():
    """gain_raw very near zero would explode the inverse; production
    clamps to 100:1 as a practical maximum.
    """
    mb = synthetic_mb_comp(group_count=1, bands=[
        (20, -160, 100, 30000, 32500, 0),  # gain_frac < 0.01 → clamp branch
    ])
    out = make_multiband_compressor(mb, SYNTHETIC_FREQS_20)
    assert out["band0"]["ratio"] == 100.0


# --- warn_unmodeled_features: watching-only XML fields ---

def _capture_warnings(monkeypatch, profile_xml: str) -> list[str]:
    """Run warn_unmodeled_features on a synthetic profile and capture
    whatever cprint emits. Returns one entry per emitted line.
    """
    out: list[str] = []

    def fake_cprint(style: str, text: str = "") -> None:
        out.append(text)

    monkeypatch.setattr(dolby_to_easyeffects, "cprint", fake_cprint)
    profile = ET.fromstring(profile_xml)
    warn_unmodeled_features(profile)
    return out


def test_warn_silent_on_default_values(monkeypatch):
    """The 17AA22E6 dynamic profile shape: peak-level=0, preset=ieq_balanced,
    no DSO, no advanced-virt — should print nothing.
    """
    out = _capture_warnings(monkeypatch, """
        <profile type="dynamic">
          <tuning-cp>
            <peak-level value="0"/>
            <ieq-bands-set preset="ieq_balanced"/>
          </tuning-cp>
        </profile>
    """)
    assert out == []


def test_warn_peak_level_nonzero_fires_with_db_conversion(monkeypatch):
    """value=-3 → −3/16 ≈ −0.19 dB at the standard convention; the
    warning should surface both the raw value, the dB conversion, and
    the report URL.
    """
    out = _capture_warnings(monkeypatch, """
        <profile type="dynamic">
          <tuning-cp>
            <peak-level value="-3"/>
          </tuning-cp>
        </profile>
    """)
    assert len(out) == 1
    msg = out[0]
    assert "peak-level=-3" in msg
    assert "-0.19 dB" in msg
    assert "github.com/antoinecellerier" in msg


def test_warn_ieq_bands_set_balanced_does_not_fire(monkeypatch):
    """Default (or absent) preset='ieq_balanced' is the corpus-wide
    constant — no warning expected.
    """
    out = _capture_warnings(monkeypatch, """
        <profile type="dynamic">
          <tuning-cp>
            <ieq-bands-set preset="ieq_balanced"/>
          </tuning-cp>
        </profile>
    """)
    assert out == []
    out_no_attr = _capture_warnings(monkeypatch, """
        <profile type="dynamic">
          <tuning-cp>
            <ieq-bands-set/>
          </tuning-cp>
        </profile>
    """)
    assert out_no_attr == []


def test_warn_ieq_bands_set_unusual_preset_fires(monkeypatch):
    """If the XML names anything other than ieq_balanced, surface it
    so the user can pick the matching variant and self-report.
    """
    out = _capture_warnings(monkeypatch, """
        <profile type="dynamic">
          <tuning-cp>
            <ieq-bands-set preset="ieq_warm"/>
          </tuning-cp>
        </profile>
    """)
    assert len(out) == 1
    msg = out[0]
    assert "ieq_warm" in msg
    assert "github.com/antoinecellerier" in msg


def test_warn_existing_unmodeled_features_still_fire(monkeypatch):
    """Regression guard for the original two warnings — the lambda-based
    refactor of _UNMODELED_FEATURES must not have silenced them.
    """
    out = _capture_warnings(monkeypatch, """
        <profile type="dynamic">
          <tuning-cp>
            <dynamic_speaker_optimization_enable value="1"/>
            <advanced-speaker-virtualizer-rendering-config/>
          </tuning-cp>
        </profile>
    """)
    assert any("Dynamic Speaker Optimization" in m for m in out)
    assert any("advanced speaker virtualizer" in m for m in out)
