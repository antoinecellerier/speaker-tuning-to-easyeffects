"""Unpack a vendor's audio driver download, keeping only what the readers read.

The converters take the download itself in place of the tuning XML: an Inno
Setup `.exe` (Lenovo), a 7-Zip self-extractor (Framework), a signed `.exe`
carrying an archive in its resources (ASUS), a Windows Installer `.msi`
(Surface) or a `.zip` (Samsung). The archive is listed first. Only the files
the readers open are kept, at their archive paths, in
``driver-cache/<name>-<sha256 prefix>/`` beside a ``PROVENANCE.txt``: the DAX3
tuning XMLs (`lib.dax.discover.is_dolby_tuning_filename`) and what a vendor
APO finder reads (`lib.apo.discover.wanted_members`). Each reader owns its
predicate, so a new format adds its own and this module never learns it. A
`.msi` is the exception to extracting only those: `msiextract` has no member
filter, so it is unpacked in full beside the tree and pruned.

The first bytes say only zip, msi or exe. Which kind of `.exe` it is, the
tools decide, by whether they can list it, and a listing with no tuning in
it moves on to the next tool. Byte signatures past the header only pick
which tool a missing-tool error names, because they misroute: an ASUS `.exe`
embeds an Inno loader in its resources, and plain `7z` opens that loader
instead of the archive beside it.

A download with no tuning XML in its listing leaves nothing in the cache;
the ASUS route's resources go to a temporary directory that is removed. A
tree is written to a hidden ``.partial-*`` directory and renamed into place
once its ``PROVENANCE.txt`` is written, so a tree without one is never ours:
it is neither reused nor removed. A ``.partial-*`` directory over an hour old
is from a killed run, and the next unpack removes it.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import io
import mmap
import os
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from lib import console, doctor, packages, tool_env
from lib.apo import discover as apo_discover
from lib.apo import sonitude
from lib.dax import discover
from lib.hardware import codecs
from lib.paths import REPO_ROOT

# Set by the test suite, so no test writes into the checkout's driver-cache/,
# which bare runs and the corpus tier walk.
CACHE_ENV = "ATMOS_DRIVER_CACHE"
PROVENANCE = "PROVENANCE.txt"
_PARTIAL_PREFIX = ".partial-"
# A partial tree this old is from a run that was killed: an interrupted .msi
# unpack would otherwise leave gigabytes in a hidden directory.
_STALE_PARTIAL_S = 3600

# Where the doc that covers every download format lives; named, not linked
# (.claude/rules/user-messages.md).
_GUIDE = ('the "From a vendor driver download" section of '
          "docs/getting-the-xml.md")

_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
_7Z_MAGIC = b"7z\xbc\xaf'\x1c"
_INNO_MARK = b"Inno Setup Setup Data ("

# A driver package's tuning files are a few MB, so these only stop a
# crafted zip, whose headers are trusted for the sizes extraction stops at.
_ZIP_MAX_MEMBERS = 200_000
_ZIP_MAX_WANTED_BYTES = 1 << 30

_FORMAT_LABELS = {
    "inno": "Inno Setup installer",
    "7z": "self-extracting archive 7-Zip opens",
    "pe-resource": "installer with an archive inside",
    "msi": "Windows Installer package (.msi)",
    "zip": "zip archive",
}


class PackageError(RuntimeError):
    """A download this can't use. The message says why and ends on what to
    do, so `console.run_guarded` adds no --help pointer under it: no option
    fixes any of these. A missing tool sets `next_step` to its install
    command."""

    next_step = ""


def default_cache_root() -> Path:
    return Path(os.environ.get(CACHE_ENV) or REPO_ROOT / "driver-cache")


def kind(path: Path) -> str | None:
    """``"zip"``, ``"msi"`` or ``"exe"`` for a download, else None.

    None also for a file that can't be read and for a directory, so anything
    that is not a download takes the XML path exactly as before.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return None
    if head.startswith(_ZIP_MAGIC):
        return "zip"
    if head == _OLE_MAGIC:
        return "msi"
    if head[:2] == b"MZ":
        return "exe"
    return None


# --- listing ---------------------------------------------------------------

@dataclass
class _Member:
    path: str               # the archive's own path, "/"-separated
    size: int | None = None
    link: bool = False


@dataclass
class _Opened:
    """A download a tool could list, and how to extract part of it."""
    fmt: str
    tool: str
    members: list[_Member]
    extract: object         # (partial: Path, wanted: list[str]) -> None
    full_command: str
    full_size: int | None = None   # bytes a full unpack writes first (.msi)
    cleanup: list = field(default_factory=list)


def _sevenzip() -> str | None:
    """The 7-Zip binary on PATH: Debian 12's `7zip` and nixpkgs' `_7zz` ship
    only `7zz`, the others `7z`."""
    for name in ["7z", "7zz"]:
        if tool_env.which(name):
            return name
    return None


def _tool_version(argv: list[str]) -> str:
    try:
        r = tool_env.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    lines = [ln.strip() for ln in (r.stdout + r.stderr).splitlines()
             if ln.strip()]
    # "7-Zip 26.04 (x64) : Copyright (c) …", "7-Zip (z) 22.01 (x64) : …":
    # the name and version, without the banner.
    if not lines:
        return ""
    head = re.split(r" : | \(C\)", lines[0])[0]
    return " ".join(re.sub(r"\([^)]*\)", " ", head).split())


# What innoextract said the last time it could not list a download, for the
# error that follows when the download still looks like an Inno installer.
_inno_refusal = ""


def _inno_open(path: Path) -> _Opened | None:
    """The Inno Setup listing of *path*, or None when innoextract can't read
    it (absent, or not an Inno installer)."""
    global _inno_refusal
    if not tool_env.which("innoextract"):
        return None
    r = tool_env.run(["innoextract", "--list", "-c", "0", "-p", "0",
                      str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        _inno_refusal = " ".join((r.stderr or r.stdout or "").split())[:200]
        return None
    members = {}
    for line in r.stdout.splitlines():
        m = re.match(r'\s*- "(.+?)"', line)
        if m:
            members[m.group(1)] = _Member(m.group(1))

    def extract(partial: Path, wanted: list[str]) -> None:
        # One -I per file, anchored with a leading "/": innoextract matches
        # whole path components anywhere in the path, ignoring case.
        argv = ["innoextract", "-s", "-c", "0", "-p", "0", "-d", str(partial)]
        for p in wanted:
            argv += ["-I", "/" + p]
        _check(tool_env.run(argv + [str(path)], capture_output=True,
                            text=True), "innoextract", path)

    return _Opened("inno", _tool_version(["innoextract", "--version"]),
                   list(members.values()), extract,
                   f"innoextract -d DIR {shlex.quote(path.name)}")


def _parse_7z_slt(out: str) -> tuple[str | None, list[_Member]]:
    """The type of the archive `7z l -slt` lists, and its file members.

    7-Zip may open an archive nested in the file, and then the members are
    the nested one's: Framework's SFX bundle lists as a PE, then `----`, then
    its 7z overlay. So the last `Type` before the members is the one that
    counts, and `7z x` extracts from that same archive."""
    head, sep, body = out.partition("\n----------\n")
    types = re.findall(r"^Type = (.+)$", head, re.MULTILINE)
    kind_ = types[-1].strip() if types else None
    members = []
    for block in (body.split("\n\n") if sep else []):
        rec = dict(re.findall(r"^(.+?) = (.*)$", block, re.MULTILINE))
        if "Path" not in rec:
            continue
        attrs = rec.get("Attributes", "")
        if rec.get("Folder") == "+" or attrs.startswith("D"):
            continue
        size = rec.get("Size", "")
        members.append(_Member(rec["Path"],
                               int(size) if size.isdigit() else None,
                               link=_is_special(rec, attrs)))
    return kind_, members


def _is_special(rec: dict[str, str], attrs: str) -> bool:
    """Whether a `7z -slt` member is anything but a regular file: a link,
    device or fifo. Read off the POSIX type character, never the permission
    letters, which 7-Zip rewrites as s/S/t/T for the setuid, setgid and
    sticky bits, so a pattern on them misses a link that carries one. A
    Windows reparse point (L) or any link property counts too."""
    win, _, posix = attrs.partition(" ")
    if posix and posix[0] != "-":
        return True
    if "L" in win:
        return True
    return any(rec.get(k) for k in ("Symbolic Link", "Hard Link", "Link"))


def _7z_list(exe: str, path: Path, pe: bool = False):
    argv = [exe, "l", "-slt", "-y"] + (["-tPE"] if pe else []) + [str(path)]
    r = tool_env.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        return None, []
    return _parse_7z_slt(r.stdout)


def _7z_extract(exe: str, archive: Path, partial: Path, wanted: list[str],
                pe: bool = False) -> None:
    # A list file rather than arguments: a Lenovo package holds hundreds of
    # tunings. -spd: names are literal, never wildcards.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt",
                                     delete=False) as fh:
        fh.write("\n".join(wanted) + "\n")
    try:
        argv = ([exe, "x", "-y", "-spd", "-scsUTF-8"]
                + (["-tPE"] if pe else [])
                + [f"-o{partial}", str(archive), f"@{fh.name}"])
        _check(tool_env.run(argv, capture_output=True, text=True), exe,
               archive)
    finally:
        os.unlink(fh.name)


def _sevenzip_open(path: Path) -> _Opened | None:
    """A 7-Zip SFX: an `.exe` that plain `7z` lists as an archive."""
    exe = _sevenzip()
    if exe is None:
        return None
    outer, members = _7z_list(exe, path)
    if not outer or outer == "PE":
        return None
    return _Opened(
        "7z", _tool_version([exe]), members,
        lambda partial, wanted: _7z_extract(exe, path, partial, wanted),
        f"{exe} x -oDIR {shlex.quote(path.name)}")


def _resource_open(path: Path) -> _Opened | None:
    """A PE carrying archives in its resources (ASUS). Plain `7z` may open
    an installer embedded there rather than the PE itself, so the PE's own
    sections are listed, with `-tPE`."""
    exe = _sevenzip()
    if exe is None:
        return None
    _, sections = _7z_list(exe, path, pe=True)
    candidates = [m.path for m in sections
                  if m.path.startswith(".rsrc/") and (m.size or 0) >= 1 << 16]
    if not candidates:
        return None
    scratch = Path(tempfile.mkdtemp(prefix="driver-package-"))
    try:
        _7z_extract(exe, path, scratch, candidates, pe=True)
        inner = []
        for rel in candidates:
            f = scratch / rel
            try:
                with open(f, "rb") as fh:
                    magic = fh.read(6)
            except OSError:
                continue
            if magic == _7Z_MAGIC or magic.startswith(_ZIP_MAGIC):
                kind_, inner_members = _7z_list(exe, f)
                if kind_ and kind_ != "PE":
                    inner.append((rel, f, inner_members))
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    if not inner:
        shutil.rmtree(scratch, ignore_errors=True)
        return None

    def extract(partial: Path, wanted: list[str]) -> None:
        wanted_set = set(wanted)
        for _rel, f, inner_members in inner:
            mine = [m.path for m in inner_members if m.path in wanted_set]
            if mine:
                _7z_extract(exe, f, partial, mine)

    names = " and ".join(rel for rel, _f, _m in inner)
    return _Opened(
        "pe-resource", _tool_version([exe]),
        [m for _rel, _f, ms in inner for m in ms], extract,
        f"{exe} x -tPE -oDIR {shlex.quote(path.name)} {names}, then "
        f"{exe} x -oDIR on each",
        cleanup=[lambda: shutil.rmtree(scratch, ignore_errors=True)])


def _msi_open(path: Path) -> _Opened:
    if not tool_env.which("msiextract"):
        raise _missing_tool(packages.MSITOOLS, "msiextract", path)
    r = tool_env.run(["msiextract", "--list", str(path)],
                     capture_output=True, text=True)
    if r.returncode != 0:
        raise PackageError(f"{path.name} is not a Windows Installer package "
                           "(.msi) msiextract can read.")
    members = [_Member(ln.strip()) for ln in r.stdout.splitlines()
               if ln.strip()]

    def extract(partial: Path, wanted: list[str]) -> None:
        # msiextract has no member filter: unpack all of it beside the tree,
        # then keep only what was asked for.
        full = partial / ".full"
        try:
            _check(tool_env.run(["msiextract", "-C", str(full), str(path)],
                                capture_output=True, text=True),
                   "msiextract", path)
            for rel in wanted:
                src, dest = full / rel, partial / rel
                if src.is_file() and not src.is_symlink():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(src, dest)
        finally:
            shutil.rmtree(full, ignore_errors=True)

    return _Opened("msi", "msiextract "
                   + _tool_version(["msiextract", "--version"]),
                   members, extract,
                   f"msiextract -C DIR {shlex.quote(path.name)}",
                   full_size=_msi_installed_size(path))


def _msi_installed_size(path: Path) -> int | None:
    """What a full unpack writes, from the package's File table."""
    if not tool_env.which("msiinfo"):
        return None
    r = tool_env.run(["msiinfo", "export", str(path), "File"],
                     capture_output=True, text=True)
    if r.returncode != 0:
        return None
    total = 0
    # Tab-separated, after three header rows; FileSize is the fourth column.
    for line in r.stdout.splitlines()[3:]:
        cols = line.split("\t")
        if len(cols) > 3 and cols[3].isdigit():
            total += int(cols[3])
    return total or None


def _zip_open(path: Path) -> _Opened:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as exc:
        raise PackageError(f"{path.name} is not a zip archive this can read: "
                           f"{exc}.") from exc
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > _ZIP_MAX_MEMBERS:
        zf.close()
        raise PackageError(f"{path.name} lists {len(infos)} files, more than "
                           "a driver download holds; not unpacking it.")
    by_name = {i.filename.replace("\\", "/"): i for i in infos}
    members = [_Member(name, i.file_size,
                       link=(i.external_attr >> 16) & 0o170000 == 0o120000)
               for name, i in by_name.items()]

    def extract(partial: Path, wanted: list[str]) -> None:
        with zf:
            for rel in wanted:
                dest = partial / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with zf.open(by_name[rel]) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
                    raise PackageError(f"{path.name}: {rel} is damaged: "
                                       f"{exc}.") from exc
                except NotImplementedError as exc:
                    # A compression method Python lacks, such as Deflate64,
                    # which Windows' own zipper uses for large archives.
                    raise PackageError(
                        f"{path.name} uses a compression Python can't read "
                        f"({exc}). Unpack it with 7z x instead, as {_GUIDE} "
                        "describes.") from exc

    return _Opened("zip", "Python zipfile", members, extract,
                   f"unzip -d DIR {shlex.quote(path.name)}",
                   cleanup=[zf.close])


def _check(r, tool: str, path: Path) -> None:
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise PackageError(f"{tool} failed on {path.name}: "
                           + " ".join(err))


# --- which tool a download needs ------------------------------------------

def _pe_triage(path: Path) -> str | None:
    """The unpacker an `.exe` most plausibly needs: `packages.INNOEXTRACT`,
    `packages.SEVENZIP`, or None when nothing suggests either.

    Reads what follows the PE image, its overlay, which ends where an
    Authenticode signature starts, since signed downloads carry one
    there. A 7z SFX starts its overlay with the 7z signature; an Inno
    installer's overlay holds its setup data. A PE with neither but a 7z or
    zip archive in its `.rsrc` section is the ASUS shape.
    """
    try:
        with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0,
                                              access=mmap.ACCESS_READ) as m:
            lfanew, = struct.unpack_from("<I", m, 0x3C)
            if m[lfanew:lfanew + 4] != b"PE\0\0":
                return None
            nsec, = struct.unpack_from("<H", m, lfanew + 6)
            opt_size, = struct.unpack_from("<H", m, lfanew + 20)
            opt = lfanew + 24
            magic, = struct.unpack_from("<H", m, opt)
            dirs = opt + (96 if magic == 0x10B else 112)
            # Data directory 4, the certificate table, holds a file offset.
            cert_off, cert_size = struct.unpack_from("<II", m, dirs + 8 * 4)
            end, rsrc = 0, None
            for i in range(nsec):
                sec = opt + opt_size + 40 * i
                raw_size, raw_ptr = struct.unpack_from("<II", m, sec + 16)
                end = max(end, raw_ptr + raw_size)
                if m[sec:sec + 8].rstrip(b"\0") == b".rsrc":
                    rsrc = (raw_ptr, raw_ptr + raw_size)
            stop = cert_off if cert_size and cert_off >= end else len(m)
            if m.find(_7Z_MAGIC, end, min(stop, end + (1 << 16))) >= 0:
                return packages.SEVENZIP
            if m.find(_INNO_MARK, end, stop) >= 0:
                return packages.INNOEXTRACT
            if rsrc and (m.find(_7Z_MAGIC, *rsrc) >= 0
                         or m.find(_ZIP_MAGIC, *rsrc) >= 0):
                return packages.SEVENZIP
    except (OSError, ValueError, struct.error):
        return None
    return None


_TOOL_NAMES = {packages.INNOEXTRACT: "innoextract",
               packages.SEVENZIP: "7-Zip",
               packages.MSITOOLS: "msitools"}

# Where `unavailable` says dnf has no package (packages.fedoraproject.org,
# 2026-10-10): innoextract and msitools are in EPEL 8 and 9 and nowhere for
# EL10; 7zip is in EPEL 9 and 10, and EL8 has only p7zip.
_NOT_IN_REPOS = {
    packages.INNOEXTRACT: "on RHEL, Rocky and Alma 8 and 9 it comes from "
                          "EPEL, and version 10 has none",
    packages.SEVENZIP: "on RHEL, Rocky and Alma 9 and 10 it comes from EPEL",
    packages.MSITOOLS: "on RHEL, Rocky and Alma 8 and 9 it comes from EPEL, "
                       "and version 10 has none",
}

# Where no package can be installed. The whole tree, not the XML alone: a
# Surface's own speaker tuning is found beside the Dolby folder.
_ELSEWHERE = ("Otherwise, unpack the download on another machine, copy the "
              "whole unpacked folder here, and pass the XML inside it.")


def _missing_tool(key: str, binaries: str, path: Path) -> PackageError:
    name = _TOOL_NAMES[key]
    what = name if name == binaries else f"{name} ({binaries})"
    fam = packages.family()
    if fam and packages.names([key], fam) and packages.unavailable(key, fam):
        exc = PackageError(f"Unpacking {path.name} needs {what}, which is not "
                           "installed.")
        manager = packages.package_manager(fam)
        exc.next_step = (("cta", f"{manager} has no {name} package here: "
                                 f"{_NOT_IN_REPOS[key]}. {_ELSEWHERE}"),)
        return exc
    exc = PackageError(f"Unpacking {path.name} needs {what}, which is not "
                       "installed. Install it, then run the same command "
                       "again.")
    exc.next_step = packages.install_steps([key], see=_GUIDE)
    return exc


def _is_tuning(name: str) -> bool:
    """A Dolby tuning XML, or the archive of a vendor-only tuning (HP's
    Sonitude presets, `lib/apo/sonitude.py`) that ships in place of one."""
    return (discover.is_dolby_tuning_filename(name)
            or sonitude.is_archive(name))


def _has_tuning(opened: _Opened) -> bool:
    return any(_is_tuning(PurePosixPath(m.path).name)
               for m in opened.members)


def _open(path: Path, fmt: str) -> _Opened:
    if fmt == "zip":
        return _zip_open(path)
    if fmt == "msi":
        return _msi_open(path)
    global _inno_refusal
    _inno_refusal = ""
    # The first route that lists a tuning wins. One that lists the download
    # but no tuning is kept as the answer only if no later route does
    # better, so a wrapper that lists as an archive still reaches the one
    # inside it.
    first = None
    for route in (_inno_open, _sevenzip_open, _resource_open):
        opened = route(path)
        if opened is None:
            continue
        if _has_tuning(opened):
            if first is not None:
                for done in first.cleanup:
                    done()
            return opened
        if first is None:
            first = opened
        else:
            for done in opened.cleanup:
                done()
    if first is not None:
        return first
    likely = _pe_triage(path)
    if likely == packages.INNOEXTRACT and not tool_env.which("innoextract"):
        raise _missing_tool(likely, "innoextract", path)
    if likely == packages.SEVENZIP and _sevenzip() is None:
        raise _missing_tool(likely, "7z or 7zz", path)
    if likely == packages.INNOEXTRACT:
        # Installed, yet it can't list it: an innoextract older than the Inno
        # Setup that built this installer, or a damaged download.
        version = _tool_version(["innoextract", "--version"])
        said = f" It said: {_inno_refusal}" if _inno_refusal else ""
        raise PackageError(f"{path.name} looks like an Inno Setup installer, "
                           f"but {version or 'innoextract'} can't read it.{said}"
                           " A newer innoextract may, or the download may be "
                           "damaged: download it again.")
    raise PackageError(
        f"Couldn't open {path.name}: it is not an installer or archive this "
        "can unpack. Check that it is your laptop's audio driver download, "
        f"an .exe, .msi or .zip, as {_GUIDE} describes.")


# --- the cache --------------------------------------------------------------

def _safe_stem(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", path.stem)[:80] or "download"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_provenance(tree: Path) -> dict[str, str]:
    out = {}
    try:
        text = (tree / PROVENANCE).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        if line == "Kept:":
            break
        key, sep, value = line.partition(": ")
        if sep:
            out[key] = value
    return out


def _trees(cache: Path):
    """Each tree this module made under *cache*, with its provenance."""
    try:
        entries = sorted(cache.iterdir())
    except OSError:
        return
    for d in entries:
        if d.name.startswith(".") or not d.is_dir():
            continue
        prov = _read_provenance(d)
        if prov:
            yield d, prov


def _sweep_stale_partials(cache: Path) -> None:
    try:
        entries = list(cache.iterdir())
    except OSError:
        return
    now = time.time()
    for d in entries:
        try:
            if (d.name.startswith(_PARTIAL_PREFIX) and d.is_dir()
                    and now - d.stat().st_mtime > _STALE_PARTIAL_S):
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def _unsafe(member: _Member) -> bool:
    p = PurePosixPath(member.path)
    return (member.link or p.is_absolute() or ".." in p.parts
            or bool(p.parts and ":" in p.parts[0]))


def _has_symlink(tree: Path) -> bool:
    for dirpath, dirnames, filenames in os.walk(tree):
        for name in dirnames + filenames:
            if os.path.islink(os.path.join(dirpath, name)):
                return True
    return False


def _provenance_text(path: Path, st, sha: str, opened: _Opened,
                     kept: list[str]) -> str:
    lines = [
        "Unpacked by speaker-tuning-to-easyeffects from a vendor driver "
        "download.",
        f"Partial extraction: only the {len(kept)} files listed under Kept "
        "were kept. This is not the whole download.",
        "",
        f"Source: {path.name}",
        f"Size: {st.st_size}",
        f"Modified-ns: {st.st_mtime_ns}",
        f"SHA-256: {sha}",
        f"Format: {_FORMAT_LABELS[opened.fmt]}",
        f"Tool: {opened.tool}",
        f"Unpacked: {datetime.date.today().isoformat()}",
        "",
        "Kept:",
        *(f"  {p}" for p in kept),
        "",
        "To unpack the whole download yourself:",
        f"  {opened.full_command}",
    ]
    return "\n".join(lines) + "\n"


def unpack(path: Path, cache_root: Path | None = None) -> Path:
    """The tree holding *path*'s tuning files, unpacking it if needed.

    Reused when a tree already records this download, by name, size and
    modification time, or failing that by SHA-256, so a re-download or a
    copy makes no second tree. Raises `PackageError` for a download with no
    tuning XML (nothing is written), one no installed tool can open, or a
    member path that would land outside the tree.
    """
    path = Path(path)
    cache = Path(cache_root) if cache_root else default_cache_root()
    st = path.stat()
    for tree, prov in _trees(cache):
        if (prov.get("Source") == path.name
                and prov.get("Size") == str(st.st_size)
                and prov.get("Modified-ns") == str(st.st_mtime_ns)):
            console.cprint("dim", "Reusing the tuning files unpacked "
                           f"earlier: {doctor.tilde(tree)}")
            return tree

    fmt = kind(path)
    if fmt is None:
        raise PackageError(f"{path.name} is not a driver download.")
    # Before any tool runs, so a copy of a download already unpacked is
    # reused on a machine that lacks the tool.
    sha = _sha256(path)
    for tree, prov in _trees(cache):
        if prov.get("SHA-256") == sha:
            console.cprint("dim", "Reusing the tuning files unpacked "
                           f"earlier: {doctor.tilde(tree)}")
            return tree
    opened = _open(path, fmt)
    try:
        return _unpack_opened(path, st, sha, cache, opened)
    finally:
        for done in opened.cleanup:
            done()


def _unpack_opened(path: Path, st, sha: str, cache: Path,
                   opened: _Opened) -> Path:
    label = _FORMAT_LABELS[opened.fmt]
    if any(_unsafe(m) for m in opened.members):
        raise PackageError(f"{path.name} holds a link or a path that leads "
                           "outside its own folder; not unpacking it.")
    names = [m.path for m in opened.members]
    tuning = [p for p in names if _is_tuning(PurePosixPath(p).name)]
    if not tuning:
        raise PackageError(
            f"{path.name} opened ({label}), but it holds no Dolby tuning "
            "file, so it is not the download this needs. Look for your "
            "model's Dolby or audio driver download instead, as "
            f"{_GUIDE} describes.")
    wanted = sorted(set(tuning) | apo_discover.wanted_members(names))

    sizes = {m.path: m.size for m in opened.members}
    need = opened.full_size or sum(sizes.get(p) or 0 for p in wanted)
    if opened.fmt == "zip" and need > _ZIP_MAX_WANTED_BYTES:
        raise PackageError(f"{path.name}'s tuning files claim {need} bytes, "
                           "more than a driver download holds; not unpacking "
                           "it.")
    cache.mkdir(parents=True, exist_ok=True)
    _sweep_stale_partials(cache)
    free = shutil.disk_usage(cache).free
    # A margin over the listed sizes, for the filesystem's own overhead.
    need += 64 << 20
    if free < need:
        raise PackageError(
            f"Unpacking {path.name} needs about {need >> 20} MB free in "
            f"{doctor.tilde(cache)}, which has {free >> 20} MB. Free some "
            "space there and run the same command again.")

    console.cprint("dim", f"Unpacking {path.name} ({label}, {opened.tool})…")
    partial = Path(tempfile.mkdtemp(dir=cache, prefix=_PARTIAL_PREFIX))
    # mkdtemp's 0700, widened to what the user's umask gives any new folder.
    umask = os.umask(0)
    os.umask(umask)
    os.chmod(partial, 0o777 & ~umask)
    try:
        opened.extract(partial, wanted)
        if _has_symlink(partial):
            raise PackageError(f"{path.name} unpacked a link; not using it.")
        kept = sorted(str(p.relative_to(partial).as_posix())
                      for p in partial.rglob("*") if p.is_file())
        if not any(_is_tuning(PurePosixPath(p).name) for p in kept):
            raise PackageError(f"Unpacking {path.name} produced none of the "
                               "tuning files its listing names.")
        (partial / PROVENANCE).write_text(
            _provenance_text(path, st, sha, opened, kept), encoding="utf-8")
        dest = cache / f"{_safe_stem(path)}-{sha[:12]}"
        try:
            os.rename(partial, dest)
        except OSError:
            # Another run finished first, or something else holds the name.
            if not _read_provenance(dest).get("SHA-256") == sha:
                raise PackageError(f"{doctor.tilde(dest)} already exists, "
                                   f"with no {PROVENANCE} from this tool for "
                                   "this download. Move it away and run the "
                                   "same command again.")
            shutil.rmtree(partial, ignore_errors=True)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    n = sum(_is_tuning(PurePosixPath(p).name) for p in kept)
    console.cprint("ok", f"Unpacked {n} tuning files into {doctor.tilde(dest)}")
    return dest


# --- picking this machine's XML --------------------------------------------

def _shell_path(path: Path) -> str:
    """*path* as a shell word that survives a paste. Under the home
    directory it is written ``~/'rest'``: the ``~`` stays outside the quotes
    so the shell expands it, and `console.run_guarded`'s home-to-``~``
    rewrite finds no home path inside them to break. Innoextract's trees all
    need the quotes, for the ``$`` in ``code$GetExtractPath$``."""
    try:
        rel = Path(path).relative_to(Path.home())
    except ValueError:
        return shlex.quote(str(path))
    return "~/" + shlex.quote(str(rel))


def _speaker_ids_read() -> bool:
    """Whether the run read IDs a speaker tuning could match: an HDA codec
    other than a display's, or SoundWire devices with the PCI subsystem their
    tunings are named by."""
    if any("HDMI" not in name.upper() and "DISPLAYPORT" not in name.upper()
           for _v, _s, name in codecs.get_hda_codec_ids()):
        return True
    return bool(codecs.get_soundwire_ids()
                and codecs.get_pci_audio_subsystem())


def _unmatched(download: Path, tree: Path, detail: str = "") -> PackageError:
    """None of *tree*'s tunings is this machine's. Only a run that read the
    speaker's IDs can say the download is likely another model's."""
    by_hand = ('pick the XML by hand, as the "Picking the XML by hand" '
               "section of docs/getting-the-xml.md describes, and pass it "
               "directly.")
    if _speaker_ids_read():
        advice = ("None matches this laptop's audio hardware, so the download "
                  "is likely for another model: get your own model's. If it "
                  f"is yours, {by_hand}")
    else:
        advice = ("The run couldn't read this laptop's speaker hardware, so "
                  f"it can't tell which is this laptop's: {by_hand}")
    lead = f"{detail}\n" if detail else ""
    return PackageError(f"{lead}The tuning files from {download.name} are in "
                        f"{doctor.tilde(tree)}. {advice}")


def _quiet_hits(dirs: list[Path], best_guess: bool) -> list[Path]:
    hits = []
    for d in dirs:
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                discover.find_tuning_xml(d, best_guess=best_guess)
            except (FileNotFoundError, RuntimeError):
                continue
        hits.append(d)
    return hits


def tuning_dir(tree: Path, download: Path, best_guess: bool = False) -> Path:
    """The folder of *tree* that holds this machine's tuning.

    One folder is the answer as it is. Among several, an exact codec match
    by filename wins (`discover.dirs_with_an_exact_match`), then a subsystem
    match, then a quiet trial of each folder through `find_tuning_xml`'s own
    tiers; a `--best-guess` manufacturer guess counts only when nothing else
    does. Shared with `tools/fetch_driver`, so the converter and the fetcher
    pick the same folder from the same tree.
    """
    dirs = discover.walk_for_dolby_xml_dirs(tree)
    if not dirs:
        raise PackageError(f"{doctor.tilde(tree)} holds no tuning XML; "
                           f"remove it and pass {download.name} again.")
    if len(dirs) == 1:
        return dirs[0]
    exact = discover.dirs_with_an_exact_match(dirs)
    if len(exact) == 1:
        return exact[0]
    if exact:
        raise _several(exact, download)
    # A subsystem alone can belong to another codec, so several such folders
    # go on to the trial rather than being offered as matches.
    by_subsys = discover.dirs_for_this_machine(dirs)
    if len(by_subsys) == 1:
        return by_subsys[0]
    hits = _quiet_hits(by_subsys or dirs, best_guess=False)
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise _several(hits, download)
    if best_guess:
        hits = _quiet_hits(dirs, best_guess=True)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise _several(hits, download)
    raise _unmatched(download, tree,
                     f"None of the {len(dirs)} tuning folders in "
                     f"{download.name} names this laptop's audio hardware.")


def _several(hits: list[Path], download: Path) -> PackageError:
    listing = "\n".join(f"  --windows {_shell_path(d)}" for d in hits)
    return PackageError(
        f"{download.name} holds tunings that match this laptop in "
        f"{len(hits)} folders. Run the same command again with one of these "
        f"in place of {download.name}:\n{listing}")


def pick_xml(tree: Path, download: Path, best_guess: bool = False) -> Path:
    """This machine's tuning XML in an unpacked *tree*."""
    folder = tuning_dir(tree, download, best_guess)
    try:
        return discover.find_tuning_xml(folder, best_guess=best_guess)
    except (FileNotFoundError, RuntimeError) as exc:
        raise _unmatched(download, tree, str(exc)) from exc
