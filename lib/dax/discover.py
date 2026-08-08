"""Find the DAX3 tuning XML that matches this machine's audio hardware.

Discovery, not parsing: *where* Dolby's tunings are (a mounted Windows
partition, an extracted DriverStore, a pile of XMLs under the working
directory) and *which* file in there belongs to this laptop.
`lib/dax/parse.py` takes over once a path is picked.

One function per question. `autoprobe_dolby_source` answers the first with no
user input, walking `/proc/mounts` and then the working directory;
`find_tuning_xml` answers the second, matching the `DEV_` / `MAN_` / `FUNC_` /
`SUBSYS_` tokens in a filename against `lib/hardware/codecs.py`'s reading of
the hardware, and falling back to each XML's own `<security-key>` when no
filename matches. Everything else here is a helper to those two.

`main()` reaches a third name, `is_soundwire_xml`, and it is the thing to know
before editing: the bus is recorded nowhere inside the XML, only in its
filename, and several emitted parameters key off that answer.
"""

from __future__ import annotations

import fnmatch
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from lib import console
from lib.hardware import codecs


# Dolby tuning XML filename sentinel. All three Dolby filename styles
# (``DEV_..._SUBSYS_...``, ``SOUNDWIRE_..._SUBSYS_...``, ``SDW_..._SUBSYS_...``)
# include ``SUBSYS_`` followed by exactly eight alphanumeric characters.
# Most subsystem IDs are hex (e.g. ``17AA22E6``) but Lenovo IdeaPad
# installers use the marketing tag ``IDEA`` as a text vendor prefix
# (e.g. ``IDEA4002``), so we accept ``[0-9A-Za-z]`` rather than restricting
# to hex — see issue #4 (taprobane99). Companion files with suffixes that
# share the filename pattern but do *not* hold DAX3 playback tunings are
# filtered out at the call sites:
#   ``_settings.xml`` — per-device simplified settings
#   ``_dmic.xml`` / ``_amic.xml`` — Dolby Fusion (microphone AEC) tunings
#                                   under ``fusion_ext_*`` and related dirs
DOLBY_FILENAME_RE = re.compile(r"SUBSYS_[0-9A-Za-z]{8}.*\.xml$", re.IGNORECASE)

# Filename-suffix exclusions applied at probe candidate sites. All lowercase;
# compare against ``name.lower().endswith(...)``.
_NON_DAX3_FILENAME_SUFFIXES = ("_settings.xml", "_dmic.xml", "_amic.xml")

# DriverStore wrapper directories. Windows renames each installed package's
# payload directory to ``<inf-stem>.inf_<arch>_<hash>``, so Dolby's DAX3
# extension packages land as ``dax3_ext_rtk.inf_amd64_<hash>`` and similar,
# with the tuning XMLs one level inside. Extractions that never went through
# Setup (``innoextract`` output, hand-organised collections) carry no wrapper,
# which is why every scan below falls back to the scanned directory itself.
_INF_WRAPPER_GLOB = "dax3_ext_*.inf_*"


def _is_inf_wrapper(name: str) -> bool:
    """True if a directory *name* is a DriverStore wrapper for DAX3 tunings.

    The same pattern as ``_INF_WRAPPER_GLOB``, matched against a name already
    in hand instead of by globbing its parent. ``fnmatchcase`` rather than
    ``fnmatch`` so it keeps ``Path.glob``'s case sensitivity.
    """
    return fnmatch.fnmatchcase(name, _INF_WRAPPER_GLOB)


def is_soundwire_xml(filename: str) -> bool:
    """True if the tuning filename marks a SoundWire (not HD-Audio) codec.

    The bus is not recorded inside the XML — only the filename carries it,
    in two forms Dolby ships interchangeably: ``SOUNDWIRE_MAN_*`` and the
    shorter ``SDW_*``. Several emitted parameters key off this, so the
    derivation lives here rather than inline at each caller.
    """
    upper = filename.upper()
    return "SOUNDWIRE" in upper or upper.startswith("SDW_")


def _has_dolby_xml(directory: Path) -> bool:
    """Return True if ``directory`` directly contains a Dolby-shaped XML."""
    try:
        for entry in directory.iterdir():
            name = entry.name
            if name.lower().endswith(_NON_DAX3_FILENAME_SUFFIXES):
                continue
            if entry.is_file() and DOLBY_FILENAME_RE.search(name):
                return True
    except OSError:
        pass
    return False


def _resolve_driver_store(windows_root: Path) -> Path | None:
    """Locate the driver-store-ish directory to scan for Dolby tuning XMLs.

    Accepts:

    1. A Windows system root (e.g. ``C:\\Windows``) whose ``System32/DriverStore/FileRepository``
       subdirectory exists.
    2. A drive-root mount (e.g. ``C:\\``) with a case-insensitive ``Windows/``
       child that satisfies (1).
    3. A pre-extracted DriverStore directory containing ``dax3_ext_*.inf_*``
       subdirectories directly.
    4. Any directory that directly contains one or more Dolby-shaped XML
       files (``DEV_*_SUBSYS_*.xml``, SoundWire variants, etc.) — covers the
       ``innoextract`` default output and arbitrary hand-organised XML
       collections.

    Returns the directory whose immediate children will be scanned by
    ``find_tuning_xml``, or ``None`` if no layout matches. I/O errors are
    swallowed and treated as "no match".
    """
    try:
        file_repo = windows_root / "System32" / "DriverStore" / "FileRepository"
        if file_repo.is_dir():
            return file_repo
        if not windows_root.is_dir():
            return None
        if any(windows_root.glob(_INF_WRAPPER_GLOB)):
            return windows_root
        if _has_dolby_xml(windows_root):
            return windows_root
        for child in windows_root.iterdir():
            if not child.is_dir() or child.name.lower() != "windows":
                continue
            nested = child / "System32" / "DriverStore" / "FileRepository"
            if nested.is_dir():
                return nested
    except OSError:
        return None
    return None


_NTFS_FAMILY_FSTYPES = frozenset({"ntfs", "ntfs3", "fuseblk"})


def _unescape_proc_mount(s: str) -> str:
    """Decode /proc/mounts octal escapes (\\040, \\011, \\012, \\134)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def _ntfs_family_mountpoints() -> list[Path]:
    """Return mountpoints from /proc/mounts whose fstype can hold Windows."""
    try:
        data = Path("/proc/mounts").read_text()
    except OSError:
        return []
    mounts: list[Path] = []
    for line in data.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        _device, mountpoint, fstype = parts[0], parts[1], parts[2]
        if fstype in _NTFS_FAMILY_FSTYPES:
            mounts.append(Path(_unescape_proc_mount(mountpoint)))
    return mounts


_CWD_PROBE_MAX_DEPTH = 10


def _detect_expected_subsys_ids() -> set[str]:
    """Return SUBSYS values (8 hex chars, uppercase) that would match this
    machine's audio hardware in a Dolby XML filename.

    Combines HDA codec subsystem IDs from ``/proc/asound`` with the PCI
    audio subsystem ID (``{device}{vendor}`` for SoundWire naming). May
    return an empty set if no hardware is detected.
    """
    ids: set[str] = set()
    for _vendor, subsys, _name in codecs.get_hda_codec_ids():
        ids.add(subsys.upper())
    pci_subsys = codecs.get_pci_audio_subsystem()
    if pci_subsys:
        vendor, device = pci_subsys
        ids.add(f"{device}{vendor}".upper())
    return ids


def _candidate_has_matching_xml(candidate: Path, expected_subsys: set[str]) -> bool:
    """Return True iff ``candidate`` contains a Dolby XML whose filename
    encodes any of the ``expected_subsys`` values.

    Resolves ``candidate`` to a driver-store the same way ``find_tuning_xml``
    does, then scans XMLs under ``dax3_ext_*.inf_*`` wrappers (if present)
    or directly under the resolved dir.
    """
    if not expected_subsys:
        return False
    driver_store = _resolve_driver_store(candidate)
    if driver_store is None:
        return False
    xml_dirs = sorted(driver_store.glob(_INF_WRAPPER_GLOB)) or [driver_store]
    for xml_dir in xml_dirs:
        try:
            for entry in xml_dir.iterdir():
                if not entry.is_file():
                    continue
                if entry.name.lower().endswith(_NON_DAX3_FILENAME_SUFFIXES):
                    continue
                name = entry.name.upper()
                if not DOLBY_FILENAME_RE.search(entry.name):
                    continue
                for subsys in expected_subsys:
                    if f"SUBSYS_{subsys}" in name:
                        return True
        except OSError:
            continue
    return False


def _walk_for_dolby_xml_dirs(root: Path, max_depth: int = _CWD_PROBE_MAX_DEPTH) -> list[Path]:
    """Return directories under ``root`` that directly contain a Dolby XML.

    Walks with ``followlinks=False`` and a depth cap (depth 0 = ``root``
    itself). Hidden subdirectories (``.git``, ``.venv``, etc.) are pruned
    in-place so they never enter the walk.
    """
    root_parts_len = len(root.parts)
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        current = Path(dirpath)
        depth = len(current.parts) - root_parts_len
        if depth >= max_depth:
            dirnames[:] = []
        for fn in filenames:
            if fn.lower().endswith(_NON_DAX3_FILENAME_SUFFIXES):
                continue
            if DOLBY_FILENAME_RE.search(fn):
                results.append(current)
                break
    return results


def autoprobe_dolby_source() -> Path:
    """Find a single Dolby tuning source without user input.

    Tries, in order:

    1. **Mount probe** — enumerate NTFS-family mountpoints from
       ``/proc/mounts`` and keep any whose DriverStore contains at least one
       ``dax3_ext_*.inf_*`` subdir.
    2. **CWD probe** (only if the mount probe finds nothing) — bounded walk
       of the current working directory for any directory that directly
       contains a Dolby-shaped XML. Covers the ``innoextract`` default
       layout (``./driver-cache/code$GetExtractPath$/Dolby/03_dax_ext/``)
       as well as ad-hoc XML collections.

    Returns a path suitable for ``find_tuning_xml``. Raises
    ``FileNotFoundError`` with a diagnostic if zero or multiple candidates
    match; the caller should surface the message to the user.
    """
    mount_candidates: list[Path] = []
    inspected_mounts = _ntfs_family_mountpoints()
    for mp in inspected_mounts:
        driver_store = _resolve_driver_store(mp)
        if driver_store is None:
            continue
        try:
            if any(driver_store.glob(_INF_WRAPPER_GLOB)):
                mount_candidates.append(mp)
        except OSError:
            continue

    cwd_candidates: list[Path] = []
    cwd = Path.cwd()
    if not mount_candidates:
        seen: set[Path] = set()
        for cand in _walk_for_dolby_xml_dirs(cwd):
            # Cosmetic lift: a directly-matched ``dax3_ext_*.inf_*`` wrapper
            # is reported as its parent (the extraction root), matching the
            # path the user would otherwise pass as ``--windows DIR``.
            if _is_inf_wrapper(cand.name):
                cand = cand.parent
            if cand in seen:
                continue
            seen.add(cand)
            cwd_candidates.append(cand)

    candidates = mount_candidates + cwd_candidates

    def _announce(winner: Path) -> None:
        if winner in mount_candidates:
            console.cprint("ok", f"Auto-detected Windows mount: {winner}")
        else:
            console.cprint("ok", f"Auto-detected extracted DriverStore: {winner}")

    if len(candidates) == 1:
        _announce(candidates[0])
        return candidates[0]

    # With multiple candidates, try to pick the one whose XMLs actually
    # match this machine's audio hardware. Avoids unnecessary ambiguity
    # when the user has multiple extracted driver trees on disk and
    # only one is for their device.
    hardware_matches: list[Path] = []
    if len(candidates) > 1:
        expected = _detect_expected_subsys_ids()
        hardware_matches = [c for c in candidates if _candidate_has_matching_xml(c, expected)]
        if len(hardware_matches) == 1:
            _announce(hardware_matches[0])
            return hardware_matches[0]

    if not candidates:
        if inspected_mounts:
            mount_desc = (
                "no Dolby DriverStore found on mounted NTFS filesystems "
                f"({', '.join(str(p) for p in inspected_mounts)})"
            )
        else:
            mount_desc = "no NTFS-family filesystems mounted"
        cwd_desc = (
            f"no Dolby-shaped XMLs found under {cwd} "
            f"(searched up to {_CWD_PROBE_MAX_DEPTH} levels deep)"
        )
        raise FileNotFoundError(
            f"Auto-detection failed: {mount_desc}; {cwd_desc}. "
            "Pass --windows DIR (e.g. a mounted Windows partition or an "
            "extracted DriverStore) or a positional XML path explicitly."
        )

    # Narrow the listing to whatever is most actionable: the
    # hardware-matching subset if more than one matched, else the full
    # candidate list if none matched.
    if len(hardware_matches) > 1:
        listing = "\n".join(f"  - {p}" for p in hardware_matches)
        header = (
            f"Auto-detection found {len(hardware_matches)} Dolby sources "
            "matching this machine's audio hardware:"
        )
    else:
        listing = "\n".join(f"  - {p}" for p in candidates)
        header = (
            f"Auto-detection found {len(candidates)} Dolby sources, "
            "none of which match this machine's audio hardware:"
        )
    raise FileNotFoundError(
        f"{header}\n{listing}\nPass --windows DIR to pick one explicitly."
    )


def _scan_speaker_tunings_by_manufacturer(xml_files, sdw_man_ids):
    """Content-validate DAX3 XMLs when no filename matched the hardware.

    Parses each candidate's ``<endpoint type>`` and ``<security-key>`` (the
    authoritative hardware binding, e.g.
    ``SOUNDWIRE\\SDCA_FUNCTION_10&MAN_01FA&FUNC_3556&…&SUBSYS_CA0A144D``) and
    keeps ``internal_speaker`` tunings whose ``MAN`` token is a manufacturer
    physically present on this machine. Returns a sorted list of
    ``(path, man, subsys)``; ``subsys`` is the security-key's PCI subsystem
    token (``"?"`` if absent), which the caller uses to pick the exact
    per-device match. Generic untuned tunings (empty security-key, hence no
    ``MAN`` token) are skipped, as are unreadable/malformed files.
    """
    guesses = []
    for xml_file in xml_files:
        try:
            root = ET.parse(xml_file).getroot()
        except (ET.ParseError, OSError):
            continue
        ep = root.find(".//endpoint")
        if ep is None or ep.get("type") != "internal_speaker":
            continue
        sk = root.find("./setting/security-key")
        key = (sk.get("value", "") if sk is not None else "").upper()
        man_m = re.search(r"MAN_([0-9A-F]{4})", key)
        if not man_m or man_m.group(1) not in sdw_man_ids:
            continue
        sub_m = re.search(r"SUBSYS_([0-9A-Z]{8})", key)
        guesses.append((xml_file, man_m.group(1), sub_m.group(1) if sub_m else "?"))
    return sorted(guesses)


def find_tuning_xml(windows_root: Path, best_guess: bool = False):
    """Find the DAX3 tuning XML matching this machine's audio hardware.

    Searches the Windows DriverStore for DAX3 tuning XMLs and matches
    against:
    - HDA codec subsystem IDs from /proc/asound (traditional HDA codecs)
    - SoundWire device IDs + PCI subsystem ID (newer Intel platforms)

    When no filename matches, a tuning whose security-key's PCI subsystem
    equals this machine's is selected automatically (authoritative). Failing
    that, with ``best_guess`` set, fall back to the only internal-speaker tuning
    whose security-key manufacturer matches a detected SoundWire manufacturer (a
    warned, unverified guess). With several such candidates the raised error
    lists them so the user can pass one as the positional XML path argument
    rather than waiting on a code fix.
    """
    hda_codecs = codecs.get_hda_codec_ids()
    sdw_devices = codecs.get_soundwire_ids()
    pci_subsys = codecs.get_pci_audio_subsystem()

    if not hda_codecs and not sdw_devices:
        raise FileNotFoundError(
            "No HDA codecs or SoundWire devices found. "
            "Cannot auto-detect audio hardware."
        )

    # HDA match tokens for DEV_*_SUBSYS_*.xml files. The subsystem alone is
    # NOT unique: Lenovo reuses codec subsystem ids across different Realtek
    # codecs (issue #33 — IdeaPad Pro 5 14APH8's ALC287 shares SUBSYS 17AA38C5
    # with an ALC257 SKU, and both tunings ship in the same driver store). The
    # filename's DEV token is the codec device id (the low 16 bits of the HDA
    # vendor id, 10EC0287 → 0287), so the strong key is the (DEV, SUBSYS) pair;
    # a subsystem-only match is kept as a fallback tier in case a filename's
    # DEV token ever diverges from the codec id (mirrors the SoundWire
    # FUNC-preferred-not-required tiering below).
    hda_subsys_ids = {s.upper() for _, s, _name in hda_codecs}
    hda_dev_subsys = {(v.upper()[-4:], s.upper()) for v, s, _name in hda_codecs}

    # PCI subsystem match token. Dolby PCI-keyed filenames — SoundWire on newer
    # Intel platforms, and Apple Boot Camp tunings on Intel Macs (issue #21) —
    # encode it as {pci_subsys_device}{pci_subsys_vendor}, e.g. PCI subsystem
    # 17AA:2339 -> SUBSYS_233917AA, or Apple 106B:1880 -> SUBSYS_1880106B. (HDA
    # codec filenames instead use the codec's own subsystem, vendor-first.)
    if sdw_devices and pci_subsys is None:
        raise RuntimeError(
            "SoundWire devices detected but could not determine PCI subsystem ID. "
            "Cannot safely select a tuning XML."
        )
    pci_subsys_id = None
    if pci_subsys:
        vendor, device = pci_subsys
        pci_subsys_id = f"{device}{vendor}".upper()

    # SoundWire match tokens. The strong key is (manufacturer, part) — Dolby's
    # filename FUNC token usually equals the Linux SoundWire part id (all 29
    # corpus Qualcomm MAN_025D tunings). But it need NOT: on Cirrus cs35l56
    # platforms (issue #26) the filename is FUNC_3556 while sysfs reports parts
    # 3557 (amps) / 4245 (codec), and the XML's own security-key confirms 3556
    # is a device id, SUBSYS_<pci> the per-device key. So FUNC is *preferred,
    # not required*: we first match (man, part) exactly (sdw_man_func), and only
    # if nothing matches that way fall back to PCI-subsystem + manufacturer
    # (sdw_man_ids). That keeps the old behaviour verbatim where FUNC equals a
    # part — important because some Lenovo SKUs ship two tunings sharing
    # MAN+SUBSYS but differing in FUNC (e.g. SUBSYS_383917AA: FUNC_0721 vs
    # FUNC_1320); the exact (man, part) tier still disambiguates those.
    sdw_man_func = {(m.upper(), p.upper()) for m, p in sdw_devices}
    sdw_man_ids = {m for m, _p in sdw_man_func}

    driver_store = _resolve_driver_store(windows_root)
    if driver_store is None:
        file_repo = windows_root / "System32" / "DriverStore" / "FileRepository"
        raise FileNotFoundError(
            f"DriverStore not found at {file_repo} and {windows_root} does not "
            f"contain dax3_ext_*.inf_* subdirectories or Dolby-shaped XMLs. "
            f"Pass either a Windows system root or an extracted DriverStore."
        )

    # Scan for XMLs inside dax3_ext_*.inf_* wrappers, falling back to the
    # driver_store root itself if no wrappers are present (layout 4).
    xml_dirs = sorted(driver_store.glob(_INF_WRAPPER_GLOB))
    if not xml_dirs:
        xml_dirs = [driver_store]
    candidates = []
    # HDA files whose SUBSYS matches a codec subsystem but whose DEV token is
    # NOT that codec's device id (see the hda_dev_subsys note above).
    hda_subsys_only = []
    # SoundWire files matched by PCI subsystem + manufacturer but whose FUNC is
    # NOT a detected part id (FUNC preferred-not-required; see note above).
    sdw_pci_only = []
    # Every DAX3-eligible file enumerated, reused by the content scan on no-match.
    scanned_files = []
    for dax_dir in xml_dirs:
        for xml_file in sorted(dax_dir.glob("*.[xX][mM][lL]")):
            if xml_file.name.lower().endswith(_NON_DAX3_FILENAME_SUFFIXES):
                continue
            scanned_files.append(xml_file)
            name = xml_file.name.upper()

            # Match HDA-style: DEV_XXXX_SUBSYS_YYYYYYYY_...
            # Also matches INTELAUDIO_DEV_... variants
            if "DEV_" in name and "SUBSYS_" in name:
                match = re.search(r"SUBSYS_([0-9A-F]{8})", name)
                if match and match.group(1) in hda_subsys_ids:
                    dev = re.search(r"DEV_([0-9A-F]{4})", name)
                    if dev and (dev.group(1), match.group(1)) in hda_dev_subsys:
                        candidates.append(xml_file)
                    else:
                        hda_subsys_only.append(xml_file)
                    continue
                # PCI-keyed fallback for Apple Boot Camp tunings on Intel Macs
                # (issue #21), e.g. PCI_DEV_1803_SUBSYS_1880106B_PCI_SUBSYS_...,
                # whose first SUBSYS token is the audio function's PCI subsystem
                # in device-first order (106B = Apple), not an HDA codec
                # subsystem. Tentative — unverified on real T2-Mac Linux
                # hardware. Additive and safe: HDA/SoundWire filenames use the
                # opposite byte order, so this cannot mis-match them.
                if match and pci_subsys_id and match.group(1) == pci_subsys_id:
                    candidates.append(xml_file)
                    continue

            # Match SoundWire-style: SOUNDWIRE_MAN_XXXX_FUNC_YYYY_SUBSYS_ZZZZZZZZ
            # or SOUNDWIRE_SDCAFUNCTION_NN_MAN_XXXX_FUNC_YYYY_SUBSYS_ZZZZZZZZ.
            # ZZZZZZZZ is the PCI subsystem (device-first, unique per SKU).
            # Exact (man, part) is a strong match; a non-part FUNC drops to the
            # PCI-subsystem fallback (sdw_pci_only) — see the token note above.
            sdw_match = re.search(
                r"MAN_([0-9A-F]{4})_FUNC_([0-9A-F]{4})_SUBSYS_([0-9A-F]{8})",
                name,
            )
            if sdw_match:
                man, func, subsys = sdw_match.group(1, 2, 3)
                if subsys == pci_subsys_id and man in sdw_man_ids:
                    if (man, func) in sdw_man_func:
                        candidates.append(xml_file)
                    else:
                        sdw_pci_only.append(xml_file)
                    continue

            # Match SDW_XXXX_SUBSYS_YYYYYYYY_... style
            sdw_alt = re.search(r"^SDW_[0-9A-F]+_SUBSYS_([0-9A-F]{8})", name)
            if sdw_alt and pci_subsys_id and sdw_alt.group(1) == pci_subsys_id:
                candidates.append(xml_file)
                continue

    # No (DEV, SUBSYS)-exact HDA match: accept the subsystem-only fallback.
    if not candidates and hda_subsys_only:
        candidates = hda_subsys_only
        if len(hda_subsys_only) > 1:
            console.warn(
                "Multiple tunings match the codec subsystem but none match its "
                "device id; selecting the highest tuning_version. Pass the XML "
                "path explicitly if the result sounds wrong."
            )

    # No exact (man, part) / HDA / Apple match: accept the PCI-subsystem fallback.
    if not candidates and sdw_pci_only:
        candidates = sdw_pci_only
        if len(sdw_pci_only) > 1:
            console.warn(
                "Multiple SoundWire tunings share this PCI subsystem with a "
                "non-part FUNC; selecting the highest tuning_version. Pass the "
                "XML path explicitly if the result sounds wrong."
            )

    if not candidates:
        hda_info = ", ".join(f"vendor={v} subsys={s}" for v, s, _name in hda_codecs)
        sdw_info = ", ".join(f"man={m} part={p}" for m, p in sdw_devices)
        pci_info = f"pci_subsys={pci_subsys}" if pci_subsys else "no PCI subsystem"
        detected = (
            f"Detected HDA codecs: {hda_info or 'none'}; "
            f"SoundWire devices: {sdw_info or 'none'}; {pci_info}"
        )

        # No filename matched. Fall back to *content*: parse each XML's
        # security-key and keep internal-speaker tunings whose manufacturer is
        # present (issue #26). Nothing to guess from on a pure-HDA machine.
        guesses = (
            _scan_speaker_tunings_by_manufacturer(scanned_files, sdw_man_ids)
            if sdw_man_ids
            else []
        )

        # Authoritative content match: the security-key's own PCI subsystem
        # equals this machine's. As specific as a filename SUBSYS match, so use
        # it automatically even without --best-guess — covers a tuning whose
        # filename convention we don't parse but whose security-key we do.
        exact = [g for g in guesses if pci_subsys_id and g[2] == pci_subsys_id]
        if len(exact) == 1:
            path = exact[0][0]
            console.cprint("ok", f"Matched tuning XML (by security-key PCI subsystem): {path}")
            return path

        if best_guess and len(guesses) == 1:
            path, man, subsys = guesses[0]
            console.warn(
                f"--best-guess: no exact hardware match; using the only "
                f"internal-speaker tuning for manufacturer {man} — {path.name} "
                f"(SUBSYS_{subsys}). Unverified: matched by manufacturer only, "
                f"not by device id."
            )
            console.cprint("ok", f"Matched tuning XML (best-guess): {path}")
            return path

        lines = [f"No matching DAX3 tuning XML found in {driver_store}. {detected}"]
        if guesses:
            if best_guess and len(guesses) > 1:
                lines.append(
                    f"\n--best-guess found {len(guesses)} internal-speaker tunings "
                    f"for your manufacturer and will not guess between them — pass "
                    f"one as the positional XML path argument:"
                )
            else:
                lines.append(
                    f"\n{len(guesses)} internal-speaker tuning(s) match your "
                    f"manufacturer — pass one as the positional XML path argument "
                    f"(or re-run with --best-guess if there is exactly one):"
                )
            lines += [f"  {p}   # MAN_{m} SUBSYS_{s}" for p, m, s in guesses]
        raise FileNotFoundError("\n".join(lines))

    if len(candidates) > 1:
        # Prefer the highest tuning version from the XML metadata. Parse each
        # candidate once, recording both the numeric version (sort key) and
        # the raw value string (display); on a parse/decode failure both fall
        # back to 0 / "?" so the malformed candidate sorts last and prints
        # without crashing the listing.
        def parse_version(path):
            # ver is the raw value string for display; version is its int form
            # for sorting (0 when absent/non-numeric, so it sorts last).
            try:
                tv = ET.parse(path).getroot().find("tuning_version")
                ver = tv.get("value", "?") if tv is not None else "?"
            except (ET.ParseError, ValueError, AttributeError):
                return path, 0, "?"
            try:
                version = int(tv.get("value", "0")) if tv is not None else 0
            except (ValueError, AttributeError):
                version = 0
            return path, version, ver

        ranked = sorted(
            (parse_version(c) for c in candidates),
            key=lambda pv: pv[1],
            reverse=True,
        )
        candidates = [path for path, _version, _ver in ranked]
        console.cprint("head", "Multiple matching XMLs found, using highest tuning version:")
        for i, (c, _version, ver) in enumerate(ranked):
            if i == 0:
                console.cprint("ok", f"  → {c} (tuning_version={ver})")
            else:
                print(f"    {c} (tuning_version={ver})")
    else:
        console.cprint("ok", f"Matched tuning XML: {candidates[0]}")

    return candidates[0]
