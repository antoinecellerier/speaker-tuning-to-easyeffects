"""HP's Bang & Olufsen tuning (`lib/apo/sonitude.py`) and the vendor-only run.

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
from pathlib import Path

import numpy as np
import pytest

import dolby_to_easyeffects
from lib import console, driver_package as dp
from lib.apo import layer as apo_layer
from lib.apo import sonitude
from lib.dax import parse
from lib.preset import emit, fir
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
    None = off)."""
    def rec(payload: bytes) -> bytes:
        return struct.pack("<I", len(payload)) + payload

    def curve(c):
        return (struct.pack("<H", len(c)) + struct.pack(f"<{len(c)}f", *c)
                + b"\0\0")

    def corner(hz):
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


def _archive(path: Path, entries: dict[str, int], presets: dict[int, bytes],
             codename: str = "X_Synth") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("SSIDMAP.bin", _ssidmap(entries))
        for n, data in presets.items():
            z.writestr(f"{n}.{codename}.txt", "Product/Family: synthetic\n")
            z.writestr(f"{n}/{sonitude.SPEAKER_PRESET}", data)
    return path


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
    assert layer is not None and layer.standalone and layer.default_on
    assert layer.label == "HP Bang & Olufsen (Sonitude preset 177, X_Synth)"
    assert layer.hardware_id == ("INTELAUDIO\\FUNC_01&VEN_10EC&DEV_0285"
                                 "&SUBSYS_103C860F")
    assert layer.eq_left == () and layer.dynamics == ()
    assert len(layer.curve_hz) == 265
    # Above the break (80 Hz, point 72) the curve is read as is, cuts only.
    assert np.allclose(layer.curve_left_db[73:], np.minimum(_curve(), 0)[73:],
                       atol=1e-5)


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
                 {1: _preset_bin(_curve(step_at=None))})
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


def test_a_curve_without_a_break_keeps_its_own_roll_off(tmp_path):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1},
                 {1: _preset_bin(_ramp())})
    layer = sonitude.find(a, "10EC0285", "103C860F", True)
    assert np.allclose(layer.curve_left_db, _ramp(), atol=1e-5)


@pytest.mark.parametrize("step,replaced", [(5.9, False), (6.1, True)])
def test_the_break_is_a_step_of_more_than_6_db(step, replaced):
    curve = [-20.0] * 100 + [-20.0 + step] * (sonitude.CURVE_POINTS - 100)
    out = sonitude._high_pass_below_break(curve, 80.0)
    assert (out[:100] != tuple(curve[:100])) == replaced
    assert out[100:] == tuple(curve[100:])


def test_each_channel_finds_its_own_break(tmp_path):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1},
                 {1: _preset_bin(_curve(), _ramp())})
    layer = sonitude.find(a, "10EC0285", "103C860F", True)
    assert layer.curve_left_db[0] < -70
    assert np.allclose(layer.curve_right_db, _ramp(), atol=1e-5)


# --- the run -----------------------------------------------------------------

def _run(tmp_path, capsys, source, *flags):
    out_dir, irs_dir = tmp_path / "out", tmp_path / "irs"
    dolby_to_easyeffects.main([
        str(source), "--skip-ee-check", "--output-dir", str(out_dir),
        "--irs-dir", str(irs_dir), *flags])
    return " ".join(capsys.readouterr().out.split()), out_dir


def test_a_vendor_only_run_builds_one_preset_from_the_curve(
        tmp_path, silence_console, capsys):
    silence_console(console)
    a = _archive(tmp_path / "dl" / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    out, out_dir = _run(tmp_path, capsys, a, "--hw-id", "10EC0285:103C860F")
    presets = sorted(out_dir.glob("*.json"))
    assert [p.name for p in presets] == ["HP-BO-Balanced.json"]
    order = json.loads(presets[0].read_text())["output"]["plugins_order"]
    assert order == ["convolver#0", "limiter#0"]
    assert "The only speaker tuning this laptop ships" in out
    assert "Correction check passed from 403 Hz up" in out
    assert "[vendor-tuning-unconfirmed]" in out
    # Nothing that refers to a Dolby file or offers a refused flag.
    assert "--disable vendor-apo" not in out
    assert "attach the XML" not in out
    assert "Dolby's default voicing" not in out


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
    assert "Found HP Bang & Olufsen (Sonitude preset 177, X_Synth)" in out


def test_no_preset_for_this_machine_says_which_ids_it_tried(
        tmp_path, silence_console):
    silence_console(console)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    with pytest.raises(RuntimeError, match="10EC0285:103C8600"):
        dolby_to_easyeffects.main([str(a), "--dry-run", "--skip-ee-check",
                                   "--hw-id", "10EC0285:103C8600"])


@pytest.mark.parametrize("flags", [("--list",), ("--all-profiles",),
                                   ("--profile", "music"),
                                   ("--disable", "vendor-apo")])
def test_flags_that_need_a_dolby_file_are_refused(tmp_path, flags, capsys):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    with pytest.raises(SystemExit):
        dolby_to_easyeffects.main([str(a), "--hw-id", "10EC0285:103C860F",
                                   *flags])
    assert "Bang & Olufsen" in capsys.readouterr().err


def test_hw_id_without_a_vendor_tuning_is_refused(tmp_path, capsys):
    xml = tmp_path / "DEV_0285_SUBSYS_103C860F.xml"
    xml.write_text("<x/>")
    with pytest.raises(SystemExit):
        dolby_to_easyeffects.main([str(xml), "--hw-id", "10EC0285:103C860F"])
    assert "--hw-id" in capsys.readouterr().err


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
        tmp_path, silence_console, capsys):
    silence_console(console)
    out, out_dir = _run(tmp_path, capsys, _download(tmp_path),
                        "--hw-id", "10EC0285:103C860F")
    assert "Found HP Bang & Olufsen (Sonitude preset 177, X_Synth)" in out
    assert [p.name for p in out_dir.glob("*.json")] == ["HP-BO-Balanced.json"]


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
    assert picked and "Bang & Olufsen (Sonitude" not in out


# --- choosing the preset -----------------------------------------------------

@pytest.mark.parametrize("text,want", [
    ("10EC0285:103C860F", "10EC0285:103C860F"),
    # As --speaker-info prints them.
    ("0x10ec0285:0x103c860f", "10EC0285:103C860F"),
    ("intelaudio:10EC0285:103C860F", "INTELAUDIO:10EC0285:103C860F"),
])
def test_hw_id_takes_the_ids_as_speaker_info_prints_them(text, want):
    assert dolby_to_easyeffects._hw_id(text) == want


@pytest.mark.parametrize("text", ["10EC0285", "10EC0285:103C86GZ",
                                  "10EC285:103C860F", "PCI:10EC0285:103C860F",
                                  "10EC0285:103C860F:X"])
def test_hw_id_refuses_what_is_not_two_hex_ids(text):
    with pytest.raises(argparse.ArgumentTypeError):
        dolby_to_easyeffects._hw_id(text)


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


def test_two_tunings_for_one_id_need_the_bus_with_hw_id(
        tmp_path, silence_console):
    silence_console(console)
    with pytest.raises(RuntimeError, match="--hw-id INTELAUDIO:10EC0285"):
        dolby_to_easyeffects.main([str(_two_buses(tmp_path)), "--dry-run",
                                   "--skip-ee-check",
                                   "--hw-id", "10EC0285:103C860F"])


@pytest.mark.parametrize("bus,preset", [("INTELAUDIO", 1), ("HDAUDIO", 2)])
def test_the_bus_in_hw_id_picks_its_archive(tmp_path, silence_console,
                                            capsys, bus, preset):
    silence_console(console)
    out, _ = _run(tmp_path, capsys, _two_buses(tmp_path), "--dry-run",
                  "--hw-id", f"{bus}:10EC0285:103C860F")
    assert f"Sonitude preset {preset}," in out
    assert "two different tunings" not in out


def test_a_bus_without_the_id_names_the_bus_with_it(tmp_path,
                                                    silence_console):
    silence_console(console)
    download = _bundle(tmp_path, _archive(
        tmp_path / "src" / _HDA_ARCHIVE, {"103C860F": 2},
        {2: _preset_bin(_curve())}))
    with pytest.raises(RuntimeError, match="HDAUDIO archive only"):
        dolby_to_easyeffects.main([str(download), "--dry-run",
                                   "--skip-ee-check", "--hw-id",
                                   "INTELAUDIO:10EC0285:103C860F"])


def _this_machine(fake_host, sof):
    fake_host("/proc/asound/card0/codec#0",
              "Codec: Realtek ALC285\nVendor Id: 0x10ec0285\n"
              "Subsystem Id: 0x103c860f\n")
    fake_host("/proc/asound/cards",
              " 0 [sofhdadsp      ]: sof-hda-dsp - sof-hda-dsp\n" if sof
              else " 0 [PCH            ]: HDA-Intel - HDA Intel PCH\n")


@pytest.mark.parametrize("sof,preset,other", [(True, 1, "HDAUDIO"),
                                              (False, 2, "INTELAUDIO")])
def test_two_tunings_for_this_laptop_say_which_was_used_and_why(
        tmp_path, fake_host, silence_console, capsys, sof, preset, other):
    silence_console(console)
    _this_machine(fake_host, sof)
    out, _ = _run(tmp_path, capsys, _two_buses(tmp_path), "--dry-run")
    assert f"Sonitude preset {preset}," in out
    assert "two different tunings for this laptop" in out
    assert f"--hw-id {other}:10EC0285:103C860F" in out


def test_one_tuning_under_two_buses_is_not_a_choice(
        tmp_path, fake_host, silence_console, capsys):
    silence_console(console)
    _this_machine(fake_host, True)
    out, _ = _run(tmp_path, capsys, _two_buses(tmp_path, same=True),
                  "--dry-run")
    assert "Sonitude preset 1," in out
    assert "two different tunings" not in out


# --- what a failed lookup says -----------------------------------------------

def test_a_missing_archive_is_said_as_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        dolby_to_easyeffects.main([str(tmp_path / _ARCHIVE), "--dry-run",
                                   "--skip-ee-check",
                                   "--hw-id", "10EC0285:103C860F"])


def test_a_damaged_archive_is_said_as_unreadable_not_as_another_model(
        tmp_path, silence_console):
    silence_console(console)
    (tmp_path / _ARCHIVE).write_bytes(b"PK\x03\x04 not a zip")
    with pytest.raises(RuntimeError, match="can't be read") as exc:
        dolby_to_easyeffects.main([str(tmp_path / _ARCHIVE), "--dry-run",
                                   "--skip-ee-check",
                                   "--hw-id", "10EC0285:103C860F"])
    assert "another model" not in str(exc.value)


def test_a_miss_with_hw_id_does_not_suggest_hw_id(tmp_path, silence_console):
    silence_console(console)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    with pytest.raises(RuntimeError) as exc:
        dolby_to_easyeffects.main([str(a), "--dry-run", "--skip-ee-check",
                                   "--hw-id", "10EC0285:103C8600"])
    assert "with --hw-id" not in str(exc.value)
    assert "--speaker-info" in str(exc.value)


def test_a_miss_on_this_laptop_points_at_the_download(
        tmp_path, fake_host, silence_console):
    silence_console(console)
    _this_machine(fake_host, True)
    a = _archive(tmp_path / _ARCHIVE, {"103C8600": 177},
                 {177: _preset_bin(_curve())})
    with pytest.raises(RuntimeError, match="10EC0285:103C860F.*download"):
        dolby_to_easyeffects.main([str(a), "--dry-run", "--skip-ee-check"])


# --- flags with nothing to act on --------------------------------------------

@pytest.mark.parametrize("flags", [
    ("--enable", "autogain"), ("--enable", "dialog"),
    ("--enable", "virtual-bass"), ("--enable", "level-restore"),
    ("--mode", "headphone"), ("--endpoint", "headphone")])
def test_flags_for_a_dolby_stage_are_refused(tmp_path, flags, capsys):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    with pytest.raises(SystemExit):
        dolby_to_easyeffects.main([str(a), "--hw-id", "10EC0285:103C860F",
                                   *flags])
    assert "Bang & Olufsen" in capsys.readouterr().err


def test_level_restore_is_not_offered_on_a_curve_that_only_cuts(
        tmp_path, silence_console, capsys):
    silence_console(console)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    out, _ = _run(tmp_path, capsys, a, "--hw-id", "10EC0285:103C860F")
    assert "level-restore" not in out


@pytest.mark.parametrize("prefix,stem", [(None, "HP-BO"), ("Dolby", "Dolby"),
                                         ("Mine", "Mine")])
def test_a_typed_prefix_stands(tmp_path, silence_console, capsys, prefix,
                               stem):
    silence_console(console)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    _, out_dir = _run(tmp_path, capsys, a, "--hw-id", "10EC0285:103C860F",
                      *(["--prefix", prefix] if prefix else []))
    assert [p.name for p in out_dir.glob("*.json")] == [
        f"{stem}-Balanced.json"]


def test_a_vendor_only_run_hands_a_wrapper_no_xml(tmp_path, silence_console):
    silence_console(console)
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    resolved = {}
    dolby_to_easyeffects.main([str(a), "--dry-run", "--skip-ee-check",
                               "--hw-id", "10EC0285:103C860F"],
                              resolved=resolved)
    assert resolved == {"xml_path": None, "vendor_only": True}


# --- the PipeWire wrapper ----------------------------------------------------

def _wrapper(tmp_path, *flags):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 177},
                 {177: _preset_bin(_curve())})
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("XDG_DATA_HOME", "XDG_CONFIG_HOME")}
    return subprocess.run(
        [sys.executable, str(_WRAPPER), str(a), "--hw-id",
         "10EC0285:103C860F", "--output-dir", str(tmp_path / "confs"),
         "--target-sink", "", "--no-activate", "--no-color", *flags],
        capture_output=True, text=True, timeout=120,
        env={**env, "HOME": str(home)})


def test_the_wrapper_converts_the_one_vendor_preset(tmp_path):
    result = _wrapper(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in (tmp_path / "confs").iterdir()) == [
        "HP_BO_Balanced.conf", "HP_BO_Balanced.irs"]
    assert "other voicings" not in result.stdout
    assert "Dolby's default voicing" not in result.stdout


def test_the_wrapper_says_a_vendor_tuning_has_one_voicing(tmp_path):
    result = _wrapper(tmp_path, "--variant", "warm")
    assert result.returncode == 1
    assert "one voicing, Balanced" in result.stdout
    assert "see the log above" not in result.stdout


# --- the high-pass field -----------------------------------------------------

def test_the_stage_blocks_walk_to_the_end_of_the_file():
    values = sonitude.stage_values(_preset_bin(_curve()))
    assert sorted(values) == [(5, 13), (5, 14), (5, 19), (5, 20)]
    assert sonitude.stage_values(_preset_bin(_curve()) + b"\0") == {}


def test_the_high_pass_corner_comes_from_its_field(tmp_path):
    """_curve() steps up after 80 Hz; a 75 Hz corner in the field moves the
    Butterworth there, and the stepped segment is still what it replaces."""
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1},
                 {1: _preset_bin(_curve(), high_pass=(75.0, 75.0))})
    layer = sonitude.find(a, "10EC0285", "103C860F", True)
    hz = np.array(layer.curve_hz[:73])
    assert np.allclose(layer.curve_left_db[:73], _butterworth4_db(75.0, hz),
                       atol=1e-4)
    assert np.allclose(layer.curve_left_db[73:],
                       np.minimum(_curve(), 0)[73:], atol=1e-5)


def test_a_preset_with_its_high_pass_off_keeps_the_stored_segment(tmp_path):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1},
                 {1: _preset_bin(_curve(), high_pass=(None, 80.0))})
    layer = sonitude.find(a, "10EC0285", "103C860F", True)
    assert np.allclose(layer.curve_left_db, np.minimum(_curve(), 0),
                       atol=1e-5)
    assert layer.curve_right_db[0] < -70


@pytest.mark.parametrize("corner", [float("nan"), 5.0, 5000.0])
def test_a_high_pass_no_tuning_uses_is_unsupported(tmp_path, corner):
    a = _archive(tmp_path / _ARCHIVE, {"103C860F": 1},
                 {1: _preset_bin(_curve(), high_pass=(corner, 80.0))})
    with pytest.raises(apo_layer.UnsupportedApoConfig, match="high-pass"):
        sonitude.find(a, "10EC0285", "103C860F", True)
