"""Smoke tests for tools/corpus_audit.py — the cross-device sweep tool.

Guards XML discovery (name filter, `_settings.xml` exclusion), the
endpoint/profile row extraction, and the corpus-root precedence
(CLI > ATMOS_CORPUS_DIR > cwd). Uses a synthetic, hand-built XML — no real
DAX3 tuning data.
"""
import importlib.util
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "corpus_audit", _REPO / "tools" / "corpus_audit.py"
)
corpus_audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(corpus_audit)


_SYNTHETIC_XML = """<device_data>
  <endpoint type="internal_speaker" operating_mode="normal" fs="48000">
    <profile type="dynamic">
      <tuning-cp>
        <ieq-enable value="1"/>
        <ieq-amount value="10"/>
        <ieq-bands-set preset="ieq_balanced"/>
        <volmax-boost value="96"/>
      </tuning-cp>
      <tuning-vlldp>
        <mb-compressor-enable value="1"/>
        <mb-compressor-tuning>
          <group_count value="2"/>
          <band_group_0 value="3,-103,19639,24080,32123,32"/>
        </mb-compressor-tuning>
      </tuning-vlldp>
    </profile>
  </endpoint>
</device_data>"""


def test_is_dax3_xml_name_filter():
    assert corpus_audit.is_dax3_xml("DEV_0287_SUBSYS_17AA22E6.xml")
    assert corpus_audit.is_dax3_xml("SOUNDWIRE_MAN_025D_FUNC_1318_SUBSYS_233917AA.xml")
    # The same tunings under the other hardware-ID namespaces their .inf binds
    # them under — shipped that way by the package, not renamed by Setup
    # (cross-device-findings.md §17). The sweep used to test for a leading
    # DEV_/SOUNDWIRE/SDW and skipped all of these, undercounting the corpus by
    # a few hundred real speaker tunings.
    assert corpus_audit.is_dax3_xml("HDAUDIO_DEV_0257_SUBSYS_17AA3801.xml")
    assert corpus_audit.is_dax3_xml("INTELAUDIO_DEV_0274_SUBSYS_17AA3801.xml")
    assert corpus_audit.is_dax3_xml("AUCD_DEV_0C29_SUBSYS_233817AA_ADCM_SUBSYS_233817AA.xml")
    # Companions that share the shape but hold no playback tuning.
    assert not corpus_audit.is_dax3_xml("DEV_0287_SUBSYS_17AA22E6_settings.xml")
    assert not corpus_audit.is_dax3_xml("DEV_0287_SUBSYS_17AA22E6_dmic.xml")
    assert not corpus_audit.is_dax3_xml("SOUNDWIRE_MAN_025D_SUBSYS_233917AA_amic.xml")
    assert not corpus_audit.is_dax3_xml("readme.txt")
    assert not corpus_audit.is_dax3_xml("random.xml")


def test_find_xmls_excludes_companions(tmp_path):
    (tmp_path / "DEV_0287_SUBSYS_17AA22E6.xml").write_text(_SYNTHETIC_XML)
    (tmp_path / "DEV_0287_SUBSYS_17AA22E6_settings.xml").write_text("<x/>")
    (tmp_path / "DEV_0287_SUBSYS_17AA22E6_dmic.xml").write_text("<x/>")
    found = corpus_audit.find_xmls([str(tmp_path)])
    assert len(found) == 1
    assert found[0].endswith("DEV_0287_SUBSYS_17AA22E6.xml")


def test_codec_of_reads_through_bus_prefixes():
    c = corpus_audit.codec_of
    assert c("DEV_0287_SUBSYS_17AA22E6.xml") == "DEV_0287"
    assert c("HDAUDIO_DEV_0257_SUBSYS_17AA3801.xml") == "DEV_0257"
    assert c("INTELAUDIO_DEV_0274_SUBSYS_17AA3801.xml") == "DEV_0274"
    assert c("PCI_DEV_1803_SUBSYS_1880106B.xml") == "DEV_1803"
    assert c("AUCD_DEV_0C29_SUBSYS_233817AA.xml") == "DEV_0C29"
    assert c("SOUNDWIRE_MAN_025D_FUNC_1318_SUBSYS_233917AA.xml") == "SOUNDWIRE"
    assert c("SDW_MAN_025D_SUBSYS_233917AA.xml") == "SDW"


def test_package_of_matches_any_package_prefix():
    p = corpus_audit.package_of
    # A prefix match, so packages absent from any hardcoded list still bucket.
    assert p("/x/ext_ideapad_AIO_senary_21h2_22h2_v8.920.549.59/a.xml") == (
        "ext_ideapad_AIO_senary_21h2_22h2_v8.920.549.59")
    assert p("/x/dax3_ext_rtk.inf_amd64_deadbeef/a.xml") == (
        "dax3_ext_rtk.inf_amd64_deadbeef")
    # "extracted" starts with ext but not ext_ — it is not a package name.
    assert p("/x/extracted/dolby-xmls/a.xml") == "OTHER"


def test_package_of_falls_back_to_an_inf_beside_the_tunings(tmp_path):
    """Packages that ship flat, with the .inf next to the XMLs rather than in
    a directory named after them — Samsung's Cirrus SoundWire drop."""
    d = tmp_path / "APO" / "Dolby"
    d.mkdir(parents=True)
    xml = d / "SOUNDWIRE_MAN_01FA_FUNC_3556_SUBSYS_F020144D.xml"
    xml.write_text(_SYNTHETIC_XML)
    (d / "dax3_swc_aposvc.inf").write_text("")  # not a tuning package
    assert corpus_audit.package_of(str(xml)) == "OTHER"
    (d / "dax3_ext_cirrus.inf").write_text("")
    corpus_audit._package_inf_in.cache_clear()
    assert corpus_audit.package_of(str(xml)) == "dax3_ext_cirrus"


def test_composition_prints_makeup_and_stops(tmp_path, capsys):
    (tmp_path / "DEV_0287_SUBSYS_17AA22E6.xml").write_text(_SYNTHETIC_XML)
    assert corpus_audit.main(["--composition", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("1 tuning XMLs total")
    assert "1 content-unique" in out
    assert "1 distinct SUBSYS device ids" in out
    assert "1 profile rows total" in out
    # --composition stops before the per-parameter sweep.
    assert "Universal-constant checks" not in out


def test_analyse_extracts_profile_row(tmp_path):
    p = tmp_path / "DEV_0287_SUBSYS_17AA22E6.xml"
    p.write_text(_SYNTHETIC_XML)
    rows = corpus_audit.analyse(str(p))
    assert len(rows) == 1
    row = rows[0]
    assert row["codec"] == "DEV_0287"
    assert row["profile"] == "dynamic"
    assert row["operating_mode"] == "normal"
    assert row["ieq_amount"] == 10
    assert row["ieq_preset"] == "ieq_balanced"
    assert row["volmax_boost"] == 96
    assert row["mbc_enable"] == 1
    assert row["group_count"] == 2


def test_discover_roots_precedence(monkeypatch):
    monkeypatch.setenv("ATMOS_CORPUS_DIR", "/env/path")
    assert corpus_audit.discover_roots(["/cli/a", "/cli/b"]) == ["/cli/a", "/cli/b"]
    assert corpus_audit.discover_roots([]) == ["/env/path"]
    monkeypatch.delenv("ATMOS_CORPUS_DIR", raising=False)
    assert corpus_audit.discover_roots([]) == ["."]


def test_subsys_of():
    assert corpus_audit.subsys_of("SOUNDWIRE_MAN_025D_FUNC_0721_SUBSYS_37A317AA.xml") == "37A317AA"
    assert corpus_audit.subsys_of("DEV_0287_SUBSYS_17AA22E6.xml") == "17AA22E6"
    assert corpus_audit.subsys_of("nosubsys.xml") == "nosubsys.xml"


def test_threshold_schema_classification():
    import xml.etree.ElementTree as ET
    ts = corpus_audit.threshold_schema
    assert ts(None) is None
    assert ts(ET.fromstring('<threshold_high value="-96,-80"/>')) == "direct"
    assert ts(ET.fromstring('<threshold_high preset="arr"/>')) == "direct"
    assert ts(ET.fromstring('<threshold_high/>')) == "empty"
    # newer SoundWire per-channel schema, real values → the dropped-before-fix bucket
    assert ts(ET.fromstring(
        '<threshold_high><ch_00 value="-282,-294,0,0"/></threshold_high>')) == "ch_nonzero"
    assert ts(ET.fromstring(
        '<threshold_high><ch_00 value="0,0,0,0"/></threshold_high>')) == "ch_zero"
    assert ts(ET.fromstring(
        '<threshold_high><ch_00 preset="array_20_zero"/></threshold_high>')) == "ch_preset"


def test_peq_effective_boost():
    import xml.etree.ElementTree as ET
    b = corpus_audit.peq_effective_boost
    # bell: gain * min(1, 2/q)
    assert b(ET.fromstring('<filter speaker="0" type="1" gain="6" q="2"/>')) == 6.0
    assert b(ET.fromstring('<filter speaker="0" type="1" gain="8" q="4"/>')) == 4.0
    # shelf: full gain
    assert b(ET.fromstring('<filter speaker="0" type="3" gain="3"/>')) == 3.0
    # cuts / HP / disabled contribute nothing
    assert b(ET.fromstring('<filter speaker="0" type="1" gain="-5" q="1"/>')) == 0.0
    assert b(ET.fromstring('<filter speaker="0" type="7" order="4"/>')) == 0.0
    assert b(ET.fromstring('<filter speaker="0" type="1" gain="6" q="2" enabled="0"/>')) == 0.0


def test_resolve_value():
    import xml.etree.ElementTree as ET
    rv = corpus_audit.resolve_value
    const = ET.fromstring('<constant><array_20_zero target="0,0,0"/></constant>')
    assert rv(None, None) is None
    assert rv(ET.fromstring('<ch_00 value="1,2,3"/>'), const) == "1,2,3"
    assert rv(ET.fromstring('<ch_00 preset="array_20_zero"/>'), const) == "0,0,0"
    # value= wins over preset=; unknown preset → None
    assert rv(ET.fromstring('<ch_00 value="9" preset="array_20_zero"/>'), const) == "9"
    assert rv(ET.fromstring('<ch_00 preset="missing"/>'), const) is None
