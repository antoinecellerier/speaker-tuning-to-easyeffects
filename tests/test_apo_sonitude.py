"""The Sonitude tuning (`lib/apo/sonitude.py`) and the vendor-only run.

Every archive here is synthetic: a writer below builds the `SSIDMAP.bin`,
manifest and `Preset.bin` shapes the parser reads, so no HP file ships with
the tests. The corpus tier reads the real archives.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

import dolby_to_easyeffects
from lib import console, driver_package as dp, tuning_source
from lib.apo import discover as apo_discover
from lib.apo import layer as apo_layer
from lib.apo import sonitude
from lib.dax import discover as dax_discover
from lib.dax import parse
from lib.preset import emit, fir
from lib.report import messages
from tests.conftest import write_synthetic_tuning_xml

_ARCHIVE = "SEAPODAT.INTELAUDIO.FUNC_01-VEN_10EC-DEV_0285.zip"
_HDA_ARCHIVE = "SEAPODAT.HDAUDIO.FUNC_01-VEN_10EC-DEV_0285.zip"
_WRAPPER = Path(dolby_to_easyeffects.__file__).parent / "dolby_to_pipewire.py"


def _ssidmap(entries: dict[str, int], size: int = 20) -> bytes:
    body = b"".join(struct.pack(f"<I{size - 4}s", n,
                                f"SUBSYS_{s}".encode()
                                + (b"&REV_1000" if size == 32 else b""))
                    for s, n in entries.items())
    return struct.pack("<4sIII", b"SRSM", 1, len(entries), size) + body


def _curve(step_at: int | None = 72) -> list[float]:
    """A speaker-like curve: a 12 dB/octave roll-off up to *step_at*, then a
    bass bump over a midrange dip; a jump at *step_at* when given."""
    out = []
    for k in range(sonitude.CURVE_POINTS):
        f = 10 * 2 ** (k / 24)
        eq = 8 * math.exp(-math.log2(f / 100) ** 2 * 4) \
            - 6 * math.exp(-math.log2(f / 600) ** 2 * 2)
        if step_at is not None and k <= step_at:
            out.append(-5.0 - 0.5 * (step_at - k))
        else:
            out.append(eq)
    return out


def _preset_bin(left: list[float], right: list[float] | None = None,
                high_pass=(80.0, 80.0)) -> bytes:
    """A Preset.bin in the stage/key layout: a header the parser skips, a
    section table pointing at the body, an empty stage, then stage 5 with
    the two curves (keys 13, 14) and the high-pass corners (keys 19, 20;
    None = off, bytes = that payload as it is)."""
    def rec(payload: bytes) -> bytes:
        return struct.pack("<I", len(payload)) + payload

    def curve(c):
        return (struct.pack("<H", len(c)) + struct.pack(f"<{len(c)}f", *c)
                + b"\0\0")

    def corner(hz):
        if isinstance(hz, bytes):
            return rec(hz)
        return rec(struct.pack("<Bf", 0 if hz is None else 1, hz or 0.0))

    keys = {13: rec(curve(left)),
            14: rec(curve(right if right is not None else left)),
            19: corner(high_pass[0]), 20: corner(high_pass[1])}
    body = (struct.pack("<iii", 6, 2**31 - 1, -2**31)
            + struct.pack("<iii", 5, 13, 20)
            + b"".join(keys.get(k, struct.pack("<I", 0))
                       for k in range(13, 21)))
    start = 0x28 + 32
    table = struct.pack("<8I", 0, 0, 0, 0, start, len(body), 0, 0)
    return struct.pack("<I", 0x100006) + b"\0" * 36 + table + body


def _archive(path: Path, entries: dict[str, int],
             presets: dict[int, bytes | dict[str, bytes]],
             codename: str = "X_Synth") -> Path:
    """An archive mapping *entries*; a preset given as bytes holds the music
    profile's file only, one given as a dict profile → bytes holds each."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("SSIDMAP.bin", _ssidmap(entries))
        for n, data in presets.items():
            z.writestr(f"{n}.{codename}.txt", "Product/Family: synthetic\n")
            for profile, blob in (data.items() if isinstance(data, dict)
                                  else [("music", data)]):
                z.writestr(f"{n}/{sonitude.speaker_preset(profile)}", blob)
    return path


def _three_modes() -> dict[str, bytes]:
    """Music, Movie and Voice presets that differ in curve and high-pass."""
    return {"music": _preset_bin(_curve()),
            "movie": _preset_bin(_curve(step_at=90), high_pass=(120.0, 120.0)),
            "voice": _preset_bin(_ramp(), high_pass=(None, None))}


@pytest.fixture
def unheard_movie_and_voice(monkeypatch):
    """The Sonitude format with Movie and Voice still unheard, as it shipped
    before a listener compared them: the marking path's own tests. Every
    lookup in lib/ reaches a format through `discover.STANDALONE`."""
    fmt = replace(sonitude.FORMAT, experimental=("movie", "voice"))
    monkeypatch.setattr(apo_discover, "STANDALONE", (fmt,))
    return fmt


# --- the containers ----------------------------------------------------------

@pytest.mark.parametrize("size", [20, 32])
def test_ssidmap_maps_each_subsystem_to_its_preset(size):
    data = _ssidmap({"103C860F": 177, "103C860C": 157}, size)
    assert sonitude.read_ssidmap(data) == {"103C860F": 177, "103C860C": 157}


def test_an_ssidmap_of_another_shape_is_refused():
    data = _ssidmap({"103C860F": 177})
    with pytest.raises(ValueError):
        sonitude.read_ssidmap(data[:-1])
    with pytest.raises(ValueError):
        sonitude.read_ssidmap(b"XXXX" + data[4:])


def test_both_curves_are_read_in_file_order():
    left, right = _curve(), [v + 1 for v in _curve()]
    curves = sonitude.read_curves(_preset_bin(left, right))
    assert len(curves) == 2
    assert np.allclose(curves[0], left, atol=1e-5)
    assert np.allclose(curves[1], right, atol=1e-5)


def test_the_curve_grid_is_a_24th_octave_from_10_hz():
    g = sonitude.curve_grid_hz()
    assert len(g) == 265 and g[0] == 10.0
    assert math.isclose(g[24], 20.0) and math.isclose(g[-1], 20480.0)


# --- binding a device --------------------------------------------------------

def test_find_binds_the_subsystem_and_reads_the_speaker_curve(tmp_path):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    layer = sonitude.find(a, "10EC0285", "103c860f", prefer_intelaudio=True)
    assert layer is not None and layer.whole_tuning and layer.default_on
    assert layer.label == "Sonitude speaker tuning (preset 177, X_Synth)"
    assert layer.hardware_id == ("INTELAUDIO\\FUNC_01&VEN_10EC&DEV_0285"
                                 "&SUBSYS_103C860F")
    assert layer.eq_left == () and layer.dynamics == ()
    assert len(layer.curve_hz) == 265
    # Above the break (80 Hz, point 72) the curve is read as is, cuts only.
    assert np.allclose(layer.curve_left_db[73:], np.minimum(_curve(), 0)[73:],
                       atol=1e-5)


def test_each_profile_reads_its_own_curve_and_high_pass(tmp_path):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177}, {177: _three_modes()})
    layers = sonitude.load_profiles(a, 177, "INTELAUDIO", "10EC0285",
                                    "103C860F")
    assert list(layers) == ["music", "movie", "voice"]
    music, movie, voice = layers.values()
    assert movie.config_path == a / f"177/{sonitude.speaker_preset('movie')}"
    assert {music.label, movie.label, voice.label} == {music.label}
    # Each mode's own high-pass: 80 Hz, 120 Hz, off.
    for layer, corner in ((music, 80.0), (movie, 120.0)):
        n = _below(corner)
        assert np.allclose(layer.curve_left_db[:n], _butterworth4_db(
            corner, np.array(layer.curve_hz[:n])), atol=1e-4)
    assert np.allclose(voice.curve_left_db, _ramp(), atol=1e-5)
    assert [_high_pass_stage(m).summary for m in layers.values()] == [
        "corner at 80 Hz", "corner at 120 Hz",
        "off in this preset, so the curve's own low end is kept"]
    # And its own curve above it.
    assert np.allclose(movie.curve_left_db[92:],
                       np.minimum(_curve(step_at=90), 0)[92:], atol=1e-5)
    assert not np.allclose(movie.curve_left_db, music.curve_left_db)


def test_a_preset_without_the_other_modes_holds_music_alone(tmp_path):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    assert list(sonitude.load_profiles(a, 177, "INTELAUDIO", "10EC0285",
                                       "103C860F")) == ["music"]


def test_another_codec_or_subsystem_binds_nothing(tmp_path):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    assert sonitude.find(a, "10EC0287", "103C860F", True) is None
    assert sonitude.find(a, "10EC0285", "103C8600", True) is None


@pytest.mark.parametrize("sof,bus", [(True, "INTELAUDIO"),
                                     (False, "HDAUDIO")])
def test_the_bus_the_machine_runs_on_picks_between_archives(tmp_path, sof,
                                                            bus):
    _archive(tmp_path / _ARCHIVE, {"103C860F": 1}, {1: _preset_bin(_curve())})
    _archive(tmp_path / _HDA_ARCHIVE, {"103C860F": 2},
             {2: _preset_bin(_curve())})
    layer = sonitude.find(tmp_path, "10EC0285", "103C860F", sof)
    assert layer.hardware_id.startswith(bus)


def test_a_bound_preset_without_a_speaker_file_is_unsupported(tmp_path):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177}, {})
    with pytest.raises(apo_layer.UnsupportedApoConfig):
        sonitude.find(a, "10EC0285", "103C860F", True)


# --- hostile archives ---------------------------------------------------------
# The download can come from a mirror, so each of these is a shape an attacker
# controls (security review, 2026-10-18).

def test_a_preset_of_repeated_headers_reads_two_curves_and_stops():
    data = struct.pack("<IH", 1062, 265) * 200_000  # 1.2 MB of candidates
    curves = sonitude.read_curves(data)
    assert len(curves) <= 2


def _deflated(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def test_an_ssidmap_claiming_megabytes_is_refused_unread(tmp_path,
                                                         monkeypatch):
    a = _deflated(tmp_path / _ARCHIVE,
                  {"SSIDMAP.bin": b"\0" * (sonitude.MAX_MEMBER_BYTES + 1)})
    reads = []
    monkeypatch.setattr(zipfile.ZipFile, "read",
                        lambda self, name, pwd=None: reads.append(name))
    assert sonitude.find(a, "10EC0285", "103C860F", True) is None
    assert reads == []


def test_a_preset_claiming_megabytes_is_unsupported(tmp_path):
    a = _deflated(tmp_path / _ARCHIVE, {
        "SSIDMAP.bin": _ssidmap({"103C860F": 1}),
        f"1/{sonitude.SPEAKER_PRESET}":
            b"\0" * (sonitude.MAX_MEMBER_BYTES + 1)})
    with pytest.raises(apo_layer.UnsupportedApoConfig):
        sonitude.find(a, "10EC0285", "103C860F", True)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 4000.0, -3e38])
def test_a_curve_no_tuning_uses_is_unsupported(tmp_path, bad):
    curve = _curve()
    curve[100] = bad
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1},
                 {1: _preset_bin(curve)})
    with pytest.raises(apo_layer.UnsupportedApoConfig):
        sonitude.find(a, "10EC0285", "103C860F", True)


def test_the_printed_codename_keeps_no_control_characters(tmp_path):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 7},
                 {7: _preset_bin(_curve())},
                 codename="\x1b[2J\x1b]52;c;ZXZpbA==\x07Venus\r\nx" + "y" * 99)
    label = sonitude.find(a, "10EC0285", "103C860F", True).label
    assert not any(ord(c) < 32 or ord(c) == 127 for c in label)
    assert "Venus" in label and len(label) < 120


def _corrupt(path: Path, member: str) -> None:
    """Flip the bytes of *member*'s deflated data in place."""
    with zipfile.ZipFile(path) as z:
        info = z.getinfo(member)
    data = bytearray(path.read_bytes())
    o = info.header_offset
    start = o + 30 + struct.unpack_from("<HH", data, o + 26)[0] \
        + struct.unpack_from("<HH", data, o + 26)[1]
    for i in range(start, start + min(info.compress_size, 64)):
        data[i] ^= 0xFF
    path.write_bytes(bytes(data))


def test_a_damaged_ssidmap_skips_the_archive(tmp_path):
    a = _deflated(tmp_path / _ARCHIVE, {"SSIDMAP.bin": _ssidmap(
        {f"103C{i:04X}": i for i in range(400)})})
    _corrupt(a, "SSIDMAP.bin")
    assert sonitude.find(a, "10EC0285", "103C0001", True) is None


def test_a_damaged_preset_is_unsupported_not_a_crash(tmp_path):
    member = f"1/{sonitude.SPEAKER_PRESET}"
    a = _deflated(tmp_path / _ARCHIVE, {
        "SSIDMAP.bin": _ssidmap({"103C860F": 1}),
        member: _preset_bin(_curve())})
    _corrupt(a, member)
    with pytest.raises(apo_layer.UnsupportedApoConfig):
        sonitude.find(a, "10EC0285", "103C860F", True)


def test_an_archive_name_with_a_trailing_newline_is_not_one():
    assert not sonitude.is_archive(Path(_ARCHIVE + "\n"))


def test_a_non_finite_filter_fails_the_correction_check():
    taps = np.zeros(fir.FIR_LENGTH)
    taps[0] = float("nan")
    bins = np.fft.rfftfreq(fir.FIR_LENGTH, 1 / fir.SAMPLE_RATE)
    assert emit._worst_shape_error(taps, [0.0], 0.0, [1000], bins,
                                   rows=False) == float("inf")


# --- the FIR fold ------------------------------------------------------------

def test_a_curve_fold_realises_a_smooth_curve():
    curve = (sonitude.curve_grid_hz(), tuple(_curve(step_at=None)))
    t = parse.flat_tuning(sonitude.probe_freqs())
    flat = [0.0] * len(t.freqs)
    taps, _ = fir.make_fir(t.freqs, flat, eq_curve=curve)
    f = np.geomspace(100, 16000, 60)
    got = 20 * np.log10(np.abs([np.dot(taps, np.exp(
        -2j * np.pi * x * np.arange(len(taps)) / fir.SAMPLE_RATE)) for x in f]))
    bins = np.fft.rfftfreq(fir.FIR_LENGTH, 1 / fir.SAMPLE_RATE)
    want = (fir.design_target_db(t.freqs, flat, f, (), curve)
            - fir.design_target_db(t.freqs, flat, bins, (), curve).max())
    assert np.max(np.abs(got - want)) < emit.FIR_VERIFY_OK_DB


def test_a_curve_fold_is_floored_like_a_biquad_fold():
    deep = (sonitude.curve_grid_hz(), tuple([-80.0] * 40 + [0.0] * 225))
    bins = np.fft.rfftfreq(fir.FIR_LENGTH, 1 / fir.SAMPLE_RATE)
    target = fir.design_target_db([1000], [0.0], bins, (), deep)
    assert target.min() >= target.max() - fir.FIR_TARGET_RANGE_DB - 0.01


def test_no_curve_leaves_the_design_bit_identical():
    freqs, gains = [100, 1000, 10000], [3.0, 0.0, -2.0]
    a, pa = fir.make_fir(freqs, gains)
    b, pb = fir.make_fir(freqs, gains, eq_curve=None)
    assert np.array_equal(a, b) and pa == pb


def test_the_check_grades_a_curve_from_where_its_points_clear_a_bin():
    """Below ~400 Hz a 1/24-octave curve's points sit closer than the FIR's
    11.7 Hz bins, so it states detail no 4096-tap filter separates."""
    grid = sonitude.curve_grid_hz()
    from_hz = emit._curve_resolved_from_hz((grid, tuple(_curve())))
    assert 400 < from_hz < 410
    assert emit._curve_resolved_from_hz(None) == 0.0


def test_the_curve_is_applied_as_cuts_only(tmp_path):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1},
                 {1: _preset_bin(_curve(step_at=None),
                                 high_pass=(None, None))})
    layer = sonitude.find(a, "10EC0285", "103C860F", True)
    assert max(layer.curve_left_db) == 0.0 and max(_curve(step_at=None)) > 5
    assert min(layer.curve_left_db) == pytest.approx(
        min(_curve(step_at=None)), abs=1e-4)


def _butterworth4_db(fc: float, f: np.ndarray) -> np.ndarray:
    return -10 * np.log10(1 + (fc / f) ** 8)


def test_below_the_break_is_a_butterworth_4_high_pass_at_it(tmp_path):
    """_curve() steps up by ~11 dB after its point 72, 80 Hz: below that, the
    stored segment gives way to a 4th-order Butterworth cornered there."""
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1},
                 {1: _preset_bin(_curve())})
    layer = sonitude.find(a, "10EC0285", "103C860F", True)
    hz = np.array(layer.curve_hz[:73])
    assert hz[-1] == pytest.approx(80.0)
    assert np.allclose(layer.curve_left_db[:73], _butterworth4_db(80.0, hz),
                       atol=1e-4)
    assert layer.curve_left_db[72] == pytest.approx(-3.01, abs=0.01)


def _ramp() -> list[float]:
    """A smooth roll-off: 0.5 dB per point up to 0 dB, no step anywhere."""
    return [min(0.0, -0.5 * (100 - k)) for k in range(sonitude.CURVE_POINTS)]


def _below(corner: float) -> int:
    """How many curve points sit at or below *corner* Hz."""
    return int(np.sum(np.array(sonitude.curve_grid_hz()) <= corner + 1e-6))


def test_a_curve_without_a_step_is_high_passed_below_the_corner(tmp_path):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1},
                 {1: _preset_bin(_ramp())})
    layer = sonitude.find(a, "10EC0285", "103C860F", True)
    n = _below(80.0)
    hz = np.array(layer.curve_hz[:n])
    assert np.allclose(layer.curve_left_db[:n], _butterworth4_db(80.0, hz),
                       atol=1e-4)
    assert np.allclose(layer.curve_left_db[n:], _ramp()[n:], atol=1e-5)


@pytest.mark.parametrize("bottom,step,replaced", [
    (73, 6.1, 74),   # a step one grid point above 80 Hz: its bottom goes too
    (73, 5.9, 73),   # not a step: the point above the corner stands
    (75, 6.1, 73),   # a step further up: the stored segment above stands
])
def test_a_step_just_above_the_corner_leaves_no_notch(bottom, step, replaced):
    curve = ([-20.0] * (bottom + 1)
             + [-20.0 + step] * (sonitude.CURVE_POINTS - bottom - 1))
    out = sonitude._apply_high_pass(curve, 80.0)
    assert _below(80.0) == 73
    hz = sonitude.curve_grid_hz()
    assert np.allclose(out[:replaced], _butterworth4_db(80.0, np.array(
        hz[:replaced])), atol=1e-9)
    assert out[replaced:] == tuple(curve[replaced:])


def test_a_rise_in_the_replaced_segment_hides_no_step_above_the_corner():
    """A step of more than BREAK_STEP_DB far below the corner is replaced
    anyway; the step one grid point above it still takes its bottom point."""
    curve = ([-40.0] * 11 + [-20.0] * 63
             + [-13.9] * (sonitude.CURVE_POINTS - 74))
    out = sonitude._apply_high_pass(curve, 80.0)
    hz = sonitude.curve_grid_hz()
    assert np.allclose(out[:74], _butterworth4_db(80.0, np.array(hz[:74])),
                       atol=1e-9)
    assert out[74:] == tuple(curve[74:])


def test_each_channel_takes_its_own_high_pass(tmp_path):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1},
                 {1: _preset_bin(_curve(), _ramp(), high_pass=(80.0, None))})
    layer = sonitude.find(a, "10EC0285", "103C860F", True)
    assert layer.curve_left_db[0] < -70
    assert np.allclose(layer.curve_right_db, _ramp(), atol=1e-5)
    assert _high_pass_stage(layer).summary == "corner at 80 Hz left, off right"


def _high_pass_stage(layer):
    return next(s for s in layer.stages if s.name == "high-pass")


def test_the_stages_say_what_is_built_and_what_is_left_out(tmp_path):
    """Built from the file: the curve and the high-pass. Everything else is
    named as left out, for a listener comparing with Windows."""
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1}, {1: _preset_bin(_curve())})
    layer = sonitude.find(a, "10EC0285", "103C860F", True)
    built, left_out = apo_layer.BUILT, apo_layer.NOT_REPRODUCED
    assert [(s.name, s.status) for s in layer.stages] == [
        ("EQ curve", built), ("high-pass", built),
        ("compressor and limiter", left_out),
        ("psychoacoustic bass", left_out), ("10-band equaliser", left_out),
        ("outside this tuning", left_out)]
    assert layer.stages[0].builds == "eq"
    assert all(s.status in apo_layer.STATUSES for s in layer.stages)


# --- the run -----------------------------------------------------------------

def _this_machine(fake_host, sof=True, subsys="103c860f"):
    """This machine as a device with codec 10EC0285 and *subsys*, on SOF or
    not, as `lib/apo/sonitude.py` `resolve` reads it."""
    fake_host("/proc/asound/card0/codec#0",
              "Codec: Realtek ALC285\nVendor Id: 0x10ec0285\n"
              f"Subsystem Id: 0x{subsys}\n")
    fake_host("/proc/asound/cards",
              " 0 [sofhdadsp      ]: sof-hda-dsp - sof-hda-dsp\n" if sof
              else " 0 [PCH            ]: HDA-Intel - HDA Intel PCH\n")


def _run(tmp_path, capsys, source, *flags):
    out_dir, irs_dir = tmp_path / "out", tmp_path / "irs"
    dolby_to_easyeffects.main([
        str(source), "--skip-ee-check", "--output-dir", str(out_dir),
        "--irs-dir", str(irs_dir), *flags])
    return " ".join(capsys.readouterr().out.split()), out_dir


def test_a_vendor_only_run_builds_one_preset_from_the_curve(
        tmp_path, fake_host, silence_console, capsys):
    silence_console(console)
    _this_machine(fake_host)
    a = _archive(tmp_path / "dl" / _ARCHIVE, {"103C860F": 177},
                 {177: _three_modes()})
    out, out_dir = _run(tmp_path, capsys, a)
    presets = sorted(out_dir.glob("*.json"))
    assert [p.name for p in presets] == ["Sonitude-Music.json"]
    order = json.loads(presets[0].read_text())["output"]["plugins_order"]
    assert order == ["convolver#0", "limiter#0"]
    assert ("Profile: music (the first-listed of this speaker's 3 sound "
            "modes — --list names them; --profile picks)") in out
    assert "No Dolby file was given or found" in out
    assert "It voices the 'music' sound mode only" in out
    assert "experimental: nobody has listened" not in out
    assert "Correction check passed from 403 Hz up" in out
    assert "[vendor-tuning-unconfirmed]" in out
    # Nothing that refers to a Dolby file or offers a refused flag.
    assert "--disable vendor-apo" not in out
    assert "attach the XML" not in out
    # The closing asks for the download link, and for no file to attach.
    assert ("If you report, include a link to your device's audio-driver "
            "download, if you know it.") in out
    assert "attach" not in out
    assert "Dolby's default voicing" not in out


def _kernel_db(irs_dir: Path, stem: str) -> np.ndarray:
    """The left channel's response of preset *stem*'s impulse, at _PIN_HZ."""
    from scipy.io import wavfile
    sr, taps = wavfile.read(next(irs_dir.glob(f"{stem}-*.irs")))
    H = np.fft.rfft(taps[:, 0].astype(float), 1 << 16)
    f = np.fft.rfftfreq(1 << 16, 1 / sr)
    return np.interp(_PIN_HZ, f, 20 * np.log10(np.abs(H) + 1e-15))


def test_profile_movie_builds_the_movie_preset_from_its_own_curve(
        tmp_path, fake_host, silence_console, capsys,
        unheard_movie_and_voice):
    silence_console(console)
    _this_machine(fake_host)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177}, {177: _three_modes()})
    out, out_dir = _run(tmp_path, capsys, a, "--profile", "movie")
    assert [p.name for p in out_dir.glob("*.json")] == ["Sonitude-Movie.json"]
    assert "Profile: movie " in out
    assert "/177/0/EFX/AnyOutput/Movie/Preset.bin), applied" in out
    assert "Nobody has listened to this sound mode yet." in out
    assert ("Sonitude-Movie is experimental: nobody has listened to it yet. "
            "How loud each plays next to the others may differ from Windows: "
            "each preset is scaled to its own peak.") in out
    # Its own high-pass, 120 Hz: a 4th-order cut there, not music's 80 Hz.
    movie = _kernel_db(tmp_path / "irs", "Sonitude-Movie")
    out, _ = _run(tmp_path, capsys, a)
    music = _kernel_db(tmp_path / "irs", "Sonitude-Music")
    at_80 = int(np.argmin(np.abs(_PIN_HZ - 80)))
    assert movie[at_80] < music[at_80] - 6


def test_all_profiles_builds_one_preset_per_mode(tmp_path, fake_host,
                                                 silence_console, capsys,
        unheard_movie_and_voice):
    silence_console(console)
    _this_machine(fake_host)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177}, {177: _three_modes()})
    out, out_dir = _run(tmp_path, capsys, a, "--all-profiles")
    assert sorted(p.name for p in out_dir.glob("*.json")) == [
        "Sonitude-Movie.json", "Sonitude-Music.json", "Sonitude-Voice.json"]
    assert len(list((tmp_path / "irs").glob("*.irs"))) == 3
    assert ("Generating presets for all 3 profiles: music, movie, voice"
            in out)
    # The device's section once; each mode's file and tone each time.
    assert out.count("No Dolby file was given or found") == 1
    assert out.count("Nothing to do. Left out of the preset") == 1
    # Each mode's built stages, and what all of them leave out once.
    assert out.count("What the tuning does, stage by stage:") == 3
    assert out.count("not reproduced compressor and limiter") == 1
    assert out.count("not reproduced the same stages as above") == 2
    assert out.count("Nobody has listened to this sound mode yet.") == 2
    assert "Sonitude-Music, Sonitude-Movie, Sonitude-Voice" in out
    assert ("Sonitude-Movie and Sonitude-Voice are experimental: nobody has "
            "listened to them yet.") in out
    # Each kernel is its own mode's: three different filters.
    kernels = [_kernel_db(tmp_path / "irs", f"Sonitude-{m}")
               for m in ("Music", "Movie", "Voice")]
    assert not np.allclose(kernels[0], kernels[1], atol=0.5)
    assert not np.allclose(kernels[0], kernels[2], atol=0.5)


def test_list_names_the_modes_as_profiles(tmp_path, fake_host,
                                          silence_console, capsys,
        unheard_movie_and_voice):
    silence_console(console)
    _this_machine(fake_host)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177}, {177: _three_modes()})
    out, out_dir = _run(tmp_path, capsys, a, "--list")
    assert out.endswith(
        f"Endpoints and profiles in {a}: endpoint: internal_speaker "
        "(operating_mode=normal) profile: music profile: movie "
        "(experimental) profile: voice (experimental)")
    assert not out_dir.exists()


def test_every_sonitude_mode_has_been_heard(tmp_path, fake_host,
                                            silence_console, capsys):
    """One listener compared Music, Movie and Voice with Windows (#140), so
    no preset, listing or closing marks one unheard."""
    silence_console(console)
    _this_machine(fake_host)
    assert sonitude.FORMAT.experimental == ()
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177}, {177: _three_modes()})
    out, _ = _run(tmp_path, capsys, a, "--all-profiles")
    assert "Nobody has listened" not in out
    assert "nobody has listened" not in out
    out, _ = _run(tmp_path, capsys, a, "--list")
    assert out.endswith("profile: music profile: movie profile: voice")


def test_an_unknown_profile_names_the_three(tmp_path, fake_host,
                                            silence_console):
    silence_console(console)
    _this_machine(fake_host)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177}, {177: _three_modes()})
    with pytest.raises(ValueError, match="Profile 'game' not found. "
                       "Available: music, movie, voice"):
        dolby_to_easyeffects.main([str(a), "--dry-run", "--skip-ee-check",
                                   "--profile", "game"])


# A placeholder preset: flat, with its high-pass off.
_FLAT = _preset_bin([0.0] * sonitude.CURVE_POINTS, high_pass=(None, None))


def _flat_music() -> dict[str, bytes]:
    """`_three_modes` with a Music mode that changes nothing."""
    return {**_three_modes(), "music": _FLAT}


def test_a_flat_mode_without_a_high_pass_changes_nothing(tmp_path):
    """Flat within `FLAT_DB` and no high-pass: the build would leave the
    sound as it is. A high-pass alone, or a cut only just past the
    tolerance, still changes it."""
    flat = [0.0] * sonitude.CURVE_POINTS
    near = [-sonitude.FLAT_DB / 2] * sonitude.CURVE_POINTS
    dip = [0.0] * 100 + [-2 * sonitude.FLAT_DB] + [0.0] * 164
    boosts = [3.0] * sonitude.CURVE_POINTS  # cuts only: built flat
    cases = {1: (_FLAT, True),
             2: (_preset_bin(near, high_pass=(None, None)), True),
             3: (_preset_bin(boosts, high_pass=(None, None)), True),
             4: (_preset_bin(flat, high_pass=(80.0, 80.0)), False),
             5: (_preset_bin(dip, high_pass=(None, None)), False),
             6: (_preset_bin(flat, high_pass=(None, 80.0)), False),
             7: (_preset_bin(_curve()), False)}
    a = _archive(tmp_path / _ARCHIVE, {f"103C86{n:02X}": n for n in cases},
                 {n: blob for n, (blob, _) in cases.items()})
    for n, (_, expected) in cases.items():
        layer = sonitude.find(a, "10EC0285", f"103C86{n:02X}", True)
        assert layer.changes_nothing is expected, n


def test_a_mode_that_changes_nothing_is_not_built_by_default(
        tmp_path, fake_host, silence_console, capsys):
    """The first mode that changes the sound is the default, and the run
    says why the first-listed one is missing, without calling the one it
    built the Windows default."""
    silence_console(console)
    _this_machine(fake_host)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 75}, {75: _flat_music()})
    out, out_dir = _run(tmp_path, capsys, a)
    assert [p.name for p in out_dir.glob("*.json")] == ["Sonitude-Movie.json"]
    assert out.count("The tuning's Music mode changes nothing, so no preset "
                     "is built for it.") == 1
    assert ("Profile: movie (the first-listed of the 2 sound modes that "
            "change this speaker's sound — --list names them; --profile "
            "picks)") in out
    assert ("It voices the 'movie' sound mode only (the first one that "
            "changes the sound) — --all-profiles builds every mode that "
            "changes the sound.") in out
    assert "Windows default" not in out


def test_all_profiles_builds_only_the_modes_that_change_the_sound(
        tmp_path, fake_host, silence_console, capsys):
    silence_console(console)
    _this_machine(fake_host)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 75}, {75: _flat_music()})
    out, out_dir = _run(tmp_path, capsys, a, "--all-profiles")
    assert sorted(p.name for p in out_dir.glob("*.json")) == [
        "Sonitude-Movie.json", "Sonitude-Voice.json"]
    assert "Generating presets for all 2 profiles: movie, voice" in out
    assert out.count("The tuning's Music mode changes nothing") == 1


def test_the_only_mode_that_changes_the_sound_is_named_so(
        tmp_path, fake_host, silence_console, capsys):
    silence_console(console)
    _this_machine(fake_host)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 75},
                 {75: {**_flat_music(), "voice": _FLAT}})
    out, out_dir = _run(tmp_path, capsys, a, "--dry-run")
    assert ("The tuning's Music and Voice modes change nothing, so no "
            "preset is built for them.") in out
    assert ("Profile: movie (the only one of this speaker's 3 sound modes "
            "that changes the sound)") in out
    # Nothing else to build, so no pointer to --all-profiles.
    assert "--all-profiles builds" not in out


def test_a_profile_that_changes_nothing_is_refused_with_the_reason(
        tmp_path, fake_host, silence_console):
    silence_console(console)
    _this_machine(fake_host)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 75}, {75: _flat_music()})
    with pytest.raises(ValueError, match="Profile 'music' changes nothing in "
                       "this tuning, so there is no preset to build for it. "
                       "Profiles that change the sound: movie, voice") as e:
        dolby_to_easyeffects.main([str(a), "--dry-run", "--skip-ee-check",
                                   "--profile", "music"])
    # The sentence ends on what to do, so no --help pointer follows it.
    assert e.value.next_step == ""


def test_list_marks_a_mode_that_changes_nothing(tmp_path, fake_host,
                                                silence_console, capsys,
        unheard_movie_and_voice):
    silence_console(console)
    _this_machine(fake_host)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 75}, {75: _flat_music()})
    out, _ = _run(tmp_path, capsys, a, "--list")
    assert out.endswith(
        "profile: music (changes nothing in this tuning) profile: movie "
        "(experimental) profile: voice (experimental)")


def test_a_tuning_that_changes_nothing_anywhere_is_a_clean_error(
        tmp_path, fake_host, capsys):
    _this_machine(fake_host)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 75},
                 {75: {p: _FLAT for p in sonitude.PROFILES}})
    for flags in ([], ["--all-profiles"], ["--profile", "movie"]):
        rc = dolby_to_easyeffects.run_cli(
            [str(a), "--dry-run", "--skip-ee-check", "--no-color", *flags])
        out = " ".join(capsys.readouterr().out.split())
        assert rc == 1
        assert out.endswith(
            "Error: This tuning changes nothing in any of its sound modes "
            "(music, movie, voice), so there is no preset to build."), flags


def test_the_wrapper_skips_a_mode_that_changes_nothing(tmp_path, fake_host):
    """The wrapper converts what the generator built, so the flat mode never
    reaches it."""
    _this_machine(fake_host)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 75}, {75: _flat_music()})
    result = _wrapper(tmp_path, "--all-profiles", "--target-object",
                      "alsa_output.speakers", source=a)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in (tmp_path / "confs").iterdir()) == sorted(
        f"Sonitude_{m}.{ext}" for m in ("Movie", "Voice")
        for ext in ("conf", "irs"))
    assert "The tuning's Music mode changes nothing" in result.stdout


def test_a_vendor_only_run_reads_this_machines_codec(tmp_path, fake_host,
                                                    silence_console, capsys):
    silence_console(console)
    fake_host("/proc/asound/card0/codec#0",
              "Codec: Realtek ALC285\nVendor Id: 0x10ec0285\n"
              "Subsystem Id: 0x103c860f\n")
    fake_host("/proc/asound/cards",
              " 0 [sofhdadsp      ]: sof-hda-dsp - sof-hda-dsp\n")
    a = _archive(tmp_path / "dl" / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    out, out_dir = _run(tmp_path, capsys, a, "--dry-run")
    assert "Found Sonitude speaker tuning (preset 177, X_Synth)" in out


def test_no_preset_for_this_machine_says_which_ids_it_tried(
        tmp_path, fake_host, silence_console):
    silence_console(console)
    _this_machine(fake_host, subsys="103c8600")
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    with pytest.raises(RuntimeError, match="10EC0285:103C8600"):
        dolby_to_easyeffects.main([str(a), "--dry-run", "--skip-ee-check"])


def test_disabling_the_only_tuning_is_refused(tmp_path, capsys):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    with pytest.raises(SystemExit):
        dolby_to_easyeffects.main([str(a), "--disable", "vendor-apo"])
    assert ("the Sonitude tuning is this run's only one"
            in capsys.readouterr().err)


def test_a_download_holding_only_the_archives_unpacks_them(tmp_path):
    inner = _archive(tmp_path / "src" / "SR" / "SNAPO64Ext" / _ARCHIVE,
                     {"103C860F": 177}, {177: _preset_bin(_curve())})
    download = tmp_path / "sp000000.zip"
    with zipfile.ZipFile(download, "w") as z:
        z.write(inner, f"src/SR/SNAPO64Ext/{_ARCHIVE}")
        z.writestr("src/Realtek/RTKVHD64.sys", b"\0" * 1024)
    tree = dp.unpack(download, cache_root=tmp_path / "cache")
    kept = sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*")
                  if p.is_file())
    assert kept == [dp.PROVENANCE, f"src/SR/SNAPO64Ext/{_ARCHIVE}"]
    assert sonitude.find(tree, "10EC0285", "103C860F", True) is not None


def _download(tmp_path, extra: dict[str, bytes] | None = None) -> Path:
    inner = _archive(tmp_path / "src" / _ARCHIVE, {"103C860F": 177},
                     {177: _preset_bin(_curve())})
    download = tmp_path / "sp000000.zip"
    with zipfile.ZipFile(download, "w") as z:
        z.write(inner, f"src/SR/SNAPO64Ext/{_ARCHIVE}")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return download


def test_a_download_of_only_the_archives_builds_the_vendor_preset(
        tmp_path, fake_host, silence_console, capsys):
    silence_console(console)
    _this_machine(fake_host)
    out, out_dir = _run(tmp_path, capsys, _download(tmp_path))
    assert "Found Sonitude speaker tuning (preset 177, X_Synth)" in out
    assert [p.name for p in out_dir.glob("*.json")] == ["Sonitude-Music.json"]
    assert "include a link to your device's audio-driver download" in out


def test_a_download_with_a_dolby_file_stays_on_the_dolby_path(
        tmp_path, silence_console, capsys, monkeypatch):
    silence_console(console)
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_0285_SUBSYS_103C860F.xml")
    download = _download(tmp_path, {
        "src/Dolby/DEV_0285_SUBSYS_103C860F.xml": xml.read_bytes()})
    picked = []

    def pick(tree, path, best_guess=False):
        picked.append(tree)
        return next(tree.rglob("DEV_0285_SUBSYS_103C860F.xml"))

    monkeypatch.setattr(dp, "pick_xml", pick)
    out, _ = _run(tmp_path, capsys, download, "--dry-run")
    assert picked and "Sonitude speaker tuning" not in out


def _with_another_devices_dolby_file(tmp_path) -> Path:
    """A download holding the archive and a Dolby XML for another codec and
    subsystem."""
    other = "DEV_0236_SUBSYS_17AA0000.xml"
    xml = write_synthetic_tuning_xml(tmp_path / other)
    return _download(tmp_path, {f"src/Dolby/{other}": xml.read_bytes()})


def test_a_download_whose_dolby_file_is_another_devices_builds_the_archive(
        tmp_path, fake_host, silence_console, capsys):
    silence_console(console)
    _this_machine(fake_host)
    out, _ = _run(tmp_path, capsys,
                  _with_another_devices_dolby_file(tmp_path), "--dry-run")
    assert "Found Sonitude speaker tuning (preset 177, X_Synth)" in out


def test_a_download_matching_neither_tuning_says_so_as_the_archive_does(
        tmp_path, fake_host, silence_console):
    silence_console(console)
    _this_machine(fake_host, subsys="103c8600")
    with pytest.raises(RuntimeError, match="holds Sonitude tunings, but none "
                       "for this device's audio hardware"):
        dolby_to_easyeffects.main([
            str(_with_another_devices_dolby_file(tmp_path)), "--dry-run",
            "--skip-ee-check"])


def test_a_download_with_dolby_files_it_cant_choose_between_still_says_so(
        tmp_path, fake_host, silence_console, monkeypatch):
    """Only "none is this device's" goes on to the archive: a choice the
    user has to make stays theirs."""
    silence_console(console)
    _this_machine(fake_host)

    def pick(tree, path, best_guess=False):
        raise dp.PackageError("holds matching tunings in 2 folders")

    monkeypatch.setattr(dp, "pick_xml", pick)
    with pytest.raises(dp.PackageError, match="in 2 folders"):
        dolby_to_easyeffects.main([
            str(_with_another_devices_dolby_file(tmp_path)), "--dry-run",
            "--skip-ee-check"])


# --- choosing the preset -----------------------------------------------------

def _bundle(tmp_path, *archives: Path) -> Path:
    """A driver download holding *archives*."""
    download = tmp_path / "sp000001.zip"
    with zipfile.ZipFile(download, "w") as z:
        for a in archives:
            z.write(a, f"src/SR/SNAPO64Ext/{a.name}")
    return download


def _two_buses(tmp_path, same=False):
    """A download whose two archives both bind 103C860F, to presets whose
    curves differ unless *same*."""
    return _bundle(
        tmp_path,
        _archive(tmp_path / "src" / _ARCHIVE, {"103C860F": 1},
                 {1: _preset_bin(_curve())}),
        _archive(tmp_path / "src" / _HDA_ARCHIVE, {"103C860F": 2},
                 {2: _preset_bin(_curve() if same
                                 else _curve(step_at=90))}))


@pytest.mark.parametrize("bus,preset", [("INTELAUDIO", 1), ("HDAUDIO", 2)])
def test_an_archive_alone_is_the_only_one_read(tmp_path, fake_host,
                                               silence_console, capsys, bus,
                                               preset):
    """The two-tunings warning's remedy: either archive passed alone builds
    its own preset, whichever bus this machine runs."""
    silence_console(console)
    _this_machine(fake_host, True)
    _two_buses(tmp_path)
    archive = tmp_path / "src" / (_ARCHIVE if bus == "INTELAUDIO"
                                  else _HDA_ARCHIVE)
    out, _ = _run(tmp_path, capsys, archive, "--dry-run")
    assert f"(preset {preset}," in out
    assert "two different tunings" not in out


@pytest.mark.parametrize("sof,preset,other", [(True, 1, "HDAUDIO"),
                                              (False, 2, "INTELAUDIO")])
def test_two_tunings_for_this_device_say_which_was_used_and_why(
        tmp_path, fake_host, silence_console, capsys, sof, preset, other):
    silence_console(console)
    _this_machine(fake_host, sof)
    out, _ = _run(tmp_path, capsys, _two_buses(tmp_path), "--dry-run")
    assert f"(preset {preset}," in out
    assert "two different tunings for this device" in out
    # The other archive, as unpacked from the download, to pass alone.
    assert (f"starts with {other}, rebuild with that archive alone as the "
            "input: ") in out
    assert f"SEAPODAT.{other}.FUNC_01-VEN_10EC-DEV_0285.zip" in out
    assert "--hw-id" not in out


def test_one_tuning_under_two_buses_is_not_a_choice(
        tmp_path, fake_host, silence_console, capsys):
    silence_console(console)
    _this_machine(fake_host, True)
    out, _ = _run(tmp_path, capsys, _two_buses(tmp_path, same=True),
                  "--dry-run")
    assert "(preset 1," in out
    assert "two different tunings" not in out


def test_curves_apart_by_rounding_are_one_tuning(
        tmp_path, fake_host, silence_console, capsys):
    """Most two-bus pairs that differ at all differ by float rounding."""
    silence_console(console)
    _this_machine(fake_host, True)
    _bundle(tmp_path,
            _archive(tmp_path / "src" / _ARCHIVE, {"103C860F": 1},
                     {1: _preset_bin(_curve())}),
            _archive(tmp_path / "src" / _HDA_ARCHIVE, {"103C860F": 2},
                     {2: _preset_bin([v + 0.001 for v in _curve()])}))
    out, _ = _run(tmp_path, capsys, tmp_path / "sp000001.zip", "--dry-run")
    assert "(preset 1," in out
    assert "two different tunings" not in out


# --- what a failed lookup says -----------------------------------------------

def test_a_missing_archive_is_said_as_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        dolby_to_easyeffects.main([str(tmp_path / _ARCHIVE), "--dry-run",
                                   "--skip-ee-check"])


def test_a_damaged_archive_is_said_as_unreadable_not_as_another_model(
        tmp_path, fake_host, silence_console):
    silence_console(console)
    _this_machine(fake_host)
    (tmp_path / _ARCHIVE).write_bytes(b"PK\x03\x04 not a zip")
    with pytest.raises(RuntimeError, match="can't be read") as exc:
        dolby_to_easyeffects.main([str(tmp_path / _ARCHIVE), "--dry-run",
                                   "--skip-ee-check"])
    assert "another model" not in str(exc.value)


def test_unreadable_codec_ids_say_what_that_means_on_the_device(
        tmp_path, silence_console):
    """A SoundWire machine reads no HDA codec on the device itself, so the
    error can't tell its reader to go and run it there."""
    silence_console(console)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    with pytest.raises(RuntimeError, match="picked by an HDA audio codec's "
                       "IDs, and none can be read here. On the device the "
                       "tuning is for, that means its audio isn't on an HDA "
                       "codec") as exc:
        dolby_to_easyeffects.main([str(a), "--dry-run", "--skip-ee-check"])
    assert "Run this on the device" not in str(exc.value)
    assert "--hw-id" not in str(exc.value)
    assert "--speaker-info" not in str(exc.value)


def test_a_miss_on_this_device_points_at_the_download(
        tmp_path, fake_host, silence_console):
    silence_console(console)
    _this_machine(fake_host, True)
    a = _archive(tmp_path / _ARCHIVE, {"103C8600": 177},
                 {177: _preset_bin(_curve())})
    with pytest.raises(RuntimeError, match="10EC0285:103C860F.*download"):
        dolby_to_easyeffects.main([str(a), "--dry-run", "--skip-ee-check"])


def test_a_miss_in_one_archive_points_at_the_whole_download(
        tmp_path, fake_host, silence_console):
    """One archive holds one bus, so the right download's other archive may
    be the one that binds: not "another model's download"."""
    silence_console(console)
    _this_machine(fake_host, True)
    a = _archive(tmp_path / _ARCHIVE, {"103C8600": 177},
                 {177: _preset_bin(_curve())})
    with pytest.raises(RuntimeError) as exc:
        dolby_to_easyeffects.main([str(a), "--dry-run", "--skip-ee-check"])
    assert "maps no preset to this device's audio hardware" in str(exc.value)
    assert "pass the whole download instead" in str(exc.value)
    assert "another model" not in str(exc.value)


# --- flags with nothing to act on --------------------------------------------

@pytest.mark.parametrize("flags", [
    ("--enable", "autogain"), ("--enable", "dialog"),
    ("--enable", "virtual-bass"),
    ("--mode", "headphone"), ("--endpoint", "headphone"), ("--best-guess",),
    ("--volmax-slot", "output-gain"), ("--disable", "mbc")])
def test_flags_for_a_dolby_stage_are_refused(tmp_path, flags, capsys):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    with pytest.raises(SystemExit):
        dolby_to_easyeffects.main([str(a), *flags])
    assert "and this tuning has none" in capsys.readouterr().err


def test_level_restore_is_refused_with_its_own_reason(tmp_path, capsys):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    with pytest.raises(SystemExit):
        dolby_to_easyeffects.main([str(a), "--enable", "level-restore"])
    assert ("--enable level-restore gives back level the filter's peak "
            "scaling removed, and this tuning's filter loses next to none"
            ) in capsys.readouterr().err


def test_level_restore_is_not_offered_on_a_curve_that_only_cuts(
        tmp_path, fake_host, silence_console, capsys):
    silence_console(console)
    _this_machine(fake_host)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    out, _ = _run(tmp_path, capsys, a)
    assert "level-restore" not in out


@pytest.mark.parametrize("prefix,stem", [(None, "Sonitude"), ("Dolby", "Dolby"),
                                         ("Mine", "Mine")])
def test_a_typed_prefix_stands(tmp_path, fake_host, silence_console, capsys,
                               prefix, stem):
    silence_console(console)
    _this_machine(fake_host)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    _, out_dir = _run(tmp_path, capsys, a,
                      *(["--prefix", prefix] if prefix else []))
    assert [p.name for p in out_dir.glob("*.json")] == [
        f"{stem}-Music.json"]


def test_a_vendor_only_run_hands_a_wrapper_its_archive(tmp_path, fake_host,
                                                       silence_console):
    silence_console(console)
    _this_machine(fake_host)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    resolved = {}
    dolby_to_easyeffects.main([str(a), "--dry-run", "--skip-ee-check"],
                              resolved=resolved)
    assert resolved == {"tuning_file": a, "voicings": False,
                        "presets": ["Sonitude-Music"], "experimental": []}


# --- an installed Windows ---------------------------------------------------

_STORE = Path("Windows/System32/DriverStore/FileRepository")


def _package(root: Path, name: str = "snapo64ext.inf_amd64_0123abcd",
             driver_ver: str | None = "07/08/2025,17.29.53.8400",
             step_at: int = 72) -> Path:
    """A SNAPO64Ext DriverStore folder under *root*'s Windows, holding the
    archive and, unless *driver_ver* is None, the INF that dates it."""
    folder = root / _STORE / name
    _archive(folder / _ARCHIVE, {"103C860F": 177},
             {177: _preset_bin(_curve(step_at=step_at))})
    if driver_ver is not None:
        (folder / "SNAPO64Ext.inf").write_text(
            f"[Version]\nSignature=\"$Windows NT$\"\nDriverVer={driver_ver}\n")
    return folder


def _windows(tmp_path) -> str:
    """--windows on `_package`'s install, as one argument for `_run`."""
    return f"--windows={tmp_path / 'c' / 'Windows'}"


def test_a_windows_install_with_only_the_archives_builds_from_them(
        tmp_path, fake_host, silence_console, capsys):
    silence_console(console)
    _this_machine(fake_host)
    folder = _package(tmp_path / "c")
    out, out_dir = _run(tmp_path, capsys, _windows(tmp_path))
    assert f"Auto-detected: {folder}" in out
    assert "Found Sonitude speaker tuning (preset 177, X_Synth)" in out
    assert [p.name for p in out_dir.glob("*.json")] == ["Sonitude-Music.json"]
    # The archive the run read, in the DriverStore, is the file to attach.
    assert f"{folder / _ARCHIVE}/177/" in out


def test_a_matching_dolby_file_wins_and_the_other_tuning_is_named(
        tmp_path, fake_host, silence_console, capsys):
    silence_console(console)
    _this_machine(fake_host)
    folder = _package(tmp_path / "c")
    dax = tmp_path / "c" / _STORE / "dax3_ext_rtk.inf_amd64_4567"
    dax.mkdir(parents=True)
    xml = write_synthetic_tuning_xml(dax / "DEV_0285_SUBSYS_103C860F.xml")
    out, out_dir = _run(tmp_path, capsys, _windows(tmp_path), "--dry-run")
    assert f"Auto-detected: {xml}" in out
    assert (f"A Sonitude tuning is there too ({folder}); using the Dolby "
            "file.") in out
    assert "Sonitude speaker tuning (preset" not in out
    assert "Dolby-Balanced" in out


def test_several_driver_versions_build_from_the_newest_and_say_so(
        tmp_path, fake_host, silence_console, capsys):
    """By DriverVer date, whatever the folder names' order."""
    silence_console(console)
    _this_machine(fake_host)
    _package(tmp_path / "c", "snapo64ext.inf_amd64_ffff",
             "01/02/2024,17.0.0.1", step_at=90)
    newest = _package(tmp_path / "c", "SNAPO64Ext.inf_amd64_0000",
                      "07/08/2025,17.29.53.8400")
    out, _ = _run(tmp_path, capsys, _windows(tmp_path), "--dry-run")
    assert (f"Auto-detected: {newest} (the newest of the 2 Sonitude driver "
            "versions there)") in out
    assert f"{newest / _ARCHIVE}/177/" in out


def test_driver_versions_are_ordered_by_date_then_version_then_mtime(
        tmp_path):
    store = tmp_path / _STORE
    older = _package(tmp_path, "snapo64ext.inf_amd64_a", "07/08/2025,17.1")
    newer = _package(tmp_path, "snapo64ext.inf_amd64_b", "07/08/2025,17.2")
    # No INF: its folder's date, here long before either stamp.
    undated = _package(tmp_path, "snapo64ext.inf_amd64_c", None)
    os.utime(undated, (0, 0))
    (store / "snapo64ext.inf_amd64_empty").mkdir()
    (store / "dax3_ext_rtk.inf_amd64_d").mkdir()
    assert sonitude.driver_store_packages(store) == [newer, older, undated]
    # A UTF-16 INF, as some packages ship.
    (older / "SNAPO64Ext.inf").write_bytes(
        "\ufeff[Version]\r\nDriverVer = 12/31/2025, 1.0\r\n"
        .encode("utf-16-le"))
    assert sonitude.driver_store_packages(store)[0] == older


def test_a_mounted_windows_with_only_the_archives_is_auto_detected(
        tmp_path, fake_host, silence_console, capsys, monkeypatch):
    silence_console(console)
    _this_machine(fake_host)
    folder = _package(tmp_path / "c")
    monkeypatch.setattr(dax_discover, "_ntfs_family_mountpoints",
                        lambda: [tmp_path / "c"])
    out, _ = _run(tmp_path, capsys, "--dry-run")
    assert f"Auto-detected Windows mount: {tmp_path / 'c'}" in out
    assert f"Auto-detected: {folder}" in out
    assert "Found Sonitude speaker tuning (preset 177, X_Synth)" in out


# --- the tuning source -------------------------------------------------------

def _args(argv):
    parser = dolby_to_easyeffects.build_parser(argv)
    return parser, parser.parse_args(argv)


def _source(argv):
    """The `TuningSource` main() resolves for *argv*."""
    return dolby_to_easyeffects._tuning_source(*reversed(_args(argv)))


def test_the_registry_knows_an_archive_by_name_and_a_folder_by_content(
        tmp_path):
    a = _archive(tmp_path / "dl" / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    (tmp_path / "xml").mkdir()
    xml = write_synthetic_tuning_xml(
        tmp_path / "xml" / "DEV_0285_SUBSYS_103C860F.xml")
    assert apo_discover.standalone_for(a) is sonitude.FORMAT
    # By name alone, so a missing one is said as a missing file.
    assert (apo_discover.standalone_for(tmp_path / "gone" / _ARCHIVE)
            is sonitude.FORMAT)
    assert apo_discover.standalone_for(tmp_path / "dl") is sonitude.FORMAT
    assert apo_discover.standalone_for(xml) is None
    assert apo_discover.standalone_for(xml.parent) is None
    assert apo_discover.stands_in_for_xml(f"src/SR/SNAPO64Ext/{_ARCHIVE}")
    assert not apo_discover.stands_in_for_xml(
        "src/Dolby/DEV_0285_SUBSYS_103C860F.xml")
    assert sonitude.FORMAT.wanted_members(
        [f"src/{_ARCHIVE}", "src/RTKVHD64.sys"]) == {f"src/{_ARCHIVE}"}


def test_a_dolby_xml_is_a_dolby_source(tmp_path):
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_0285_SUBSYS_103C860F.xml")
    src = _source([str(xml)])
    assert (src.format, src.has_voicings, src.xml_path, src.closing_file,
            src.prefix, src.layer, src.profiles, src.unoffered) == (
        None, True, xml, xml, "Dolby", None, {}, ())
    assert not src.experimental("music")
    # The Dolby file holds every profile, so none is a vendor layer's.
    assert src.layer_for("music") is None and src.layer_for(None) is None
    assert src.buildable == src.unbuildable == []


def test_an_archive_is_a_standalone_source(tmp_path, fake_host,
                                           silence_console,
        unheard_movie_and_voice):
    silence_console(console)
    _this_machine(fake_host)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _three_modes()})
    src = _source([str(a)])
    assert (src.has_voicings, src.xml_path, src.closing_file, src.prefix,
            src.origin, src.unoffered) == (
        False, None, a, "Sonitude", a,
        ("vendor-apo-active", "level-restore"))
    assert src.format is unheard_movie_and_voice
    assert list(src.profiles) == ["music", "movie", "voice"]
    assert src.layer is src.profiles["music"]
    assert src.layer_for("voice") is src.profiles["voice"]
    # A profile the tuning doesn't carry is no layer's; profiles_to_build
    # refuses it before any lookup.
    assert src.layer_for("game") is None
    assert src.layer.label == ("Sonitude speaker tuning (preset 177, "
                               "X_Synth)")
    assert [p for p in src.profiles if src.experimental(p)] == [
        "movie", "voice"]
    assert src.profiles_to_build(None, False) == ["music"]
    assert src.profiles_to_build("Voice", False) == ["voice"]
    assert src.profiles_to_build(None, True) == ["music", "movie", "voice"]


def test_a_download_of_only_archives_is_a_standalone_source(
        tmp_path, fake_host, silence_console):
    silence_console(console)
    _this_machine(fake_host)
    src = _source([str(_download(tmp_path)), "--prefix", "Mine"])
    assert src.format is sonitude.FORMAT and src.prefix == "Mine"
    assert src.origin.is_dir() and sonitude.archives_in(src.origin)
    assert src.layer is not None and src.xml_path is None
    # The archive in the unpacked download that holds this device's tuning.
    assert src.closing_file == src.layer.inf_path
    assert src.closing_file.name == _ARCHIVE
    assert src.closing_file.is_relative_to(src.origin)


def test_a_download_with_a_dolby_xml_is_a_dolby_source(
        tmp_path, silence_console, monkeypatch):
    silence_console(console)
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_0285_SUBSYS_103C860F.xml")
    download = _download(tmp_path, {
        "src/Dolby/DEV_0285_SUBSYS_103C860F.xml": xml.read_bytes()})
    monkeypatch.setattr(dp, "pick_xml", lambda tree, path, best_guess=False:
                        next(tree.rglob("DEV_0285_SUBSYS_103C860F.xml")))
    src = _source([str(download)])
    assert src.format is None and src.has_voicings and src.layer is None
    assert src.xml_path.name == "DEV_0285_SUBSYS_103C860F.xml"


def _source_of(tmp_path, flat: tuple[str, ...],
               names=("music", "movie", "voice")):
    """A standalone `TuningSource` holding *names*, those in *flat*
    changing nothing, built without a machine to bind."""
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    layer = sonitude.find(a, "10EC0285", "103C860F", True)
    return replace(tuning_source.standalone(apo_discover.STANDALONE[0], a,
                                            None),
                   profiles={n: replace(layer, changes_nothing=n in flat)
                             for n in names})


@pytest.mark.parametrize("flat,names,profile,picked,banner", [
    ((), ("music", "movie", "voice"), "voice", True, "voice"),
    ((), ("music",), "music", False,
     "music (this speaker's only sound mode)"),
    (("music", "voice"), ("music", "movie", "voice"), "movie", False,
     "movie (the only one of this speaker's 3 sound modes that changes the "
     "sound)"),
    (("music",), ("music", "movie", "voice"), "movie", False,
     "movie (the first-listed of the 2 sound modes that change this "
     "speaker's sound — --list names them; --profile picks)"),
    ((), ("music", "movie", "voice"), "music", False,
     "music (the first-listed of this speaker's 3 sound modes — --list "
     "names them; --profile picks)"),
])
def test_the_profile_banner_says_why_this_mode(tmp_path, flat, names,
                                               profile, picked, banner):
    src = _source_of(tmp_path, flat, names)
    assert src.profile_banner(profile, picked) == banner


def test_the_listing_marks_each_mode(tmp_path, unheard_movie_and_voice):
    src = _source_of(tmp_path, ("music",))
    assert src.listing() == [
        "  endpoint: internal_speaker (operating_mode=normal)",
        "    profile: music (changes nothing in this tuning)",
        "    profile: movie (experimental)",
        "    profile: voice (experimental)"]


_NO_DOLBY = "reads a Dolby file, and this tuning has none — drop it"


@pytest.mark.parametrize("flags,text", [
    (("--endpoint", "headphone"), f"--endpoint {_NO_DOLBY}"),
    (("--mode", "headphone"), f"--mode {_NO_DOLBY}"),
    (("--best-guess",), f"--best-guess {_NO_DOLBY}"),
    (("--enable", "virtual-bass"),
     "--enable virtual-bass acts on a Dolby stage, and this tuning has none "
     "— drop it"),
    (("--volmax-slot", "output-gain"),
     "--volmax-slot acts on a Dolby stage, and this tuning has none — "
     "drop it"),
    # The default, typed: indistinguishable from not typing it, and as
    # harmless.
    (("--volmax-slot", "input-gain"), None),
    (("--disable", "vendor-apo"),
     "--disable vendor-apo would leave nothing to build: the Sonitude tuning "
     "is this run's only one — drop it"),
    # The tuning's content modes are its profiles.
    (("--list",), None),
    (("--all-profiles",), None),
    (("--profile", "movie"), None),
    ((), None),
])
def test_a_standalone_source_refuses_what_needs_a_dolby_file(
        tmp_path, flags, text):
    a = tmp_path / _ARCHIVE
    _, args = _args([str(a), *flags])
    src = tuning_source.standalone(sonitude.FORMAT, a, args.prefix)
    assert src.refusal(args) == text


@pytest.mark.parametrize("name", [n for n in messages.DISABLEABLE_FILTERS
                                  if n != apo_layer.FLAG])
def test_every_dolby_stage_disable_is_refused(tmp_path, name):
    a = tmp_path / _ARCHIVE
    _, args = _args([str(a), "--disable", name])
    src = tuning_source.standalone(sonitude.FORMAT, a, args.prefix)
    assert src.refusal(args) == (f"--disable {name} acts on a Dolby stage, "
                                 "and this tuning has none — drop it")


@pytest.mark.parametrize("flags", [
    ("--list",), ("--best-guess",), ("--volmax-slot", "output-gain"),
    ("--disable", "mbc"), ("--enable", "dialog"), ("--mode", "headphone")])
def test_a_dolby_source_refuses_none_of_its_own_flags(tmp_path, flags):
    xml = tmp_path / "DEV_0285_SUBSYS_103C860F.xml"
    _, args = _args([str(xml), *flags])
    assert tuning_source.dolby(xml, None).refusal(args) is None


# --- the PipeWire wrapper ----------------------------------------------------

def _wrapper(tmp_path, *flags, source: Path | None = None):
    """The wrapper run on *source*, by default a Sonitude archive with all
    three modes for the device `_this_machine` fakes: the child reads it
    through the environment `fake_host` set."""
    if source is None:
        source = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                          {177: _three_modes()})
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("XDG_DATA_HOME", "XDG_CONFIG_HOME")}
    return subprocess.run(
        [sys.executable, str(_WRAPPER), str(source),
         "--output-dir", str(tmp_path / "confs"),
         "--target-sink", "", "--no-activate", "--no-color", *flags],
        capture_output=True, text=True, timeout=120,
        env={**env, "HOME": str(home)})


def test_the_wrapper_converts_the_default_profile(tmp_path, fake_host):
    _this_machine(fake_host)
    result = _wrapper(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in (tmp_path / "confs").iterdir()) == [
        "Sonitude_Music.conf", "Sonitude_Music.irs"]
    assert "other voicings" not in result.stdout
    assert "Dolby's default voicing" not in result.stdout
    assert "experimental:" not in result.stdout.split("[2/3]")[1]


def test_the_wrapper_converts_every_profile_under_all_profiles(tmp_path, fake_host):
    _this_machine(fake_host)
    result = _wrapper(tmp_path, "--all-profiles", "--target-object",
                      "alsa_output.speakers")
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in (tmp_path / "confs").iterdir()) == sorted(
        f"Sonitude_{m}.{ext}" for m in ("Music", "Movie", "Voice")
        for ext in ("conf", "irs"))
    converting = result.stdout.split("[2/3]")[1].split("\n")
    assert [line.strip() for line in converting[1:4]] == [
        "Sonitude-Music", "Sonitude-Movie", "Sonitude-Voice"]


def test_the_wrapper_refuses_a_voicing_on_a_tuning_without_any(tmp_path, fake_host):
    _this_machine(fake_host)
    result = _wrapper(tmp_path, "--variant", "warm")
    assert result.returncode == 1
    assert ("error: --variant picks among a Dolby tuning's voicings, and "
            "this tuning has none — drop --variant warm") in result.stdout
    assert not (tmp_path / "confs").exists()


def test_the_wrapper_still_converts_a_dolby_voicing(tmp_path):
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_0285_SUBSYS_103C860F.xml")
    result = _wrapper(tmp_path, "--variant", "warm", source=xml)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in (tmp_path / "confs").iterdir()) == [
        "Dolby_Warm.conf", "Dolby_Warm.irs"]


# --- the high-pass field -----------------------------------------------------

def test_the_stage_blocks_walk_to_the_end_of_the_file():
    values = sonitude.stage_values(_preset_bin(_curve()))
    assert sorted(values) == [(5, 13), (5, 14), (5, 19), (5, 20)]
    assert sonitude.stage_values(_preset_bin(_curve()) + b"\0") == {}


def test_the_high_pass_corner_comes_from_its_field(tmp_path):
    """_curve() steps up after 80 Hz; with a 70 Hz corner the Butterworth
    sits at 70 Hz, and the stored segment between 70 and 80 Hz, more than a
    grid point above the corner, stands."""
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1},
                 {1: _preset_bin(_curve(), high_pass=(70.0, 70.0))})
    layer = sonitude.find(a, "10EC0285", "103C860F", True)
    n = _below(70.0)
    hz = np.array(layer.curve_hz[:n])
    assert np.allclose(layer.curve_left_db[:n], _butterworth4_db(70.0, hz),
                       atol=1e-4)
    assert np.allclose(layer.curve_left_db[n:],
                       np.minimum(_curve(), 0)[n:], atol=1e-5)


def test_a_preset_with_its_high_pass_off_keeps_the_stored_segment(tmp_path):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1},
                 {1: _preset_bin(_curve(), high_pass=(None, 80.0))})
    layer = sonitude.find(a, "10EC0285", "103C860F", True)
    assert np.allclose(layer.curve_left_db, np.minimum(_curve(), 0),
                       atol=1e-5)
    assert layer.curve_right_db[0] < -70


@pytest.mark.parametrize("data", [
    _preset_bin(_curve()) + b"\0",   # the stage blocks don't walk to the end
    _preset_bin(_curve(), high_pass=(struct.pack("<Bf", 1, 80.0) + b"\0",) * 2),
], ids=["walk", "length"])
def test_a_high_pass_field_not_read_is_not_said_to_be_off(tmp_path, data):
    """Built as off, by keeping the stored segment, but said as unread."""
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1}, {1: data})
    layer = sonitude.find(a, "10EC0285", "103C860F", True)
    assert np.allclose(layer.curve_left_db, np.minimum(_curve(), 0),
                       atol=1e-5)
    stage = _high_pass_stage(layer)
    assert stage.status == apo_layer.NOT_REPRODUCED
    assert stage.summary == ("not read from this preset, so the curve's own "
                             "low end is kept")


def test_a_high_pass_read_on_one_channel_only_says_which(tmp_path):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1},
                 {1: _preset_bin(_curve(), high_pass=(80.0, b"\1"))})
    layer = sonitude.find(a, "10EC0285", "103C860F", True)
    stage = _high_pass_stage(layer)
    assert stage.status == apo_layer.BUILT
    assert stage.summary == "corner at 80 Hz left, not read right"


@pytest.mark.parametrize("corner", [float("nan"), 5.0, 5000.0])
def test_a_high_pass_no_tuning_uses_is_unsupported(tmp_path, corner):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1},
                 {1: _preset_bin(_curve(), high_pass=(corner, 80.0))})
    with pytest.raises(apo_layer.UnsupportedApoConfig, match="high-pass"):
        sonitude.find(a, "10EC0285", "103C860F", True)


# --- the pin a refactor must not move -----------------------------------------

# Recorded from the build below before the source refactor (2026-10-21).
# A change in behaviour re-records both in the same commit as its cause
# (.claude/rules/testing.md); a refactor leaves them as they are.
_PIN_PRESET_SHA = (
    "20577810ae3da8de8d1d282907664728a8fe2ed6a483df6010299d8dd636f007")
_PIN_HZ = np.geomspace(30, 16000, 24)
_PIN_DB = [-33.39, -24.39, -15.28, -6.83, 0.43, -0.02, 0.08, 0.09, -0.44,
           -1.77, -4.44, -5.96, -4.31, -1.68, -0.35, -0.03, -0.0, 0.01, -0.0,
           -0.0, 0.0, -0.0, 0.0, 0.0]


def test_the_vendor_only_build_is_pinned(tmp_path, fake_host,
                                         silence_console, capsys):
    import hashlib
    from scipy.io import wavfile
    silence_console(console)
    _this_machine(fake_host, True)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    _, out_dir = _run(tmp_path, capsys, a)
    preset = json.loads(next(out_dir.glob("*.json")).read_text())
    # The kernel name hashes the taps, and `_generator` carries the release
    # version; the taps are pinned below instead.
    preset["output"]["convolver#0"].pop("kernel-name")
    preset.pop("_generator")
    digest = hashlib.sha256(
        json.dumps(preset, sort_keys=True).encode()).hexdigest()
    assert digest == _PIN_PRESET_SHA, digest
    sr, taps = wavfile.read(next((tmp_path / "irs").glob("*.irs")))
    H = np.fft.rfft(taps[:, 0].astype(float), 1 << 16)
    f = np.fft.rfftfreq(1 << 16, 1 / sr)
    db = np.interp(_PIN_HZ, f, 20 * np.log10(np.abs(H) + 1e-15))
    assert np.allclose(db, _PIN_DB, atol=0.05)
