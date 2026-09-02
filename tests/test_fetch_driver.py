"""Catalog resolution and safety checks for tools/fetch_driver/get_lenovo_dax_xml.py.

No network: `_get` is monkeypatched to serve canned catalog / descriptor XML.
"""

import hashlib
import subprocess

import pytest

from lib.hardware import codecs
from tools.fetch_driver import get_lenovo_dax_xml as g

CATALOG = """<?xml version="1.0"?>
<packages count="4">
  <package><location>https://x/dock.xml</location><category>Audio</category></package>
  <package><location>https://x/realtek.xml</location><category>Audio</category></package>
  <package><location>https://x/net.xml</location><category>Networking</category></package>
  <package><location>https://x/lenovoprovisiondolbyvisionp17_2_.xml</location><category>Video</category></package>
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
    <File><Name>r1mra06w.exe</Name><CRC>BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB</CRC></File>
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
<Package name="LenovoProvisionDolbyVision" version="99.0.1.0">
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


def test_pick_skips_a_dock_audio_package(net, monkeypatch):
    """DOCK_ is USB dock audio, not the internal codec. It carries no Dolby
    hwid either, so give it one — otherwise this passes on the wrong reason."""
    monkeypatch.setattr(g, "_get", lambda u: {
        "https://x/dock.xml": DOCK.replace(
            "<Files>",
            "<PackageXML><HardwareID><![CDATA[SWC\\VEN_DOLBY&PID_DAX3APOSVC]]>"
            "</HardwareID></PackageXML><Files>"),
        "https://x/plain.xml": PLAIN_AUDIO,
    }[u].encode())
    d = g.pick_descriptor(["https://x/dock.xml", "https://x/plain.xml"], [])
    assert d.name == "AUD_N3AA1"


def test_pick_prefers_dolby_apo_and_skips_dock(net):
    d = g.pick_descriptor(["https://x/dock.xml", "https://x/realtek.xml"], [])
    assert d.name == "AUD_R1MAR"
    assert d.exe_name == "r1mra06w.exe"
    assert d.exe_url == "https://x/r1mra06w.exe"
    assert d.sha256 == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


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
    # A marketing name is not a machine type. Slicing four characters off
    # these yielded THIN / IDEA, which 404 the catalog with a message that
    # blames the machine type the user never gave.
    ("product_name", "ThinkPad X1 Carbon Gen 9", ""),
    ("product_name", "IdeaPad Pro 5 14AHP9", ""),
    ("product_name", "Legion Y540-15IRH", ""),
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
    with pytest.raises(g.Fail, match=r"sudo apt install innoextract"):
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


def test_a_network_error_mid_download_is_a_message_not_a_traceback(tmp_path,
                                                                   monkeypatch):
    """A dropped connection used to escape `main`'s `except Fail` as a
    traceback, which reads as a bug in the tool rather than a bad network."""
    def boom(*a, **k):
        raise g.urllib.error.URLError("connection reset")

    monkeypatch.setattr(g.urllib.request, "urlopen", boom)
    dest = tmp_path / "d.exe"
    with pytest.raises(g.Fail, match="connection reset"):
        g.download("https://x/d.exe", dest, "0" * 64)
    assert not dest.exists()


def test_an_http_error_on_the_exe_is_a_message_not_a_traceback(tmp_path,
                                                               monkeypatch):
    def boom(*a, **k):
        raise g.urllib.error.HTTPError("https://x/d.exe", 404, "Not Found",
                                       None, None)

    monkeypatch.setattr(g.urllib.request, "urlopen", boom)
    with pytest.raises(g.Fail, match="HTTP 404"):
        g.download("https://x/d.exe", tmp_path / "d.exe", "0" * 64)


def test_unreadable_descriptors_are_named_not_hidden(monkeypatch):
    """All-403 descriptors used to report "none look like an internal-codec
    driver" — true of an empty list, and the wrong thing to go fix."""
    monkeypatch.setattr(
        g, "_get", lambda u: (_ for _ in ()).throw(g.Fail(f"{u} -> HTTP 403")))
    with pytest.raises(g.Fail, match=r"HTTP 403") as excinfo:
        g.pick_descriptor(["https://x/a.xml", "https://x/b.xml"], TOKENS)
    assert "https://x/a.xml" in str(excinfo.value)
    assert "https://x/b.xml" in str(excinfo.value)


def test_a_catalog_with_no_audio_package_says_so(monkeypatch):
    monkeypatch.setattr(g, "_get", lambda u: b"")
    with pytest.raises(g.Fail, match="lists no audio package"):
        g.pick_descriptor([], TOKENS)


def test_a_crc_that_is_not_a_sha256_skips_verification():
    """`<CRC>` is a SHA-256 on every descriptor we've read, but the tag name
    doesn't promise one. Treating a CRC32 as a digest would abort every run of
    that machine type after the full download, blaming the file."""
    assert g.Descriptor("https://x/d.xml", DOCK.encode()).sha256 == ""
    assert g.Descriptor("https://x/r.xml", REALTEK.encode()).sha256 == "b" * 64


def test_an_exe_url_with_no_filename_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "codec_tokens", lambda: [])
    args = g.build_parser().parse_args(
        ["--exe-url", "https://x/", "--driver-cache", str(tmp_path)])
    with pytest.raises(g.Fail, match="no filename in"):
        g.run(args)


def _fan_out(root, skus):
    """A per-SKU extraction: one sibling directory per machine, as the Legion
    Y540-15IRH installer lays it out."""
    base = root / "code$GetExtractPath$" / "Dolby" / "ext_realtek_lenovo_ideapad"
    made = {}
    for subsys in skus:
        d = base / f"sku_{subsys}"
        d.mkdir(parents=True)
        (d / f"DEV_0257_SUBSYS_{subsys}.xml").write_text("<x/>")
        made[subsys] = d
    return made


def test_a_per_sku_fan_out_resolves_to_this_machine(tmp_path, monkeypatch):
    """A fan-out used to abort after the whole driver had been downloaded.
    Picking dirs[0] would have picked another laptop's tuning."""
    dirs = _fan_out(tmp_path, ["17AA380F", "17AA5094", "17AA22E6"])
    monkeypatch.setattr(
        codecs, "get_hda_codec_ids", lambda: [("10EC0257", "17AA380F", "ALC257")])
    monkeypatch.setattr(codecs, "get_pci_audio_subsystem", lambda: None)
    assert g.tuning_xml_dir(tmp_path) == dirs["17AA380F"]


def test_a_fan_out_that_matches_nothing_still_names_every_directory(tmp_path,
                                                                    monkeypatch):
    """Narrowing that finds nothing must not silently pick one."""
    _fan_out(tmp_path, ["17AA380F", "17AA5094"])
    monkeypatch.setattr(
        codecs, "get_hda_codec_ids", lambda: [("10EC0257", "17AA9999", "ALC257")])
    monkeypatch.setattr(codecs, "get_pci_audio_subsystem", lambda: None)
    with pytest.raises(g.Fail, match="more than one directory") as excinfo:
        g.tuning_xml_dir(tmp_path)
    assert "sku_17AA380F" in str(excinfo.value)
    assert "sku_17AA5094" in str(excinfo.value)


def test_an_extraction_of_only_companions_retries_unfiltered(tmp_path,
                                                             monkeypatch):
    """`_settings`/`_dmic` companions are not tuning XMLs. Judging the filtered
    pass by "any .xml" called it a success and skipped the retry that would
    have found the real files."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = tmp_path / "cache" / "extract"
        out.mkdir(parents=True, exist_ok=True)
        if "-I" in cmd:                      # filtered pass: companions only
            (out / "DEV_0257_SUBSYS_17AA5094_dmic.xml").write_text("<x/>")
        else:                                # unfiltered retry: the real thing
            (out / "DEV_0257_SUBSYS_17AA5094.xml").write_text("<x/>")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(g.shutil, "which", lambda _: "/usr/bin/innoextract")
    monkeypatch.setattr(g.subprocess, "run", fake_run)
    out = g.extract(tmp_path / "d.exe", tmp_path / "cache")
    assert len(calls) == 2, "the unfiltered retry never ran"
    assert g.tuning_xml_dir(out).name == "extract"


def test_a_subsystem_two_sku_dirs_share_stays_ambiguous(tmp_path, monkeypatch):
    """Real packages reuse a subsystem across SKU directories (17AA382B sits in
    two of the Legion Y540 package's fifteen). Narrowing must not guess."""
    _fan_out(tmp_path, ["17AA382B", "17AA3833"])
    extra = tmp_path / "code$GetExtractPath$" / "Dolby" / \
        "ext_realtek_lenovo_ideapad" / "sku_other"
    extra.mkdir(parents=True)
    (extra / "DEV_0257_SUBSYS_17AA382B.xml").write_text("<x/>")
    monkeypatch.setattr(
        codecs, "get_hda_codec_ids", lambda: [("10EC0257", "17AA382B", "ALC257")])
    monkeypatch.setattr(codecs, "get_pci_audio_subsystem", lambda: None)
    with pytest.raises(g.Fail, match="more than one directory"):
        g.tuning_xml_dir(tmp_path)


def test_hdmi_codecs_are_not_offered_to_the_catalog():
    """A display codec has no Dolby tuning, and its DEV token must not join the
    codec match. The old SSID test caught AMD's 00AA0100 but not Intel's."""
    ids = [("10EC0287", "17AA22E6", "Realtek ALC287"),
           ("8086281C", "80860101", "Intel Alderlake-P HDMI"),
           ("1002AA01", "00AA0100", "ATI R6xx HDMI")]
    real = codecs.get_hda_codec_ids
    codecs.get_hda_codec_ids = lambda: ids
    try:
        assert g.codec_tokens() == [("10EC", "0287", "17AA22E6", "Realtek ALC287")]
    finally:
        codecs.get_hda_codec_ids = real
