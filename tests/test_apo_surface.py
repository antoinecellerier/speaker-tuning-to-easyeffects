"""The Surface APO finder and config reader (`lib/apo/surface.py`).

Every package here is synthetic (`tests/conftest.py`
`write_surface_package`). The binding, the de-interleaving and the
dynamics mapping are what's under test, not Microsoft's values.
"""

from __future__ import annotations

import json

import pytest

from lib.apo import discover, layer, surface
from tests.conftest import (_apo_block, surface_apo_json,
                            synthetic_surface_eq, write_surface_package)


def _efx(doc):
    return doc["entities"][0]["children"][0]["children"]


def _set(doc, block, **params):
    """Set *params* on the R/EFX block named *block*."""
    children = next(b for b in _efx(doc) if b["name"] == block)["children"]
    for name, value in params.items():
        next(p for p in children if p["name"] == name)["value"] = value


@pytest.mark.parametrize("layout", ["msi", "driverstore"])
@pytest.mark.parametrize("utf16", [False, True])
def test_finds_the_config_its_inf_binds(tmp_path, layout, utf16):
    xml = write_surface_package(tmp_path, layout=layout, utf16=utf16)
    apo = discover.find_for_xml(xml)
    assert apo is not None
    assert apo.config_path.name == "SurfaceAPO_TEST.json"
    assert "DEV_0274&SUBSYS_10EC1284" in apo.hardware_id
    assert apo.label == "Microsoft Surface APO" and apo.default_on


def test_binding_is_read_from_the_inf_not_the_filename(tmp_path):
    """A package whose .inf binds another device is ignored, even when its
    config is named after this one."""
    xml = write_surface_package(tmp_path, inf_subsys="10EC1282",
                                config="SurfaceAPO_1284.json")
    assert discover.find_for_xml(xml) is None


def test_a_wrong_device_package_beside_the_right_one_is_ignored(tmp_path):
    xml = write_surface_package(tmp_path, layout="driverstore",
                                inf_subsys="10EC1282",
                                config="SurfaceAPO_OTHER.json",
                                package_dir="surfaceapoextension.inf_amd64_a")
    write_surface_package(tmp_path, layout="driverstore",
                          package_dir="surfaceapoextension.inf_amd64_b")
    assert discover.find_for_xml(xml).config_path.name == \
        "SurfaceAPO_TEST.json"


def test_the_newest_binding_package_wins(tmp_path):
    xml = write_surface_package(tmp_path, layout="driverstore",
                                driver_ver="1.9.0.0", config="old.json",
                                package_dir="surfaceapoextension.inf_amd64_a")
    write_surface_package(tmp_path, layout="driverstore",
                          driver_ver="1.10.0.0", config="new.json",
                          package_dir="surfaceapoextension.inf_amd64_b")
    assert discover.find_for_xml(xml).config_path.name == "new.json"


SDW_XML = "SOUNDWIRE_MAN_025D_FUNC_1320_SUBSYS_307210EC.xml"
SDW_HWID = ("SOUNDWIRE\\SDCA_10&MAN_025D&FUNC_1320&TYPE_01&VER_01&ADR_04"
            "&LID_00&UID_00&SUBSYS_307210EC&DynamicEnumSpeaker0")


def test_a_soundwire_xml_binds_by_manufacturer_function_and_subsys(tmp_path):
    xml = write_surface_package(tmp_path / "a", xml_name=SDW_XML,
                                hwid=SDW_HWID)
    assert discover.find_for_xml(xml).hardware_id == SDW_HWID
    other_func = SDW_HWID.replace("FUNC_1320", "FUNC_1318")
    xml = write_surface_package(tmp_path / "b", xml_name=SDW_XML,
                                hwid=other_func)
    assert discover.find_for_xml(xml) is None


def test_a_config_set_through_an_interface_section_binds(tmp_path):
    xml = write_surface_package(tmp_path, xml_name=SDW_XML, hwid=SDW_HWID,
                                via_interfaces=True)
    assert discover.find_for_xml(xml).config_path.name == \
        "SurfaceAPO_TEST.json"


def test_the_package_is_found_by_its_inf_whatever_its_folder(tmp_path):
    """Each MSI names the folder differently; the .inf name is what holds."""
    xml = write_surface_package(tmp_path, package_dir="APOExtension")
    assert discover.find_for_xml(xml) is not None


def test_a_driverstore_folder_named_for_another_inf_is_not_read(tmp_path):
    xml = write_surface_package(tmp_path, layout="driverstore",
                                package_dir="realtekapo.inf_amd64_a")
    assert discover.find_for_xml(xml) is None


def test_no_package_and_soundwire_find_nothing(tmp_path):
    xml = tmp_path / "DEV_0274_SUBSYS_10EC1284_PCI_SUBSYS_72708086.xml"
    xml.write_text("<device_data/>")
    assert discover.find_for_xml(xml) is None
    sdw = tmp_path / "SOUNDWIRE_MAN_025D_FUNC_1320_SUBSYS_307010EC.xml"
    sdw.write_text("<device_data/>")
    assert discover.find_for_xml(sdw) is None


def test_main_eq_is_deinterleaved_and_identities_dropped(tmp_path):
    left = synthetic_surface_eq()
    right = [left[0], (1.1, -1.5, 0.6, -1.4, 0.5)]
    coeffs = []
    for lsec, rsec in zip(left + [(1.0, 0.0, 0.0, 0.0, 0.0)],
                          right + [(1.0, 0.0, 0.0, 0.0, 0.0)]):
        coeffs += [*lsec, *rsec]
    doc = surface_apo_json()
    efx = doc["entities"][0]["children"][0]["children"]
    main_eq = next(b for b in efx if b["name"] == "MainEQ")
    next(p for p in main_eq["children"]
         if p["name"] == "Coefficients")["value"] = coeffs
    apo = discover.find_for_xml(
        write_surface_package(tmp_path, config_json=doc))
    assert apo.eq_left == tuple(left)
    assert apo.eq_right == tuple(right)


def test_main_eq_blocks_cascade_in_file_order(tmp_path):
    first, second = synthetic_surface_eq()
    doc = surface_apo_json(sections=[first])
    efx = _efx(doc)
    at = next(i for i, b in enumerate(efx) if b["name"] == "MainEQ")
    efx[at]["name"] = "MainEQ1"
    efx.insert(at + 1, _apo_block("MainEQ2", 48000, Enabled=[True],
                                  Coefficients=[*second, *second]))
    apo = discover.find_for_xml(write_surface_package(tmp_path,
                                                      config_json=doc))
    assert apo.eq_left == apo.eq_right == (first, second)


def test_a_config_with_no_endpoint_chain_binds_no_layer(tmp_path):
    """Only per-mode EQs, which are not a source: no layer, no warning."""
    doc = surface_apo_json()
    doc["entities"][0]["children"][0]["name"] = "R/MFX/DEFAULT"
    xml = write_surface_package(tmp_path, config_json=doc)
    assert discover.find_for_xml(xml) is None


def test_a_chain_with_nothing_to_build_binds_no_layer(tmp_path):
    """Identity EQ and no dynamics: a layer on by default would otherwise
    report an empty stage as applied."""
    doc = surface_apo_json(sections=[])
    efx = doc["entities"][0]["children"][0]["children"]
    efx[:] = [b for b in efx if b["name"] not in ("VolumeDepMBDRC4",
                                                 "Crystal")]
    xml = write_surface_package(tmp_path, config_json=doc)
    assert discover.find_for_xml(xml) is None


def test_a_44k1_only_config_is_reported_not_skipped(tmp_path):
    xml = write_surface_package(tmp_path,
                                config_json=surface_apo_json(fs=44100))
    with pytest.raises(layer.UnsupportedApoConfig, match="48 kHz"):
        discover.find_for_xml(xml)


def test_a_missing_config_is_reported(tmp_path):
    xml = write_surface_package(tmp_path)
    (xml.parent.parent / "surfaceapoextension" / "SurfaceAPO_TEST.json"
     ).unlink()
    with pytest.raises(layer.UnsupportedApoConfig, match="missing"):
        discover.find_for_xml(xml)


def test_a_torn_main_eq_is_reported(tmp_path):
    doc = surface_apo_json()
    efx = doc["entities"][0]["children"][0]["children"]
    main_eq = next(b for b in efx if b["name"] == "MainEQ")
    next(p for p in main_eq["children"]
         if p["name"] == "Coefficients")["value"] = [1.0] * 7
    with pytest.raises(layer.UnsupportedApoConfig, match="MainEQ"):
        discover.find_for_xml(write_surface_package(tmp_path,
                                                    config_json=doc))


def test_drc_maps_pregain_and_merges_inert_bands(tmp_path):
    apo = discover.find_for_xml(write_surface_package(tmp_path))
    drc = next(d for d in apo.dynamics if d.name == "drc")
    # Bands 3 and 4 are ratio 1 with no pregain: one inert band above 800.
    assert drc.crossovers_hz == (120.0, 800.0)
    assert [b.enabled for b in drc.bands] == [True, True, False]
    assert drc.bands[0].threshold_db == -10.0
    assert drc.bands[0].ratio == 3.0
    assert drc.bands[0].pregain_db == 2.0
    assert drc.bands[0].attack_ms == 2.0
    assert drc.bands[0].release_ms == 30.0


def test_drc_uses_the_full_volume_state_and_says_when_states_differ(tmp_path):
    doc = surface_apo_json()
    efx = doc["entities"][0]["children"][0]["children"]
    drc_block = next(b for b in efx if b["name"] == "VolumeDepMBDRC4")
    thr = next(p for p in drc_block["children"] if p["name"] == "ThresholdDb")
    thr["value"] = [-10.0, -8.0, 0.0, 0.0] + [-20.0, -8.0, 0.0, 0.0] * 9
    apo = discover.find_for_xml(write_surface_package(tmp_path,
                                                      config_json=doc))
    drc = next(d for d in apo.dynamics if d.name == "drc")
    assert drc.bands[0].threshold_db == -10.0
    assert any("state 0, read as full volume, is used" in n for n in apo.notes)


def test_an_n_band_drc_drops_its_inaudible_bands(tmp_path):
    """The Surface Pro 8 shape: 8 bands, five split off above 20 kHz."""
    doc = surface_apo_json()
    efx = _efx(doc)
    efx[:] = [b for b in efx if b["name"] != "VolumeDepMBDRC4"]
    row = [-3.0, -6.0, -9.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    efx.insert(3, _apo_block(
        "VolumeDepMBDRC", 48000, Enabled=[True],
        CrossoverFreqs=[500.0, 4000.0, 22600.0, 22700.0, 22800.0, 22900.0,
                        23000.0],
        ThresholdDb=row * 10, Ratio=[2.0] * 8 * 10, PreGainDb=[0.0] * 80,
        OutputLimit=[0.0] * 80, AttackTimeMs=[1.0] * 8,
        HoldTimeMs=[0.0] * 8, ReleaseTimeMs=[50.0] * 8))
    apo = discover.find_for_xml(write_surface_package(tmp_path,
                                                      config_json=doc))
    drc = next(d for d in apo.dynamics if d.name == "drc")
    assert drc.crossovers_hz == (500.0, 4000.0)
    assert [b.threshold_db for b in drc.bands] == [-3.0, -6.0, -9.0]


def test_crystal_blocks_merge_into_one_sorted_limiter(tmp_path):
    """Crystal1 + Crystal2 (the Surface Laptop 6 shape): each block limits
    only its ActiveLimiterCount entries, and the merged set sorts by F0."""
    doc = surface_apo_json()
    efx = _efx(doc)
    at = next(i for i, b in enumerate(efx) if b["name"] == "Crystal")
    efx[at] = _apo_block(
        "Crystal1", 48000, Enabled=[True], F0=[500.0, 900.0],
        Bandwidth=[200.0, 200.0], Limit=[-20.0, -5.0],
        AttackTimeMs=[1.0, 1.0], HoldTimeMs=[0.0, 0.0],
        ReleaseTimeMs=[40.0, 40.0], ActiveLimiterCount=[1])
    efx.insert(at + 1, _apo_block(
        "Crystal2", 48000, Enabled=[True], F0=[200.0], Bandwidth=[100.0],
        Limit=[-10.0], AttackTimeMs=[1.0], HoldTimeMs=[0.0],
        ReleaseTimeMs=[40.0]))
    apo = discover.find_for_xml(write_surface_package(tmp_path,
                                                      config_json=doc))
    crystal = next(d for d in apo.dynamics if d.name == "crystal")
    assert crystal.crossovers_hz == (150.0, 316.2, 600.0)
    assert [b.threshold_db for b in crystal.bands[1:3]] == [-10.0, -20.0]


def test_identical_crystal_entries_collapse(tmp_path):
    """The Surface Pro 8 lists each resonance twice in a row."""
    doc = surface_apo_json()
    _set(doc, "Crystal", F0=[500.0, 500.0, 200.0, 200.0],
         Bandwidth=[200.0, 200.0, 100.0, 100.0],
         Limit=[-20.0, -20.0, -10.0, -10.0], AttackTimeMs=[1.0] * 4,
         HoldTimeMs=[0.0] * 4, ReleaseTimeMs=[40.0] * 4)
    apo = discover.find_for_xml(write_surface_package(tmp_path,
                                                      config_json=doc))
    crystal = next(d for d in apo.dynamics if d.name == "crystal")
    assert crystal.crossovers_hz == (150.0, 316.2, 600.0)


def test_more_resonances_than_bands_are_listed_not_fatal(tmp_path):
    doc = surface_apo_json()
    f0 = [100.0 * (i + 1) for i in range(7)]
    _set(doc, "Crystal", F0=f0, Bandwidth=[50.0] * 7, Limit=[-10.0] * 7,
         AttackTimeMs=[1.0] * 7, HoldTimeMs=[0.0] * 7,
         ReleaseTimeMs=[40.0] * 7)
    apo = discover.find_for_xml(write_surface_package(tmp_path,
                                                      config_json=doc))
    assert [d.name for d in apo.dynamics] == ["drc"]
    assert apo.skipped == (("Resonance limiter", "its 7 resonances are more "
                            "than one limiter stage can split apart "
                            "(Crystal)"),)


def test_resonances_sharing_a_frequency_say_so(tmp_path):
    doc = surface_apo_json()
    _set(doc, "Crystal", F0=[200.0, 200.0], Bandwidth=[100.0, 50.0],
         Limit=[-10.0, -20.0], AttackTimeMs=[1.0, 1.0],
         HoldTimeMs=[0.0, 0.0], ReleaseTimeMs=[40.0, 40.0])
    apo = discover.find_for_xml(write_surface_package(tmp_path,
                                                      config_json=doc))
    assert [d.name for d in apo.dynamics] == ["drc"]
    assert "share a frequency" in apo.skipped[0][1]


def test_crystal_bands_bracket_each_resonance(tmp_path):
    apo = discover.find_for_xml(write_surface_package(tmp_path))
    crystal = next(d for d in apo.dynamics if d.name == "crystal")
    # F0 200 (BW 100) and 500 (BW 200): inert below 150, split at the
    # geometric midpoint sqrt(200·500), inert above 600.
    assert crystal.crossovers_hz == (150.0, 316.2, 600.0)
    assert [b.enabled for b in crystal.bands] == [False, True, True, False]
    assert crystal.bands[1].sidechain_hz == (150.0, 250.0)
    assert crystal.bands[2].sidechain_hz == (400.0, 600.0)
    assert [b.threshold_db for b in crystal.bands[1:3]] == [-10.0, -20.0]
    assert crystal.detection == "Peak"


def test_unreproduced_blocks_are_listed(tmp_path):
    apo = discover.find_for_xml(write_surface_package(tmp_path))
    text = "\n".join(apo.notes)
    assert "(VolumeDepLS)" in text
    assert "look-ahead" in text
    assert "(VolumeDepMBDRC4 hold times)" in text  # Crystal holds 0 ms here


def test_an_unknown_block_is_listed_and_an_identity_one_is_not(tmp_path):
    doc = surface_apo_json()
    _set(doc, "VolumeDepLS", Coefficients=[1.0, 0.0, 0.0, 0.0, 0.0] * 20)
    _efx(doc).append(_apo_block("Widener", 48000, Enabled=[True]))
    apo = discover.find_for_xml(write_surface_package(tmp_path,
                                                      config_json=doc))
    text = "\n".join(apo.notes)
    assert "(Widener)" in text
    assert "VolumeDepLS" not in text


def test_is_active_follows_the_flags():
    apo = layer.ApoLayer(label="", config_path=None, inf_path=None,
                         hardware_id="", sample_rate=48000, eq_left=(),
                         eq_right=(), dynamics=(), notes=())
    flag = {layer.FLAG}
    assert layer.FLAG == "vendor-apo"
    assert not layer.is_active(None, flag, set())
    assert not layer.is_active(apo, set(), set())
    assert layer.is_active(apo, flag, set())
    on = layer.ApoLayer(**{**apo.__dict__, "default_on": True})
    assert layer.is_active(on, set(), set())
    assert not layer.is_active(on, set(), flag)


def test_config_json_round_trips_through_the_helper(tmp_path):
    """Guards the synthetic builder itself: what it writes is JSON."""
    xml = write_surface_package(tmp_path)
    cfg = xml.parent.parent / "surfaceapoextension" / "SurfaceAPO_TEST.json"
    assert json.loads(cfg.read_text())["entities"][0]["name"] == \
        "InitialValueStore"


def test_a_config_without_its_value_store_is_reported(tmp_path):
    """A JSON missing InitialValueStore must surface as unusable, not crash
    the run."""
    xml = write_surface_package(tmp_path, config_json={"entities": []})
    with pytest.raises(layer.UnsupportedApoConfig, match="don't parse"):
        discover.find_for_xml(xml)


def test_an_active_output_limiter_is_listed_as_replaced(tmp_path):
    doc = surface_apo_json()
    efx = doc["entities"][0]["children"][0]["children"]
    lim = next(b for b in efx if b["name"] == "OutputLimiter")
    next(p for p in lim["children"] if p["name"] == "ThresholdDb")[
        "value"] = [-3.0]
    apo = discover.find_for_xml(write_surface_package(tmp_path,
                                                      config_json=doc))
    assert any("preset's own limiter replaces" in n for n in apo.notes)


def test_a_block_missing_a_field_is_reported_not_raised(tmp_path):
    """Discovery runs on every run, flag or not: an unexpected shape anywhere
    in the config must surface as unusable, never as a crash."""
    doc = surface_apo_json()
    drc = next(b for b in _efx(doc) if b["name"] == "VolumeDepMBDRC4")
    drc["children"] = [p for p in drc["children"]
                       if p["name"] != "CrossoverFreqs"]
    xml = write_surface_package(tmp_path, config_json=doc)
    with pytest.raises(layer.UnsupportedApoConfig, match="don't parse"):
        discover.find_for_xml(xml)


def test_an_unreadable_inf_binds_nothing(tmp_path):
    xml = write_surface_package(tmp_path)
    inf = xml.parent.parent / "surfaceapoextension" / "SurfaceAPOExtension.inf"
    inf.write_bytes(b"\xff\xfe;")  # UTF-16 with an odd byte count
    assert discover.find_for_xml(xml) is None


def test_a_bare_relative_xml_path_finds_its_package(tmp_path, monkeypatch):
    xml = write_surface_package(tmp_path)
    monkeypatch.chdir(xml.parent)
    assert discover.find_for_xml(xml.name) is not None


def test_the_bound_config_name_matches_case_insensitively(tmp_path):
    """The Surface Pro 12 .inf spells its config SurfaceAPO_308c.json."""
    xml = write_surface_package(tmp_path, config="SurfaceAPO_308c.json")
    cfg = xml.parent.parent / "surfaceapoextension" / "SurfaceAPO_308c.json"
    cfg.rename(cfg.with_name("SurfaceAPO_308C.json"))
    assert discover.find_for_xml(xml).config_path.name == \
        "SurfaceAPO_308C.json"
