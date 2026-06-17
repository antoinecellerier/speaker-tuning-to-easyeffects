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
    assert corpus_audit.is_dax3_xml("SOUNDWIRE_MAN_025D_FUNC_1318.xml")
    assert corpus_audit.is_dax3_xml("SDW_x.xml")
    assert not corpus_audit.is_dax3_xml("DEV_0287_SUBSYS_17AA22E6_settings.xml")
    assert not corpus_audit.is_dax3_xml("readme.txt")
    assert not corpus_audit.is_dax3_xml("random.xml")


def test_find_xmls_excludes_settings(tmp_path):
    (tmp_path / "DEV_0287_SUBSYS_TEST.xml").write_text(_SYNTHETIC_XML)
    (tmp_path / "DEV_0287_SUBSYS_TEST_settings.xml").write_text("<x/>")
    found = corpus_audit.find_xmls([str(tmp_path)])
    assert len(found) == 1
    assert found[0].endswith("DEV_0287_SUBSYS_TEST.xml")


def test_analyse_extracts_profile_row(tmp_path):
    p = tmp_path / "DEV_0287_SUBSYS_TEST.xml"
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
