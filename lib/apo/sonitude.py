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
  1.02 and 7.15. Below the corner of the preset's high-pass
  (`HIGH_PASS_KEYS`), the curve is read as a Butterworth-4 high-pass there,
  a reading pre-registered on the ZBook's Voice mode and within 0.97 dB rms
  there, 50–160 Hz.
- The rest of `Preset.bin` is read as stage blocks of keyed values
  (`stage_values`); of those, only the high-pass is used.
- Endpoint 0, `EFX/AnyOutput/AnyContent` is read as the speaker's "Music"
  preset: the archive's own manifest names that file "<model> Music".
  Everything else in the preset (dynamics, the user equaliser, the other
  content modes) is not read.

`resolve` picks the preset for this machine, by its codec IDs and whether it
runs Intel's audio DSP driver (SOF), or for `--hw-id`'s IDs. `FORMAT` is this
format's entry in `lib/apo/discover.py` `STANDALONE`.

Stdlib-only (`tests/test_layout.py` `STDLIB_ONLY`), because discovery runs
before the generator's deferred DSP imports.
"""

from __future__ import annotations

import argparse
import math
import re
import struct
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from lib import doctor, host
from lib.apo.layer import ApoLayer, StandaloneFormat, UnsupportedApoConfig
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


def _apply_high_pass(curve, corner_hz: float | None) -> tuple[float, ...]:
    """*curve* with its points below *corner_hz* read as a Butterworth-4
    high-pass there; unchanged when the preset's high-pass is off (None)."""
    if corner_hz is None:
        return tuple(curve)
    hz = curve_grid_hz()
    edge = corner_hz * (1.0 + 1e-9)
    n = sum(1 for f in hz if f <= edge)
    k = next((i for i in range(len(curve) - 1)
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


def _high_pass_hz(values: dict, key: int) -> float | None:
    """The corner of channel *key*'s high-pass in Hz; None when off or
    absent. Raises ValueError for a corner no tuning uses."""
    payload = values.get((5, key))
    if payload is None or len(payload) != 5 or not payload[0] & 1:
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
    """The speaker layer *source*'s archives hold for this codec and ID.

    When both buses bind the device, *prefer_intelaudio* picks between them.
    Returns None when no archive binds it; raises `UnsupportedApoConfig`
    when one does but its preset can't be read.
    """
    hits = bindings(source, codec_vendor_id, subsys).hits
    if not hits:
        return None
    bus = _preferred_bus(hits, prefer_intelaudio)
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
    left = tuple(min(v, 0.0)
                 for v in _apply_high_pass(curves[0], corners[0]))
    right = (tuple(min(v, 0.0)
                   for v in _apply_high_pass(curves[1], corners[1]))
             if len(curves) > 1 else left)
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
            "the psychoacoustic bass stage, which adds harmonics to the "
            "deepest bass (not read yet)",
            "the B&O Audio Control equaliser, flat in the preset",
            "the Movie and Voice modes",
            "on one laptop, a fixed EQ that stays on with Windows' audio "
            "enhancements off, measured on Windows (issue #140) and found "
            "in no file read",
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


def parse_hw_id(text: str) -> str:
    """--hw-id's value, checked and normalised to "[BUS:]CODEC:SUBSYS".

    Takes the IDs as --speaker-info prints them, so a 0x prefix is fine. A
    string, not a tuple, because dolby_to_pipewire.py forwards it as one."""
    parts = text.strip().upper().split(":")
    bus = parts.pop(0) if len(parts) == 3 else None
    ids = [p.removeprefix("0X") for p in parts]
    if (len(ids) != 2 or (bus is not None and bus not in BUSES)
            or not all(len(i) == 8 and all(c in "0123456789ABCDEF" for c in i)
                       for i in ids)):
        raise argparse.ArgumentTypeError(
            f"{text!r}: expected CODEC:SUBSYS, 8 hex digits each, such as "
            "0x10EC0285:0x103C860F, optionally after HDAUDIO: or "
            "INTELAUDIO:")
    return ":".join([*([bus] if bus else []), *ids])


def resolve(source: Path, hw_id: str | None,
            warn: Callable[[str], None]) -> ApoLayer:
    """The layer *source*'s archives hold for this machine, or for the
    laptop *hw_id* names (see `parse_hw_id`); raises RuntimeError when none
    binds it or the one that does can't be read.

    Prints nothing: when two archives bind this laptop to different tunings,
    *warn* gets the sentence saying which was used.
    """
    try:
        return _resolve(source, hw_id, warn)
    except UnsupportedApoConfig as exc:
        target = "--hw-id's IDs" if hw_id else "this laptop"
        raise RuntimeError(f"HP's Bang & Olufsen tuning at "
                           f"{doctor.tilde(exc.path)} binds {target} "
                           f"but can't be used: {exc.reason}.") from exc


def _resolve(source: Path, hw_id: str | None,
             warn: Callable[[str], None]) -> ApoLayer:
    src = doctor.tilde(source)
    if hw_id:
        *bus, codec, subsys = hw_id.split(":")
        bus = bus[0] if bus else None
        ids = [(codec, subsys)]
    else:
        bus = None
        ids = [(v, s) for v, s, name in codecs.get_hda_codec_ids()
               if "HDMI" not in name.upper()
               and "DISPLAYPORT" not in name.upper()]
        if not ids:
            raise RuntimeError(
                f"{src} holds HP's Bang & Olufsen tunings, but this "
                "machine's audio codec IDs can't be read to pick one. Pass "
                "the laptop's with --hw-id, as its --speaker-info prints "
                "them.")
    cards = host.path("/proc/asound/cards")
    try:
        sof = speakers._card_uses_sof(cards.read_text().splitlines())
    except OSError:
        sof = False
    unreadable: list[Path] = []
    for codec, subsys in ids:
        found = bindings(source, codec, subsys)
        unreadable += found.unreadable
        hits = {b: h for b, h in found.hits.items() if bus in (None, b)}
        if bus and found.hits and not hits:
            (other,) = found.hits
            raise RuntimeError(
                f"{src} holds a tuning for {codec}:{subsys} in its {other} "
                f"archive only. Pass --hw-id {other}:{codec}:{subsys}, or "
                "leave the bus out.")
        if not hits:
            continue
        # A SOF machine runs Intel's DSP, which on Windows is the INTELAUDIO
        # bus; the other archive is for Windows' standard driver.
        picked = _preferred_bus(hits, sof)
        layer = load(*hits[picked], picked, codec, subsys)
        other = next((b for b in hits if b != picked), None)
        if other is None:
            return layer
        try:
            alt = load(*hits[other], other, codec, subsys)
        except UnsupportedApoConfig:
            return layer
        if (alt.curve_left_db, alt.curve_right_db) == (
                layer.curve_left_db, layer.curve_right_db):
            return layer
        both = (f"preset {hits[picked][1]} in its {picked} archive and "
                f"preset {hits[other][1]} in its {other} one")
        if hw_id:
            raise RuntimeError(
                f"{src} holds two different tunings for {codec}:{subsys}: "
                f"{both}. Pass the bus too, as the speakers' hardware ID "
                "starts in Windows' Device Manager: --hw-id "
                f"INTELAUDIO:{codec}:{subsys} or HDAUDIO:{codec}:{subsys}.")
        runs = "runs" if sof else "doesn't run"
        warn(f"{src} holds two different tunings for this laptop: "
             f"{both}. Using the {picked} one, because this machine {runs} "
             "Intel's audio DSP driver (SOF). If the speakers' hardware ID in "
             f"Windows' Device Manager starts with {other}, rebuild with "
             f"--hw-id {other}:{codec}:{subsys}.")
        return layer
    if unreadable:
        raise RuntimeError(
            f"{doctor.tilde(unreadable[0])} can't be read as one of HP's "
            "tuning archives, so whether it holds this laptop's tuning is "
            "unknown. Try a fresh copy of the download.")
    said = ", ".join(f"{c}:{s}" for c, s in ids)
    if hw_id:
        raise RuntimeError(
            f"{src} holds no HP Bang & Olufsen tuning for "
            f"{bus + ':' if bus else ''}{said}. Check the IDs against that "
            "laptop's --speaker-info, or use its model's driver download.")
    raise RuntimeError(
        f"{src} holds HP's Bang & Olufsen tunings, but none for this "
        f"laptop's audio hardware (codec:subsystem {said}), so it is likely "
        "another model's download. Use the audio driver download HP lists "
        "for this model.")


# This format's entry in `lib/apo/discover.py` `STANDALONE`.
FORMAT = StandaloneFormat(
    is_source=is_source,
    wanted_members=wanted_members,
    resolve=resolve,
    # "Dolby" would mislabel a preset built from HP's own tuning.
    prefix="HP-BO",
    owner="HP's Bang & Olufsen",
    # The speaker preset read is the "Music" one (`SPEAKER_PRESET`).
    heading="Internal speakers (the output this preset is for), Music mode",
    probe_freqs=probe_freqs,
)
