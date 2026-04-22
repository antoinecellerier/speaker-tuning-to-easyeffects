#!/usr/bin/env python3
"""Convert Dolby DAX3 tuning XML to EasyEffects output presets.

Generates minimum-phase FIR impulse responses from the Dolby IEQ target
curves and audio-optimizer speaker correction, then creates EasyEffects
presets using the Convolver plugin for the combined EQ and a parametric
Equalizer for the explicit speaker PEQ filters.

This avoids all parametric bell filter overlap/solver issues — the FIR
directly implements the exact target frequency response.

Output chain:
  - convolver#0: IEQ curve + audio-optimizer (as FIR impulse response)
  - bass_enhancer#0: psychoacoustic bass via harmonic generation
  - stereo_tools#0: surround virtualizer (stereo widening from surround-boost)
  - equalizer#0: speaker PEQ bells + high-pass (parametric filters from Dolby)
  - equalizer#1: dialog enhancer (speech presence boost from dialog-enhancer settings)
  - autogain#0: volume leveler (from volume-leveler settings)
  - multiband_compressor#0: dynamics processing (from mb-compressor-tuning)
  - multiband_compressor#1: per-band limiter (from regulator-tuning)
  - limiter#0: brickwall output limiter (safety net)
"""

import argparse
import json
import math
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.io import wavfile

_FLATPAK_BASE = Path.home() / ".var" / "app" / "com.github.wwmm.easyeffects" / "config" / "easyeffects"
_NATIVE_BASE = Path.home() / ".local" / "share" / "easyeffects"
_EASYEFFECTS_BASE = _FLATPAK_BASE if _FLATPAK_BASE.exists() else _NATIVE_BASE

DEFAULT_OUTPUT_DIR = _EASYEFFECTS_BASE / "output"
DEFAULT_IRS_DIR = _EASYEFFECTS_BASE / "irs"
DEFAULT_AUTOLOAD_DIR = _EASYEFFECTS_BASE / "autoload" / "output"

SAMPLE_RATE = 48000
FIR_LENGTH = 4096  # ~85ms, plenty for EQ


def parse_csv_ints(s: str) -> list[int]:
    return [int(x) for x in s.split(",")]


def get_hda_codec_ids():
    """Read HDA codec names and subsystem IDs from /proc/asound.

    Returns a list of (vendor_id, subsystem_id, codec_name) tuples, e.g.
    [("10EC0287", "17AA22E6", "Realtek ALC287")].
    """
    results = []
    for codec_path in sorted(Path("/proc/asound").glob("card*/codec*")):
        try:
            text = codec_path.read_text()
        except OSError:
            continue
        codec_name = ""
        vendor_id = None
        subsys_id = None
        for line in text.splitlines():
            if line.startswith("Codec:"):
                codec_name = line.split(":", 1)[1].strip()
            elif line.startswith("Vendor Id:"):
                vendor_id = line.split("0x", 1)[-1].strip().upper()
            elif line.startswith("Subsystem Id:"):
                subsys_id = line.split("0x", 1)[-1].strip().upper()
        if vendor_id and subsys_id:
            results.append((vendor_id, subsys_id, codec_name))
    return results


def get_soundwire_ids():
    """Read SoundWire device IDs from /sys/bus/soundwire/devices.

    Returns a list of (manufacturer_id, part_id) tuples as uppercase hex
    strings, e.g. [("025D", "1318")].
    """
    results = []
    sdw_path = Path("/sys/bus/soundwire/devices")
    if not sdw_path.is_dir():
        return results
    for dev_dir in sorted(sdw_path.iterdir()):
        # SoundWire slave devices look like "sdw:L:N:MMMM:PPPP:VV"
        match = re.match(
            r"sdw:\d+:\d+:([0-9a-fA-F]{4}):([0-9a-fA-F]{4}):\d+", dev_dir.name
        )
        if match:
            man_id = match.group(1).upper()
            part_id = match.group(2).upper()
            results.append((man_id, part_id))
    return results


def _walk_to_pci_subsys(start: Path):
    """Walk up sysfs from `start` to find the nearest PCI subsystem IDs."""
    current = start.resolve()
    while current != Path("/"):
        subsys_vendor_path = current / "subsystem_vendor"
        subsys_device_path = current / "subsystem_device"
        if subsys_vendor_path.exists() and subsys_device_path.exists():
            try:
                vendor = subsys_vendor_path.read_text().strip()
                device = subsys_device_path.read_text().strip()
            except OSError:
                pass
            else:
                vendor = vendor.replace("0x", "").upper()
                device = device.replace("0x", "").upper()
                if vendor and device:
                    return (vendor, device)
        current = current.parent
    return None


def get_pci_audio_subsystem():
    """Get the PCI subsystem ID of the audio controller.

    Returns (subsys_vendor, subsys_device) as uppercase 4-char hex strings,
    e.g. ("17AA", "2339"), or None if not found.

    Prefers the PCI ancestor of a SoundWire device when present so we pick
    the controller that actually hosts the speaker amplifiers, rather than
    whichever /sys/class/sound card sorts first (which may be HDMI audio
    on a discrete GPU). Falls back to walking up from sound cards for
    traditional HDA systems.
    """
    sdw_bus = Path("/sys/bus/soundwire/devices")
    if sdw_bus.is_dir():
        for dev_dir in sorted(sdw_bus.iterdir()):
            result = _walk_to_pci_subsys(dev_dir)
            if result:
                return result

    pci_path = Path("/sys/class/sound")
    if not pci_path.is_dir():
        return None
    for card_dir in sorted(pci_path.glob("card*")):
        device_link = card_dir / "device"
        if not device_link.exists():
            continue
        result = _walk_to_pci_subsys(device_link)
        if result:
            return result
    return None


@dataclass
class SpeakerPin:
    """A single internal speaker output (HDA pin or SoundWire amplifier)."""
    node: str            # HDA node ID or SoundWire device name
    control_name: str    # ALSA control name or driver name
    role: str            # "woofer" or "tweeter"
    stereo: bool = True


@dataclass
class SpeakerInfo:
    """Collected audio hardware information for --speaker-info."""
    product: str = ""
    family: str = ""
    kernel: str = ""
    sound_cards: list[str] = field(default_factory=list)
    hda_codecs: list[tuple[str, str, str]] = field(default_factory=list)
    soundwire_devices: list[tuple[str, str]] = field(default_factory=list)
    pci_subsystem: tuple[str, str] | None = None
    pcm_devices: list[tuple[str, str]] = field(default_factory=list)
    # SoundWire-specific
    sdw_codecs: list[str] = field(default_factory=list)
    sdw_amplifiers: list[str] = field(default_factory=list)
    # Speaker pins (HDA or SoundWire)
    speakers: list[SpeakerPin] = field(default_factory=list)

    @property
    def bus_type(self) -> str:
        if self.soundwire_devices:
            return "soundwire"
        if self.hda_codecs:
            return "hda"
        return "unknown"

    @property
    def layout_summary(self) -> str:
        if not self.speakers:
            return "Could not determine speaker layout"
        total = sum(2 if s.stereo else 1 for s in self.speakers)
        by_role: dict[str, int] = {}
        for s in self.speakers:
            by_role[s.role] = by_role.get(s.role, 0) + (2 if s.stereo else 1)
        if len(self.speakers) == 1:
            return f"{total} speakers → full-range stereo"
        parts = " + ".join(f"{n}x {role}" for role, n in by_role.items())
        return f"{total} speakers → multi-way: {parts}"


def _detect_soundwire_speakers(info: SpeakerInfo):
    """Detect speaker amplifiers on the SoundWire bus."""
    sdw_path = Path("/sys/bus/soundwire/devices")
    if not sdw_path.is_dir():
        return

    amp_patterns = ("rt13", "rt_amp", "max98", "cs35")

    for dev_dir in sorted(sdw_path.iterdir()):
        match = re.match(
            r"sdw:\d+:\d+:([0-9a-fA-F]{4}):([0-9a-fA-F]{4}):\d+",
            dev_dir.name,
        )
        if not match:
            continue
        driver_link = dev_dir / "driver"
        driver_name = driver_link.resolve().name if driver_link.is_symlink() else ""
        lower_driver = driver_name.lower()

        if any(p in lower_driver for p in amp_patterns):
            info.sdw_amplifiers.append(f"{dev_dir.name} (driver: {driver_name})")
            info.speakers.append(SpeakerPin(
                node=dev_dir.name,
                control_name=driver_name,
                role="amplifier",
            ))
        else:
            info.sdw_codecs.append(f"{dev_dir.name} (driver: {driver_name})")

    if info.speakers:
        return

    # Fallback: check ALSA mixer for amp controls when sysfs gives nothing
    try:
        result = subprocess.run(
            ["amixer", "-c0", "scontrols"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            m = re.search(r"'(rt\d+[^']*|max98[^']*|cs35[^']*)\s+DAC'", line, re.I)
            if m:
                name = m.group(1)
                info.sdw_amplifiers.append(f"{name} (from ALSA mixer)")
                info.speakers.append(SpeakerPin(
                    node="mixer", control_name=name, role="amplifier",
                ))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _detect_hda_speakers(info: SpeakerInfo):
    """Detect internal speakers from HDA codec pin configurations."""
    for codec_path in sorted(Path("/proc/asound").glob("card*/codec*")):
        try:
            text = codec_path.read_text()
        except OSError:
            continue
        nodes = re.split(r"(?=^Node 0x[0-9a-fA-F]+ )", text, flags=re.MULTILINE)
        for block in nodes:
            if "[Pin Complex]" not in block or "[Fixed] Speaker at Int" not in block:
                continue
            node_match = re.match(r"Node (0x[0-9a-fA-F]+)", block)
            if not node_match:
                continue
            ctrl_match = re.search(r'Control: name="([^"]+)"', block)
            ctrl_name = ctrl_match.group(1) if ctrl_match else "Speaker"
            lower = ctrl_name.lower()
            role = "woofer" if ("bass" in lower or "woofer" in lower) else "tweeter"
            info.speakers.append(SpeakerPin(
                node=node_match.group(1),
                control_name=ctrl_name,
                role=role,
                stereo="Stereo" in block.split("\n", 1)[0],
            ))


def _gather_speaker_info() -> SpeakerInfo:
    """Collect all audio hardware information into a SpeakerInfo."""
    import platform

    info = SpeakerInfo(kernel=platform.release())

    # System identity
    for attr, path in [("product", "/sys/class/dmi/id/product_name"),
                       ("family", "/sys/class/dmi/id/product_family")]:
        p = Path(path)
        if p.exists():
            setattr(info, attr, p.read_text().strip())

    # Sound cards
    cards_path = Path("/proc/asound/cards")
    if cards_path.exists():
        info.sound_cards = [l.strip() for l in cards_path.read_text().strip().splitlines()]

    # Bus-agnostic detection
    info.hda_codecs = get_hda_codec_ids()
    info.soundwire_devices = get_soundwire_ids()
    info.pci_subsystem = get_pci_audio_subsystem()

    # PCM playback devices
    for card_dir in sorted(Path("/proc/asound").glob("card*")):
        for pcm_dir in sorted(card_dir.glob("pcm*p")):
            info_path = pcm_dir / "info"
            if not info_path.exists():
                continue
            fields = {}
            for line in info_path.read_text().splitlines():
                if ": " in line:
                    k, v = line.split(": ", 1)
                    fields[k.strip()] = v.strip()
            info.pcm_devices.append((fields.get("device", "?"), fields.get("id", "?")))

    # Speaker detection — branch by bus type
    if info.bus_type == "soundwire":
        _detect_soundwire_speakers(info)
    elif info.bus_type == "hda":
        _detect_hda_speakers(info)

    return info


def _print_speaker_info(info: SpeakerInfo):
    """Print the collected speaker info report."""
    sections = []

    # System
    lines = []
    if info.product:
        lines.append(f"  Product: {info.product}")
    if info.family:
        lines.append(f"  Family:  {info.family}")
    lines.append(f"  Kernel:  {info.kernel}")
    sections.append(("System", lines))

    # Sound cards
    sections.append(("Sound cards",
                      [f"  {c}" for c in info.sound_cards] or ["  (none found)"]))

    # HDA codecs
    sections.append(("HDA codecs",
                      [f"  {name or 'Unknown'} — Vendor: 0x{v}  Subsystem: 0x{s}"
                       for v, s, name in info.hda_codecs]
                      or ["  (none)"]))

    # SoundWire devices
    sections.append(("SoundWire devices",
                      [f"  Manufacturer: 0x{m}  Part: 0x{p}" for m, p in info.soundwire_devices]
                      or ["  (none)"]))

    # PCI audio subsystem
    pci_line = f"  Subsystem: {info.pci_subsystem[0]}:{info.pci_subsystem[1]}" if info.pci_subsystem else "  (none)"
    sections.append(("PCI audio subsystem", [pci_line]))

    # Speaker amplifiers / HDA pins (bus-specific section)
    if info.bus_type == "soundwire":
        amp_lines = [f"  Codec: {c}" for c in info.sdw_codecs]
        amp_lines += [f"  Amplifier: {a}" for a in info.sdw_amplifiers]
        if not info.sdw_amplifiers:
            amp_lines.append("  (no speaker amplifiers detected)")
        sections.append(("Speaker amplifiers", amp_lines))
    elif info.bus_type == "hda" and info.speakers:
        sections.append(("HDA internal speakers", [
            f"  {s.node}: {s.control_name} ({s.role}, {'stereo' if s.stereo else 'mono'})"
            for s in info.speakers
        ]))

    # PCM playback devices
    sections.append(("PCM playback devices",
                      [f"  pcm{dev}p: {name}" for dev, name in info.pcm_devices]))

    # Speaker layout estimate
    sections.append(("Speaker layout estimate", [f"  {info.layout_summary}"]))

    for title, lines in sections:
        print(f"=== {title} ===")
        print("\n".join(lines))
        print()


def report_speaker_info():
    """Report detected audio hardware and speaker layout."""
    info = _gather_speaker_info()
    _print_speaker_info(info)


def find_tuning_xml(windows_root: Path):
    """Find the DAX3 tuning XML matching this machine's audio hardware.

    Searches the Windows DriverStore for DAX3 tuning XMLs and matches
    against:
    - HDA codec subsystem IDs from /proc/asound (traditional HDA codecs)
    - SoundWire device IDs + PCI subsystem ID (newer Intel platforms)
    """
    hda_codecs = get_hda_codec_ids()
    sdw_devices = get_soundwire_ids()
    pci_subsys = get_pci_audio_subsystem()

    if not hda_codecs and not sdw_devices:
        raise FileNotFoundError(
            "No HDA codecs or SoundWire devices found. "
            "Cannot auto-detect audio hardware."
        )

    # HDA subsystem IDs for matching DEV_*_SUBSYS_*.xml files
    hda_subsys_ids = {s.upper() for _, s, _name in hda_codecs}

    # SoundWire: build expected SUBSYS value from PCI subsystem ID.
    # Dolby filenames encode it as {pci_subsys_device}{pci_subsys_vendor},
    # e.g. PCI subsystem 17AA:2339 -> SUBSYS_233917AA
    if sdw_devices and pci_subsys is None:
        raise RuntimeError(
            "SoundWire devices detected but could not determine PCI subsystem ID. "
            "Cannot safely select a tuning XML."
        )
    sdw_subsys_id = None
    if pci_subsys:
        vendor, device = pci_subsys
        sdw_subsys_id = f"{device}{vendor}".upper()

    # SoundWire manufacturer+function pairs for matching
    sdw_man_func = {(m.upper(), p.upper()) for m, p in sdw_devices}

    # Search DriverStore for DAX3 tuning XMLs.
    # Accept either a Windows system root (needs the FileRepository subpath),
    # the root of a mounted C: drive (a sibling Windows/ subdir contains the
    # system root), or an already-extracted DriverStore directory containing
    # dax3_ext_*.inf_* subdirectories directly.
    file_repo = windows_root / "System32" / "DriverStore" / "FileRepository"
    driver_store = None
    if file_repo.is_dir():
        driver_store = file_repo
    elif windows_root.is_dir():
        if any(windows_root.glob("dax3_ext_*.inf_*")):
            driver_store = windows_root
        else:
            # Maybe the user passed the C:\ mount point instead of C:\Windows.
            # Look for a case-insensitive "Windows" subdirectory with the
            # expected DriverStore layout.
            for child in windows_root.iterdir():
                if not child.is_dir() or child.name.lower() != "windows":
                    continue
                nested = child / "System32" / "DriverStore" / "FileRepository"
                if nested.is_dir():
                    driver_store = nested
                    break
    if driver_store is None:
        raise FileNotFoundError(
            f"DriverStore not found at {file_repo} and {windows_root} does not "
            f"contain dax3_ext_*.inf_* subdirectories. "
            f"Pass either a Windows system root or an extracted DriverStore."
        )

    # Look for dax3_ext_*.inf_* directories
    candidates = []
    for dax_dir in sorted(driver_store.glob("dax3_ext_*.inf_*")):
        for xml_file in sorted(dax_dir.glob("*.[xX][mM][lL]")):
            if xml_file.name.lower().endswith("_settings.xml"):
                continue
            name = xml_file.name.upper()

            # Match HDA-style: DEV_XXXX_SUBSYS_YYYYYYYY_...
            # Also matches INTELAUDIO_DEV_... variants
            if "DEV_" in name and "SUBSYS_" in name:
                match = re.search(r"SUBSYS_([0-9A-F]{8})", name)
                if match and match.group(1) in hda_subsys_ids:
                    candidates.append(xml_file)
                    continue

            # Match SoundWire-style: SOUNDWIRE_MAN_XXXX_FUNC_YYYY_SUBSYS_ZZZZZZZZ
            # or SOUNDWIRE_SDCAFUNCTION_NN_MAN_XXXX_FUNC_YYYY_SUBSYS_ZZZZZZZZ
            sdw_match = re.search(
                r"MAN_([0-9A-F]{4})_FUNC_([0-9A-F]{4})_SUBSYS_([0-9A-F]{8})",
                name,
            )
            if sdw_match:
                man = sdw_match.group(1)
                func = sdw_match.group(2)
                subsys = sdw_match.group(3)
                if (man, func) in sdw_man_func and subsys == sdw_subsys_id:
                    candidates.append(xml_file)
                    continue

            # Match SDW_XXXX_SUBSYS_YYYYYYYY_... style
            sdw_alt = re.search(r"^SDW_[0-9A-F]+_SUBSYS_([0-9A-F]{8})", name)
            if sdw_alt and sdw_subsys_id and sdw_alt.group(1) == sdw_subsys_id:
                candidates.append(xml_file)
                continue

    if not candidates:
        hda_info = ", ".join(f"vendor={v} subsys={s}" for v, s in hda_codecs)
        sdw_info = ", ".join(f"man={m} part={p}" for m, p in sdw_devices)
        pci_info = f"pci_subsys={pci_subsys}" if pci_subsys else "no PCI subsystem"
        raise FileNotFoundError(
            f"No matching DAX3 tuning XML found in {driver_store}. "
            f"Detected HDA codecs: {hda_info or 'none'}; "
            f"SoundWire devices: {sdw_info or 'none'}; {pci_info}"
        )

    if len(candidates) > 1:
        # Prefer the highest tuning version from the XML metadata
        def xml_sort_key(path):
            try:
                root = ET.parse(path).getroot()
                tv = root.find("tuning_version")
                version = int(tv.get("value", "0")) if tv is not None else 0
            except (ET.ParseError, ValueError, AttributeError):
                version = 0
            return version

        candidates.sort(key=xml_sort_key, reverse=True)
        print(f"Multiple matching XMLs found, using highest tuning version:")
        for c in candidates:
            try:
                root = ET.parse(c).getroot()
                tv = root.find("tuning_version")
                ver = tv.get("value", "?") if tv is not None else "?"
            except ET.ParseError:
                ver = "?"
            marker = "→ " if c == candidates[0] else "  "
            print(f"  {marker}{c} (tuning_version={ver})")
    else:
        print(f"Matched tuning XML: {candidates[0]}")

    return candidates[0]


def list_endpoints(path: Path):
    """Print available endpoints and profiles in the XML."""
    tree = ET.parse(path)
    root = tree.getroot()
    for ep in root.findall(".//endpoint"):
        ep_type = ep.get("type")
        op_mode = ep.get("operating_mode")
        profiles = [p.get("type") for p in ep.findall("profile")]
        print(f"  endpoint: {ep_type} (operating_mode={op_mode})")
        for p in profiles:
            print(f"    profile: {p}")


_SAFE_PROFILE_RE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_profile_type(t: str) -> str:
    """Normalize a profile type for safe use in output file paths.

    Profile names flow into `{output_dir}/{...}-{profile}-....json` and the
    matching `.irs`, so values like `../foo` from a crafted XML would escape
    the intended directory. Replace anything outside a plain identifier with
    `_` rather than rejecting — unknown vendor profile names should still
    produce a usable (if ugly) preset name.
    """
    safe = _SAFE_PROFILE_RE.sub("_", t)
    return safe or "_"


def get_profile_types(path: Path, endpoint_type: str, operating_mode: str) -> list[str]:
    """Return all profile type names for the given endpoint/mode, excluding 'off'."""
    tree = ET.parse(path)
    root = tree.getroot()
    ep = root.find(
        f".//endpoint[@type='{endpoint_type}'][@operating_mode='{operating_mode}']"
    )
    if ep is None:
        return []
    return [p.get("type") for p in ep.findall("profile") if p.get("type") != "off"]


def find_speaker_sinks() -> list[dict]:
    """Find internal speaker output sinks from PipeWire.

    Returns a list of dicts with 'name', 'description', and 'profile' keys,
    corresponding to the PipeWire node.name, node.description, and
    device.profile.description properties used by EasyEffects autoload.

    Only returns sinks with the 'audio-speakers' device icon, excluding
    HDMI/DisplayPort outputs.
    """
    try:
        result = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=5
        )
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        return []

    sinks = []
    for obj in data:
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") != "Audio/Sink":
            continue
        # Only include sinks with the speaker icon (excludes HDMI, headphones, etc.)
        if props.get("device.icon_name") != "audio-speakers":
            continue
        sinks.append({
            "name": props.get("node.name", ""),
            "description": props.get("node.description", ""),
            "profile": props.get("device.profile.description", ""),
        })
    return sinks


def write_autoload(autoload_dir: Path, device_name: str, device_description: str,
                   device_profile: str, preset_name: str) -> Path:
    """Write an EasyEffects autoload config file for a device/route → preset mapping.

    EasyEffects loads this file when the given PipeWire sink becomes the active
    output, automatically switching to the named preset.

    File is named '{device_name}:{device_profile}.json' (with '/' replaced by '_'),
    matching EasyEffects' AutoloadManager::getFilePath() convention.
    """
    autoload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = device_name.replace("/", "_")
    safe_profile = device_profile.replace("/", "_")
    path = autoload_dir / f"{safe_name}:{safe_profile}.json"
    path.write_text(json.dumps({
        "device": device_name,
        "device-description": device_description,
        "device-profile": device_profile,
        "preset-name": preset_name,
    }, indent=4) + "\n")
    return path


def resolve_xml_value(element, constants):
    """Resolve a value from either a value= attribute or a preset= reference.

    SoundWire XMLs (e.g. Lunar Lake) use preset references like
    <ch_00 preset="array_20_zero" /> instead of inline value="..." attributes.
    The preset name refers to a named element under <constant> whose target=
    attribute holds the actual CSV data.
    """
    if element is None:
        return ""
    val = element.get("value")
    if val is not None and val != "":
        return val
    preset_name = element.get("preset")
    if preset_name and constants is not None:
        ref = constants.find(preset_name)
        if ref is not None:
            return ref.get("target", "")
    return ""


def parse_xml(path: Path, endpoint_type="internal_speaker",
              operating_mode="normal", profile_type=None):
    tree = ET.parse(path)
    root = tree.getroot()
    constant = root.find("constant")

    freqs = parse_csv_ints(constant.find("band_20_freq").get("fs_48000"))

    curves = {}
    for el in constant:
        if el.tag.startswith("ieq_"):
            curves[el.tag] = parse_csv_ints(el.get("target"))

    endpoint = root.find(
        f".//endpoint[@type='{endpoint_type}'][@operating_mode='{operating_mode}']"
    )
    if endpoint is None:
        raise ValueError(
            f"Endpoint type='{endpoint_type}' operating_mode='{operating_mode}' "
            f"not found. Use --list to see available endpoints."
        )

    # Select the profile for vlldp settings (AO, PEQ, MB compressor)
    if profile_type:
        profile = endpoint.find(f"profile[@type='{profile_type}']")
        if profile is None:
            available = [p.get("type") for p in endpoint.findall("profile")]
            raise ValueError(
                f"Profile '{profile_type}' not found. "
                f"Available: {', '.join(available)}"
            )
    else:
        profile = endpoint.find("profile")

    # IEQ amount from the selected profile's tuning-cp (or first with IEQ enabled)
    ieq_amount = 10
    cp = profile.find("tuning-cp")
    if cp is not None:
        enable = cp.find("ieq-enable")
        if enable is not None and enable.get("value") == "1":
            amt = cp.find("ieq-amount")
            if amt is not None:
                ieq_amount = int(amt.get("value"))

    vlldp = profile.find("tuning-vlldp")

    ao_bands = vlldp.find("audio-optimizer-bands")
    ao_left = parse_csv_ints(resolve_xml_value(ao_bands.find("ch_00"), constant))
    ao_right = parse_csv_ints(resolve_xml_value(ao_bands.find("ch_01"), constant))

    peq_filters = []
    peq_enable = vlldp.find("speaker-peq-enable")
    if peq_enable is None or peq_enable.get("value") != "0":
        for f in vlldp.findall(".//speaker-peq-filters/filter"):
            if f.get("enabled") == "0":
                continue
            ftype = int(f.get("type"))
            if ftype not in (1, 4, 7, 9):
                print(f"  Warning: unknown PEQ filter type {ftype}, skipping")
                continue
            peq_filters.append({
                "speaker": int(f.get("speaker")),
                "type": ftype,
                "f0": float(f.get("f0")),
                "gain": float(f.get("gain", "0")),
                "q": float(f.get("q", "0.707")),
                "s": float(f.get("s", "1.0")),
                "order": int(f.get("order", "0")),
            })

    # Volume leveler settings (from tuning-cp of the selected profile)
    vol_leveler = None
    if cp is not None:
        vl_enable = cp.find("volume-leveler-enable")
        if vl_enable is not None:
            vl_amount = cp.find("volume-leveler-amount")
            vl_in = cp.find("volume-leveler-in-target")
            vl_out = cp.find("volume-leveler-out-target")
            vol_leveler = {
                "enable": int(vl_enable.get("value")),
                "amount": int(vl_amount.get("value")) if vl_amount is not None else 0,
                "in_target": int(vl_in.get("value")) / 16.0 if vl_in is not None else -20.0,
                "out_target": int(vl_out.get("value")) / 16.0 if vl_out is not None else -20.0,
            }

    # volmax-boost (tuning-cp) — Dolby's loudness-maximiser ceiling: the
    # maximum gain above the volume leveler's out-target. Parsed outside
    # the MBC block because the regulator is the preferred injection point
    # and MBC may be disabled on some profiles.
    volmax_boost = 0.0
    if cp is not None:
        volmax = cp.find("volmax-boost")
        if volmax is not None:
            volmax_boost = int(volmax.get("value")) / 16.0

    # Dialog enhancer settings (from tuning-cp)
    dialog_enhancer = None
    if cp is not None:
        de_enable = cp.find("dialog-enhancer-enable")
        if de_enable is not None and de_enable.get("value") == "1":
            de_amount = cp.find("dialog-enhancer-amount")
            dialog_enhancer = {
                "amount": int(de_amount.get("value")) if de_amount is not None else 5,
            }

    # Surround virtualizer settings (from tuning-cp)
    surround = None
    if cp is not None:
        sr_enable = cp.find("surround-decoder-enable")
        if sr_enable is not None and sr_enable.get("value") == "1":
            sr_boost = cp.find("surround-boost")
            surround = {
                "boost": int(sr_boost.get("value")) / 16.0 if sr_boost is not None else 0.0,
            }

    # Multi-band compressor settings (from tuning-vlldp)
    mb_comp = None
    mbc_enable = vlldp.find("mb-compressor-enable")
    if mbc_enable is not None and mbc_enable.get("value") == "1":
        mbc_tuning = vlldp.find("mb-compressor-tuning")
        if mbc_tuning is not None:
            group_count = int(mbc_tuning.find("group_count").get("value"))
            band_groups = []
            for i in range(4):
                bg = mbc_tuning.find(f"band_group_{i}")
                if bg is not None:
                    band_groups.append(parse_csv_ints(bg.get("value")))
            target_power = vlldp.find("mb-compressor-target-power-level")
            # Also grab regulator stress for additional context
            reg_stress = vlldp.find("regulator-stress-amount")
            mb_comp = {
                "group_count": group_count,
                "band_groups": band_groups,
                "target_power": int(target_power.get("value")) / 16.0 if target_power is not None else -5.0,
                "reg_stress": parse_csv_ints(reg_stress.get("value")) if reg_stress is not None else [],
            }

    # Regulator settings (per-band limiter from tuning-vlldp)
    regulator = None
    reg_dist = vlldp.find("regulator-speaker-dist-enable")
    if reg_dist is not None and reg_dist.get("value") == "1":
        reg_tuning = vlldp.find("regulator-tuning")
        if reg_tuning is not None:
            th_el = reg_tuning.find("threshold_high")
            tl_el = reg_tuning.find("threshold_low")
            th_val = resolve_xml_value(th_el, constant)
            tl_val = resolve_xml_value(tl_el, constant)
            th = [x / 16.0 for x in parse_csv_ints(th_val)] if th_val else [0.0] * 20
            tl = [x / 16.0 for x in parse_csv_ints(tl_val)] if tl_val else [-12.0] * 20
            reg_stress = vlldp.find("regulator-stress-amount")
            stress = parse_csv_ints(reg_stress.get("value")) if reg_stress is not None else [0] * 8
            reg_slope = vlldp.find("regulator-distortion-slope")
            slope = int(reg_slope.get("value")) / 16.0 if reg_slope is not None else 1.0
            reg_timbre = vlldp.find("regulator-timbre-preservation")
            timbre = int(reg_timbre.get("value")) / 16.0 if reg_timbre is not None else 0.75
            regulator = {
                "threshold_high": th,
                "threshold_low": tl,
                "stress": [x / 16.0 for x in stress],
                "distortion_slope": slope,
                "timbre_preservation": timbre,
            }

    return freqs, curves, ieq_amount, ao_left, ao_right, peq_filters, vol_leveler, dialog_enhancer, surround, mb_comp, regulator, volmax_boost


# --- FIR generation ---

def interpolate_curve_db(band_freqs, band_gains_db, fft_freqs):
    """Interpolate a gain curve (in dB) to FFT frequency bins.

    Uses log-frequency interpolation with linear dB values.
    Extrapolates flat beyond the band edges.
    """
    log_bands = np.log(np.maximum(band_freqs, 1.0))
    log_fft = np.log(np.maximum(fft_freqs, 1.0))
    return np.interp(log_fft, log_bands, band_gains_db,
                     left=band_gains_db[0], right=band_gains_db[-1])


def make_fir(band_freqs, gains_db, normalize=True):
    """Generate a minimum-phase FIR filter from a target dB curve.

    Uses homomorphic processing: the minimum-phase impulse response
    is constructed from the log-magnitude spectrum via the cepstrum.
    """
    n = FIR_LENGTH
    fft_freqs = np.fft.rfftfreq(n, d=1.0 / SAMPLE_RATE)

    # Interpolate target curve to FFT bins
    gains_at_bins = interpolate_curve_db(
        np.array(band_freqs, dtype=float),
        np.array(gains_db, dtype=float),
        fft_freqs
    )

    # Log magnitude (natural log for cepstral processing)
    log_mag = gains_at_bins * (np.log(10.0) / 20.0)  # dB to ln(linear)

    # Minimum-phase via cepstrum:
    # 1. IFFT of log-magnitude gives the real cepstrum
    # 2. Causal windowing (double positive-time, zero negative-time)
    # 3. FFT back gives log(H_min) = log|H| + j*phase_min
    # 4. exp() gives H_min, IFFT gives impulse response
    cepstrum = np.fft.irfft(log_mag, n=n)
    # Causal window: keep n=0, double n=1..N/2-1, zero n=N/2..N-1
    cepstrum[1:n // 2] *= 2.0
    cepstrum[n // 2 + 1:] = 0.0
    # Reconstruct minimum-phase spectrum
    log_H_min = np.fft.rfft(cepstrum, n=n)
    H_min = np.exp(log_H_min)
    fir = np.fft.irfft(H_min, n=n)

    peak_mag = np.max(np.abs(H_min))
    peak_db = 20.0 * np.log10(peak_mag + 1e-12)

    if normalize:
        if peak_mag > 0:
            fir /= peak_mag

    return fir, peak_db


def save_wav_stereo(path, fir_left, fir_right):
    """Save stereo impulse response as 32-bit float WAV."""
    stereo = np.column_stack([fir_left, fir_right]).astype(np.float32)
    wavfile.write(str(path), SAMPLE_RATE, stereo)


# --- EasyEffects preset builders ---

def make_band(freq: float, gain: float, q=1.5) -> dict:
    return {
        "frequency": freq,
        "gain": round(gain, 4),
        "mode": "RLC (BT)",
        "mute": False,
        "q": q,
        "slope": "x1",
        "solo": False,
        "type": "Bell",
        "width": 4.0,
    }


def make_convolver(kernel_name: str, output_gain: float = 0.0):
    """Convolver plugin config referencing an IR by name.

    EasyEffects 8.x uses kernel-name (filename stem without extension),
    and looks for the WAV in its irs/ directory.
    """
    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": round(output_gain, 2),
        "kernel-name": kernel_name,
        "ir-width": 100,
        "autogain": False,
    }


def make_hp_band(freq: float, order: int) -> dict:
    """High-pass filter band for speaker protection."""
    # order 4 = 24 dB/oct = x4 slope
    slope_map = {1: "x1", 2: "x2", 3: "x3", 4: "x4"}
    return {
        "frequency": freq,
        "gain": 0.0,
        "mode": "RLC (BT)",
        "mute": False,
        "q": 0.707,
        "slope": slope_map.get(order, "x4"),
        "solo": False,
        "type": "Hi-pass",
        "width": 4.0,
    }


def make_shelf_band(freq: float, gain: float, s: float = 1.0) -> dict:
    """Low-shelf filter band from Dolby PEQ type 4.

    The S (shelf slope) parameter controls the steepness of the transition.
    S=1.0 gives a Butterworth (maximally flat) response with Q≈0.707.
    Convert S to Q using the standard audio shelf formula.
    """
    # S-to-Q conversion for shelving filters:
    # Q = 1/sqrt((A + 1/A) * (1/S - 1) + 2) where A = 10^(gain/40)
    # For S=1.0, this simplifies to Q ≈ 0.707 (Butterworth).
    # The (A + 1/A) term is symmetric in A↔1/A, so the sign of gain
    # doesn't affect Q — boost and cut shelves of equal magnitude
    # share the same Q.
    a = 10 ** (gain / 40.0) if gain != 0 else 1.0
    denom = (a + 1.0 / a) * (1.0 / s - 1.0) + 2.0
    q = 1.0 / math.sqrt(max(denom, 0.01))
    return {
        "frequency": freq,
        "gain": round(gain, 4),
        "mode": "RLC (BT)",
        "mute": False,
        "q": round(q, 4),
        "slope": "x1",
        "solo": False,
        "type": "Lo-shelf",
        "width": 4.0,
    }


def make_peq_eq(peq_filters):
    """Parametric EQ for the explicit speaker PEQ from Dolby.

    Handles filter types: 1 (bell), 4 (low-shelf), 7 and 9 (high-pass).
    The HP protects laptop speakers from sub-bass energy they can't reproduce.
    """
    peq_left_bells = [f for f in peq_filters if f["speaker"] == 0 and f["type"] == 1]
    peq_right_bells = [f for f in peq_filters if f["speaker"] == 1 and f["type"] == 1]
    hp_left = [f for f in peq_filters if f["speaker"] == 0 and f["type"] in (7, 9)]
    hp_right = [f for f in peq_filters if f["speaker"] == 1 and f["type"] in (7, 9)]
    shelf_left = [f for f in peq_filters if f["speaker"] == 0 and f["type"] == 4]
    shelf_right = [f for f in peq_filters if f["speaker"] == 1 and f["type"] == 4]

    num_bells = max(len(peq_left_bells), len(peq_right_bells))
    num_hp = max(len(hp_left), len(hp_right))
    num_shelf = max(len(shelf_left), len(shelf_right))
    num_bands = num_hp + num_shelf + num_bells

    if num_bands == 0:
        return None

    left_bands = {}
    right_bands = {}

    # HP filters first
    for j, pf in enumerate(hp_left):
        left_bands[f"band{j}"] = make_hp_band(pf["f0"], pf["order"])
    for j, pf in enumerate(hp_right):
        right_bands[f"band{j}"] = make_hp_band(pf["f0"], pf["order"])

    # Shelf filters next
    off = num_hp
    for j, pf in enumerate(shelf_left):
        left_bands[f"band{off + j}"] = make_shelf_band(pf["f0"], pf["gain"], pf["s"])
    for j, pf in enumerate(shelf_right):
        right_bands[f"band{off + j}"] = make_shelf_band(pf["f0"], pf["gain"], pf["s"])

    # Bell filters after
    off = num_hp + num_shelf
    for j, pf in enumerate(peq_left_bells):
        left_bands[f"band{off + j}"] = make_band(pf["f0"], pf["gain"], q=pf["q"])
    for j, pf in enumerate(peq_right_bells):
        right_bands[f"band{off + j}"] = make_band(pf["f0"], pf["gain"], q=pf["q"])

    # Fill missing
    for idx in range(num_bands):
        key = f"band{idx}"
        if key not in left_bands:
            if idx < num_hp:
                left_bands[key] = make_hp_band(100.0, 4)
            else:
                left_bands[key] = make_band(1000.0, 0.0)
        if key not in right_bands:
            if idx < num_hp:
                right_bands[key] = make_hp_band(100.0, 4)
            else:
                right_bands[key] = make_band(1000.0, 0.0)

    # Compensate for PEQ boost to prevent clipping. Bells are scaled by
    # bandwidth: a narrow Q=4.6 bell at +4 dB barely raises broadband
    # level, while a wide Q=0.7 bell at +4 dB raises it nearly 4 dB
    # (effective boost ≈ gain * min(1, 2/Q)). Shelves get the full gain
    # because they boost the entire band above the corner frequency.
    effective_boosts = []
    for pf in peq_left_bells + peq_right_bells:
        if pf["gain"] <= 0:
            continue
        q = pf.get("q", 1.0)
        effective_boosts.append(pf["gain"] * min(1.0, 2.0 / q))
    for pf in shelf_left + shelf_right:
        if pf["gain"] <= 0:
            continue
        effective_boosts.append(pf["gain"])
    peak_boost = max(effective_boosts, default=0.0)
    output_gain = -peak_boost

    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": round(output_gain, 2),
        "mode": "IIR",
        "num-bands": num_bands,
        "split-channels": True,
        "left": left_bands,
        "right": right_bands,
    }


def make_stereo_tools(surround):
    """Stereo widening mapped from Dolby surround virtualizer.

    Dolby's surround decoder/virtualizer creates a wider stereo image
    from stereo content. We approximate this using the Calf Stereo
    Tools plugin's stereo-base parameter, which controls the Mid/Side
    balance to widen or narrow the stereo field.

    surround-boost (0-16 in 1/16 dB scale) maps to stereo-base:
      0 dB → 0.0 (no widening)
      6 dB → 0.3 (moderate widening)
    Kept conservative to avoid phase artifacts on laptop speakers.
    """
    if not surround or surround["boost"] <= 0:
        return None

    # Map surround-boost (dB) to stereo-base (0-1 range).
    # 6 dB boost → 0.3 stereo-base (moderate widening).
    # Cap at 0.5 to prevent excessive widening artifacts.
    base = min(surround["boost"] / 20.0, 0.5)

    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "balance-in": 0.0,
        "balance-out": 0.0,
        "softclip": False,
        "mutel": False,
        "muter": False,
        "phasel": False,
        "phaser": False,
        "mode": "LR > LR (Stereo Default)",
        "side-level": 0.0,
        "side-balance": 0.0,
        "middle-level": 0.0,
        "middle-panorama": 0.0,
        "stereo-base": round(base, 2),
        "delay": 0.0,
        "sc-level": 1.0,
        "stereo-phase": 0.0,
    }


def make_dialog_enhancer(dialog_enhancer, is_soundwire=False):
    """Dialog enhancer mapped as a broad speech-band EQ boost.

    Dolby's dialog enhancer (DE) isolates speech frequencies and
    selectively boosts them. We approximate this with a broad Bell
    filter centered at 2.5 kHz (speech presence region), with gain
    scaled by the DE amount (0-16 scale).

    For HDA presets: amount/16 * 6 dB, giving a maximum of +6 dB.
    For SoundWire presets: stronger mapping (amount/16 * 8 dB) plus
    a second bell at 4 kHz for consonant clarity, compensating for
    the simpler full-range speakers on newer platforms.
    """
    if not dialog_enhancer:
        return None

    amount = dialog_enhancer["amount"]

    if is_soundwire:
        gain_presence = round(amount / 16.0 * 8.0, 2)
        gain_clarity = round(gain_presence * 0.6, 2)
        if gain_presence <= 0:
            return None
        return {
            "bypass": False,
            "input-gain": 0.0,
            "output-gain": 0.0,
            "mode": "IIR",
            "num-bands": 2,
            "split-channels": False,
            "left": {
                "band0": make_band(2500.0, gain_presence, q=0.7),
                "band1": make_band(4000.0, gain_clarity, q=1.0),
            },
            "right": {
                "band0": make_band(2500.0, gain_presence, q=0.7),
                "band1": make_band(4000.0, gain_clarity, q=1.0),
            },
        }

    gain = round(amount / 16.0 * 6.0, 2)
    if gain <= 0:
        return None

    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "mode": "IIR",
        "num-bands": 1,
        "split-channels": False,
        "left": {"band0": make_band(2500.0, gain, q=0.7)},
        "right": {"band0": make_band(2500.0, gain, q=0.7)},
    }


def make_autogain(vol_leveler, conservative=False):
    """Autogain plugin mapping from Dolby volume leveler.

    The Dolby volume leveler brings quiet passages up to a target loudness.
    EasyEffects' autogain does the same using EBU R 128 loudness measurement.

    Dolby volume-leveler-amount (0-10) maps to aggressiveness:
      0 = gentle (long history window)
      10 = aggressive (short history window)

    For HDA presets: bypassed by default because the convolver's steep
    spectral shape (IEQ + audio-optimizer) creates ~10 dB peak-to-LUFS
    mismatch that causes distortion without Dolby's MI steering.

    For SoundWire presets (conservative=True): enabled with gentle settings.
    The simpler spectral shape (IEQ only, no AO correction) has much less
    peak-to-LUFS mismatch, so conservative autogain is safe.
    """
    if not vol_leveler or not vol_leveler["enable"]:
        return None

    amount = vol_leveler["amount"]
    target = vol_leveler["out_target"]

    if conservative:
        max_history = max(40 - amount * 4, 15)
        return {
            "bypass": False,
            "input-gain": 0.0,
            "output-gain": 0.0,
            "maximum-history": max_history,
            "reference": "Geometric Mean (MSI)",
            "silence-threshold": -50.0,
            "target": round(target - 6.0, 1),
        }

    max_history = max(30 - amount * 5, 10)
    return {
        "bypass": True,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "maximum-history": max_history,
        "reference": "Geometric Mean (MSI)",
        "silence-threshold": -70.0,
        "target": round(target, 1),
    }


def decode_mbc_time_constant(coeff, block_size=256):
    """Decode a Dolby time constant coefficient to milliseconds.

    Dolby stores time constants as exponential smoothing coefficients
    in Q15 fixed-point format, operating per block (not per sample).
    coeff/32768 = (1 - alpha), where alpha = 1 - exp(-1/(tau * blocks_per_sec)).
    """
    blocks_per_sec = SAMPLE_RATE / block_size
    one_minus_alpha = coeff / 32768.0
    if one_minus_alpha <= 0.0 or one_minus_alpha >= 1.0:
        return 100.0  # fallback
    tau = -1.0 / (blocks_per_sec * math.log(one_minus_alpha))
    return tau * 1000.0  # seconds to ms


def make_multiband_compressor(mb_comp, freqs):
    """Multi-band compressor mapping from Dolby mb-compressor-tuning.

    The Dolby MB compressor uses raw DSP coefficients in 6-tuples:
      [crossover_band_idx, threshold_q4, gain_coeff_q15,
       attack_coeff_q15, release_coeff_q15, makeup_q4]

    Where:
      - crossover_band_idx: index into the 20-band frequency table
      - threshold: in 1/16 dB
      - gain_coeff: Q15 fixed-point, 32767 = unity (bypass)
        ratio ≈ 1 / (gain_coeff / 32768)
      - attack/release: exponential smoothing coefficients (block-rate)
      - makeup: in 1/16 dB
    """
    if not mb_comp or mb_comp["group_count"] < 2:
        return None

    band_groups = mb_comp["band_groups"]
    if len(band_groups) < 2:
        return None

    def decode_band(bg):
        xover_idx, thresh_raw, gain_raw, attack_raw, release_raw, makeup_raw = bg
        threshold = thresh_raw / 16.0
        # gain_coeff → ratio: 32767 = 1:1 (bypass), lower = more compression
        gain_frac = gain_raw / 32768.0
        ratio = 1.0 / gain_frac if gain_frac > 0.01 else 100.0
        attack_ms = decode_mbc_time_constant(attack_raw)
        release_ms = decode_mbc_time_constant(release_raw)
        makeup = makeup_raw / 16.0
        return {
            "xover_idx": xover_idx,
            "threshold": threshold,
            "ratio": ratio,
            "attack_ms": attack_ms,
            "release_ms": release_ms,
            "makeup": makeup,
        }

    band0 = decode_band(band_groups[0])
    band1 = decode_band(band_groups[1])

    # Crossover frequency from band index into the 20-freq table
    xover_idx = band0["xover_idx"]
    if 0 <= xover_idx < len(freqs):
        crossover_freq = float(freqs[xover_idx])
    else:
        crossover_freq = 500.0  # fallback

    # Build EasyEffects multiband compressor with 2 active bands
    # Band 0 = low (below crossover), Band 1 = high (above crossover)
    # Bands 2-7 are disabled
    result = {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "dry": -80.01,
        "wet": 0.0,
        "compressor-mode": "Modern",
        "envelope-boost": "None",
        "stereo-split": False,
    }

    for i in range(8):
        bandn = f"band{i}"
        if i == 0:
            # Low band — always enabled, no split-frequency
            b = band0
            result[bandn] = {
                "compressor-enable": True,
                "mute": False,
                "solo": False,
                "attack-threshold": round(b["threshold"], 1),
                "attack-time": round(b["attack_ms"], 1),
                "release-threshold": -80.01,
                "release-time": round(b["release_ms"], 1),
                "ratio": round(b["ratio"], 2),
                "knee": -6.0,
                "makeup": round(b["makeup"], 1),
                "compression-mode": "Downward",
                "sidechain-type": "Internal",
                "sidechain-mode": "RMS",
                "sidechain-source": "Middle",
                "stereo-split-source": "Left/Right",
                "sidechain-lookahead": 0.0,
                "sidechain-reactivity": 10.0,
                "sidechain-preamp": 0.0,
                "sidechain-custom-lowcut-filter": False,
                "sidechain-custom-highcut-filter": False,
                "sidechain-lowcut-frequency": 10.0,
                "sidechain-highcut-frequency": crossover_freq,
                "boost-threshold": -60.0,
                "boost-amount": 0.0,
            }
        elif i == 1:
            # High band
            b = band1
            result[bandn] = {
                "enable-band": True,
                "split-frequency": crossover_freq,
                "compressor-enable": True,
                "mute": False,
                "solo": False,
                "attack-threshold": round(b["threshold"], 1),
                "attack-time": round(b["attack_ms"], 1),
                "release-threshold": -80.01,
                "release-time": round(b["release_ms"], 1),
                "ratio": round(b["ratio"], 2),
                "knee": -6.0,
                "makeup": round(b["makeup"], 1),
                "compression-mode": "Downward",
                "sidechain-type": "Internal",
                "sidechain-mode": "RMS",
                "sidechain-source": "Middle",
                "stereo-split-source": "Left/Right",
                "sidechain-lookahead": 0.0,
                "sidechain-reactivity": 10.0,
                "sidechain-preamp": 0.0,
                "sidechain-custom-lowcut-filter": False,
                "sidechain-custom-highcut-filter": False,
                "sidechain-lowcut-frequency": crossover_freq,
                "sidechain-highcut-frequency": 20000.0,
                "boost-threshold": -60.0,
                "boost-amount": 0.0,
            }
        else:
            # Disabled bands
            result[bandn] = {
                "enable-band": False,
                "compressor-enable": False,
                "mute": False,
                "solo": False,
                "attack-threshold": -12.0,
                "attack-time": 20.0,
                "release-threshold": -80.01,
                "release-time": 100.0,
                "ratio": 1.0,
                "knee": -6.0,
                "makeup": 0.0,
                "compression-mode": "Downward",
                "sidechain-type": "Internal",
                "sidechain-mode": "RMS",
                "sidechain-source": "Middle",
                "stereo-split-source": "Left/Right",
                "sidechain-lookahead": 0.0,
                "sidechain-reactivity": 10.0,
                "sidechain-preamp": 0.0,
                "sidechain-custom-lowcut-filter": False,
                "sidechain-custom-highcut-filter": False,
                "sidechain-lowcut-frequency": 10.0,
                "sidechain-highcut-frequency": 20000.0,
                "boost-threshold": -60.0,
                "boost-amount": 0.0,
            }

    return result


def make_regulator(regulator, freqs, volmax_boost=0.0):
    """Per-band limiter mapped from Dolby regulator-tuning.

    The Dolby regulator is a 20-band limiter that prevents speaker
    distortion. We approximate it using EasyEffects' multiband compressor
    configured as a limiter.

    The 20 Dolby bands are grouped into zones with similar thresholds
    to fit within EasyEffects' 8-band limit.

    Regulator parameters mapped:
      - distortion_slope: controls limiter ratio. 1.0 = hard limiter
        (infinity:1), lower values = softer limiting. Mapped as
        ratio = 1 / (1 - slope) when slope < 1, else 100:1.
      - timbre_preservation: 0-1, controls knee softness. Higher values
        mean softer knee to preserve spectral shape. Mapped to
        knee = -6 * timbre dB (0 = hard knee, 1 = -6 dB soft knee).

    volmax_boost is applied as `output-gain`; see `make_preset` for how
    that interacts with the rest of the chain.
    """
    if not regulator:
        return None

    th = regulator["threshold_high"]
    slope = regulator.get("distortion_slope", 1.0)
    timbre = regulator.get("timbre_preservation", 0.75)

    # Derive ratio from distortion slope:
    # slope=1.0 → hard limiter (use 100:1 as practical maximum)
    # slope=0.5 → ratio=2:1 (moderate compression)
    if slope >= 1.0:
        ratio = 100.0
    elif slope <= 0.0:
        ratio = 1.0  # bypass
    else:
        ratio = 1.0 / (1.0 - slope)

    # Derive knee from timbre preservation:
    # timbre=0 → hard knee (0 dB), timbre=1 → soft knee (-6 dB)
    knee = -6.0 * timbre

    # Group the 20 bands into zones with distinct thresholds.
    # Find runs of identical threshold_high values.
    zones = []  # list of (start_idx, end_idx, threshold)
    i = 0
    while i < len(th):
        j = i + 1
        while j < len(th) and th[j] == th[i]:
            j += 1
        zones.append((i, j - 1, th[i]))
        i = j

    # Merge zones if we have more than 8 (EasyEffects limit)
    # In practice, Dolby regulators typically produce 2-5 zones
    while len(zones) > 8:
        # Merge the two adjacent zones with the smallest threshold difference
        min_diff = float("inf")
        min_idx = 0
        for k in range(len(zones) - 1):
            diff = abs(zones[k][2] - zones[k + 1][2])
            if diff < min_diff:
                min_diff = diff
                min_idx = k
        z1 = zones[min_idx]
        z2 = zones[min_idx + 1]
        merged_thresh = max(z1[2], z2[2])  # use the less aggressive threshold
        zones[min_idx] = (z1[0], z2[1], merged_thresh)
        del zones[min_idx + 1]

    # Build the multiband compressor (used as limiter: ratio=100:1, fast attack)
    result = {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": round(volmax_boost, 1),
        "dry": -80.01,
        "wet": 0.0,
        "compressor-mode": "Modern",
        "envelope-boost": "None",
        "stereo-split": False,
    }

    for i in range(8):
        bandn = f"band{i}"
        if i < len(zones):
            zone_start, zone_end, threshold = zones[i]
            # Crossover at the geometric mean between the last freq of this
            # zone and the first freq of the next zone
            if i > 0:
                prev_end = zones[i - 1][1]
                cross_freq = math.sqrt(freqs[prev_end] * freqs[zone_start])
            else:
                cross_freq = 10.0  # not used for band 0

            # Bands with threshold >= 0 dB never trigger; disable to save CPU
            is_active = threshold < 0
            band = {
                "compressor-enable": is_active,
                "mute": False,
                "solo": False,
                "attack-threshold": round(threshold, 1),
                "attack-time": 1.0,  # very fast for limiting
                "release-threshold": -80.01,
                "release-time": 50.0,
                "ratio": round(ratio, 1),
                "knee": round(knee, 1),
                "makeup": 0.0,
                "compression-mode": "Downward",
                "sidechain-type": "Internal",
                "sidechain-mode": "Peak",  # peak detection for limiting
                "sidechain-source": "Middle",
                "stereo-split-source": "Left/Right",
                "sidechain-lookahead": 1.0,  # 1 ms head start for transients
                "sidechain-reactivity": 10.0,
                "sidechain-preamp": 0.0,
                "sidechain-custom-lowcut-filter": False,
                "sidechain-custom-highcut-filter": False,
                "sidechain-lowcut-frequency": 10.0,
                "sidechain-highcut-frequency": 20000.0,
                "boost-threshold": -60.0,
                "boost-amount": 0.0,
            }
            if i > 0:
                band["enable-band"] = True
                band["split-frequency"] = round(cross_freq, 1)
            result[bandn] = band
        else:
            # Disabled band
            result[bandn] = {
                "enable-band": False,
                "compressor-enable": False,
                "mute": False,
                "solo": False,
                "attack-threshold": -12.0,
                "attack-time": 20.0,
                "release-threshold": -80.01,
                "release-time": 100.0,
                "ratio": 1.0,
                "knee": -6.0,
                "makeup": 0.0,
                "compression-mode": "Downward",
                "sidechain-type": "Internal",
                "sidechain-mode": "RMS",
                "sidechain-source": "Middle",
                "stereo-split-source": "Left/Right",
                "sidechain-lookahead": 0.0,
                "sidechain-reactivity": 10.0,
                "sidechain-preamp": 0.0,
                "sidechain-custom-lowcut-filter": False,
                "sidechain-custom-highcut-filter": False,
                "sidechain-lowcut-frequency": 10.0,
                "sidechain-highcut-frequency": 20000.0,
                "boost-threshold": -60.0,
                "boost-amount": 0.0,
            }

    return result


def make_bass_enhancer(hp_freq: float, amount: float = 12.0) -> dict:
    """Psychoacoustic bass enhancement via harmonic generation.

    Small laptop speakers cannot reproduce low frequencies physically.
    The bass enhancer generates upper harmonics of the bass content,
    which the brain perceives as bass (the "missing fundamental" effect).

    Scope is set to 2x the high-pass cutoff so harmonics are generated
    only for frequencies the speaker rolls off.
    """
    scope = min(hp_freq * 2.0, 300.0)
    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "amount": round(amount, 1),
        "harmonics": 10.0,
        "scope": round(scope, 1),
        "floor": 10.0,
        "blend": -10.0,
        "floor-active": True,
        "listen": False,
    }


def make_limiter(input_gain=0.0):
    """Brickwall output limiter to catch any remaining overshoot.

    Placed at the very end of the chain as a safety net. Uses the LSP
    limiter plugin with a -1 dB threshold and 1 ms lookahead for
    transparent true-peak limiting.

    input_gain is the fallback injection point for Dolby's volmax-boost
    when the regulator (multiband_compressor#1) is absent, so the
    static loudness boost still pushes peaks into the brick-wall and
    the resulting limiting acts as a crude loudness maximiser.
    """
    return {
        "bypass": False,
        "input-gain": round(input_gain, 1),
        "output-gain": 0.0,
        "mode": "Herm Thin",
        "oversampling": "None",
        "dithering": "None",
        "sidechain-type": "Internal",
        "lookahead": 1.0,
        "attack": 1.0,
        "release": 5.0,
        "threshold": -1.0,
        "gain-boost": False,
        "stereo-link": 100.0,
        "alr": False,
        "sidechain-preamp": 0.0,
    }


# Single source of truth for the --disable flag. Adding a new entry here
# automatically extends the argparse choices and the end-of-run hint
# block; each emission branch in `make_preset` is responsible for
# recording its name into the returned `emitted` set when it actually
# runs, so there is no separate plugin-key → name map to keep in sync.
DISABLEABLE_FILTERS = {
    "volmax": ("too loud, pumping/squash on loud content",
               "drops the +volmax-boost static loudness gain"),
    "mbc": ("compressed or \"squashed\" character",
            "drops the 2-band Dolby compressor"),
    "regulator": ("unusual spectral pumping or narrow-band breathing",
                  "drops the per-band limiter"),
    "bass-enhancer": ("bass sounds artificial/distorted (SoundWire only)",
                      "drops the harmonic bass generator"),
    "dialog": ("vocals over-boosted or harsh in the presence region",
               "drops the 2.5 kHz speech-band EQ"),
    "stereo": ("phasey or hollow stereo image",
               "drops the surround widener"),
}


def make_preset(kernel_name, peq_filters, vol_leveler=None,
                dialog_enhancer=None, surround=None, mb_comp=None,
                regulator=None, freqs=None, convolver_gain=0.0,
                is_soundwire=False, volmax_boost=0.0, disabled=None):
    """Build a preset dict.

    Returns (preset, emitted) where emitted is the set of
    DISABLEABLE_FILTERS names that actually ran in this invocation —
    i.e. those the user could meaningfully --disable on a rerun.
    Tracked inline with each emission branch so the set can't drift
    from what is in the returned dict.
    """
    disabled = disabled or set()
    emitted = set()
    preset = {
        "output": {
            "blocklist": [],
            "convolver#0": make_convolver(kernel_name, output_gain=convolver_gain),
            "plugins_order": ["convolver#0"],
        }
    }

    # SoundWire speakers lack Dolby's proprietary Virtual Bass Enhancement
    # (VBE) that runs in the Windows driver. Compensate with psychoacoustic
    # harmonic generation so small speakers still produce perceived bass.
    if is_soundwire and "bass-enhancer" not in disabled:
        hp_filters = [f for f in peq_filters if f["type"] in (7, 9)]
        hp_freq = hp_filters[0]["f0"] if hp_filters else 100.0
        preset["output"]["bass_enhancer#0"] = make_bass_enhancer(hp_freq)
        preset["output"]["plugins_order"].append("bass_enhancer#0")
        emitted.add("bass-enhancer")

    # Stereo widening early in chain (before EQ changes the spectrum)
    if "stereo" not in disabled:
        st = make_stereo_tools(surround)
        if st:
            preset["output"]["stereo_tools#0"] = st
            preset["output"]["plugins_order"].append("stereo_tools#0")
            emitted.add("stereo")

    peq = make_peq_eq(peq_filters)
    if peq:
        preset["output"]["equalizer#0"] = peq
        preset["output"]["plugins_order"].append("equalizer#0")

    # Dialog enhancer (speech presence boost) before the volume leveler,
    # matching Dolby's CP order: DE → IEQ → Volume Leveler.
    if "dialog" not in disabled:
        de = make_dialog_enhancer(dialog_enhancer, is_soundwire=is_soundwire)
        if de:
            preset["output"]["equalizer#1"] = de
            preset["output"]["plugins_order"].append("equalizer#1")
            emitted.add("dialog")

    # Autogain (volume leveler) goes before the compressor/regulator to match
    # Dolby's signal flow: CP (volume leveler) → VLLDP (compressor → regulator).
    # This lets the compressor and regulator catch any overshoot from the leveler.
    autogain = make_autogain(vol_leveler, conservative=is_soundwire)
    if autogain:
        preset["output"]["autogain#0"] = autogain
        preset["output"]["plugins_order"].append("autogain#0")

    if "mbc" not in disabled:
        mbc = make_multiband_compressor(mb_comp, freqs)
        if mbc:
            preset["output"]["multiband_compressor#0"] = mbc
            preset["output"]["plugins_order"].append("multiband_compressor#0")
            emitted.add("mbc")

    # volmax-boost injection: regulator output-gain is the primary slot
    # (matches Dolby VolMax topology). If the regulator is disabled or
    # absent from the XML, fall back to limiter#0 input-gain so the boost
    # still happens. Never both.
    apply_volmax = volmax_boost if "volmax" not in disabled else 0.0
    reg = None
    if "regulator" not in disabled:
        reg = make_regulator(regulator, freqs, volmax_boost=apply_volmax)
    if reg:
        preset["output"]["multiband_compressor#1"] = reg
        preset["output"]["plugins_order"].append("multiband_compressor#1")
        emitted.add("regulator")
        limiter_boost = 0.0
    else:
        limiter_boost = apply_volmax

    if apply_volmax > 0:
        emitted.add("volmax")

    # Brickwall limiter at the end as a safety net
    preset["output"]["limiter#0"] = make_limiter(input_gain=limiter_boost)
    preset["output"]["plugins_order"].append("limiter#0")

    return preset, emitted


def main():
    parser = argparse.ArgumentParser(
        description="Convert Dolby DAX3 tuning XML to EasyEffects output presets.",
    )
    parser.add_argument(
        "xml_file",
        nargs="?",
        type=Path,
        default=None,
        help="path to the Dolby DAX3 tuning XML (e.g. DEV_0287_SUBSYS_*.xml)",
    )
    parser.add_argument(
        "--windows",
        type=Path,
        default=None,
        metavar="DIR",
        help="path to a mounted Windows directory (e.g. /mnt/windows/Windows); "
             "auto-discovers the correct tuning XML by matching the audio "
             "codec subsystem ID from /proc/asound",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"EasyEffects output preset directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--irs-dir",
        type=Path,
        default=DEFAULT_IRS_DIR,
        help=f"EasyEffects impulse response directory (default: {DEFAULT_IRS_DIR})",
    )
    parser.add_argument(
        "--prefix",
        default="Dolby",
        help="prefix for preset names (default: Dolby → Dolby-Balanced, etc.)",
    )
    parser.add_argument(
        "--endpoint",
        default="internal_speaker",
        help="endpoint type from the XML (default: internal_speaker)",
    )
    parser.add_argument(
        "--mode",
        default="normal",
        help="endpoint operating mode (default: normal)",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="profile type, e.g. dynamic, music, voice (default: first profile)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list available endpoints and profiles, then exit",
    )
    parser.add_argument(
        "--all-profiles",
        action="store_true",
        help="generate presets for all profiles in the selected endpoint/mode "
             "(profile names are included in the preset names)",
    )
    parser.add_argument(
        "--autoload",
        nargs="?",
        const=True,
        metavar="PRESET",
        help="write EasyEffects autoload config for speaker outputs. "
             "Optionally specify the preset name to autoload; "
             "defaults to the first Balanced preset generated",
    )
    parser.add_argument(
        "--autoload-dir",
        type=Path,
        default=DEFAULT_AUTOLOAD_DIR,
        help=f"EasyEffects autoload directory (default: {DEFAULT_AUTOLOAD_DIR})",
    )
    parser.add_argument(
        "--speaker-info",
        action="store_true",
        help="report detected audio hardware and speaker layout, then exit",
    )
    parser.add_argument(
        "--disable",
        action="append",
        default=[],
        choices=list(DISABLEABLE_FILTERS),
        metavar="NAME",
        help="drop a filter from the generated preset (repeatable). "
             f"Valid names: {', '.join(DISABLEABLE_FILTERS)}. "
             "Try --disable volmax if output sounds too loud / saturated, or "
             "--disable mbc if you dislike the compressor character.",
    )
    args = parser.parse_args()
    disabled = set(args.disable)

    if args.speaker_info:
        report_speaker_info()
        return

    # Resolve the XML file path
    if args.xml_file and args.windows:
        parser.error("specify either xml_file or --windows, not both")
    elif args.windows:
        xml_path = find_tuning_xml(args.windows)
        print(f"Auto-detected: {xml_path}")
    elif args.xml_file:
        xml_path = args.xml_file
    else:
        parser.error("either xml_file or --windows is required")

    xml_basename = Path(xml_path).name.upper()
    is_soundwire = "SOUNDWIRE" in xml_basename or xml_basename.startswith("SDW_")

    if args.list:
        print(f"Endpoints and profiles in {xml_path}:")
        list_endpoints(xml_path)
        return

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.irs_dir.mkdir(parents=True, exist_ok=True)

    # Determine which profiles to process
    if args.all_profiles:
        profile_types = get_profile_types(xml_path, args.endpoint, args.mode)
        if not profile_types:
            print(f"No profiles found for endpoint={args.endpoint} mode={args.mode}")
            return
        print(f"Generating presets for all {len(profile_types)} profiles: {', '.join(profile_types)}")
    else:
        profile_types = [args.profile]  # None means "first profile"

    all_preset_names = []
    active_filters = set()

    for profile_type in profile_types:
        # Build name base: prefix[-Mode][-Profile]
        # When --all-profiles is used, always include the profile name.
        name_parts = [args.prefix]
        if args.mode != "normal":
            name_parts.append(args.mode.title())
        if profile_type or args.all_profiles:
            safe_profile = sanitize_profile_type(profile_type or "default")
            if profile_type and safe_profile != profile_type:
                print(f"Warning: sanitizing profile name {profile_type!r} -> {safe_profile!r} for use in filenames")
            name_parts.append(safe_profile.title())
        name_base = "-".join(name_parts)

        print(f"\n{'='*60}")
        if is_soundwire:
            print(f"SoundWire device detected — using enhanced preset generation")
        print(f"Endpoint: {args.endpoint} (mode={args.mode})")
        print(f"Profile: {profile_type or '(first)'}")

        freqs, curves, ieq_amount, ao_left, ao_right, peq_filters, vol_leveler, dialog_enhancer, surround, mb_comp, regulator, volmax_boost = parse_xml(
            xml_path,
            endpoint_type=args.endpoint,
            operating_mode=args.mode,
            profile_type=profile_type,
        )

        scale = ieq_amount / 10.0
        print(f"ieq-amount: {ieq_amount}/10 (scale: {scale:.2f})")

        # Audio-optimizer curves in dB
        ao_db_left = np.array(ao_left) / 16.0
        ao_db_right = np.array(ao_right) / 16.0
        float_freqs = np.array(freqs, dtype=float)

        print(f"\nAudio-optimizer (dB):")
        print(f"  Left:  {[f'{x:+.1f}' for x in ao_db_left]}")
        print(f"  Right: {[f'{x:+.1f}' for x in ao_db_right]}")

        print(f"\nPEQ filters (kept as parametric EQ):")
        for pf in peq_filters:
            spk = "L" if pf["speaker"] == 0 else "R"
            if pf["type"] in (7, 9):
                print(f"  [{spk}] HP @ {pf['f0']} Hz, order {pf['order']} ({pf['order'] * 6} dB/oct)")
            elif pf["type"] == 4:
                print(f"  [{spk}] Lo-shelf @ {pf['f0']} Hz, {pf['gain']:+.1f} dB, S={pf['s']}")
            elif pf["type"] == 1:
                print(f"  [{spk}] Bell @ {pf['f0']} Hz, {pf['gain']:+.1f} dB, Q={pf['q']}")

        if dialog_enhancer:
            gain = dialog_enhancer["amount"] / 16.0 * 6.0
            print(f"\nDialog enhancer: amount={dialog_enhancer['amount']}, "
                  f"mapped to +{gain:.1f} dB @ 2.5 kHz")

        if surround:
            base = min(surround["boost"] / 20.0, 0.5)
            print(f"\nSurround virtualizer: boost={surround['boost']:.1f} dB, "
                  f"mapped to stereo-base={base:.2f}")

        if vol_leveler:
            print(f"\nVolume leveler: {'enabled' if vol_leveler['enable'] else 'disabled'}")
            print(f"  amount: {vol_leveler['amount']}")
            print(f"  in-target: {vol_leveler['in_target']:.1f} dB")
            print(f"  out-target: {vol_leveler['out_target']:.1f} dB")

        if mb_comp:
            print(f"\nMulti-band compressor: {mb_comp['group_count']} bands")
            print(f"  target-power-level: {mb_comp['target_power']:.1f} dB")
            for i, bg in enumerate(mb_comp["band_groups"][:mb_comp["group_count"]]):
                xover_idx = bg[0]
                xover_hz = freqs[xover_idx] if 0 <= xover_idx < len(freqs) else "?"
                thresh = bg[1] / 16.0
                ratio_frac = bg[2] / 32768.0
                ratio = 1.0 / ratio_frac if ratio_frac > 0.01 else float('inf')
                attack = decode_mbc_time_constant(bg[3])
                release = decode_mbc_time_constant(bg[4])
                makeup = bg[5] / 16.0
                print(f"  band {i}: xover={xover_hz} Hz, thresh={thresh:+.1f} dB, "
                      f"ratio={ratio:.2f}:1, attack={attack:.1f} ms, "
                      f"release={release:.1f} ms, makeup={makeup:+.1f} dB")

        if regulator:
            print(f"\nRegulator (per-band limiter):")
            print(f"  threshold_high (dB): {[f'{x:+.1f}' for x in regulator['threshold_high']]}")
            print(f"  threshold_low (dB):  {[f'{x:+.1f}' for x in regulator['threshold_low']]}")
            print(f"  stress (dB):         {[f'{x:+.1f}' for x in regulator['stress']]}")
            print(f"  distortion-slope:    {regulator.get('distortion_slope', 1.0):.2f}")
            print(f"  timbre-preservation: {regulator.get('timbre_preservation', 0.75):.2f}")

        if volmax_boost <= 0:
            slot = "value is 0, no boost to apply"
        elif "volmax" in disabled:
            slot = "disabled via --disable volmax"
        elif regulator and "regulator" not in disabled:
            slot = "applied as regulator output-gain"
        else:
            slot = "applied as limiter input-gain"
        print(f"\nvolmax-boost: {volmax_boost:+.1f} dB ({slot})")
        print()

        ieq_presets = {
            f"{name_base}-Balanced": "ieq_balanced",
            f"{name_base}-Detailed": "ieq_detailed",
            f"{name_base}-Warm": "ieq_warm",
        }

        for preset_name, curve_key in ieq_presets.items():
            if curve_key not in curves:
                print(f"  Skipping {preset_name}: curve '{curve_key}' not found in XML")
                continue

            gains_raw = curves[curve_key]
            ieq_db = np.array(gains_raw) / 16.0 * scale

            # Combined target: IEQ + audio-optimizer (summed in dB)
            combined_left = ieq_db + ao_db_left
            combined_right = ieq_db + ao_db_right

            # Generate FIR impulse responses
            fir_left, peak_db_left = make_fir(float_freqs, combined_left, normalize=True)
            fir_right, peak_db_right = make_fir(float_freqs, combined_right, normalize=True)
            peak_db = max(peak_db_left, peak_db_right)

            # For SoundWire presets, restore half the headroom that peak
            # normalization removed. The IEQ-only curve (no AO correction)
            # peaks at low-mids; normalizing to that peak pushes presence
            # and treble below their intended level. Restoring 50% keeps
            # the spectral shape while recovering perceived brightness.
            convolver_gain = peak_db * 0.5 if is_soundwire else 0.0

            # Save stereo impulse response
            irs_path = args.irs_dir / f"{preset_name}.irs"
            save_wav_stereo(irs_path, fir_left, fir_right)

            # Create preset (kernel-name is the WAV filename stem)
            preset, emitted = make_preset(preset_name, peq_filters, vol_leveler,
                                          dialog_enhancer, surround, mb_comp, regulator,
                                          freqs, convolver_gain=convolver_gain,
                                          is_soundwire=is_soundwire,
                                          volmax_boost=volmax_boost,
                                          disabled=disabled)
            active_filters.update(emitted)
            out_path = args.output_dir / f"{preset_name}.json"
            out_path.write_text(json.dumps(preset, indent=4) + "\n")

            all_preset_names.append(preset_name)

            print(f"Wrote {irs_path}")
            print(f"Wrote {out_path}")
            if convolver_gain != 0.0:
                print(f"  Convolver output-gain: {convolver_gain:+.1f} dB "
                      f"(FIR peak was {peak_db:+.1f} dB, restoring 50%)")
            print(f"  {curve_key} combined IEQ+AO curve (left channel):")
            print(f"  {'freq':>8}  {'IEQ':>6}  {'AO':>6}  {'combined':>8}")
            for i, f in enumerate(freqs):
                print(f"  {f:>7} Hz  {ieq_db[i]:+5.1f}  {ao_db_left[i]:+5.1f}  {combined_left[i]:+7.1f}")

            # Verify FIR frequency response
            H = np.fft.rfft(fir_left, n=FIR_LENGTH)
            fft_freqs = np.fft.rfftfreq(FIR_LENGTH, d=1.0 / SAMPLE_RATE)
            mag_db = 20.0 * np.log10(np.abs(H) + 1e-12)
            print(f"\n  FIR verification (left, normalized to peak=0):")
            for i, f in enumerate(freqs):
                idx = np.argmin(np.abs(fft_freqs - f))
                print(f"  {f:>7} Hz  target: {combined_left[i] - np.max(combined_left):+6.1f}  "
                      f"actual: {mag_db[idx]:+6.1f}  "
                      f"error: {mag_db[idx] - (combined_left[i] - np.max(combined_left)):+5.2f}")
            print()

    # Autoload configuration
    if args.autoload and all_preset_names:
        autoload_preset = args.autoload if isinstance(args.autoload, str) else all_preset_names[0]
        sinks = find_speaker_sinks()
        if not sinks:
            print("Warning: no speaker sinks found via pw-dump; cannot configure autoload.")
            print("  Is PipeWire running? Try running the script while logged into your desktop session.")
        else:
            print(f"\nConfiguring autoload → '{autoload_preset}':")
            for sink in sinks:
                path = write_autoload(
                    args.autoload_dir,
                    sink["name"],
                    sink["description"],
                    sink["profile"],
                    autoload_preset,
                )
                print(f"  Wrote {path}")
                print(f"  Device: {sink['description']} ({sink['profile']})")

    # End-of-run troubleshooting hint. Only list filters that actually
    # got emitted this run — no point suggesting --disable for
    # something the user couldn't hear anyway.
    shown = [k for k in DISABLEABLE_FILTERS if k in active_filters]
    if shown:
        print(f"\n{'=' * 60}")
        print("If anything sounds off on your hardware, you can rebuild")
        print("without specific filters instead of editing the chain in")
        print("EasyEffects. Re-run adding one or more of:")
        print()
        for name in shown:
            symptom, effect = DISABLEABLE_FILTERS[name]
            print(f"  --disable {name:<14}  # if you hear: {symptom}")
            print(f"  {'':<24}    ({effect})")
        print()
        print("Flags are repeatable, e.g. --disable volmax --disable mbc.")

    print()
    print("How does it sound? Please report back (good or bad) at")
    print("  https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues")


if __name__ == "__main__":
    main()
