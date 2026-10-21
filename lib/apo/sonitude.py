"""HP's Bang & Olufsen speaker tuning: Sonitude (formerly Sound Research) presets.

Some HP laptops ship no Dolby file. Their speaker voicing is the Sonitude
APO's per-model preset, packed in `SEAPODAT.<bus>.FUNC_01-VEN_<v>-DEV_<d>.zip`
archives inside HP's Realtek driver download (`SNAPO64Ext`). An archive maps
each subsystem ID to a numbered preset (`SSIDMAP.bin`), and each preset holds
one `Preset.bin` per endpoint, output and content mode. This module reads each
content mode's speaker preset, its 265-point EQ curve and high-pass, into an
`ApoLayer` that stands in for the whole tuning, because there is no Dolby chain
under it. The content modes are the tuning's profiles (`PROFILES`), as a Dolby
file's music, movie and voice profiles are.

What is read, and how sure that reading is (research `r-sonitude-apo-presets`):

- `SSIDMAP.bin`: "SRSM", u32 version, u32 count, u32 record size (20 or 32),
  then count × (u32 preset number, NUL-padded "SUBSYS_xxxxxxxx[&REV_…]").
  The layout fits every archive in the one download read.
- `Preset.bin`: length-prefixed values. Two of them are u16 265 followed by
  265 float32: a dB curve per channel, read on the grid 10·2^(k/24) Hz and
  applied as cuts only (positive gains dropped). The grid, "this curve is
  the EQ" and the cuts-only reading are hypotheses. Against Windows
  recordings of two laptops, from 200 Hz to 12.5 kHz, the cuts-only curve
  is within 1.00 dB rms of what the B&O switch toggles on the ZBook (#140)
  and 1.61 dB rms of an OMEN's published Windows response; the full curve is
  1.02 and 7.15. Below the corner of the preset's high-pass
  (`HIGH_PASS_KEYS`), the curve is read as a Butterworth-4 high-pass there,
  a reading pre-registered on the ZBook's Voice mode and within 0.97 dB rms
  there, 50–160 Hz.
- The rest of `Preset.bin` is read as stage blocks of keyed values
  (`stage_values`); of those, only the high-pass is used.
- Endpoint 0, `EFX/AnyOutput/<content mode>` is read as the speaker's preset
  for that mode. `AnyContent` is read as "Music": the archive's own manifest
  names that file "<model> Music". Every speaker preset in the one download
  read holds `Movie` and `Voice` beside it. Everything else in the preset
  (dynamics, the user equaliser, the headphone and posture modes) is not
  read.

`resolve` picks the preset for this machine, by its codec IDs and whether it
runs Intel's audio DSP driver (SOF). `FORMAT` is this format's entry in
`lib/apo/discover.py` `STANDALONE`.

Stdlib-only (`tests/test_layout.py` `STDLIB_ONLY`), because discovery runs
before the generator's deferred DSP imports.
"""

from __future__ import annotations

import datetime
import math
import re
import struct
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from lib import doctor, host
from lib.apo.layer import (BUILT, NOT_REPRODUCED, ApoLayer, Stage,
                           StandaloneFormat, UnsupportedApoConfig)
from lib.hardware import codecs, speakers

# A SEAPODAT archive's name carries the bus and codec it binds, as the
# Windows hardware ID does (`INTELAUDIO\FUNC_01&VEN_10EC&DEV_0285`). 29 of
# the 33 in the one download read are named so; the other four bind Intel
# DSP-controller or DIF IDs instead, and are not read.
_ARCHIVE_RE = re.compile(
    r"SEAPODAT\.(HDAUDIO|INTELAUDIO)\.FUNC_01-VEN_([0-9A-F]{4})-DEV_"
    r"([0-9A-F]{4})\.zip", re.IGNORECASE)

# The archives come from a download anyone can rehost, so what is read out of
# them is bounded before it is read. The real ones hold at most 483 members,
# none over 8 KB, and their curves span -105.1 to +19.9 dB over all 5,912
# in the one download read. A member over the cap is refused unread, which a
# deflate bomb would otherwise expand into memory; a curve outside the range
# is refused before it reaches the FIR design, where a NaN or a value above
# ~3000 dB turns every tap into NaN.
MAX_MEMBERS = 10_000
MAX_MEMBER_BYTES = 1 << 20
MAX_CURVE_DB = 120.0

# What a damaged, encrypted or unsupported archive raises while being read.
_READ_ERRORS = (OSError, EOFError, zipfile.BadZipFile, zlib.error,
                RuntimeError, NotImplementedError, ValueError)

# The speaker presets: endpoint 0, the endpoint effect on any output, one per
# content mode. The run's profile name → the mode's folder, the default
# first: a run without --profile builds it, unless it changes nothing
# (`ApoLayer.changes_nothing`). The archive's manifest names the
# AnyContent file "<model> Music" on the one device checked (#140).
PROFILES = {"music": "AnyContent", "movie": "Movie", "voice": "Voice"}


def speaker_preset(profile: str) -> str:
    """*profile*'s speaker preset, as its path below the preset number."""
    return f"0/EFX/AnyOutput/{PROFILES[profile]}/Preset.bin"


SPEAKER_PRESET = speaker_preset("music")

CURVE_POINTS = 265


def curve_grid_hz() -> tuple[float, ...]:
    """The frequency of each curve point, read as 1/24 octave from 10 Hz."""
    return tuple(10.0 * 2.0 ** (k / 24.0) for k in range(CURVE_POINTS))


def is_archive(path: Path) -> bool:
    """Whether *path* is named like a Sonitude tuning archive."""
    return bool(_ARCHIVE_RE.fullmatch(Path(path).name))


def is_source(path: Path) -> bool:
    """Whether *path* stands in for the XML: an archive, by its name alone
    (a missing one is then said as a missing file), or a folder holding one,
    such as an unpacked driver download."""
    path = Path(path)
    return is_archive(path) or (path.is_dir() and bool(archives_in(path)))


def wanted_members(paths: list[str]) -> set[str]:
    """The archives in a driver download's file list."""
    return {p for p in paths if is_archive(PurePosixPath(p).name)}


def archives_in(source: Path) -> list[Path]:
    """*source* itself when it is an archive, else every archive under it."""
    source = Path(source)
    if source.is_file():
        return [source] if is_archive(source) else []
    return sorted(p for p in source.rglob("*") if p.is_file()
                  and is_archive(p))


# An installed Windows keeps each version of the SNAPO64Ext package in its own
# DriverStore folder, `snapo64ext.inf_amd64_<hash>`, and the package's INF
# copies its archives into that folder (DestinationDirs 13), so the folder
# holds them directly.
_PACKAGE_RE = re.compile(r"snapo64ext\.inf_amd64_.+", re.IGNORECASE)
_DRIVER_VER_RE = re.compile(r"^\s*DriverVer\s*=\s*(\d{1,2})/(\d{1,2})/(\d{4})"
                            r"\s*(?:,\s*([\d.]+))?", re.IGNORECASE | re.MULTILINE)


def driver_store_packages(store: Path) -> list[Path]:
    """The package folders directly in DriverStore folder *store* that hold
    an archive, newest first (`_driver_version`)."""
    try:
        folders = [p for p in Path(store).iterdir()
                   if _PACKAGE_RE.fullmatch(p.name) and p.is_dir()
                   and archives_in(p)]
    except OSError:
        return []
    return sorted(folders, key=_driver_version, reverse=True)


def _driver_version(folder: Path) -> tuple:
    """How new the driver version in *folder* is, to sort by: its INF's
    DriverVer date and version, the stamp the vendor gives each version.
    A folder whose INF is missing or says no date falls back to the date of
    its last change, which a copied tree resets, so the stamp comes first."""
    for inf in folder.glob("*"):
        if inf.suffix.lower() != ".inf" or not inf.is_file():
            continue
        try:
            with inf.open("rb") as f:
                raw = f.read(MAX_MEMBER_BYTES)
        except OSError:
            continue
        # INF files are UTF-16 when they start with its byte-order mark.
        text = (raw.decode("utf-16", "replace")
                if raw[:2] in (b"\xff\xfe", b"\xfe\xff")
                else raw.decode("latin-1"))
        m = _DRIVER_VER_RE.search(text)
        if m:
            month, day, year = (int(g) for g in m.groups()[:3])
            try:
                date = datetime.date(year, month, day)
            except ValueError:
                continue
            version = tuple(int(n) for n in (m.group(4) or "").split(".")
                            if n)
            return date, version
    try:
        mtime = folder.stat().st_mtime
    except OSError:
        return datetime.date.min, ()
    return datetime.date.fromtimestamp(mtime), ()


def read_ssidmap(data: bytes) -> dict[str, int]:
    """Subsystem ID ("103C860F") → preset number, from an `SSIDMAP.bin`."""
    if len(data) < 16:
        raise ValueError("SSIDMAP.bin is shorter than its header")
    magic, _version, count, size = struct.unpack_from("<4sIII", data, 0)
    if magic != b"SRSM" or size not in (20, 32) or 16 + count * size != len(data):
        raise ValueError("SSIDMAP.bin has an unknown layout")
    out = {}
    for i in range(count):
        number, raw = struct.unpack_from(f"<I{size - 4}s", data, 16 + i * size)
        m = re.match(rb"SUBSYS_([0-9A-Fa-f]{8})", raw)
        if m:
            out.setdefault(m.group(1).decode().upper(), number)
    return out


def read_curves(data: bytes, limit: int = 2) -> list[tuple[float, ...]]:
    """The first *limit* 265-point float blocks in a `Preset.bin`, in order.

    A block is u32 length, u16 point count, the points as float32, and two
    trailing bytes the length includes. Candidates are found by the count's
    bytes, so the scan stays linear in the file and stops at *limit*.
    """
    out = []
    want = 2 + 4 * CURVE_POINTS
    marker = struct.pack("<H", CURVE_POINTS)
    i = data.find(marker, 4)
    while i != -1 and len(out) < limit:
        o = i - 4
        (length,) = struct.unpack_from("<I", data, o)
        if length in (want, want + 2) and o + 4 + length <= len(data):
            out.append(struct.unpack_from(f"<{CURVE_POINTS}f", data, o + 6))
        i = data.find(marker, i + 1)
    return out


def _read(archive: zipfile.ZipFile, name: str) -> bytes:
    """Member *name*'s bytes, refused unread when it claims more than
    `MAX_MEMBER_BYTES`."""
    if archive.getinfo(name).file_size > MAX_MEMBER_BYTES:
        raise ValueError(f"{name} is larger than a tuning file")
    return archive.read(name)


# Below the high-pass corner Windows cuts as a 4th-order Butterworth there,
# not as the segment the curve stores (research `r-sonitude-apo-presets`,
# pre-registered on the ZBook's Voice mode); above it, the curve stands. The
# curve's 1/24-octave grid and the corner don't always line up: where the
# curve steps up to its in-band level within one grid point above the
# corner, the step's bottom point belongs to the stored segment and is
# replaced too, or it would be left as a one-point notch. A step is a rise
# of more than `BREAK_STEP_DB` between neighbouring points; in the one
# download read it sits within 5 % of the corner in 243 of the 245 speaker
# presets that have both.
BREAK_STEP_DB = 6.0
_GRID_STEP = 2.0 ** (1.0 / 24.0)


def _apply_high_pass(curve, corner_hz: float | str | None
                     ) -> tuple[float, ...]:
    """*curve* with its points below *corner_hz* read as a Butterworth-4
    high-pass there; unchanged when the preset's high-pass is off (None) or
    unread (`UNREAD`)."""
    if corner_hz is None or corner_hz is UNREAD:
        return tuple(curve)
    hz = curve_grid_hz()
    edge = corner_hz * (1.0 + 1e-9)
    n = sum(1 for f in hz if f <= edge)
    # The search starts at the last point below the corner: a rise further
    # down is inside the replaced segment, and a step there would hide the
    # one at the corner. A rise from that point is the step at the corner
    # itself, which leaves nothing above it to replace.
    k = next((i for i in range(max(n - 1, 0), len(curve) - 1)
              if curve[i + 1] - curve[i] > BREAK_STEP_DB), None)
    if k is not None and k >= n and hz[k] <= edge * _GRID_STEP:
        n = k + 1
    return (tuple(-10.0 * math.log10(1.0 + (corner_hz / f) ** 8)
                  for f in hz[:n]) + tuple(curve[n:]))


# Past a fixed header, Preset.bin is a run of stage blocks to the end of the
# file: i32 stage, i32 first key, i32 last key, then per key a u32 length and
# that many bytes (length 0 = absent; an empty stage has first > last). The
# section table's third entry at 0x28 gives where the blocks start. The walk
# ends exactly at the end of all 1,829 endpoint-0 presets in the one
# download read.
_SECTION_TABLE = 0x28
MAX_KEYS = 4096


def stage_values(data: bytes) -> dict[tuple[int, int], bytes]:
    """Preset.bin's values keyed by (stage, key); empty when the file
    doesn't walk to its end in that layout."""
    if len(data) < _SECTION_TABLE + 32:
        return {}
    (o,) = struct.unpack_from("<I", data, _SECTION_TABLE + 16)
    out = {}
    while o < len(data):
        if o + 12 > len(data):
            return {}
        stage, first, last = struct.unpack_from("<iii", data, o)
        o += 12
        if first > last:
            continue
        if last - first >= MAX_KEYS:
            return {}
        for key in range(first, last + 1):
            if o + 4 > len(data):
                return {}
            (n,) = struct.unpack_from("<I", data, o)
            o += 4
            if o + n > len(data):
                return {}
            if n:
                out[(stage, key)] = data[o:o + n]
            o += n
    return out if o == len(data) else {}


# Stage 5 keys 19 and 20 hold the speaker's high-pass, left and right: a flag
# byte (bit 0 on) then its corner in Hz as float32. Where the curve steps up,
# it does so within 5 % of this corner in 243 of the 245 speaker presets
# that have both, and on the ZBook's Voice mode Windows cuts as a
# Butterworth-4 there (research `r-sonitude-apo-presets`). Enabled corners
# run 40–250 Hz in the download read.
HIGH_PASS_KEYS = (19, 20)

# A channel whose high-pass field is missing or of another length: built as
# off, since nothing gives a corner, but not said to be off. Every speaker
# preset in the one download read holds both fields at five bytes.
UNREAD = "unread"


def _high_pass_hz(values: dict, key: int) -> float | str | None:
    """The corner of channel *key*'s high-pass in Hz; None when off, and
    `UNREAD` when the field can't be read. Raises ValueError for a corner no
    tuning uses."""
    payload = values.get((5, key))
    if payload is None or len(payload) != 5:
        return UNREAD
    if not payload[0] & 1:
        return None
    (corner,) = struct.unpack_from("<f", payload, 1)
    if not (math.isfinite(corner) and 10.0 <= corner <= 1000.0):
        raise ValueError(f"a high-pass at {corner} Hz")
    return corner


def _sane(curve) -> bool:
    return all(math.isfinite(v) and abs(v) <= MAX_CURVE_DB for v in curve)


def _members(archive: zipfile.ZipFile) -> dict[str, str]:
    """Archive member names keyed by their `/`-separated, case-folded form."""
    names = archive.namelist()
    if len(names) > MAX_MEMBERS:
        raise ValueError("more members than a tuning archive holds")
    return {n.replace("\\", "/").lower(): n for n in names}


# The codename is printed, so only characters a model codename uses survive:
# a member name is the one string here that reaches the terminal, and an
# escape sequence in it would reach it raw.
_LABEL_CHARS = re.compile(r"[^A-Za-z0-9 _.()-]")


def _preset_label(names: dict[str, str], number: int) -> str:
    """The model codename the archive's manifest gives preset *number*."""
    for key in names:
        if re.fullmatch(rf"{number}\.(.+)\.txt", key, re.DOTALL):
            raw = names[key].replace("\\", "/").split("/")[-1][
                len(str(number)) + 1:-4]
            return _LABEL_CHARS.sub("", raw)[:64]
    return ""


BUSES = ("HDAUDIO", "INTELAUDIO")


def _preferred_bus(hits, prefer_intelaudio: bool) -> str:
    """Which of the buses *hits* binds to use when both do."""
    order = BUSES[::-1] if prefer_intelaudio else BUSES
    return min(hits, key=order.index)


@dataclass(frozen=True)
class Bindings:
    """What *source*'s archives hold for one codec and subsystem ID."""
    # Bus → (archive, preset number), for each archive whose map names the ID.
    hits: dict[str, tuple[Path, int]]
    # Archives for this codec that could not be read, so may hold the ID.
    unreadable: tuple[Path, ...]
    # Whether any archive is for this codec at all.
    codec_seen: bool


def bindings(source: Path, codec_vendor_id: str, subsys: str) -> Bindings:
    """Which of *source*'s archives bind this codec ("10EC0285") and
    subsystem ID ("103C860F"), and to which preset.

    Windows names the codec `INTELAUDIO\\…` behind Intel's audio DSP driver
    and `HDAUDIO\\…` behind the standard one, and a download carries an
    archive for each, so one ID can bind two presets.
    """
    vendor, device = codec_vendor_id[:4].upper(), codec_vendor_id[4:].upper()
    subsys = subsys.upper()
    hits, unreadable, seen = {}, [], False
    for path in archives_in(source):
        bus, ven, dev = _ARCHIVE_RE.fullmatch(path.name).groups()
        if (ven.upper(), dev.upper()) != (vendor, device):
            continue
        seen = True
        try:
            with zipfile.ZipFile(path) as z:
                names = _members(z)
                if "ssidmap.bin" not in names:
                    unreadable.append(path)
                    continue
                number = read_ssidmap(
                    _read(z, names["ssidmap.bin"])).get(subsys)
        except _READ_ERRORS:
            unreadable.append(path)
            continue
        if number is not None:
            hits.setdefault(bus.upper(), (path, number))
    return Bindings(hits, tuple(unreadable), seen)


def find(source: Path, codec_vendor_id: str, subsys: str,
         prefer_intelaudio: bool) -> ApoLayer | None:
    """The default profile's speaker layer *source*'s archives hold for this
    codec and ID.

    When both buses bind the device, *prefer_intelaudio* picks between them.
    Returns None when no archive binds it; raises `UnsupportedApoConfig`
    when one does but its preset can't be read.
    """
    hits = bindings(source, codec_vendor_id, subsys).hits
    if not hits:
        return None
    bus = _preferred_bus(hits, prefer_intelaudio)
    return load(*hits[bus], bus, codec_vendor_id, subsys)


# How close to 0 dB a built curve stays to count as flat. In the one download
# read, the mapped speaker presets that are flat in a sound mode sit within
# 0.0013 dB of it there, and every other one cuts by 4.3 dB or more
# somewhere; 0.05 dB is well clear of both.
FLAT_DB = 0.05


def load(path: Path, number: int, bus: str, codec_vendor_id: str,
         subsys: str, profile: str = "music") -> ApoLayer:
    """Preset *number*'s speaker layer for *profile* (`PROFILES`), from the
    archive at *path*.

    Raises `UnsupportedApoConfig` when the preset can't be read.
    """
    vendor, device = codec_vendor_id[:4].upper(), codec_vendor_id[4:].upper()
    subsys = subsys.upper()
    member = f"{number}/{speaker_preset(profile)}"
    try:
        with zipfile.ZipFile(path) as z:
            names = _members(z)
            if member.lower() not in names:
                raise UnsupportedApoConfig(path, f"preset {number} has no "
                                           f"speaker file ({member})")
            data = _read(z, names[member.lower()])
            curves = read_curves(data)
            values = stage_values(data)
            corners = [_high_pass_hz(values, k) for k in HIGH_PASS_KEYS]
            codename = _preset_label(names, number)
    except _READ_ERRORS as exc:
        raise UnsupportedApoConfig(path, f"its preset {number} can't be "
                                   f"read ({exc})") from exc
    if not curves:
        raise UnsupportedApoConfig(path, f"preset {number}'s speaker file "
                                   "holds no EQ curve this tool can read")
    if not all(_sane(c) for c in curves):
        raise UnsupportedApoConfig(path, f"preset {number}'s EQ curve holds "
                                   "values no tuning uses")
    # Cuts only: Windows applies the curve's negative gains and none of its
    # boosts. A hypothesis that two devices' Windows captures support and no
    # file states (research `r-sonitude-apo-presets`).
    shaped = [_apply_high_pass(c, hz) for c, hz in zip(curves, corners)]
    left = tuple(min(v, 0.0) for v in shaped[0])
    right = (tuple(min(v, 0.0) for v in shaped[1])
             if len(curves) > 1 else left)
    # The FIR is built from these two curves alone, so flat ones and no
    # high-pass build a preset that changes nothing.
    changes_nothing = (all(abs(v) <= FLAT_DB for v in left + right)
                       and all(c is None or c is UNREAD
                               for c in corners[:len(shaped)]))
    name = f"preset {number}" + (f", {codename}" if codename else "")
    return ApoLayer(
        label=f"Sonitude speaker tuning ({name})",
        config_path=path / member,
        inf_path=path,
        hardware_id=(f"{bus}\\FUNC_01&VEN_{vendor}&DEV_{device}"
                     f"&SUBSYS_{subsys}"),
        sample_rate=48000,
        eq_left=(), eq_right=(), dynamics=(),
        stages=_stages(corners[:len(shaped)],
                       any(v > 0.0 for c in shaped for v in c), profile),
        default_on=True,
        whole_tuning=True,
        curve_hz=curve_grid_hz(),
        curve_left_db=left,
        curve_right_db=right,
        changes_nothing=changes_nothing,
    )


# The order a preset's stages run in is unread, so the built ones are listed
# first, then the rest.
_RESEARCH = "r-sonitude-apo-presets"


def _stages(corners: list[float | str | None], boosts: bool,
            profile: str = "music") -> tuple[Stage, ...]:
    """*profile*'s speaker preset stages, as the run reports them, for the
    high-pass *corners* its curves were built with (left, then right if it
    has its own; None when off, `UNREAD` when not read), and whether the
    curve *boosts* anywhere."""
    # The boosts' evidence is two laptops' Music mode only.
    tested = ("two laptops' Windows recordings have none" if profile == "music"
              else "as in Music; untested for this mode")
    # Off is built as it is, by keeping the curve; an unread field isn't.
    high_pass = (NOT_REPRODUCED if all(c is UNREAD for c in corners)
                 else BUILT)
    return (
        Stage("EQ curve", BUILT, f"its boosts left out: {tested}"
              if boosts else "", builds="eq", evidence=_RESEARCH),
        Stage("high-pass", high_pass, _corner_summary(corners),
              evidence=_RESEARCH),
        Stage("compressor and limiter", NOT_REPRODUCED, "not read yet",
              evidence=_RESEARCH),
        # Which stage adds the harmonics is a reading of the file; that the
        # tuning adds them is measured.
        Stage("psychoacoustic bass", NOT_REPRODUCED,
              "not read yet; on one laptop the tuning adds harmonics to the "
              "deepest bass, likely from this stage", evidence=_RESEARCH),
        # Not reproduced rather than built as flat: the converter doesn't
        # read it, so it can't know this preset's is flat. That it is the
        # app's user equaliser is a reading too.
        Stage("10-band equaliser", NOT_REPRODUCED,
              "not read; taken to be the app's user equaliser, flat in the "
              "one preset checked", evidence=_RESEARCH),
        Stage("outside this tuning", NOT_REPRODUCED,
              "on one laptop Windows also shapes the sound with its audio "
              "enhancements off (measured at 50 % volume); its source is "
              "unknown", evidence=_RESEARCH),
    )


def _corner_summary(corners: list[float | str | None]) -> str:
    """The high-pass stage's clause: its corner, per channel where they
    differ."""
    def one(hz):
        if hz is None:
            return "off"
        return "not read" if hz is UNREAD else f"{hz:g} Hz"
    left, right = (corners * 2)[:2]
    if left is None and right is None:
        return "off in this preset, so the curve's own low end is kept"
    if left is UNREAD and right is UNREAD:
        return "not read from this preset, so the curve's own low end is kept"
    if one(left) == one(right):
        return f"corner at {one(left)}"
    return f"corner at {one(left)} left, {one(right)} right"


def load_profiles(path: Path, number: int, bus: str, codec_vendor_id: str,
                  subsys: str) -> dict[str, ApoLayer]:
    """Preset *number*'s layer for each profile its archive holds, the
    default first.

    The default profile's file is required, so its absence raises
    `UnsupportedApoConfig` as `load` does; another mode's is left out when
    the archive has no file for it.
    """
    try:
        with zipfile.ZipFile(path) as z:
            names = _members(z)
    except _READ_ERRORS as exc:
        raise UnsupportedApoConfig(path, f"its preset {number} can't be "
                                   f"read ({exc})") from exc
    return {profile: load(path, number, bus, codec_vendor_id, subsys, profile)
            for i, profile in enumerate(PROFILES)
            if i == 0
            or f"{number}/{speaker_preset(profile)}".lower() in names}


def probe_freqs() -> list[int]:
    """The curve's own grid thinned to 1/3 octave, 20 Hz–16 kHz: where the
    correction check probes a vendor-only build."""
    return [round(10.0 * 2.0 ** (m / 3.0)) for m in range(3, 33)]


def resolve(source: Path,
            warn: Callable[[str], None]) -> dict[str, ApoLayer]:
    """The layer per profile *source*'s archives hold for this machine, the
    default profile first (`load_profiles`); raises RuntimeError when none
    binds it or the preset that does can't be read.

    Prints nothing: when two archives bind this device to different tunings,
    *warn* gets the sentence saying which was used, and on its own line the
    other archive's path, which wrapped prose would split.
    """
    try:
        return _resolve(source, warn)
    except UnsupportedApoConfig as exc:
        raise RuntimeError(f"The Sonitude tuning at "
                           f"{doctor.tilde(exc.path)} binds this device "
                           f"but can't be used: {exc.reason}.") from exc


# Two presets whose curves agree this closely are the same tuning: the
# fleet's two-bus pairs that differ at all mostly differ by float rounding.
_SAME_CURVE_DB = 0.01


def _same_tuning(a: dict[str, ApoLayer], b: dict[str, ApoLayer]) -> bool:
    """Whether two archives' presets hold the same curves in every
    profile."""
    if a.keys() != b.keys():
        return False
    for p in a:
        for x, y in ((a[p].curve_left_db, b[p].curve_left_db),
                     (a[p].curve_right_db, b[p].curve_right_db)):
            if len(x) != len(y) or any(abs(u - v) > _SAME_CURVE_DB
                                       for u, v in zip(x, y)):
                return False
    return True


def _resolve(source: Path,
             warn: Callable[[str], None]) -> dict[str, ApoLayer]:
    src = doctor.tilde(source)
    ids = [(v, s) for v, s, name in codecs.get_hda_codec_ids()
           if "HDMI" not in name.upper()
           and "DISPLAYPORT" not in name.upper()]
    # Not "run this on the device": a SoundWire machine can be on it and
    # still show no HDA codec but a display's, and the archives keyed by
    # other IDs aren't read (`_ARCHIVE_RE`).
    if not ids:
        raise RuntimeError(
            f"{src} holds Sonitude tunings, picked by an HDA audio codec's "
            "IDs, and none can be read here. On the device the tuning is "
            "for, that means its audio isn't on an HDA codec this tool "
            "reads, so it can't pick that device's tuning.")
    cards = host.path("/proc/asound/cards")
    try:
        sof = speakers._card_uses_sof(cards.read_text().splitlines())
    except OSError:
        sof = False
    unreadable: list[Path] = []
    for codec, subsys in ids:
        found = bindings(source, codec, subsys)
        unreadable += found.unreadable
        hits = found.hits
        if not hits:
            continue
        # A SOF machine runs Intel's DSP, which on Windows is the INTELAUDIO
        # bus; the other archive is for Windows' standard driver.
        picked = _preferred_bus(hits, sof)
        layers = load_profiles(*hits[picked], picked, codec, subsys)
        other = next((b for b in hits if b != picked), None)
        if other is None:
            return layers
        try:
            alt = load_profiles(*hits[other], other, codec, subsys)
        except UnsupportedApoConfig:
            return layers
        if _same_tuning(alt, layers):
            return layers
        both = (f"preset {hits[picked][1]} in its {picked} archive and "
                f"preset {hits[other][1]} in its {other} one")
        runs = "runs" if sof else "doesn't run"
        # The archive alone as the input holds one bus, so it is the one
        # read (`archives_in`).
        warn(f"{src} holds two different tunings for this device: "
             f"{both}. Using the {picked} one, because this machine {runs} "
             "Intel's audio DSP driver (SOF). If the speakers' hardware ID in "
             f"Windows' Device Manager starts with {other}, rebuild with that "
             "archive alone as the input:\n"
             f"  {doctor.tilde(hits[other][0])}")
        return layers
    if unreadable:
        raise RuntimeError(
            f"{doctor.tilde(unreadable[0])} can't be read as a Sonitude "
            "tuning archive, so whether it holds this device's tuning is "
            "unknown. Try a fresh copy of the download.")
    said = ", ".join(f"{c}:{s}" for c, s in ids)
    # One archive holds one bus and codec, so the right download's other
    # archive may be the one that maps this device.
    if source.is_file():
        raise RuntimeError(
            f"{src} maps no preset to this device's audio hardware "
            f"(codec:subsystem {said}). If it came from this model's "
            "download, pass the whole download instead.")
    raise RuntimeError(
        f"{src} holds Sonitude tunings, but none for this device's audio "
        f"hardware (codec:subsystem {said}), so it is likely another "
        "model's download. Use the audio driver download its manufacturer "
        "lists for this model.")


# This format's entry in `lib/apo/discover.py` `STANDALONE`.
FORMAT = StandaloneFormat(
    is_source=is_source,
    wanted_members=wanted_members,
    driver_store_packages=driver_store_packages,
    resolve=resolve,
    # "Dolby" would mislabel a preset built from another engine's tuning.
    prefix="Sonitude",
    owner="Sonitude",
    heading="Internal speakers (the output this preset is for)",
    probe_freqs=probe_freqs,
    # Built as music is, the one mode a listener has heard (research
    # `r-sonitude-apo-presets`).
    experimental=("movie", "voice"),
)
