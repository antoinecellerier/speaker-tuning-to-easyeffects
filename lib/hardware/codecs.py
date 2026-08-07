#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
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
  - equalizer#0: speaker PEQ bells + high-pass (parametric filters from Dolby)
  - equalizer#1: dialog enhancer (speech presence boost from dialog-enhancer settings)
  - autogain#0: volume leveler (from volume-leveler settings)
  - multiband_compressor#0: dynamics processing (from mb-compressor-tuning)
  - multiband_compressor#1: per-band limiter (from regulator-tuning)
  - limiter#0: brickwall output limiter (safety net)
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import NamedTuple

from lib import console, doctor, ee_paths, version
from lib.data import kernel_releases
from lib.data import speaker_pin_quirks
from lib.dax import parse
# Aliased: main() binds a local named `findings`, which would shadow the
# module for the one line that resets _TAG_CONVENTION_SHOWN through it.
from lib.report import findings as report_findings

# Optional tab-completion (README "Shell tab-completion"). Absent argcomplete, the
# script behaves exactly as before — same contract as rich in lib/console.py.
try:
    import argcomplete
except ImportError:
    argcomplete = None


def _load_dsp() -> None:
    """Import the DSP stack into module globals.

    NumPy and SciPy are ~0.4 s of this script's ~0.5 s startup, and
    argcomplete re-runs the whole script on *every* TAB press, exiting inside
    autocomplete() long before any DSP code is reached. So the completion path
    skips them and complete_and_load() imports them once it knows this is a
    real run. The `from __future__ import annotations` above is what makes
    that legal: `np.ndarray` in a signature is a string, not a lookup.

    lib.preset.fir is bound here for the same reason and not at the top of
    this file: it imports numpy itself, so importing it eagerly would undo
    the deferral this function exists for.
    """
    global np, wavfile, fir
    import numpy as np
    from scipy.io import wavfile
    from lib.preset import fir


if "_ARGCOMPLETE" not in os.environ:
    _load_dsp()


# Shared with ee_to_pipewire.py, which resolves the same install root for its
# own --irs-dir default and must not import this module to do it (numpy/scipy
# in a converter that does no DSP). Kept under the private names the rest of
# this file already uses.
_FLATPAK_APP_ID = ee_paths.FLATPAK_APP_ID
_FLATPAK_BASE = ee_paths.FLATPAK_BASE
_NATIVE_BASE = ee_paths.NATIVE_BASE
_prefer_flatpak = ee_paths.prefer_flatpak

_USE_FLATPAK = _prefer_flatpak()
_EASYEFFECTS_BASE = ee_paths.easyeffects_base()

DEFAULT_OUTPUT_DIR = _EASYEFFECTS_BASE / "output"
DEFAULT_IRS_DIR = _EASYEFFECTS_BASE / "irs"
DEFAULT_AUTOLOAD_DIR = _EASYEFFECTS_BASE / "autoload" / "output"

# EasyEffects 8.x KConfig file. Separate from _EASYEFFECTS_BASE (which is
# under XDG_DATA_HOME for presets/IRs); this one is under XDG_CONFIG_HOME.
_FLATPAK_RC = Path.home() / ".var" / "app" / _FLATPAK_APP_ID / "config" / "easyeffects" / "db" / "easyeffectsrc"
_NATIVE_RC = Path.home() / ".config" / "easyeffects" / "db" / "easyeffectsrc"
DEFAULT_EASYEFFECTS_RC = _FLATPAK_RC if _USE_FLATPAK else _NATIVE_RC

BYPASS_PRESET_NAME = "Nothing"


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


def _card_pci_preference(card_name: str, proc_asound: Path) -> int:
    """Rank a sound card for the PCI-subsystem probe: 0 = has a non-HDMI HDA
    codec (the analog controller quirks and Dolby SKU ids key on), 1 = no HDA
    codec info (e.g. USB), 2 = HDMI/DP codecs only (a GPU audio function whose
    PCI subsystem is the GPU's, not the machine SKU's)."""
    names = []
    for codec_path in sorted((proc_asound / card_name).glob("codec*")):
        try:
            text = codec_path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("Codec:"):
                names.append(line.split(":", 1)[1].strip())
    if not names:
        return 1
    if all("HDMI" in n.upper() for n in names):
        return 2
    return 0


def get_pci_audio_subsystem(
    sound_class=Path("/sys/class/sound"),
    proc_asound=Path("/proc/asound"),
    sdw_bus=Path("/sys/bus/soundwire/devices"),
):
    """Get the PCI subsystem ID of the audio controller.

    Returns (subsys_vendor, subsys_device) as uppercase 4-char hex strings,
    e.g. ("17AA", "2339"), or None if not found.

    Prefers the PCI ancestor of a SoundWire device when present so we pick
    the controller that actually hosts the speaker amplifiers, rather than
    whichever /sys/class/sound card sorts first (which may be HDMI audio
    on a discrete GPU). Falls back to walking up from sound cards for
    traditional HDA systems — ranked so the analog codec's controller wins
    over a GPU HDMI function: on AMD dual-controller laptops card0 is the
    GPU audio function with its own PCI subsystem id (issue #33: 17AA:3823
    reported where the analog controller — the id kernel quirks and Dolby
    PCI-keyed filenames use — was a different device).
    """
    if sdw_bus.is_dir():
        for dev_dir in sorted(sdw_bus.iterdir()):
            result = _walk_to_pci_subsys(dev_dir)
            if result:
                return result

    if not sound_class.is_dir():
        return None
    cards = [c for c in sorted(sound_class.glob("card*")) if (c / "device").exists()]
    cards.sort(key=lambda c: _card_pci_preference(c.name, proc_asound))
    for card_dir in cards:
        result = _walk_to_pci_subsys(card_dir / "device")
        if result:
            return result
    return None


@dataclass
class SpeakerPin:
    """A single internal speaker output (HDA pin or SoundWire amplifier)."""
    node: str            # HDA node ID or SoundWire device name
    control_name: str    # ALSA control name or driver name
    role: str            # "woofer" or "tweeter"
    channels: int = 1    # audio channels this output carries. An HDA codec pin
                         # can drive a stereo (L+R) speaker = 2; SoundWire instead
                         # enumerates one slave device per amp chip, so each is a
                         # single addressable amp → probe it, default 1 (NOT 2 —
                         # that HDA-style default is what double-counted #27).
    codec: str = ""      # subsystem id of the codec exposing this pin, e.g.
                         # "17AA22E6". Empty on SoundWire. Pins must be counted
                         # per codec, not per machine: a report carries the HDMI
                         # codecs (0x00AA0100, 0x80860101) alongside the analog
                         # one, and only the analog codec's SSID keys a quirk.
    override: str = ""   # set when the kernel is driving this pin against the
                         # firmware's description of it — the label says which
                         # layer did so. Printed, because it is the *only*
                         # visible sign that a pin fixup took (issue #53): the
                         # firmware's own value stays in place underneath.


@dataclass
class UnconfiguredPin:
    """An HDA pin the codec can drive but the kernel left unconfigured.

    A pin complex that is output-capable (``Pincap`` lists ``OUT``) while the
    default config the driver acts on reports no physical connection — the
    firmware's own, unless a fixup overrode it, in which case the pin is a
    speaker and belongs above.
    Printed as raw evidence under "HDA internal speakers", never warned on: a
    genuinely unused pin and a speaker pin the BIOS wrongly calls unconnected
    look *identical* here (issue #53 — pin 0x17's dark woofers vs. pins
    0x1b/0x1e on the development machine, which are simply spare). Only a
    quirk-table match (``lib/data/speaker_pin_quirks.py``) tells them apart.

    Its value is that the pins are otherwise invisible: the speaker scan below
    keeps only ``[Fixed] Speaker at Int`` pins, so every report we have ever
    collected silently omits the one line that would show a missing woofer.
    """
    node: str            # HDA node ID, e.g. "0x17"
    codec: str           # subsystem id of the codec exposing it, e.g. "17AA22E6"
    pincap: str          # raw Pincap flags, e.g. "IN OUT EAPD Detect"
    pin_default: str     # raw default-config hex, e.g. "0x411111f0"


@dataclass
class FirmwareGate:
    """A smart-amp firmware-load ALSA control that gates the speakers.

    On some laptops whose woofers run through a TI TAS2563/2781 smart
    amplifier, the firmware does not auto-load and the amp stays muted until
    an ALSA control ("Speaker Force Firmware Load") is switched on (issue
    #17). This is a kernel/ALSA-side gate — nothing in the DAX XML hints at
    it — so the preset can be perfect while the bass speakers are silent.
    On other devices the firmware auto-loads fine and flipping the gate is
    an audible no-op (#39, ROG Xbox Ally X) — the warning stays because the
    toggle is cheap and harmless, but it says so.
    """
    card_index: str      # ALSA card index, e.g. "0"
    card_id: str         # ALSA card short id, e.g. "sofhdadsp" (stable across boots)
    numid: str           # control numid, e.g. "3"
    iface: str           # control iface, e.g. "CARD" (modern tas2781) or "MIXER"
    name: str            # control name, e.g. "Speaker Force Firmware Load"
    on: bool             # current state


def amixer_enable_cmd(gate: FirmwareGate) -> str:
    """The one-line command that switches a gate on, shared by every place
    that offers the fix (the end-of-run warning, --speaker-info, --doctor) so
    the three can't drift.

    iface= is load-bearing: these are iface=CARD controls on modern kernels,
    and a bare name= means iface=MIXER to amixer → "Cannot find the given
    element" (issue #39). Double-quoting the identifier lets the inner 'name'
    quotes reach amixer's parser — that form also survives control names
    containing commas.
    """
    return (f"amixer -c {gate.card_id} cset "
            f"\"iface={gate.iface},name='{gate.name}'\" on")


@dataclass
class AmpStatus:
    """Bind status of one SoundWire amplifier, for the merged amp-status report.

    SoundWire-specific: an amp enumerated on the bus but with no driver bound is
    the one clear-cut signal we surface (and even then neutrally — it could be a
    non-amp slave or a still-binding device). Channel count is probed from
    sysfs. Firmware presence and kernel-log lines are gathered separately and
    shown as raw evidence for a human to read — no sysfs/debugfs exposes amp
    audio-state (cs35l56 kernel doc), so we never render a health *verdict*.
    """
    node: str            # SoundWire device name (or mixer-control name on fallback)
    driver: str          # bound driver name, or "" when unbound
    bound: bool          # driver symlink present
    channels: int        # probed audio channels (0 = unknown)


@dataclass
class SpeakerInfo:
    """Collected audio hardware information for --speaker-info."""
    product: str = ""
    family: str = ""
    kernel: str = ""
    distro: str = ""
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
    # Output-capable HDA pins the kernel left unconfigured (evidence only)
    unconfigured_pins: list[UnconfiguredPin] = field(default_factory=list)
    # Smart-amp firmware-load gates (e.g. TAS2781 "Speaker Force Firmware Load")
    firmware_gates: list[FirmwareGate] = field(default_factory=list)
    # Merged amp-status evidence (see "=== Speaker amplifier status ===")
    amp_status: list[AmpStatus] = field(default_factory=list)
    amp_firmware: list[str] = field(default_factory=list)     # firmware files present
    amp_firmware_missing: bool = False  # a loaded driver needs a blob and none was found
    amp_log: list[tuple[bool, str]] = field(default_factory=list)  # (is_error, log line)
    amp_log_available: bool = True   # False when the kernel log was unreadable
    amp_log_grep: str = ""           # grep -iE alternation for the self-check hint

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
        total = sum(s.channels for s in self.speakers)
        by_role: dict[str, int] = {}
        for s in self.speakers:
            by_role[s.role] = by_role.get(s.role, 0) + s.channels
        if len(self.speakers) == 1 and self.speakers[0].channels == 2:
            return f"{total} speakers → full-range stereo"
        parts = " + ".join(f"{n}x {role}" for role, n in by_role.items())
        return f"{total} speakers → multi-way: {parts}"


def _read_sysfs_int(path: Path) -> int | None:
    """Largest integer in a sysfs attribute (handles single values and lists)."""
    try:
        nums = [int(n) for n in re.findall(r"\d+", path.read_text())]
    except (OSError, ValueError):  # ValueError covers non-UTF-8 sysfs bytes
        return None
    return max(nums) if nums else None


def _amp_channels_from_sysfs(dev_dir: Path) -> int | None:
    """Best-effort audio-channel count of a SoundWire amp from its sink ports.

    Reads ``<dev>/dpN_sink/max_ch`` — the path/attr are confirmed against the
    kernel ABI ``sysfs-bus-soundwire-slave``. Caveat: ``max_ch`` is the DisCo
    *maximum supported* channel count (a capability ceiling), not the provisioned
    count — there is no static sysfs attribute for the latter. For a dedicated
    mono amp the sink port should declare ``max_ch=1``, but a mono part that
    advertised a 2-channel-capable port would over-count; unverified without a
    real capture, which is why the layout line is an *estimate* and the caller
    falls back to 1 (one enumerated slave = one amp) when this returns None.
    """
    chans = []
    for sink in sorted(dev_dir.glob("dp*_sink")):
        c = _read_sysfs_int(sink / "max_ch")
        if c:
            chans.append(c)
    return max(chans) if chans else None


def _detect_soundwire_speakers(info: SpeakerInfo):
    """Detect speaker amplifiers on the SoundWire bus, with per-amp bind status.

    SoundWire enumerates one slave device per amp chip, so each amp counts once;
    its channel count is *probed* from the sink data-port DisCo props, else 1 —
    never the old stereo default that double-counted six mono cs35l56 as twelve
    (issue #27). Records per-amp bind status into ``info.amp_status`` (an
    enumerated-but-unbound device is surfaced neutrally — it may be a non-amp
    slave or one still binding).
    """
    sdw_path = Path("/sys/bus/soundwire/devices")
    if not sdw_path.is_dir():
        return

    amp_patterns = _AMP_DRIVER_TOKENS  # single source of amp-family identity

    for dev_dir in sorted(sdw_path.iterdir()):
        match = re.match(
            r"sdw:\d+:\d+:([0-9a-fA-F]{4}):([0-9a-fA-F]{4}):\d+",
            dev_dir.name,
        )
        if not match:
            continue
        driver_link = dev_dir / "driver"
        bound = driver_link.is_symlink()
        driver_name = driver_link.resolve().name if bound else ""
        lower_driver = driver_name.lower()

        if any(p in lower_driver for p in amp_patterns):
            # One slave = one mono amp; probe channels from sysfs, default 1.
            channels = _amp_channels_from_sysfs(dev_dir) or 1
            info.sdw_amplifiers.append(f"{dev_dir.name} (driver: {driver_name})")
            info.speakers.append(SpeakerPin(
                node=dev_dir.name,
                control_name=driver_name,
                role="amplifier",
                channels=channels,
            ))
            info.amp_status.append(AmpStatus(
                node=dev_dir.name, driver=driver_name, bound=True, channels=channels,
            ))
        elif not bound:
            # Enumerated SoundWire slave with no driver bound — surfaced
            # neutrally (could be a non-amp peripheral or one still binding).
            info.amp_status.append(AmpStatus(
                node=dev_dir.name, driver="", bound=False, channels=0,
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
                    node="mixer", control_name=name, role="amplifier", channels=1,
                ))
                info.amp_status.append(AmpStatus(
                    node=name, driver=name, bound=True, channels=1,
                ))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


class PinOverride(NamedTuple):
    """A pin default config the kernel uses in place of the firmware's."""
    cfg: int
    source: str          # what put it there, in the user's terms


# Where those overrides live, lowest priority first — the order
# ``snd_hda_codec_get_pincfg`` resolves them in (user beats driver beats the
# firmware's own value), so merging in this order reproduces what the driver
# acts on. Both files exist on every HDA codec; only ``user_pin_configs`` is
# gated behind CONFIG_SND_HDA_RECONFIG, hence the tolerant read.
_PIN_CFG_OVERRIDE_FILES = (
    ("driver_pin_configs", "kernel fixup"),   # a quirk the driver applied
    ("user_pin_configs", "manual pincfg"),    # hand-written via sysfs
)


def parse_pin_config_overrides(text: str) -> dict[str, int]:
    """``"0x17 0x90170121\\n"`` → ``{"0x17": 0x90170121}``.

    One ``nid cfg`` pair per line, as ``pin_configs_show`` writes them.
    """
    overrides = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                overrides[f"0x{int(parts[0], 16):02x}"] = int(parts[1], 16)
            except ValueError:
                continue
    return overrides


# Fields of the 32-bit pin default config. Named for what the HDA spec calls
# them, and each value confirmed against how the kernel renders real dumps:
# 0x90170110 prints as "[Fixed] Speaker at Int", 0x411111f0 as "[N/A] Speaker
# at Ext Rear", 0x03211020 as "[Jack] HP Out at Ext Left" — so connectivity
# 2 is Fixed, 1 is none, device 1 is Speaker, and location base 1 is internal.
_PIN_CONN_NONE = 1        # firmware says nothing is wired to this pin
_PIN_CONN_FIXED = 2       # a device is soldered to it
_PIN_DEVICE_SPEAKER = 1
_PIN_LOCATION_INTERNAL = 1


def _pin_is_internal_speaker(cfg: int) -> bool:
    return ((cfg >> 30) & 0x3 == _PIN_CONN_FIXED
            and (cfg >> 20) & 0xf == _PIN_DEVICE_SPEAKER
            and (cfg >> 28) & 0x3 == _PIN_LOCATION_INTERNAL)


def _pin_is_unconnected(cfg: int) -> bool:
    return (cfg >> 30) & 0x3 == _PIN_CONN_NONE


def parse_hda_codec_pins(
        codec_text: str,
        overrides: dict[str, PinOverride] | None = None,
) -> tuple[str, list[SpeakerPin], list[UnconfiguredPin]]:
    """Split one ``/proc/asound/card*/codec#*`` dump into its speaker pins and
    its output-capable-but-unconfigured pins, plus the codec's subsystem id.

    Pure text parsing so it can be unit-tested without hardware — same shape as
    ``parse_firmware_gate_controls()``.

    The two lists come from one pass because they partition the same pin
    complexes by their *effective* default config: a fixed internal speaker is
    a wired one, "no physical connection" is the lie a missing pin quirk tells
    (issue #53).

    Effective, not printed: ``/proc`` renders the config the **hardware**
    holds (``AC_VERB_GET_CONFIG_DEFAULT``, ``sound/hda/common/proc.c``), and a
    kernel pin fixup never writes that register — it stores an override in
    ``codec->driver_pins`` that only the driver-side lookup consults. Reading
    the printed line alone therefore shows the firmware's lie *even on a
    machine where the fix is live*, so a user who applied it would be told
    forever that they hadn't. ``overrides`` carries what the driver actually
    uses, and it wins here for the same reason it wins there.
    """
    ssid_match = re.search(r"^Subsystem Id: 0x([0-9a-fA-F]+)", codec_text,
                           flags=re.MULTILINE)
    codec_ssid = ssid_match.group(1).upper() if ssid_match else ""

    speakers: list[SpeakerPin] = []
    unconfigured: list[UnconfiguredPin] = []
    nodes = re.split(r"(?=^Node 0x[0-9a-fA-F]+ )", codec_text, flags=re.MULTILINE)
    for block in nodes:
        if "[Pin Complex]" not in block:
            continue
        node_match = re.match(r"Node (0x[0-9a-fA-F]+)", block)
        if not node_match:
            continue
        node = node_match.group(1)

        default = re.search(r"Pin Default (0x[0-9a-fA-F]+):", block)
        if not default:
            continue
        override = (overrides or {}).get(node)
        cfg = override.cfg if override else int(default.group(1), 16)

        if _pin_is_internal_speaker(cfg):
            ctrl_match = re.search(r'Control: name="([^"]+)"', block)
            ctrl_name = ctrl_match.group(1) if ctrl_match else "Speaker"
            lower = ctrl_name.lower()
            role = "woofer" if ("bass" in lower or "woofer" in lower) else "tweeter"
            speakers.append(SpeakerPin(
                node=node,
                control_name=ctrl_name,
                role=role,
                channels=2 if "Stereo" in block.split("\n", 1)[0] else 1,
                codec=codec_ssid,
                override=override.source if override else "",
            ))
            continue

        # Unconfigured: output-capable, but nothing the kernel uses reports a
        # connection. "OUT" is matched as a whole flag rather than a substring
        # so a future flag containing those letters can't smuggle a pin in.
        pincap = re.search(r"Pincap 0x[0-9a-fA-F]+: (.*)", block)
        if pincap and _pin_is_unconnected(cfg) and "OUT" in pincap.group(1).split():
            unconfigured.append(UnconfiguredPin(
                node=node,
                codec=codec_ssid,
                pincap=pincap.group(1).strip(),
                pin_default=f"0x{cfg:08x}",
            ))
    return codec_ssid, speakers, unconfigured


def read_pin_config_overrides(codec_path: Path,
                              sysfs_class_sound=Path("/sys/class/sound"),
                              ) -> dict[str, PinOverride]:
    """The pin configs the driver uses in place of the firmware's, for one codec.

    ``/proc/asound/card0/codec#0`` → ``/sys/class/sound/hwC0D0``: the card
    index and the codec address are what name both, so the two views can be
    lined up without parsing either. Missing files mean no override — a
    machine whose kernel applies nothing reads exactly like one with an empty
    list.
    """
    card = re.search(r"card(\d+)", codec_path.parent.name)
    addr = re.search(r"(\d+)$", codec_path.name)
    if not (card and addr):
        return {}
    codec_dir = sysfs_class_sound / f"hwC{card.group(1)}D{addr.group(1)}"
    resolved: dict[str, PinOverride] = {}
    for filename, source in _PIN_CFG_OVERRIDE_FILES:
        try:
            text = (codec_dir / filename).read_text()
        except OSError:
            continue
        resolved.update({node: PinOverride(cfg, source)
                         for node, cfg in parse_pin_config_overrides(text).items()})
    return resolved


def _maybe_demo_hidden_speaker_pin(info: SpeakerInfo) -> bool:
    """Stand in for a machine whose firmware hides a woofer pin.

    Same demo/preview convention as ``DEMO_FIRMWARE_GATE``, and needed for the
    same reason: this warning is keyed to the *machine*, not to anything in a
    tuning XML, so `tools/preview_output.py` can never find a corpus file that
    triggers it and the copy would go unread by every review round.
    ``DEMO_SPEAKER_PIN=17AA386A`` reproduces issue #53's Yoga 7 16IAH7 — pin
    0x14 configured, 0x17 called unconnected, 0x1b/0x1e genuinely spare.

    It substitutes the whole machine, not just its pins, which is why it sets
    the codec list and clears the SoundWire one: callers pick the detection
    branch off ``bus_type``, so a demo that filled in pins alone did nothing
    on any host that wasn't itself HDA — a SoundWire laptop, or CI, where
    there is no codec to make ``bus_type`` "hda" at all. Returns True when a
    demo was injected (skip real detection then).
    """
    ssid = (os.environ.get("DEMO_SPEAKER_PIN") or "").strip().upper()
    if not ssid:
        return False
    info.hda_codecs = [("10EC0287", ssid, "Realtek ALC287")]
    info.soundwire_devices = []
    info.speakers.append(SpeakerPin(node="0x14",
                                    control_name="Speaker Playback Switch",
                                    role="tweeter", channels=2, codec=ssid))
    for node, pincap in (("0x17", "OUT HP Detect"),
                         ("0x1b", "IN OUT EAPD Detect"), ("0x1e", "OUT")):
        info.unconfigured_pins.append(UnconfiguredPin(
            node=node, codec=ssid, pincap=pincap, pin_default="0x411111f0"))
    return True


def _detect_hda_speakers(info: SpeakerInfo,
                         proc_asound=Path("/proc/asound"),
                         sysfs_class_sound=Path("/sys/class/sound")):
    """Detect internal speakers from HDA codec pin configurations."""
    for codec_path in sorted(proc_asound.glob("card*/codec*")):
        try:
            text = codec_path.read_text()
        except OSError:
            continue
        _, speakers, unconfigured = parse_hda_codec_pins(
            text, read_pin_config_overrides(codec_path, sysfs_class_sound))
        info.speakers.extend(speakers)
        info.unconfigured_pins.extend(unconfigured)


# A smart-amp firmware-load gate is an ALSA control (not a DAX field) that
# must be on before TI TAS2563/2781 amplifiers will drive the woofers.
# Matched by name: "Speaker Force Firmware Load" is the one Lenovo laptops
# expose (issue #17); the pattern is loosened to the "...Force Firmware Load"
# family so sibling controls match too. Extend here if more turn up.
_FIRMWARE_GATE_NAME_RE = re.compile(r"force firmware load", re.I)
_AMIXER_CONTROL_HEAD_RE = re.compile(r"numid=(\d+),iface=(\w+),.*?name='([^']*)'")


def parse_firmware_gate_controls(
        amixer_contents: str) -> list[tuple[str, str, str, bool]]:
    """Extract firmware-load gate controls from ``amixer -c N contents`` text.

    Each control prints as a block:

        numid=3,iface=CARD,name='Speaker Force Firmware Load'
          ; type=BOOLEAN,access=rw------,values=1
          : values=off

    The iface must be captured, not assumed: modern tas2781 kernels expose
    these as iface=CARD, and ``amixer cset name=…`` without an iface assumes
    MIXER and fails with "Cannot find the given element" (issue #39, ROG
    Xbox Ally X) — the fix command has to spell the iface out.

    Returns ``(numid, iface, name, on)`` per name-matched control. Pure text
    parsing so it can be unit-tested without hardware.
    """
    gates: list[tuple[str, str, str, bool]] = []
    for block in re.split(r"(?=^numid=)", amixer_contents, flags=re.MULTILINE):
        head = _AMIXER_CONTROL_HEAD_RE.match(block)
        if not head:
            continue
        numid, iface, name = head.group(1), head.group(2), head.group(3)
        if not _FIRMWARE_GATE_NAME_RE.search(name):
            continue
        val = re.search(r":\s*values=(\w+)", block)
        on = val is not None and val.group(1).lower() in ("on", "1", "true")
        gates.append((numid, iface, name, on))
    return gates


def detect_speaker_firmware_gates() -> list[FirmwareGate]:
    """Scan ALSA cards for smart-amp firmware-load gate controls.

    Reads each card's raw control list via ``amixer -c <N> contents`` (the
    same tool the SoundWire fallback already shells out to) and returns a
    FirmwareGate per matching control. Empty when amixer is absent or no
    gate exists.
    """
    # Demo/preview hook (same ATMOS_* convention as the test corpus env vars):
    # inject a synthetic gate so the issue-#17 warning can be previewed on a
    # machine without a TI smart amp. The value is the gate *state*:
    # DEMO_FIRMWARE_GATE=off (or 0) shows the muted case a user would see
    # (the warning fires); =on (or 1) shows the already-enabled, silent case.
    demo = os.environ.get("DEMO_FIRMWARE_GATE")
    if demo:
        on = demo.strip().lower() in ("on", "1", "true")
        return [FirmwareGate(card_index="0", card_id="sofhdadsp", numid="3",
                             iface="CARD",
                             name="Speaker Force Firmware Load", on=on)]

    gates: list[FirmwareGate] = []
    for card_dir in sorted(Path("/proc/asound").glob("card*")):
        m = re.match(r"card(\d+)$", card_dir.name)
        if not m:
            continue  # skips the /proc/asound/cards file and oddly-named dirs
        idx = m.group(1)
        id_file = card_dir / "id"
        card_id = id_file.read_text().strip() if id_file.is_file() else idx
        try:
            result = subprocess.run(
                ["amixer", "-c", idx, "contents"],
                capture_output=True, text=True, timeout=5,
            )
        except FileNotFoundError:
            return gates  # amixer not installed — nothing more to scan
        except subprocess.TimeoutExpired:
            continue
        for numid, iface, name, on in parse_firmware_gate_controls(result.stdout):
            gates.append(FirmwareGate(
                card_index=idx, card_id=card_id, numid=numid, iface=iface,
                name=name, on=on,
            ))
    return gates


def warn_speaker_firmware_gate(gates: list[FirmwareGate]) -> Finding | None:
    """Warn — with copy-paste fixes — about any firmware-load gate that's off,
    and return the ask for whether toggling it worked.

    Silent when no gate is off (the gate is either absent or already enabled,
    so the speakers aren't muted on its account).

    The procedure below *is* this finding's detail, so the caller doesn't
    reprint it — only the returned one-line ask travels to the closing block.
    """
    off = [g for g in gates if not g.on]
    if not off:
        return None
    g0 = off[0]  # representative gate for the verify examples

    console.cprint("warn", f"\n{'=' * 60}")
    console.cprint("warn", "⚠  [firmware-gate] Smart-amp firmware gate is OFF — your speakers")
    console.cprint("warn", "   may be silent, thin or crackly even though the preset is correct.")
    console.cprint("dim", "Many devices drive their speakers through a TI TAS2563/2781 smart")
    console.cprint("dim", "amplifier whose firmware does not auto-load; until this ALSA control")
    console.cprint("dim", "is switched on the amp runs untuned upstream of the preset. On most")
    console.cprint("dim", "devices that mutes the woofers; where the amp drives every speaker,")
    console.cprint("dim", "it can instead make everything thin, quiet or prone to dropouts.")
    print()
    # Enable now: no root needed — the active logind session already holds an
    # ACL on /dev/snd/control*. Persist with `alsactl store`, which saves the
    # state that alsa-restore.service replays at boot (the standard ALSA path).
    console.cprint("dim", "1. Enable it now (no root needed), then listen for a change:")
    for g in off:
        console.cprint("cta", f"     {amixer_enable_cmd(g)}")
    print()
    # The card name is this machine's own (read from /proc/asound/cardN/id),
    # so the command is right as printed — but a card that renumbers between
    # boots, or a copy-paste into a later session, lands on "cannot find
    # card", and until now the only recovery text covered the command working
    # and changing nothing.
    console.cprint("dim", "   Errors with \"cannot find card\"? The card was renamed or")
    console.cprint("dim", "   renumbered since this run — list them with:  aplay -l")
    console.cprint("dim", f"   and use the name it shows in place of {g0.card_id}.")
    print()
    console.cprint("dim", "   No change at all, and nothing sounded wrong to begin with? Then")
    console.cprint("dim", "   this gate wasn't your problem — skip the rest of this section.")
    print()
    console.cprint("dim", "2. If that worked, persist it across reboots — saves the ALSA state")
    console.cprint("dim", "   that alsa-restore replays at boot:")
    console.cprint("cta", "     sudo alsactl store")
    print()
    # No systemd-unit fallback here any more. It named one ("fall back to a
    # systemd --user oneshot that runs the amixer command above at login")
    # without the unit, the path or the enable command — a reviewer rated it
    # unusable for the same reason as the old firmware-extraction line: a fix
    # you can name but not run. Writing the unit out is four more lines for a
    # case we have never seen reported, so the ask goes back to us instead.
    console.cprint("dim", "   (Doesn't survive a reboot? alsa-restore can race the driver on")
    console.cprint("dim", "   some setups — tell us and we'll work out the fix with you.)")
    print()
    console.cprint("dim", "3. Self-check — confirm the control stuck and the firmware loaded:")
    console.cprint("cta", f"     amixer -c {g0.card_id} cget "
                  f"\"iface={g0.iface},name='{g0.name}'\"")
    # No ".bin" suffix in the glob: distros may ship the blobs compressed
    # (TAS2XXX….bin.zst on SteamOS — the kernel decompresses transparently)
    # and the narrower pattern would report them "missing" (#39).
    console.cprint("cta", "     journalctl -k -b | grep -iE 'tas2|firmware'")
    console.cprint("cta", "     ls -l /lib/firmware/TAS2*")
    console.cprint("dim", "   (no journal access? try:  sudo dmesg | grep -i tas2)")
    print()
    console.cprint("dim", "   Still wrong, and the log shows 'Direct firmware load for")
    console.cprint("dim", "   TAS2XXX….bin failed' or no such file exists? Your distro's")
    console.cprint("dim", "   linux-firmware is missing this machine's blob, and no preset")
    console.cprint("dim", "   makes up for it. Update linux-firmware; if the file still")
    console.cprint("dim", "   doesn't turn up, report it with that log line — it names the")
    console.cprint("dim", "   exact file the kernel wants.")
    # No extraction pointer here, deliberately. The old wording ("extract it
    # from your Windows driver or TI's TAS2781-LINUX package and drop it into
    # /lib/firmware") read as a step and stopped a reviewer dead: no tool, no
    # method, and nothing marking it as specialist work. It meant to record
    # that a reporter had managed it — but that case is Cirrus (#27), whose
    # file layout and naming don't transfer to TI, and TI's own package is
    # driver source and a calibration tool, not a source of per-machine
    # blobs. A hint that fits neither the reader's amp nor their skill level
    # costs more attention than it returns.
    # The feedback ask (it gates whether we automate this) used to be two dim
    # lines here, deliberately whispered so it wouldn't rival the closing call
    # to action. It travels to that block instead now, where it can be a
    # normal ask without competing with anything.
    return _firmware_gate_finding()


# --- A woofer pin the BIOS hides (issue #53) --------------------------------
#
# Some laptops report the pin complex driving their woofers as unconnected, so
# the kernel configures only the tweeter pin and the preset drives half the
# speaker set. The DAX XML cannot see this — its internal_speaker endpoints
# describe *channels* (always "2"), never drivers, on 2- and 4-driver machines
# alike — so the only signal is that upstream Linux carries a per-machine fixup
# for this exact subsystem id while the running kernel isn't applying it.
#
# Detection is table-driven rather than inferred from the pins themselves: an
# output-capable pin with no default configuration is indistinguishable from a
# genuinely spare one (the development machine has two, 0x1b and 0x1e), so only
# a machine-specific match can tell a hidden woofer from an unused pin.

_MODPROBE_CONF = "/etc/modprobe.d/speaker-pin-fix.conf"


# A /proc/asound/cards entry: " 0 [sofhdadsp      ]: sof-hda-dsp - sof-hda-dsp".
# The driver field is what identifies the stack; matching "sof" anywhere in the
# line instead catches the *shortname*, and "microsoft" contains "sof" — a
# plugged-in Microsoft webcam or headset would otherwise read as a SOF machine.
_CARD_DRIVER_RE = re.compile(r"^\s*\d+\s*\[[^\]]*\]:\s*(\S+)")


def _card_uses_sof(sound_cards: list[str]) -> bool:
    """Whether any sound card is driven by the SOF stack.

    Load-bearing twice over: SOF zeroes the PCI subsystem id the HDA layer
    sees (so PCI-keyed quirks can't match), and it owns the ``hda_model``
    parameter that forces one.
    """
    for line in sound_cards:
        m = _CARD_DRIVER_RE.match(line)
        if m and m.group(1).lower().startswith("sof"):
            return True
    return False


def hda_model_module(uses_sof: bool,
                     module_root=Path("/sys/module")) -> tuple[str, str]:
    """``(module, parameter)`` that force an HDA fixup on this system.

    Which driver owns the codec decides this, not which module merely exposes
    a parameter: on an Intel machine the SOF modules are routinely loaded
    beside ``snd_hda_intel`` (both are present on the development machine), so
    picking the first ``hda_model`` found writes the option to a module that
    isn't driving anything — the user reboots and nothing changes.

    The SOF parameter itself is still found by scanning, because it moved:
    ``snd_sof_intel_hda_generic`` today, ``snd_sof_intel_hda_common`` before
    the generic split.
    """
    if uses_sof:
        for params in sorted(module_root.glob("*/parameters/hda_model")):
            return params.parent.parent.name, "hda_model"
    return "snd_hda_intel", "model"


def _ssid_key(ssid: str) -> tuple[int, int] | None:
    """``("17AA22E6")`` → ``(0x17AA, 0x22E6)``; None if not an 8-hex-digit id."""
    if not re.fullmatch(r"[0-9A-Fa-f]{8}", ssid or ""):
        return None
    return int(ssid[:4], 16), int(ssid[4:], 16)


def find_hidden_speaker_pin(
        info: SpeakerInfo) -> tuple[speaker_pin_quirks.PinQuirk, str] | None:
    """The pin fixup this machine should be getting but isn't, else None.

    Mirrors ``snd_hda_pick_fixup`` (``sound/hda/common/auto_parser.c``) so we
    only claim a match the kernel could actually make:

    * every entry can match the *codec's* subsystem id — either because it is
      an ``HDA_CODEC_QUIRK`` or via the codec-SSID fallback the lookup ends on;
    * a PCI-keyed entry can also match the PCI subsystem id, but not on SOF,
      where the id the kernel sees is zeroed. Our own PCI id is read from
      sysfs and is *not* zeroed, so trusting it there would claim a match the
      kernel never makes.

    Fires when a pin the fixup declares as an internal speaker is not one, on
    the codec the entry matches. Targeting the *named node* rather than
    counting pins is what makes this safe across the whole fixup family: some
    of these declare a machine's only speaker pin (``ALC289_FIXUP_DELL_SPK1``),
    where "fewer than two pins" would keep firing after the user fixed it,
    forever. Node-targeting also ignores the ALSA control name, which varies
    ("Bass Speaker" on one machine, "Speaker Front" on another) while the
    fixup's effect does not.

    Pins are matched per codec, and the fixup's pins must be *findable* on the
    codec that matched — already configured, or sitting there unconfigured.
    That last test is what keeps a machine's other codecs out of it: an HDMI
    codec has no pin 0x14/0x17 to be short of, so a machine-wide PCI id can't
    make it look like the analog one.

    Returns ``(quirk, codec subsystem id, pins actually missing)``. The missing
    list is what the messages name — reporting every pin the fixup declares
    would tell a user their working pin is broken too.
    """
    if info.bus_type != "hda":
        return None
    uses_sof = _card_uses_sof(info.sound_cards)

    # Seed from the *unconfigured* output pins too, not just the speakers: some
    # of these fixups declare a machine's only speaker pin (HP Spectre x360,
    # ASUS ROG), and before the fix such a codec has no speaker pins at all.
    # Keying on speakers alone would skip exactly the machines that need this.
    configured: dict[str, set[str]] = {}
    spare: dict[str, set[str]] = {}
    for pin in info.unconfigured_pins:
        spare.setdefault(pin.codec, set()).add(pin.node.lower())
        configured.setdefault(pin.codec, set())
    for pin in info.speakers:
        configured.setdefault(pin.codec, set()).add(pin.node.lower())

    for codec_ssid, nodes in sorted(configured.items()):
        key = _ssid_key(codec_ssid)
        quirk = speaker_pin_quirks._SPEAKER_PIN_QUIRKS.get(key) if key else None
        if quirk is None and nodes and not uses_sof and info.pci_subsystem:
            # The PCI id belongs to the machine, not to any one codec, so it
            # may only stand in for a codec that already owns speaker pins —
            # otherwise it lends the analog machine's identity to whichever
            # other codec happens to have a spare output pin.
            pci_key = _ssid_key("".join(info.pci_subsystem))
            candidate = (speaker_pin_quirks._SPEAKER_PIN_QUIRKS.get(pci_key)
                         if pci_key else None)
            if candidate and not candidate.codec_only:
                quirk = candidate
        if not quirk:
            continue
        declared = [n.lower() for n in quirk.pins.split()]
        # Every declared pin has to exist on this codec, as a speaker already
        # or as an unconfigured output pin. A declared pin that is neither is
        # some other codec's business.
        if not all(n in nodes or n in spare.get(codec_ssid, set())
                   for n in declared):
            continue
        missing = [n for n in declared if n not in nodes]
        if missing:
            return quirk, codec_ssid, missing
    return None


def upgrade_prospect(quirk: speaker_pin_quirks.PinQuirk,
                     release: str | None = None) -> str:
    """Whether upgrading the kernel would fix this, in the user's terms.

    Three genuinely different situations, and telling the wrong one wastes a
    reader's evening: no release carries the fix yet; a release does and they
    are behind it; or they are already past it, in which case the fix is
    reaching them and something else on the machine is stopping it — so
    "upgrade" would be advice to go and get what they already have.

    Shared by the end-of-run block and --doctor so the two can't drift.
    """
    import platform

    # Where the reader is left depends on whether a hand-forcible name exists:
    # with one, each branch hands off to the procedure that follows; without,
    # the branch has to be a complete answer on its own.
    tail = (" To apply it on the kernel you have now:" if quirk.model else
            " This fixup has no name the driver accepts, so it can't be forced "
            "by hand — a kernel that carries it is the only route.")
    if not quirk.since:
        return ("The fix is merged upstream but is not in any released kernel "
                "yet, so upgrading won't help today." + tail)
    running = parse_kernel_series(release or platform.release())
    fixed_in = parse_kernel_series(quirk.since)
    if running and fixed_in and running >= fixed_in:
        return (f"Linux {quirk.since} carries this fix and you are on "
                f"{running[0]}.{running[1]}, so it should already be applying "
                "— something on this machine is stopping it (a vendor kernel "
                "that dropped the fix, or a different model id than upstream "
                "expects)." + tail)
    return (f"Linux {quirk.since} and newer apply this automatically, so a "
            "kernel upgrade is the durable fix." + tail)


def _pin_phrase(missing: list[str]) -> str:
    """"pin 0x17" / "pins 0x14 and 0x17" — the copy has to work for both.

    A fixup declares one pin or two, and on a machine with none configured
    both of them are missing, so no message here may assume a count.
    """
    if len(missing) == 1:
        return f"pin {missing[0]}"
    return "pins " + " and ".join(missing)


def speaker_pin_fix_steps(quirk: speaker_pin_quirks.PinQuirk,
                          missing: list[str], uses_sof: bool,
                          width: int,
                          speaker_info_below: bool = False,
                          ) -> tuple[tuple[str, str], ...]:
    """Apply → confirm → undo, as ``(style, text)`` lines.

    Shared by the end-of-run warning and ``--doctor``'s check the way
    ``amixer_enable_cmd`` is shared, so the procedure can't drift between the
    two surfaces — and empty where the fixup has no forcible name, since then
    there is no procedure, only the upgrade route ``upgrade_prospect`` states.

    Prose wraps to *width*; commands never do. A command wider than the
    terminal is soft-wrapped by the terminal and still pastes as one line,
    where a folded one would not run at all — so the caller passes the width
    its own surface uses and no line here is broken by hand.

    ``speaker_info_below`` is the one thing that differs between the two
    callers: a --doctor run prints the hardware section itself, so sending
    that reader off to --speaker-info for a section already on their screen
    reads as a third command to type. The commands stay identical either way.
    """
    if not quirk.model:
        return ()
    module, param = hda_model_module(uses_sof)

    def prose(text: str, indent: str = "", hang: str = "") -> list[tuple[str, str]]:
        return [("dim", line) for line in textwrap.wrap(
            text, width, initial_indent=indent,
            subsequent_indent=hang or indent, break_on_hyphens=False)]

    # Where to look afterwards — the one sentence that differs by surface.
    verify = (f'look at the "HDA internal speakers" section below: '
              f"{_pin_phrase(missing)} should be listed there, tagged "
              "[kernel fixup]."
              if speaker_info_below else
              f"re-run with --speaker-info: {_pin_phrase(missing)} should be "
              'listed under "HDA internal speakers", tagged [kernel fixup].')
    return tuple([
        *prose("1. Write the option, then reboot:"),
        ("cta", f"     echo 'options {module} {param}={quirk.model}' \\"),
        ("cta", f"       | sudo tee {_MODPROBE_CONF}"),
        # The fixup's name is the kernel's, and several of them carry a model
        # that isn't the one running: this row is keyed to a codec id, and its
        # upstream entry reads "Yoga 7 16IAP7" while the name says yoga9. A
        # reader who spots that in a line they are about to sudo stops there.
        *prose("   That name is the kernel's label for the fix, not your "
               "model — it is matched to your machine by hardware id.",
               hang="   "),
        ("", ""),
        # Two independent confirmations, and the audible one leads because it
        # is the one the user cares about. Hedged the way the warning above is:
        # the pin usually drives woofers, but several fixups in the table
        # declare a machine's only speaker pin, where nothing was playing.
        *prose("2. After rebooting you should hear it — usually the bass "
               "coming back, or sound from speakers that were silent. To "
               f"check the kernel side, {verify}", hang="   "),
        ("", ""),
        *prose("Still missing, or the speakers went quiet? Undo it:",
               indent="   "),
        ("cta", f"     sudo rm {_MODPROBE_CONF}"),
        *prose("and reboot again. Nothing else on the system is touched.",
               indent="   "),
    ])


def warn_hidden_speaker_pin(
        found: tuple[speaker_pin_quirks.PinQuirk, str, list[str]] | None,
        info: SpeakerInfo) -> Finding | None:
    """Warn — with a copy-paste fix and its undo — that the kernel is leaving
    one of this machine's speakers unconfigured.

    Silent when nothing matched, which is the overwhelming majority of
    machines: the table lists the models upstream has had to fix, and a
    2-driver device showing one pin is simply correct.

    The procedure below *is* this finding's detail, so the caller doesn't
    reprint it — only the returned one-line ask travels to the closing block.
    """
    if not found:
        return None
    quirk, codec_ssid, missing = found
    phrase = _pin_phrase(missing)

    # "the preset shapes the rest alone" only holds if a speaker pin survived:
    # where the fixup declares the machine's only one, there is no rest.
    others = any(p.codec == codec_ssid for p in info.speakers)
    console.cprint("warn", f"\n{'=' * 60}")
    console.cprint("warn", "⚠  [speaker-pin] Linux isn't driving all of your speakers.")
    console._cprint_wrapped("dim",
        f"Upstream Linux carries a fix for this exact model that declares "
        f"{phrase} on codec {codec_ssid} an internal speaker, and your kernel "
        "isn't applying it. Your machine's firmware describes it as "
        "unconnected and the kernel takes it at its word, so whatever it "
        "drives — often the woofers — gets no signal"
        + (", and the preset shapes the rest alone." if others else "."))
    print()
    console._cprint_wrapped("dim", upgrade_prospect(quirk))
    steps = speaker_pin_fix_steps(quirk, missing,
                                  _card_uses_sof(info.sound_cards),
                                  console._wrap_width())
    if steps:
        print()
    for style, text in steps:
        if text:
            console.cprint(style, text)
        else:
            print()
    print()
    return _hidden_pin_finding(quirk, missing)


def _hidden_pin_finding(quirk: speaker_pin_quirks.PinQuirk,
                        missing: list[str]) -> Finding:
    """Whether forcing the missing pin actually restored the bass.

    Carries an ask only when the run printed a procedure to ask about. Where
    the fixup has no forcible name there is nothing the reader can do on this
    run, and `.claude/rules/user-messages.md` is explicit that such a finding
    takes no ask — its detail still travels, so a pasted report still shows it.
    """
    phrase = _pin_phrase(missing)
    if not quirk.model:
        return Finding(
            slug="speaker-pin", kind="hint",
            detail=f"{phrase[0].upper() + phrase[1:]} is declared an internal "
                   "speaker by a kernel fix this machine isn't getting, and "
                   "the driver accepts no name that would force it — see above.")
    return Finding(
        slug="speaker-pin", kind="hint",
        detail=f"{phrase[0].upper() + phrase[1:]} is left unconfigured, so "
               "those speakers get no signal — see the procedure above.",
        ask="Did forcing the missing speaker pin bring your bass back? "
            "(issue #53)")


def unlisted_speaker_pin_finding(info: SpeakerInfo) -> Finding | None:
    """Ask for the *negative* signal: one speaker pin, spare output pins, and
    no upstream fixup for this machine.

    The table above only knows machines upstream Linux has already been told
    about. A laptop whose woofers are hidden and whose subsystem id nobody has
    reported yet looks exactly like a genuine 2-driver laptop from here — and
    both are common. The manufacturer's spec sheet settles it in seconds, but
    only the owner can look it up, so the ask goes to them.

    Deliberately narrow. Spare output-capable pins are ordinary (the
    development machine has two), so this stays quiet unless the machine is
    also down to a single speaker pin — the one shape where a driver could
    plausibly be missing. Silent when a quirk already matched: that run has a
    real fix to offer and doesn't need a question competing with it.
    """
    if info.bus_type != "hda" or not info.unconfigured_pins:
        return None
    if len(info.speakers) != 1 or find_hidden_speaker_pin(info):
        return None
    spare = ", ".join(p.node for p in info.unconfigured_pins)
    # Names the pins rather than pointing at a table: the only place this
    # detail prints during a normal run is here, with no --speaker-info
    # output above it to refer back to.
    return Finding(
        slug="speaker-count", kind="ask",
        detail=f"Linux configured one internal speaker pin on this machine "
               f"({info.speakers[0].node}), and left {spare} unused though "
               "they can drive output. That is normal on a device with a "
               "single stereo pair — most machines have spare pins. It is "
               "only wrong if your device really has more speakers than that: "
               "then a kernel fix is missing for your exact model. Tell us "
               "and we can suggest a setting to test — the fix itself has to "
               "land in Linux, which is outside this project.",
        ask="Does your device have more speakers than the single pair Linux "
            "found? (issue #53)")


# --- Smart-amp status: bus-agnostic evidence (issue #27) --------------------
#
# Whether the speaker amps are actually live is a smart-amp question, not a
# SoundWire one: HDA-attached Cirrus (cs35l41) and TI (TAS2781) amps load DSP
# firmware too, and a cs35l56 with no firmware still plays — but as a quiet
# "mono mix" with no voicing/protection (cs35l56 kernel doc). No sysfs/debugfs
# exposes amp audio-state, so the authoritative signal is the kernel log. We
# gather *evidence* (an enumerated-but-unbound amp is the one hard verdict;
# firmware files + log markers are shown for a human) keyed by driver, so the
# engine is generic and adding a device is one registry row.

# One row per smart-amp family: (driver/module name tokens, firmware globs,
# kernel-log keywords, kernel-log failure markers). Empty globs = the family
# ships no DSP blob. Tokens are matched as substrings of a driver/module name
# and double as the SoundWire amp-detection patterns, so adding a device is
# genuinely one row. The max98 tokens are the specific *smart*-amp parts, not a
# bare "max98" — that would also catch the max98090 jack codec and the dumb
# max98357/360 I2S amps (same reason "rt13" is narrow enough to skip rt711).
#
# The failure markers are firmware/tuning/DSP-bring-up error strings verified
# verbatim against the mainline driver source (file:line cited per family) — NOT
# the kernel doc, whose ".bin file required but not found" is prose the driver
# never prints. They classify which collected log lines are failures; "" = no
# honest tell. Absence of a marker is never a pass: a real cs35l56 report (#27)
# printed FIRMWARE_MISSING / "Calibration disabled…" / "Can't read tuning IDs"
# while our first marker set (boot/init timeouts only) reported "no errors",
# which is exactly why the no-error line tells the reader to eyeball the log.
_AMP_FAMILIES = (
    # Cirrus cs35l41 (HDA) / cs35l56 / cs35l57 (SoundWire). Markers:
    # cs35l56-shared.c "FIRMWARE_MISSING" (l.1388), "Can't read tuning IDs"
    # (l.1424), "Firmware boot timed out" (l.455); cs35l56.c "init_completion
    # timed out" (l.866/1373); cs-amp-lib.c "Calibration disabled due to missing
    # firmware controls" (l.140/172, shared lib — also fires for cs35l41).
    (("cs35l",), ("cirrus/cs35l*",), ("cs35l", "cirrus"),
     r"firmware_missing|can't read tuning ids"
     r"|calibration disabled due to missing firmware controls"
     r"|firmware boot timed out|init_completion timed out"),
    # TI smart amps (issue #17 family). Markers from tas2781-fmwlib.c /
    # tas2781-i2c.c: "FW download failed", "Failed to read firmware",
    # "Request firmware … failed", "Firmware is NULL", "Bin file error".
    (("tas2",), ("TAS2*", "ti/tas2*", "tas2*"), ("tas2",),
     r"fw download failed|failed to read firmware|request firmware .* failed"
     r"|firmware is null|bin file error"),
    # Realtek SoundWire amps — only rt1320 loads a firmware patch (rt1316/rt1318
    # are register-only). Markers from rt1320-sdw.c: "Failed to load … firmware",
    # "FW file doesn't match to device", "Can't find proper FW file name".
    (("rt13", "rt_amp"), (), ("rt13",),
     r"failed to load .* firmware|fw file doesn't match to device"
     r"|can't find proper fw file name"),
    # Maxim DSM smart amps — no honest firmware-missing tell: only max98390 loads
    # a DSM calibration param, and a missing file falls through silently
    # (max98390.c err path), so we collect its log lines but flag nothing.
    (("max98373", "max98390", "max98363", "max98396", "max98512"),
     (), ("max98",), ""),
)

_AMP_DRIVER_TOKENS = tuple(tok for fam in _AMP_FAMILIES for tok in fam[0])


def _amp_firmware_profile(driver: str) -> tuple[list[str], list[str]] | None:
    """(firmware globs under /lib/firmware, kernel-log keywords) for a driver.

    Looks the driver up in ``_AMP_FAMILIES`` — the single source of amp-family
    identity. None ⇒ not a recognised smart amp.
    """
    d = driver.lower()
    for tokens, globs, keywords, _markers in _AMP_FAMILIES:
        if any(t in d for t in tokens):
            return (list(globs), list(keywords))
    return None


def _loaded_amp_drivers() -> list[str]:
    """Loaded kernel modules that look like smart-amp drivers (any bus)."""
    moddir = Path("/sys/module")
    if not moddir.is_dir():
        return []
    return sorted(m.name for m in moddir.iterdir()
                  if any(t in m.name.lower() for t in _AMP_DRIVER_TOKENS))


def _list_firmware_files(globs: list[str], roots=None) -> list[str]:
    """Existing firmware files matching globs under /lib/firmware (+ updates/)."""
    if roots is None:
        roots = (Path("/lib/firmware"), Path("/lib/firmware/updates"))
    found = set()
    for root in roots:
        for g in globs:
            for p in root.glob(g):
                if p.is_file():
                    found.add(str(p.relative_to(root)))
    return sorted(found)


def _read_kernel_log() -> str | None:
    """Current-boot kernel log via journalctl then dmesg; None if none readable.

    ``journalctl -o cat`` emits the message text only — no hostname or wall-clock
    timestamp — so the lines stay safe to paste into a device-report issue (same
    privacy posture as get_distro_pretty_name; dmesg carries no hostname either).
    ``errors="replace"`` keeps a stray non-UTF-8 byte from aborting the report,
    and the timeout is kept short since this runs on the default --doctor path.
    """
    for cmd in (["journalctl", "-k", "-b", "-o", "cat", "--no-pager"], ["dmesg"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               errors="replace", timeout=4)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    return None


# Union of every family's source-verified failure markers (co-located in
# _AMP_FAMILIES above). We do NOT try to classify "healthy": no log line
# reliably proves firmware loaded (`patched=N` is nuanced; success strings are
# vendor-specific), so a green verdict would mislead — and the marker list is
# deliberately not exhaustive, so the no-error path tells the reader to read the
# lines themselves. Every matched line is shown verbatim as evidence.
_AMP_LOG_ERROR_RE = re.compile(
    "|".join(markers for *_, markers in _AMP_FAMILIES if markers), re.I,
)


def _amp_log_is_error(line: str) -> bool:
    """True when a kernel-log line is an unambiguous amp firmware/init error."""
    return bool(_AMP_LOG_ERROR_RE.search(line))


def scan_amp_log(log_text: str, keywords: list[str]) -> list[tuple[bool, str]]:
    """(is_error, line) for kernel-log lines mentioning any amp keyword."""
    if not keywords:
        return []
    kw = re.compile("|".join(re.escape(k) for k in keywords), re.I)
    return [(_amp_log_is_error(line), line.strip())
            for line in log_text.splitlines() if kw.search(line)]


def _gather_amp_evidence(info: SpeakerInfo) -> None:
    """Populate driver-keyed firmware-presence and kernel-log evidence."""
    # Driver tokens from both loaded modules and bound SoundWire amps.
    drivers = _loaded_amp_drivers() + [a.driver for a in info.amp_status if a.driver]
    profiles = [p for p in (_amp_firmware_profile(d) for d in drivers) if p]
    if not profiles:
        return
    globs = sorted({g for pr in profiles for g in pr[0]})
    keywords = sorted({k for pr in profiles for k in pr[1]})
    # Self-check hint derived from the keywords we actually scan, so the printed
    # `grep` command can't contradict what the report found.
    info.amp_log_grep = "|".join(keywords)
    if globs:
        info.amp_firmware = _list_firmware_files(globs)
        # A blob-needing driver is loaded but none was found. Decided once here
        # (not at render) so the flag can't diverge from what we searched for.
        info.amp_firmware_missing = not info.amp_firmware
    log = _read_kernel_log()
    if log is None:
        info.amp_log_available = False
    else:
        info.amp_log = scan_amp_log(log, keywords)


def _maybe_demo_amp_status(info: SpeakerInfo) -> bool:
    """Inject synthetic amp status so the section can be previewed without hardware.

    ``ATMOS_DEMO_AMP_STATUS`` = ``ok`` (healthy 6× cs35l56) / ``unbound`` (one
    amp with no driver) / ``fail`` (bound but firmware missing + log error).
    Returns True when a demo was injected (skip real gathering then).
    """
    mode = (os.environ.get("ATMOS_DEMO_AMP_STATUS") or "").strip().lower()
    nodes = [f"sdw:0:1:01fa:3557:01:{i}" for i in range(6)]
    if mode == "unbound":
        info.amp_status = [AmpStatus(nodes[0], "", False, 0)]
        info.amp_status += [AmpStatus(n, "cs35l56", True, 1) for n in nodes[1:]]
        info.amp_firmware = ["cirrus/cs35l56-b0-dsp1-misc-aabbccdd-amp1.bin"]
        info.amp_log = [(False, "cs35l56 sdw:0:1: DSP1: cirrus/cs35l56-…wmfw")]
    elif mode == "fail":
        info.amp_status = [AmpStatus(n, "cs35l56", True, 1) for n in nodes]
        # Real #27 shape: generic cirrus blobs are present, but the *machine*
        # firmware is absent, so the driver logs FIRMWARE_MISSING even though
        # file-presence looks fine — the log marker, not the count, is the tell.
        info.amp_firmware = ["cirrus/cs35l56-b0-dsp1-misc-aabbccdd-amp1.bin"]
        info.amp_log = [
            (True, "cs35l56 sdw:0:1:01fa:3557:01:0: FIRMWARE_MISSING"),
            (True, "cs35l56 sdw:0:1:01fa:3557:01:0: Calibration disabled "
                   "due to missing firmware controls"),
        ]
    elif mode == "ok":
        info.amp_status = [AmpStatus(n, "cs35l56", True, 1) for n in nodes]
        info.amp_firmware = ["cirrus/cs35l56-b0-dsp1-misc-aabbccdd-amp1.bin"]
        info.amp_log = [(False, "cs35l56 sdw:0:1: DSP1: cirrus/cs35l56-…wmfw")]
    else:
        return False  # unset or unknown value → no demo (never fake a healthy report)
    return True


def get_distro_pretty_name(os_release=Path("/etc/os-release")) -> str:
    """Read PRETTY_NAME from /etc/os-release (e.g. "Fedora Linux 44"), or "".

    Only PRETTY_NAME — no hostname, machine-id, or serials. A missing or
    unreadable file, or an absent key, yields "" so the caller drops the line.
    """
    try:
        text = Path(os_release).read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("PRETTY_NAME="):
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return ""


def _gather_speaker_pins() -> SpeakerInfo:
    """Just enough of a SpeakerInfo to answer find_hidden_speaker_pin.

    A few /proc reads and one sysfs walk. Kept apart from
    ``_gather_speaker_info`` deliberately: that one also shells out to
    ``amixer`` per card and to ``journalctl``/``dmesg`` (seconds, on a machine
    with a big journal), globs /lib/firmware, and honours the demo-injection
    env vars — all of it for the amp-status report, which a default run never
    prints. A normal conversion must not pay for it.
    """
    import platform

    info = SpeakerInfo(kernel=platform.release())
    cards_path = Path("/proc/asound/cards")
    if cards_path.exists():
        info.sound_cards = [l.strip() for l
                            in cards_path.read_text().strip().splitlines()]
    info.hda_codecs = get_hda_codec_ids()
    info.soundwire_devices = get_soundwire_ids()   # decides bus_type
    info.pci_subsystem = get_pci_audio_subsystem()
    # Asked first, not folded into the condition below: injecting the demo is
    # what makes bus_type read "hda", so testing bus_type first would skip it.
    if not _maybe_demo_hidden_speaker_pin(info):
        if info.bus_type == "hda":
            _detect_hda_speakers(info)
    return info


def _gather_speaker_info() -> SpeakerInfo:
    """Collect all audio hardware information into a SpeakerInfo."""
    import platform

    info = SpeakerInfo(kernel=platform.release(), distro=get_distro_pretty_name())

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

    # Speaker detection — branch by bus type, unless a demo machine stands in
    if not _maybe_demo_hidden_speaker_pin(info):
        if info.bus_type == "soundwire":
            _detect_soundwire_speakers(info)
        elif info.bus_type == "hda":
            _detect_hda_speakers(info)

    # Bus-agnostic: a TI smart-amp firmware gate sits on the SOF/HDA card
    # regardless of how the speakers themselves are wired.
    info.firmware_gates = detect_speaker_firmware_gates()

    # Merged amp-status evidence (firmware presence + kernel-log markers),
    # unless a demo override is requested for previewing the section.
    if not _maybe_demo_amp_status(info):
        _gather_amp_evidence(info)

    return info


def _amp_status_lines(info: SpeakerInfo) -> list[str]:
    """Build the compact "Speaker amplifier status" body — raw evidence, no verdict.

    Terse by default (a one-line bound-amp summary) and shows actual kernel-log
    lines rather than a health verdict, because nothing in the log reliably
    proves an amp is voicing correctly. Only an enumerated-but-unbound device,
    an off firmware gate (#17), and a narrow set of unambiguous kernel-log
    errors are flagged; the rest is shown for the reader to interpret.
    """
    lines: list[str] = []
    bound = [a for a in info.amp_status if a.bound]
    unbound = [a for a in info.amp_status if not a.bound]

    if bound:
        drivers = ", ".join(sorted({a.driver for a in bound})) or "unknown"
        chans = sorted({a.channels for a in bound if a.channels})
        ch_str = "/".join(f"{c}ch" for c in chans) if chans else "?ch"
        lines.append(f"  {len(bound)} amplifier(s) bound ({drivers}); {ch_str}")
    if unbound:
        # Neutral: an unbound slave may be a non-amp peripheral (jack codec,
        # DMIC) or one still binding — not necessarily a silent speaker.
        names = ", ".join(a.node for a in unbound)
        lines.append(f"  {len(unbound)} SoundWire device(s) with no driver bound "
                     f"(may be non-amp or still binding): {names}")

    # #17 TI firmware gate, folded into the unified view (HDA or SoundWire).
    # The symptom stays open-ended for the same reason the finding's copy
    # does: the amp drives only the woofers on most laptops, but where it
    # drives every speaker an off gate makes everything thin or crackly
    # rather than silencing the bass (#39).
    for g in info.firmware_gates:
        mark = "" if g.on else "⚠ "
        state = "on" if g.on else "OFF — speakers may be silent, thin or crackly"
        lines.append(f"  {mark}{g.name}: {state} (card {g.card_id})")
        if not g.on:
            # Both --speaker-info and --doctor end in this section, so it is
            # the only place either of them can hand over the fix. Its own
            # line, unwrapped, because it has to survive a copy-paste.
            lines.append(f"      turn it on:  {amixer_enable_cmd(g)}")

    # Driver-keyed firmware presence (only when a smart-amp driver is loaded).
    if info.amp_firmware:
        extra = f", …+{len(info.amp_firmware) - 1} more" if len(info.amp_firmware) > 1 else ""
        lines.append(f"  Firmware: {len(info.amp_firmware)} file(s) present "
                     f"(e.g. {info.amp_firmware[0]}{extra}); presence is generic — "
                     "the kernel log decides whether this model's blob loaded")
    elif info.amp_firmware_missing:
        # Neutral: absence isn't proof — the blob may live outside the searched
        # roots, or under an SSID-specific name we can't predict.
        lines.append("  Firmware: none found under /lib/firmware — could not "
                     "confirm (see the kernel log)")

    # Self-check grep derived from the keywords we actually scanned, so the
    # printed command can't contradict what the report found.
    grep = info.amp_log_grep or "cs35l|tas2|cirrus"
    grep_hint = f"journalctl -k -b | grep -iE '{grep}'"

    # Kernel-log evidence: show the lines, flag only unambiguous errors.
    if not info.amp_log_available:
        lines.append(f"  Kernel log: not accessible — run:  {grep_hint}")
    elif info.amp_log:
        errors = [l for is_err, l in info.amp_log if is_err]
        if errors:
            lines.append("  ⚠ Kernel log — amp firmware/init error:")
            lines += [f"      {l}" for l in errors[:3]]
            # Surface the cap (no silent truncation) and the command to read the
            # full log — the matched lines are a sample, not the whole story.
            tail = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
            lines.append(f"      see full log{tail}:  {grep_hint}")
        else:
            lines.append(f"  Kernel log: {len(info.amp_log)} amp line(s), no known "
                         "failure marker — scan isn't exhaustive, so read them yourself:")
            lines.append(f"      {grep_hint}")
    elif info.amp_firmware_missing:
        # Firmware looked missing but the current boot log has no amp lines (e.g.
        # rotated out) — still point at the log rather than dangle the reference.
        lines.append(f"  Kernel log: no amp lines this boot — inspect:  {grep_hint}")

    return lines or ["  (no smart amplifier detected)"]


def _print_speaker_info(info: SpeakerInfo):
    """Print the collected speaker info report."""
    sections = []

    # System
    lines = []
    if info.product:
        lines.append(f"  Product: {info.product}")
    if info.family:
        lines.append(f"  Family:  {info.family}")
    if info.distro:
        lines.append(f"  OS:      {info.distro}")
    kernel_line = f"  Kernel:  {info.kernel}"
    # Age annotation (issue #33): makes a pasted report self-triaging — an old
    # series is a real bad-sound suspect regardless of the preset.
    series = parse_kernel_series(info.kernel)
    aged = _kernel_series_age(series, date.today()) if series else None
    if aged:
        released, months = aged
        plural = "" if months == 1 else "s"
        kernel_line += (f" (series {series[0]}.{series[1]}, released {released}"
                        f" — ~{months} month{plural} old)")
    lines.append(kernel_line)
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
    elif info.bus_type == "hda" and (info.speakers or info.unconfigured_pins):
        speaker_lines = [
            f"  {s.node}: {s.control_name} ({s.role}, "
            f"{'stereo' if s.channels == 2 else 'mono'})"
            # The tag is what makes the fix verifiable: the firmware still
            # calls such a pin unconnected, so without it an applied quirk and
            # a BIOS-declared speaker are the same line (issue #53).
            + (f" [{s.override}]" if s.override else "")
            for s in info.speakers
        ] or ["  (none configured)"]
        if info.unconfigured_pins:
            # Raw evidence, and one verdict where we have one: these are
            # usually spare pins, but a speaker pin the BIOS wrongly calls
            # unconnected looks identical (issue #53) — except on the machines
            # upstream ships a fix for, where the quirk table names the pin.
            # Marking it is what keeps this section from talking a reader out
            # of a fix the same report just handed them.
            found = find_hidden_speaker_pin(info)
            flagged = set(found[2]) if found else set()
            speaker_lines.append("  Output-capable pins left unconfigured:")
            speaker_lines += [
                f"    {p.node}: pincap {p.pincap}, default {p.pin_default}"
                + ("  ⚠ a kernel fix declares this a speaker"
                   if p.node in flagged else "")
                for p in info.unconfigured_pins
            ]
            # Said here because this section is what a reader stares at: spare
            # pins are ordinary, and a list of them is not a fault report.
            # Wrapped to the terminal like the rest of this tool's prose —
            # rich is handed soft_wrap=True and never reflows — with the
            # continuation hanging under the opening bracket.
            speaker_lines += textwrap.wrap(
                ("(the unflagged ones are normal — a spare pin only matters "
                 "if your device has more speakers than are listed above)"
                 if flagged else
                 "(spare pins are normal — this only matters if your device "
                 "has more speakers than are listed above)"),
                width=console._wrap_width(), initial_indent="    ",
                subsequent_indent="     ", break_on_hyphens=False)
        sections.append(("HDA internal speakers", speaker_lines))

    # PCM playback devices
    sections.append(("PCM playback devices",
                      [f"  pcm{dev}p: {name}" for dev, name in info.pcm_devices]))

    # Merged, bus-agnostic amplifier status: per-amp bind/channels/runtime, the
    # #17 TI firmware gate, driver-keyed firmware presence, and kernel-log
    # evidence — one section, kept terse (detail only when something's wrong).
    sections.append(("Speaker amplifier status", _amp_status_lines(info)))

    # Speaker layout estimate. It counts what Linux configured, so on a machine
    # with a pin fix missing it states the very number the warning above says
    # is wrong — read as a bottom line, that talks the reader out of the fix.
    layout = f"  {info.layout_summary}"
    if info.bus_type == "hda" and find_hidden_speaker_pin(info):
        layout += " (what Linux drives — the flagged pin above would add more)"
    sections.append(("Speaker layout estimate", [layout]))

    for title, lines in sections:
        console.cprint("head", f"=== {title} ===")
        print("\n".join(lines))
        print()


def report_speaker_info():
    """Report detected audio hardware and speaker layout."""
    # Version-stamp the block: users paste this verbatim into the device-report
    # issue form, so the maintainer can see which build was tested.
    console.cprint("head", f"speaker-tuning-to-easyeffects {version.get_version()}")
    print()
    info = _gather_speaker_info()
    _print_speaker_info(info)


# --- Environment self-diagnostics (--doctor) ---------------------------------
# A generated preset can be flawless yet inaudible because of the *environment*
# it lands in: EasyEffects 7 (which can't read the v8 preset format), presets
# written to the Flatpak path while EE runs native (or vice-versa), a missing
# impulse file so the speaker-correction convolver loads nothing, no Dolby
# preset selected, or a kernel series so old it mis-configures the speaker
# path itself (issue #33). --doctor surfaces those deterministically (#22),
# and warn_ee_environment() reuses the same probes to warn at the end of a
# normal run. The pure helpers below take plain inputs so they're unit-tested
# without touching the system; the _probe_/_gather_ wrappers do the I/O.

# The report vocabulary is shared with ee_to_pipewire.py's PipeWire-side
# doctor (see lib/doctor.py) so the two read as one tool. Bound to the names this
# file already uses; the printers below bind our console and wrap width.
DOCTOR_PASS = doctor.DOCTOR_PASS
DOCTOR_WARN = doctor.DOCTOR_WARN
DOCTOR_FAIL = doctor.DOCTOR_FAIL
DOCTOR_UNKNOWN = doctor.DOCTOR_UNKNOWN
CheckResult = doctor.CheckResult

# EE names stacked instances of a plugin "convolver#0", "equalizer#1", … —
# match the speaker-correction convolver regardless of its index. Keep the
# "kernel-name" literal in step with make_convolver().
_CONVOLVER_KEY_RE = re.compile(r"^convolver#\d+$")


@dataclass
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)
    facts: dict = field(default_factory=dict)   # raw probed values, always shown
    speaker_info: "SpeakerInfo | None" = None


def _tilde(path) -> str:
    """Render a path with $HOME collapsed to ~ — paste-safe (no username)."""
    s = str(path)
    home = str(Path.home())
    return "~" + s[len(home):] if s == home or s.startswith(home + os.sep) else s


def parse_ee_version(text: str) -> tuple[int, int, int] | None:
    """Extract (major, minor, patch) from an EasyEffects version string.

    Keys ONLY on the first ``N.N[.N]`` numeric token, so it's robust to the
    ``easyeffects ``/``EasyEffects ``/``Version: `` prefix and to case. Patch
    defaults to 0 when absent. Returns None when there's no version-like token.
    """
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def ee_silent_message(reason: str, tail: str) -> str:
    """The 'installed but --version didn't answer' explanation, shared by
    --doctor and the end-of-run warning so the two can't drift. ``tail``
    finishes the sentence with what it means where it's being said."""
    return (f"EasyEffects is installed but `easyeffects --version` didn't "
            f"answer ({reason}), so its version wasn't checked. EasyEffects 8 "
            f"needs a display to answer --version, so this is expected from a "
            f"headless shell (ssh, tmux){tail}")


def ee_v7_message(vstr: str) -> str:
    """Why an EasyEffects before 8 can't use these presets, shared by --doctor
    and the end-of-run warning so the two can't drift. Callers supply their own
    headline and install instructions — one inline sentence for the report,
    copy-paste commands for the warning."""
    return (f"EasyEffects 8 changed the preset (filter-chain) format, and these "
            f"presets use the new one. On {vstr} they don't load correctly — the "
            "speaker-correction filter loads nothing, so you'll hear little or "
            "no difference.")


def ee_version_status(version: tuple[int, int, int] | None,
                      found: bool, silent: str | None = None) -> CheckResult:
    """Verdict for the EasyEffects version. FAIL — the only loud error — is
    reserved for a *cleanly parsed* major < 8, so an EE-8 user is never told
    they're on 7. ``found`` distinguishes "no EE at all" (a valid
    generating-for-another-machine case → WARN) from "installed but version
    unreadable" (→ UNKNOWN); ``silent`` names the reason when EE is installed
    but never answered at all (→ UNKNOWN, never "not found")."""
    if version is None:
        if not found and silent:
            return CheckResult(DOCTOR_UNKNOWN, "EasyEffects version",
                ee_silent_message(silent, " — re-run this from your desktop "
                                          "session to check the version."))
        if not found:
            return CheckResult(DOCTOR_WARN, "EasyEffects version",
                "not found on PATH or via Flatpak. If you're generating presets "
                "to copy to another machine, ignore this — otherwise install "
                "EasyEffects 8 (e.g. the Flathub Flatpak).")
        return CheckResult(DOCTOR_UNKNOWN, "EasyEffects version",
            "EasyEffects is installed but its version couldn't be read — make "
            "sure it's version 8 or newer.")
    vstr = ".".join(str(x) for x in version)
    if version[0] < 8:
        return CheckResult(DOCTOR_FAIL, "EasyEffects version",
            f"{vstr} detected — these presets need EasyEffects 8. "
            + ee_v7_message(vstr) +
            " Install EasyEffects 8 (the Flathub Flatpak, or your distro's "
            "package if it ships 8.x).")
    return CheckResult(DOCTOR_PASS, "EasyEffects version", f"{vstr} (compatible).")


# A stable distro's kernel is at most ~9 months old on the distro's release day
# (Debian 13 shipped 6.12 at 9 months; Ubuntu LTS GA kernels at ~1 month), so
# 18 months keeps every fresh install quiet for 9+ months and never flags
# HWE/Fedora/Arch users — while still catching the real case we have (#33
# fired at 6.12 + 20 months; LTS point releases backport one-line quirks but
# not the driver rework / power-management fixes of that class).
_KERNEL_OLD_MONTHS = 18


def parse_kernel_series(release: str) -> tuple[int, int] | None:
    """(major, minor) from a ``platform.release()`` string, e.g.
    ``"6.12.74+deb13+1-amd64"`` → ``(6, 12)``. None when unparseable."""
    m = re.match(r"(\d+)\.(\d+)", (release or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def _kernel_series_age(series: tuple[int, int],
                       today: date) -> tuple[str, int] | None:
    """(release "YYYY-MM", age in whole months) for an in-table series."""
    released = kernel_releases._KERNEL_SERIES_RELEASES.get(series)
    if not released:
        return None
    y, mo = (int(x) for x in released.split("-"))
    return released, (today.year - y) * 12 + (today.month - mo)


def kernel_age_status(release: str, today: date | None = None) -> CheckResult:
    """Verdict for the running kernel's age. WARN is a hint, not an error: an
    old series *can* be the whole problem on laptop speakers (issue #33), but
    only the user can tell — the detail says what symptom would confirm it."""
    today = today or date.today()
    label = "Kernel age"
    series = parse_kernel_series(release)
    if series is None:
        return CheckResult(DOCTOR_UNKNOWN, label,
            f"couldn't parse a kernel version from {release!r}.")
    sstr = f"{series[0]}.{series[1]}"
    if series > max(kernel_releases._KERNEL_SERIES_RELEASES):
        return CheckResult(DOCTOR_PASS, label,
            f"{sstr} — newer than any series this tool knows about.")
    aged = _kernel_series_age(series, today)
    if aged is None:
        if series < min(kernel_releases._KERNEL_SERIES_RELEASES):
            return CheckResult(DOCTOR_WARN, label,
                f"{sstr} is very old (pre-2021). Laptop speaker support "
                "lands kernel-side; strongly consider a newer kernel.")
        return CheckResult(DOCTOR_UNKNOWN, label, f"{sstr} — unknown series.")
    released, months = aged
    if months <= _KERNEL_OLD_MONTHS:
        plural = "" if months == 1 else "s"
        return CheckResult(DOCTOR_PASS, label,
            f"{sstr} (released {released}, ~{months} month{plural} old).")
    return CheckResult(DOCTOR_WARN, label,
        f"{sstr} was released {released} (~{months} months ago). "
        + kernel_old_message())


def kernel_old_message() -> str:
    """Why an old kernel series matters for laptop speakers, shared by --doctor
    and the end-of-run hint so the two can't drift. Callers supply the headline
    naming the series and its age."""
    return ("Laptop speaker fixes (amp drivers, codec setup, power-management "
            "quirks) land kernel-side and are not always backported to older "
            "series — if your speakers sound thin, muffled or garbled even with "
            "EasyEffects off, a newer kernel (your distro's backports or "
            "hardware-enablement/HWE kernel) may fix that.")


def install_status(flatpak_exists: bool, native_exists: bool,
                   base_is_flatpak: bool, base_display: str,
                   ee_is_flatpak: bool | None) -> CheckResult:
    """Verdict for where presets are written vs where EE actually runs.
    ``ee_is_flatpak`` is which install answered the version probe (None if
    unknown)."""
    where = "Flatpak" if base_is_flatpak else "native"
    if not flatpak_exists and not native_exists:
        return CheckResult(DOCTOR_WARN, "Install location",
            f"no EasyEffects data dir found yet; presets go to the {where} "
            f"location ({base_display}). Launch EasyEffects once, or pass "
            "--output-dir/--irs-dir.")
    if ee_is_flatpak is not None:
        run_where = "Flatpak" if ee_is_flatpak else "native"
        if run_where != where:
            # WARN, not FAIL: the install that answered the probe isn't
            # necessarily the one the user launches (dual-install systems), so
            # we can't assert "EE never sees them" with certainty.
            return CheckResult(DOCTOR_WARN, "Install location",
                f"presets were written to the {where} location ({base_display}), "
                f"but the EasyEffects detected is the {run_where} build — if "
                "that's the one you run, it won't see them. Re-run with "
                "--output-dir/--irs-dir for the install you use.")
        return CheckResult(DOCTOR_PASS, "Install location",
            f"{run_where} install; presets written to {base_display}.")
    if flatpak_exists and native_exists:
        return CheckResult(DOCTOR_WARN, "Install location",
            f"both Flatpak and native data dirs exist; the script writes to the "
            f"{where} one ({base_display}) — make sure that's the EasyEffects "
            "you run.")
    return CheckResult(DOCTOR_PASS, "Install location",
        f"{where} install; presets written to {base_display}.")


def check_preset_kernel(preset_json: dict, irs_stems: set,
                        preset_name: str) -> CheckResult:
    """Verify a generated output preset's speaker-correction filter (convolver)
    references an impulse file (.irs) that actually exists. A missing or
    misnamed impulse = silent passthrough: the dominant audible block does
    nothing. ``irs_stems`` are the .irs filename stems present in the irs dir."""
    label = f"Preset {preset_name}"
    if not isinstance(preset_json, dict) or not isinstance(
            preset_json.get("output"), dict):
        return CheckResult(DOCTOR_FAIL, label,
            "not a valid EasyEffects output preset (no 'output' section).")
    conv_keys = [k for k in preset_json["output"] if _CONVOLVER_KEY_RE.match(k)]
    if not conv_keys:
        return CheckResult(DOCTOR_WARN, label,
            "no speaker-correction filter (convolver) in this preset.")
    conv = preset_json["output"][conv_keys[0]]
    conv = conv if isinstance(conv, dict) else {}
    if "kernel-path" in conv and "kernel-name" not in conv:
        return CheckResult(DOCTOR_FAIL, label,
            "uses the old EasyEffects 7 'kernel-path' format — these presets "
            "need EasyEffects 8.")
    name = conv.get("kernel-name", "")
    if not name:
        return CheckResult(DOCTOR_FAIL, label,
            "the speaker-correction filter has no impulse file set — it's silent.")
    if name not in irs_stems:
        return CheckResult(DOCTOR_FAIL, label,
            f"impulse file '{name}.irs' is missing from the irs dir — the "
            "speaker-correction filter loads nothing (silent). Re-run the "
            "script so the .irs is written next to the preset.")
    if conv.get("bypass") is True:
        return CheckResult(DOCTOR_WARN, label,
            f"loads {name}.irs but the speaker-correction filter is bypassed in "
            "the preset.")
    return CheckResult(DOCTOR_PASS, label,
        f"speaker-correction filter loads {name}.irs.")


def loaded_preset_status(rc_data: dict, generated_names) -> CheckResult:
    """Whether EasyEffects' selected output preset is one this script generated.
    Reports last-loaded / fallback without over-claiming which is *active*
    (per-device autoloading lives elsewhere in EE's config). The empty
    ``Nothing`` bypass preset is excluded from "generated": having it selected
    is itself a "sounds like nothing" cause, not a healthy state."""
    dolby = {n for n in generated_names if n != BYPASS_PRESET_NAME}
    loaded = rc_data.get("last_output_preset", "")
    fallback = rc_data.get("fallback_preset", "")
    if not loaded and not fallback:
        return CheckResult(DOCTOR_WARN, "Selected preset",
            "EasyEffects has no output preset recorded yet — open it and load a "
            "Dolby-* preset for the speakers.")
    if loaded == BYPASS_PRESET_NAME:
        return CheckResult(DOCTOR_WARN, "Selected preset",
            f"the silent '{BYPASS_PRESET_NAME}' bypass preset is selected — that's "
            "no processing by design. Load a Dolby-* preset in EasyEffects.")
    if loaded in dolby:
        matched = loaded
    elif rc_data.get("uses_fallback") and fallback in dolby:
        matched = fallback
    else:
        matched = ""
    if matched:
        return CheckResult(DOCTOR_PASS, "Selected preset",
            f"EasyEffects last loaded '{matched}'.")
    return CheckResult(DOCTOR_WARN, "Selected preset",
        f"EasyEffects' selected output preset is '{loaded or fallback}', which "
        "this script didn't generate — load a Dolby-* preset in EasyEffects.")


def autostart_status(rc_data: dict) -> CheckResult:
    """Whether EasyEffects is set to keep running in the background so the
    preset stays applied. Two Background-Service toggles matter, both persisted
    in ``[Window]``: ``autostartOnLogin`` (launch at login — default off) and
    ``enableServiceMode`` (stay active when the window is closed — default on).
    The preset only processes audio while EasyEffects runs, so if EITHER is off
    it silently stops applying after a window-close or reboot — a common "it was
    working, now it sounds like nothing" cause. Both off and a single one off
    are all problem states, so we name exactly the toggle(s) that are off."""
    autostart = rc_data.get("autostart_on_login")
    service = rc_data.get("service_mode")
    if autostart and service:
        return CheckResult(DOCTOR_PASS, "Background service",
            "EasyEffects autostarts as a background service at login — the "
            "preset applies automatically and survives reboots.")
    # Name the toggle(s) up front and adjacent, then group the explanations —
    # inline parentheticals buried the second toggle so it read as one warning.
    off, why = [], []
    if not service:
        off.append("'Enable service mode'")
        why.append("service mode keeps it running once the window is closed")
    if not autostart:
        off.append("'Autostart on login'")
        why.append("autostart relaunches it after a reboot")
    return CheckResult(DOCTOR_WARN, "Background service",
        "EasyEffects won't reliably keep processing in the background, so the "
        "preset applies only while it's open. In EasyEffects > Preferences > "
        "Background Service, turn on " + " and ".join(off)
        + " (" + "; ".join(why) + ").")


def speaker_pin_status(info: SpeakerInfo) -> CheckResult | None:
    """Verdict line for a speaker pin the firmware hides, or None when this
    machine isn't one upstream has had to fix (nearly all of them — a PASS for
    a quirk that was never needed is noise).

    WARN, not FAIL, on the same reasoning as the kernel-age check: the match is
    machine-exact, but only the user can confirm they hear no bass, and a
    2-driver laptop that somehow matched would be unharmed by ignoring this.

    The procedure rides in ``steps``, unwrapped, rather than in the detail:
    the whole fix is here, so nobody has to re-run the tool a different way to
    reach it. Where the fixup has no forcible name the builder returns nothing
    and ``upgrade_prospect`` has already said why, so the check promises no
    command it won't print.
    """
    found = find_hidden_speaker_pin(info)
    if not found:
        return None
    quirk, codec_ssid, missing = found
    return CheckResult(
        DOCTOR_WARN, "Speaker pins",
        f"upstream Linux carries a fix for this exact model that declares "
        f"{_pin_phrase(missing)} on codec {codec_ssid} an internal speaker, "
        "and your kernel isn't applying it — those speakers get no signal, "
        "whatever the preset does. "
        + upgrade_prospect(quirk, info.kernel),
        # Same width the printer will wrap the detail to, less its indent, so
        # the prose here lines up with the prose above it.
        steps=speaker_pin_fix_steps(quirk, missing,
                                    _card_uses_sof(info.sound_cards),
                                    console._wrap_width() - 9,
                                    speaker_info_below=True))


def firmware_gate_status(gates: list[FirmwareGate]) -> CheckResult | None:
    """Verdict line for the smart-amp firmware gates, or None when the machine
    exposes no such control (most don't — there is nothing to report either
    way, and a PASS for an absent control is noise).

    The gate sits *upstream* of everything EasyEffects does, which is why it
    belongs among the checks and not only in the raw hardware dump: a report
    that says "no blocking problems" beside a gate that is off is wrong about
    the one thing most likely to explain silence.

    WARN, not FAIL: an off gate mutes the woofers on most laptops, but on some
    the firmware auto-loads anyway and flipping it is an audible no-op (#39),
    so it is a strong suspect rather than a proven fault.

    The command rides in ``steps``, unwrapped. It also prints in the amp
    section further down, which --speaker-info reaches and this check does
    not, so the repeat within a --doctor run is deliberate: the check is where
    a reader acts, the section is raw evidence.
    """
    if not gates:
        return None
    off = [g for g in gates if not g.on]
    if not off:
        return CheckResult(DOCTOR_PASS, "Speaker firmware gate",
                           "the amplifier is allowed to load its firmware.")
    names = ", ".join(g.name for g in off)
    return CheckResult(
        DOCTOR_WARN, "Speaker firmware gate",
        f"{names} is off, so the amplifier runs untuned ahead of the preset "
        "and your speakers may be silent, thin or crackly whatever the preset "
        "does. Switch it on:",
        steps=tuple(("cta", amixer_enable_cmd(g)) for g in off))


_doctor_summary = doctor.summarize


def _flatpak_version_text(info_output: str) -> str:
    """Pull just the ``Version:`` line out of `flatpak info` output. The full
    blob has other numeric tokens (sizes, refs) that would mis-parse, so we
    isolate the one line; absent → "" (→ UNKNOWN, never a wrong version)."""
    for line in info_output.splitlines():
        if line.strip().lower().startswith("version:"):
            return line
    return ""


@dataclass
class EEProbe:
    """Outcome of looking for an EasyEffects install.

    ``found`` means a binary *answered*; ``silent`` is set instead when one is
    demonstrably installed but couldn't answer, and carries the short reason.
    All three of found / silent / neither are distinct states — collapsing the
    middle one into "not installed" is what misled issue #46.
    """
    version: tuple[int, int, int] | None = None
    found: bool = False
    source: str = ""
    is_flatpak: bool | None = None
    silent: str | None = None


def _probe_ee_version() -> EEProbe:
    """Probe the installed EasyEffects version. Read-only, time-bounded, never
    raises.

    Probes the install the script writes to (per _USE_FLATPAK) first, then the
    other, and prefers a *parseable* version over a found-but-unreadable answer
    — so a stale/shim binary on one install can't mask a healthy version on the
    other (issue #22 review). ``found`` means an EE binary actually answered, so
    version=None with found=True means 'installed but version unreadable'."""
    def run(cmd):
        """(output, failure) — exactly one is non-None; failure is a short
        human-readable reason the command produced no answer."""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except FileNotFoundError:
            return None, None                      # nothing to run: absent, not silent
        except subprocess.TimeoutExpired:
            return None, "timed out after 5s"
        except (subprocess.SubprocessError, OSError) as exc:
            return None, str(exc) or type(exc).__name__
        if r.returncode != 0:
            first = next((ln.strip() for ln in (r.stderr or "").splitlines()
                          if ln.strip()), "")
            return None, first or f"exited with status {r.returncode}"
        return (r.stdout or "") + "\n" + (r.stderr or ""), None

    def native():
        out, failure = run(["easyeffects", "--version"])
        if out is not None:
            return parse_ee_version(out), True, None
        # A binary that's on PATH (or already running) but couldn't answer is
        # installed, not absent. EE 8's Qt build needs a display to handle
        # --version, so from a headless shell (ssh, tmux) it exits non-zero —
        # indistinguishable from "not installed" if we only read the exit code
        # (issue #46, where a healthy 8.2.8 was reported missing).
        installed = shutil.which("easyeffects") or easyeffects_is_running()
        return None, False, (failure or "no output") if installed else None

    def flatpak():
        # `flatpak info` exits non-zero precisely when the app isn't installed,
        # so a failure here is absence — never the silent-but-installed case.
        out, _failure = run(["flatpak", "info", _FLATPAK_APP_ID])
        if out is None:
            return None, False, None
        return parse_ee_version(_flatpak_version_text(out)), True, None

    probes = ([(True, flatpak), (False, native)] if _USE_FLATPAK
              else [(False, native), (True, flatpak)])
    fallback = EEProbe()                 # best found-but-unparseable, in order
    for is_flatpak, probe in probes:
        version, found, silent = probe()
        src = "flatpak info" if is_flatpak else "easyeffects --version"
        if not found:
            if silent and fallback.silent is None:
                fallback.silent = silent
                fallback.source = src
            continue
        if version is not None:
            return EEProbe(version, True, src, is_flatpak)
        if not fallback.found:           # remember the first install that answered
            fallback = EEProbe(None, True, src, is_flatpak, fallback.silent)
    return fallback


def _gather_doctor_report(output_dir: Path, irs_dir: Path, rc_path: Path,
                          custom_dirs: bool = False) -> DoctorReport:
    """Run every probe and assemble a DoctorReport. All I/O is wrapped so a
    missing binary / unreadable file degrades to a soft line, never a crash."""
    report = DoctorReport()

    # 1. EasyEffects version / compatibility
    probe = _probe_ee_version()
    version, found, source, ee_is_flatpak = (
        probe.version, probe.found, probe.source, probe.is_flatpak)
    report.checks.append(ee_version_status(version, found, probe.silent))

    # 2. Install location (skip the EE-location verdict for custom dirs)
    if custom_dirs:
        report.checks.append(CheckResult(DOCTOR_PASS, "Install location",
            f"custom output dir ({_tilde(output_dir)}) — skipping EasyEffects "
            "location checks."))
    else:
        report.checks.append(install_status(
            _FLATPAK_BASE.exists(), _NATIVE_BASE.exists(), _USE_FLATPAK,
            _tilde(_EASYEFFECTS_BASE), ee_is_flatpak))

    # 3. Preset + impulse-file integrity
    try:
        irs_stems = {p.stem for p in irs_dir.glob("*.irs")}
    except OSError:
        irs_stems = set()
    try:
        preset_paths = sorted(output_dir.glob("*.json"))
    except OSError:
        preset_paths = []
    generated_names = [p.stem for p in preset_paths]
    dolby_presets = [p for p in preset_paths if p.stem != BYPASS_PRESET_NAME]
    if not dolby_presets:
        report.checks.append(CheckResult(DOCTOR_WARN, "Generated presets",
            f"no presets found in {_tilde(output_dir)} — run the script on your "
            "tuning XML first."))
    else:
        for p in dolby_presets:
            try:
                data = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                report.checks.append(CheckResult(DOCTOR_FAIL, f"Preset {p.stem}",
                    "could not be read / not valid JSON."))
                continue
            report.checks.append(check_preset_kernel(data, irs_stems, p.stem))

    # 4. EasyEffects runtime state (loaded preset, sink, chain)
    try:
        rc_text = rc_path.read_text(encoding="utf-8")
    except OSError:
        rc_text = ""
    rc = read_ee_rc(rc_text)
    # The selected-preset check compares against presets in output_dir; that's
    # only meaningful when output_dir is where EE actually loads from (default
    # dirs). Under custom dirs, surface the loaded preset as a fact instead.
    if rc_text and not custom_dirs:
        report.checks.append(loaded_preset_status(rc, generated_names))
    # Background-service / autostart is install-global, not output-dir-specific,
    # so it runs even under custom dirs (unlike the selected-preset check).
    if rc_text:
        report.checks.append(autostart_status(rc))

    # 5. Hardware / codec context (folds in --speaker-info)
    report.speaker_info = _gather_speaker_info()

    # 6. Smart-amp firmware gate — upstream of the whole preset (issue #17)
    gate_check = firmware_gate_status(report.speaker_info.firmware_gates)
    if gate_check is not None:
        report.checks.append(gate_check)

    # 7. A woofer pin the firmware hides, so half the speakers go unused
    #    upstream of the whole preset (issue #53)
    pin_check = speaker_pin_status(report.speaker_info)
    if pin_check is not None:
        report.checks.append(pin_check)

    # 8. Kernel age — speaker-amp fixes land kernel-side (issue #33)
    report.checks.append(kernel_age_status(report.speaker_info.kernel))

    report.facts = {
        "ee_version": (".".join(map(str, version)) if version else "unknown")
                      + (f" (via {source})" if source else ""),
        "ee_running": easyeffects_is_running(),
        "install": "Flatpak" if _USE_FLATPAK else "native",
        "output_dir": _tilde(output_dir),
        "irs_dir": _tilde(irs_dir),
        "preset_count": len(generated_names),
        "irs_count": len(irs_stems),
        "rc_path": _tilde(rc_path),
        "rc_present": bool(rc_text),
        "selected_preset": rc.get("last_output_preset", "")
                           or rc.get("fallback_preset", ""),
        "autostart_on_login": rc.get("autostart_on_login", False),
        "service_mode": rc.get("service_mode", True),
        "output_device": rc.get("output_device", ""),
        "output_plugins": rc.get("output_plugins", []),
    }
    return report


def emit_check(check: CheckResult) -> None:
    """Print one diagnostic line: status box, label, wrapped detail, steps.

    Hands off to the shared printer so this report and ee_to_pipewire.py's
    PipeWire-side one read as one tool. This used to be a second
    implementation of it, which is how a check's ``steps`` reached the
    PipeWire doctor and not this one — the same duplication the steps
    themselves exist to end.
    """
    doctor.emit_check(check, console.cprint, console._wrap_width())


def print_doctor_summary(checks: list[CheckResult]) -> None:
    """Print the counted one-line summary. Split from the verdict below it
    because the two surfaces put different things between them — the
    EasyEffects report interleaves its paste block."""
    fail, warn, ok, unknown = _doctor_summary(checks)
    parts = [f"{fail} FAIL", f"{warn} WARN", f"{ok} PASS"]
    if unknown:
        parts.append(f"{unknown} UNKNOWN")
    console.cprint("err" if fail else ("warn" if (warn or unknown) else "ok"),
           "Summary: " + ", ".join(parts))


def print_doctor_verdict(checks: list[CheckResult]) -> None:
    """Print the one-line verdict, shared so both doctors conclude the same way.

    A WARN suppresses the all-clear: every warning either report can raise
    names something that plausibly explains "I hear no difference", so
    "no blocking problems" printed beside one contradicts the lines above it.
    """
    fail, warn, ok, unknown = _doctor_summary(checks)
    if not (fail or warn or unknown):
        console.cprint("ok", "No blocking problems detected.")
    elif warn and not fail:
        console.cprint("warn", "Nothing failed outright — the ⚠ lines above are what "
                       "to fix first.")
    elif unknown and not fail:
        console.cprint("warn", "Some checks couldn't be verified (the [ ? ] lines "
                       "above); the rest look OK.")


def _print_doctor_report(report: DoctorReport) -> None:
    """Print a compact, paste-safe diagnostic report."""
    emit = emit_check

    console.cprint("head", f"speaker-tuning-to-easyeffects {version.get_version()}")
    console.cprint("head", "=== EasyEffects doctor ===")
    print()
    # Per-preset checks collapse to one line when they all pass (a machine can
    # have dozens of profiles); any problem preset is still listed individually.
    preset_checks = [c for c in report.checks if c.label.startswith("Preset ")]
    preset_problems = [c for c in preset_checks if c.status != DOCTOR_PASS]
    shown_presets = False
    for c in report.checks:
        if c.label.startswith("Preset "):
            if not shown_presets:
                shown_presets = True
                ok_n = len(preset_checks) - len(preset_problems)
                if ok_n:
                    console.cprint("ok", f"  [{DOCTOR_PASS:^4}] Presets "
                                 f"({ok_n}/{len(preset_checks)} load their impulse file)")
                for pc in preset_problems:
                    emit(pc)
            continue
        emit(c)
    print()
    print_doctor_summary(report.checks)
    print()

    # Raw probed facts — always shown so an issue can be diagnosed remotely even
    # when a heuristic verdict is UNKNOWN or wrong.
    f = report.facts
    console.cprint("head", "=== Environment (paste this into your issue) ===")
    print(f"  Tool:         speaker-tuning-to-easyeffects {version.get_version()}")
    print(f"  EasyEffects:  {f.get('ee_version', '?')}; "
          f"running: {'yes' if f.get('ee_running') else 'no'}")
    print(f"  Install:      {f.get('install')} (writes to {f.get('output_dir')})")
    print(f"  Presets/IRs:  {f.get('preset_count', 0)} presets, "
          f"{f.get('irs_count', 0)} impulse files")
    print(f"  Config:       {f.get('rc_path')} "
          f"({'present' if f.get('rc_present') else 'absent'})")
    print(f"  Background:   service mode "
          f"{'on' if f.get('service_mode') else 'off'}, autostart "
          f"{'on' if f.get('autostart_on_login') else 'off'}")
    if f.get("selected_preset"):
        print(f"  Selected:     {f['selected_preset']}")
    if f.get("output_device"):
        print(f"  Output sink:  {f['output_device']}")
    if f.get("output_plugins"):
        print(f"  Active chain: {', '.join(f['output_plugins'])}")
    print()

    # What the doctor can't see — guide the user through the manual checks.
    print_doctor_verdict(report.checks)
    console.cprint("dim", "If you still hear no difference between the preset and bypass:")
    console.cprint("dim", "  • In EasyEffects, toggle the preset off/on to A/B it.")
    console.cprint("dim", "  • Make sure global bypass (the power-button icon, top bar) is OFF.")
    console.cprint("dim", "  • Confirm system output is the speaker sink and volume is up.")
    print()

    if report.speaker_info is not None:
        _print_speaker_info(report.speaker_info)

    console.cprint("cta", "Still stuck? Paste everything above into an issue:")
    console.cprint("cta", f"  {_REPORT_FORM_URL}")


def report_doctor(args) -> None:
    """--doctor entry point: run environment self-diagnostics and print them."""
    custom_dirs = (args.output_dir != DEFAULT_OUTPUT_DIR
                   or args.irs_dir != DEFAULT_IRS_DIR)
    report = _gather_doctor_report(args.output_dir, args.irs_dir,
                                   DEFAULT_EASYEFFECTS_RC, custom_dirs=custom_dirs)
    _print_doctor_report(report)


def warn_ee_environment(args) -> None:
    """End-of-run check for a normal generation run: loudly warn if the
    installed EasyEffects can't use the presets we just wrote. Silent on the
    happy path. Reuses --doctor's probes; mirrors warn_speaker_firmware_gate."""
    probe = _probe_ee_version()
    version, found, ee_is_flatpak = probe.version, probe.found, probe.is_flatpak
    ver = ee_version_status(version, found, probe.silent)

    if ver.status == DOCTOR_FAIL:
        vstr = ".".join(str(x) for x in version)
        console.cprint("err", f"\n{'=' * 60}")
        console.cprint("err", f"⚠  EasyEffects {vstr} detected — these presets need EasyEffects 8.")
        print()
        console._cprint_wrapped("dim", ee_v7_message(vstr))
        print()
        console.cprint("dim", "To fix, install EasyEffects 8:")
        console.cprint("cta", "  • Easiest on any distro — the Flathub Flatpak:")
        console.cprint("cta", "      flatpak install flathub com.github.wwmm.easyeffects")
        console.cprint("dim", "  • Or your distro's own package if it already ships 8.x")
        console.cprint("dim", "    (Debian trixie, Ubuntu 24.04+ and Fedora ≤43 still ship 7.x).")
        return

    if not found and probe.silent:
        # Installed but unreachable — say so, rather than sending someone off to
        # install what they already have (issue #46).
        # "written above" only holds on a run that wrote something: this check
        # is gated on --skip-ee-check alone, so on a dry run it referred to
        # presets the same output twice says were not written.
        console.cprint("warn", "\n⚠  " + ee_silent_message(
            probe.silent,
            " and doesn't affect what this run would write." if args.dry_run
            else " and doesn't affect the presets written above."))
    elif not found:
        console.cprint("warn", "\n⚠  Couldn't find EasyEffects — install version 8 to use these "
                       "presets (e.g. the Flathub Flatpak). Ignore if you're "
                       "generating for another machine.")

    # Install-location mismatch (only meaningful for the default EE dirs): the
    # detected EE build differs from where we wrote. Warn so the user can point
    # --output-dir/--irs-dir at the install they actually run.
    if (args.output_dir == DEFAULT_OUTPUT_DIR and args.irs_dir == DEFAULT_IRS_DIR
            and ee_is_flatpak is not None and ee_is_flatpak != _USE_FLATPAK):
        run_where = "Flatpak" if ee_is_flatpak else "native"
        where = "Flatpak" if _USE_FLATPAK else "native"
        console.cprint("warn", f"\n⚠  Presets were written to the {where} EasyEffects "
                       f"location, but the {run_where} install was detected — if "
                       "that's the one you use, it won't see them (run --doctor).")


def warn_old_kernel(release: str | None = None) -> None:
    """End-of-run hint: an old kernel series can mis-configure the speaker
    path no matter how good the preset is — issue #33 was fixed by a
    kernel upgrade, not a preset change. Silent unless the running series is
    older than _KERNEL_OLD_MONTHS. Mirrors warn_ee_environment."""
    if release is None:
        import platform
        release = platform.release()
    if kernel_age_status(release).status != DOCTOR_WARN:
        return
    series = parse_kernel_series(release)
    aged = _kernel_series_age(series, date.today()) if series else None
    sstr = f"{series[0]}.{series[1]}" if series else release
    when = f" (released {aged[0]}, ~{aged[1]} months ago)" if aged else ""

    console.cprint("warn", f"\n⚠  Your kernel series {sstr} is old{when}.")
    console._cprint_wrapped("dim", kernel_old_message())


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
        if any(windows_root.glob("dax3_ext_*.inf_*")):
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
    for _vendor, subsys, _name in get_hda_codec_ids():
        ids.add(subsys.upper())
    pci_subsys = get_pci_audio_subsystem()
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
    xml_dirs = sorted(driver_store.glob("dax3_ext_*.inf_*")) or [driver_store]
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
            if any(driver_store.glob("dax3_ext_*.inf_*")):
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
            if cand.name.startswith("dax3_ext_") and ".inf_" in cand.name:
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
    hda_codecs = get_hda_codec_ids()
    sdw_devices = get_soundwire_ids()
    pci_subsys = get_pci_audio_subsystem()

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
    xml_dirs = sorted(driver_store.glob("dax3_ext_*.inf_*"))
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


# Speaker-sink detection for autoload / smart-filter targeting.
#
# NOTE: this is the device-detection (structural) path, not the audio-math
# path, so the "every emitted parameter must trace to an XML field" invariant
# (CLAUDE.md) does NOT apply here — runtime PipeWire node selection has no XML
# provenance. The heuristics below are pragmatic and always overridable by the
# user (--autoload-sink here, --target-sink in ee_to_pipewire.py).

# device.icon_name values that mark an output we never treat as the internal
# speaker, even under the relaxed tier.
_NON_SPEAKER_ICONS = {"audio-headphones", "audio-headset"}


def _enumerate_audio_sinks() -> list[dict]:
    """Return every PipeWire Audio/Sink node with the props we classify on.

    This is the single ``pw-dump`` boundary; tests monkeypatch it to feed
    synthetic sink lists. Each dict carries 'name', 'description', 'profile',
    and 'route' (the fields EasyEffects autoload needs) plus 'icon_name', 'bus',
    and 'api' (used to tell internal speakers from HDMI / Bluetooth / headsets
    and to explain the choice in diagnostics).

    'profile' is the card *profile* description (e.g. "Analog Stereo"); 'route'
    is the active output *route* description (e.g. "Speaker"). EasyEffects keys
    its autoload files on the route description — the node's
    ``device_route_description``, taken from the SPA_PARAM_Route ``description``
    — not the profile. On UCM "HiFi" cards the two happen to coincide
    ("Speaker"), but on a classic ``analog-stereo`` card the profile is
    "Analog Stereo" while the active output route is still "Speaker", so an
    autoload entry filed under the profile never matches and the fallback wins
    (issue #18). 'route' is "" when the active output route can't be resolved
    (virtual sinks, or an older pw-dump that omits Device params); the autoload
    caller skips such sinks rather than fall back to the profile, since guessing
    a filename EE won't match just silently recreates the #18 failure.
    """
    try:
        result = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=5
        )
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        return []
    if not isinstance(data, list):  # pw-dump normally emits an array; be defensive
        return []

    # Map PipeWire Device id -> {card-profile-device index -> output route
    # description}. A sink node carries 'device.id' (its Device object) and
    # 'card.profile.device' (the route's device index within that card), so we
    # can resolve the active output route description EasyEffects matches on.
    routes_by_device: dict = {}
    for obj in data:
        if not str(obj.get("type", "")).endswith("Device"):
            continue
        dev_id = obj.get("id")
        if dev_id is None:
            continue
        params = obj.get("info", {}).get("params", {})
        out_routes = {}
        for route in params.get("Route", []) or []:
            if route.get("direction") != "Output":
                continue
            dev_idx = route.get("device")
            desc = route.get("description")
            if dev_idx is not None and desc:
                out_routes[dev_idx] = desc
        if out_routes:
            routes_by_device[dev_id] = out_routes

    sinks = []
    for obj in data:
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") != "Audio/Sink":
            continue
        route = routes_by_device.get(props.get("device.id"), {}).get(
            props.get("card.profile.device"))
        sinks.append({
            "name": props.get("node.name", ""),
            "description": props.get("node.description", ""),
            "profile": props.get("device.profile.description", ""),
            "route": route or "",
            "icon_name": props.get("device.icon_name", ""),
            "bus": props.get("device.bus", ""),
            "api": props.get("device.api", ""),
        })
    return sinks


def _classify_sink(sink: dict) -> str:
    """Classify a sink as 'strict', 'relaxed', or 'excluded'.

    'strict'   — tagged as an internal speaker (device.icon_name ==
                 'audio-speakers'); the only tier used when tagging is correct.
    'relaxed'  — an internal *analog* output that isn't tagged as a speaker but
                 also isn't obviously HDMI / Bluetooth / a headset. Fallback for
                 laptops whose UCM2 profile omits the speaker icon (issue #18:
                 the generic HDA HiFi-analog.conf sets no DeviceIcon, so
                 WirePlumber assigns the generic 'audio-card-analog' icon).
    'excluded' — everything else: HDMI/DisplayPort/SPDIF, Bluetooth, headsets,
                 and virtual / loopback / combine sinks.
    """
    if sink.get("icon_name") == "audio-speakers":
        return "strict"

    name_l = sink.get("name", "").lower()
    icon_l = sink.get("icon_name", "").lower()
    profile_l = sink.get("profile", "").lower()

    # Must be a real ALSA output sink (excludes virtual/loopback/combine sinks
    # and our own effect_input.* chain node).
    if not name_l.startswith("alsa_output"):
        return "excluded"
    # Not Bluetooth.
    if sink.get("api") == "bluez5" or "bluez" in name_l:
        return "excluded"
    # Not HDMI / DisplayPort / SPDIF (digital passthrough). Match on node.name
    # and icon, and also on the profile description ("Digital Stereo (HDMI)",
    # "... (IEC958)", DisplayPort/SPDIF variants) so a digital output whose
    # node.name lacks the usual hdmi/iec958 token is still excluded.
    _DIGITAL = ("hdmi", "iec958", "spdif", "s/pdif", "displayport")
    if ("hdmi" in name_l or "iec958" in name_l or "hdmi" in icon_l
            or any(m in profile_l for m in _DIGITAL)):
        return "excluded"
    # Not headphones / a headset.
    if sink.get("icon_name") in _NON_SPEAKER_ICONS:
        return "excluded"
    if "headphone" in name_l or "headset" in name_l:
        return "excluded"
    return "relaxed"


def _relaxed_sort_key(sink: dict) -> tuple:
    """Preference order for relaxed candidates (lower sorts first).

    Tie-break only — never excludes. Prefer internal buses (pci/soundwire) over
    usb/unknown, then the exact issue-#18 symptom (audio-card-analog).
    """
    bus_rank = 0 if sink.get("bus") in ("pci", "soundwire") else 1
    icon_rank = 0 if sink.get("icon_name") == "audio-card-analog" else 1
    return (bus_rank, icon_rank, sink.get("name", ""))


def select_speaker_sinks() -> dict:
    """Select internal-speaker sink(s) from PipeWire, with tier reporting.

    Returns a dict {'tier', 'selected', 'all_sinks'}:
      - tier 'strict':  one or more sinks tagged device.icon_name=audio-speakers.
      - tier 'relaxed': no strict match, but internal analog sink(s) found
                        (sorted by preference). The caller decides whether to
                        auto-apply (a unique candidate) or prompt (ambiguous).
      - tier 'none':    no candidate at all.
    'selected' and 'all_sinks' both hold full enumerated dicts (name,
    description, profile, icon_name, bus, api) so callers can both write
    autoload entries and render diagnostics. ('all_sinks' is everything seen.)
    """
    all_sinks = _enumerate_audio_sinks()
    # Single classification pass — keeps the strict/relaxed/excluded partition
    # total and mutually exclusive (no double classify, no drift between arms).
    by_tier: dict[str, list[dict]] = {"strict": [], "relaxed": [], "excluded": []}
    for s in all_sinks:
        by_tier[_classify_sink(s)].append(s)
    if by_tier["strict"]:
        return {"tier": "strict", "selected": by_tier["strict"], "all_sinks": all_sinks}
    if by_tier["relaxed"]:
        relaxed = sorted(by_tier["relaxed"], key=_relaxed_sort_key)
        return {"tier": "relaxed", "selected": relaxed, "all_sinks": all_sinks}
    return {"tier": "none", "selected": [], "all_sinks": all_sinks}


def _sink_diag_line(sink: dict, with_description: bool = True) -> str:
    """One-line diagnostic: the sink's node.name (what --autoload-sink/
    --target-sink take) plus icon/bus detail, and optionally a human
    description, to identify the device. Shared by both converters so their
    candidate/diagnostic lines stay in lockstep."""
    desc = sink.get("description") or ""
    desc_part = f'  "{desc}"' if (with_description and desc) else ""
    return (f"node.name={sink.get('name', '?')}{desc_part}  "
            f"(icon={sink.get('icon_name') or '?'}, bus={sink.get('bus') or '?'})")


def _print_sink_candidates(sinks: list[dict]) -> None:
    """Print a numbered candidate list (shared by the picker and skip paths)."""
    for i, s in enumerate(sinks, 1):
        console.cprint("dim", f"  [{i}] {_sink_diag_line(s)}")


def _prompt_pick_sink(candidates: list[dict]) -> dict | None:
    """Prompt for a 1-based choice among already-listed `candidates`, or None.

    The caller is expected to have printed the numbered candidate list. Only
    prompts when both stdin AND stdout are TTYs — piping stdout (e.g.
    ``--autoload | tee log``) would otherwise block on a prompt the user can't
    see — and treats EOF / interrupt / empty / invalid input as a skip, so
    non-interactive runs (pipes, CI, pytest) never block.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        raw = input(f"Select speaker sink [1-{len(candidates)}], "
                    "or Enter to skip: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not raw:
        return None
    try:
        idx = int(raw)
    except ValueError:
        console.cprint("warn", f"  Not a number: {raw!r} — skipping autoload.")
        return None
    if not (1 <= idx <= len(candidates)):
        console.cprint("warn", f"  Out of range: {idx} — skipping autoload.")
        return None
    return candidates[idx - 1]


def _resolve_autoload_sinks(override_names: list[str], dry_run: bool) -> list[dict]:
    """Resolve which sink(s) to write autoload entries for.

    Honors the --autoload-sink override first; otherwise runs tiered speaker
    detection (strict audio-speakers tag → relaxed internal-analog fallback)
    and prints diagnostics explaining the choice. Returns a list of sink dicts
    (with name/description/profile keys) to write, or [] to skip autoload (with
    the reason already printed to the user).
    """
    # Explicit override: bypass detection entirely.
    if override_names:
        by_name = {s["name"]: s for s in _enumerate_audio_sinks()}
        resolved = []
        for name in override_names:
            sink = by_name.get(name)
            if sink is None:
                console.cprint("warn", f"  --autoload-sink {name!r}: not currently in "
                               "pw-dump, so its output route is unknown.")
                sink = {"name": name, "description": name, "profile": "", "route": ""}
            resolved.append(sink)
        return resolved

    sel = select_speaker_sinks()
    tier = sel["tier"]

    if tier == "strict":
        return sel["selected"]

    if tier == "relaxed":
        candidates = sel["selected"]
        console.cprint("warn", "\nNo sink is tagged as an internal speaker "
                       "(device.icon_name=audio-speakers).")
        if len(candidates) == 1:
            sink = candidates[0]
            console.cprint("warn", "  Falling back to the only internal analog output found:")
            console.cprint("dim", f"    {_sink_diag_line(sink)}")
            console.cprint("dim", "  If this is wrong, re-run with --autoload-sink <node.name>.")
            return [sink]
        # Ambiguous: list, then prompt on a TTY (never under --dry-run).
        console.cprint("warn", f"  Found {len(candidates)} internal analog sinks:")
        _print_sink_candidates(candidates)
        chosen = None if dry_run else _prompt_pick_sink(candidates)
        if chosen is not None:
            return [chosen]
        console.cprint("dim", "  Re-run with --autoload-sink <node.name> (repeatable) to choose.")
        return []

    # tier == "none"
    all_sinks = sel["all_sinks"]
    if not all_sinks:
        console.cprint("warn", "\nWarning: no Audio/Sink nodes found via pw-dump; "
                       "cannot configure autoload.")
        console.cprint("dim", "  Is PipeWire running? Run this from your logged-in "
                      "desktop session.")
    else:
        console.cprint("warn", "\nWarning: no internal-speaker sink found (none tagged "
                       "device.icon_name=audio-speakers, and no internal analog "
                       "output).")
        console.cprint("head", "  Audio/Sink nodes seen:")
        _print_sink_candidates(all_sinks)
        console.cprint("dim", "  Re-run with --autoload-sink <node.name> to bind autoload manually.")
    return []


@contextlib.contextmanager
def _atomic_write(path: Path):
    """Yield a same-directory temp path, then os.replace it into place when the
    block completes — so a crash mid-write can't leave a truncated file that
    EasyEffects would silently fail to load. The dotfile temp name keeps a
    leftover from a failed write out of EE's ``*.json`` / ``*.irs`` scan. The
    single home for the temp-then-rename pattern; callers fill the temp however
    they like (text, WAV, configparser)."""
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        yield tmp
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def _atomic_write_text(path: Path, data: str) -> None:
    """Atomically write text to ``path`` (see _atomic_write)."""
    with _atomic_write(path) as tmp:
        tmp.write_text(data)


def write_autoload(autoload_dir: Path, device_name: str, device_description: str,
                   device_profile: str, preset_name: str, dry_run: bool = False) -> Path:
    """Write an EasyEffects autoload config file for a device/route → preset mapping.

    EasyEffects loads this file when the given PipeWire sink becomes the active
    output, automatically switching to the named preset.

    File is named '{device_name}:{device_profile}.json' (with '/' replaced by '_'),
    matching EasyEffects' AutoloadManager::getFilePath() convention.
    """
    safe_name = device_name.replace("/", "_")
    safe_profile = device_profile.replace("/", "_")
    path = autoload_dir / f"{safe_name}:{safe_profile}.json"
    if dry_run:
        return path
    autoload_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps({
        "device": device_name,
        "device-description": device_description,
        "device-profile": device_profile,
        "preset-name": preset_name,
    }, indent=4) + "\n")
    return path


def write_bypass_preset(output_dir: Path, preset_name: str,
                        dry_run: bool = False) -> tuple[Path, str]:
    """Write an empty bypass preset used as EasyEffects' global fallback.

    Returns (path, status) where status is "written", "kept", or "would-write".
    If a preset of the same name already exists on disk, it is preserved — the
    user may have hand-built one and we don't want to clobber it.
    """
    path = output_dir / f"{preset_name}.json"
    if path.exists():
        return path, "kept"
    if dry_run:
        return path, "would-write"
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps({
        "_generator": f"dolby_to_easyeffects.py {version.get_version()}",
        "output": {"blocklist": [], "plugins_order": []},
    }, indent=4) + "\n")
    return path, "written"


def _ee_rc_parser() -> configparser.ConfigParser:
    """A ConfigParser configured to read/write an easyeffectsrc faithfully.

    EE uses camelCase keys and INI files with no interpolation; the default
    parser would lowercase keys (EE then ignores them) and choke on '%'.
    """
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    return parser


def read_ee_rc(rc_text: str) -> dict:
    """Parse easyeffectsrc text into the fields the diagnostics care about.

    Pure (text in, dict out — no filesystem). Verified key locations against a
    live EE 8.x rc: the loaded output preset is ``[Presets]
    lastLoadedOutputPreset``; the global Fallback Preset toggle and the
    Background-Service ``autostartOnLogin`` / ``enableServiceMode`` flags are
    ``[Window]`` keys (``enableServiceMode`` is written only when toggled off —
    an absent key is the ON default); the target sink and active plugin chain
    are
    ``[StreamOutputs] outputDevice``/``plugins``. Missing sections/keys fall
    back to empty/False so callers never KeyError on a partial or older rc.
    Note there is NO global-bypass key here — that toggle is runtime/GUI only.
    """
    parser = _ee_rc_parser()
    try:
        parser.read_string(rc_text)
    except configparser.Error:
        pass  # garbage rc → all defaults below

    def g(section: str, key: str, default: str = "") -> str:
        return parser.get(section, key, fallback=default).strip()

    plugins = g("StreamOutputs", "plugins")
    return {
        "last_output_preset": g("Presets", "lastLoadedOutputPreset"),
        "fallback_preset": g("Window", "outputAutoloadingFallbackPreset"),
        # EE serialises booleans as the literal KConfig strings "true"/"false",
        # so `.lower() == "true"` is an exact-format check matching what EE 8.x
        # writes. If EE ever emitted "1"/"yes" these would read False and the
        # autoload patch would re-run on every invocation.
        "uses_fallback": g("Window", "outputAutoloadingUsesFallback",
                           "false").lower() == "true",
        "autostart_on_login": g("Window", "autostartOnLogin",
                                "false").lower() == "true",
        # enableServiceMode is written only when toggled OFF (non-default);
        # an absent key is the ON default — hence default "true" here, the
        # opposite polarity to autostartOnLogin above.
        "service_mode": g("Window", "enableServiceMode",
                          "true").lower() == "true",
        "output_device": g("StreamOutputs", "outputDevice"),
        "output_plugins": [p for p in plugins.split(",") if p],
    }


def set_autoload_fallback(rc_path: Path, preset_name: str,
                          dry_run: bool = False) -> tuple[str, str]:
    """Enable EasyEffects' global Fallback Preset toggle in its KConfig file.

    EasyEffects 8.x stores the toggle as two keys under the [Window] section
    (they're bound to QML properties attached to the main window object —
    quirky location, but matches EE's config binding). No EE CLI or D-Bus
    interface exists for this setting, so direct file edit is the only option.

    Returns (status, existing_preset) where status is one of:
      - "already-configured": both keys set and fallback enabled; file untouched.
      - "patched": file created or keys set/updated.
      - "would-patch": dry-run equivalent of "patched".
    """
    rc_text = ""
    if rc_path.exists():
        try:
            rc_text = rc_path.read_text(encoding="utf-8")
        except OSError:
            rc_text = ""

    rc = read_ee_rc(rc_text)
    existing_preset = rc["fallback_preset"]
    if rc["uses_fallback"] and existing_preset:
        return "already-configured", existing_preset

    if dry_run:
        return "would-patch", existing_preset

    parser = _ee_rc_parser()
    if rc_text:
        parser.read_string(rc_text)
    section = "Window"
    if not parser.has_section(section):
        parser.add_section(section)
    parser.set(section, "outputAutoloadingFallbackPreset", preset_name)
    parser.set(section, "outputAutoloadingUsesFallback", "true")

    rc_path.parent.mkdir(parents=True, exist_ok=True)
    with _atomic_write(rc_path) as tmp, tmp.open("w", encoding="utf-8") as f:
        parser.write(f, space_around_delimiters=False)
    return "patched", existing_preset


def easyeffects_is_running() -> bool:
    """Return True if an EasyEffects process is currently running.

    Used to warn the user that easyeffectsrc edits won't take effect until
    EE is restarted — EE reads the file on startup and writes it on exit,
    so mid-run writes get clobbered.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-x", "easyeffects"],
            capture_output=True, timeout=2,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        # OSError covers FileNotFoundError (no pgrep) and PermissionError
        # (sandboxed/SELinux hosts) — never crash a caller that only wants a
        # best-effort "is EE up?" (e.g. --doctor's fact-gathering).
        return False


# Raised all over this file and consumed by the closing block, so the record
# type is shared rather than owned (see lib/report/findings.py). Kept under
# the names the rest of this file already uses, like the ee_paths and doctor
# re-exports above.
Finding = report_findings.Finding
_print_finding_detail = report_findings._print_finding_detail


# The device-report issue form (.github/ISSUE_TEMPLATE/device-report.yml).
# There is exactly one link in the output and this is it. Everything the
# closing block asks about is device-specific, and acting on any of it needs
# what this form requires — model, --speaker-info output, the generation log.
# A second, generic /issues link used to ride the mid-run feature-gap
# warnings; once those moved into the same closing block it was simply a rival
# call to action, pointing somewhere reports arrive stripped of that context.
_REPORT_FORM_URL = (
    "https://github.com/antoinecellerier/speaker-tuning-to-easyeffects"
    "/issues/new?template=device-report.yml"
)


# --- Finding factories -----------------------------------------------------
#
# Every finding raised outside the _UNMODELED_FEATURES table is built here,
# one function each, rather than inline at its raise site. They are the single
# definition of their wording: an earlier arrangement had the strings inline
# and the contract tests restating them, which is the drift e3a7ee4 removed
# from the doctor/warning pair — two copies, edited one at a time.

def _profile_mismatch_finding(declared: str, profile_used: str) -> Finding:
    """Dolby names a different profile than the one we built."""
    # kind="ask": "tell us which sounds better" is something the project
    # needs, and hint-routing left the one ending that solicits the
    # comparison without the Help-the-project block or the attach path
    # (round 8). This is also the confirmation channel the parked
    # build-the-declared-default change waits on.
    return Finding(
        slug="profile-mismatch", kind="ask",
        # The naming note pre-empts a round-6 worry: a reviewer assumed the
        # suggested --profile re-run would overwrite the presets they were
        # told to compare against.
        detail=f"This XML names '{declared}' as the profile the device ships "
               f"on under Windows, but we built '{profile_used}' (this "
               "speaker's first-listed). A --profile re-run writes its own "
               "preset files, so both stay installed.",
        # Names the action and what it gets you. An earlier wording led with
        # "worth an A/B against Windows", which read as though the user had to
        # go and do something in Windows.
        # Names both sides. "the profile this device ships on" alone left the
        # reader unable to tell what they'd be comparing against, and reading
        # as though the tool had knowingly picked the wrong one.
        # Says why the names matter and closes the loop: "re-run to compare"
        # alone left a reviewer comparing with no idea what to do with the
        # result, and the two names connected to nothing else in the block.
        # "the Windows default", not "Windows uses": default_profile is the
        # shipping default; what the user actually ran on Windows may
        # differ.
        ask=f"We built '{profile_used}' but the Windows default is "
            f"'{declared}' — re-run with --profile {declared} and tell us "
            "which sounds better.")


def _untamed_boost_ask(coupled_bands_possible: bool) -> str:
    """The two-step ask both members of the untamed-boost family carry.

    One template for one risk family (round 8: two wordings for the same risk
    left the reader unsure which explanation to trust) — but step 2 only
    exists where `--enable coupled-bands` could actually do something. On a
    tuning with no qualifying band the flag changes nothing, the run's own
    "Optional extras" menu doesn't offer it, and a re-run answers
    "--enable coupled-bands had no effect".

    "(not both)": every compact form — "swap it for" (round 3), "instead"
    (round 5), "just" (round 8), "replace that flag with" (round 9) — kept
    reading ambiguous against the seam line's "they combine". The
    parenthetical says it outright.
    """
    if not coupled_bands_possible:
        return "If loud parts distort, re-run with --disable volmax."
    return ("If loud parts distort, re-run with --disable volmax; if "
            "still harsh, swap to --enable coupled-bands (not both).")


def _loudness_untamed_finding(coupled_bands_possible: bool = True) -> Finding:
    """Every regulator band sits at or above 0 dBFS, so nothing is tamed."""
    return Finding(
        slug="loudness-untamed",
        # Self-contained: it used to say "threshold_high above", pointing at
        # a table that only prints with -v now. The field name stays in
        # parentheses as the grep handle. No limiter noun at all (round 8):
        # "brickwall" → "final safety limiter" → "the preset's own output
        # limiter" each read as a second mystery stage; what the reader
        # needs is the consequence, phrased identically to the
        # boost-unlimited sibling — one template for one risk family.
        # No raw field name (round 9): "threshold_high" read as leaked
        # code and undercut trust. The -v table still prints the field.
        # "band by band" is load-bearing, not filler: limiter#0 ships on
        # every preset, so the bare "nothing limits it" that dropping the
        # noun left behind was false. The qualifier keeps the sentence
        # true without reintroducing a stage the reader has to look up.
        detail="This tuning's regulator never engages — every band's "
               "limit sits at or above full volume — so nothing trims "
               "the loudness boost band by band on its way out.",
        # Same two-step ask as boost-unlimited — one template for one risk
        # family (round 9); coupled-bands is exactly the all-inert class's
        # remedy (issue #27), where it qualifies.
        ask=_untamed_boost_ask(coupled_bands_possible))


def _boost_unlimited_finding(peak_db: float, freq,
                             coupled_bands_possible: bool = True,
                             restored: bool = False) -> Finding:
    """The band carrying the largest boost is one the regulator leaves free."""
    # Name everything riding on that band, not just volmax: under
    # --enable level-restore the peak itself is added back as gain, so
    # "with the volmax boost on top" would describe half the drive. The
    # clause stays one phrase either way — this is a detail line, and the
    # flag's own menu entry carries the "may distort" caveat.
    on_top = ("the volmax boost and the restored level on top" if restored
              else "the volmax boost on top")
    return Finding(
        slug="boost-unlimited",
        # Same closing formula as loudness-untamed — one template for one
        # risk family (round 8: two wordings for the same risk left the
        # reader unsure which explanation to trust).
        detail=f"The biggest correction boost ({peak_db:+.1f} dB at {freq} Hz) "
               f"lands on a band the regulator leaves unlimited, with "
               f"{on_top} — nothing trims it band by band on its "
               "way out.",
        # Sequenced, and step 2 speaks the menu's symptom family for
        # coupled-bands (harshness) instead of inventing its own: with
        # "if they still distort" the same screen sold the flag for
        # distortion while the menu sold it for harshness (rounds 3 and 5
        # — one heard symptom per flag). Not "loud music": the vocabulary
        # trap reserves "music" for the mbc symptom. No region word — the
        # unlimited band's frequency is device-specific and the detail
        # above already names it. Wording and the "(not both)" rationale
        # live in _untamed_boost_ask, shared with loudness-untamed.
        ask=_untamed_boost_ask(coupled_bands_possible))


def _experimental_finding(named: str, flags: list[str]) -> Finding:
    """Emission paths reproduced from the XML but never confirmed by ear.

    The ask has to say what to listen for and how to compare, because the
    reader has no reference: they have never heard this laptop tuned
    correctly, so "does it sound right?" is unanswerable on its own. Naming
    the --disable flag turns it into an A/B they can actually run.
    """
    if len(flags) == 1:
        ask = (f"Re-run with --disable {flags[0]} and tell us which version "
               "sounded better.")
    else:
        ask = "Tell us whether it sounds right — either answer helps."
    # Not slug="experimental": the --enable menu describes coupled-bands as
    # "experimental (issue #44)" on the same screen, so a report quoting
    # "[experimental]" could mean either. The slug states the situation the
    # detail describes; the menus keep "experimental" as an adjective.
    return Finding(
        slug="unconfirmed-by-ear", kind="ask",
        detail=f"Built from your tuning but never confirmed by ear: {named}. "
               "These come straight out of the Dolby file and the numbers "
               "check out, but nobody with a device that uses them has told "
               "us how they sound.",
        ask=ask)


def _firmware_gate_finding() -> Finding:
    """Whether toggling the smart-amp gate actually restored the bass."""
    return Finding(
        slug="firmware-gate", kind="ask",
        detail="Smart-amp firmware gate is off — see the procedure above.",
        ask="Did toggling the smart-amp control change how it sounds? "
            "(issue #17)")


def _leveler_gap_finding(substages: list[str], autogain_on: bool,
                              autogain_available: bool = True,
                              disabled_by_flag: bool = False) -> Finding | None:
    """The Dolby leveler companion stages this converter cannot reproduce.

    Unlike every other mapping these carry no parameters at all — the schema
    has an on/off bit and nothing else, no threshold, ratio, attack or release
    in either tuning block — so no stage can be derived from them, and
    inventing one is the per-device hand-tuning the XML-only rule forbids.

    Two strengths. Where the leveler ships bypassed (HDA default) the
    companions cannot be heard and there is nothing for anyone to do: detail
    only, no ask. Where the leveler runs (SoundWire default, or ``--enable
    autogain``) it runs without the compressor Dolby pairs with it — a
    plausible cause of exactly the pumping that state gets blamed for — so
    that case asks for the one capture that could settle it, and names
    ``--disable autogain`` as the off-switch.

    "May be part of it", not "the most likely reason": the measured driver of
    quiet-swell/loud-duck is EE's own non-content-aware autogain (design-notes,
    "Why autogain is bypassed by default"), and the corpus doc records that the
    companion compressor does not explain the issue-#25 overshoot — neither
    device carrying it. The copy had promoted this docstring's own hedge.

    Every user-review round misread this copy until it said where the
    leveler itself stands: the parsed-XML block above prints the leveler's
    own amount/targets, so "cannot reproduce" without an owner read as the
    converter contradicting itself about the leveler.
    """
    if not substages:
        return None
    named = ", ".join(substages)
    # Plain words first, raw names in parentheses (round 7): the raw
    # volume-leveler-drc/-compressor tokens were the one list without the
    # friendly-name treatment every other stage gets.
    head = ("Also in your tuning but not rebuilt: companion compression "
            f"stages Dolby pairs with its volume leveler ({named}). "
            "Harmless as built: ")
    if not autogain_on:
        # Only point at --enable autogain when it could actually change this.
        # On a tuning whose XML disables the leveler outright the flag does
        # nothing, and suggesting it contradicts the "had no effect" warning
        # printed just above.
        #
        # --disable autogain also clears the marker, so without its own
        # branch this blamed the tuning for the reader's own flag — while
        # the leveler section a few lines up correctly credited the flag.
        if disabled_by_flag:
            tail = ("--disable autogain switched the leveler off in this "
                    "preset, so they cannot be heard.")
        elif autogain_available:
            tail = ("the leveler ships switched off in this preset, so they "
                    "cannot be heard — this only matters if you rebuild with "
                    "--enable autogain.")
        else:
            tail = ("your tuning switches the leveler off outright, so they "
                    "cannot be heard and no flag here changes that.")
        return Finding(slug="leveler-gap", kind="ask", detail=head + tail)
    return Finding(
        slug="leveler-gap", kind="ask",
        detail="The volume leveler itself is rebuilt and running in this "
               "preset. But your tuning pairs it with companion "
               f"compression stage(s) ({named}) this converter cannot "
               "rebuild: the tuning file "
               "records only that they are switched on, not how they are "
               "set. If quiet passages swell then duck when things get "
               "loud, that gap may be part of it (--disable "
               "autogain switches the "
               "leveler off). Settling it needs a capture from a Windows "
               "install with Dolby on this same machine — a few minutes "
               "of scripted recording; if you dual-boot, we'll walk you "
               "through it.",
        # Deliberately does not ask them to go and do the capture, and does
        # not point at the measure_dax README: two rounds of reviewers read
        # the self-serve route as homework that gates help and said they'd
        # give up there — the ask below owns the route ("tell us, we'll
        # walk you through it"), and the procedure link belongs in that
        # conversation. It is a multi-step measurement on a second OS, and
        # most people run this script once. The walk-you-through offer also
        # rides the detail (round 4: read top-down, "settling it needs a
        # capture" arrived 40 lines before the offer and read as an
        # unexplained requirement).
        # Names Windows so anyone who doesn't dual-boot can skip the line
        # rather than reading to the end to find out they can't help — the
        # capture measures what DAX does, so it has to run there.
        # Vocabulary is the autogain row's ("swell then duck"), NOT the
        # regulator's "wobbles or surges" — a round-2 reviewer hearing
        # volume movement couldn't tell which of the two remedies to try
        # because both claimed "surges".
        ask="If quiet passages swell then duck, tell us — a Windows "
            "capture would settle it and we'll walk you through it.")


def _print_ask(style: str, finding: Finding) -> None:
    """One bullet: the sentence first, then the slug, dimmed.

    The slug trails because a first-time reader needs the sentence, not the
    tag — it only matters once they want to scroll back to the detail it was
    raised with, so it should not be the first thing the eye lands on. Dim for
    the same reason.

    Two styles on one line means assembling spans rather than handing cprint a
    string: the console runs with markup off, so bracket syntax in the text is
    literal (which is what keeps ``[slug]`` printable at all). Spans sidestep
    that entirely — nothing is parsed out of the message.
    """
    # Scope rides in the tag, not the sentence: it is bookkeeping, and the
    # sentence has a one-line budget to keep. Silent when the finding applies
    # everywhere, which on a default single-profile run is always — so the
    # common case pays nothing for it.
    tag = (f"[{finding.slug} · {finding.scope}]" if finding.scope
           else f"[{finding.slug}]")
    lines = textwrap.wrap(f"  • {finding.ask}  {tag}", width=console._wrap_width(),
                          subsequent_indent="    ", break_on_hyphens=False)
    for line in lines:
        if console._CONSOLE is not None and line.endswith(tag):
            # Imported here, not beside the console: this is rich's only
            # caller outside cprint, and a live console already proves the
            # import succeeds. Nothing else needs the two-style path.
            from rich.text import Text
            span = Text()
            span.append(line[:-len(tag)], style=style)
            span.append(tag, style="dim")
            console._CONSOLE.print(span, soft_wrap=True)
        else:
            console.cprint(style, line)


def _print_attach_lines(xml_path) -> None:
    """The what-to-send lines, shared by both closing branches.

    cta, not dim: this is the one concrete task the report needs, and it
    printed fainter than the reassurance bullet above it (round-2 color
    finding). "If you report", the intro line's vocabulary: unconditional
    "attach this to your report" left a round-4 reviewer unsure whether
    filing was mandatory. Download link preferred over attaching: a
    driver-package link identifies the exact tuning build and carries
    every sibling XML for the device; "(if you know it)" because a reader
    who found the file on their Windows partition has no download to link
    (round 8).
    """
    if xml_path is None:
        return
    print()
    console._cprint_wrapped("cta", "  If you report, best is a link to "
                           "your device's audio-driver download "
                           "(if you know it) — or just attach the "
                           "XML file:",
                    indent="  ")
    # Absolute and quoted. Dolby's own directory names contain '$'
    # (…/code$GetExtractPath$/…), so an unquoted relative path is
    # eaten by the shell the moment anyone types ls on it and the
    # file looks missing. Same cta as its instruction — the copy
    # target must not be the faintest line in the block.
    console.cprint("cta", f"    '{Path(xml_path).resolve()}'")


def print_project_asks(findings: list[Finding], dry_run: bool = False,
                       xml_path=None, pipewire_native: bool = False) -> None:
    """Print the closing block: what the project needs, then the one ask.

    Always prints. Most people run this script once, on one machine, and
    never again, so whatever we want from them we get on this run or not at
    all — there is no next run to defer to. On a clean run that means three
    lines and no header; a rule and a heading over a bare "how does it sound"
    would be noise on the common path.

    Specifics first and the link last, so the bullets make the case for why
    this particular run is worth reporting and the URL is what is still on
    screen when the run ends.

    ``dry_run`` swaps the closing line, because nothing was installed and
    "how does it sound?" is then an impossible instruction — the announcement
    that this was a dry run is hundreds of lines up by the time anyone reads
    the end, so the last thing on screen has to carry it too.
    """
    asks = [f for f in findings if f.kind == "ask" and f.ask]
    # Every tag shown this run, not just the ones with an ask. A hint like
    # [loudness-untamed] is often the only finding that actually fired for
    # the device, and listing only asks under "quote the tag in brackets"
    # sent reporters to quote the speculative one and never mention it.
    tagged = [f for f in findings if f.slug]
    print()
    if asks:
        console.cprint("head", "=" * 60)
        console.cprint("head", "Help the project")
        print()
        # Say what the bracketed tags are for. Read cold they look like debug
        # labels that leaked out of the code, which is how they get ignored.
        # "these most of all" introduced a list that is usually one item
        # long, and its "these" pointed backwards at nothing on a top-down
        # read.
        console._cprint_wrapped("dim", "Some of this only a real device can answer. "
                               "If you report, quote the [tag] so we know "
                               "which line you mean:")
        # Plain, not cta: bold-magenta bullets read as warnings — a round-4
        # reviewer took the peak-level reassurance ("should sound right")
        # for something being wrong, because it matched the report call's
        # color. The hierarchy is dim intro → plain bullets → cta
        # instructions (attach line, final call), so the calls to action
        # still print brighter than the specifics (round-2 rule).
        for finding in asks:
            _print_ask("", finding)
        # The tool found the tuning XML; the user never went looking for it,
        # so an ask to "send us your tuning XML" is unactionable without the
        # path. Printed once here rather than inside each bullet, which the
        # one-sentence budget has no room for.
        # For every ask, not just ones whose wording mentions the XML
        # (round 6): the file helps triage whatever the report is about,
        # and the old wording-sniffing gate was one rewording away from
        # silently switching the path off.
        _print_attach_lines(xml_path)
        print()
    elif tagged:
        # No ask fired, but something upstream still carries a tag. Say it is
        # worth quoting, or the reader is left holding a bracketed token with
        # no reason to think it means anything to us. The attach lines print
        # here too (round 10, user-picked): a run whose only findings are ⚠
        # warnings is exactly one the project wants the tuning source for,
        # and this branch used to leave its reporter with nothing to attach.
        console.cprint("head", "=" * 60)
        console._cprint_wrapped("dim", "Saw a [tag] above? Quote it if you report — "
                               "it tells us which finding you mean.")
        _print_attach_lines(xml_path)
        print()

    # Stages this tuning has that we drop. They carry no ask, because there is
    # nothing anyone can do about them — but they printed two hundred lines
    # up and never again, so the closing block read as the whole story when a
    # piece of the tuning was missing from it. One line, no bullet list: it is
    # context for a report, not another thing to action.
    dropped = [f.slug for f in findings if f.kind == "ask" and not f.ask]
    if dropped:
        # Not "Not reproduced on this device" — reviewers read that as
        # issue-tracker language ("we couldn't reproduce your bug"), the
        # opposite of what it says. And the mention needs a reason, or it is
        # a nothing-to-do entry that teaches readers to skip the block.
        console._cprint_wrapped("dim", "Parts of your tuning this converter doesn't "
                               "rebuild: "
                               + ", ".join(f"[{s}]" for s in dropped)
                               + " — nothing you need to do, but mention "
                                 "them if you report so we know which "
                                 "devices have them.")
        print()
    # The link prints either way. Suppressing it on a dry run left the block
    # above saying "quote the tag in brackets if you report one" with nowhere
    # to report to — worse than the impossible "how does it sound?" it was
    # meant to fix, because that at least named a destination.
    # For the wrapper's reader the repo name says "easyeffects" — the very
    # thing they chose this path to avoid — and the only link in the run
    # points there, so one clause says their report belongs here too.
    if dry_run:
        # Just the pointer. That nothing was written is said immediately
        # above by whoever ran the dry run — print_what_now here, the [3/3]
        # banner under dolby_to_pipewire.py — and saying it twice in
        # consecutive sentences reads like a stutter.
        lead = ("Reporting anything above? PipeWire-only reports are "
                "welcome — here's where:" if pipewire_native else
                "Reporting anything above? Here's where:")
    else:
        lead = ("How does it sound? Please report back — good or bad, or "
                "if you need help"
                + (" (PipeWire-only reports are welcome)"
                   if pipewire_native else "") + ":")
    console._cprint_wrapped("cta", lead)
    # The URL gets its own line and is never wrapped: broken across lines it
    # can't be clicked or copied, which defeats the whole point of the ask.
    console.cprint("cta", f"  {_REPORT_FORM_URL}")


# --- FIR generation ---
#
# make_fir and friends are in lib/preset/fir.py. This one stayed: it writes
# through _atomic_write, which four other callers here share and which has no
# home in lib/ yet, and a move commit may not re-point a body it carries.


def save_wav_stereo(path: Path, fir_left: np.ndarray,
                    fir_right: np.ndarray) -> None:
    """Save stereo impulse response as 32-bit float WAV."""
    stereo = np.column_stack([fir_left, fir_right]).astype(np.float32)
    with _atomic_write(path) as tmp:
        wavfile.write(str(tmp), fir.SAMPLE_RATE, stereo)


# --- EasyEffects preset builders ---

def _eq_band(*, frequency, gain, q, slope, lsp_type) -> dict:
    """One EQ band in EE PEQ schema order. ``mode``/``mute``/``solo``/``width``
    are EE-schema fillers (topology, not tuning) — defined once here so a future
    EE-schema tweak lands in a single place. The per-band builders below pass
    only the values that differ (frequency/gain/q/slope/type)."""
    return {
        "frequency": frequency,
        "gain": gain,
        "mode": "RLC (BT)",
        "mute": False,
        "q": q,
        "slope": slope,
        "solo": False,
        "type": lsp_type,
        "width": 4.0,
    }


def make_band(freq: float, gain: float, q=1.5) -> dict:
    return _eq_band(
        frequency=freq, gain=round(gain, 4), q=q, slope="x1", lsp_type="Bell"
    )


def make_convolver(kernel_name: str) -> dict:
    """Convolver plugin config referencing an IR by name.

    EasyEffects 8.x uses kernel-name (filename stem without extension),
    and looks for the WAV in its irs/ directory.
    """
    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "kernel-name": kernel_name,
        "ir-width": 100,
        "autogain": False,
    }


# Dolby HP/LP ``order=N`` → LSP user-facing slope ``x{N/2}`` (LSP internally
# doubles the slope so nSlope equals the filter order; see make_hp_band).
_ORDER_TO_LSP_SLOPE = {2: "x1", 4: "x2", 6: "x3", 8: "x4"}


def _make_passfilter(freq: float, order: int, lsp_type: str) -> dict:
    """Shared HP/LP pass-filter band. ``lsp_type`` selects the LSP ``type``
    label ("Hi-pass"/"Lo-pass"); the rest is identical between directions
    (see make_hp_band / make_lp_band for the slope-doubling rationale)."""
    return _eq_band(
        frequency=freq,
        gain=0.0,
        q=0.707,
        slope=_ORDER_TO_LSP_SLOPE.get(order, "x4"),
        lsp_type=lsp_type,
    )


def make_hp_band(freq: float, order: int) -> dict:
    """High-pass filter band for speaker protection.

    Dolby's ``order=N`` declares an N-th-order high-pass. LSP's
    ``RLC (BT)`` HP user-facing slope ``x1..x4`` is *internally doubled*
    to ``nSlope=2,4,6,8`` (that's literally ``*slope = 2 * *slope`` in
    ``para_equalizer.cpp:167``), and ``calc_rlc_filter`` then builds
    ``nSlope/2`` cascaded 2nd-order sections at the user-Q — so internal
    ``nSlope`` equals filter order. So Dolby ``order=N`` maps to LSP
    user-facing slope ``x{N/2}`` (see ``_ORDER_TO_LSP_SLOPE``). Corpus has
    order ∈ {2, 4, 8}.
    """
    return _make_passfilter(freq, order, "Hi-pass")


def _shelf_q_from_s(gain: float, s: float) -> float:
    """Standard audio S-to-Q conversion for shelving filters.

    Q = 1/sqrt((A + 1/A) * (1/S - 1) + 2) where A = 10^(gain/40).
    For S=1.0 this simplifies to Q ≈ 0.707 (Butterworth). The
    (A + 1/A) term is symmetric in A↔1/A, so the sign of gain does
    not affect Q — and the formula is also symmetric between
    low-shelf and high-shelf variants.
    """
    a = 10 ** (gain / 40.0) if gain != 0 else 1.0
    denom = (a + 1.0 / a) * (1.0 / s - 1.0) + 2.0
    return 1.0 / math.sqrt(max(denom, 0.01))


def _make_shelf(freq: float, gain: float, s: float, lsp_type: str) -> dict:
    """Shared low/high-shelf band. ``lsp_type`` selects the LSP ``type``
    label ("Lo-shelf"/"Hi-shelf"); the Q-from-S derivation is identical in
    both directions (``_shelf_q_from_s`` is symmetric in shelf direction)."""
    return _eq_band(
        frequency=freq,
        gain=round(gain, 4),
        q=round(_shelf_q_from_s(gain, s), 4),
        slope="x1",
        lsp_type=lsp_type,
    )


def make_shelf_band(freq: float, gain: float, s: float = 1.0) -> dict:
    """Low-shelf filter band from Dolby PEQ type 4."""
    return _make_shelf(freq, gain, s, "Lo-shelf")


def make_hishelf_band(freq: float, gain: float, s: float = 1.0) -> dict:
    """High-shelf filter band from Dolby PEQ type 3.

    Mirror of make_shelf_band with LSP's "Hi-shelf" mode. Same Q-from-S
    derivation — the formula is symmetric in shelf direction. Corpus
    gains are strictly non-negative (0 to +15 dB) across the 1754
    type-3 filters observed, typically a +2-5 dB presence lift around
    2.7 kHz. Experimental path — not yet audibly validated.
    """
    return _make_shelf(freq, gain, s, "Hi-shelf")


def make_lp_band(freq: float, order: int) -> dict:
    """Low-pass filter band from Dolby PEQ types 6 and 8.

    Mirror of make_hp_band with LSP's "Lo-pass" mode — same LSP slope
    doubling convention (see make_hp_band docstring), so order N maps
    to slope ``x{N/2}`` via ``_ORDER_TO_LSP_SLOPE``. Rare: a few hundred LP
    filters across the corpus, mostly order=8 tweeter-guard rolloff.
    Experimental path — not yet audibly validated.
    """
    return _make_passfilter(freq, order, "Lo-pass")


def make_peq_eq(peq_filters: list[dict]) -> dict | None:
    """Parametric EQ for the explicit speaker PEQ from Dolby.

    Handles filter types: 1 (bell), 4 (low-shelf), 7/9 (high-pass),
    3 (high-shelf, experimental), 6/8 (low-pass, experimental). The HP
    protects laptop speakers from sub-bass energy they can't reproduce;
    the LP is a tweeter-guard rolloff seen on a handful of ALC274 SKUs.
    """
    bells_l = [f for f in peq_filters if f["speaker"] == 0 and f["type"] == 1]
    bells_r = [f for f in peq_filters if f["speaker"] == 1 and f["type"] == 1]
    hp_l = [f for f in peq_filters if f["speaker"] == 0 and f["type"] in (7, 9)]
    hp_r = [f for f in peq_filters if f["speaker"] == 1 and f["type"] in (7, 9)]
    lp_l = [f for f in peq_filters if f["speaker"] == 0 and f["type"] in (6, 8)]
    lp_r = [f for f in peq_filters if f["speaker"] == 1 and f["type"] in (6, 8)]
    loshelf_l = [f for f in peq_filters if f["speaker"] == 0 and f["type"] == 4]
    loshelf_r = [f for f in peq_filters if f["speaker"] == 1 and f["type"] == 4]
    hishelf_l = [f for f in peq_filters if f["speaker"] == 0 and f["type"] == 3]
    hishelf_r = [f for f in peq_filters if f["speaker"] == 1 and f["type"] == 3]

    num_bells = max(len(bells_l), len(bells_r))
    num_hp = max(len(hp_l), len(hp_r))
    num_lp = max(len(lp_l), len(lp_r))
    num_loshelf = max(len(loshelf_l), len(loshelf_r))
    num_hishelf = max(len(hishelf_l), len(hishelf_r))
    num_bands = num_hp + num_lp + num_loshelf + num_hishelf + num_bells

    if num_bands == 0:
        return None

    left_bands = {}
    right_bands = {}

    def place(bucket_l, bucket_r, builder, off):
        for j, pf in enumerate(bucket_l):
            left_bands[f"band{off + j}"] = builder(pf)
        for j, pf in enumerate(bucket_r):
            right_bands[f"band{off + j}"] = builder(pf)

    off = 0
    place(hp_l, hp_r, lambda pf: make_hp_band(pf["f0"], pf["order"]), off)
    off += num_hp
    place(lp_l, lp_r, lambda pf: make_lp_band(pf["f0"], pf["order"]), off)
    off += num_lp
    place(loshelf_l, loshelf_r,
          lambda pf: make_shelf_band(pf["f0"], pf["gain"], pf["s"]), off)
    off += num_loshelf
    place(hishelf_l, hishelf_r,
          lambda pf: make_hishelf_band(pf["f0"], pf["gain"], pf["s"]), off)
    off += num_hishelf
    place(bells_l, bells_r,
          lambda pf: make_band(pf["f0"], pf["gain"], q=pf["q"]), off)

    # Fill missing bands on whichever channel is shorter. Each slot keeps
    # its filter category so the channels stay topologically matched.
    fillers = []
    for _ in range(num_hp):
        fillers.append(lambda: make_hp_band(100.0, 4))
    for _ in range(num_lp):
        fillers.append(lambda: make_lp_band(20000.0, 4))
    for _ in range(num_loshelf):
        fillers.append(lambda: make_shelf_band(100.0, 0.0))
    for _ in range(num_hishelf):
        fillers.append(lambda: make_hishelf_band(10000.0, 0.0))
    for _ in range(num_bells):
        fillers.append(lambda: make_band(1000.0, 0.0))
    for idx in range(num_bands):
        key = f"band{idx}"
        if key not in left_bands:
            left_bands[key] = fillers[idx]()
        if key not in right_bands:
            right_bands[key] = fillers[idx]()

    # Compensate for PEQ boost to prevent clipping. Bells are scaled by
    # bandwidth: a narrow Q=4.6 bell at +4 dB barely raises broadband
    # level, while a wide Q=0.7 bell at +4 dB raises it nearly 4 dB
    # (effective boost ≈ gain * min(1, 2/Q)). Shelves (both low- and
    # high-shelf) contribute their full gain because they raise an entire
    # half-band above/below the corner. HP/LP filters are cut-only and
    # reduce headroom, so they don't enter the compensation sum.
    effective_boosts = []
    for pf in bells_l + bells_r:
        if pf["gain"] <= 0:
            continue
        q = pf.get("q", 1.0)
        effective_boosts.append(pf["gain"] * min(1.0, 2.0 / q))
    for pf in loshelf_l + loshelf_r + hishelf_l + hishelf_r:
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


# NOTE: there is deliberately no surround→stereo-widening builder. Earlier
# revisions mapped `surround-boost` to a Calf Stereo Tools `stereo-base`
# widening (commit 82d7f3d). A 2026-06-13 DAX capture on the X1 Yoga
# falsified that mapping: on 2-channel content DAX applies *zero* stereo
# widening — `surround-boost=96` (movie) is identical to `surround-boost=0`
# (game) to 0.01 dB RMS in both L and R, and leaves the L/R correlation
# untouched (no magnitude M/S rebalance, no phase decorrelation). The field
# is a virtualization/surround-render depth control that is dormant without
# a multichannel/object bed, not a stereo-width knob — so the faithful
# stereo-playback behaviour is to not widen. See docs/design-notes.md,
# unvalidated-scaling entry 2. (ee_to_pipewire.py keeps `emit_stereo_tools`
# as a translator for any preset that still carries a stereo_tools block.)


def make_dialog_enhancer(dialog_enhancer: dict | None) -> dict | None:
    """Dialog enhancer mapped as a broad speech-band EQ boost.

    Dolby's dialog enhancer (DE) isolates speech frequencies and
    selectively boosts them. We approximate this with a broad Bell
    filter centered at 2.5 kHz (speech presence region), with gain
    scaled by the DE amount (0-16 scale): amount/16 * 6 dB, giving a
    maximum of +6 dB.

    (An earlier SoundWire-only variant used a stronger *8 mapping plus
    a 4 kHz "clarity" bell — removed: it was calibrated against the
    pre-#13 chain whose over-applied IEQ crushed the treble it was
    compensating; see design-notes unvalidated-scaling entry 1.)
    """
    if not dialog_enhancer:
        return None

    amount = dialog_enhancer["amount"]

    gain = round(amount / parse.DB_FIXED_POINT_SCALE * 6.0, 2)
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


def make_autogain(vol_leveler: dict | None,
                  conservative: bool = False,
                  enabled: bool = False) -> dict | None:
    """Autogain plugin mapping from Dolby volume leveler.

    The Dolby volume leveler brings quiet passages up to a target loudness.
    EasyEffects' autogain does the same using EBU R 128 loudness measurement.

    Dolby volume-leveler-amount (0-10) maps to aggressiveness:
      0 = gentle (long history window)
      10 = aggressive (short history window)

    For HDA presets: bypassed by default. EE's leveler has no equivalent
    of Dolby's MI steering: it boosts legitimate quiet content (a low
    background under intermittent speech, ~+14 dB measured) and each loud
    onset then rides ~4 dB of overshoot into the downstream dynamics —
    audible saturation, measured independent of `maximum-history`
    (design-notes). `--enable autogain` (enabled=True) opts in for the
    ~+9 dB program loudness it brings (issue #25). Either way the silence
    gate ships at -50 dB — the #25 field-confirmed fix for crackle on
    short sounds arriving after silence — so manual GUI enabling is safe.

    For SoundWire presets (conservative=True): active with gentler
    settings — a -6 dB target offset and a longer history window.
    """
    if not vol_leveler or not vol_leveler["enable"]:
        return None

    amount = vol_leveler["amount"]
    target = vol_leveler["out_target"]

    if conservative:
        max_history = max(40 - amount * 4, 15)
        target -= 6.0
    else:
        max_history = max(30 - amount * 5, 10)
    return {
        "bypass": not (conservative or enabled),
        "input-gain": 0.0,
        "output-gain": 0.0,
        "maximum-history": max_history,
        "reference": "Geometric Mean (MSI)",
        "silence-threshold": -50.0,
        "target": round(target, 1),
    }


# Dolby DSP coefficients (MBC gain/attack/release) are Q15 fixed point:
# the stored int divided by 2^15 gives the fractional value; 2^15 is "unity".
Q15_SCALE = 32768.0
# The Dolby MB compressor operates per block of this many samples (not per
# sample), so time-constant decoding converts via blocks-per-second.
MBC_BLOCK_SIZE = 256


def decode_mbc_time_constant(coeff: int, block_size: int = MBC_BLOCK_SIZE) -> float:
    """Decode a Dolby time constant coefficient to milliseconds.

    Dolby stores time constants as exponential smoothing coefficients
    in Q15 fixed-point format, operating per block (not per sample).
    coeff/32768 = (1 - alpha), where alpha = 1 - exp(-1/(tau * blocks_per_sec)).
    """
    blocks_per_sec = fir.SAMPLE_RATE / block_size
    one_minus_alpha = coeff / Q15_SCALE
    if one_minus_alpha <= 0.0 or one_minus_alpha >= 1.0:
        return 100.0  # fallback
    tau = -1.0 / (blocks_per_sec * math.log(one_minus_alpha))
    return tau * 1000.0  # seconds to ms


# LSP MBC/limiter release-threshold floor: parked just under -80 dB so the
# release stage effectively never re-triggers. Shared by the disabled and
# active band builders.
MBC_RELEASE_THRESHOLD_FLOOR = -80.01


def _disabled_band() -> dict:
    """The LSP 'band off' parameter dict, shared by make_multiband_compressor
    and make_regulator (the literal was byte-identical in both).

    Key order and the trap-fix values are load-bearing: the preset JSON
    preserves insertion order, and design-notes track compression-mode
    "Downward" (over LSP's "Upward" default), boost-amount 0.0, and
    enable-band False as the LSP defaults that must be explicitly overridden.
    Returns a fresh dict each call so each band gets its own object.
    """
    return {
        "enable-band": False,
        "compressor-enable": False,
        "mute": False,
        "solo": False,
        "attack-threshold": -12.0,
        "attack-time": 20.0,
        "release-threshold": MBC_RELEASE_THRESHOLD_FLOOR,
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


def decode_mbc_bands(mb_comp: dict | None) -> list[dict]:
    """Decode Dolby mb-compressor band_groups into per-band dynamics dicts.

    Single source of truth for the MBC band decode: both
    ``make_multiband_compressor`` (the LSP builder) and the main()
    diagnostics printer (`_report_parsed_profile`) call this so they can
    never drift. Returns a list of dicts, one per emitted band, each with
    keys: ``xover_idx``, ``threshold`` (dB), ``ratio`` (x:1),
    ``attack_ms``, ``release_ms``, ``makeup`` (dB).

    PURE — no printing or warnings. The R5 out-of-range fallback warnings
    (ratio clamp, attack/release Q15-range fallbacks) are emitted by the
    builder only, so they fire exactly once per band per run (this decode
    is also called by the silent diagnostics path). Out-of-range values
    are still *handled* here (ratio clamps to 100.0, time constants fall
    back via ``decode_mbc_time_constant``) so the returned values match
    what the builder emits — the builder just additionally warns.

    Band selection mirrors the builder: at most ``group_count`` bands,
    capped by the number of band_groups parsed and LSP's 8-band limit.
    Returns ``[]`` when there is nothing to decode.
    """
    if not mb_comp:
        return []

    band_groups = mb_comp["band_groups"]
    n_bands = min(mb_comp["group_count"], len(band_groups), 8)
    if n_bands < 1:
        return []

    decoded = []
    for bg in band_groups[:n_bands]:
        xover_idx, thresh_raw, gain_raw, attack_raw, release_raw, makeup_raw = bg
        threshold = thresh_raw / parse.DB_FIXED_POINT_SCALE
        # gain_coeff → ratio: 32767 = 1:1 (bypass), lower = more compression
        gain_frac = gain_raw / Q15_SCALE
        # out-of-range gain → clamp to practical max (builder warns)
        ratio = 1.0 / gain_frac if gain_frac > 0.01 else 100.0
        attack_ms = decode_mbc_time_constant(attack_raw)
        release_ms = decode_mbc_time_constant(release_raw)
        makeup = makeup_raw / parse.DB_FIXED_POINT_SCALE
        decoded.append({
            "xover_idx": xover_idx,
            "threshold": threshold,
            "ratio": ratio,
            "attack_ms": attack_ms,
            "release_ms": release_ms,
            "makeup": makeup,
        })
    return decoded


def make_multiband_compressor(mb_comp: dict | None,
                              freqs: list[int]) -> dict | None:
    """Multi-band compressor mapping from Dolby mb-compressor-tuning.

    The Dolby MB compressor uses raw DSP coefficients in 6-tuples:
      [crossover_band_idx, threshold_q4, gain_coeff_q15,
       attack_coeff_q15, release_coeff_q15, makeup_q4]

    Where:
      - crossover_band_idx: index into the 20-band frequency table.
        For each band i, this is the *upper* edge of that band; the
        last band's value is a sentinel (typically len(freqs) = 20)
        meaning "up to Nyquist".
      - threshold: in 1/16 dB
      - gain_coeff: Q15 fixed-point, 32767 = unity (bypass)
        ratio ≈ 1 / (gain_coeff / 32768)
      - attack/release: exponential smoothing coefficients (block-rate)
      - makeup: in 1/16 dB

    Corpus composition (1050-XML cohort, MBC-enabled rows): 1 band on
    294 profiles (music-dominated, fast attack/release used as a
    loudness maximiser with full-band ratio up to 2:1), 2 bands on
    561, 3 on 175, 4 on 121. LSP MBC supports 8 bands max, so any
    value above that would be clipped — but Dolby's schema only
    allocates 4 band_group_N elements. For group_count=1 the single
    band covers the whole spectrum (no split frequency); bands 1-7
    in the emitted config stay disabled via enable-band=False.
    """
    if not mb_comp:
        return None

    decoded = decode_mbc_bands(mb_comp)
    n_bands = len(decoded)
    if n_bands < 1:
        return None
    band_groups = mb_comp["band_groups"]

    # R5 fallback warnings about the EMITTED dynamics. decode_mbc_bands is
    # pure/silent (it is also called by the main() diagnostics, which must
    # not re-warn), so the warnings live here in the builder path only —
    # firing exactly once per affected band per run. Walk the decoded bands
    # alongside their raw band_groups to inspect the original coefficients.
    for i, (b, bg) in enumerate(zip(decoded, band_groups[:n_bands])):
        _, _, gain_raw, attack_raw, release_raw, _ = bg
        if not gain_raw / Q15_SCALE > 0.01:
            console.warn(f"MBC band {i} gain coeff {gain_raw} "
                 f"out of range — clamping ratio to {b['ratio']:.0f}:1")
        if not 0 < attack_raw < Q15_SCALE:
            console.warn(f"MBC band {i} attack coeff {attack_raw} "
                 f"out of range — using {b['attack_ms']:.0f} ms fallback")
        if not 0 < release_raw < Q15_SCALE:
            console.warn(f"MBC band {i} release coeff {release_raw} "
                 f"out of range — using {b['release_ms']:.0f} ms fallback")

    # Crossovers between adjacent bands. Band i ends at freqs[decoded[i].xover_idx];
    # band i+1's lower edge is the same frequency. Only the first n_bands - 1
    # crossovers are meaningful — the last band's xover_idx is the high-cap
    # sentinel and isn't used as a split point.
    def xover_to_freq(idx, fallback):
        if 0 <= idx < len(freqs):
            return float(freqs[idx])
        return fallback

    crossovers = [xover_to_freq(decoded[i]["xover_idx"], 500.0)
                  for i in range(n_bands - 1)]

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
        if i < n_bands:
            b = decoded[i]
            # Band i sits between its lower edge (crossovers[i-1] for i>0,
            # else 0/DC) and its upper edge (crossovers[i] for i<n_bands-1,
            # else 20 kHz Nyquist).
            lower = crossovers[i - 1] if i > 0 else 10.0
            upper = crossovers[i] if i < n_bands - 1 else 20000.0
            band = {}
            if i > 0:
                # Band 0 is always enabled with no split-frequency; bands 1+
                # need both fields set so LSP MBC actually splits at lower.
                band["enable-band"] = True
                band["split-frequency"] = lower
            band.update({
                "compressor-enable": True,
                "mute": False,
                "solo": False,
                "attack-threshold": round(b["threshold"], 4),
                "attack-time": round(b["attack_ms"], 4),
                "release-threshold": MBC_RELEASE_THRESHOLD_FLOOR,
                "release-time": round(b["release_ms"], 4),
                "ratio": round(b["ratio"], 4),
                "knee": -6.0,
                "makeup": round(b["makeup"], 4),
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
                "sidechain-lowcut-frequency": lower,
                "sidechain-highcut-frequency": upper,
                "boost-threshold": -60.0,
                "boost-amount": 0.0,
            })
            result[bandn] = band
        else:
            # Disabled bands
            result[bandn] = _disabled_band()

    return result


def make_regulator(regulator: dict | None, freqs: list[int],
                   volmax_boost: float = 0.0,
                   volmax_slot: str = "input-gain",
                   couple_bands: bool = False) -> dict | None:
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

    `regulator-stress-amount`, `regulator-overdrive` and
    `regulator-relaxation-amount` are parsed for visibility (debug
    print + `_UNMODELED_FEATURES` watch list) but not mapped here. See
    docs/design-notes.md "Follow-ups" entry on regulator-stress for
    the empirical work that closed that hypothesis.

    couple_bands (experimental, `--enable coupled-bands`, issue #44):
    by default a zone whose threshold_high is >= 0 dBFS is treated as
    "never triggers" and disabled. A second-device DAX capture showed
    band dynamics on exactly such bands when the XML marks them
    non-isolated (`isolated_band` 0). With couple_bands on, a zero-dB
    zone whose bands are all isolated_band==0 takes its threshold at
    face value instead — a live limiter at full scale, which engages
    when upstream gain (e.g. volmax on input-gain) pushes the band past
    0 dBFS. Zones without isolated data, or containing an
    isolated_band==1 band, keep the default disabled behaviour. See
    design-notes Finding 10 / unvalidated-scaling entry 11 (f).

    volmax_boost lands on `input-gain` by default (issue #23) so the per-band
    compression tames the boosted low end before the brickwall;
    `volmax_slot="output-gain"` opts back into the older post-band-limiting
    placement. See `make_preset` for how that interacts with the chain.
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

    # Build the multiband compressor (used as limiter: ratio=100:1, fast attack).
    # volmax_slot picks which gain slot carries the static volmax-boost:
    # input-gain (default, issue #23) applies it pre-band-limiting, letting the
    # regulator's per-band downward compression tame the boosted low end before
    # the brickwall; output-gain opts back into post-band-limiting placement
    # (the full loudness makeup straight into the brickwall — the pre-#23
    # behaviour, kept for A/B and aggressive-regulator loudness recovery).
    # Neither placement is Dolby-documented (volmax-boost is a CP-stage leveler
    # ceiling; both slots are pragmatic approximations). Any value other than
    # "output-gain" keeps the input-gain default.
    boost = round(volmax_boost, 1)
    on_input = volmax_slot != "output-gain"
    result = {
        "bypass": False,
        "input-gain": boost if on_input else 0.0,
        "output-gain": 0.0 if on_input else boost,
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
            # — unless the experimental coupled-bands mapping takes the 0 dBFS
            # threshold at face value on a fully non-isolated zone (docstring).
            is_active = threshold < 0
            if not is_active and couple_bands:
                iso = regulator.get("isolated_band")
                is_active = (iso is not None and
                             all(iso[k] == 0
                                 for k in range(zone_start, zone_end + 1)))
            band = {
                "compressor-enable": is_active,
                "mute": False,
                "solo": False,
                "attack-threshold": round(threshold, 4),
                "attack-time": 1.0,  # very fast for limiting
                "release-threshold": MBC_RELEASE_THRESHOLD_FLOOR,
                "release-time": 50.0,
                "ratio": round(ratio, 4),
                "knee": round(knee, 4),
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
            result[bandn] = _disabled_band()

    return result


def _coupled_bands_eligible(regulator: dict | None) -> bool:
    """True when the XML carries bands the experimental coupled-bands
    mapping could activate: threshold_high >= 0 dBFS (excluded from
    limiting by default) while marked non-isolated (isolated_band == 0).
    Band-level check used for the end-of-run `--enable` hint; the actual
    activation in make_regulator is zone-level and can be stricter."""
    iso = (regulator or {}).get("isolated_band")
    if not iso:
        return False
    return any(t >= 0 and i == 0
               for t, i in zip(regulator["threshold_high"], iso))


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


def bass_enhancer_from_peq(peq_filters: list[dict]) -> dict:
    """The bass-enhancer stage as make_preset ships it for SoundWire,
    derived from the PEQ high-pass corner (fallback 100 Hz). Shared with
    the run report so the printed numbers cannot drift from the built
    stage.

    Whether the corner was derived or fell back is answered by
    ``bass_enhancer_scope_is_derived`` rather than a key on the returned
    stage: every key here is emitted into the preset, and the converter's
    coverage guard rightly rejects one it cannot translate.
    """
    hp = [f for f in peq_filters if f["type"] in (7, 9)]
    return make_bass_enhancer(hp[0]["f0"] if hp else 100.0)


def bass_enhancer_scope_is_derived(peq_filters: list[dict]) -> bool:
    """True when the bass-enhancer range came from the tuning's own high-pass.

    Most SoundWire tunings carry no PEQ at all — 36 of 39 distinct corpus
    files — so the printed range is twice the 100 Hz fallback, and the run
    report used to credit that constant to "this speaker's bass cutoff".
    """
    return any(f["type"] in (7, 9) for f in peq_filters)


def make_limiter(input_gain: float = 0.0) -> dict:
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
# The symptoms must not overlap. They used to share vocabulary — volmax said
# "pumping/squash", mbc "squashed character", regulator "spectral pumping" —
# so a user who hears squashed sound gets three candidates and no way to
# choose, which is the same as getting none. Each one now claims a distinct
# thing you can hear, in words someone who has never read an audio manual can
# match against, and they are ordered most-likely-to-help first.
DISABLEABLE_FILTERS = {
    "volmax": ("loud parts distort or sound crushed",
               "drops the +volmax-boost static loudness gain"),
    "mbc": ("music sounds flat and lifeless, with no light and shade",
            "drops the Dolby multi-band compressor"),
    "regulator": ("the volume audibly wobbles or surges on its own",
                  "drops the per-band limiter"),
    "autogain": ("quiet passages swell, then duck when things get loud",
                 "drops the volume leveler"),
    "bass-enhancer": ("bass sounds artificial or buzzy",
                      "drops the harmonic bass generator"),
    "dialog": ("voices are too forward or shouty",
               "drops the 2.5 kHz speech-band EQ"),
    "high-shelf": ("cymbals and 's' sounds are piercing",
                   "drops Dolby's type-3 high-shelf boost (experimental)"),
    "lo-pass": ("the top end sounds dull or muffled",
                "drops Dolby's type-6/8 low-pass rolloff (experimental)"),
}

# One stage sits in both menus: the volume leveler ships active on SoundWire
# (--disable autogain switches it off) and bypassed on HDA (--enable autogain
# switches it on). Its --disable row must key off the -active marker, not the
# "autogain" marker that means "present but bypassed" and feeds the --enable
# menu — otherwise every HDA run would offer to disable a stage that is
# already off.
_DISABLE_MENU_MARKER = {"autogain": "autogain-active"}

# Mirror of DISABLEABLE_FILTERS for stages that ship present but inactive:
# --enable NAME activates them on a rebuild. Same contract — adding an
# entry extends the argparse choices and the end-of-run hint block.
# The caveat is one short clause, not an explanation: this menu sits beside
# the one-line --disable menu and reads as its twin. What the stage actually
# does, and why the mapping is what it is, live in the README and design-notes
# behind the issue number — which stays, because switching a stage ON is the
# direction that carries a risk worth naming before someone tries it.
ENABLEABLE_FILTERS = {
    # "enabling may…" marks the second clause as the flag's side effect —
    # run together with the trigger it read as one continuous symptom. The
    # risk wording is the leveler family's one phrasing ("swell then
    # duck"); three variants for one risk read as three different risks
    # (round 3).
    # Autogain says its piece three times in a run — here, at the stage that
    # detects it, and in the closing block's guaranteed-differences line — and
    # that is deliberate (user decision, round 12, after a reviewer called the
    # third one padding). The three serve different readers: one scrolled back
    # to the detection site, one reading only the closing block, one scanning
    # this menu. Trimming any of them leaves that reader with nothing.
    "autogain": ("it sounds right but quieter than it did on Windows",
                 "enabling may make quiet passages swell then duck "
                 "(issue #25)"),
    # Describes what you'd hear, not where in the chain it happens: "where the
    # limiter is inactive" names an internal state the listener has no access
    # to, so it can't be matched against anything.
    # No region claim ("in the treble"): the flag extends limiting to
    # whichever zero-threshold non-isolated bands the tuning has — treble
    # on the two examined devices (3-6 kHz dev XML, 13.9 kHz #44), but
    # full-band on the issue-#27 class, so naming treble over-claims.
    # "(issue #NN)", not the bare "(#NN)": reviewers guessed the numbers
    # were GitHub issues but had no confirmation. Still no URL — the one
    # link rule.
    "coupled-bands": ("loud music turns harsh",
                      "experimental (issue #44)"),
    # Trigger says "than with the preset off", not "than on Windows": that
    # second phrasing is autogain's, and the two flags sit in the same menu.
    # The distinction is the whole diagnosis — autogain closes a gap against
    # Windows, this one closes a gap against bypass, which is the symptom
    # that identifies a curve whose peak outruns its volmax-boost.
    "level-restore": ("it sounds quieter than with the preset switched off",
                      "experimental; loud content may distort (issue #50)"),
}

# Emission paths that are numerically verified but not yet user-validated
# on real hardware. Keys that overlap with DISABLEABLE_FILTERS are turned
# off with --disable <key>; "mbc-1band" is a marker-only name (no separate
# flag — users who want it off should pass --disable mbc instead), and
# "coupled-bands-active" is the marker make_preset emits when --enable
# coupled-bands actually engaged a zone (drop the --enable flag to turn it
# off). Used to trigger a targeted "please report" prompt at end-of-run
# when any of these fired for the current preset.
# Plain name first, the tuning's own token in parentheses (round 8:
# "type-3 high-shelf" bare read as an undefined severity level).
EXPERIMENTAL_MARKERS = {
    "high-shelf": "a treble shelf boost (the tuning's type-3 high-shelf)",
    "lo-pass": "a top-end rolloff (type-6/8 low-pass)",
    "mbc-1band": "the compressor running as a single band (group_count=1)",
    "coupled-bands-active": "the coupled-bands limiter (isolated_band)",
    "level-restore-active": "the level the impulse response was normalised by, "
                            "handed back as a static gain",
}


def print_what_now(preset_names: list[str], autoloaded: bool,
                   dry_run: bool, output_dir=None,
                   profile_used: str | None = None,
                   n_modes: int = 0,
                   default_unknown: bool = False,
                   autogain_off: bool = False,
                   menu_printed: bool = False,
                   declared_default: str | None = None) -> None:
    """Say the run worked and how to start using it.

    ``profile_used``/``n_modes`` let the closing say the presets voice one
    sound mode of several (round 5: the pick was explained at the top, but
    the closing never said the other modes exist or that this run built
    only this one — --all-profiles is the answer, and it was never
    mentioned anywhere a user reads). ``default_unknown`` adds the guess
    caveat to that line (round 6: the caveat lived only at the top banner,
    which a Done-stopper never rereads). ``autogain_off`` adds the one
    guaranteed audible difference from Windows — the tuning's leveler
    shipping off — for the same reason: it never reached the last screen
    (round 6).

    The run reports each file as it writes it, hundreds of lines before the
    end, and then closed on troubleshooting advice for problems the user
    hasn't had yet — so the last screen never confirmed success and never
    said what to do with any of it. Someone running this once has no idea
    that a preset is a thing you go and select in EasyEffects.

    Silent under --autoload, which already wired the preset to the speakers
    and printed its own confirmation: repeating "go and select it" there
    would be wrong.
    """
    if not preset_names or autoloaded:
        return
    # One wording for both branches. The swell/duck caveat rides the
    # suggestion (round 7): alone on the last screen, "add --enable
    # autogain" read as a no-downside fix while its known side effect sat
    # scrolled away. Same risk phrasing as the leveler family everywhere.
    autogain_note = ("  Likely quieter than on Windows: your tuning's "
                     "volume leveler ships off here — --enable autogain "
                     "turns it on (may make quiet passages swell then "
                     "duck).")
    # The mismatch echo mirrors the autogain-note pattern (round 10): the
    # most actionable fix in the run lived only at the top and in the ask
    # small-print, never on the screen people act from.
    mismatch_note = None
    if (declared_default and profile_used
            and declared_default != profile_used):
        mismatch_note = (f"  Windows ships this device on "
                         f"'{declared_default}'; these voice "
                         f"'{profile_used}' — --profile "
                         f"{declared_default} rebuilds.")
    # Derived from what was actually built (round 7, user catch): a
    # tuning lacking a voicing curve skips that preset, so the hint must
    # not describe a preset that doesn't exist.
    hints = []
    if any(n.endswith("-Detailed") for n in preset_names):
        hints.append("Detailed is brighter")
    if any(n.endswith("-Warm") for n in preset_names):
        hints.append("Warm softer")
    voicing_hint = f" ({', '.join(hints)})" if hints else ""
    console.cprint("head", f"\n{'=' * 60}")
    if dry_run:
        # cta, not ok: green is this run's "check passed, nothing to do"
        # color, and the one line that still demands a re-run read as "all
        # done" in the same green (round-2 color finding).
        console.cprint("cta", f"Dry run — nothing was written. Re-run without "
                      f"--dry-run to install these {len(preset_names)} presets:")
        # One comma-separated line, not one name per line (round 7): the
        # vertical list ate the last screen's budget.
        console._cprint_wrapped("dim", "    " + ", ".join(preset_names),
                        indent="    ")
        # One clause on what installing gets them: a dry-run reader asked
        # "do I hear the change after re-running, or is there another step?"
        # and had nothing to go on until the real run printed its answer.
        # Only reached without --autoload (the early return above owns that
        # case), so "pick one yourself" is true here — and naming --autoload
        # gives the reader the self-loading default before the re-run, not
        # after it.
        console._cprint_wrapped("dim", "  You'll then pick one in EasyEffects — "
                               f"start with {preset_names[0]}"
                               f"{voicing_hint}; the real run "
                               "prints the exact steps. (Or add --autoload "
                               "and it loads itself for your speakers.)",
                        indent="  ")
        if profile_used and n_modes > 1:
            caveat = (" (we assume it is your Windows default)"
                      if default_unknown else "")
            console._cprint_wrapped("dim", f"  These voice the '{profile_used}' "
                                   f"sound mode only{caveat} — "
                                   "--all-profiles builds every mode.",
                            indent="  ")
        if mismatch_note:
            console._cprint_wrapped("dim", mismatch_note, indent="  ")
        if autogain_off:
            console._cprint_wrapped("dim", autogain_note, indent="  ")
        return
    # "starting in": each preset is two files and only the .json lands in
    # output_dir — the .irs impulse response goes to --irs-dir, a different
    # directory by default. "wrote N presets to <dir>" named half of what
    # the run had just listed above.
    console.cprint("ok", f"Done — wrote {len(preset_names)} presets"
                 + (f", starting in {output_dir}:" if output_dir else ":"))
    # Name them all — naming only the first left the reader wondering what
    # the other two were — but on one comma-separated line (round 7): the
    # vertical list ate the last screen's budget. No blank after (round
    # 10, user-picked): the closing had grown exactly one line past a
    # 26-line window, scrolling the green "Done" off the last screen.
    console._cprint_wrapped("dim", "    " + ", ".join(preset_names), indent="    ")
    # "Brighter"/"softer" measured against ieq_balanced on the corpus
    # curves (Dolby-global): detailed ≈ +4 dB treble, warm ≈ −2.5 dB
    # treble. Round 5: the closing named a starting preset but never said
    # what the other two are for, so nobody would try them.
    console._cprint_wrapped("dim", "  To use them: open EasyEffects, go to Output, and "
                           f"pick '{preset_names[0]}' from the Presets menu — "
                           f"that's the one to start with{voicing_hint}. "
                           "Or re-run with "
                           "--autoload to have it load itself for your "
                           "speakers.", indent="  ")
    if profile_used and n_modes > 1:
        caveat = (" (we assume it is your Windows default)"
                  if default_unknown else "")
        console._cprint_wrapped("dim", f"  These voice the '{profile_used}' sound "
                               f"mode only{caveat} — --all-profiles builds "
                               "every mode.", indent="  ")
    if mismatch_note:
        console._cprint_wrapped("dim", mismatch_note, indent="  ")
    if autogain_off:
        console._cprint_wrapped("dim", autogain_note, indent="  ")
    # The one-line map back to the menu (round 7): with the Done block
    # grown, the symptom→flag menu scrolls off a 26-line screen and the
    # reader said they'd never think to scroll. The pointer puts the
    # menu's existence on the last screen without re-breaking the round-3
    # order (success last, not troubleshooting).
    # "(re-running ... reprints it)": scrollback is gone once the terminal
    # closes, and the pointer alone was a dead end then (round 9).
    if menu_printed:
        console._cprint_wrapped("dim", "  Something sound off later? Scroll up to "
                               "\"If something doesn't sound right\" "
                               "(re-running this command reprints it).",
                        indent="  ")


# Width of the "    --disable volmax      " gutter each flag row hangs from,
# so a wrapped symptom lines up under the text it continues rather than under
# the flag.
_FLAG_GUTTER = 30


def _print_flag_hint(flag: str, comment: str, effect: str = "") -> None:
    """One row of a flag menu: the flag, its symptom, optionally its effect.

    Wrapped explicitly, because cprint hands text to the console verbatim so
    that URLs survive — which means anything long enough to need folding has
    to ask for it.
    """
    gutter = " " * _FLAG_GUTTER
    # Continuations indent two past the gutter so they land under the
    # comment text, not under its "#" — flush with the marker they read as
    # stray fragments (round 2).
    # Plain, not dim (round 5): fully dimmed rows read as less important
    # than the report asks below — these are the fix a user with bad audio
    # needs. Plain keeps them a step below the bold asks, which stay the
    # block's emphasis (user decision).
    console._cprint_wrapped("", f"    {flag:<{_FLAG_GUTTER - 4}}{comment}",
                    indent=gutter + "  ")
    if effect:
        console._cprint_wrapped("", f"{gutter}({effect})", indent=gutter + " ")


def print_troubleshooting(findings: list[Finding],
                          filters_by_profile: dict[str, set[str]],
                          installs_presets: bool = True,
                          enabled_by_flag: frozenset[str] = frozenset(),
                          dry_run: bool = False) -> bool:
    """Print what the user can do about their own audio, most specific first.

    Someone with a symptom scans until something matches and stops reading, so
    the findings this run actually raised come before the generic menu — and a
    hint that says "re-run with --disable volmax" turns that menu into context
    rather than arriving as a repeat of it.

    The menu is the longest, least targeted block in the tail, so it is one
    line per filter: the symptom is what someone picks a flag by, and the
    effect clause ("drops the per-band limiter") restates what the flag name
    already says. It used to carry that clause plus a per-profile scope note
    and shrink only once a hint had named a flag — two renderings of one menu,
    for a reason no single user could see, since each one sees one run.

    The symptom text stays rather than deferring to --help: --help lists the
    valid names and two examples, not the per-filter symptom, so pointing at
    it would be a claim that isn't true.
    """
    hints = [f for f in findings if f.kind == "hint" and f.ask]
    # A stage the user switched on with --enable never gets a --disable row:
    # both flags at once is a hard error, and the undo for a flag you typed
    # is removing it, not stacking its opposite. Only a stage active by the
    # device's own default (the leveler on SoundWire) is offered here.
    shown = [k for k in DISABLEABLE_FILTERS
             if _DISABLE_MENU_MARKER.get(k, k) in filters_by_profile
             and k not in enabled_by_flag]
    # Don't offer to switch off a stage this run already reported as never
    # engaging: "the volume wobbles on its own — --disable regulator" under a
    # warning that the regulator never does anything is a straight
    # contradiction, and the reader can't tell which half to believe.
    if any(f.slug == "loudness-untamed" for f in hints):
        shown = [k for k in shown if k != "regulator"]
    enable_hints = [k for k in ENABLEABLE_FILTERS if k in filters_by_profile]
    if not hints and not shown and not enable_hints:
        # Returns whether the menu printed, so the closing's scroll-up
        # pointer never points at a menu that isn't there.
        return False

    console.cprint("head", f"\n{'=' * 60}")
    console.cprint("head", "If something doesn't sound right")
    if hints:
        print()
        for finding in hints:
            _print_ask("warn", finding)

    # The menu lists every filter this run emitted, including any a hint above
    # already named. Omitting those looked tidier and read as a bug: a hint
    # says "re-run with --disable volmax" and the list of valid filters right
    # under it doesn't contain volmax, so the reader concludes one of the two
    # is stale and trusts neither.
    # Both autogain rows point at [leveler-gap] when that note fired: the
    # note names --disable autogain as the off-switch, and round 4 found
    # the pointer on the --enable row (leveler off by default) but missing
    # from the --disable row (leveler running), where the note is live.
    gap = any(f.slug == "leveler-gap" for f in findings)
    if shown:
        print()
        # Opens on the condition, so the list reads as "only if you hear it"
        # rather than as a to-do for a preset nobody has heard yet — on a
        # clean device this is the first thing under the heading.
        console._cprint_wrapped("dim", "  If anything sounds off on your hardware, you "
                               "can rebuild without specific filters:",
                        indent="  ")
        for name in shown:
            symptom, _effect = DISABLEABLE_FILTERS[name]
            comment = f"# {symptom}"
            if name == "autogain" and gap:
                comment += " — see [leveler-gap]"
            _print_flag_hint(f"--disable {name}", comment)

    # Same one-line shape as the --disable menu above, with the caveat folded
    # into the same line rather than hanging under it. "Shipped present but
    # inactive" was the old heading and could not be parsed cold — it names an
    # internal state (the stage is in the preset, bypassed) rather than
    # anything the reader can act on.
    if enable_hints:
        print()
        console.cprint("dim", "  Optional extras, switched off by default:")
        # On a device whose tuning pairs the leveler with sub-stages we can't
        # reproduce, --enable autogain is the switch that turns them on. The
        # run says so in the leveler-gap note far above; the menu offered the
        # flag with no hint of it, so the two never met.
        for name in enable_hints:
            symptom, caveat = ENABLEABLE_FILTERS[name]
            if name == "autogain" and gap:
                # The flag cannot enable a stage the preset never contains.
                # What it does is run our leveler without the companion
                # compression Dolby pairs with it — which is what the inline
                # [leveler-gap] note says, and what this row said backwards.
                caveat = ("on this device it runs without the companion "
                          "stage we can't reproduce, so quiet passages may "
                          "swell then duck — see [leveler-gap]")
            _print_flag_hint(f"--enable {name}", f"# {symptom} — {caveat}")

    # How to actually apply any of the above. Every suggestion here is a flag
    # on a re-run, and the output never said what to re-run, that flags can be
    # combined, or that EasyEffects keeps serving the old preset until it is
    # reloaded — so a rebuild that silently didn't take effect reads as "the
    # flag didn't help".
    if shown or enable_hints:
        print()
        # Only mention reloading in EasyEffects when this run is the thing
        # that put a preset there. Under dolby_to_pipewire.py these presets
        # are staged and thrown away, and the reader picked that path
        # precisely because they don't run EasyEffects — so the sentence that
        # tells them how to apply a fix ended in something they can't do. The
        # wrapper's own [3/3] steps cover applying it there.
        tail = (" Then reload the preset in EasyEffects to hear the change."
                if installs_presets else "")
        # Under --dry-run, "the same command you ran" would rebuild nothing —
        # the reader is four lines from being told nothing was written, and
        # telling them to reload a preset that doesn't exist read as the two
        # blocks not knowing about each other.
        # "the flags above", not "these": on a terminal whose window folds
        # exactly at this sentence, "these" is the first visible word of the
        # last screen with its antecedent scrolled off (round 4). Naming the
        # referent keeps the sentence whole at any fold.
        lead = ("Add any of the flags above when you re-run without "
                "--dry-run"
                if dry_run else
                "Add any of the flags above to the same command you ran")
        console._cprint_wrapped("dim", f"  {lead}; they combine.{tail}", indent="  ")
    return True

# Colorize the --disable/--enable NAME values inside --help prose with the
# same style the left column uses for metavar placeholders, so
# "--enable autogain" in a help sentence reads like "--enable NAME" does.
# rich-argparse applies each `highlights` regex to the rendered help text
# and styles a named group <g> as "argparse.<g>" — "metavar" is dark_cyan.
# The lookarounds exclude hyphen-adjacent hits so `volmax` never matches
# inside `volmax-boost` or `--volmax-slot`. Appended once at import time
# (the parser factory may run more than once under tests).
if console._HelpFormatter is not argparse.HelpFormatter:
    _FILTER_NAME_ALTERNATION = "|".join(
        re.escape(name)
        for name in sorted({*DISABLEABLE_FILTERS, *ENABLEABLE_FILTERS},
                           key=len, reverse=True))
    console._HelpFormatter.highlights = [
        *console._HelpFormatter.highlights,
        # "--disable volmax" / "--enable autogain" usage examples
        rf"--(?:disable|enable)\s+(?P<metavar>{_FILTER_NAME_ALTERNATION})",
        # the "Valid names: a, b, c." enumerations — each name sits between
        # ": "/", " and ","/"." there, which prose mentions never do
        rf"(?<=[:,] )(?P<metavar>{_FILTER_NAME_ALTERNATION})(?=[,.])",
    ]


def make_preset(kernel_name: str, peq_filters: list[dict],
                vol_leveler: dict | None = None,
                dialog_enhancer: dict | None = None,
                mb_comp: dict | None = None, regulator: dict | None = None,
                freqs: list[int] | None = None,
                is_soundwire: bool = False, volmax_boost: float = 0.0,
                volmax_slot: str = "input-gain",
                fir_peak_db: float = 0.0,
                enabled: set[str] | None = None,
                disabled: set[str] | None = None) -> tuple[dict, set[str]]:
    """Build a preset dict.

    Returns (preset, emitted) where emitted is the set of flag-actionable
    names for a rerun: DISABLEABLE_FILTERS names that actually ran
    (--disable candidates) plus ENABLEABLE_FILTERS names that shipped
    present but inactive (--enable candidates). Tracked inline with each
    emission branch so the set can't drift from what is in the returned
    dict.
    """
    enabled = enabled or set()
    disabled = disabled or set()
    emitted = set()
    preset = {
        "_generator": f"dolby_to_easyeffects.py {version.get_version()}",
        "output": {
            "blocklist": [],
            "convolver#0": make_convolver(kernel_name),
            "plugins_order": ["convolver#0"],
        }
    }

    # SoundWire speakers lack Dolby's proprietary Virtual Bass Enhancement
    # (VBE) that runs in the Windows driver. Compensate with psychoacoustic
    # harmonic generation so small speakers still produce perceived bass.
    if is_soundwire and "bass-enhancer" not in disabled:
        preset["output"]["bass_enhancer#0"] = bass_enhancer_from_peq(
            peq_filters)
        preset["output"]["plugins_order"].append("bass_enhancer#0")
        emitted.add("bass-enhancer")

    # No stereo widening: `surround-boost` is a virtualization-render-depth
    # control, dormant on 2-channel content — DAX applies no stereo widening
    # on stereo playback (design-notes entry 2). Earlier revisions emitted a
    # stereo_tools#0 widener here.

    effective_peq = peq_filters
    if "high-shelf" in disabled:
        effective_peq = [f for f in effective_peq if f["type"] != 3]
    if "lo-pass" in disabled:
        effective_peq = [f for f in effective_peq if f["type"] not in (6, 8)]
    peq = make_peq_eq(effective_peq)
    if peq:
        preset["output"]["equalizer#0"] = peq
        preset["output"]["plugins_order"].append("equalizer#0")
        if any(f["type"] == 3 for f in effective_peq):
            emitted.add("high-shelf")
        if any(f["type"] in (6, 8) for f in effective_peq):
            emitted.add("lo-pass")

    # Dialog enhancer (speech presence boost) before the volume leveler,
    # matching Dolby's CP order: DE → IEQ → Volume Leveler.
    if "dialog" not in disabled:
        de = make_dialog_enhancer(dialog_enhancer)
        if de:
            preset["output"]["equalizer#1"] = de
            preset["output"]["plugins_order"].append("equalizer#1")
            emitted.add("dialog")

    # Autogain (volume leveler) goes before the compressor/regulator to match
    # Dolby's signal flow: CP (volume leveler) → VLLDP (compressor → regulator).
    # This lets the compressor and regulator catch any overshoot from the leveler.
    if "autogain" not in disabled:
        autogain = make_autogain(vol_leveler, conservative=is_soundwire,
                                 enabled="autogain" in enabled)
        if autogain:
            preset["output"]["autogain#0"] = autogain
            preset["output"]["plugins_order"].append("autogain#0")
            if autogain["bypass"]:
                emitted.add("autogain")  # actionable via --enable on a rerun
            else:
                # Marker (not an ENABLEABLE_FILTERS key, so it never reaches
                # the hint block): lets main() tell "--enable autogain worked"
                # from "the XML's leveler is disabled, so the flag did
                # nothing".
                emitted.add("autogain-active")

    if "mbc" not in disabled:
        mbc = make_multiband_compressor(mb_comp, freqs)
        if mbc:
            preset["output"]["multiband_compressor#0"] = mbc
            preset["output"]["plugins_order"].append("multiband_compressor#0")
            emitted.add("mbc")
            if mb_comp and mb_comp["group_count"] == 1:
                emitted.add("mbc-1band")

    # volmax-boost injection: regulator input-gain is the default slot (issue
    # #23) — placed pre-band-limiting so the per-band compression tames the
    # boosted low end before the brickwall, instead of feeding the full static
    # makeup straight into it (volmax-boost is a CP-stage volume-leveler
    # ceiling, not a Dolby-documented placement; this is a pragmatic
    # approximation). --volmax-slot output-gain opts back into the pre-#23
    # post-band placement. If the regulator is disabled or absent from the XML,
    # fall back to limiter#0 input-gain so the boost still happens. Never both.
    # volmax_slot only re-routes the regulator path; the limiter fallback is
    # unaffected.
    apply_volmax = volmax_boost if "volmax" not in disabled else 0.0
    # --enable level-restore rides the same slot rather than adding a stage of
    # its own: it is a static broadband gain like volmax-boost, and issue #23
    # measured what the placement is worth (0.06% THD pre-band-limiting vs
    # 11.6% straight into the brickwall). fir_peak_db is what make_fir divided
    # out of the impulse response, so this restores a measured quantity rather
    # than applying an offset. --disable volmax drops only its own term; the
    # two are independent.
    level_restore = fir_peak_db if "level-restore" in enabled else 0.0
    static_boost = apply_volmax + level_restore
    reg = None
    if "regulator" not in disabled:
        reg = make_regulator(regulator, freqs, volmax_boost=static_boost,
                             volmax_slot=volmax_slot,
                             couple_bands="coupled-bands" in enabled)
    if reg:
        preset["output"]["multiband_compressor#1"] = reg
        preset["output"]["plugins_order"].append("multiband_compressor#1")
        emitted.add("regulator")
        limiter_boost = 0.0
        # A band that is enabled at a >= 0 dB threshold can only come from
        # the coupled-bands mapping — the default path disables those.
        coupled_fired = any(
            reg[f"band{i}"]["compressor-enable"]
            and reg[f"band{i}"]["attack-threshold"] >= 0
            for i in range(8))
        if "coupled-bands" in enabled and coupled_fired:
            # Marker (not an ENABLEABLE_FILTERS key): lets main() tell
            # "--enable coupled-bands worked" from "nothing to couple in"
            # — same contract as autogain-active above.
            emitted.add("coupled-bands-active")
        elif _coupled_bands_eligible(regulator):
            emitted.add("coupled-bands")  # actionable via --enable on a rerun
    else:
        limiter_boost = static_boost

    if apply_volmax > 0:
        emitted.add("volmax")
    if level_restore != 0:
        # Marker, not an --enable candidate: the flag is already on when this
        # fires. Same contract as autogain-active/coupled-bands-active.
        # `!= 0`, not `> 0`: a curve that only cuts normalises to a peak below
        # unity, so make_fir *adds* gain there and restoring it is negative.
        # No corpus XML does that (0 of 3051 checked 2026-08-04), but the
        # marker should track "the flag changed the output", not its sign.
        emitted.add("level-restore-active")
    elif fir_peak_db > apply_volmax:
        # Offer the flag only where it would do something (the precedent is
        # coupled-bands, 619a663). The gate is the deficit itself: the
        # convolver gives back fir_peak_db less than the tuning asks for, and
        # only the static boost puts any of it back — so a peak above it is
        # a preset that plays quieter than bypass. Below it there is nothing
        # to restore and the menu stays quiet.
        emitted.add("level-restore")

    # Brickwall limiter at the end as a safety net
    preset["output"]["limiter#0"] = make_limiter(input_gain=limiter_boost)
    preset["output"]["plugins_order"].append("limiter#0")

    return preset, emitted


class _HelpHintParser(argparse.ArgumentParser):
    """ArgumentParser that appends a --help pointer to usage errors, so a
    bad/unknown flag gets the same 'Run with --help' nudge that runtime
    errors get from the top-level handler. Mirrors argparse's default
    error(): usage synopsis to stderr, then 'prog: error: message', exit 2.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(
            2,
            f"{self.prog}: error: {message}\n"
            "Run with --help to see usage and all options.\n",
        )


def _report_parsed_profile(tuning, ao_db_left, ao_db_right, scale, disabled,
                           volmax_slot="input-gain", enabled=None,
                           is_soundwire=False, verbose=False):
    """Print the human-readable per-profile diagnostics for a parsed tuning
    (audio-optimizer / PEQ / dialog / surround / leveler / MBC / regulator /
    volmax), and return the findings raised while doing so.

    Side-effect-free apart from stdout — split out of main() so the
    orchestration there stays legible. Each finding prints its technical half
    here, in place; main() collects the returned list and renders the one-line
    asks at the end, where a user still has them on screen."""
    ieq_amount = tuning.ieq_amount
    peq_filters = tuning.peq_filters
    dialog_enhancer = tuning.dialog_enhancer
    surround = tuning.surround
    vol_leveler = tuning.vol_leveler
    mb_comp = tuning.mb_comp
    regulator = tuning.regulator
    volmax_boost = tuning.volmax_boost
    freqs = tuning.freqs

    findings: list[Finding] = []

    declared = tuning.default_profile
    if declared and declared != tuning.profile_used:
        findings.append(_profile_mismatch_finding(declared,
                                                 tuning.profile_used))
        _print_finding_detail(findings[-1])

    # One clause of meaning: this used to print bare ("ieq-amount: 10%
    # (scale: 0.10)") — no heading, nothing tying back to it, and a
    # reviewer couldn't tell whether it mattered.
    # Leads with the plain name (round 3: the bare acronym was the one
    # line still doing it) and ties the three preset files to the profile
    # they voice — reviewers read them as unrelated flavors.
    # "of full strength" anchors the percentage's scale — a bare "10%"
    # gave no way to tell strong from weak (round 6). "Differ in shape,
    # not strength": one number over three differently-described presets
    # left a round-7 reviewer unsure whether it covered all three. No
    # is-this-typical cue — no corpus stat backs one.
    # The list is derived, not hardcoded (round 7, user catch): the emit
    # loop skips any voicing whose ieq_* curve the XML lacks, so the
    # summary must not promise three when fewer will build.
    voicings = [label for label, key in VOICING_CURVES.items()
                if key in tuning.curves]
    if voicings:
        n_voc = ("three" if len(voicings) == 3
                 else str(len(voicings)) if len(voicings) > 1 else "one")
        names = "/".join(voicings)
        plural = "s" if len(voicings) > 1 else ""
        if tuning.ieq_enabled:
            console._cprint_wrapped("", f"Voicing strength (ieq-amount): {ieq_amount}% "
                                f"of full strength — this profile's {n_voc} "
                                f"voicing{plural} ({names}) "
                                + ("all apply" if len(voicings) > 1
                                   else "applies")
                                + " at this strength on top of the speaker "
                                "correction"
                                + ("; they differ in shape, not strength"
                                   if len(voicings) > 1 else ""), indent="  ")
        else:
            # With <ieq-enable> at 0 — about 45% of dynamic-profile corpus
            # rows — the tuning states no strength and Dolby engages none,
            # while ieq_amount still holds our assumed 10 and the build
            # applies it (scale = ieq_amount/100, unconditional). Stating the
            # percentage first and "Windows applies none" after read as a
            # contradiction to two reviewers, so the fact leads and the
            # number arrives as ours. What it buys is worth saying: that
            # scale multiplies the per-voicing curve and nothing else varies
            # between the three presets, so at 0 they would be one file.
            console._cprint_wrapped("", "Voicing strength (ieq-amount): this profile "
                                "switches the voicing off, so Windows applies "
                                f"none. We use {ieq_amount}% of full strength "
                                "instead — without it "
                                + (f"the {n_voc} voicings ({names}) would be "
                                   "identical; they differ in shape, not "
                                   "strength" if len(voicings) > 1 else
                                   f"the {names} voicing would add nothing to "
                                   "the speaker correction"), indent="  ")

    # Audio-optimizer: one triage-grade line by default — deepest cut/boost
    # with its frequency, and channel symmetry, which is what a pasted
    # normal-verbosity report gets read for first. The raw twenty-number
    # arrays read as "my sound is about to be damaged" (round 3, two
    # reviewers) and move behind -v.
    ao_l, ao_r = np.asarray(ao_db_left), np.asarray(ao_db_right)
    if not tuning.ao_enabled:
        print("\nAudio-optimizer: switched off in this profile")
        console.cprint("warn", "  audio-optimizer-enable=0 — the correction curve "
                       "this profile ships is not applied; only the IEQ "
                       "voicing reaches the convolver here.")
    else:
        parts = []
        cut = float(min(ao_l.min(), ao_r.min()))
        boost = float(max(ao_l.max(), ao_r.max()))

        # A register word beside each Hz value: the numbers alone don't say
        # whether the deepest cut lands in bass or treble (round 6), and
        # that is the one thing a listener can check by ear.
        def register(f):
            return ("bass" if f < 250
                    else "midrange" if f <= 4000 else "treble")

        if cut < 0:
            f_cut = freqs[int(np.argmin(np.minimum(ao_l, ao_r)))]
            parts.append(f"cuts to {cut:+.1f} dB (deepest at {f_cut} Hz, "
                         f"{register(f_cut)})")
        if boost > 0:
            f_boost = freqs[int(np.argmax(np.maximum(ao_l, ao_r)))]
            parts.append(f"boosts to {boost:+.1f} dB (at {f_boost} Hz, "
                         f"{register(f_boost)})")
        if not parts:
            parts.append("flat (all 0 dB)")
        # "(normal ...)": two round-9 reviewers read asymmetric correction
        # as a possible fault in their hardware.
        sym = ("same correction for left and right"
               if np.allclose(ao_l, ao_r)
               else "left and right corrected differently (normal — each "
                    "speaker gets its own correction)")
        # Friendly name first, like every other header (round 9).
        print("\nSpeaker correction (audio-optimizer): "
              + ", ".join(parts) + f", {sym}")
    if verbose:
        print(f"  Left:  {[f'{x:+.1f}' for x in ao_db_left]}")
        print(f"  Right: {[f'{x:+.1f}' for x in ao_db_right]}")

    # The row types carry a what-you-hear clause where the name alone says
    # nothing to a non-engineer — the dialog/bass sections had one and this
    # section didn't, which read as "am I supposed to understand this?".
    # Every type gets one (round 3: the glossed and bare rows side by side
    # read worse than all-bare). Header only when there are rows — over
    # nothing it read as a failed section.
    #
    # Most tunings configure L and R identically; printing both channels
    # doubled every row for no information (round 5). When the two channel
    # configurations match and -v is off, each filter prints once. Any L/R
    # difference keeps per-channel rows — the difference is itself the
    # detail worth reading — but the filter-design internals (order, S, Q)
    # are -v-only in every view: an unglossed S=1.0 on a default row was
    # the round-6 nit (freq and gain, the audible knobs, stay).
    def _peq_spec(pf):
        return {k: v for k, v in pf.items() if k != "speaker"}

    left_specs = [_peq_spec(p) for p in peq_filters if p["speaker"] == 0]
    right_specs = [_peq_spec(p) for p in peq_filters if p["speaker"] == 1]
    condensed = not verbose and left_specs == right_specs
    if peq_filters:
        # Plain name leads, acronym trails (round 8) — this header was the
        # one still leading with the acronym; "kept as parametric EQ" was
        # near-tautological next to "EQ filters" and goes.
        print("\nSpeaker EQ filters (PEQ"
              + ("; same for both speakers):  (details with -v)"
                 if condensed else "):"))
    for pf in (peq_filters if not condensed
               else [p for p in peq_filters if p["speaker"] == 0]):
        spk = "" if condensed else ("[L] " if pf["speaker"] == 0 else "[R] ")
        if pf["type"] in (7, 9):
            # Says there is no knob: "bass sounds thin" is the one symptom
            # with no flag in the menu (deliberately — this filter protects
            # the driver), and a round-4 reviewer went hunting for one and
            # settled on --disable bass-enhancer, a different symptom.
            tech = (f", order {pf['order']} ({pf['order'] * 6} dB/oct)"
                    if verbose else "")
            print(f"  {spk}HP @ {pf['f0']} Hz{tech} — cuts bass the speaker can't play (speaker protection; no flag turns it off)")
        elif pf["type"] in (6, 8):
            tech = (f", order {pf['order']} ({pf['order'] * 6} dB/oct)"
                    if verbose else "")
            print(f"  {spk}Lo-pass @ {pf['f0']} Hz{tech} — rolls off the top end  [unconfirmed-by-ear]")
        elif pf["type"] == 4:
            tech = f", S={pf['s']}" if verbose else ""
            print(f"  {spk}Lo-shelf @ {pf['f0']} Hz, {pf['gain']:+.1f} dB{tech} — shapes the low end")
        elif pf["type"] == 3:
            # "High-shelf" in display copy — matching --disable high-shelf;
            # the LSP mode string stays "Hi-shelf" (emitted parameter).
            tech = f", S={pf['s']}" if verbose else ""
            print(f"  {spk}High-shelf @ {pf['f0']} Hz, {pf['gain']:+.1f} dB{tech} — shapes the treble  [unconfirmed-by-ear]")
        elif pf["type"] == 1:
            tech = f", Q={pf['q']}" if verbose else ""
            # "lifts or trims", not "evens out": the same line prints for
            # positive-gain bells, which add a narrow band rather than
            # levelling one.
            print(f"  {spk}Bell @ {pf['f0']} Hz, {pf['gain']:+.1f} dB{tech} — lifts or trims a narrow band")

    if is_soundwire and "bass-enhancer" not in disabled:
        # Converter-added, not XML-derived: SoundWire tunings rely on Dolby's
        # in-driver Virtual Bass Enhancement, which has no XML parameters to
        # translate. It was the one active stage the run never mentioned —
        # so the --disable menu offered to drop something the reader had
        # never heard of (user-review round 1).
        be = bass_enhancer_from_peq(peq_filters)
        # "Separate from" only when the [speaker-optimizer] note fired this
        # run: a round-4 reviewer couldn't tell this boost and that
        # dropped protection stage apart ("is my bass protected or not?"),
        # but either message can appear without the other, so the clause
        # must not dangle on runs where the note never printed. Named
        # outright (round 5): "the bass-protection stage noted above" was
        # ambiguous against the HP rows' "speaker protection" clause.
        sep = (" (separate from the Dynamic Speaker Optimization stage "
               "noted above)"
               if any(f.slug == "speaker-optimizer" for f in tuning.findings)
               else "")
        # Says where the Hz figure comes from (round 7): a number in the
        # same sentence as "no settings in the XML" read as pulled from
        # nowhere. The scope derives from the PEQ high-pass corner
        # (min(2*hp, 300) — see make_bass_enhancer).
        print()
        # Two corrections to one sentence:
        # - the scope is only device-derived when the tuning ships a PEQ
        #   high-pass. Most SoundWire tunings carry no PEQ at all, so the
        #   200 Hz that prints is 2x the 100 Hz fallback — a constant the
        #   old wording credited to "this speaker's bass cutoff".
        # - the settings are IN the XML: bass-enhancer-enable/-boost/
        #   -cutoff-frequency/-width are present on every corpus row, all
        #   frozen (enable 0). What is missing is a tuning to copy, not the
        #   fields. The +dB is our own choice either way.
        scope_why = ("sized from this speaker's bass cutoff"
                     if bass_enhancer_scope_is_derived(peq_filters) else
                     "our default range — your tuning sets no bass cutoff")
        console._cprint_wrapped("", f"Bass enhancer: +{be['amount']:.1f} dB "
                            f"harmonics below {be['scope']:.0f} Hz "
                            f"({scope_why}) — our own stand-in for Dolby's "
                            "in-driver bass enhancement, which every tuning "
                            f"we've seen ships switched off{sep}",
                        indent="  ")

    if dialog_enhancer:
        # dB first: "amount=5" has no knowable scale (it's a raw schema
        # value), so the derived boost leads and the raw stays as the
        # report handle.
        gain = dialog_enhancer["amount"] / parse.DB_FIXED_POINT_SCALE * 6.0
        raw = f"amount {dialog_enhancer['amount']} of 16 in your tuning"
        if "dialog" in disabled:
            # Same shape as the volmax line: a stage the flag dropped says
            # so, instead of describing itself as if it shipped.
            print(f"\nDialog enhancer: {raw} — dropped by --disable dialog")
        else:
            # "about", and "where speech sits" rather than "speech boost":
            # the 6 dB ceiling behind the figure is on the unvalidated list
            # (reference.md "Validated vs unvalidated mappings"), and ours
            # is a static bell — Dolby's is speech-gated, so it lifts that
            # band on everything, not only on dialogue.
            print(f"\nDialog enhancer: about +{gain:.1f} dB around 2.5 kHz, "
                  f"where speech sits ({raw})")

    if surround:
        # No "virtualizer" in ANY form here — noun or verb: with the
        # [virtualizer] finding on the same screen, two features sharing
        # the word read as one feature with contradictory verdicts (rounds
        # 2-4; round 3 dropped the noun, the surviving "virtualizing" still
        # read as the contradiction). And no doc citation — three rounds of
        # reviewers called it unfollowable dev-talk on a line whose inline
        # reason stands alone.
        # Verdict first (round 7): leading with the dB figure made the
        # boost read as active for a beat before "skipped" landed.
        #
        # Says what was measured, not what Dolby intends. A DAX capture
        # found surround-boost=96 and =0 identical on 2-channel content
        # (0.01 dB S/M); that the boost applies to *surround* content is
        # the leading hypothesis in design-notes, never captured — no
        # multichannel capture exists. And with the tuning at 0 dB there is
        # nothing to skip, so that case says so instead.
        if surround["boost"] == 0:
            print("\nSurround (multi-channel) rendering boost: your tuning "
                  "sets none, so there is nothing to carry over")
        else:
            print("\nSurround (multi-channel) rendering boost: skipped on "
                  f"purpose — your tuning sets {surround['boost']:.1f} dB, "
                  "but we measured no difference it makes to ordinary "
                  "stereo playback")

    if vol_leveler:
        # Says BOTH states — the tuning file's and this preset's — and
        # names the flag that flips it. The label leads with "Autogain"
        # because that is the flag word: a round-4 reviewer got "Volume
        # leveler" from this line and then couldn't find that word anywhere
        # in the flag menus. Each state clause gives the two worlds their
        # own subjects ("your tuning … this preset") — the compressed
        # "enabled — ships switched off" read as the line contradicting
        # itself (rounds 3 and 4).
        enabled_flags = enabled or set()
        if not vol_leveler["enable"]:
            state = "switched off in your tuning"
        elif "autogain" in disabled:
            state = ("on in your tuning — removed from this preset by "
                     "--disable autogain")
        elif "autogain" in enabled_flags:
            state = ("on in your tuning — running in this preset (you "
                     "passed --enable autogain)")
        elif is_soundwire:
            state = ("on in your tuning — running in this preset "
                     "(--disable autogain switches it off)")
        else:
            # Carries its why (round 6): the override of the tuning's own
            # setting was only explained 48 lines later in the flag menu.
            # Same risk phrasing as the menu row — the leveler family's
            # one wording.
            state = ("on in your tuning, but this preset ships with it "
                     "off — it can make quiet passages swell then duck "
                     "(issue #25); add --enable autogain to turn it on")
        print(f"\nAutogain (volume leveler): {state}")
        # Settings only when the stage actually runs in this preset: on a
        # shipped-off build the targets are numbers the reader can't tie
        # to anything they'll hear (round 5).
        running = (vol_leveler["enable"] and "autogain" not in disabled
                   and ("autogain" in enabled_flags or is_soundwire))
        if running:
            # These are the tuning's numbers, and the built stage is not
            # identical to them: the SoundWire path takes 6 dB off the target
            # for headroom (make_autogain, conservative=True), so printing
            # them unlabelled reported a target the preset does not use.
            print(f"  your tuning: amount {vol_leveler['amount']}, targets "
                  f"{vol_leveler['in_target']:.1f} dB in / "
                  f"{vol_leveler['out_target']:.1f} dB out"
                  + ("  (this preset aims 6 dB lower, for headroom)"
                     if is_soundwire else ""))

    if mb_comp and "mbc" in disabled:
        # A dropped stage says so instead of describing itself, the shape
        # the volmax and leveler lines already use.
        print(f"\nMulti-band compressor (mbc): {mb_comp['group_count']} "
              "frequency band(s) in your tuning — dropped by --disable mbc")
    elif mb_comp:
        tag = "  [unconfirmed-by-ear]" if mb_comp["group_count"] == 1 else ""
        # "on loud content": measured dormant on the -10 dBFS stimuli and
        # only waking near -2 dBFS (design-notes, unvalidated-scaling entry
        # 6), so the bare present tense described a stage that mostly isn't
        # doing anything.
        print(f"\nMulti-band compressor (mbc): {mb_comp['group_count']} "
              "frequency band(s) — on loud content, evens out loud vs quiet "
              f"separately per frequency range{tag}")
        # Read-only, like regulator-overdrive and -relaxation: the field is
        # parsed and shown as a report handle but drives no emitted
        # parameter, so "the level it evens toward" credited the preset
        # with behaviour it does not have.
        print(f"  target-power-level: {mb_comp['target_power']:.1f} dB "
              "(read from your tuning; this preset doesn't use it)")
        # Print FROM the single-source decode — no inline re-decode, no
        # warnings (those fire in make_multiband_compressor). xover_hz is a
        # display concern derived here from the stored xover_idx + band
        # position, exactly as before.
        decoded = decode_mbc_bands(mb_comp)
        # The threshold range is the summary's diagnostic payload: it is
        # the first thing a triage of a squashed-sounding report reaches
        # for, and most reports arrive at normal verbosity.
        thr = [b["threshold"] for b in decoded]
        if len(thr) == 1:
            print(f"  threshold {thr[0]:+.1f} dB (where it kicks in)"
                  + ("" if verbose else "  (full band table with -v)"))
        else:
            print(f"  thresholds {max(thr):+.1f} to {min(thr):+.1f} dB "
                  "(where bands kick in)"
                  + ("" if verbose else "  (full band table with -v)"))
        n_bands_print = len(decoded)
        for i, b in enumerate(decoded if verbose else []):
            xover_idx = b["xover_idx"]
            if i == n_bands_print - 1:
                # Sentinel in the last band — it runs to the top of the
                # range. Printed as a frequency: "Nyquist" was the one word
                # in an otherwise numeric table a reviewer had never seen.
                xover_hz = ("full-band" if n_bands_print == 1
                            else f"{fir.SAMPLE_RATE // 2} Hz (top of range)")
            elif 0 <= xover_idx < len(freqs):
                xover_hz = f"{freqs[xover_idx]} Hz"
            else:
                xover_hz = "?"
            print(f"  band {i}: xover={xover_hz}, thresh={b['threshold']:+.1f} dB, "
                  f"ratio={b['ratio']:.2f}:1, attack={b['attack_ms']:.2f} ms, "
                  f"release={b['release_ms']:.2f} ms, makeup={b['makeup']:+.1f} dB")

    if regulator and "regulator" in disabled:
        # Dropped stages say so rather than describing themselves; without
        # this the whole section — protective gloss, band counts and the
        # coupled-bands offer — described a limiter the preset doesn't have.
        print("\nRegulator (per-band limiter): in your tuning — dropped by "
              "--disable regulator")
    elif regulator:
        # Plain tail + a triage-grade summary (how many bands limit, and
        # how hard) — the raw arrays were six unexplained lines of numbers
        # (round 3, all three reviewers) and move behind -v. The active-band
        # count and floor are what a report diagnosis reads first.
        th = regulator["threshold_high"]
        active = [x for x in th if x < 0]
        # "Steps in only when": distinguishes it from the always-shaping
        # multi-band compressor two sections up, whose gloss otherwise
        # read as the same job (round 5). The inert case leads with the
        # fact instead (round 9, user-picked rendering): the protective
        # gloss followed by "it never engages" read as reassurance
        # retracted in the same breath.
        if active:
            # "at the level this tuning sets", not "when loud parts would
            # distort": the engagement point is whatever threshold_high the
            # tuning carries, which is not a distortion point, and the
            # realised curve is measured well short of the configured limit
            # (design-notes, unvalidated-scaling entry 11).
            print("\nRegulator (per-band limiter): a protective ceiling, "
                  "band by band — steps in on loud content, at the level "
                  "this tuning sets")
            # "your tuning limits": the count is of raw XML bands, while
            # make_regulator merges them into <=8 zones keeping the highest
            # threshold, so some counted bands are not separately limited in
            # the preset. Attributing the count to the tuning keeps it true.
            print(f"  your tuning limits {len(active)} of {len(th)} frequency "
                  f"bands (deepest {min(th):+.1f} dB)"
                  + ("" if verbose else "  (full tables with -v)"))
        else:
            print()
            console._cprint_wrapped("", "Regulator (per-band limiter): configured "
                                "never to engage on this tuning — every "
                                "band's limit sits at or above full volume"
                                + ("" if verbose
                                   else "  (full tables with -v)"),
                            indent="  ")
        iso = regulator.get("isolated_band")
        # Gated on the same eligibility test the flag menu and the --enable
        # marker use, not on the field merely being present: where every
        # unlimited band is also marked isolated the flag adds nothing, and
        # this line offered an effect in a run whose own menu didn't list
        # the flag and whose re-run answers "had no effect".
        if _coupled_bands_eligible(regulator):
            # Co-located with the fact it explains: the only plain wording
            # for coupled-bands used to sit a screen away in the flag menu
            # (rounds 2–3). Mechanism only, no second count (round 7, user
            # decision): "marks N of 20 isolated (limited on their own)"
            # both over-claimed a field whose semantics are still open
            # (design-notes) and read as flatly contradicting the "limits
            # N bands" line whenever the counts differ. The raw
            # isolated_band array stays under -v.
            # "Some of": the flag's scope is a subset of the unlimited
            # bands (those the tuning also marks non-isolated), and the
            # subset word carries that without the 'isolated' jargon three
            # rounds of reviewers bounced off (rounds 7-9). The -v table
            # names the field for anyone digging. "Adds a limit to", not
            # "extends limiting to" (round 10): on all-inert tunings —
            # where the flag helps most — "extends" read as growing
            # existing limits, of which that reader has none.
            console._cprint_wrapped("", "  --enable coupled-bands adds a limit to "
                                "some of the bands the tuning leaves "
                                "unlimited (experimental, issue #44)",
                            indent="    ")
        if verbose:
            print(f"  threshold_high (dB): {[f'{x:+.1f}' for x in regulator['threshold_high']]}")
            print(f"  threshold_low (dB):  {[f'{x:+.1f}' for x in regulator['threshold_low']]}")
            print(f"  stress (dB):         {[f'{x:+.1f}' for x in regulator['stress']]}"
                  f"  ({len(regulator['stress'])} zones, not per-band)")
            print(f"  distortion-slope:    {regulator.get('distortion_slope', 1.0):.2f}")
            print(f"  timbre-preservation: {regulator.get('timbre_preservation', 0.75):.2f}")
            print(f"  overdrive (raw):     {regulator.get('overdrive', 0)}  (recorded for research; no effect on your output)")
            print(f"  relaxation (raw):    {regulator.get('relaxation', 96)}  (recorded for research; no effect on your output)")
            if iso is not None:
                print(f"  isolated_band:       {iso}")

    # Glossed like every other stage; the gain-slot detail is -v only.
    # Round-4 review (all three reviewers): the bare "(applied as
    # regulator input-gain)" was the one summary line with no plain
    # meaning, and it implied the boost dies with --disable regulator —
    # the limiter fallback keeps it, so the slot is an implementation
    # detail, not a dependency.
    # "Loudness boost (volmax-boost):" — the friendly-name-first header
    # shape every other section uses; this was the one lowercase raw-flag
    # header left (round 6).
    if volmax_boost == 0:
        print(f"\nLoudness boost (volmax-boost): {volmax_boost:+.1f} dB "
              "(your tuning asks for none)")
    elif volmax_boost < 0:
        # A negative boost is still applied — it goes into the same gain
        # slot as a positive one — so "asks for none" was wrong about the
        # one case where the tuning asks for a cut.
        print(f"\nLoudness boost (volmax-boost): {volmax_boost:+.1f} dB "
              "from your tuning — a cut, not a boost")
    elif "volmax" in disabled:
        print(f"\nLoudness boost (volmax-boost): {volmax_boost:+.1f} dB "
              "in your tuning — dropped by --disable volmax")
    else:
        # Names its own off-switch, like the leveler line does: the menu
        # row says "--disable volmax" and the reader had to spot the
        # substring match to connect the two (round 5).
        if verbose:
            slot = (f"regulator {volmax_slot}"
                    if regulator and "regulator" not in disabled
                    else "limiter input-gain")
            tail = f"(applied as {slot}; --disable volmax turns it off)"
        else:
            tail = "(--disable volmax turns it off)"
        print()
        console._cprint_wrapped("", "Loudness boost (volmax-boost): "
                            f"{volmax_boost:+.1f} dB from your tuning "
                            f"{tail}", indent="  ")
    # A band with threshold >= 0 dBFS never triggers, so make_regulator
    # disables it; if every band is like that, the regulator carries the
    # volmax boost but tames nothing — the issue-#23 "per-band compression
    # tames the boost before the brickwall" rationale doesn't apply, and
    # both volmax slots degenerate to the same untamed brickwall feed
    # (issue #27 field report; see design-notes).
    if (volmax_boost > 0 and "volmax" not in disabled
            and regulator and "regulator" not in disabled
            and all(t >= 0 for t in regulator["threshold_high"])
            and not ("coupled-bands" in (enabled or set())
                     and _coupled_bands_eligible(regulator))):
        findings.append(_loudness_untamed_finding(
            _coupled_bands_eligible(regulator)))
        _print_finding_detail(findings[-1])
    # The partial case: the regulator limits *somewhere*, so the warning above
    # stays quiet, yet the band carrying the tuning's largest boost is one of
    # the bands it leaves alone — the boost and the volmax gain on top of it
    # reach the brickwall unprotected. Two ways in, and they need different
    # gates because the drive level differs:
    #
    #  - Default path: the FIR is peak-normalised, so that band leaves the
    #    convolver at 0 dB and reaches the brickwall at exactly volmax_boost
    #    above bypass — the same drive every tuning gets, whatever its peak.
    #    What the peak measures here is spectral contrast, not level, so the
    #    bar stays where it was: the boost reaching this XML's full gain
    #    range. Re-derived 2026-08-04 over 3051 parsed corpus XMLs — 10.6%,
    #    against the all-inert case's 16% (issue #46's T495 is one). Read
    #    that bar honestly: only 172 of those files declare
    #    <geq_maximum_range> at all (30 of the 1661 that reach this branch),
    #    so for almost every device it compares against our assumed +12.0 dB
    #    rather than a rail the tuning stated.
    #  - --enable level-restore: the peak is handed back to the chain, so
    #    the same band now arrives at volmax_boost + peak_db — 15.2 dB above
    #    bypass on issue #50's tuning. That is the flag's own risk, so it
    #    warns whatever the peak's relation to the rail. It reaches 54% of
    #    the tunings that get this far, which would be a nag as a default
    #    but is the point when someone has opted into the boost.
    elif (volmax_boost > 0 and "volmax" not in disabled
            and regulator and "regulator" not in disabled
            and not ("coupled-bands" in (enabled or set())
                     and _coupled_bands_eligible(regulator))):
        peak_band = max(range(len(ao_db_left)),
                        key=lambda i: max(ao_db_left[i], ao_db_right[i]))
        peak_db = max(ao_db_left[peak_band], ao_db_right[peak_band])
        thresholds = regulator["threshold_high"]
        at_rail = peak_db >= tuning.geq_max_range / parse.DB_FIXED_POINT_SCALE
        restored = "level-restore" in (enabled or set())
        if ((at_rail or restored)
                and peak_band < len(thresholds)
                and thresholds[peak_band] >= 0):
            findings.append(_boost_unlimited_finding(
                peak_db, freqs[peak_band],
                _coupled_bands_eligible(regulator), restored))
            _print_finding_detail(findings[-1])
    print()
    return findings


# Verdict gate for the printed FIR verification: far above the minimum-phase
# design's normal residual (~0.05 dB at the 20 probe points) and below
# anything audible, so it warns only when the reconstruction actually broke.
FIR_VERIFY_OK_DB = 0.5


# The three IEQ voicings a run can build, in build order — single source
# for the emit loop and every line of copy that names them. A voicing whose
# curve the XML lacks is skipped, so copy derives its list from this ∩ the
# parsed curves rather than promising all three (round 7).
VOICING_CURVES = {
    "Balanced": "ieq_balanced",
    "Detailed": "ieq_detailed",
    "Warm": "ieq_warm",
}


def _emit_ieq_presets(tuning, name_base, ao_db_left, ao_db_right, float_freqs,
                      scale, is_soundwire, disabled, args, profile_label,
                      all_preset_names, filters_by_profile,
                      warned: bool = False):
    """Generate the Balanced/Detailed/Warm IEQ presets for one parsed profile:
    build each combined FIR, write the .irs + .json, print the verification
    table, and record emitted filters. Mutates ``all_preset_names`` and
    ``filters_by_profile`` in place (main() reads them after the loop)."""
    curves = tuning.curves
    peq_filters = tuning.peq_filters
    vol_leveler = tuning.vol_leveler
    dialog_enhancer = tuning.dialog_enhancer
    mb_comp = tuning.mb_comp
    regulator = tuning.regulator
    freqs = tuning.freqs
    volmax_boost = tuning.volmax_boost

    ieq_presets = {f"{name_base}-{label}": key
                   for label, key in VOICING_CURVES.items()}

    # One hidden-tables hint per profile, at the spot the first table would
    # have occupied — three identical lines read as a nag.
    tables_hint_pending = not args.verbose
    # (preset_name, worst-deviation) per built FIR — the default view prints
    # one consolidated verdict after the loop; three identical green
    # "passed" lines read as three separate validations (round 6).
    check_results: list[tuple[str, float]] = []

    for preset_name, curve_key in ieq_presets.items():
        if curve_key not in curves:
            console.cprint("warn", f"  Skipping {preset_name}: curve '{curve_key}' not found in XML")
            continue

        gains_raw = curves[curve_key]
        ieq_db = np.array(gains_raw) / parse.DB_FIXED_POINT_SCALE * scale

        # Combined target: IEQ + audio-optimizer (summed in dB)
        combined_left = ieq_db + ao_db_left
        combined_right = ieq_db + ao_db_right

        # Generate FIR impulse responses
        fir_left, peak_left_db = fir.make_fir(float_freqs, combined_left,
                                          normalize=True)
        fir_right, peak_right_db = fir.make_fir(float_freqs, combined_right,
                                            normalize=True)

        # --enable level-restore: hand the chain back the level normalisation
        # removed. make_fir divides each channel by its own realised peak, so
        # a curve whose peak outruns its volmax-boost emits a preset quieter
        # than bypass — the deficit is exactly peak_db - volmax_boost, and it
        # is what issues #25/#46/#50 describe. The restored amount is the
        # peak make_fir measured, so nothing here is a tuned offset.
        #
        # Re-reference both channels to the louder peak first. Normalising
        # each channel to its own peak also flattens the L/R level
        # relationship the two AO curves ask for — the two combined peaks
        # diverge on 19.1% of the corpus (median 0.93 dB, max 5.56;
        # re-derived 2026-08-04 over 3051 parsed XMLs). A common reference
        # keeps that relationship and still leaves every channel at or below
        # 0 dBFS, so the on-disk peak-normalisation convention holds.
        fir_peak_db = max(peak_left_db, peak_right_db)
        # Non-zero only on the flag-on path, and only for the quieter
        # channel; the correction check below re-references by the same
        # amount so it keeps grading the filter rather than the re-reference.
        left_offset_db = 0.0
        if "level-restore" in args.enable:
            left_offset_db = peak_left_db - fir_peak_db
            fir_left *= 10.0 ** (left_offset_db / 20.0)
            fir_right *= 10.0 ** ((peak_right_db - fir_peak_db) / 20.0)

        # Save stereo impulse response
        irs_path = args.irs_dir / f"{preset_name}.irs"
        if not args.dry_run:
            save_wav_stereo(irs_path, fir_left, fir_right)

        # Create preset (kernel-name is the WAV filename stem)
        preset, emitted = make_preset(preset_name, peq_filters, vol_leveler,
                                      dialog_enhancer, mb_comp, regulator,
                                      freqs, is_soundwire=is_soundwire,
                                      volmax_boost=volmax_boost,
                                      volmax_slot=args.volmax_slot,
                                      fir_peak_db=fir_peak_db,
                                      enabled=set(args.enable),
                                      disabled=disabled)
        for name in emitted:
            filters_by_profile.setdefault(name, set()).add(profile_label)
        out_path = args.output_dir / f"{preset_name}.json"
        if not args.dry_run:
            _atomic_write_text(out_path, json.dumps(preset, indent=4) + "\n")

        all_preset_names.append(preset_name)

        # "Staged", dimmed, when a wrapper is writing into a tempdir it
        # will delete: round-4's wrapper reviewer saw the same green
        # "Wrote" on these doomed files as on the conf that survives, and
        # expected to find them later.
        if args.dry_run:
            style, verb = "ok", "Would write"
        elif getattr(args, "staged", False):
            style, verb = "dim", "Staged"
        else:
            style, verb = "ok", "Wrote"
        console.cprint(style, f"{verb} {irs_path}")
        console.cprint(style, f"{verb} {out_path}")
        # The tables are behind -v: even marked skippable they were the
        # bulk of the output, burying the findings between them, and their
        # only reader is someone diagnosing a wrong-sounding preset — who
        # is told to re-run with -v. The verdict line below prints either
        # way, so the check itself is never hidden.
        if args.verbose:
            print(f"  {curve_key} combined IEQ+AO curve (left channel):")
            print(f"  {'freq':>8}  {'IEQ':>6}  {'AO':>6}  {'combined':>8}")
            for i, f in enumerate(freqs):
                print(f"  {f:>7} Hz  {ieq_db[i]:+5.1f}  {ao_db_left[i]:+5.1f}  {combined_left[i]:+7.1f}")
        elif tables_hint_pending:
            tables_hint_pending = False
            console.cprint("dim", "  (frequency tables hidden — re-run with -v to "
                          "print them)")

        # Verify FIR frequency response — the math runs either way; -v only
        # decides whether the per-frequency rows print.
        H = np.fft.rfft(fir_left, n=fir.FIR_LENGTH)
        fft_freqs = np.fft.rfftfreq(fir.FIR_LENGTH, d=1.0 / fir.SAMPLE_RATE)
        mag_db = 20.0 * np.log10(np.abs(H) + fir.LOG_MAG_FLOOR)
        if args.verbose:
            console.cprint("dim", "\n  FIR verification (left, normalized to "
                          "peak=0):")
        worst = 0.0
        for i, f in enumerate(freqs):
            idx = np.argmin(np.abs(fft_freqs - f))
            target = (combined_left[i] - np.max(combined_left)
                      + left_offset_db)
            err = mag_db[idx] - target
            worst = max(worst, abs(err))
            if args.verbose:
                console.cprint("dim", f"  {f:>7} Hz  target: {target:+6.1f}  "
                      f"actual: {mag_db[idx]:+6.1f}  "
                      f"error: {err:+5.2f}")
        # A table of sixty "error" rows with no verdict reads as a slow
        # drift going wrong; nobody outside this file knows 0.03 dB is a
        # pass. The threshold is far above the minimum-phase design's
        # normal residual (~0.05 dB) and below anything audible.
        # "Correction check", not "FIR check": FIR was the one label in the
        # summary with no plain reading (round 4), and "correction" is the
        # audio-optimizer line's vocabulary for the same curve.
        # No "(inaudible)": printed a few lines under a ⚠ loudness warning,
        # the green all-clear read as canceling it (round 5). This line is
        # about curve accuracy only — keep listening language out.
        check_results.append((preset_name, worst))
        if args.verbose:
            # Next to its own table; the default view gets one verdict for
            # all three after the loop.
            # "its target" named nothing a reader could point at. The target
            # is the curve computed from their tuning — say that, since the
            # whole value of the line is which side it certifies.
            if worst <= FIR_VERIFY_OK_DB:
                console.cprint("ok", f"  Correction check passed: the built filter "
                             f"matches the curve your tuning file asks for, "
                             f"within {worst:.2f} dB")
            else:
                console.cprint("warn", f"  Correction check: {worst:.2f} dB away from "
                               "the curve your tuning file asks for, at worst "
                               "— unexpected, please report this run")
        print()

    fails = [(n, w) for n, w in check_results if w > FIR_VERIFY_OK_DB]
    if not args.verbose and check_results:
        if not fails:
            worst_all = max(w for _, w in check_results)
            # Dim, not green, when a ⚠ fired above: the celebratory color
            # read as cancelling the warning (round 9, user-picked
            # rendering) — the check only covers curve accuracy.
            console.cprint("dim" if warned else "ok",
                   f"  Correction check passed: all "
                   f"{len(check_results)} filters match the curve your tuning "
                   f"file asks for, within {worst_all:.2f} dB")
        else:
            for name, w in fails:
                console.cprint("warn", f"  Correction check ({name}): {w:.2f} dB away "
                               "from the curve your tuning file asks for, at "
                               "worst — unexpected, please report this run")
        print()


def _make_adder(container, only):
    """Shared-group plumbing: an ``add_argument`` wrapper that skips flags not
    selected by ``only`` (keyed by primary name: first option string, or the
    positional's name) and records the added actions so callers — notably
    dolby_to_pipewire.py — can rebuild a child argv from them generically."""
    added = []

    def add(*names, **kwargs):
        if only is None or names[0] in only:
            added.append(container.add_argument(*names, **kwargs))

    return add, added


TUNING_INPUT_DESCRIPTION = (
    "with neither an XML path nor --windows, the script auto-discovers: it "
    "probes mounted Windows partitions (/proc/mounts) and the current "
    "directory for a tuning source"
)


def add_tuning_input_args(container, *, only=None):
    """Tuning-input flags (group shared with dolby_to_pipewire.py)."""
    add, added = _make_adder(container, only)
    add(
        "xml_file",
        nargs="?",
        type=Path,
        default=None,
        help="path to the Dolby DAX3 tuning XML (e.g. DEV_0287_SUBSYS_*.xml)",
    )
    add(
        "--windows",
        type=Path,
        default=None,
        metavar="DIR",
        help="path to a mounted Windows directory (e.g. /mnt/windows/Windows); "
             "auto-discovers the correct tuning XML by matching the audio "
             "codec subsystem ID from /proc/asound",
    )
    add(
        "--best-guess",
        action="store_true",
        help="if auto-detection finds no exact hardware match, fall back to the "
             "only internal-speaker tuning whose manufacturer is present "
             "(unverified — matched by manufacturer, not device id). With "
             "several such candidates it lists them so you can pass one as the "
             "positional XML path. No effect when an exact match is found",
    )
    return added


def add_inspection_args(container, *, only=None):
    """Inspection modes (group shared with dolby_to_pipewire.py)."""
    add, added = _make_adder(container, only)
    add(
        "--list",
        action="store_true",
        help="list available endpoints and profiles, then exit",
    )
    add(
        "--speaker-info",
        action="store_true",
        help="report detected audio hardware and speaker layout, then exit",
    )
    add(
        "--doctor", "--diagnose",
        dest="doctor",
        action="store_true",
        help="run environment self-diagnostics (EasyEffects version, install "
             "location, preset/impulse-file integrity, selected preset, "
             "background service mode + autostart, hardware) and exit — "
             "paste the output into an issue if a preset seems inaudible",
    )
    return added


def add_profile_selection_args(container, *, only=None):
    """Profile-selection flags (group shared with dolby_to_pipewire.py)."""
    add, added = _make_adder(container, only)
    add(
        "--endpoint",
        default="internal_speaker",
        help="endpoint type from the XML (default: internal_speaker)",
    )
    add(
        "--mode",
        default="normal",
        help="endpoint operating mode (default: normal)",
    )
    add(
        "--profile",
        default=None,
        help="profile type, e.g. dynamic, music, voice (default: first profile)",
    )
    add(
        "--all-profiles",
        action="store_true",
        help="generate presets for all profiles in the selected endpoint/mode "
             "(profile names are included in the preset names)",
    )
    return added


def add_autoload_args(container, *, only=None):
    """Autoload flags — EasyEffects-only, never shared with the wrapper."""
    add, added = _make_adder(container, only)
    add(
        "--autoload",
        nargs="?",
        const=True,
        metavar="PRESET",
        help="write EasyEffects autoload config for speaker outputs. "
             "Optionally specify the preset name to autoload; "
             "defaults to the first Balanced preset generated",
    )
    add(
        "--autoload-dir",
        type=Path,
        default=DEFAULT_AUTOLOAD_DIR,
        help=f"EasyEffects autoload directory (default: {DEFAULT_AUTOLOAD_DIR})",
    )
    add(
        "--autoload-sink",
        action="append",
        default=[],
        metavar="NODE_NAME",
        help="explicit PipeWire sink node.name to bind autoload to, bypassing "
             "speaker-sink detection (repeatable). Use this when auto-detection "
             "picks the wrong output or finds none — e.g. a device whose "
             "internal speaker is mis-tagged (no audio-speakers device icon). "
             "Find the name with 'pw-dump | grep node.name', or run with "
             "--autoload to print the candidate list. Mirrors "
             "ee_to_pipewire.py's --target-sink.",
    )
    add(
        "--no-autoload-bypass",
        dest="autoload_bypass",
        action="store_false",
        help=f"with --autoload, do not write a '{BYPASS_PRESET_NAME}' bypass "
             "preset or enable EasyEffects' global Fallback Preset. Use if "
             "you manage the fallback yourself. Existing user setups are "
             "preserved even without this flag.",
    )
    return added


def add_output_args(container, *, only=None):
    """Output naming/location flags (dolby_to_pipewire.py shares --prefix)."""
    add, added = _make_adder(container, only)
    add(
        "--prefix",
        default="Dolby",
        help="prefix for preset names (default: Dolby → Dolby-Balanced, etc.)",
    )
    add(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"EasyEffects output preset directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    add(
        "--irs-dir",
        type=Path,
        default=DEFAULT_IRS_DIR,
        help=f"EasyEffects impulse response directory (default: {DEFAULT_IRS_DIR})",
    )
    return added


def add_filter_tweak_args(container, *, only=None):
    """Filter-tweak flags (group shared with dolby_to_pipewire.py)."""
    add, added = _make_adder(container, only)
    add(
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
    add(
        "--enable",
        action="append",
        default=[],
        choices=list(ENABLEABLE_FILTERS),
        metavar="NAME",
        help="activate a filter that ships present but inactive "
             f"(repeatable). Valid names: {', '.join(ENABLEABLE_FILTERS)}. "
             "Try --enable autogain if the preset sounds right but quieter "
             "than Windows (issue #25), --enable coupled-bands "
             "(experimental) if loud content turns harsh where the "
             "per-band limiter is inactive (issue #44), or --enable "
             "level-restore (experimental) if the preset is quieter than "
             "switching it off altogether (issue #50).",
    )
    add(
        "--volmax-slot",
        choices=["input-gain", "output-gain"],
        default="input-gain",
        help="which regulator gain slot carries the static volmax-boost. "
             "'input-gain' (default) applies it pre-band-limiting so the "
             "regulator's per-band compression tames the boosted low end before "
             "the brickwall — avoids the loud-low-frequency distortion of the "
             "older placement (issue #23). 'output-gain' opts back into "
             "post-band-limiting placement (the full loudness makeup straight "
             "into the brickwall); use it for A/B comparison, or if input-gain "
             "costs too much loudness on a device with an aggressive regulator. "
             "Neither placement is Dolby-documented; no effect when the regulator "
             "is disabled/absent (the boost then lands on limiter#0 input-gain).",
    )
    return added


def add_general_args(container, *, only=None):
    """General flags — dolby_to_pipewire.py authors its own equivalents
    (and forwards --verbose to the generator it runs)."""
    add, added = _make_adder(container, only)
    add(
        "--verbose", "-v",
        action="store_true",
        help="print the full frequency tables (hidden by default); include "
             "a -v log when reporting a sound problem",
    )
    add(
        "--dry-run",
        action="store_true",
        help="run without writing any files to disk (presets, IRs, autoload); "
             "useful for debugging script execution and output",
    )
    add(
        "--skip-ee-check",
        action="store_true",
        help="skip the end-of-run EasyEffects environment check (version and "
             "install-location warnings) — for workflows that don't target an "
             "EasyEffects install; dolby_to_pipewire.py passes this "
             "automatically",
    )
    add(
        "--skip-closing",
        action="store_true",
        help="skip the end-of-run closing blocks (what was written and how to "
             "use it, and the report-back block) — for wrappers that install "
             "elsewhere and present their own",
    )
    add(
        "--no-color",
        action="store_true",
        help="disable colored terminal output",
    )
    add(
        "--version",
        action="version",
        version=f"%(prog)s {version.get_version()}",
        help="show version and exit",
    )
    return added


def build_parser(argv: list[str] | None = None) -> argparse.ArgumentParser:
    # --no-color must be honored before argparse prints --help; pre-scan
    # argv so the formatter falls back to plain when requested.
    _argv = sys.argv[1:] if argv is None else argv
    formatter_class = argparse.HelpFormatter if "--no-color" in _argv else console._HelpFormatter
    epilog = None
    if console._MISSING_COLOR_DEPS:
        epilog = (
            f"Tip: install {' and '.join(console._MISSING_COLOR_DEPS)} for colored output "
            "(see README for distro packages)."
        )
    parser = _HelpHintParser(
        description="Convert Dolby DAX3 tuning XML to EasyEffects output presets.",
        epilog=epilog,
        formatter_class=formatter_class,
    )
    add_tuning_input_args(parser.add_argument_group(
        "tuning input", description=TUNING_INPUT_DESCRIPTION))
    add_inspection_args(parser.add_argument_group("inspection"))
    add_profile_selection_args(parser.add_argument_group("profile selection"))
    add_output_args(parser.add_argument_group("output"))
    add_autoload_args(parser.add_argument_group("autoload"))
    add_filter_tweak_args(parser.add_argument_group("filter tweaks"))
    add_general_args(parser.add_argument_group("general"))
    return parser


def _complete_sink_names(prefix: str, **_kwargs) -> list[str]:
    """Tab-completion for --autoload-sink: the PipeWire node.name values.

    Reuses the single pw-dump boundary so the names offered are exactly the
    ones the autoload resolver accepts — which is the answer the flag's help
    currently sends people to `pw-dump | grep node.name` for.
    """
    try:
        names = [s.get("name", "") for s in _enumerate_audio_sinks()]
    except Exception:  # a wedged or absent PipeWire must never break TAB
        return []
    return [n for n in names if n.startswith(prefix)]


def _complete_preset_names(prefix: str, **_kwargs) -> list[str]:
    """Tab-completion for --autoload's optional PRESET: the preset stems
    already present in the EasyEffects output directory."""
    try:
        stems = [p.stem for p in DEFAULT_OUTPUT_DIR.glob("*.json")]
    except OSError:
        return []
    return [s for s in stems if s.startswith(prefix)]


def _attach_completers(parser: argparse.ArgumentParser) -> None:
    """Tell argcomplete what each value-taking option means.

    argparse records `type=Path` for directories and XML files alike, and
    nothing at all for PipeWire node names, so that distinction has to live
    somewhere. Options carrying `choices=` are absent by design — argcomplete
    reads those off the parser itself, which is why --disable/--enable can't
    drift from DISABLEABLE_FILTERS/ENABLEABLE_FILTERS.
    """
    from argcomplete.completers import DirectoriesCompleter, FilesCompleter

    completers = {
        "xml_file":      FilesCompleter(("xml", "XML")),
        "windows":       DirectoriesCompleter(),
        "output_dir":    DirectoriesCompleter(),
        "irs_dir":       DirectoriesCompleter(),
        "autoload_dir":  DirectoriesCompleter(),
        "autoload_sink": _complete_sink_names,
        "autoload":      _complete_preset_names,
    }
    for action in parser._actions:
        completer = completers.get(action.dest)
        if completer is not None:
            action.completer = completer


def ensure_dsp() -> None:
    """Load the DSP stack if the completion-path deferral skipped it.

    Reaching here means the run is real, not a tab completion — argcomplete
    exits inside autocomplete(). Callers that hook completion themselves
    (dolby_to_pipewire.py composes its own parser) must still call this.
    """
    if "np" not in globals():
        _load_dsp()


def complete_and_load(parser: argparse.ArgumentParser) -> None:
    """Serve a shell tab-completion request, then finish start-up for a real
    run. The single call the entry point needs."""
    if argcomplete is not None:
        _attach_completers(parser)
        argcomplete.autocomplete(parser)
    ensure_dsp()


def main(argv: list[str] | None = None,
         closing: list[Finding] | None = None,
         troubleshooting: dict | None = None,
         resolved: dict | None = None,
         staged: bool = False):
    """Generate the presets. ``closing`` collects the findings the closing
    block would render, for a caller that prints that block itself (see
    ``--skip-closing``). Always populated when supplied, independently of
    the flag, so a wrapper can't accidentally drop the run's findings.
    ``troubleshooting``, when supplied, likewise takes the fix-flags menu:
    it is filled with print_troubleshooting's inputs instead of the menu
    printing here, so the caller can render it at its own end. ``resolved``
    takes what only this function can work out — currently ``xml_path``,
    which auto-discovery may have found on a mounted Windows partition; the
    closing block names it as the file to attach, and a caller printing that
    block on our behalf has no other way to learn it. ``staged`` marks the
    output dirs as a wrapper's throwaway staging area, so the per-file
    announcements say "Staged", not "Wrote"."""
    parser = build_parser(argv)
    complete_and_load(parser)
    args = parser.parse_args(argv)
    args.staged = staged
    report_findings._TAG_CONVENTION_SHOWN = False
    if args.no_color:
        console._disable_color()
    disabled = set(args.disable)
    # A name in both directions is a contradiction, not a preference to
    # resolve — silently picking a winner would leave the user believing
    # whichever flag they meant. The menus can't steer anyone here: the
    # --disable row for a stage the user switched on with --enable is
    # suppressed (see print_troubleshooting), so this only fires on a
    # hand-typed conflict.
    overlap = sorted(disabled & set(args.enable))
    if overlap:
        parser.error(f"{', '.join(overlap)} given to both --disable and "
                     f"--enable — drop one of the two flags")

    if args.speaker_info:
        report_speaker_info()
        return

    if args.doctor:
        report_doctor(args)
        return

    # Resolve the XML file path
    if args.xml_file and args.windows:
        parser.error("specify either xml_file or --windows, not both")
    elif args.windows:
        xml_path = find_tuning_xml(args.windows, best_guess=args.best_guess)
        console.cprint("ok", f"Auto-detected: {xml_path}")
    elif args.xml_file:
        xml_path = args.xml_file
    else:
        # An auto-detection miss/ambiguity is an environment condition, not
        # CLI misuse — let it propagate to the top-level handler so it prints
        # as a clean error (no usage banner) that points at --help. Routing it
        # through parser.error() would slap the usage synopsis on top and exit
        # 2, framing it as a syntax error the user can't fix by reading usage.
        windows_root = autoprobe_dolby_source()
        xml_path = find_tuning_xml(windows_root, best_guess=args.best_guess)
        console.cprint("ok", f"Auto-detected: {xml_path}")

    # Handed over the moment it is known, not at the end: a run that fails
    # further down still leaves the caller able to say which file it was
    # working from.
    if resolved is not None:
        resolved["xml_path"] = xml_path

    is_soundwire = is_soundwire_xml(Path(xml_path).name)

    if args.list:
        console.cprint("head", f"Endpoints and profiles in {xml_path}:")
        list_endpoints(xml_path)
        return

    if args.dry_run:
        console.cprint("head", "Dry run: no files will be written to disk.")
    else:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        args.irs_dir.mkdir(parents=True, exist_ok=True)

    # Determine which profiles to process
    if args.all_profiles:
        profile_types = get_profile_types(xml_path, args.endpoint, args.mode)
        if not profile_types:
            console.cprint("warn", f"No profiles found for endpoint={args.endpoint} mode={args.mode}")
            return
        console.cprint("head", f"Generating presets for all {len(profile_types)} profiles: {', '.join(profile_types)}")
    else:
        profile_types = [args.profile]  # None means "first profile"

    all_preset_names = []
    # filter name → set of profile labels that emitted it. Lets the
    # end-of-run --disable hint say *which* profiles each suggestion
    # actually touches, so a user autoloading one preset isn't misled
    # into thinking a filter applies to them when it only runs in other
    # profiles.
    filters_by_profile: dict[str, set[str]] = {}
    # Findings raised across every profile built this run, in first-seen order
    # and de-duplicated by slug: --all-profiles would otherwise repeat the same
    # one nine times. The key is the slug rather than the rendered text because
    # several findings embed a per-profile value (peak-level=-3), which made
    # text-keyed de-duplication miss them.
    findings: dict[str, Finding] = {}
    # slug → profiles that raised it, so the closing block can say when one
    # applies to some profiles and not the preset the user will autoload.
    raised_in: dict[str, list[str]] = {}
    leveler_substages: dict[str, None] = {}

    for profile_type in profile_types:
        profile_label = profile_type or "default"
        # Build name base: prefix[-Mode][-Profile]
        # When --all-profiles is used, always include the profile name.
        name_parts = [args.prefix]
        if args.mode != "normal":
            name_parts.append(args.mode.title())
        if profile_type or args.all_profiles:
            safe_profile = sanitize_profile_type(profile_type or "default")
            if profile_type and safe_profile != profile_type:
                console.warn(f"sanitizing profile name {profile_type!r} -> {safe_profile!r} for use in filenames")
            name_parts.append(safe_profile.title())
        name_base = "-".join(name_parts)

        console.cprint("head", f"\n{'='*60}")
        if is_soundwire:
            # Names the practical difference — "enhanced preset generation"
            # told the reader nothing and read as either good news or a
            # warning (round 2).
            # "where your tuning enables it": this prints from the filename,
            # before any profile is parsed, and plenty of profiles disable
            # the leveler outright (voice, off, most game). The flat "on by
            # default" then contradicted the leveler section four lines
            # below, which correctly said "switched off in your tuning".
            console.cprint("head", "SoundWire speaker hardware detected — adds a "
                           "bass enhancer, and keeps the volume leveler on "
                           "where your tuning enables it")
        # "(mode=normal)" is suppressed when it is the default: an
        # unexplained internal knob on every run's second line.
        mode = "" if args.mode == "normal" else f" (mode={args.mode})"
        console.cprint("head", f"Endpoint: {args.endpoint}{mode} (the output these "
                       "presets are for)")
        tuning = parse.parse_xml(
            xml_path,
            endpoint_type=args.endpoint,
            operating_mode=args.mode,
            profile_type=profile_type,
            announce_profile=True,
        )

        # ieq-amount is a percentage: amount=10 -> the IEQ voicing is applied
        # at 10% weight on top of the audio-optimizer correction, not as a
        # full-depth EQ. DAX steers the IEQ via Media Intelligence
        # (mi-ieq-steering-enable), so a small static weight approximates its
        # steady-state; full weight (the old amount/10 reading) over-applied
        # the IEQ and crashed the HF match to DAX by up to ~28 dB. See
        # docs/design-notes.md "Finding 9".
        scale = tuning.ieq_amount / 100.0

        # Audio-optimizer curves in dB
        ao_db_left = np.array(tuning.ao_left) / parse.DB_FIXED_POINT_SCALE
        ao_db_right = np.array(tuning.ao_right) / parse.DB_FIXED_POINT_SCALE
        float_freqs = np.array(tuning.freqs, dtype=float)

        profile_findings = _report_parsed_profile(
            tuning, ao_db_left, ao_db_right, scale, disabled,
            args.volmax_slot, enabled=set(args.enable),
            is_soundwire=is_soundwire, verbose=args.verbose)

        for finding in [*tuning.findings, *profile_findings]:
            findings.setdefault(finding.slug, finding)
            raised_in.setdefault(finding.slug, []).append(profile_label)
        leveler_substages.update(dict.fromkeys(tuning.leveler_substages))

        _emit_ieq_presets(tuning, name_base, ao_db_left, ao_db_right,
                          float_freqs, scale, is_soundwire, disabled, args,
                          profile_label, all_preset_names, filters_by_profile,
                          # ⚠ hints print warn-styled above; the check
                          # verdict goes dim on those runs so green never
                          # reads as cancelling a warning (round 9).
                          warned=any(f.kind == "hint"
                                     for f in [*tuning.findings,
                                               *profile_findings]))

    # Autoload configuration
    if args.autoload and all_preset_names:
        autoload_preset = args.autoload if isinstance(args.autoload, str) else all_preset_names[0]
        sinks = _resolve_autoload_sinks(args.autoload_sink, args.dry_run)
        if sinks:
            console.cprint("head", f"\nConfiguring autoload → '{autoload_preset}':")
            verb = "Would write" if args.dry_run else "Wrote"
            for sink in sinks:
                # EasyEffects keys the autoload file on the active output route
                # description (node.name + route), not the card profile — see
                # _enumerate_audio_sinks() and issue #18. Without the route we
                # can't predict the filename EE will look for; guessing the
                # profile silently recreates #18 on classic analog cards, so
                # skip and say why rather than write a file that never matches.
                route = sink.get("route", "")
                if not route:
                    console.cprint("warn", f"  Skipping {sink['name']}: couldn't determine "
                                   "its active output route from PipeWire, which is "
                                   "what EasyEffects matches autoload on. Re-run "
                                   "with this device as the active output, or set "
                                   "the autoload profile manually in EasyEffects.")
                    continue
                path = write_autoload(
                    args.autoload_dir,
                    sink["name"],
                    sink["description"],
                    route,
                    autoload_preset,
                    dry_run=args.dry_run,
                )
                console.cprint("ok", f"  {verb} {path}")
                print(f"  Device: {sink['description'] or sink['name']} ({route})")

        # Fallback preset: neutralize the Dolby chain on any non-speaker sink
        # (HDMI, USB headset, Bluetooth, etc.) that lacks its own autoload
        # entry. Without this, EE keeps the last-loaded preset applied and
        # mangles audio on outputs the Dolby tuning wasn't designed for.
        if args.autoload_bypass:
            console.cprint("head", f"\nConfiguring fallback preset → '{BYPASS_PRESET_NAME}':")
            bypass_path, bypass_status = write_bypass_preset(
                args.output_dir, BYPASS_PRESET_NAME, dry_run=args.dry_run,
            )
            if bypass_status == "kept":
                console.cprint("ok", f"  Kept existing {bypass_path}")
            elif bypass_status == "would-write":
                console.cprint("ok", f"  Would write {bypass_path}")
            else:
                console.cprint("ok", f"  Wrote {bypass_path}")

            fallback_status, existing = set_autoload_fallback(
                DEFAULT_EASYEFFECTS_RC, BYPASS_PRESET_NAME, dry_run=args.dry_run,
            )
            if fallback_status == "already-configured":
                console.cprint("ok", f"  Fallback preset already configured "
                              f"('{existing}') in {DEFAULT_EASYEFFECTS_RC} — leaving as-is")
            elif fallback_status == "would-patch":
                console.cprint("ok", f"  Would enable fallback preset in {DEFAULT_EASYEFFECTS_RC}")
            else:
                console.cprint("ok", f"  Enabled fallback preset in {DEFAULT_EASYEFFECTS_RC}")
                if easyeffects_is_running():
                    console.cprint("warn", "  EasyEffects is currently running — restart it for "
                                   "the fallback setting to take effect (EE rewrites "
                                   "this file on exit).")

        # Autoload only persists across logins if EasyEffects both starts at
        # login (autostart) and stays alive in the background (service mode);
        # nudge toward the prefs, but only when one is off so the fully
        # configured case stays quiet.
        try:
            _rc_text = DEFAULT_EASYEFFECTS_RC.read_text(encoding="utf-8")
        except OSError:
            _rc_text = ""
        _rc = read_ee_rc(_rc_text)
        if not (_rc.get("autostart_on_login") and _rc.get("service_mode")):
            console.cprint("warn", "  Tip: enable Background Service + Autostart on login in "
                           "EasyEffects' preferences so this autoloads on every login.")

    # A requested --enable that never produced an active stage is silent
    # otherwise: make_autogain returns None when the XML's volume leveler is
    # disabled, so the flag can't do anything and the preset is unchanged.
    # First of the closing output because it answers something the user typed,
    # rather than something we noticed.
    if "autogain" in args.enable and "autogain-active" not in filters_by_profile:
        print()
        console._cprint_wrapped("warn", "--enable autogain had no effect: this "
                                "tuning's volume leveler is disabled in the "
                                "XML, so there is no leveler stage to "
                                "activate. The preset is unchanged.")
    if ("coupled-bands" in args.enable
            and "coupled-bands-active" not in filters_by_profile):
        print()
        console._cprint_wrapped("warn", "--enable coupled-bands had no effect: this "
                                "tuning's regulator has no 0 dBFS zone whose "
                                "bands are all marked non-isolated "
                                "(isolated_band), so there is nothing to "
                                "couple in. The preset is unchanged.")

    # Environment blockers first within the troubleshooting band: each means
    # the system won't play this correctly whatever the preset says, so there
    # is no point offering filter tweaks above them.
    #
    # Some laptops gate their woofers behind a smart-amp firmware-load ALSA
    # control (issue #17). Only relevant when tuning the internal speakers —
    # irrelevant for headphone/other endpoints.
    if args.endpoint == "internal_speaker":
        gate_finding = warn_speaker_firmware_gate(
            detect_speaker_firmware_gates())
        if gate_finding is not None:
            findings.setdefault(gate_finding.slug, gate_finding)
        # A hidden woofer pin leaves half the speakers unconfigured, so the
        # preset shapes the tweeters alone (issue #53). Gathering speaker info
        # is a handful of /proc reads; only reached on the speaker endpoint.
        speaker_info = _gather_speaker_pins()
        pin_finding = warn_hidden_speaker_pin(
            find_hidden_speaker_pin(speaker_info), speaker_info)
        if pin_finding is not None:
            findings.setdefault(pin_finding.slug, pin_finding)
        # The negative signal: no fixup exists for this machine, so we can't
        # tell a hidden woofer from a plain stereo pair. Only its owner can.
        count_finding = unlisted_speaker_pin_finding(speaker_info)
        if count_finding is not None:
            _print_finding_detail(count_finding)
            findings.setdefault(count_finding.slug, count_finding)
        # An old kernel can mis-configure the speaker path below any preset
        # (issue #33) — hint at it, softly, when the series is old.
        warn_old_kernel()

    # Proactively flag an EasyEffects install that can't use what we just wrote
    # — the failure mode #22 surfaced (a correct preset silently inaudible
    # because of the environment, e.g. EE 7 or a wrong install location).
    # Silent on the happy path; reuses --doctor's probes.
    if not args.skip_ee_check:
        warn_ee_environment(args)

    # The two findings raised after the per-profile loop rather than inside
    # it. They have no mid-run site to report from, so their detail prints
    # here, where they are worked out; only their one-line ask goes on to the
    # closing block.
    #
    # Experimental emissions are numerically verified but have never been
    # confirmed by ear, and a user with an affected device is the only way
    # that changes — so they ask rather than merely announcing themselves.
    fired = [k for k in EXPERIMENTAL_MARKERS if k in filters_by_profile]
    experimental = [EXPERIMENTAL_MARKERS[k] for k in fired]
    if experimental:
        # Only the markers that are also --disable names give the user an A/B;
        # "mbc-1band" and "coupled-bands-active" have no flag of their own.
        findings.setdefault("unconfirmed-by-ear", _experimental_finding(
            ", ".join(experimental),
            [k for k in fired if k in DISABLEABLE_FILTERS]))
        _print_finding_detail(findings["unconfirmed-by-ear"])

    # Gated on the leveler actually running, not on the flag being passed:
    # --enable autogain does nothing when the XML disables the leveler, and
    # escalating on the flag alone contradicted the "had no effect" warning
    # printed a few lines above on exactly those devices.
    substage_finding = _leveler_gap_finding(
        list(leveler_substages),
        autogain_on="autogain-active" in filters_by_profile,
        # "autogain" is the marker for a leveler that shipped bypassed but
        # could be switched on; absent means the XML disabled it outright —
        # or that --disable autogain cleared it, which the flag branch owns
        # so the tuning doesn't get blamed for the reader's own choice.
        autogain_available="autogain" in filters_by_profile,
        disabled_by_flag="autogain" in args.disable)
    if substage_finding is not None:
        findings.setdefault(substage_finding.slug, substage_finding)
        _print_finding_detail(substage_finding)

    # Stamp the scope on last, once every profile has been seen. Findings
    # raised everywhere carry none, so a single-profile run — the default —
    # never shows one.
    def _scope(finding):
        seen = list(dict.fromkeys(raised_in.get(finding.slug, [])))
        if not seen or len(seen) == len(profile_types):
            return finding
        # Naming them beats counting them right up until the list is longer
        # than the sentence it annotates; nine profiles listed in full is
        # noise where "6 of 9 profiles" is the same answer.
        label = (", ".join(seen) if len(seen) <= 3
                 else f"{len(seen)} of {len(profile_types)} profiles")
        return replace(finding, scope=label)

    scoped = [_scope(f) for f in findings.values()]

    # A wrapper takes the menu along with the closing ask (round 4: printed
    # at [1/3] it told the reader what to re-run before setup had finished,
    # with two more phases of output below it) — stashed here, printed by
    # the wrapper at its own end.
    menu_printed = False
    if troubleshooting is not None:
        troubleshooting.update(
            findings=scoped,
            filters_by_profile=filters_by_profile,
            enabled_by_flag=frozenset(args.enable))
    else:
        menu_printed = print_troubleshooting(
            scoped, filters_by_profile,
            installs_presets=not args.skip_closing,
            enabled_by_flag=frozenset(args.enable),
            dry_run=args.dry_run)
    # After the troubleshooting, not before it. Printed first, the success
    # line and "how to use them" scrolled off the top of a 24-line terminal
    # and the last thing on screen was troubleshooting advice and a
    # bug-report link — which reads as though the run failed.
    # Suppressed for a wrapper along with the closing ask: it stages presets
    # into a tempdir it deletes on the way out, so "wrote 3 presets to
    # /tmp/…, open EasyEffects and pick one" named a directory that no longer
    # existed — and under the wrapper's --dry-run it also contradicted its
    # own "nothing was written" two lines later.
    if not args.skip_closing:
        # Single-mode runs only: under --all-profiles every mode was built,
        # so there is nothing to point at. get_profile_types re-reads the
        # XML, but only here, once, at the very end.
        profile_used = n_modes = None
        if not args.all_profiles and len(profile_types) == 1:
            profile_used = tuning.profile_used
            n_modes = len(get_profile_types(xml_path, args.endpoint,
                                            args.mode))
        print_what_now(all_preset_names, bool(args.autoload), args.dry_run,
                       output_dir=args.output_dir,
                       profile_used=profile_used, n_modes=n_modes or 0,
                       default_unknown=(args.profile is None
                                        and tuning.default_profile is None),
                       # "autogain" marker = leveler present but bypassed
                       # (the --enable-menu state); -active = running.
                       autogain_off=("autogain" in filters_by_profile
                                     and "autogain-active"
                                     not in filters_by_profile),
                       menu_printed=menu_printed,
                       declared_default=(tuning.default_profile
                                         if args.profile is None else None))

    # Last, so the link is still on screen when the run ends. A wrapper that
    # keeps running after us takes the block instead and prints it at its own
    # end — always collected, so nothing is lost either way.
    if closing is not None:
        closing.extend(scoped)
    if not args.skip_closing:
        print_project_asks(scoped, dry_run=args.dry_run, xml_path=xml_path)


def run_cli(argv: list[str] | None = None,
            closing: list[Finding] | None = None,
            troubleshooting: dict | None = None,
            resolved: dict | None = None,
            staged: bool = False) -> int:
    """main() with the top-level error handling the __main__ block used to
    inline, as a return code — the seam dolby_to_pipewire.py calls in-process."""
    try:
        main(argv, closing=closing, troubleshooting=troubleshooting,
             resolved=resolved, staged=staged)
    except (FileNotFoundError, RuntimeError, ValueError, ET.ParseError) as e:
        console.cprint("err", f"Error: {e}")
        console.cprint("cta", "Run with --help to see usage and all options.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_cli())
