"""A vendor APO layer stacked on the Dolby preset: chain, gain, FIR and run.

`tests/test_apo_surface.py` covers reading the vendor file. This covers what
the run, and `--enable`/`--disable vendor-apo`, do with the `ApoLayer` it
yields. Every input is
synthetic.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

import dolby_to_easyeffects
from lib import console
from lib.apo import layer as apo_layer
from lib.pipewire.conf import _assert_positional
from lib.preset import fir
from lib.preset.build import make_preset
from lib.preset.plugins import make_band_dynamics
from lib.report import messages
from tests.conftest import (SYNTHETIC_FREQS_20, read_irs_file,
                            synthetic_apo_layer, synthetic_mb_comp,
                            synthetic_peq_filters, synthetic_regulator,
                            write_surface_package, write_synthetic_tuning_xml)


def _preset(apo, regulator=True, volmax_boost=0.0, **kw):
    preset, emitted = make_preset(
        kernel_name="Apo", peq_filters=synthetic_peq_filters([]),
        mb_comp=synthetic_mb_comp(2, [(10, -160, 16384, 30000, 32500, 0),
                                      (20, -160, 16384, 30000, 32500, 0)]),
        regulator=synthetic_regulator([-6.0] * 20) if regulator else None,
        freqs=SYNTHETIC_FREQS_20, volmax_boost=volmax_boost, apo=apo, **kw)
    return preset["output"], emitted


def test_layer_dynamics_follow_dolby_and_precede_the_limiter():
    out, emitted = _preset(synthetic_apo_layer(), enabled={"vendor-apo"})
    order = out["plugins_order"]
    assert order[-4:] == ["multiband_compressor#1", "multiband_compressor#2",
                          "multiband_compressor#3", "limiter#0"]
    assert "vendor-apo-active" in emitted and "vendor-apo" not in emitted


def test_found_but_off_offers_the_flag_and_adds_nothing():
    out, emitted = _preset(synthetic_apo_layer())
    assert "multiband_compressor#2" not in out
    assert "vendor-apo" in emitted


def test_default_on_layer_is_a_disable_candidate():
    """On by default it marks itself running, never as an --enable
    candidate; switched off by --disable it offers nothing, since the undo
    is removing the flag."""
    layer = synthetic_apo_layer(default_on=True)
    out, emitted = _preset(layer)
    assert "multiband_compressor#2" in out
    assert "vendor-apo-active" in emitted and "vendor-apo" not in emitted
    out, emitted = _preset(layer, disabled={"vendor-apo"})
    assert "multiband_compressor#2" not in out
    assert not {"vendor-apo", "vendor-apo-active"} & emitted


def test_default_on_layer_sits_in_the_disable_menu_only(silence_console,
                                                        capsys):
    silence_console(console)
    _, emitted = _preset(synthetic_apo_layer(default_on=True))
    messages.print_troubleshooting([], {k: [] for k in emitted})
    out = " ".join(capsys.readouterr().out.split())
    assert "--disable vendor-apo" in out
    assert "--enable vendor-apo" not in out


def test_volmax_moves_ahead_of_the_layer_when_no_regulator_carries_it():
    """On Windows Dolby's boost precedes the vendor EFX. With no regulator,
    the limiter would apply it after the layer's dynamics instead."""
    out, _ = _preset(synthetic_apo_layer(), regulator=False,
                     volmax_boost=6.0, enabled={"vendor-apo"})
    assert out["multiband_compressor#2"]["input-gain"] == 6.0
    assert out["multiband_compressor#3"]["input-gain"] == 0.0
    assert out["limiter#0"]["input-gain"] == 0.0
    out, _ = _preset(synthetic_apo_layer(), regulator=True,
                     volmax_boost=6.0, enabled={"vendor-apo"})
    assert out["multiband_compressor#1"]["input-gain"] == 6.0
    assert out["multiband_compressor#2"]["input-gain"] == 0.0


def test_band_dynamics_maps_pregain_sidechain_and_no_lookahead():
    drc, crystal = synthetic_apo_layer().dynamics
    d = make_band_dynamics(drc)
    assert d["band0"]["makeup"] == 2.0 and d["band0"]["sidechain-preamp"] == 2.0
    assert d["band1"]["split-frequency"] == 120.0
    assert d["band2"]["split-frequency"] == 800.0
    assert d["band2"]["compressor-enable"] is False
    assert d["band2"]["attack-threshold"] == 0.0  # parked at +48 dB
    assert d["band3"]["enable-band"] is False
    c = make_band_dynamics(crystal)
    assert c["band1"]["sidechain-custom-lowcut-filter"] is True
    assert (c["band1"]["sidechain-lowcut-frequency"],
            c["band1"]["sidechain-highcut-frequency"]) == (150.0, 250.0)
    assert c["band1"]["sidechain-mode"] == "Peak"
    assert c["band1"]["ratio"] == 100.0
    for stage in (d, c):
        assert all(stage[f"band{i}"]["sidechain-lookahead"] == 0.0
                   for i in range(8))


def test_layer_stages_must_follow_the_regulator():
    with pytest.raises(ValueError, match="vendor APO"):
        _assert_positional(["multiband_compressor#2",
                            "multiband_compressor#1"])
    _assert_positional(["multiband_compressor#0", "multiband_compressor#1",
                        "multiband_compressor#2", "multiband_compressor#3"])


def _run(tmp_path, capsys, *flags):
    xml = write_surface_package(tmp_path / "pkg")
    write_synthetic_tuning_xml(xml)
    out_dir, irs_dir = tmp_path / "out", tmp_path / "irs"
    dolby_to_easyeffects.main([
        str(xml), "--skip-ee-check", "--skip-closing", "-v",
        "--output-dir", str(out_dir), "--irs-dir", str(irs_dir), *flags])
    return " ".join(capsys.readouterr().out.split()), out_dir, irs_dir


def test_disable_leaves_a_default_on_layer_out(tmp_path, silence_console,
                                              capsys):
    silence_console(console)
    out, out_dir, _ = _run(tmp_path, capsys, "--disable", "vendor-apo")
    assert "Vendor speaker tuning: Synthetic" not in out  # real label below
    assert "Vendor speaker tuning: Microsoft Surface APO" in out
    assert "Not applied: --disable vendor-apo left it out." in out
    assert "--enable vendor-apo" not in out
    presets = sorted(out_dir.glob("*.json"))
    assert presets
    for p in presets:
        assert "multiband_compressor#2" not in json.loads(p.read_text())[
            "output"]


@pytest.mark.parametrize("flags", [(), ("--enable", "vendor-apo")])
def test_run_folds_the_eq_and_adds_the_stages(tmp_path, silence_console,
                                              capsys, flags):
    """On by default; --enable, which older command lines carry, changes
    nothing."""
    silence_console(console)
    out, out_dir, irs_dir = _run(tmp_path, capsys, *flags)
    assert "Microsoft Surface APO" in out and ", applied" in out
    assert "--disable vendor-apo leaves it out." in out
    # The menu offers the off-switch even to an old --enable command line.
    assert "--disable vendor-apo #" in out
    assert "IEQ+AO+APO" in out
    assert "Correction check passed" in out
    assert "[vendor-apo-not-reproduced] Nothing to do." in out
    assert "is in the presets" not in out
    presets = sorted(out_dir.glob("*.json"))
    assert presets
    for p in presets:
        order = json.loads(p.read_text())["output"]["plugins_order"]
        assert "multiband_compressor#2" in order, p.name
    # The synthetic EQ's 60 Hz high-pass is in the impulse response.
    taps = read_irs_file(sorted(irs_dir.glob("*.irs"))[0])[3]
    f = np.array([30.0, 1000.0])
    h = np.abs(np.fft.rfft(taps, n=8 * fir.FIR_LENGTH))
    bins = np.round(f / (fir.SAMPLE_RATE / (8 * fir.FIR_LENGTH))).astype(int)
    db = 20 * np.log10(h[bins])
    assert db[0] - db[1] < -9.0


@pytest.mark.parametrize("flag", ["--enable", "--disable"])
def test_a_flag_without_a_layer_says_it_did_nothing(tmp_path, flag,
                                                    silence_console, capsys):
    silence_console(console)
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    dolby_to_easyeffects.main([str(xml), "--dry-run", "--skip-ee-check",
                               "--skip-closing", flag, "vendor-apo"])
    out = " ".join(capsys.readouterr().out.split())
    assert f"{flag} {apo_layer.FLAG} had no effect" in out


def test_an_unusable_config_is_reported_once(tmp_path, silence_console,
                                             capsys):
    """A config that binds but can't be read says so where it is found; the
    closing "had no effect" line would contradict it."""
    silence_console(console)
    xml = write_surface_package(tmp_path / "pkg")
    write_synthetic_tuning_xml(xml)
    (xml.parent.parent / "surfaceapoextension" / "SurfaceAPO_TEST.json"
     ).write_text("{ torn")
    dolby_to_easyeffects.main([str(xml), "--dry-run", "--skip-ee-check",
                               "--skip-closing", "--enable", "vendor-apo"])
    out = " ".join(capsys.readouterr().out.split())
    assert "binds this device but can't be used" in out
    assert "had no effect" not in out


def test_closing_confirms_the_layer_took_effect(tmp_path, silence_console,
                                                capsys):
    """The report section saying "applied" has scrolled off by the end; the
    closing block is where a Done-stopper learns the flag worked."""
    silence_console(console)
    xml = write_surface_package(tmp_path / "pkg")
    write_synthetic_tuning_xml(xml)
    dolby_to_easyeffects.main([str(xml), "--dry-run", "--skip-ee-check"])
    out = " ".join(capsys.readouterr().out.split())
    assert ("These would include the Microsoft Surface APO speaker tuning, "
            "on by default (--disable vendor-apo leaves it out)") in out


def test_closing_names_an_opt_in_layer_left_out(silence_console, capsys):
    """Off, the hint's ask prints above the last screen; the Done block is
    what a reader who never scrolls sees (vendor-apo review, round 2)."""
    silence_console(console)
    messages.print_what_now(["Dolby-Balanced"], False, True,
                            vendor_apo_off="Synthetic APO")
    out = " ".join(capsys.readouterr().out.split())
    assert ("Not included: the Synthetic APO speaker tuning found "
            "beside the Dolby file — --enable vendor-apo adds it") in out


def test_closing_says_nothing_of_a_layer_the_reader_disabled(
        tmp_path, silence_console, capsys):
    silence_console(console)
    xml = write_surface_package(tmp_path / "pkg")
    write_synthetic_tuning_xml(xml)
    dolby_to_easyeffects.main([str(xml), "--dry-run", "--skip-ee-check",
                               "--disable", "vendor-apo"])
    out = " ".join(capsys.readouterr().out.split())
    assert "Not included" not in out
    assert "These would include the Microsoft" not in out


def test_a_stage_the_layer_could_not_build_sits_with_the_stages(
        silence_console, capsys):
    """Not only inside the dim left-out list, where it read like a detail."""
    import dataclasses

    from lib.report import profile
    silence_console(console)
    apo = dataclasses.replace(
        synthetic_apo_layer(),
        skipped=(("Resonance limiter", "its 7 resonances are too many"),))
    profile._print_apo_layer(apo, True, False)
    out = " ".join(capsys.readouterr().out.split())
    assert "Resonance limiter: not applied, since its 7 resonances" in out
    assert out.index("not applied") < out.index("[vendor-apo-not-reproduced]")


def test_all_profiles_prints_the_vendor_section_once(tmp_path,
                                                     silence_console, capsys):
    """The layer is the device's: one section, not one per profile."""
    import re
    silence_console(console)
    xml = write_surface_package(tmp_path / "pkg")
    write_synthetic_tuning_xml(xml)
    text = xml.read_text()
    dynamic = re.search(r'<profile type="dynamic">.*?</profile>', text,
                        re.S).group(0)
    xml.write_text(text.replace(dynamic, dynamic + dynamic.replace(
        '"dynamic"', '"music"')))
    dolby_to_easyeffects.main([
        str(xml), "--skip-ee-check", "--skip-closing", "--dry-run",
        "--enable", "vendor-apo", "--all-profiles"])
    out = " ".join(capsys.readouterr().out.split())
    assert "Music" in out  # both profiles ran
    assert out.count("Vendor speaker tuning:") == 1
    assert "[vendor-apo-not-reproduced]" in out
