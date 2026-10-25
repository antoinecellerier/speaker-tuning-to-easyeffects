"""Dolby DAX2 tunings read from a driver `.inf` (`lib/apo/dax2.py`, #148).

Every `.inf` here is synthetic: `_inf` below writes the shape the reader
walks (hardware ID → install section → AddReg → `OEMSettingsOverride`
preset → `{f112024a-…},N` endpoint values), so no driver file ships with the
tests. The corpus tier reads the real ones.
"""

from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import dolby_to_easyeffects
from lib import console
from lib.apo import dax2
from lib.apo.layer import UnsupportedApoConfig
from lib.dax import parse
from tests.conftest import write_synthetic_tuning_xml

_PKEY = "{f112024a-fe30-42a8-80ab-8dd825a06f78}"
_HZ = [47, 141, 234, 328, 469, 656, 844, 1031, 1313, 1688, 2250, 3000, 3750,
       4688, 5813, 7125, 9000, 11250, 13875, 19688]
_IEQ_HZ = [65, 136, 223, 332, 467, 634, 841, 1098, 1416, 1812, 2302, 2909,
           3663, 4598, 5756, 7194, 8976, 11186, 13927, 17326]


def _banded(*arrays: list[int]) -> list[int]:
    n = len(arrays[0])
    out = [n]
    for a in arrays:
        out += [n, *a]
    return out


def _values(**over) -> dict[int, object]:
    """A speaker preset's values, as the Surface Laptop 3's is laid out."""
    left = [-160, 250, 176, 96, -2] + [0] * 15
    right = [-144, 202, 168, 106, 32] + [0] * 15
    other = [0, 266] + [0] * 18
    v = {
        2: 96, 6: -320, 7: -320, 8: 0, 9: 0, 10: 1, 12: 10,
        13: _banded(_IEQ_HZ, [16 * k for k in range(20)]),
        18: _banded(_HZ, left, right, *[other] * 6),
        21: _banded(_HZ, [0] * 20),
        22: _banded(_HZ, [-1056] + [-192] * 19, [-864] + [0] * 19,
                    [1, 1] + [0] * 18),
        24: 1, 25: 96, 26: 12, 35: 1, 38: 5,
        42: [35, 160], 45: [3, 3, -32, -144, -192], 46: [94, 469],
    }
    v.update({int(k[1:]): x for k, x in over.items()})
    return {k: x for k, x in v.items() if x is not None}


def _propvariant(value) -> str:
    if isinstance(value, int):
        data = struct.pack("<HHHHi", 3, 0, 1, 0, value)
    else:
        data = struct.pack(f"<HHHH{len(value)}i", 0x41, 0, 1, 0, *value)
    return ",".join(f"{b:02x}" for b in data)


def _inf(path: Path, subsys="10EC11B2", presets=None, version="6.0.9249.2",
         continued=False) -> Path:
    """A driver .inf binding codec 10EC0274/*subsys* to *presets*: {preset
    key: values}, the first the speaker's, each named by an override."""
    presets = presets or {"PresetX_SPK": _values(), "PresetX_HP": _values()}
    lines = [f"[Version]\nDriverVer = 10/08/2021, {version}",
             "[Manufacturer]\n%Maker% = Models, NTamd64",
             "[Models.NTamd64]",
             f'"Speakers" = Model_X, INTELAUDIO\\FUNC_01&VEN_10EC&DEV_0274&SUBSYS_{subsys}',
             f'"Speakers" = Model_X, HDAUDIO\\FUNC_01&VEN_10EC&DEV_0274&SUBSYS_{subsys}',
             "[Model_X.NTamd64]\nAddReg = Common.AddReg, Tuning_X.AddReg",
             "[Common.AddReg]\nHKR,,Driver,,x.sys",
             "[Tuning_X.AddReg]"]
    for topo, name in zip(("SingleLineOutTopo", "SecondaryLineOutTopo"),
                          presets):
        lines.append(f'HKR,InterfaceSetting,{topo},0x10000,"SysFx",'
                     f'"OEMSettingsOverride","{name}"')
    for name, values in presets.items():
        for index, value in values.items():
            data = _propvariant(value)
            if continued:
                head, _, tail = data.partition(",")
                data = f"{head},\\\n    {tail}"
            lines.append(f'HKR,InterfaceSetting\\{name}\\EP\\0, '
                         f'"{_PKEY},{index}", 0x00000001, {data}')
    lines.append('[Strings]\nMaker = "Maker"')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def _package(folder: Path, **kw) -> Path:
    """A driver package folder: the .inf beside Dolby's DAX2 APO."""
    _inf(folder / "HDXSSTM2.inf", **kw)
    (folder / "DolbyDAX2APOv201.dll").write_bytes(b"MZ")
    return folder


# --- reading ----------------------------------------------------------------

def test_the_speaker_preset_binds_through_the_hardware_id(tmp_path):
    p = dax2.bind(_inf(tmp_path / "x.inf"), "10EC0274", "10EC11B2")
    assert p.name == "PresetX_SPK"
    assert p.hardware_id.startswith("INTELAUDIO\\")
    assert p.values[2] == 96 and p.values[18][:3] == [20, 20, 47]


def test_another_codec_binds_nothing(tmp_path):
    assert dax2.bind(_inf(tmp_path / "x.inf"), "10EC0274", "10EC1234") is None
    assert dax2.bind(_inf(tmp_path / "x.inf"), "10EC0298", "10EC11B2") is None


def test_continuation_lines_read_as_one_value(tmp_path):
    p = dax2.bind(_inf(tmp_path / "x.inf", continued=True), "10EC0274",
                  "10EC11B2")
    assert p.values[13][:4] == [20, 20, 65, 136]


def test_the_values_become_the_dolby_fields_they_stand_for(tmp_path):
    p = dax2.bind(_inf(tmp_path / "x.inf"), "10EC0274", "10EC11B2")
    t = dax2.tuning(p)
    assert t.freqs == _HZ
    assert t.ao_left[:3] == [-160, 250, 176]
    assert t.ao_right[:3] == [-144, 202, 168]
    assert t.volmax_boost == 6.0
    assert (t.ieq_enabled, t.ieq_amount) == (True, 10)
    assert t.regulator["threshold_high"][0] == -54.0
    assert t.regulator["threshold_low"][0] == -66.0
    assert t.regulator["isolated_band"][:3] == [1, 1, 0]
    assert t.regulator["timbre_preservation"] == 0.75
    assert t.vol_leveler == {"enable": 0, "amount": 0, "in_target": -20.0,
                             "out_target": -20.0}
    assert t.dialog_enhancer == {"amount": 5}
    assert t.virtual_bass["subgains"] == [-32, -144, -192]
    assert (t.peq_filters, t.mb_comp, t.surround) == ([], None, None)
    assert list(t.curves) == ["ieq_balanced"] and t.partial_voicings


def test_switched_off_stages_are_left_out(tmp_path):
    p = dax2.bind(_inf(tmp_path / "x.inf", presets={
        "P_SPK": _values(_24=0, _35=0, _10=0, _12=40)}),
        "10EC0274", "10EC11B2")
    t = dax2.tuning(p)
    assert t.regulator is None and t.dialog_enhancer is None
    # IEQ off reads as the DAX3 parser reads it: the assumed amount.
    assert (t.ieq_enabled, t.ieq_amount) == (False, 10)


def test_the_ieq_curve_moves_onto_the_optimizer_grid_in_log_frequency(
        tmp_path):
    p = dax2.bind(_inf(tmp_path / "x.inf"), "10EC0274", "10EC11B2")
    ieq = dax2.tuning(p).curves["ieq_balanced"]
    src = [16 * k for k in range(20)]
    # Below the first source band and above the last: flat.
    assert ieq[0] == src[0] and ieq[-1] == src[-1]
    # 141 Hz sits between 136 and 223 Hz.
    t = (math.log(141) - math.log(136)) / (math.log(223) - math.log(136))
    assert ieq[1] == round(src[1] + t * (src[2] - src[1]))


def test_a_single_value_stored_as_a_blob_is_refused_when_it_binds(tmp_path):
    inf_path = _inf(tmp_path / "x.inf", presets={"P_SPK": _values(_2=[96])})
    with pytest.raises(UnsupportedApoConfig, match="volmax-boost.*not a "
                       "number"):
        dax2.bind(inf_path, "10EC0274", "10EC11B2")


def test_values_set_through_an_added_interface_are_read(tmp_path):
    path = _inf(tmp_path / "x.inf", presets={"P_SPK": _values()})
    text = path.read_text().replace(
        "AddReg = Common.AddReg, Tuning_X.AddReg",
        "AddReg = Common.AddReg\n"
        "[Model_X.NTamd64.Interfaces]\n"
        "AddInterface = {6994ad04-93ef-11d0-a3cc-00a0c9223196}, Spk, "
        "Speaker_Interface\n"
        "[Speaker_Interface]\nAddReg = Tuning_X.AddReg")
    path.write_text(text)
    p = dax2.bind(path, "10EC0274", "10EC11B2")
    assert p.name == "P_SPK"


def test_a_big_endian_utf16_inf_is_recognised(tmp_path):
    path = _inf(tmp_path / "x.inf")
    path.write_bytes(b"\xfe\xff" + path.read_text().encode("utf-16-be"))
    assert dax2.mentions_dax2(path)
    assert dax2.bind(path, "10EC0274", "10EC11B2").name == "PresetX_SPK"


def test_no_codec_to_bind_reads_no_inf(tmp_path, monkeypatch):
    _package(tmp_path / "pkg")
    monkeypatch.setattr(dax2, "infs_below", lambda *a, **k: pytest.fail(
        "walked with nothing to bind"))
    assert dax2.find(tmp_path, []) is None


def test_a_value_the_reader_doesnt_map_cant_refuse_the_preset(tmp_path):
    inf_path = _inf(tmp_path / "x.inf")
    text = inf_path.read_text().replace(
        f'"{_PKEY},28", 0x00000001, 03,', f'"{_PKEY},28", 0x00000001, 08,')
    inf_path.write_text(text)
    assert dax2.bind(inf_path, "10EC0274", "10EC11B2").name == "PresetX_SPK"


def test_a_lone_headphone_preset_is_not_read_as_the_speakers(tmp_path):
    inf_path = _inf(tmp_path / "x.inf", presets={"P_HP": _values()})
    with pytest.raises(UnsupportedApoConfig, match="headphone output's"):
        dax2.bind(inf_path, "10EC0274", "10EC11B2")


def test_the_amd64_model_section_wins_over_the_undecorated_one(tmp_path):
    path = _inf(tmp_path / "x.inf")
    text = path.read_text().replace(
        "[Models.NTamd64]",
        "[Models]\n"
        '"Speakers" = Model_Z, HDAUDIO\\FUNC_01&VEN_10EC&DEV_0274'
        "&SUBSYS_10EC11B2\n[Models.NTamd64]")
    path.write_text(text)
    p = dax2.bind(path, "10EC0274", "10EC11B2")
    assert p.name == "PresetX_SPK"


def test_a_driver_store_walk_stops_below_package_depth(tmp_path):
    _package(tmp_path / "a" / "b" / "c" / "d")
    assert dax2.find(tmp_path, [("10EC0274", "10EC11B2")]) is None
    _package(tmp_path / "pkg")
    assert dax2.find(tmp_path, [("10EC0274", "10EC11B2")]) is not None


def test_a_lone_preset_needs_no_speaker_name(tmp_path):
    p = dax2.bind(_inf(tmp_path / "x.inf", presets={"Only": _values()}),
                  "10EC0274", "10EC11B2")
    assert p.name == "Only"


def test_presets_none_named_for_the_speaker_are_refused(tmp_path):
    inf_path = _inf(tmp_path / "x.inf", presets={"A": _values(),
                                                 "B": _values()})
    with pytest.raises(UnsupportedApoConfig, match="no single one is named for the speaker"):
        dax2.bind(inf_path, "10EC0274", "10EC11B2")


def test_dax3s_registry_form_is_not_read_as_dax2(tmp_path):
    inf_path = _inf(tmp_path / "x.inf", presets={
        "P_SPK": {**_values(), 48: [20]}})
    with pytest.raises(UnsupportedApoConfig, match="DAX3's registry form"):
        dax2.bind(inf_path, "10EC0274", "10EC11B2")


def test_a_blob_whose_counts_dont_add_up_is_refused(tmp_path):
    inf_path = _inf(tmp_path / "x.inf", presets={
        "P_SPK": _values(_22=[20, 20, *_HZ, 19, 1, 2])})
    with pytest.raises(UnsupportedApoConfig, match="doesn't split"):
        dax2.bind(inf_path, "10EC0274", "10EC11B2")


@pytest.mark.parametrize("blob", [[3], [0, 0, 0], [20]])
def test_a_blob_holding_no_arrays_is_refused(tmp_path, blob):
    inf_path = _inf(tmp_path / "x.inf", presets={"P_SPK": _values(_18=blob)})
    with pytest.raises(UnsupportedApoConfig):
        dax2.bind(inf_path, "10EC0274", "10EC11B2")


def test_a_malformed_virtual_bass_is_left_out_not_fatal(tmp_path):
    p = dax2.bind(_inf(tmp_path / "x.inf", presets={
        "P_SPK": _values(_45=[3])}), "10EC0274", "10EC11B2")
    t = dax2.tuning(p)
    assert t.virtual_bass is None and t.ao_left


def test_only_the_install_decoration_windows_applies_is_read(tmp_path):
    path = _inf(tmp_path / "x.inf")
    text = path.read_text().replace(
        "[Common.AddReg]",
        "[Model_X.NT]\nAddReg = Other.AddReg\n"
        "[Other.AddReg]\n"
        'HKR,InterfaceSetting,SingleLineOutTopo,0x10000,"SysFx",'
        '"OEMSettingsOverride","Other_SPK"\n'
        + "\n".join(f'HKR,InterfaceSetting\\Other_SPK\\EP\\0, "{_PKEY},{i}", '
                    f"0x00000001, {_propvariant(v)}"
                    for i, v in _values(_2=160).items())
        + "\n[Common.AddReg]")
    path.write_text(text)
    p = dax2.bind(path, "10EC0274", "10EC11B2")
    assert (p.name, p.values[2]) == ("PresetX_SPK", 96)


def test_a_preset_key_spelled_in_two_cases_is_one_preset(tmp_path):
    path = _inf(tmp_path / "x.inf", presets={"P_SPK": _values()})
    text = path.read_text()
    first = text.index("\\P_SPK\\EP")
    path.write_text(text[:first] + "\\p_spk\\EP" + text[first + 10:])
    p = dax2.bind(path, "10EC0274", "10EC11B2")
    assert p.name == "p_spk"
    assert p.values.keys() == {i for i in _values() if i in dax2.FIELDS}


def test_an_unreadable_inf_beside_a_readable_one_is_passed_over(tmp_path):
    _package(tmp_path / "old", version="6.0.8828.1")
    _package(tmp_path / "new", version="6.0.9249.2",
             presets={"A": _values(), "B": _values()})
    p = dax2.find(tmp_path, [("10EC0274", "10EC11B2")])
    assert p.inf_path.parent.name == "old"


def test_an_inf_that_cannot_be_parsed_is_not_blamed_on_this_device(tmp_path):
    folder = tmp_path / "pkg"
    folder.mkdir()
    (folder / "DolbyDAX2APOv201.dll").write_bytes(b"MZ")
    # A UTF-16 byte-order mark over bytes that don't decode, key included.
    (folder / "broken.inf").write_bytes(
        b"\xff\xfe" + _PKEY.encode("utf-16-le") + b"\x00\xd8")
    assert dax2.find(tmp_path, [("10EC0274", "10EC11B2")]) is None


def test_a_codec_refused_by_one_inf_leaves_the_others_to_bind(tmp_path):
    path = _inf(tmp_path / "pkg" / "x.inf",
                presets={"A": _values(), "B": _values()})
    text = path.read_text() + (
        "[Models.NTamd64]\n" "")  # same model section, extended below
    other = ('"Speakers" = Model_Y, HDAUDIO\\FUNC_01&VEN_10EC&DEV_0298'
             '&SUBSYS_10EC1072\n'
             "[Model_Y.NTamd64]\nAddReg = Tuning_Y.AddReg\n"
             "[Tuning_Y.AddReg]\n"
             'HKR,InterfaceSetting,SingleLineOutTopo,0x10000,"SysFx",'
             '"OEMSettingsOverride","Y_SPK"\n'
             + "\n".join(f'HKR,InterfaceSetting\\Y_SPK\\EP\\0, '
                         f'"{_PKEY},{i}", 0x00000001, {_propvariant(v)}'
                         for i, v in _values().items()) + "\n")
    text = path.read_text().replace(
        "[Model_X.NTamd64]", other.split("\n", 1)[0] + "\n[Model_X.NTamd64]")
    text = text.replace("[Strings]", other.split("\n", 1)[1] + "[Strings]")
    path.write_text(text)
    (path.parent / "DolbyDAX2APOv201.dll").write_bytes(b"MZ")
    p = dax2.find(tmp_path, [("10EC0274", "10EC11B2"),
                             ("10EC0298", "10EC1072")])
    assert p.name == "Y_SPK"


def test_an_unreadable_inf_alone_says_why(tmp_path):
    _package(tmp_path / "new", presets={"A": _values(), "B": _values()})
    with pytest.raises(UnsupportedApoConfig, match="no single one is named for the speaker"):
        dax2.find(tmp_path, [("10EC0274", "10EC11B2")])


def test_an_unknown_value_type_is_refused(tmp_path):
    inf_path = _inf(tmp_path / "x.inf")
    text = inf_path.read_text().replace(
        f'"{_PKEY},2", 0x00000001, 03,', f'"{_PKEY},2", 0x00000001, 08,')
    inf_path.write_text(text)
    with pytest.raises(UnsupportedApoConfig, match="PROPVARIANT type 0x8"):
        dax2.bind(inf_path, "10EC0274", "10EC11B2")


def test_the_newest_driver_version_wins(tmp_path):
    _package(tmp_path / "old", version="6.0.8828.1",
             presets={"Old_SPK": _values(_2=160)})
    _package(tmp_path / "new", version="6.0.9249.2")
    p = dax2.find(tmp_path, [("10EC0274", "10EC11B2")])
    assert p.values[2] == 96


def test_only_infs_beside_dolbys_apo_are_read_in_a_driver_store(tmp_path):
    _inf(tmp_path / "pkg" / "HDXSSTM2.inf")
    assert dax2.find(tmp_path, [("10EC0274", "10EC11B2")]) is None
    assert dax2.find(tmp_path, [("10EC0274", "10EC11B2")],
                     beside_apo=False) is not None


def test_a_downloads_infs_are_kept_only_beside_dolbys_apo():
    assert dax2.wanted_members([
        "a/HDXSSTM.inf", "a/DolbyDAX2APOv201.dll", "b/HDXRT.inf",
        "c/x.INF", "c/dolbydax2apoprop.DLL"]) == {"a/HDXSSTM.inf", "c/x.INF"}


# --- runs -------------------------------------------------------------------

def _this_machine(fake_host, subsys="10ec11b2"):
    fake_host("/proc/asound/card0/codec#0",
              "Codec: Realtek ALC274\nVendor Id: 0x10ec0274\n"
              f"Subsystem Id: 0x{subsys}\n")
    fake_host("/proc/asound/cards",
              " 0 [PCH            ]: HDA-Intel - HDA Intel PCH\n")


def _run(tmp_path, capsys, source, *flags):
    out_dir, irs_dir = tmp_path / "out", tmp_path / "irs"
    dolby_to_easyeffects.main([
        str(source), "--skip-ee-check", "--output-dir", str(out_dir),
        "--irs-dir", str(irs_dir), *flags])
    return " ".join(capsys.readouterr().out.split()), out_dir


def test_an_inf_builds_one_balanced_preset_from_the_dolby_chain(
        tmp_path, fake_host, silence_console, capsys):
    silence_console(console)
    _this_machine(fake_host)
    out, out_dir = _run(tmp_path, capsys, _inf(tmp_path / "x.inf"))
    presets = sorted(out_dir.glob("*.json"))
    assert [p.name for p in presets] == ["Dolby-Balanced.json"]
    order = json.loads(presets[0].read_text())["output"]["plugins_order"]
    # No speaker PEQ (equalizer#0) and no multiband compressor
    # (multiband_compressor#0): DAX2 carries neither. The dialog enhancer's
    # equalizer ships bypassed; the regulator is the second compressor.
    assert order == ["convolver#0", "equalizer#1", "multiband_compressor#1",
                     "limiter#0"]
    assert "Found Dolby DAX2 (x.inf, PresetX_SPK) for INTELAUDIO" in out
    assert "Tuning: PresetX_SPK (Dolby DAX2, the only one)" in out
    assert "not found in XML" not in out
    assert "[dax2-tuning-unconfirmed]" in out
    assert "Dolby-Balanced is experimental: nobody has listened to it yet." in out
    assert "How loud each plays" not in out


def test_an_inf_binding_another_device_says_so(tmp_path, fake_host,
                                               silence_console):
    silence_console(console)
    _this_machine(fake_host, subsys="10ec1234")
    with pytest.raises(RuntimeError, match="binds no Dolby DAX2 tuning to "
                       "this device's audio hardware .*Run without "
                       "a path to search a mounted Windows partition"):
        dolby_to_easyeffects.main([str(_inf(tmp_path / "x.inf")),
                                   "--skip-ee-check", "--dry-run"])


def test_a_download_binding_another_device_says_it_is_another_models(
        tmp_path, fake_host, silence_console):
    silence_console(console)
    _this_machine(fake_host, subsys="10ec1234")
    with pytest.raises(RuntimeError, match="likely another model's driver"):
        dolby_to_easyeffects.main([str(_download(tmp_path)),
                                   "--skip-ee-check", "--dry-run"])


def test_an_inf_without_dax2_is_refused(tmp_path, silence_console):
    silence_console(console)
    plain = tmp_path / "plain.inf"
    plain.write_text("[Version]\nSignature = x\n")
    with pytest.raises(RuntimeError, match="carries no Dolby DAX2 tuning"):
        dolby_to_easyeffects.main([str(plain), "--skip-ee-check",
                                   "--dry-run"])


@pytest.mark.parametrize("flag", [["--profile", "music"], ["--all-profiles"],
                                  ["--endpoint", "headphone"]])
def test_flags_that_pick_among_an_xmls_tunings_are_refused(
        tmp_path, fake_host, silence_console, capsys, flag):
    silence_console(console)
    _this_machine(fake_host)
    with pytest.raises(SystemExit):
        dolby_to_easyeffects.main([str(_inf(tmp_path / "x.inf")),
                                   "--skip-ee-check", "--dry-run", *flag])
    assert f"— drop {flag[0]}" in capsys.readouterr().err


def test_list_names_the_one_preset(tmp_path, fake_host, silence_console,
                                   capsys):
    silence_console(console)
    _this_machine(fake_host)
    dolby_to_easyeffects.main([str(_inf(tmp_path / "x.inf")),
                               "--skip-ee-check", "--list"])
    out = capsys.readouterr().out
    assert "preset: PresetX_SPK (the only one; experimental)" in out


def _download(tmp_path, extra: dict[str, bytes] | None = None,
              with_apo=True) -> Path:
    inf_path = _inf(tmp_path / "src" / "HDXSSTM2.inf")
    download = tmp_path / "SurfaceLike.zip"
    with zipfile.ZipFile(download, "w") as z:
        z.write(inf_path, "SurfaceUpdate/hdxsstm2/HDXSSTM2.inf")
        if with_apo:
            z.writestr("SurfaceUpdate/hdxsstm2/DolbyDAX2APOv201.dll", b"MZ")
        z.writestr("SurfaceUpdate/hdxsstm2/RTKVHD64.sys", b"x")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return download


def test_a_download_with_a_dax2_inf_builds_from_it(
        tmp_path, fake_host, silence_console, capsys):
    silence_console(console)
    _this_machine(fake_host)
    out, out_dir = _run(tmp_path, capsys, _download(tmp_path))
    assert "Unpacked 1 driver .inf file into" in out
    assert "Found Dolby DAX2 (HDXSSTM2.inf, PresetX_SPK)" in out
    assert [p.name for p in out_dir.glob("*.json")] == ["Dolby-Balanced.json"]


def test_a_download_on_a_machine_with_no_readable_codec_blames_no_model(
        tmp_path, fake_host, silence_console):
    silence_console(console)
    fake_host("/proc/asound/cards", "")
    with pytest.raises(RuntimeError, match="couldn't read this machine's"):
        dolby_to_easyeffects.main([str(_download(tmp_path)),
                                   "--skip-ee-check", "--dry-run"])


def test_a_download_whose_inf_has_no_apo_beside_it_is_refused(
        tmp_path, fake_host, silence_console):
    silence_console(console)
    _this_machine(fake_host)
    with pytest.raises(Exception, match="holds no Dolby or Sonitude tuning"):
        dolby_to_easyeffects.main([str(_download(tmp_path, with_apo=False)),
                                   "--skip-ee-check", "--dry-run"])


def test_a_dolby_xml_for_this_device_wins_over_dax2(
        tmp_path, fake_host, silence_console, capsys):
    silence_console(console)
    _this_machine(fake_host)
    xml = write_synthetic_tuning_xml(
        tmp_path / "DEV_0274_SUBSYS_10EC11B2_PCI_SUBSYS_72708086.xml")
    download = _download(tmp_path, {
        "SurfaceUpdate/dax3extrtk/" + xml.name: xml.read_bytes()})
    out, _ = _run(tmp_path, capsys, download, "--dry-run")
    assert "Found Dolby DAX2" not in out
    # The XML's build: its profile banner, not the DAX2 tuning line.
    assert "Profile: " in out and "(Dolby DAX2:" not in out


def test_a_windows_driver_store_falls_back_to_dax2(
        tmp_path, fake_host, silence_console, capsys):
    silence_console(console)
    _this_machine(fake_host)
    store = tmp_path / "Windows" / "System32" / "DriverStore" / "FileRepository"
    _package(store / "hdxsstm2.inf_amd64_0123456789abcdef")
    out, _ = _run(tmp_path, capsys, "--dry-run", "--windows",
                  str(tmp_path))
    assert "Found Dolby DAX2 (HDXSSTM2.inf, PresetX_SPK)" in out


def _mounted_windows(root: Path, **kw) -> Path:
    """A mounted Windows at *root* whose DriverStore holds a DAX2 package."""
    store = root / "Windows" / "System32" / "DriverStore" / "FileRepository"
    return _package(store / "hdxsstm2.inf_amd64_0123456789abcdef", **kw)


def test_a_mounted_windows_whose_dax2_binds_is_auto_detected(
        tmp_path, fake_host, silence_console, capsys, monkeypatch):
    from lib.dax import discover
    silence_console(console)
    _this_machine(fake_host)
    _mounted_windows(tmp_path / "c")
    monkeypatch.setattr(discover, "_ntfs_family_mountpoints",
                        lambda: [tmp_path / "c"])
    out, _ = _run(tmp_path, capsys, "--dry-run")
    assert f"Auto-detected Windows mount: {tmp_path / 'c'}" in out
    assert "Found Dolby DAX2 (HDXSSTM2.inf, PresetX_SPK)" in out


def test_another_devices_dax2_on_a_mount_leaves_the_cwd_tree_to_be_found(
        tmp_path, fake_host, silence_console, capsys, monkeypatch):
    from lib.dax import discover
    silence_console(console)
    _this_machine(fake_host)
    _mounted_windows(tmp_path / "c", subsys="10EC1234")
    monkeypatch.setattr(discover, "_ntfs_family_mountpoints",
                        lambda: [tmp_path / "c"])
    cwd = tmp_path / "cwd"
    (cwd / "tree").mkdir(parents=True)
    write_synthetic_tuning_xml(
        cwd / "tree" / "DEV_0274_SUBSYS_10EC11B2_PCI_SUBSYS_72708086.xml")
    monkeypatch.chdir(cwd)
    out, _ = _run(tmp_path, capsys, "--dry-run")
    assert "Auto-detected extracted DriverStore" in out
    assert "Windows mount" not in out and "Found Dolby DAX2" not in out


_WRAPPER = Path(dolby_to_easyeffects.__file__).parent / "dolby_to_pipewire.py"


def test_a_dax2_tuning_that_cant_be_read_gives_way_to_a_standalone_one(
        tmp_path, monkeypatch):
    import types
    from lib.apo import discover as apo_discover
    from lib.dax import discover
    monkeypatch.setattr(discover, "find_tuning_xml", lambda *a, **k: (
        _ for _ in ()).throw(FileNotFoundError("no XML")))

    def refuse(*a, **k):
        raise RuntimeError("binds this device's audio hardware to a Dolby "
                           "tuning this tool can't read")
    monkeypatch.setattr(dax2, "for_this_machine", refuse)
    fmt = types.SimpleNamespace(owner="Sonitude", prefix="Sonitude")
    monkeypatch.setattr(apo_discover, "standalone_in_driver_store",
                        lambda store: (fmt, [tmp_path / "snapo"]))
    args = types.SimpleNamespace(best_guess=False, prefix=None)
    source = dolby_to_easyeffects._windows_source(tmp_path, args)
    assert source.format is fmt and source.dax2 is None


def _wrapper(tmp_path, source, *flags):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if k not in ("XDG_DATA_HOME", "XDG_CONFIG_HOME")}
    return subprocess.run(
        [sys.executable, str(_WRAPPER), str(source),
         "--output-dir", str(tmp_path / "confs"),
         "--target-sink", "", "--no-activate", "--no-color", *flags],
        capture_output=True, text=True, timeout=120,
        env={**env, "HOME": str(home)})


def test_the_wrapper_converts_the_balanced_preset(tmp_path, fake_host):
    _this_machine(fake_host)
    result = _wrapper(tmp_path, _inf(tmp_path / "x.inf"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in (tmp_path / "confs").iterdir()) == [
        "Dolby_Balanced.conf", "Dolby_Balanced.irs"]


def test_the_wrapper_refuses_a_voicing_dax2_doesnt_carry(tmp_path, fake_host):
    _this_machine(fake_host)
    result = _wrapper(tmp_path, _inf(tmp_path / "x.inf"), "--variant", "warm")
    assert result.returncode == 1
    assert ("--variant warm isn't available: this tuning has one voicing "
            "curve, built as Balanced. Run the same command without "
            "--variant." in (
                result.stdout + result.stderr))


# Recorded from the build below when DAX2 input landed (2026-10-25). A
# change in behaviour re-records both in the same commit as its cause
# (.claude/rules/testing.md); a refactor leaves them as they are.
_PIN_PRESET_SHA = (
    "959a0c2422a81c4b2a1d27c30eea3b8e9a16060246c3e5770f97d645fe3a2491")
_PIN_DB = [-25.48, -26.0, -24.0, -16.84, -10.43, -4.23, -0.78, -3.17, -6.35,
           -10.47, -15.08, -15.2, -15.09, -14.99, -14.88, -14.77, -14.66,
           -14.54, -14.42, -14.3, -14.18, -14.06, -13.93, -13.83]


def test_the_dax2_build_is_pinned(tmp_path, fake_host, silence_console,
                                  capsys):
    import hashlib
    np = pytest.importorskip("numpy")
    wavfile = pytest.importorskip("scipy.io.wavfile")
    silence_console(console)
    _this_machine(fake_host)
    _, out_dir = _run(tmp_path, capsys, _inf(tmp_path / "x.inf"))
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
    db = np.interp(np.geomspace(30, 16000, 24), f,
                   20 * np.log10(np.abs(H) + 1e-15))
    assert np.allclose(db, _PIN_DB, atol=0.05)
