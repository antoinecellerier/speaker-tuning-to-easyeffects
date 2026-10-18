"""HP's Bang & Olufsen tuning (`lib/apo/sonitude.py`) and the vendor-only run.

Every archive here is synthetic: a writer below builds the `SSIDMAP.bin`,
manifest and `Preset.bin` shapes the parser reads, so no HP file ships with
the tests. The corpus tier reads the real archives.
"""

from __future__ import annotations

import json
import math
import struct
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

_ARCHIVE = "SEAPODAT.INTELAUDIO.FUNC_01-VEN_10EC-DEV_0285.zip"
_HDA_ARCHIVE = "SEAPODAT.HDAUDIO.FUNC_01-VEN_10EC-DEV_0285.zip"


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


def _preset_bin(left: list[float], right: list[float] | None = None) -> bytes:
    """Header bytes the parser skips, filler values, then the two curves."""
    def block(c):
        return (struct.pack("<IH", 2 + 4 * len(c) + 2, len(c))
                + struct.pack(f"<{len(c)}f", *c) + b"\0\0")
    filler = struct.pack("<I", 4) + struct.pack("<f", 0.001)
    return (b"\x06\x00\x10\x00" + b"\0" * 64 + filler + block(left)
            + block(right if right is not None else left) + filler)


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
    assert np.allclose(layer.curve_left_db, _curve(), atol=1e-5)


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


def test_a_step_sharper_than_the_bins_is_found():
    curve = (sonitude.curve_grid_hz(), tuple(_curve()))
    hz, step = emit._curve_jump(curve)
    assert math.isclose(hz, 80.0) and step > 6
    assert emit._curve_jump((sonitude.curve_grid_hz(),
                             tuple(_curve(step_at=None)))) is None


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
    assert "Correction check passed from 200 Hz up" in out
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
