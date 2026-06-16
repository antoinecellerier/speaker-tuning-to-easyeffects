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
