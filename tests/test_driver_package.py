"""A vendor's driver download, passed in place of the tuning XML.

The unpacking tools are stood in for in-process: what is under test is how
their listings are read, what is asked of them, and what is kept. The real
tools are read in `tests/test_live_machine.py`.
"""

import json
import os
import re
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

import dolby_to_easyeffects
from lib import console, driver_package as dp, packages, tool_env
from lib.apo import stacked
from tests.conftest import (write_pe, write_surface_package,
                            write_synthetic_tuning_xml)

_7Z_MAGIC = b"7z\xbc\xaf'\x1c"
_XML = "DEV_0274_SUBSYS_10EC1284_PCI_SUBSYS_72708086.xml"
# The codec the Surface fixture's XML tunes, as /proc/asound shows it.
_CODEC = ("Codec: Realtek ALC274\nVendor Id: 0x10ec0274\n"
          "Subsystem Id: 0x10ec1284\n")


@pytest.fixture
def cache(tmp_path):
    return tmp_path / "driver-cache"


def _surface_zip(tmp_path, name="SurfaceTest.zip", extra=True) -> Path:
    """A zip shaped like an unpacked Surface .msi: the Dolby folder, the APO
    folder beside it, and payload the readers never open."""
    src = tmp_path / "src" / "SurfaceUpdate"
    write_synthetic_tuning_xml(write_surface_package(src))
    if extra:
        (src / "graphicsbase").mkdir()
        (src / "graphicsbase" / "driver.sys").write_bytes(b"\0" * 4096)
        (src / "dax3extrtk" / (_XML[:-4] + "_settings.xml")).write_text("<x/>")
    out = tmp_path / name
    with zipfile.ZipFile(out, "w") as zf:
        for f in sorted(src.parent.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src.parent).as_posix())
    return out


# --- what counts as a download -----------------------------------------------

def test_kind_reads_the_content_not_the_name(tmp_path):
    (tmp_path / "a.exe").write_bytes(b"PK\x03\x04rest")
    (tmp_path / "b.bin").write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"x")
    (tmp_path / "c.zip").write_bytes(b"MZ\x90\x00")
    (tmp_path / "d.xml").write_text("<device_data/>")
    assert [dp.kind(tmp_path / n) for n in ("a.exe", "b.bin", "c.zip",
                                           "d.xml")] == ["zip", "msi", "exe",
                                                         None]


def test_anything_that_is_not_a_readable_file_stays_on_the_xml_path(tmp_path):
    assert dp.kind(tmp_path) is None
    assert dp.kind(tmp_path / "missing.exe") is None


@pytest.mark.parametrize("shape,expect", [
    (dict(overlay=b"\0" * 64 + b"Inno Setup Setup Data (6.1.0)",
          signed=True), packages.INNOEXTRACT),
    (dict(overlay=_7Z_MAGIC + b"\0" * 64), packages.SEVENZIP),
    # ASUS: nothing after the image but the signature, and the archive
    # inside the resources.
    (dict(rsrc=b"\0" * 512 + _7Z_MAGIC + b"\0" * 64, signed=True),
     packages.SEVENZIP),
    (dict(rsrc=b"\0" * 512, overlay=b"\0" * 64, signed=True), None),
])
def test_the_triage_names_one_tool_per_exe_shape(tmp_path, shape, expect):
    assert dp._pe_triage(write_pe(tmp_path / "x.exe", **shape)) == expect


def test_the_inno_marker_inside_the_signature_is_not_read_as_inno(tmp_path):
    """The certificate table sits after the image too. Only what lies
    between the image and the signature is the installer's own data."""
    exe = write_pe(tmp_path / "x.exe", rsrc=b"\0" * 512, signed=True)
    exe.write_bytes(exe.read_bytes() + b"Inno Setup Setup Data (6.1.0)")
    data = bytearray(exe.read_bytes())
    # Stretch the certificate table over the trailing marker.
    import struct
    off, size = struct.unpack_from("<II", data, 64 + 24 + 112 + 32)
    struct.pack_into("<II", data, 64 + 24 + 112 + 32, off, len(data) - off)
    exe.write_bytes(bytes(data))
    assert dp._pe_triage(exe) is None


# --- extraction --------------------------------------------------------------

def test_a_zip_keeps_only_the_tunings_and_the_apo_folder(tmp_path, cache):
    tree = dp.unpack(_surface_zip(tmp_path), cache_root=cache)
    kept = sorted(p.relative_to(tree).as_posix()
                  for p in tree.rglob("*") if p.is_file())
    assert kept == [dp.PROVENANCE,
                    f"SurfaceUpdate/dax3extrtk/{_XML}",
                    "SurfaceUpdate/surfaceapoextension/SurfaceAPOExtension.inf",
                    "SurfaceUpdate/surfaceapoextension/SurfaceAPO_TEST.json"]
    assert tree.parent == cache and re.fullmatch(r"SurfaceTest-[0-9a-f]{12}",
                                                tree.name)


def test_the_filtered_tree_binds_the_same_apo_layer_as_the_full_one(tmp_path,
                                                                   cache):
    download = _surface_zip(tmp_path)
    full = stacked.find_for_xml(
        tmp_path / "src" / "SurfaceUpdate" / "dax3extrtk" / _XML)
    tree = dp.unpack(download, cache_root=cache)
    filtered = stacked.find_for_xml(
        tree / "SurfaceUpdate" / "dax3extrtk" / _XML)
    assert full is not None and filtered is not None
    assert (filtered.eq_left, filtered.dynamics) == (full.eq_left,
                                                     full.dynamics)


def test_provenance_names_the_source_and_every_kept_file(tmp_path, cache):
    download = _surface_zip(tmp_path)
    tree = dp.unpack(download, cache_root=cache)
    text = (tree / dp.PROVENANCE).read_text()
    assert "Partial extraction" in text and "not the whole download" in text
    prov = dp._read_provenance(tree)
    assert prov["Source"] == download.name
    assert prov["Size"] == str(download.stat().st_size)
    assert prov["Modified-ns"] == str(download.stat().st_mtime_ns)
    assert len(prov["SHA-256"]) == 64
    kept = re.findall(r"^  (SurfaceUpdate/\S+)$", text, re.MULTILINE)
    assert len(kept) == 3
    assert "unzip -d DIR SurfaceTest.zip" in text


def test_a_second_run_reuses_the_tree(tmp_path, cache, capsys):
    download = _surface_zip(tmp_path)
    first = dp.unpack(download, cache_root=cache)
    capsys.readouterr()
    assert dp.unpack(download, cache_root=cache) == first
    assert "Reusing the tuning files" in capsys.readouterr().out


def test_a_copy_or_a_touch_makes_no_second_tree(tmp_path, cache, monkeypatch):
    download = _surface_zip(tmp_path)
    first = dp.unpack(download, cache_root=cache)
    copy = tmp_path / "SurfaceTest (1).zip"
    copy.write_bytes(download.read_bytes())
    os.utime(copy, ns=(1, 1))
    assert dp.unpack(copy, cache_root=cache) == first
    assert [d.name for d in cache.iterdir()] == [first.name]


def test_a_tree_this_did_not_make_is_neither_reused_nor_removed(tmp_path,
                                                               cache):
    download = _surface_zip(tmp_path)
    tree = dp.unpack(download, cache_root=cache)
    (tree / dp.PROVENANCE).unlink()
    with pytest.raises(dp.PackageError, match="no PROVENANCE.txt from this"):
        dp.unpack(download, cache_root=cache)
    assert (tree / "SurfaceUpdate" / "dax3extrtk" / _XML).is_file()


def test_a_download_with_no_tuning_writes_nothing(tmp_path, cache):
    download = tmp_path / "codec.zip"
    with zipfile.ZipFile(download, "w") as zf:
        zf.writestr("Realtek/HDXRT.inf", "x")
        zf.writestr("Realtek/DEV_0274_SUBSYS_10EC1284_settings.xml", "<x/>")
    with pytest.raises(dp.PackageError, match="holds no Dolby or Sonitude tuning file"):
        dp.unpack(download, cache_root=cache)
    assert not cache.exists()


def test_apo_files_alone_are_not_a_tuning(tmp_path, cache):
    download = tmp_path / "apo.zip"
    with zipfile.ZipFile(download, "w") as zf:
        zf.writestr("apo/SurfaceAPOExtension.inf", "x")
        zf.writestr("apo/SurfaceAPO_1.json", "{}")
    with pytest.raises(dp.PackageError, match="holds no Dolby or Sonitude tuning file"):
        dp.unpack(download, cache_root=cache)
    assert not cache.exists()


@pytest.mark.parametrize("member", ["../escape/" + _XML, "/abs/" + _XML,
                                    "C:/win/" + _XML])
def test_a_member_outside_the_tree_refuses_the_download(tmp_path, cache,
                                                        member):
    download = tmp_path / "bad.zip"
    with zipfile.ZipFile(download, "w") as zf:
        zf.writestr(member, "<x/>")
    with pytest.raises(dp.PackageError, match="outside its own folder"):
        dp.unpack(download, cache_root=cache)
    assert not cache.exists()


def test_a_link_member_refuses_the_download(tmp_path, cache):
    download = tmp_path / "link.zip"
    with zipfile.ZipFile(download, "w") as zf:
        info = zipfile.ZipInfo("d/" + _XML)
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(dp.PackageError, match="a link"):
        dp.unpack(download, cache_root=cache)


def test_a_symlink_in_the_unpacked_tree_is_refused(tmp_path, cache,
                                                   monkeypatch):
    real = dp._zip_open

    def planting(path):
        opened = real(path)
        inner = opened.extract

        def extract(partial, wanted):
            inner(partial, wanted)
            os.symlink("/etc/passwd", partial / "planted")
        opened.extract = extract
        return opened

    monkeypatch.setattr(dp, "_zip_open", planting)
    with pytest.raises(dp.PackageError, match="unpacked a link"):
        dp.unpack(_surface_zip(tmp_path), cache_root=cache)
    assert [p.name for p in cache.iterdir()] == []


def test_a_zip_claiming_too_much_is_refused(tmp_path, cache, monkeypatch):
    monkeypatch.setattr(dp, "_ZIP_MAX_WANTED_BYTES", 10)
    with pytest.raises(dp.PackageError, match="more than a driver download"):
        dp.unpack(_surface_zip(tmp_path), cache_root=cache)


def test_a_failed_extraction_leaves_no_partial_tree(tmp_path, cache,
                                                    monkeypatch):
    download = _surface_zip(tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(dp.shutil, "copyfileobj", boom)
    with pytest.raises(OSError, match="disk full"):
        dp.unpack(download, cache_root=cache)
    assert list(cache.iterdir()) == []


def test_a_killed_runs_partial_tree_is_swept(tmp_path, cache):
    cache.mkdir()
    old, fresh = cache / ".partial-old", cache / ".partial-fresh"
    old.mkdir()
    fresh.mkdir()
    an_hour_ago = time.time() - 2 * dp._STALE_PARTIAL_S
    os.utime(old, (an_hour_ago, an_hour_ago))
    dp.unpack(_surface_zip(tmp_path), cache_root=cache)
    assert not old.exists() and fresh.exists()


def test_the_cache_defaults_to_the_environment_override(monkeypatch,
                                                         tmp_path):
    monkeypatch.setenv(dp.CACHE_ENV, str(tmp_path / "c"))
    assert dp.default_cache_root() == tmp_path / "c"
    monkeypatch.delenv(dp.CACHE_ENV)
    assert dp.default_cache_root() == Path(dp.REPO_ROOT) / "driver-cache"


# --- the tools, stood in for --------------------------------------------------

class _Tools:
    """`tool_env.which`/`run` for the unpackers, answering from a table."""

    def __init__(self, monkeypatch, installed, handler):
        self.calls = []
        monkeypatch.setattr(tool_env, "which",
                            lambda name: f"/usr/bin/{name}"
                            if name in installed else None)

        def run(argv, **kw):
            self.calls.append(list(argv))
            rc, out = handler(argv)
            return subprocess.CompletedProcess(argv, rc, out, "")
        monkeypatch.setattr(tool_env, "run", run)


def _dest(argv, flag):
    for a in argv:
        if a.startswith(flag):
            return Path(a[len(flag):])
    return Path(argv[argv.index(flag.rstrip("=")) + 1])


def _inno_listing(paths):
    return ('Listing "x" - setup data version 5.5.7 (unicode)\n'
            + "".join(f' - "{p}" (1 KiB)\n' for p in paths))


def test_inno_extracts_each_wanted_file_by_its_anchored_path(tmp_path, cache,
                                                             monkeypatch):
    xml = f"code$GetExtractPath$/Dolby/03_dax_ext/{_XML}"
    paths = [xml, "code$GetExtractPath$/Dolby/03_dax_ext/x.dll",
             "code$GetExtractPath$/Setup.exe"]

    def handler(argv):
        if "--version" in argv:
            return 0, "innoextract 1.9\n"
        if "--list" in argv:
            return 0, _inno_listing(paths)
        out = Path(argv[argv.index("-d") + 1])
        for i, a in enumerate(argv):
            if a == "-I":
                f = out / argv[i + 1].lstrip("/")
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("<x/>")
        return 0, ""

    tools = _Tools(monkeypatch, {"innoextract"}, handler)
    exe = write_pe(tmp_path / "lenovo.exe", overlay=b"Inno Setup Setup Data (")
    tree = dp.unpack(exe, cache_root=cache)
    extract = [c for c in tools.calls if "-d" in c][0]
    assert [extract[i + 1] for i, a in enumerate(extract) if a == "-I"] == [
        "/" + xml]
    assert (tree / xml).is_file()
    assert "Tool: innoextract 1.9" in (tree / dp.PROVENANCE).read_text()


def _slt(kind_, members, offset=None):
    head = f"Path = archive\nType = {kind_}\n" + (
        f"Offset = {offset}\n" if offset is not None else "")
    body = "\n".join(f"Path = {p}\nSize = {s}\nAttributes = A -rw-r--r--\n"
                     for p, s in members)
    return f"7-Zip 26.04 (x64)\n\n--\n{head}\n----------\n{body}\n"


def test_a_7z_sfx_hands_7z_a_list_of_the_wanted_files(tmp_path, cache,
                                                      monkeypatch):
    xml = f"18_Dolby/ext_realtek_framework/{_XML}"
    listed = []

    def handler(argv):
        if argv[1:2] == ["l"]:
            return 0, _slt("7z", [(xml, 5), ("18_Dolby/a.dll", 9),
                                  ("01_Chipset/b.sys", 9)], offset=512)
        if argv[1:2] == ["x"]:
            listfile = next(a[1:] for a in argv if a.startswith("@"))
            names = Path(listfile).read_text().split()
            listed.extend(names)
            for n in names:
                f = _dest(argv, "-o") / n
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("<x/>")
            return 0, ""
        return 0, "7-Zip 26.04 (x64) : Copyright\n"

    _Tools(monkeypatch, {"7z"}, handler)
    exe = write_pe(tmp_path / "bundle.exe", overlay=_7Z_MAGIC)
    tree = dp.unpack(exe, cache_root=cache)
    assert listed == [xml] and (tree / xml).is_file()


def test_a_7z_sfx_listed_as_a_pe_with_the_7z_nested_opens_as_7z(
        tmp_path, cache, monkeypatch):
    """Framework's bundle: 7-Zip 26.04 lists the PE stub, then `----` and
    the 7z overlay it opened inside it, then the 7z's members."""
    xml = f"18_Dolby/ext_realtek_framework/{_XML}"

    def handler(argv):
        if argv[1:2] == ["l"]:
            nested = _slt("7z", [(xml, 5), ("01_Chipset/b.sys", 9)]).replace(
                "--\nPath = archive\n",
                "--\nPath = archive\nType = PE\n\n----\nPath = [0]\n"
                "Size = 99\n--\nPath = [0]\n", 1)
            return 0, nested
        if argv[1:2] == ["x"]:
            for n in Path(next(a[1:] for a in argv if a.startswith("@"))
                          ).read_text().split():
                f = _dest(argv, "-o") / n
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("<x/>")
            return 0, ""
        return 0, "7-Zip 26.04 (x64) : Copyright\n"

    _Tools(monkeypatch, {"7z"}, handler)
    exe = write_pe(tmp_path / "bundle.exe", overlay=_7Z_MAGIC)
    tree = dp.unpack(exe, cache_root=cache)
    assert (tree / xml).is_file()
    assert "7z x -oDIR bundle.exe" in (tree / dp.PROVENANCE).read_text()


def test_an_asus_exe_is_opened_through_the_archive_in_its_resources(
        tmp_path, cache, monkeypatch):
    """The Ally X shape: plain `7z` opens the embedded Inno loader
    `.rsrc/EXE/102`, a PE, and only `-tPE` lists `.rsrc/ZIP/103`."""
    xml = f"ext_realtek_asus_consumer/v10/{_XML}"

    def handler(argv):
        target = argv[-1] if not argv[-1].startswith("@") else argv[-2]
        if argv[1:2] == ["l"]:
            if target.endswith("103"):
                return 0, _slt("7z", [(xml, 5), ("swc_factory/a.dll", 9)])
            if "-tPE" in argv:
                return 0, _slt("PE", [(".rsrc/EXE/102", 1 << 17),
                                      (".rsrc/ZIP/103", 1 << 17),
                                      (".rsrc/ICON/1", 100)])
            return 0, _slt("PE", [(".rsrc/0/RCDATA/11111", 44)])
        if argv[1:2] == ["x"]:
            names = Path(next(a[1:] for a in argv if a.startswith("@"))
                         ).read_text().split()
            for n in names:
                f = _dest(argv, "-o") / n
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(_7Z_MAGIC if n.endswith("103") else
                              b"MZ" if n.endswith("102") else b"<x/>")
            return 0, ""
        return 0, "7-Zip 26.04 (x64)\n"

    _Tools(monkeypatch, {"7z"}, handler)
    exe = write_pe(tmp_path / "asus.exe", rsrc=b"\0" * 64 + _7Z_MAGIC,
                   signed=True)
    tree = dp.unpack(exe, cache_root=cache)
    kept = sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*")
                  if p.is_file())
    assert kept == [dp.PROVENANCE, xml]


def test_an_msi_unpacks_in_full_then_keeps_only_the_wanted_files(
        tmp_path, cache, monkeypatch):
    paths = [f"SurfaceUpdate/dax3extrtk/{_XML}",
             "SurfaceUpdate/surfaceapoextension/SurfaceAPOExtension.inf",
             "SurfaceUpdate/surfaceapoextension/SurfaceAPO_1284.json",
             "SurfaceUpdate/graphicsbase/igd.sys"]

    def handler(argv):
        if "--list" in argv:
            return 0, "\n".join(paths) + "\n"
        if "-C" in argv:
            for p in paths:
                f = Path(argv[argv.index("-C") + 1]) / p
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("x")
            return 0, ""
        if argv[0] == "msiinfo":
            return 0, ("File\tComponent_\tFileName\tFileSize\n"
                       "s72\ts72\tl255\ti4\nFile\tFile\n"
                       "a\tc\tigd.sys\t1000\n")
        return 0, "0.106\n"

    _Tools(monkeypatch, {"msiextract", "msiinfo"}, handler)
    download = tmp_path / "SurfacePro9.msi"
    download.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 64)
    tree = dp.unpack(download, cache_root=cache)
    kept = sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*")
                  if p.is_file())
    assert kept == [dp.PROVENANCE] + sorted(paths[:3])
    assert not (tree / ".full").exists()


# --- naming the missing tool --------------------------------------------------

def _no_tools(monkeypatch):
    return _Tools(monkeypatch, set(), lambda argv: (1, ""))


@pytest.mark.parametrize("shape,named,not_named", [
    (dict(overlay=b"Inno Setup Setup Data (6.1.0)", signed=True),
     "innoextract", "7-Zip"),
    (dict(rsrc=b"\0" * 512 + _7Z_MAGIC, signed=True), "7-Zip",
     "innoextract"),
])
def test_a_missing_tool_names_only_the_one_this_exe_needs(
        tmp_path, cache, monkeypatch, shape, named, not_named):
    _no_tools(monkeypatch)
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.DEBIAN)
    with pytest.raises(dp.PackageError) as exc:
        dp.unpack(write_pe(tmp_path / "x.exe", **shape), cache_root=cache)
    text = str(exc.value) + " ".join(t for _s, t in exc.value.next_step)
    assert named in text and not_named not in text
    assert "sudo apt install " in text


def test_an_msi_without_msitools_names_msitools(tmp_path, cache, monkeypatch):
    _no_tools(monkeypatch)
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.ARCH)
    download = tmp_path / "s.msi"
    download.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1"))
    with pytest.raises(dp.PackageError) as exc:
        dp.unpack(download, cache_root=cache)
    assert exc.value.next_step == (("cta", "sudo pacman -S msitools"),)


def test_a_repository_without_the_tool_points_at_epel(tmp_path, cache,
                                                      monkeypatch):
    _no_tools(monkeypatch)
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.FEDORA)
    monkeypatch.setattr(packages, "unavailable", lambda key, fam: True)
    exe = write_pe(tmp_path / "x.exe", overlay=b"Inno Setup Setup Data (")
    with pytest.raises(dp.PackageError) as exc:
        dp.unpack(exe, cache_root=cache)
    (style, text), = exc.value.next_step
    assert "dnf has no innoextract package here" in text and "EPEL" in text
    assert "dnf install" not in text


def test_an_exe_nothing_can_open_says_so(tmp_path, cache, monkeypatch):
    _no_tools(monkeypatch)
    exe = write_pe(tmp_path / "x.exe", rsrc=b"\0" * 512, signed=True)
    with pytest.raises(dp.PackageError, match="Couldn't open x.exe"):
        dp.unpack(exe, cache_root=cache)


def test_an_innoextract_too_old_for_the_installer_says_so(tmp_path, cache,
                                                         monkeypatch):
    _Tools(monkeypatch, {"innoextract"},
           lambda argv: (0, "innoextract 1.9\n") if "--version" in argv
           else (1, "Not a supported Inno Setup installer!"))
    exe = write_pe(tmp_path / "x.exe", overlay=b"Inno Setup Setup Data (")
    with pytest.raises(dp.PackageError, match="innoextract 1.9 can't read it"):
        dp.unpack(exe, cache_root=cache)


# --- picking this machine's XML -----------------------------------------------

def test_no_hardware_detected_names_the_tree(tmp_path, cache):
    download = _surface_zip(tmp_path)
    tree = dp.unpack(download, cache_root=cache)
    with pytest.raises(dp.PackageError) as exc:
        dp.pick_xml(tree, download)
    assert "No HDA codecs" in str(exc.value)
    assert "can't tell which is this laptop's" in str(exc.value)
    assert "Picking the XML by hand" in str(exc.value)
    # Nothing was matched against, so nothing says the download is wrong.
    assert "another model" not in str(exc.value)


def test_a_download_for_another_model_says_so(tmp_path, cache, fake_host):
    fake_host("/proc/asound/card0/codec#0", _CODEC.replace("10ec1284",
                                                            "104310c4"))
    download = _surface_zip(tmp_path)
    tree = dp.unpack(download, cache_root=cache)
    with pytest.raises(dp.PackageError, match="another model") as exc:
        dp.pick_xml(tree, download)
    assert "104310C4" in str(exc.value)
    assert exc.value.next_step == ""


def test_several_matching_folders_are_listed(tmp_path, cache, fake_host):
    fake_host("/proc/asound/card0/codec#0", _CODEC)
    download = tmp_path / "two.zip"
    with zipfile.ZipFile(download, "w") as zf:
        for d in ("a", "b"):
            zf.writestr(f"{d}/{_XML}", "<x/>")
    tree = dp.unpack(download, cache_root=cache)
    with pytest.raises(dp.PackageError, match="in 2 folders") as exc:
        dp.pick_xml(tree, download)
    assert str(exc.value).count("--windows ") == 2


def test_one_matching_folder_among_several_is_picked(tmp_path, cache,
                                                     fake_host):
    fake_host("/proc/asound/card0/codec#0", _CODEC)
    download = tmp_path / "fan.zip"
    with zipfile.ZipFile(download, "w") as zf:
        zf.writestr(f"mine/{_XML}", "<x/>")
        zf.writestr("other/DEV_0287_SUBSYS_17AA22E6.xml", "<x/>")
    tree = dp.unpack(download, cache_root=cache)
    assert dp.pick_xml(tree, download) == tree / "mine" / _XML


# --- through the converter ----------------------------------------------------

def test_the_converter_takes_a_download_in_place_of_the_xml(
        tmp_path, cache, fake_host, monkeypatch, silence_console, capsys):
    silence_console(console)
    fake_host("/proc/asound/card0/codec#0", _CODEC)
    monkeypatch.setenv(dp.CACHE_ENV, str(cache))
    out_dir, irs_dir = tmp_path / "out", tmp_path / "irs"
    dolby_to_easyeffects.main([
        str(_surface_zip(tmp_path)), "--skip-ee-check", "--skip-closing",
        "--output-dir", str(out_dir), "--irs-dir", str(irs_dir)])
    out = " ".join(capsys.readouterr().out.split())
    assert "Unpacked 1 tuning files into" in out
    assert "Microsoft Surface APO" in out and ", applied" in out
    presets = sorted(out_dir.glob("*.json"))
    assert presets
    assert "multiband_compressor#2" in json.loads(
        presets[0].read_text())["output"]["plugins_order"]


def test_a_dry_run_writes_only_the_cache(tmp_path, cache, fake_host,
                                         monkeypatch, silence_console, capsys):
    silence_console(console)
    fake_host("/proc/asound/card0/codec#0", _CODEC)
    monkeypatch.setenv(dp.CACHE_ENV, str(cache))
    download = _surface_zip(tmp_path)
    before = {p for p in tmp_path.rglob("*")}
    dolby_to_easyeffects.main([str(download), "--dry-run", "--skip-ee-check",
                               "--skip-closing"])
    after = {p for p in tmp_path.rglob("*")} - before
    assert after and all(cache in p.parents or p == cache for p in after)


# --- review fixes --------------------------------------------------------------

@pytest.mark.parametrize("attrs", ["A lrwxrwxrwx", "A lrwsrwxrwx",
                                   "A lrwxrwxrwt", "A crw-r--r--",
                                   "AL -rw-r--r--"])
def test_a_link_hidden_behind_permission_letters_is_still_a_link(attrs):
    """7-Zip writes the setuid, setgid and sticky bits as s/S/t/T over the
    execute letters, so a pattern on the letters missed a link that carried
    one, and an old 7-Zip writes through a link it just created."""
    out = _slt("7z", []).rstrip("\n") + (
        f"\nPath = d/{_XML}\nSize = 5\nAttributes = {attrs}\n")
    _, members = dp._parse_7z_slt(out)
    assert members[0].link


def test_a_regular_file_is_not_a_link():
    _, members = dp._parse_7z_slt(_slt("7z", [(f"d/{_XML}", 5)]))
    assert not members[0].link


def test_a_copy_is_reused_by_content_with_no_tool_installed(tmp_path, cache,
                                                           monkeypatch):
    """The SHA-256 lookup runs before any tool: a re-download of an .msi
    already unpacked needs no msitools to be reused."""
    download = tmp_path / "s.msi"
    download.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 64)
    tree = cache / "s-0123456789ab"
    tree.mkdir(parents=True)
    (tree / dp.PROVENANCE).write_text(
        f"Source: other.msi\nSHA-256: {dp._sha256(download)}\n"
        f"Rules: {dp._UNPACK_RULES}\n\nKept:\n")
    _no_tools(monkeypatch)
    assert dp.unpack(download, cache_root=cache) == tree


def test_a_listing_with_no_tuning_moves_on_to_the_next_route(
        tmp_path, cache, monkeypatch):
    """A wrapper 7z lists as an archive, holding no tuning, does not stop the
    resource route that finds the real one."""
    xml = f"inner/{_XML}"

    def handler(argv):
        target = argv[-1] if not argv[-1].startswith("@") else argv[-2]
        if argv[1:2] == ["l"]:
            if target.endswith("103"):
                return 0, _slt("7z", [(xml, 5)])
            if "-tPE" in argv:
                return 0, _slt("PE", [(".rsrc/ZIP/103", 1 << 17)])
            return 0, _slt("Cab", [("setup.dll", 9)])
        if argv[1:2] == ["x"]:
            names = Path(next(a[1:] for a in argv if a.startswith("@"))
                         ).read_text().split()
            for n in names:
                f = _dest(argv, "-o") / n
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(_7Z_MAGIC if n.endswith("103") else b"<x/>")
            return 0, ""
        return 0, "7-Zip 26.04 (x64)\n"

    _Tools(monkeypatch, {"7z"}, handler)
    tree = dp.unpack(write_pe(tmp_path / "w.exe", rsrc=_7Z_MAGIC),
                     cache_root=cache)
    assert (tree / xml).is_file()


def test_a_zip_python_cannot_decompress_says_so(tmp_path, cache, monkeypatch):
    download = _surface_zip(tmp_path)

    def deflate64(*a, **k):
        raise NotImplementedError("That compression method is not supported")
    monkeypatch.setattr(zipfile.ZipFile, "open", deflate64)
    with pytest.raises(dp.PackageError, match="Unpack it with 7z x instead"):
        dp.unpack(download, cache_root=cache)


def test_errors_carry_no_help_pointer(tmp_path, cache):
    """No option fixes any of these, so `run_guarded` must not append its
    --help line."""
    download = tmp_path / "codec.zip"
    with zipfile.ZipFile(download, "w") as zf:
        zf.writestr("Realtek/HDXRT.inf", "x")
    with pytest.raises(dp.PackageError) as exc:
        dp.unpack(download, cache_root=cache)
    assert exc.value.next_step == ""


def test_a_repository_without_the_tool_does_not_also_say_install_it(
        tmp_path, cache, monkeypatch):
    _no_tools(monkeypatch)
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.FEDORA)
    monkeypatch.setattr(packages, "unavailable", lambda key, fam: True)
    exe = write_pe(tmp_path / "x.exe", overlay=b"Inno Setup Setup Data (")
    with pytest.raises(dp.PackageError) as exc:
        dp.unpack(exe, cache_root=cache)
    assert "Install it" not in str(exc.value)
    (_s, text), = exc.value.next_step
    assert "8 and 9" in text and "copy the whole unpacked folder" in text


def test_the_folder_listing_survives_a_paste(tmp_path, monkeypatch):
    """`run_guarded` turns the home directory into `~`. Inside the quotes an
    innoextract path needs for its `$`, the shell would not expand it."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    word = dp._shell_path(tmp_path / "atmos" / "code$GetExtractPath$" / "a")
    assert word == "~/'atmos/code$GetExtractPath$/a'"
    assert str(tmp_path) not in word


def test_several_folders_are_offered_in_place_of_the_download(
        tmp_path, cache, fake_host):
    fake_host("/proc/asound/card0/codec#0", _CODEC)
    download = tmp_path / "two.zip"
    with zipfile.ZipFile(download, "w") as zf:
        for d in ("a", "b"):
            zf.writestr(f"{d}/{_XML}", "<x/>")
    tree = dp.unpack(download, cache_root=cache)
    with pytest.raises(dp.PackageError, match="in place of two.zip"):
        dp.pick_xml(tree, download)


def test_an_exact_codec_match_wins_over_a_shared_subsystem(tmp_path, cache,
                                                           fake_host):
    """Issue #33's shape: two codecs' tunings share a subsystem. Only the
    folder naming this codec's device ID is this machine's."""
    fake_host("/proc/asound/card0/codec#0", _CODEC)
    download = tmp_path / "shared.zip"
    with zipfile.ZipFile(download, "w") as zf:
        zf.writestr("alc287/DEV_0287_SUBSYS_10EC1284.xml", "<x/>")
        zf.writestr(f"alc274/{_XML}", "<x/>")
    tree = dp.unpack(download, cache_root=cache)
    assert dp.tuning_dir(tree, download) == tree / "alc274"


def test_a_display_codec_alone_is_no_verdict_on_the_download(tmp_path, cache,
                                                             fake_host):
    """A SoundWire laptop whose bus didn't enumerate shows only its HDMI
    codec: nothing a speaker tuning could match was read."""
    fake_host("/proc/asound/card0/codec#0",
              "Codec: Intel Meteorlake HDMI\nVendor Id: 0x8086281d\n"
              "Subsystem Id: 0x80860101\n")
    download = _surface_zip(tmp_path)
    tree = dp.unpack(download, cache_root=cache)
    with pytest.raises(dp.PackageError) as exc:
        dp.pick_xml(tree, download)
    assert "couldn't read this laptop's speaker hardware" in str(exc.value)
    assert "another model" not in str(exc.value)


def test_a_tree_kept_under_older_rules_is_unpacked_again(tmp_path, cache):
    download = tmp_path / "pkg.zip"
    with zipfile.ZipFile(download, "w") as zf:
        zf.writestr("Dolby/" + _XML, "<x/>")
    tree = dp.unpack(download, cache_root=cache)
    prov = tree / dp.PROVENANCE
    prov.write_text(prov.read_text().replace("Rules: ", "Rules: old-"))
    again = dp.unpack(download, cache_root=cache)
    assert "Rules: " + dp._UNPACK_RULES in (again / dp.PROVENANCE).read_text()


def test_a_tree_kept_under_older_rules_outlives_a_failed_unpack(
        tmp_path, cache, monkeypatch):
    """A run that can't unpack the download again, such as on a machine
    without the tool, leaves the tree it would have replaced."""
    download = tmp_path / "pkg.zip"
    with zipfile.ZipFile(download, "w") as zf:
        zf.writestr("Dolby/" + _XML, "<x/>")
    tree = dp.unpack(download, cache_root=cache)
    prov = tree / dp.PROVENANCE
    prov.write_text(prov.read_text().replace("Rules: ", "Rules: old-"))

    def no_tool(path, fmt):
        raise dp.PackageError("no tool here")
    monkeypatch.setattr(dp, "_open", no_tool)
    with pytest.raises(dp.PackageError, match="no tool here"):
        dp.unpack(download, cache_root=cache)
    assert (tree / "Dolby" / _XML).is_file()
