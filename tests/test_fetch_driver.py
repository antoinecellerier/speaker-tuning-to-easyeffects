"""Catalog resolution and safety checks for tools/fetch_driver/get_lenovo_dax_xml.py.

No network: `_get` is monkeypatched to serve canned catalog / descriptor XML.
"""

import hashlib

import pytest

from tools.fetch_driver import get_lenovo_dax_xml as g

CATALOG = """<?xml version="1.0"?>
<packages count="3">
  <package><location>https://x/dock.xml</location><category>Audio</category></package>
  <package><location>https://x/realtek.xml</location><category>Audio</category></package>
  <package><location>https://x/net.xml</location><category>Networking</category></package>
</packages>"""

DOCK = """<?xml version="1.0"?>
<Package name="DOCK_AUDIO_N20PE" version="6.3.9600.2299">
  <Files><File><Name>n20pe10w.exe</Name><CRC>aa</CRC></File></Files>
</Package>"""

REALTEK = """<?xml version="1.0"?>
<Package name="AUD_R1MAR" version="6.0.9366.1">
  <PackageXML>
    <HardwareID><![CDATA[SWC\\VEN_DOLBY&PID_DAX3APOSVC]]></HardwareID>
    <HardwareID><![CDATA[HDAUDIO\\FUNC_01&VEN_10EC&DEV_0257&SUBSYS_17AA5094]]></HardwareID>
  </PackageXML>
  <Files>
    <File><Name>r1mra06w.exe</Name><CRC>BEEF</CRC></File>
    <File id="EN"><Name>r1mra06w.html</Name><CRC>99</CRC></File>
  </Files>
</Package>"""

OLDER_REALTEK = REALTEK.replace('version="6.0.9366.1"', 'version="6.0.9000.1"')

# An audio driver that serves the codec but carries no Dolby APO hwid — the
# common case (5 of 8 machine types in the PR review's sweep).
PLAIN_AUDIO = """<?xml version="1.0"?>
<Package name="AUD_N3AA1" version="6.0.9764.1">
  <PackageXML>
    <HardwareID><![CDATA[HDAUDIO\\FUNC_01&VEN_10EC&DEV_0257&SUBSYS_17AA5094]]></HardwareID>
  </PackageXML>
  <Files><File><Name>n3aa1.exe</Name><CRC>ab</CRC></File></Files>
</Package>"""

# The Dolby *Vision* Provisioning Kit — a display package that used to leak
# into the audio candidate pool via a name-substring match.
DOLBY_VISION = """<?xml version="1.0"?>
<Package name="LenovoProvisionDolbyVision" version="2.0.1.0">
  <Files><File><Name>dolbyvision.exe</Name><CRC>cd</CRC></File></Files>
</Package>"""

TOKENS = [("10EC", "0257", "17AA5094", "ALC257")]


@pytest.fixture
def net(monkeypatch):
    pages = {
        "https://download.lenovo.com/catalog/20XL_Win11.xml": CATALOG,
        "https://x/dock.xml": DOCK,
        "https://x/realtek.xml": REALTEK,
        "https://x/net.xml": "<Package name='NET'/>",
    }

    def fake_get(url):
        if url not in pages:
            raise g.Fail(f"{url} -> HTTP 404")
        return pages[url].encode()

    monkeypatch.setattr(g, "_get", fake_get)
    return pages


def test_catalog_filters_to_audio(net):
    urls = g.catalog_packages("20XL", ["11"])
    assert urls == ["https://x/dock.xml", "https://x/realtek.xml"]


def test_pick_prefers_dolby_apo_and_skips_dock(net):
    d = g.pick_descriptor(["https://x/dock.xml", "https://x/realtek.xml"], [])
    assert d.name == "AUD_R1MAR"
    assert d.exe_name == "r1mra06w.exe"
    assert d.exe_url == "https://x/r1mra06w.exe"
    assert d.sha256 == "beef"


def test_pick_breaks_ties_on_version(net, monkeypatch):
    monkeypatch.setattr(g, "_get", lambda u: {
        "https://x/a.xml": REALTEK, "https://x/b.xml": OLDER_REALTEK,
    }[u].encode())
    d = g.pick_descriptor(["https://x/b.xml", "https://x/a.xml"], [])
    assert d.version == "6.0.9366.1"


def test_pick_without_dolby_uses_highest_version(monkeypatch):
    plain = DOCK.replace("DOCK_AUDIO_N20PE", "AUDIO_PLAIN")
    older = plain.replace('version="6.3.9600.2299"', 'version="6.1.0.0"')
    monkeypatch.setattr(g, "_get", lambda u: {
        "https://x/new.xml": plain, "https://x/old.xml": older,
    }[u].encode())
    d = g.pick_descriptor(["https://x/old.xml", "https://x/new.xml"], [])
    assert d.name == "AUDIO_PLAIN"
    assert d.version == "6.3.9600.2299"


def test_pick_prefers_descriptor_that_serves_the_codec(monkeypatch):
    # PLAIN_AUDIO serves the codec but has no Dolby APO; a higher-version
    # package that serves nothing must not win on version alone.
    other = REALTEK.replace("AUD_R1MAR", "AUD_OTHER").replace(
        "6.0.9366.1", "9.9.9.9").replace(
        "HDAUDIO\\FUNC_01&VEN_10EC&DEV_0257&SUBSYS_17AA5094",
        "HDAUDIO\\FUNC_01&VEN_10EC&DEV_0999")
    monkeypatch.setattr(g, "_get", lambda u: {
        "https://x/plain.xml": PLAIN_AUDIO, "https://x/other.xml": other,
    }[u].encode())
    d = g.pick_descriptor(["https://x/other.xml", "https://x/plain.xml"], TOKENS)
    assert d.name == "AUD_N3AA1"


def test_pick_ignores_dolby_vision_display_package(monkeypatch):
    monkeypatch.setattr(g, "_get", lambda u: {
        "https://x/vision.xml": DOLBY_VISION, "https://x/audio.xml": PLAIN_AUDIO,
    }[u].encode())
    d = g.pick_descriptor(["https://x/vision.xml", "https://x/audio.xml"], TOKENS)
    assert d.name == "AUD_N3AA1"


def test_catalog_unreachable_is_actionable(monkeypatch):
    monkeypatch.setattr(g, "_get", lambda u: (_ for _ in ()).throw(g.Fail("HTTP 403")))
    with pytest.raises(g.Fail, match="no catalog reachable"):
        g.catalog_packages("ZZZZ", ["11", "10"])


def test_download_aborts_on_checksum_mismatch(tmp_path, monkeypatch):
    payload = b"not the real driver"

    class FakeResp:
        headers = {"Content-Length": str(len(payload))}

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1):
            b, self._done = getattr(self, "_buf", payload), True
            if getattr(self, "_sent", False):
                return b""
            self._sent = True
            return b

    monkeypatch.setattr(g.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    dest = tmp_path / "d.exe"
    with pytest.raises(g.Fail, match="checksum mismatch"):
        g.download("https://x/d.exe", dest, "0" * 64)
    assert not dest.exists()


def test_download_accepts_matching_checksum(tmp_path, monkeypatch):
    payload = b"good"
    good = hashlib.sha256(payload).hexdigest()

    class FakeResp:
        headers = {}

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1):
            if getattr(self, "_sent", False):
                return b""
            self._sent = True
            return payload

    monkeypatch.setattr(g.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    dest = tmp_path / "d.exe"
    g.download("https://x/d.exe", dest, good)
    assert dest.read_bytes() == payload


@pytest.mark.parametrize("field,value,expect", [
    ("product_sku", "LENOVO_MT_20XL_BU_Think_FM_ThinkPad T14 Gen 2a", "20XL"),
    ("product_name", "20XLS23200", "20XL"),
])
def test_machine_type_parsing(monkeypatch, field, value, expect):
    vals = {"sys_vendor": "LENOVO", "product_sku": "", "product_name": "", field: value}
    monkeypatch.setattr(g, "_dmi", lambda f: vals.get(f, ""))
    assert g.machine_type() == expect


def test_machine_type_empty_when_not_lenovo(monkeypatch):
    monkeypatch.setattr(g, "_dmi", lambda f: "Dell Inc." if f == "sys_vendor" else "x")
    assert g.machine_type() == ""


def test_extract_without_innoextract_names_the_package(tmp_path, monkeypatch):
    monkeypatch.setattr(g.shutil, "which", lambda _: None)
    monkeypatch.setattr(g.packages, "family", lambda: g.packages.DEBIAN)
    with pytest.raises(g.Fail, match="innoextract"):
        g.extract(tmp_path / "x.exe", tmp_path)


def test_tuning_xml_dir_finds_the_one_dolby_dir(tmp_path):
    deep = tmp_path / "code$GetExtractPath$" / "Dolby" / "03_dax_ext"
    deep.mkdir(parents=True)
    (deep / "DEV_0257_SUBSYS_17AA5094.xml").write_text("<x/>")
    (deep / "DEV_0257_SUBSYS_17AA5094_dmic.xml").write_text("<x/>")  # companion
    assert g.tuning_xml_dir(tmp_path) == deep


def test_tuning_xml_dir_errors_when_nothing_extracted(tmp_path):
    with pytest.raises(g.Fail, match="no Dolby DAX3 tuning"):
        g.tuning_xml_dir(tmp_path)
