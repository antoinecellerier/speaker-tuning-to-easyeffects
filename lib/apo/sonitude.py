"""HP's Bang & Olufsen speaker tuning: Sonitude (formerly Sound Research) presets.

HP ships no Dolby file on these laptops. The speaker voicing is the Sonitude
APO's per-model preset, packed in `SEAPODAT.<bus>.FUNC_01-VEN_<v>-DEV_<d>.zip`
archives inside HP's Realtek driver download (`SNAPO64Ext`). An archive maps
each subsystem ID to a numbered preset (`SSIDMAP.bin`), and each preset holds
one `Preset.bin` per endpoint, output and content mode. This module reads the
speaker preset's 265-point EQ curve into an `ApoLayer` that stands in for the
whole tuning, because there is no Dolby chain under it.

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
  1.02 and 7.15.
- Endpoint 0, `EFX/AnyOutput/AnyContent` is read as the speaker's "Music"
  preset: the archive's own manifest names that file "<model> Music".
  Everything else in the preset (dynamics, the user equaliser, the other
  content modes) is not read.

Stdlib-only (`tests/test_layout.py` `STDLIB_ONLY`), because discovery runs
before the generator's deferred DSP imports.
"""

from __future__ import annotations

import math
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from lib.apo.layer import ApoLayer, UnsupportedApoConfig

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

# The speaker preset: endpoint 0, the endpoint effect on any output, the
# default content mode. The archive's manifest names this file "<model>
# Music" on the one device checked (#140).
SPEAKER_PRESET = "0/EFX/AnyOutput/AnyContent/Preset.bin"

CURVE_POINTS = 265


def curve_grid_hz() -> tuple[float, ...]:
    """The frequency of each curve point, read as 1/24 octave from 10 Hz."""
    return tuple(10.0 * 2.0 ** (k / 24.0) for k in range(CURVE_POINTS))


def is_archive(path: Path) -> bool:
    """Whether *path* is named like a Sonitude tuning archive."""
    return bool(_ARCHIVE_RE.fullmatch(Path(path).name))


def archives_in(source: Path) -> list[Path]:
    """*source* itself when it is an archive, else every archive under it."""
    source = Path(source)
    if source.is_file():
        return [source] if is_archive(source) else []
    return sorted(p for p in source.rglob("*") if p.is_file()
                  and is_archive(p))


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
    """The speaker layer *source*'s archives hold for this codec and ID.

    When both buses bind the device, *prefer_intelaudio* picks between them.
    Returns None when no archive binds it; raises `UnsupportedApoConfig`
    when one does but its preset can't be read.
    """
    hits = bindings(source, codec_vendor_id, subsys).hits
    if not hits:
        return None
    order = BUSES[::-1] if prefer_intelaudio else BUSES
    bus = min(hits, key=order.index)
    return load(*hits[bus], bus, codec_vendor_id, subsys)


def load(path: Path, number: int, bus: str, codec_vendor_id: str,
         subsys: str) -> ApoLayer:
    """Preset *number*'s speaker layer, from the archive at *path*.

    Raises `UnsupportedApoConfig` when the preset can't be read.
    """
    vendor, device = codec_vendor_id[:4].upper(), codec_vendor_id[4:].upper()
    subsys = subsys.upper()
    member = f"{number}/{SPEAKER_PRESET}"
    try:
        with zipfile.ZipFile(path) as z:
            names = _members(z)
            if member.lower() not in names:
                raise UnsupportedApoConfig(path, f"preset {number} has no "
                                           f"speaker file ({member})")
            curves = read_curves(_read(z, names[member.lower()]))
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
    left = tuple(min(v, 0.0) for v in curves[0])
    right = (tuple(min(v, 0.0) for v in curves[1]) if len(curves) > 1
             else left)
    name = f"preset {number}" + (f", {codename}" if codename else "")
    return ApoLayer(
        label=f"HP Bang & Olufsen (Sonitude {name})",
        config_path=path / member,
        inf_path=path,
        hardware_id=(f"{bus}\\FUNC_01&VEN_{vendor}&DEV_{device}"
                     f"&SUBSYS_{subsys}"),
        sample_rate=48000,
        eq_left=(), eq_right=(), dynamics=(),
        notes=(
            "the compressor and limiter stages (not read yet)",
            "the B&O Audio Control equaliser, flat in the preset",
            "the Movie and Voice modes",
            "a steeper cut below about 60 Hz, and on one laptop a fixed EQ "
            "that stays on with Windows' audio enhancements off, both "
            "measured on Windows (issue #140) and found in no file read",
        ),
        default_on=True,
        standalone=True,
        curve_hz=curve_grid_hz(),
        curve_left_db=left,
        curve_right_db=right,
    )


def probe_freqs() -> list[int]:
    """The curve's own grid thinned to 1/3 octave, 20 Hz–16 kHz: where the
    correction check probes a vendor-only build."""
    return [round(10.0 * 2.0 ** (m / 3.0)) for m in range(3, 33)]
