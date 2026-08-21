"""The committed Finding 8 phase-2 scorer (tools/measure_ee/).

Locks the scoring arithmetic a contributor capture will be judged by:
the floor clamp on both sides, the x2 overshoot weight at/above 200 Hz,
the macro (per-tone) averaging, and the capture-runnable guard subset.
Signals are synthesized against a tiny stimulus sidecar so the numbers
are exact by construction — no fixture WAVs, no localresearch inputs.
"""
import json
from pathlib import Path

import numpy as np
import pytest

from tools.measure_ee.analyze_vbe_chain import Stimulus
from tools.measure_ee.score_vbe_chain import (
    FLOOR, CellBank, guards, score,
)

SR = 48000
TONES = [50.0, 80.0, 120.0, 180.0]


@pytest.fixture()
def stim(tmp_path: Path) -> Stimulus:
    sidecar = tmp_path / "stimulus_bass_burst.json"
    sidecar.write_text(json.dumps({
        "sample_rate_hz": SR,
        "tone_freqs_hz": TONES,
        "tone_duration_s": 1.0,
        "gap_duration_s": 0.2,
        "pre_silence_s": 0.1,
    }))
    return Stimulus(sidecar)


def synth(stim: Stimulus, partials: dict[int, list[tuple[float, float]]]
          ) -> np.ndarray:
    """A bass-burst-shaped signal: per segment index, a list of
    (freq_hz, amplitude) sines spanning that tone's slot."""
    total = int(round((stim.pre_s + len(stim.tones)
                       * (stim.tone_s + stim.gap_s)) * stim.sr))
    x = np.zeros(total)
    for idx, comps in partials.items():
        start = int(round((stim.pre_s + idx
                           * (stim.tone_s + stim.gap_s)) * stim.sr))
        n = int(round(stim.tone_s * stim.sr))
        t = np.arange(n) / stim.sr
        for freq, amp in comps:
            x[start:start + n] += amp * np.sin(2 * np.pi * freq * t)
    return x


def fundamentals(scale: float = 1.0) -> dict[int, list[tuple[float, float]]]:
    return {i: [(f, 0.1 * scale)] for i, f in enumerate(TONES)}


def test_identical_capture_scores_zero_and_passes_guards(stim):
    dax = CellBank(synth(stim, fundamentals()), stim)
    capture = CellBank(synth(stim, fundamentals()), stim)
    macro, flat = score(capture, dax)
    assert macro == 0.0 and flat == 0.0
    assert guards(capture, dax, dry=capture) == []


def test_overshoot_at_200_hz_double_weights(stim):
    """A product the DAX doesn't have, at exactly the 200 Hz boundary:
    the error doubles (added mud is worse than missing sparkle)."""
    dax = CellBank(synth(stim, fundamentals()), stim)
    extra = fundamentals()
    extra[0].append((200.0, 0.005))       # cell (50, 4), capture-only
    capture = CellBank(synth(stim, extra), stim)
    got = capture.cell_db(50.0, 4)
    assert got > FLOOR, "synthesized partial must clear the floor"
    # Six 50 Hz cells, three scored tones; only this cell errs.
    expected = 2 * (got - FLOOR) / 6 / 3
    macro, _ = score(capture, dax)
    assert macro == pytest.approx(expected, abs=1e-9)


def test_undershoot_is_never_doubled(stim):
    """The same cell missing from the capture instead: plain weight."""
    with_product = fundamentals()
    with_product[0].append((200.0, 0.005))
    dax = CellBank(synth(stim, with_product), stim)
    capture = CellBank(synth(stim, fundamentals()), stim)
    want = dax.cell_db(50.0, 4)
    expected = (want - FLOOR) / 6 / 3
    macro, _ = score(capture, dax)
    assert macro == pytest.approx(expected, abs=1e-9)


def test_shifted_fundamental_fails_g1(stim):
    dax = CellBank(synth(stim, fundamentals()), stim)
    dry = CellBank(synth(stim, fundamentals()), stim)
    hot = CellBank(synth(stim, fundamentals(scale=1.3)), stim)  # +2.3 dB
    fails = guards(hot, dax, dry)
    assert any(f.startswith("G1") for f in fails)
    # Without a dry capture the G1 half is skipped, not failed.
    assert not any(f.startswith("G1") for f in guards(hot, dax, dry=None))


def test_dirty_180_source_fails_g2(stim):
    dax = CellBank(synth(stim, fundamentals()), stim)
    leaky = fundamentals()
    leaky[3].append((360.0, 0.01))        # 2nd harmonic of the 180 Hz tone
    capture = CellBank(synth(stim, leaky), stim)
    assert "G2 180-source dirty (360 Hz)" in guards(capture, dax, dry=None)
