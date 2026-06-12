"""Property tests for the measurement stimuli and the side/mid analysis.

Locks in the generator invariants the scaling-validation capture campaign
relies on (docs/design-notes.md, unvalidated-scaling entries 1/2/6/11):
levels, durations, metadata contracts, and the analyzer's widening
readout against a synthetic known-widener input.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "tools" / "measure_dax"))
import make_stimulus as ms  # noqa: E402
import analyze  # noqa: E402


# ----- speech stimulus (catalogue entry 1) -----

def _active_rms_db(mono: np.ndarray) -> float:
    peak = float(np.max(np.abs(mono))) + 1e-30
    active = np.abs(mono) > peak * 10.0 ** (-40.0 / 20.0)
    return 20.0 * np.log10(float(np.sqrt(np.mean(mono[active] ** 2))))


def test_speech_levels_and_meta():
    stereo, meta = ms.make_speech(level_dbfs_rms=-18.0)
    assert meta["kind"] == "speech"
    assert meta["stereo_mode"] == "symmetric"
    assert meta["source"] in ("espeak", "shaped_noise")
    # dialog is center-panned: L must equal R exactly
    assert np.array_equal(stereo[:, 0], stereo[:, 1])
    peak_db = 20.0 * np.log10(float(np.max(np.abs(stereo))) + 1e-30)
    assert peak_db <= -1.0 + 1e-6  # peak guard
    # active-RMS within 1 dB of target unless the peak guard rescaled it
    rms_db = _active_rms_db(stereo[:, 0].astype(np.float64))
    assert rms_db <= -17.0
    assert stereo.shape[0] == meta["stimulus_samples"]
    assert meta["duration_seconds"] == pytest.approx(
        ms.SPEECH_T + ms.STEADY_TAIL)
    # analyzer window must sit inside the active region
    assert meta["analysis_window_end_seconds"] <= ms.SPEECH_T


def test_speech_is_deterministic():
    a, meta_a = ms.make_speech()
    b, meta_b = ms.make_speech()
    assert meta_a["source"] == meta_b["source"]
    assert np.array_equal(a, b)


# ----- loud stepped sine (catalogue entries 6/11) -----

def test_stepped_loud_crosses_mbc_knee():
    """The loud stepped variant exists to wake the MBC out of dormancy.
    Dev-device XML thresholds decode to ≈ −6.44 dBFS; the held-tone RMS
    must clear that at unity chain gain (the −18/−42 variants don't)."""
    stereo, meta = ms.make_stepped_sine(level_dbfs_peak=-2.0)
    peak_db = 20.0 * np.log10(float(np.max(np.abs(stereo))))
    assert peak_db == pytest.approx(-2.0, abs=0.1)
    # held-tone RMS = peak − 3.01 dB for a sine
    assert peak_db - 3.01 > -6.44
    assert meta["segments"], "analyzer needs per-tone segment indices"


# ----- side/mid widening readout (catalogue entry 2) -----

def _write_stim(tmp_path: Path, stereo: np.ndarray) -> Path:
    p = tmp_path / "stimulus_stereo_pink.wav"
    wavfile.write(str(p), ms.SR, stereo.astype(np.float32))
    return p


def test_sm_delta_recovers_known_widener(tmp_path):
    """Apply a known +3 dB Side gain to the decorrelated stereo pink and
    check analyze_pink reads the widening transfer back."""
    stereo, meta = ms.make_stereo_pink(level_dbfs_rms=-18.0)
    stim_path = _write_stim(tmp_path, stereo)
    mid = (stereo[:, 0] + stereo[:, 1]) / 2.0
    side = (stereo[:, 0] - stereo[:, 1]) / 2.0
    g = 10.0 ** (3.0 / 20.0)
    capture = np.column_stack([mid + g * side, mid - g * side])
    res = analyze.analyze_pink(capture.astype(np.float32), ms.SR, meta,
                               stim_path)
    assert res.sm_delta_db is not None
    in_band = (res.f >= 200) & (res.f <= 18000)
    med = float(np.median(res.sm_delta_db[in_band]))
    # +3 dB amplitude on S → S PSD ×10^(3/10) → S/M PSD ratio +3.0 dB
    assert med == pytest.approx(3.0, abs=0.5)


def test_sm_skipped_for_symmetric_stimulus(tmp_path):
    stereo, meta = ms.make_pink(level_dbfs_rms=-18.0)
    p = tmp_path / "stimulus_pink.wav"
    wavfile.write(str(p), ms.SR, stereo.astype(np.float32))
    res = analyze.analyze_pink(stereo, ms.SR, meta, p)
    assert res.sm_db is None and res.sm_delta_db is None


# ----- absolute-level transfer (catalogue entry 8) -----

def test_eq_raw_recovers_known_broadband_gain(tmp_path):
    """A flat −3 dB chain must read back as −3 dB in eq_gain_db_raw —
    the broadband-level observable the normalized curves destroy (the
    PEQ anti-clipping trim lives there)."""
    stereo, meta = ms.make_pink(level_dbfs_rms=-18.0)
    p = tmp_path / "stimulus_pink.wav"
    wavfile.write(str(p), ms.SR, stereo.astype(np.float32))
    capture = (stereo * 10.0 ** (-3.0 / 20.0)).astype(np.float32)
    res = analyze.analyze_pink(capture, ms.SR, meta, p)
    assert res.eq_gain_db_raw_L is not None
    in_band = (res.f >= 200) & (res.f <= 18000)
    med = float(np.median(res.eq_gain_db_raw_L[in_band]))
    assert med == pytest.approx(-3.0, abs=0.2)
    # and the normalized curve must NOT carry the offset (peak at 0 dB)
    assert float(np.max(res.eq_gain_db_L[in_band])) == pytest.approx(
        0.0, abs=0.2)
