"""Dolby DAX2: the speaker tuning a driver's `.inf` writes into the registry.

Some drivers carry Dolby's tuning as registry values, in the older DAX2
format, instead of shipping a DAX3 XML. The audio driver's `.inf` binds the
codec's hardware ID to an install section, whose AddReg sections name a
preset through `OEMSettingsOverride` and write it as endpoint properties
`{f112024a-…},N`, N = 0..47, one PROPVARIANT each. Research
`r-dolby-registry-preset` holds the layout and the evidence behind every index
below; `r-dolby-registry-fleet` the drivers known to ship one.

Nothing here is specific to one OEM or codec vendor: an `.inf` is a candidate
when it carries the endpoint property key and, unless it was passed directly,
sits beside Dolby's DAX2 APO; the preset is reached from the device's own
hardware ID.

Stdlib-only, like `lib/apo/layer.py`: `tuning()` imports the DAX3 parser and
the FIR's interpolation when it is called, after the run has loaded both.
"""

from __future__ import annotations

import functools
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from lib import doctor
from lib.apo import inf
from lib.apo.layer import UnsupportedApoConfig
from lib.hardware import codecs

LABEL = "Dolby DAX2"

# A DAX2 preset carries one IEQ curve, built as this voicing alone.
VOICING = "Balanced"

# Dolby's endpoint property set; each value is "{guid},<index>".
_EP_PKEY = "{f112024a-fe30-42a8-80ab-8dd825a06f78}"

# How each index reads, and how sure the name is (`r-dolby-registry-preset`):
# "shape" names a blob by its self-describing frequency grid and layout,
# "match" by the DAX3 registry-form preset whose values match one DAX3 XML
# field each, "assumed" by sitting beside the fields it switches. Indices
# absent here are not read. Each value is a number or a blob of numbers.
SHAPE, MATCH, ASSUMED = "shape", "match", "assumed"
NUMBER, BLOB = "number", "blob"
FIELDS: dict[int, tuple[str, str, str]] = {
    2: ("volmax-boost", NUMBER, MATCH),
    6: ("volume-leveler-in-target", NUMBER, MATCH),
    7: ("volume-leveler-out-target", NUMBER, MATCH),
    8: ("volume-leveler-amount", NUMBER, ASSUMED),
    9: ("volume-leveler-enable", NUMBER, ASSUMED),
    10: ("ieq-enable", NUMBER, ASSUMED),
    12: ("ieq-amount", NUMBER, MATCH),
    13: ("ieq-bands", BLOB, SHAPE),
    18: ("audio-optimizer-bands", BLOB, SHAPE),
    22: ("regulator-tuning", BLOB, SHAPE),
    24: ("regulator-speaker-dist-enable", NUMBER, ASSUMED),
    25: ("regulator-relaxation-amount", NUMBER, MATCH),
    26: ("regulator-timbre-preservation", NUMBER, MATCH),
    35: ("dialog-enhancer-enable", NUMBER, ASSUMED),
    38: ("dialog-enhancer-amount", NUMBER, MATCH),
    42: ("virtual-bass-src-freqs", BLOB, MATCH),
    45: ("virtual-bass-subgains", BLOB, MATCH),
    46: ("virtual-bass-mix-freqs", BLOB, MATCH),
}


@dataclass(frozen=True)
class Dax2Preset:
    """One DAX2 preset the `.inf` binds to this device."""
    inf_path: Path
    hardware_id: str
    name: str                 # the preset key, e.g. DAX2Preset11B2_SPK
    values: dict[int, object]  # index → int, or list[int] for a blob

    @property
    def label(self) -> str:
        return f"{LABEL} ({self.inf_path.name}, {self.name})"


# The key as an .inf spells it in each encoding it comes in.
_EP_PKEY_BYTES = (_EP_PKEY.encode("ascii"),
                  _EP_PKEY.encode("utf-16-le"),
                  _EP_PKEY.encode("utf-16-be"))


def mentions_dax2(path: Path) -> bool:
    """Whether *path*'s bytes carry the DAX2 endpoint property key, in each
    encoding an `.inf` comes in. Read once per file version: a run asks it
    of the same `.inf` several times."""
    try:
        st = Path(path).stat()
    except OSError:
        return False
    return _mentions(str(path), st.st_mtime_ns, st.st_size)


@functools.lru_cache(maxsize=256)
def _mentions(path: str, _mtime_ns: int, _size: int) -> bool:
    try:
        raw = Path(path).read_bytes().lower()
    except OSError:
        return False
    return any(key in raw for key in _EP_PKEY_BYTES)


# Dolby's DAX2 APO, which each Surface package whose file list was checked
# ships beside the .inf carrying a DAX2 tuning (`r-dolby-registry-fleet`):
# the name is Dolby's, not the OEM's, so it narrows the .inf files worth
# reading by name alone.
_APO_DLL_RE = re.compile(r"dolbydax2apo[^/\\]*\.dll", re.IGNORECASE)


def wanted_members(paths: list[str]) -> set[str]:
    """The `.inf` members of a download's file list that sit beside Dolby's
    DAX2 APO, the ones worth unpacking to read. Paths are the archive's own,
    `/`-separated."""
    folders = {p.rpartition("/")[0] for p in paths
               if _APO_DLL_RE.fullmatch(p.rpartition("/")[2])}
    return {p for p in paths if p.lower().endswith(".inf")
            and p.rpartition("/")[0] in folders}


# How many folder levels below a DriverStore or folder passed with
# --windows a DAX2 package is looked for: FileRepository/<package>/ is one,
# an extracted MSI's SurfaceUpdate/<package>/ two.
_MAX_PACKAGE_DEPTH = 3


def infs_below(root: Path, first: bool = False,
               beside_apo: bool = True) -> list[Path]:
    """Every `.inf` at or under *root* that carries a DAX2 key.

    Content decides, since the .inf's own name is each OEM's. *beside_apo*
    reads only those in a folder that also holds Dolby's DAX2 APO, as a
    DriverStore package does; a download's unpacked tree holds the .inf
    files alone (`wanted_members`), so it is read without. *first* stops at
    the first, for a yes/no over a whole DriverStore."""
    root = Path(root)
    if root.is_file():
        return [root] if mentions_dax2(root) else []
    found = []
    for folder, dirs, files in os.walk(root):
        # A DriverStore package holds its .inf a level or two down; deeper
        # is not a driver package, and a mount can be large.
        if beside_apo and len(Path(folder).relative_to(root).parts) >= \
                _MAX_PACKAGE_DEPTH:
            dirs.clear()
        if beside_apo and not any(_APO_DLL_RE.fullmatch(f) for f in files):
            continue
        for name in sorted(files):
            path = Path(folder, name)
            if name.lower().endswith(".inf") and mentions_dax2(path):
                found.append(path)
                if first:
                    return found
    return sorted(found)


def find(root: Path, codecs: list[tuple[str, str]],
         beside_apo: bool = True) -> Dax2Preset | None:
    """The DAX2 preset bound to one of *codecs*, (vendor-device,
    subsystem) pairs such as ("10EC0274", "10EC11B2"), from the `.inf`
    files under *root* (`infs_below`): the newest DriverVer's when several
    bind. None when none does.

    An `.inf` that binds the device but can't be read is passed over, so an
    older or newer package beside it still can, and so is one that can't be
    parsed at all, which shows nothing about binding. Raises the first
    binding refusal when no `.inf` both binds and reads."""
    if not codecs:
        return None
    bound, unreadable = [], []
    for path in infs_below(root, beside_apo=beside_apo):
        for vendor_device, subsys in codecs:
            try:
                preset = bind(path, vendor_device, subsys)
            except UnsupportedApoConfig as exc:
                unreadable.append(exc)
                continue
            except (OSError, ValueError):
                break
            if preset:
                bound.append((inf.driver_version(path), str(path), preset))
                break
    if bound:
        return max(bound, key=lambda b: b[:2])[2]
    if unreadable:
        raise unreadable[0]
    return None


# --- this machine ------------------------------------------------------------

@functools.lru_cache(maxsize=8)
def _find_cached(root: Path, ids: tuple[tuple[str, str], ...],
                 beside_apo: bool) -> Dax2Preset | None:
    """`find`, once per run for each folder and codec list: auto-detection's
    probe and the lookup after it walk the same DriverStore. A refusal is not
    cached, so it is raised again where it is reported."""
    return find(root, list(ids), beside_apo=beside_apo)


def for_this_machine(root: Path, beside_apo: bool = True
                     ) -> Dax2Preset | None:
    """The DAX2 preset an `.inf` at or under *root* binds to this machine's
    audio codec, or None when none there does. Raises RuntimeError, worded
    for the run, when one binds but can't be read."""
    ids = tuple(codecs.get_hda_codec_ids_without_display())
    try:
        return _find_cached(Path(root), ids, beside_apo)
    except UnsupportedApoConfig as exc:
        raise RuntimeError(
            f"{doctor.tilde(exc.path)} binds this device's audio hardware to "
            f"a Dolby tuning this tool can't read: {exc.reason}.") from None


def binds_this_machine(store: Path) -> bool:
    """Whether a DAX2 tuning in DriverStore *store* binds this machine's
    codec, so auto-detection keeps the mount for it. A package for other
    hardware must not hide an extracted tree in the current directory,
    which is searched only when no mount qualifies."""
    try:
        return for_this_machine(store) is not None
    except RuntimeError:
        return True  # it binds; the run then says why it can't be read


def none_for_this_machine(src: Path) -> RuntimeError:
    """The error for *src*, whose `.inf` files bind no DAX2 tuning to this
    machine. A download is likely another model's; an `.inf` copied off
    this machine's own Windows is not, and its tuning is likely elsewhere."""
    ids = codecs.get_hda_codec_ids_without_display()
    if not ids:
        return RuntimeError(
            f"{doctor.tilde(src)} holds Dolby tunings keyed by an HD-Audio "
            "codec, and the run couldn't read this machine's, so it can't "
            "tell which is this machine's.")
    said = ", ".join(f"{c}:{s}" for c, s in ids)
    head = (f"{doctor.tilde(src)} binds no Dolby DAX2 tuning to this "
            f"device's audio hardware (codec:subsystem {said})")
    if src.suffix.lower() == ".inf":
        return RuntimeError(
            f"{head}. Run without a path to search a mounted Windows "
            "partition, or pass your model's driver download.")
    return RuntimeError(
        f"{head}, so it is likely another model's driver. Use the audio "
        "driver download its manufacturer lists for this model.")


def not_a_dax2_inf(src: Path) -> RuntimeError:
    """The error for an `.inf` passed in place of the XML that carries no
    DAX2 key."""
    return RuntimeError(
        f"{doctor.tilde(src)} carries no Dolby DAX2 tuning. Pass the tuning "
        "XML or the driver download it came in, or run without a path to "
        "search a mounted Windows partition.")


# --- one .inf ----------------------------------------------------------------

def bind(path: Path, vendor_device: str, subsys: str) -> Dax2Preset | None:
    """The DAX2 preset *path* binds to the codec, if any.

    Hardware ID → install section → its AddReg sections → the presets their
    `OEMSettingsOverride` lines name → the endpoint values written under
    each. The speaker's is the only one, or the one named for the speaker.
    """
    sections = inf.read(path)
    parts = {f"ven_{vendor_device[:4]}".lower(),
             f"dev_{vendor_device[-4:]}".lower(),
             f"subsys_{subsys}".lower()}
    for hwid, install in inf.bound_installs(sections, parts):
        lines = [line for section in _addreg_sections(sections, install)
                 for line in sections.get(section, [])]
        presets = _presets(lines)
        if not presets:
            continue
        name = _speaker_preset(path, presets)
        preset = Dax2Preset(path, hwid, name,
                            _decode(path, name, presets[name]))
        _arrays(preset)  # refused here, while another .inf can still bind
        return preset
    return None


def _addreg_sections(sections: dict[str, list[str]], install: str
                     ) -> list[str]:
    """The AddReg sections an install section names, from the one
    decoration of it Windows applies on x64, the most specific present, and
    from the interface sections it adds."""
    section = inf.install_section(sections, install)
    if section is None:
        return []
    names = []
    for target in [section, *inf.interface_sections(sections, section)]:
        names += [n.lower() for n in inf.directive(sections, target, "addreg")]
    return list(dict.fromkeys(names))


def _presets(lines: list[str]) -> dict[str, dict[int, str]]:
    """{preset key: {index: the HKR line's data fields}} for every preset an
    `OEMSettingsOverride` line names and DAX2 endpoint values are written
    under."""
    named = set()
    for line in lines:
        # HKR, InterfaceSetting, <topology>, 0x10000, "SysFx",
        # "OEMSettingsOverride", <preset>…: a multi-string whose values
        # after the marker are preset keys.
        f = [x.lower() for x in inf.fields(line)]
        if "oemsettingsoverride" in f:
            named.update(x for x in f[f.index("oemsettingsoverride") + 1:]
                         if x)
    # Registry keys are case-insensitive, so one preset is one lowercased
    # key, shown under the spelling it is first written with.
    shown: dict[str, str] = {}
    presets: dict[str, dict[int, list[str]]] = {}
    for line in lines:
        f = inf.fields(line)
        if len(f) < 5 or not f[2].lower().startswith(_EP_PKEY + ","):
            continue
        # HKR, InterfaceSetting\<preset>\EP\0, "{guid},N", flags, bytes…
        key = f[1].split("\\")
        if len(key) < 2 or key[1].lower() not in named:
            continue
        index = f[2].rpartition(",")[2].strip()
        if not index.isdigit():
            continue
        name = shown.setdefault(key[1].lower(), key[1])
        presets.setdefault(name, {})[int(index)] = f[4:]
    return presets


# A preset named for the speaker, when the driver names one per endpoint.
_SPEAKER_RE = re.compile(r"spk|speaker", re.IGNORECASE)


# A preset named for the headphone output, never read as the speaker's.
_HEADPHONE_RE = re.compile(r"(^|[^a-z])hp$|headphone", re.IGNORECASE)


def _speaker_preset(path: Path, presets: dict[str, dict]) -> str:
    """The speaker's preset among those the install section names."""
    if len(presets) == 1:
        name = next(iter(presets))
        # DAX3's registry form is refused as such, by `_decode`.
        if (_HEADPHONE_RE.search(name)
                and max(presets[name]) < _DAX3_FIRST_INDEX):
            raise UnsupportedApoConfig(
                path, f"the only DAX2 preset it names, {name}, is the "
                "headphone output's")
        return name
    speaker = [name for name in presets if _SPEAKER_RE.search(name)]
    if len(speaker) == 1:
        return speaker[0]
    raise UnsupportedApoConfig(
        path, "it names several DAX2 presets and no single one is named for "
        "the speaker: " + ", ".join(sorted(presets)))


# DAX3 writes the same property set from index 48 up; a preset that does is
# DAX3's registry form, whose lower indices sit elsewhere too
# (`r-dolby-registry-preset`), so it is never read as DAX2.
_DAX3_FIRST_INDEX = 48

# PROPVARIANT types the presets use: a 32-bit int, or a blob of them.
_VT_I4 = 0x0003
_VT_BLOB = 0x0041
_HEADER_BYTES = 8


def _decode(path: Path, name: str, raw: dict[int, list[str]]
            ) -> dict[int, object]:
    """Each index's PROPVARIANT as an int or a list of ints."""
    if max(raw) >= _DAX3_FIRST_INDEX:
        raise UnsupportedApoConfig(
            path, f"{name} is DAX3's registry form, not a DAX2 tuning")
    values: dict[int, object] = {}
    for index, data in raw.items():
        # A value the reader doesn't map can't refuse the preset.
        if index not in FIELDS:
            continue
        try:
            blob = bytes(int(x, 16) for x in data if x)
        except ValueError:
            raise UnsupportedApoConfig(
                path, f"{name} value {index} is not hex bytes") from None
        if len(blob) < _HEADER_BYTES + 4 or (len(blob) - _HEADER_BYTES) % 4:
            raise UnsupportedApoConfig(
                path, f"{name} value {index} has {len(blob)} bytes, not a "
                "PROPVARIANT of 32-bit values")
        vt = struct.unpack_from("<H", blob)[0]
        ints = list(struct.unpack_from(
            f"<{(len(blob) - _HEADER_BYTES) // 4}i", blob, _HEADER_BYTES))
        if vt == _VT_I4:
            values[index] = ints[0]
        elif vt == _VT_BLOB:
            values[index] = ints
        else:
            raise UnsupportedApoConfig(
                path, f"{name} value {index} has PROPVARIANT type {vt:#x}")
    return values


def banded(path: Path, preset: Dax2Preset, index: int) -> list[list[int]]:
    """A band blob split into its arrays: [n, then n-prefixed arrays of n].

    The first array of a frequency blob is its grid, so it must rise.
    Raises `UnsupportedApoConfig` when the counts don't add up.
    """
    ints = preset.values.get(index)
    if not isinstance(ints, list) or not ints:
        raise UnsupportedApoConfig(
            path, f"{preset.name} has no {FIELDS[index][0]} (value {index})")
    n, rest, arrays = ints[0], ints[1:], []
    if n <= 0 or not rest:
        raise UnsupportedApoConfig(
            path, f"{preset.name} value {index} holds no arrays")
    while rest:
        if rest[0] != n or len(rest) < n + 1:
            raise UnsupportedApoConfig(
                path, f"{preset.name} value {index} doesn't split into "
                f"{n}-value arrays")
        arrays.append(rest[1:n + 1])
        rest = rest[n + 1:]
    return arrays


def grid(path: Path, preset: Dax2Preset, index: int
         ) -> tuple[list[int], list[list[int]]]:
    """A frequency blob's grid and the arrays on it."""
    hz, *arrays = banded(path, preset, index)
    if not arrays or hz[0] <= 0 or any(b <= a for a, b in zip(hz, hz[1:])):
        raise UnsupportedApoConfig(
            path, f"{preset.name} value {index} has no rising frequency grid")
    return hz, arrays


def _arrays(preset: Dax2Preset):
    """The preset's band arrays, checked with its single values: the
    audio-optimizer grid and its left and right channels, the IEQ grid and
    curve, and the regulator's arrays on the optimizer's grid (None when
    absent). Raises `UnsupportedApoConfig` on a shape the Dolby chain can't
    build from."""
    path = preset.inf_path
    for index, (name, kind, _) in FIELDS.items():
        if kind == NUMBER and not isinstance(preset.values.get(index, 0),
                                             int):
            raise UnsupportedApoConfig(
                path, f"{preset.name} value {index} ({name}) is a blob, "
                "not a number")
    freqs, channels = grid(path, preset, 18)
    if len(channels) < 2 or any(len(c) != len(freqs) for c in channels):
        raise UnsupportedApoConfig(
            path, f"{preset.name} audio-optimizer-bands has no left/right "
            "channels on its grid")
    ieq_hz, ieq = grid(path, preset, 13)
    regulator = None
    if 22 in preset.values:
        reg_hz, regulator = grid(path, preset, 22)
        if reg_hz != freqs or len(regulator) < 2:
            raise UnsupportedApoConfig(
                path, f"{preset.name} regulator-tuning isn't on the "
                "audio-optimizer's grid")
    return freqs, channels, ieq_hz, ieq[0], regulator


def tuning(preset: Dax2Preset):
    """The preset as the `ParsedTuning` the Dolby chain builds from.

    Units follow the DAX3 fields each index stands for: 1/16 dB for gains,
    targets and thresholds, as `lib/dax/parse.py` reads them, and a field
    the preset doesn't carry takes that parser's default. The IEQ curve sits
    on a grid of its own, so it is read onto the audio-optimizer's the way
    the FIR reads a band curve (`lib/preset/fir.py` `interpolate_curve_db`).
    """
    import numpy as np

    from lib.dax import parse
    from lib.preset import fir

    path, v = preset.inf_path, preset.values
    scale = parse.DB_FIXED_POINT_SCALE

    def scalar(index: int, default: int) -> int:
        # `_arrays` below has already refused a number stored as a blob.
        return v.get(index, default)

    freqs, channels, ieq_hz, ieq_raw, reg = _arrays(preset)
    ieq = [int(round(x)) for x in fir.interpolate_curve_db(
        np.array(ieq_hz, dtype=float), np.array(ieq_raw, dtype=float),
        np.array(freqs, dtype=float))]

    ieq_enabled = scalar(10, 1) == 1
    ieq_amount = (scalar(12, parse.IEQ_AMOUNT_DEFAULT) if ieq_enabled
                  else parse.IEQ_AMOUNT_DEFAULT)

    regulator = None
    if scalar(24, 0) == 1 and reg is not None:
        regulator = {
            "threshold_high": [x / scale for x in reg[1]],
            "threshold_low": [x / scale for x in reg[0]],
            "stress": [x / scale for x in parse.REGULATOR_STRESS_DEFAULT],
            "distortion_slope":
                parse.REGULATOR_DISTORTION_SLOPE_DEFAULT / scale,
            "timbre_preservation":
                scalar(26, parse.REGULATOR_TIMBRE_DEFAULT) / scale,
            "overdrive": parse.REGULATOR_OVERDRIVE_DEFAULT,
            "relaxation": scalar(25, parse.REGULATOR_RELAXATION_DEFAULT),
            "isolated_band": reg[2] if len(reg) > 2 else None,
        }

    vol_leveler = None
    if 9 in v:
        target = parse.VOL_LEVELER_TARGET_DEFAULT
        vol_leveler = {
            "enable": scalar(9, 0),
            "amount": scalar(8, 0),
            "in_target": scalar(6, target) / scale,
            "out_target": scalar(7, target) / scale,
        }

    dialog_enhancer = None
    if scalar(35, 0) == 1:
        dialog_enhancer = {
            "amount": scalar(38, parse.DIALOG_ENHANCER_AMOUNT_DEFAULT)}

    # Read only for the opt-in --enable virtual-bass, so one of an
    # unexpected shape is left out, as the DAX3 parser leaves it, rather
    # than stopping the build.
    virtual_bass = None
    src, mix = v.get(42), v.get(46)
    try:
        subgains = banded(path, preset, 45)[0]
    except UnsupportedApoConfig:
        subgains = None
    if (isinstance(src, list) and len(src) == 2 and isinstance(mix, list)
            and len(mix) == 2 and subgains):
        virtual_bass = {"mode": 0, "src_freqs": src, "mix_freqs": mix,
                        "subgains": subgains, "overall_gain": 0,
                        "slope_gain": 0}

    return parse.ParsedTuning(
        list(freqs), {f"ieq_{VOICING.lower()}": ieq}, ieq_amount,
        list(channels[0]), list(channels[1]), [],
        vol_leveler, dialog_enhancer, None, None, regulator,
        scalar(2, 0) / scale,
        ieq_enabled=ieq_enabled,
        virtual_bass=virtual_bass,
        partial_voicings=True,
    )
