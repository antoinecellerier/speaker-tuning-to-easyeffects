"""Microsoft Surface APO: find the config bound to this device and read it.

Some Surface models ship speaker voicing in a `SurfaceAPO_<id>.json` beside
the DAX3 XML: 11 of the 25 surveyed (`r-surface-apo-fleet`). On the Surface
Pro 9 (Intel) that XML switches off its audio-optimizer, IEQ and graphic EQ in
every profile (#113).
`SurfaceAPOExtension.inf` binds each JSON to HD-Audio or SoundWire hardware
IDs. The speaker's Realtek driver runs it as the mode and endpoint effects
(MFX, EFX), after Dolby's stream effect (SFX). Research `r-surface-apo-efx`
holds the evidence and the open questions for each mapping below, and
`r-surface-apo-fleet` the block sets other Surface models ship.

The binding is read from the `.inf`: hardware ID → install section → AddReg
(on that section, or on the interface sections it adds) → the
config-filename property. The `_<id>` suffix of the JSON's name is a
Microsoft naming habit, not the binding, so it is never matched on.

Only the `R/EFX` chain is read:

- The `MainEQ` blocks are biquad cascades, run in file order. They become
  the layer's EQ, which `lib/preset/` folds into the FIR.
- The `VolumeDepMBDRC` block becomes a multiband compressor, at volume
  state 0.
- The `Crystal` blocks become one multiband limiter, with one band per
  resonance, when they fit one 8-band stage. Otherwise the stage is listed
  as not reproduced.
- `ApoLayer.stages` lists each of these in file order, with what each
  leaves out (hold times, per-band output limits, other volume states),
  and everything else that changes the audio: the volume-dependent EQs,
  the `OutputLimiter`, and any block this module doesn't know.
  `VolumeControl` is the system volume itself, so it is not listed.

Stdlib-only, like `lib/apo/layer.py`.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from lib.apo.layer import (APPROXIMATED, BUILT, NOT_REPRODUCED, ApoLayer,
                           BandDynamics, Biquad, DynBand, Stage,
                           UnsupportedApoConfig)

LABEL = "Microsoft Surface APO"

# The device-property key SurfaceAPOExtension.inf stores the config filename
# under (`PKEY_SurfaceApoConfigFilename` in its [Strings]).
_CONFIG_FILENAME_PKEY = "{c1f75c4c-3243-11ea-850d-2e728ce88125},0"

# The ID the DAX3 XML is named after, as the hardware-ID parts it must match:
# DEV_0274_SUBSYS_10EC1284_PCI_SUBSYS_72708086.xml → DEV_0274, SUBSYS_10EC1284
# (HDAUDIO\FUNC_01&VEN_10EC&DEV_0274&SUBSYS_10EC1284), and
# SOUNDWIRE_MAN_025D_FUNC_1320_SUBSYS_307210EC.xml → MAN_025D, FUNC_1320,
# SUBSYS_307210EC (SOUNDWIRE\SDCA_10&MAN_025D&FUNC_1320&…&SUBSYS_307210EC&…).
_XML_ID_RES = (
    re.compile(r"_(MAN_[0-9A-F]{4})_(FUNC_[0-9A-F]{4})_(SUBSYS_[0-9A-F]{8})",
               re.IGNORECASE),
    re.compile(r"(DEV_[0-9A-F]{4})_(SUBSYS_[0-9A-F]{8})", re.IGNORECASE),
)

# Packages are found by their .inf's name, since the folder holding it
# differs between MSIs (`surfaceapoextension/`, `apoextension/`,
# `surfacerender_ext/`).
_INF_PREFIX = "surfaceapoextension"


def find(xml_path: Path) -> ApoLayer | None:
    """The Surface APO layer bound to the device *xml_path* tunes, if any.

    Looks in the folders beside the XML's own package: the sibling folders
    of an extracted MSI, or the sibling `<inf>.inf_<arch>_<hash>/` folders
    of a Windows DriverStore. Returns None when no `.inf` there binds this
    device, or when the bound config has no endpoint chain. Raises
    `UnsupportedApoConfig` when one binds it, but its config can't be read.
    """
    # Resolved: a bare filename's parent and grandparent are both `.`, so
    # the folders beside its package would go unsearched.
    xml_path = Path(xml_path).resolve()
    m = next(filter(None, (r.search(xml_path.name) for r in _XML_ID_RES)),
             None)
    if not m:
        return None  # e.g. Qualcomm's AUCD_ and the short SDW_ names
    parts = {g.lower() for g in m.groups()}

    bound = []
    for inf in _candidate_infs(xml_path):
        # An .inf that can't be read binds nothing this can follow. Raising
        # would stop every run on the device, with the flag on or off.
        try:
            hit = _bound_config(inf, parts)
            if hit:
                bound.append((_driver_version(inf), inf) + hit)
        except (OSError, ValueError):
            continue
    if not bound:
        return None
    _, inf, hwid, config_name = max(bound, key=lambda b: b[0])
    config = _named(inf.parent, config_name)
    if not config.is_file():
        raise UnsupportedApoConfig(config, f"{inf.name} binds it, but the "
                                   "file is missing")
    return parse_config(config, inf, hwid)


def _named(folder: Path, name: str) -> Path:
    """*folder*'s file called *name*, matched case-insensitively as Windows
    matches it; `folder / name` when there is none."""
    try:
        return next(f for f in folder.iterdir()
                    if f.name.lower() == name.lower())
    except (OSError, StopIteration):
        return folder / name


def _candidate_infs(xml_path: Path) -> list[Path]:
    """Surface APO `.inf` files in the XML's folder and the folders beside
    its package."""
    infs = _infs_in(xml_path.parent)
    for root in dict.fromkeys((xml_path.parent, xml_path.parent.parent)):
        infs += infs_below(root)
    return sorted(set(infs))


def infs_below(root: Path) -> list[Path]:
    """Surface APO `.inf` files in the folders directly under *root*.

    A DriverStore folder is named after the `.inf` it holds, so one named
    for another `.inf` is skipped unread: a FileRepository holds thousands.
    """
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    infs = []
    for d in entries:
        # The name check first: it costs no disk access, and is_dir() does.
        name = d.name.lower()
        if ".inf_" in name and not name.startswith(_INF_PREFIX):
            continue
        if d.is_dir():
            infs += _infs_in(d)
    return infs


def wanted_members(paths: list[str]) -> set[str]:
    """The archive members `find` reads, out of a download's file list.

    Every file in a folder that holds a Surface APO `.inf`: the config's name
    comes from that `.inf`'s AddReg, not from a pattern, so the whole folder
    is kept. Paths are the archive's own, `/`-separated.
    """
    folders = {p.rpartition("/")[0] for p in paths
               if p.rpartition("/")[2].lower().startswith(_INF_PREFIX)
               and p.lower().endswith(".inf")}
    return {p for p in paths if p.rpartition("/")[0] in folders}


def _infs_in(folder: Path) -> list[Path]:
    try:
        return [f for f in folder.iterdir() if f.suffix.lower() == ".inf"
                and f.name.lower().startswith(_INF_PREFIX)]
    except OSError:
        return []


# --- .inf reading -----------------------------------------------------------

def _read_inf(path: Path) -> dict[str, list[str]]:
    """An .inf as {lowercased section name: its lines}, comments stripped.

    Windows writes .inf files in UTF-16 as often as in ANSI, so the encoding
    follows the byte-order mark. `[Strings]` tokens (`%NAME%`) are
    substituted throughout.
    """
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        line = _strip_comment(line).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip().lower(), [])
        elif current is not None:
            current.append(line)
    strings = {}
    for line in sections.get("strings", []):
        key, sep, value = line.partition("=")
        if sep:
            strings[key.strip().lower()] = value.strip().strip('"')

    def subst(line: str) -> str:
        # A substituted value keeps its quotes when it holds a comma, as the
        # PKEY names do ("{guid},0"), so `_fields` still splits it as one.
        def value(m):
            v = strings.get(m.group(1).lower())
            if v is None:
                return m.group(0)
            return f'"{v}"' if "," in v else v
        return re.sub(r"%([^%]+)%", value, line)
    return {name: [subst(l) for l in lines] for name, lines in sections.items()}


def _strip_comment(line: str) -> str:
    """The line up to its first `;` outside double quotes."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ";" and not quoted:
            return line[:i]
    return line


def _fields(value: str) -> list[str]:
    """Comma-separated .inf fields, unquoted; a quoted comma stays inside."""
    fields, current, quoted = [], [], False
    for ch in value:
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            fields.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    fields.append("".join(current).strip())
    return fields


def _bound_config(inf: Path, parts: set[str]
                  ) -> tuple[str, str] | None:
    """(hardware ID, config filename) when *inf* binds the device, else None.

    *parts* are the lowercased hardware-ID parts the device's XML is named
    after (`_XML_ID_RES`); a hardware ID binds it when it carries them all.
    Walks [Manufacturer] → model sections → the install section named for
    that hardware ID → its AddReg sections (`_config_name`) → the HKR line
    setting the config-filename property.
    """
    sections = _read_inf(inf)
    for line in sections.get("manufacturer", []):
        _, _, value = line.partition("=")
        base, *decorations = _fields(value)
        for model in [base] + [f"{base}.{d}" for d in decorations]:
            for entry in sections.get(model.lower(), []):
                _, _, rhs = entry.partition("=")
                install, *hwids = _fields(rhs)
                hwid = next((h for h in hwids if parts <= set(
                    re.split(r"[\\&]", h.lower()))), None)
                if not hwid:
                    continue
                name = _config_name(sections, install)
                if name:
                    return hwid, name
    return None


def _config_name(sections: dict[str, list[str]], install: str) -> str | None:
    """The config filename an install section's AddReg sets, if any.

    The AddReg sits on the install section itself, or only on the interface
    sections its `.Interfaces` section adds (the Surface Pro 11 and 12
    packages; `r-surface-apo-fleet`).
    """
    for suffix in (".nt", ".ntamd64", ""):
        section = f"{install}{suffix}".lower()
        targets = [section]
        for line in sections.get(f"{section}.interfaces", []):
            key, _, value = line.partition("=")
            fields = _fields(value)
            # AddInterface = class GUID, reference string, install section
            if key.strip().lower() == "addinterface" and len(fields) > 2:
                targets.append(fields[2].lower())
        for target in targets:
            for addreg in _directive(sections, target, "addreg"):
                for reg in sections.get(addreg.lower(), []):
                    f = _fields(reg)
                    if len(f) >= 5 and f[2].lower() == _CONFIG_FILENAME_PKEY:
                        # The value is a DriverStore path such as
                        # %13%\SurfaceAPO_1284.json; %13% is the package dir.
                        return re.split(r"[\\/]", f[4])[-1]
    return None


def _directive(sections: dict[str, list[str]], section: str, key: str
               ) -> list[str]:
    """Every value a section's *key* lines list, e.g. its AddReg sections."""
    values = []
    for line in sections.get(section, []):
        k, _, value = line.partition("=")
        if k.strip().lower() == key:
            values += [v for v in _fields(value) if v]
    return values


def _driver_version(inf: Path) -> tuple[int, ...]:
    """[Version] DriverVer as a sortable tuple, () when absent."""
    for line in _read_inf(inf).get("version", []):
        key, _, value = line.partition("=")
        if key.strip().lower() == "driverver":
            ver = _fields(value)[-1]
            return tuple(int(p) for p in re.findall(r"\d+", ver))
    return ()


# --- config reading ---------------------------------------------------------

# The only rate the shipped R/EFX chain is defined at, and the pipeline's.
_SAMPLE_RATE = 48000

# The DRC's tables carry one row per volume state. State 0 reads as full
# volume (unvalidated): ValueTable steps down from its first value (-2.75 dB
# in 14 of the 16 configs read, -2.2222 in the Pro 8's) and the low shelf
# rises with the state. Full volume is the only state a filter placed before
# the speaker's volume control sees.
_VOLUME_STATE = 0

# LSP's multiband compressor carries 8 bands.
_MAX_BANDS = 8

# A crossover at or above this splits off only inaudible bands. The Surface
# Pro 8's 8-band DRC parks five crossovers at 22.6–23 kHz to use 3 bands.
_AUDIBLE_HZ = 20000.0

# Block names, by role. A trailing digit numbers repeats (`MainEQ1`,
# `Crystal2`); the DRC's names its band count (`VolumeDepMBDRC4`), or
# nothing (`VolumeDepMBDRC`).
_MAIN_EQ_RE = re.compile(r"MainEQ\d*")
_DRC_RE = re.compile(r"VolumeDepMBDRC\d*")
_CRYSTAL_RE = re.compile(r"Crystal\d*")
_VOLUME_EQ_RE = re.compile(r"VolumeDep(?!MBDRC)\w+")


def parse_config(config: Path, inf: Path, hwid: str) -> ApoLayer | None:
    """Read a Surface APO JSON's R/EFX chain into an `ApoLayer`.

    None when the config has no endpoint chain at all, as the Surface Laptop
    Studio 2's: it then holds only per-mode `R/MFX` EQs, which this reader
    skips. In the configs read they are identity in every mode but
    NOTIFICATION (`r-surface-apo-efx`, `r-surface-apo-fleet`). None too
    when the chain leaves nothing to build, no EQ section and no dynamics
    stage: a layer that is on by default must not report an empty stage
    as applied.
    """
    try:
        text = config.read_text(encoding="utf-8-sig")
    except (OSError, ValueError) as e:
        raise UnsupportedApoConfig(config, f"it can't be read ({e})") from e
    # Any shape this reader doesn't expect, anywhere in the file, is a config
    # it can't use: the run warns and builds the Dolby-only preset.
    try:
        return _layer(config, json.loads(text), inf, hwid)
    except (ValueError, KeyError, TypeError, IndexError, AttributeError,
            StopIteration) as e:
        raise UnsupportedApoConfig(
            config, f"its contents don't parse ({e!r})") from e


def _layer(config: Path, doc, inf: Path, hwid: str) -> ApoLayer | None:
    """`parse_config` for a decoded config document."""
    chain = _efx_chain(doc)
    if chain is _NO_EFX:
        return None
    if chain is None:
        raise UnsupportedApoConfig(
            config, f"no {_SAMPLE_RATE // 1000} kHz R/EFX render chain")

    live = {n: b for n, b in chain.items() if _enabled(b)}
    # (the stage's first block, the stage), sorted into file order below.
    stages: list[tuple[str, Stage]] = []
    main_eqs = [n for n in live if _MAIN_EQ_RE.fullmatch(n)]
    eq_left, eq_right = _main_eq(config, [(n, live[n]) for n in main_eqs])
    if eq_left or eq_right:
        stages.append((main_eqs[0], Stage(
            f"EQ ({'/'.join(main_eqs)})", BUILT, builds="eq",
            evidence=_RESEARCH)))
    drcs = [n for n in live if _DRC_RE.fullmatch(n)]
    crystals = [n for n in live if _CRYSTAL_RE.fullmatch(n)]
    dynamics = []
    if drcs:
        drc, gaps = _drc(config, drcs[0], live[drcs[0]])
        dynamics.append(drc)
        stages.append((drcs[0], _dynamics_stage(
            f"compressor ({drcs[0]})", "drc",
            gaps + _hold([live[drcs[0]]]))))
    if crystals:
        names = "/".join(crystals)
        crystal, why_not = _crystal(config, {n: live[n] for n in crystals})
        if crystal:
            dynamics.append(crystal)
            gaps = (["overlapping resonance bands split side by side"]
                    if _overlaps(crystal) else [])
            stages.append((crystals[0], _dynamics_stage(
                f"resonance limiter ({names})", "crystal",
                gaps + _hold([live[n] for n in crystals]))))
        elif why_not:
            stages.append((crystals[0], Stage(
                f"resonance limiter ({names})", NOT_REPRODUCED, why_not,
                evidence=_RESEARCH)))
    if not (eq_left or eq_right or dynamics):
        return None
    volume_eqs = [n for n in live if _VOLUME_EQ_RE.fullmatch(n)
                  and not _is_identity(live[n])]
    if volume_eqs:
        what = ("bass and treble shelves"
                if set(volume_eqs) <= {"VolumeDepLS", "VolumeDepHS"}
                else "EQ")
        stages.append((volume_eqs[0], Stage(
            f"{what} set by volume ({'/'.join(volume_eqs)})", NOT_REPRODUCED,
            "neither chain this tool builds sees the speaker's volume",
            evidence=_RESEARCH)))
    limiter = live.get("OutputLimiter")
    if limiter:
        lookahead = _first(limiter, "LookaheadTimeMs", 0.0)
        acts = (_first(limiter, "Ratio", 1.0) != 1.0
                or _first(limiter, "ThresholdDb", 0.0) < 0.0)
        if acts:
            why_not = (f"threshold {_first(limiter, 'ThresholdDb', 0.0):g} "
                       f"dB, ratio {_first(limiter, 'Ratio', 1.0):g}, which "
                       "the preset's own limiter replaces")
        elif lookahead > 0:
            why_not = (f"its {lookahead:g} ms look-ahead, left out so the "
                       "chain adds no delay")
        else:
            why_not = ""
        if why_not:
            stages.append(("OutputLimiter", Stage(
                "final limiter (OutputLimiter)", NOT_REPRODUCED, why_not,
                evidence=_RESEARCH)))
    known = {"VolumeControl", "OutputLimiter", *drcs[:1], *crystals}
    stages += [(n, Stage(f"processing this tool doesn't recognise ({n})",
                         NOT_REPRODUCED))
               for n in live if n not in known
               and not _MAIN_EQ_RE.fullmatch(n)
               and not _VOLUME_EQ_RE.fullmatch(n)]
    order = list(live)
    return ApoLayer(
        label=LABEL, config_path=config, inf_path=inf,
        hardware_id=hwid, sample_rate=_SAMPLE_RATE,
        eq_left=eq_left, eq_right=eq_right, dynamics=tuple(dynamics),
        stages=tuple(stage for _, stage in sorted(
            stages, key=lambda pair: order.index(pair[0]))),
        # On by default since the #113 reporter preferred it by ear on a
        # Surface Pro 9 (`r-surface-apo-efx`); no Windows capture yet.
        default_on=True)


_RESEARCH = "r-surface-apo-efx"


def _dynamics_stage(name: str, builds: str, gaps: list[str]) -> Stage:
    """A built dynamics stage: approximated when the build differs from
    the block in *gaps*, one clause each."""
    return Stage(name, APPROXIMATED if gaps else BUILT, "; ".join(gaps),
                 builds=builds, evidence=_RESEARCH)


def _hold(blocks: list[dict]) -> list[str]:
    """The gap a hold time leaves, when any of *blocks* sets one: neither
    LSP stage has a hold."""
    if any(h for b in blocks for h in b.get("HoldTimeMs", [])):
        return ["hold time left out: the preset's compressor has no hold "
                "setting"]
    return []


# `_efx_chain`'s answer for a config with no R/EFX node at all, as the
# Surface Laptop Studio 2's is: distinct from one whose R/EFX isn't 48 kHz.
_NO_EFX: dict = {}


def _efx_chain(doc: dict) -> dict[str, dict] | None:
    """{block name: {parameter: value}} for the 48 kHz R/EFX chain.

    `_NO_EFX` when the config has no R/EFX node; None when none of its
    R/EFX nodes runs at 48 kHz. Blocks keep their file order.
    """
    store = next(e for e in doc["entities"]
                 if e["name"] == "InitialValueStore")
    nodes = [n for n in store["children"] if n["name"] == "R/EFX"]
    if not nodes:
        return _NO_EFX
    for node in nodes:
        blocks = {b["name"]: {p["name"]: p["value"] for p in b["children"]}
                  for b in node["children"] if b["type"] == "complex"}
        if any(fmt.get("sample_rate") == _SAMPLE_RATE
               for b in blocks.values()
               for fmt in b.get("InputFormats", [])):
            return blocks
    return None


def _enabled(block: dict | None) -> bool:
    return bool(block) and bool(_first(block, "Enabled", False))


def _first(block: dict, key: str, default):
    value = block.get(key, default)
    return value[0] if isinstance(value, list) and value else value


def _num(value) -> float:
    """*value* as a finite float. A NaN or an infinity, which `json.loads`
    and `float` both accept, raises ValueError like any other bad number,
    and `parse_config` reports the config as unusable: past this point it
    would turn every FIR tap into NaN."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{value!r} is not a finite number")
    return number


_IDENTITY: Biquad = (1.0, 0.0, 0.0, 0.0, 0.0)


def _sections(coeffs: list) -> list[Biquad]:
    return [tuple(_num(c) for c in coeffs[i:i + 5])
            for i in range(0, len(coeffs), 5)]


def _is_identity(block: dict) -> bool:
    """True when every biquad section of a coefficient block passes audio
    unchanged. A guard: no shipped volume-dependent EQ read so far is
    identity in every state, though several are at state 0."""
    coeffs = block.get("Coefficients", [])
    return (len(coeffs) % 5 == 0
            and all(s == _IDENTITY for s in _sections(coeffs)))


def _main_eq(config: Path, blocks: list[tuple[str, dict]]
             ) -> tuple[tuple[Biquad, ...], tuple[Biquad, ...]]:
    """Every MainEQ block as one per-channel cascade, identities dropped.

    Blocks cascade in file order. Each coefficient list interleaves the two
    channels section by section: left section 0, right section 0, left
    section 1, …
    """
    left: list[Biquad] = []
    right: list[Biquad] = []
    for name, block in blocks:
        coeffs = block.get("Coefficients", [])
        if not coeffs or len(coeffs) % 10:
            raise UnsupportedApoConfig(
                config, f"{name} holds {len(coeffs)} coefficients, not a "
                "whole number of stereo biquad pairs")
        sections = _sections(coeffs)
        left += [s for s in sections[0::2] if s != _IDENTITY]
        right += [s for s in sections[1::2] if s != _IDENTITY]
    return tuple(left), tuple(right)


def _state_rows(config: Path, block: dict, key: str, bands: int,
                name: str) -> list[list[float]]:
    """A per-state, per-band table's rows, one per volume state."""
    values = [_num(v) for v in block[key]]
    if not values or len(values) % bands:
        raise UnsupportedApoConfig(
            config, f"{name}.{key} holds {len(values)} values, not a "
            f"multiple of {bands} bands")
    return [values[i:i + bands] for i in range(0, len(values), bands)]


def _drc(config: Path, name: str, block: dict
         ) -> tuple[BandDynamics, list[str]]:
    """A VolumeDepMBDRC block as a multiband compressor at full volume, and
    what of the block it leaves out.

    Its band count is one more than its crossovers. Bands above
    `_AUDIBLE_HZ` are dropped with their crossovers.

    PreGain boosts a band before its compressor. The LSP equivalent is the
    same gain on the detector (sidechain preamp) and on the output (makeup).
    A band with ratio 1 and no pregain does nothing. When the band below it
    is inert too, the two pass audio alike, so their split is dropped.
    """
    xovers = [_num(f) for f in block["CrossoverFreqs"]]
    n = len(xovers) + 1
    tables = [_state_rows(config, block, key, n, name) for key in
              ("ThresholdDb", "Ratio", "PreGainDb", "OutputLimit")]
    gaps = []
    if any(r != rows[0] for rows in tables for r in rows):
        gaps.append("only volume state 0, read as full volume, is used")
    threshold, ratio, pregain, limit = (rows[_VOLUME_STATE]
                                        for rows in tables)
    attack = [_num(v) for v in block["AttackTimeMs"]]
    release = [_num(v) for v in block["ReleaseTimeMs"]]
    if xovers != sorted(xovers) or len(attack) < n or len(release) < n:
        raise UnsupportedApoConfig(
            config, f"{name} has {len(xovers)} crossovers, out of order or "
            "without an attack and release per band")
    audible = 1 + sum(f < _AUDIBLE_HZ for f in xovers)

    bands: list[DynBand] = []
    splits: list[float] = []
    for i in range(audible):
        live = ratio[i] != 1.0 or pregain[i] != 0.0
        band = DynBand(enabled=live, threshold_db=threshold[i],
                       ratio=ratio[i], attack_ms=attack[i],
                       release_ms=release[i], pregain_db=pregain[i])
        if i and not live and not bands[-1].enabled:
            continue  # two inert neighbours pass audio alike: one band
        if i:
            splits.append(xovers[i - 1])
        bands.append(band)
    if len(bands) > _MAX_BANDS:
        raise UnsupportedApoConfig(
            config, f"{name} has {len(bands)} audible bands; the compressor "
            f"stage holds {_MAX_BANDS}")
    if any(v != 0.0 for v in limit[:audible]):
        gaps.append("per-band output limits left out")
    lookahead = [_num(v) for v in block.get("LookaheadTimeMs", [])]
    if any(lookahead[:audible]):
        gaps.append("look-ahead left out, so the chain adds no delay")
    return BandDynamics(name="drc", crossovers_hz=tuple(splits),
                        bands=tuple(bands), detection="RMS",
                        knee_db=0.0), gaps


def _crystal(config: Path, blocks: dict[str, dict]
             ) -> tuple[BandDynamics | None, str]:
    """Every Crystal block's resonance limiters as one multiband limiter,
    or None and why not.

    Each block limits its first `ActiveLimiterCount` entries (all of them
    when the field is absent). Entries identical in the fields the limiter
    uses (F0, Bandwidth, Limit, attack, release) collapse into one: the
    Surface Pro 8 lists each resonance twice in a row, which reads as one
    per channel. The resonances are then sorted by F0.

    Each resonance (F0, Bandwidth, Limit) becomes a band whose detector
    listens to F0 ± Bandwidth/2 and limits at Limit. The resonances overlap,
    and a multiband split cannot, so the bands split at the geometric
    midpoints between neighbouring F0s. An inert band sits below the first
    resonance's range and another above the last, so the rest of the
    spectrum passes untouched. None, with the reason, when the resonances
    don't fit those bands; None and no reason when there are none.
    """
    entries = []
    for name, block in blocks.items():
        cols = [[_num(v) for v in block[k]] for k in
                ("F0", "Bandwidth", "Limit", "AttackTimeMs", "ReleaseTimeMs")]
        count = int(_first(block, "ActiveLimiterCount", len(cols[0])))
        if not all(len(c) >= count for c in cols):
            raise UnsupportedApoConfig(
                config, f"{name} has fewer F0/Bandwidth/Limit/attack/release "
                f"entries than its {count} limiters")
        entries += zip(*(c[:count] for c in cols))
    res = sorted(set(entries))
    f0s = [r[0] for r in res]
    # Two bracket bands plus one per resonance; a shared F0 has no split.
    if len(res) > _MAX_BANDS - 2:
        return None, (f"its {len(res)} resonances are more than one limiter "
                      "stage can split apart")
    if len(set(f0s)) < len(f0s):
        return None, ("two of its resonances share a frequency, which a "
                      "limiter stage can't split apart")
    if not res:
        return None, ""
    f0, bw, lim, attack, release = (list(c) for c in zip(*res))
    lows = [max(f - b / 2, 10.0) for f, b in zip(f0, bw)]
    highs = [f + b / 2 for f, b in zip(f0, bw)]
    inert = DynBand(enabled=False, threshold_db=0.0, ratio=1.0,
                    attack_ms=attack[0], release_ms=release[0])
    splits = ([lows[0]] +
              [math.sqrt(a * b) for a, b in zip(f0, f0[1:])] +
              [highs[-1]])
    bands = ([inert] +
             [DynBand(enabled=True, threshold_db=lim[i], ratio=100.0,
                      attack_ms=attack[i], release_ms=release[i],
                      sidechain_hz=(lows[i], highs[i]))
              for i in range(len(res))] +
             [inert])
    return BandDynamics(name="crystal",
                        crossovers_hz=tuple(round(s, 1) for s in splits),
                        bands=tuple(bands), detection="Peak",
                        knee_db=0.0), ""


def _overlaps(crystal: BandDynamics) -> bool:
    """Whether two of a resonance limiter's detector ranges overlap, which
    its side-by-side bands then approximate (`_crystal`)."""
    ranges = [b.sidechain_hz for b in crystal.bands if b.sidechain_hz]
    return any(hi > lo for (_, hi), (lo, _) in zip(ranges, ranges[1:]))
