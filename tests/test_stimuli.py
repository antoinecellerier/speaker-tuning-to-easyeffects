"""Property tests for the measurement stimuli and the side/mid analysis.

Locks in the generator invariants the scaling-validation capture campaign
relies on (research `r-dialog-enhancer-gain-ceiling`,
`r-surround-boost-stereo-base`, `r-mbc-ratio-time-constants`,
`r-fixed-dynamics-constants`):
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


# ----- speech stimulus (`r-dialog-enhancer-gain-ceiling`) -----

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


# ----- human-speech probes built from a recording -----

def _fake_recording() -> tuple[np.ndarray, dict]:
    """3 s of syllable-rate modulated noise with a pause: a stand-in clip."""
    rng = np.random.default_rng(5)
    t = np.arange(3 * ms.SR) / ms.SR
    clip = rng.standard_normal(t.size) * (0.55 + 0.45 * np.cos(2 * np.pi * 4 * t))
    clip[ms.SR:ms.SR + ms.SR // 3] = 0.0
    return 0.1 * clip, {"source": "file", "source_name": "fake.wav",
                        "source_sha256": "0" * 64}


def test_speech_human_is_centred_at_the_speech_level():
    clip, prov = _fake_recording()
    stereo, meta = ms.make_speech_human(clip, prov)
    assert np.array_equal(stereo[:, 0], stereo[:, 1])
    assert _active_rms_db(stereo[:, 0].astype(np.float64)) == pytest.approx(-18.0, abs=1.0)
    assert meta["kind"] == "speech" and meta["source_sha256"] == "0" * 64
    assert meta["duration_seconds"] == pytest.approx(ms.HUMAN_SPEECH_T + ms.STEADY_TAIL)


def test_speech_over_bed_puts_only_bed_in_the_side():
    clip, prov = _fake_recording()
    stereo, meta = ms.make_speech_over_bed(clip, prov)
    active = slice(ms.SR, int(ms.HUMAN_SPEECH_T * ms.SR) - ms.SR)
    side = (stereo[active, 0].astype(np.float64) - stereo[active, 1]) / 2
    # independent equal beds: side RMS = bed RMS - 3 dB; speech cancels
    bed_db = -18.0 + ms.SPEECH_BED_DB + meta["peak_guard_scale_db"]
    side_db = 20 * np.log10(np.sqrt(np.mean(side ** 2)))
    assert side_db == pytest.approx(bed_db - 3.0, abs=0.5)
    assert meta["stereo_mode"] == "asymmetric"


def test_speech_then_pink_switches_at_the_split():
    clip, prov = _fake_recording()
    stereo, meta = ms.make_speech_then_pink(clip, prov)
    mono = stereo[:, 0].astype(np.float64)
    assert np.array_equal(stereo[:, 0], stereo[:, 1])
    split = int(ms.SPEECH_THEN_PINK_SPLIT_S * ms.SR)
    pink = mono[split + ms.SR // 10:int(ms.HUMAN_SPEECH_T * ms.SR) - ms.SR // 10]
    assert 20 * np.log10(np.sqrt(np.mean(pink ** 2))) == pytest.approx(-18.0, abs=0.5)
    assert meta["pink_seconds"] == [ms.SPEECH_THEN_PINK_SPLIT_S, ms.HUMAN_SPEECH_T]


def test_speech_over_bed_records_the_measured_gap():
    """The peak guard can lower the speech but not the bed, so the sidecar
    must carry the gap as built, not just the target."""
    clip, prov = _fake_recording()
    # A -3 dBFS speech target cannot fit under the -1 dBFS peak guard.
    _, meta = ms.make_speech_over_bed(clip, prov, level_dbfs_rms=-3.0)
    gap = meta["bed_db_re_speech_measured"]
    assert gap > ms.SPEECH_BED_DB + 0.5   # bed closer to the speech than -10


def test_unsigned_8bit_source_is_centred(tmp_path):
    t = np.arange(ms.SR) / ms.SR
    pcm = (128 + 100 * np.sin(2 * np.pi * 440 * t)).astype(np.uint8)
    src = tmp_path / "u8.wav"
    wavfile.write(str(src), ms.SR, pcm)
    mono, _ = ms.load_speech_source(src)
    assert abs(float(np.mean(mono))) < 0.01
    assert 0.7 < float(np.max(np.abs(mono))) < 0.8


def test_librivox_rejects_a_download_with_the_wrong_hash(tmp_path,
                                                         monkeypatch):
    import io
    import urllib.request

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=0: _Resp(b"not the reading"))
    with pytest.raises(SystemExit, match="not the pinned"):
        ms.fetch_librivox_clip(tmp_path)
    assert not (tmp_path / "speech_source_librivox.mp3").exists()


def test_speech_source_librivox_reaches_the_sidecars(tmp_path, monkeypatch):
    clip, _ = _fake_recording()
    wav = tmp_path / "clip.wav"
    wavfile.write(str(wav), ms.SR, clip.astype(np.float32))
    monkeypatch.setattr(ms, "fetch_librivox_clip",
                        lambda out_dir: (wav, {"source_url": "u"}))
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(ms, "OUT_DIR", out)
    ms.main(["--speech-source", "librivox", "--speech-only"])
    import json
    meta = json.loads((out / "stimulus_speech_human.json").read_text())
    assert meta["source_url"] == "u" and meta["source_name"] == "clip.wav"


def test_help_writes_no_stimuli(tmp_path, monkeypatch, capsys):
    """`--help` once ran the whole build into the caller's directory."""
    monkeypatch.setattr(ms, "OUT_DIR", tmp_path)
    with pytest.raises(SystemExit):
        ms.main(["--help"])
    assert "--speech-source" in capsys.readouterr().out
    assert not any(tmp_path.iterdir())


def test_speech_only_builds_just_the_four_probes(tmp_path, monkeypatch):
    clip, _ = _fake_recording()
    src = tmp_path / "src.wav"
    wavfile.write(str(src), ms.SR, clip.astype(np.float32))
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(ms, "OUT_DIR", out)
    ms.main(["--speech-source", str(src), "--speech-only"])
    assert sorted(p.name for p in out.glob("*.wav")) == [
        "stimulus_pink_then_speech.wav", "stimulus_speech_human.wav",
        "stimulus_speech_over_bed.wav", "stimulus_speech_then_pink.wav"]


def test_pink_then_speech_switches_at_the_split():
    clip, prov = _fake_recording()
    stereo, meta = ms.make_pink_then_speech(clip, prov)
    mono = stereo[:, 0].astype(np.float64)
    assert np.array_equal(stereo[:, 0], stereo[:, 1])
    split = int(ms.SPEECH_THEN_PINK_SPLIT_S * ms.SR)
    pink = mono[ms.SR // 10:split - ms.SR // 10]
    assert 20 * np.log10(np.sqrt(np.mean(pink ** 2))) == pytest.approx(-18.0, abs=0.5)
    speech = mono[split:int(ms.HUMAN_SPEECH_T * ms.SR)]
    assert _active_rms_db(speech) == pytest.approx(-18.0, abs=1.0)
    assert meta["speech_seconds"] == [ms.SPEECH_THEN_PINK_SPLIT_S, ms.HUMAN_SPEECH_T]


# ----- IEQ probes (`r-ieq-adapts-to-content`) -----

BAND_HZ = [250, 500, 1000, 1500, 2000, 2250, 2500, 3000, 4000, 6000, 8000]


def analyze_bands(x: np.ndarray) -> np.ndarray:
    """Power in a sixth-octave band around each BAND_HZ, in dB."""
    from scipy.signal import welch
    f, p = welch(np.asarray(x, np.float64), fs=ms.SR, nperseg=8192)
    return np.array([10 * np.log10(np.sum(p[(f > c * 2 ** -(1 / 12))
                                             & (f < c * 2 ** (1 / 12))]))
                     for c in BAND_HZ])


def test_pink_steps_hold_each_level_with_one_spectrum():
    stereo, meta = ms.make_pink_steps()
    mono = stereo[:, 0].astype(np.float64)
    guard = ms.SR // 2   # clear of the ramps and the fades
    bands = [analyze_bands(mono[int(start * ms.SR) + guard:
                                int(end * ms.SR) - guard])
             for start, end, _ in meta["segments"]]
    # Segment RMS drifts with the infrasonic part of the pink, so compare
    # audible bands. Every band steps by the level difference: the spectrum
    # stays put and only the level can move an adaptive EQ.
    levels = [lv for _, _, lv in meta["segments"]]
    for b, lv in zip(bands[1:], levels[1:]):
        assert np.max(np.abs(b - bands[0] - (lv - levels[0]))) < 0.5


def test_pink_bump_peaks_at_its_centre_and_spares_far_bands():
    bumped, meta = ms.make_pink_bump()
    # The same noise without the bump, so the difference is the bump alone.
    plain = ms._pink_noise(int(round(ms.PINK_BUMP_T * ms.SR)), seed=0)
    a = slice(int(meta["analysis_window_start_seconds"] * ms.SR),
              int(meta["analysis_window_end_seconds"] * ms.SR))
    db = analyze_bands(bumped[a, 0]) - analyze_bands(plain[a])
    db -= np.median(db)   # level renormalisation shifts every band equally
    f = np.array(BAND_HZ)
    at = int(np.argmin(np.abs(f - ms.PINK_BUMP_HZ)))
    assert db[at] == pytest.approx(ms.PINK_BUMP_DB, abs=1.0)
    assert np.all(np.abs(db[(f < 1000) | (f > 5000)]) < 1.0)


# ----- loud stepped sine (`r-mbc-ratio-time-constants`,
# `r-fixed-dynamics-constants`) -----

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


# ----- side/mid widening readout (`r-surround-boost-stereo-base`) -----

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


# ----- absolute-level transfer (`r-peq-anti-clipping-trim`) -----

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


def test_eq_raw_cancels_capture_latency(tmp_path):
    """A loopback trails its stimulus by the graph latency (~250 ms on the
    EE route). The ratio must compare the same noise on both sides, or the
    stimulus's random spectrum leaves ~0.75 dB of bin-to-bin scatter."""
    stereo, meta = ms.make_pink(level_dbfs_rms=-18.0)
    p = tmp_path / "stimulus_pink.wav"
    wavfile.write(str(p), ms.SR, stereo.astype(np.float32))
    lag = 12384                                    # 258 ms, as measured
    capture = np.vstack([np.zeros((lag, 2), np.float32), stereo])
    capture = (capture * 10.0 ** (-3.0 / 20.0)).astype(np.float32)
    res = analyze.analyze_pink(capture, ms.SR, meta, p)
    band = (res.f >= 40) & (res.f <= 150)
    assert float(np.std(np.diff(res.eq_gain_db_raw_L[band]))) < 0.05
    assert float(np.median(res.eq_gain_db_raw_L[band])) == pytest.approx(
        -3.0, abs=0.05)


# ----- level ladder and level steps (issue #140) -----

def _tone_peak_db(x: np.ndarray) -> float:
    return 20.0 * np.log10(float(np.max(np.abs(x))) + 1e-30)


def test_level_ladder_holds_each_rung_at_its_level():
    stereo, meta = ms.make_levels()
    assert meta["kind"] == "levels"
    assert np.array_equal(stereo[:, 0], stereo[:, 1])
    segs = meta["segments"]
    assert len(segs) == len(ms.LEVELS_FREQS_HZ) * len(ms.LEVELS_DBFS_PEAK)
    assert ms.LEVELS_DBFS_PEAK[0] == -34 and ms.LEVELS_DBFS_PEAK[-1] == -2
    for s in segs:
        tone = stereo[s["start_sample"]:s["start_sample"] + s["len_sample"], 0]
        assert _tone_peak_db(tone) == pytest.approx(s["level_dbfs_peak"],
                                                     abs=0.05)
        amp, _ = analyze._single_bin_dft(tone[int(ms.STEPPED_SETTLE_S * ms.SR):],
                                         ms.SR, s["freq_hz"])
        assert 20 * np.log10(amp) == pytest.approx(s["level_dbfs_peak"],
                                                   abs=0.1)
    assert stereo.shape[0] == meta["stimulus_samples"]
    assert _tone_peak_db(stereo) < 0.0


def test_level_steps_step_where_the_plan_says():
    stereo, meta = ms.make_levelstep()
    assert meta["kind"] == "levelstep"
    steps = meta["steps"]
    assert len(steps) == len(ms.LEVELSTEP_FREQS_HZ) * len(ms.LEVELSTEP_PLAN)
    ramp = int(ms.LEVELSTEP_RAMP_MS * 1e-3 * ms.SR) + 1
    for s in steps:
        seg = stereo[s["start_sample"] + ramp:
                     s["start_sample"] + s["len_sample"] - ramp, 0]
        assert _tone_peak_db(seg) == pytest.approx(s["level_dbfs_peak"],
                                                   abs=0.05)
    # each frequency's steps are contiguous: one continuous tone
    for i in range(0, len(steps), len(ms.LEVELSTEP_PLAN)):
        run = steps[i:i + len(ms.LEVELSTEP_PLAN)]
        for a, b in zip(run, run[1:]):
            assert a["start_sample"] + a["len_sample"] == b["start_sample"]


def test_the_dynamics_probes_are_deterministic():
    for make in (ms.make_levels, ms.make_levelstep):
        a, _ = make()
        b, _ = make()
        assert np.array_equal(a, b)


def test_dynamics_only_writes_just_the_two_probes(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "OUT_DIR", tmp_path)
    ms.main(["--dynamics-only"])
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "stimulus_levels.json", "stimulus_levels.wav",
        "stimulus_levelstep.json", "stimulus_levelstep.wav"]
