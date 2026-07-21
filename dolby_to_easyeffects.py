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
  - equalizer#0: speaker PEQ bells + high-pass (parametric filters from Dolby)
  - equalizer#1: dialog enhancer (speech presence boost from dialog-enhancer settings)
  - autogain#0: volume leveler (from volume-leveler settings)
  - multiband_compressor#0: dynamics processing (from mb-compressor-tuning)
  - multiband_compressor#1: per-band limiter (from regulator-tuning)
  - limiter#0: brickwall output limiter (safety net)
"""

import argparse
import configparser
import contextlib
import json
import math
import os
import re
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from _version import get_version

try:
    from rich.console import Console
    from rich.theme import Theme
    _CONSOLE = Console(
        theme=Theme({
            "err":  "bold red",
            "head": "bold cyan",
            "ok":   "green",
            "warn": "yellow",
            "cta":  "bold magenta",
            "dim":  "dim",
        }),
        markup=False,
        highlight=False,
    )
except ImportError:
    _CONSOLE = None

try:
    from rich_argparse import RichHelpFormatter as _HelpFormatter
except ImportError:
    _HelpFormatter = argparse.HelpFormatter

_MISSING_COLOR_DEPS = []
if _CONSOLE is None:
    _MISSING_COLOR_DEPS.append("rich")
if _HelpFormatter is argparse.HelpFormatter:
    _MISSING_COLOR_DEPS.append("rich-argparse")


def cprint(style: str, text: str = "") -> None:
    """Print `text` in the given semantic style, or plain if rich is absent."""
    if _CONSOLE is None:
        print(text)
        return
    _CONSOLE.print(text, style=style)


def warn(msg: str) -> None:
    """Emit a contextual detail warning with the standard ``  Warning: ``
    prefix, so per-band / per-filter / per-profile warnings read uniformly.
    Section-level warnings that want their own blank-line spacing call cprint
    directly."""
    cprint("warn", f"  Warning: {msg}")


def _disable_color() -> None:
    global _CONSOLE
    _CONSOLE = None

_FLATPAK_APP_ID = "com.github.wwmm.easyeffects"
_FLATPAK_BASE = Path.home() / ".var" / "app" / _FLATPAK_APP_ID / "config" / "easyeffects"
_NATIVE_BASE = Path.home() / ".local" / "share" / "easyeffects"


def _prefer_flatpak() -> bool:
    """Choose between Flatpak and native EasyEffects install locations.

    Prefers whichever install has a data directory (i.e. has been run
    at least once). If neither has been run, probes Flatpak app install
    roots so a freshly-installed-but-unopened Flatpak still picks the
    Flatpak paths. On systems with both installed and both launched,
    preserves the prior default (Flatpak wins).
    """
    if _FLATPAK_BASE.exists():
        return True
    if _NATIVE_BASE.exists():
        return False
    for root in (
        Path("/var/lib/flatpak/app"),
        Path.home() / ".local" / "share" / "flatpak" / "app",
    ):
        if (root / _FLATPAK_APP_ID).exists():
            return True
    return False


_USE_FLATPAK = _prefer_flatpak()
_EASYEFFECTS_BASE = _FLATPAK_BASE if _USE_FLATPAK else _NATIVE_BASE

DEFAULT_OUTPUT_DIR = _EASYEFFECTS_BASE / "output"
DEFAULT_IRS_DIR = _EASYEFFECTS_BASE / "irs"
DEFAULT_AUTOLOAD_DIR = _EASYEFFECTS_BASE / "autoload" / "output"

# EasyEffects 8.x KConfig file. Separate from _EASYEFFECTS_BASE (which is
# under XDG_DATA_HOME for presets/IRs); this one is under XDG_CONFIG_HOME.
_FLATPAK_RC = Path.home() / ".var" / "app" / _FLATPAK_APP_ID / "config" / "easyeffects" / "db" / "easyeffectsrc"
_NATIVE_RC = Path.home() / ".config" / "easyeffects" / "db" / "easyeffectsrc"
DEFAULT_EASYEFFECTS_RC = _FLATPAK_RC if _USE_FLATPAK else _NATIVE_RC

BYPASS_PRESET_NAME = "Nothing"

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


@dataclass
class FirmwareGate:
    """A smart-amp firmware-load ALSA control that gates the speakers.

    On laptops whose woofers run through a TI TAS2563/2781 smart amplifier,
    the firmware does not auto-load and the amp stays muted until an ALSA
    control ("Speaker Force Firmware Load") is switched on (issue #17). This
    is a kernel/ALSA-side gate — nothing in the DAX XML hints at it — so the
    preset can be perfect while the bass speakers are silent.
    """
    card_index: str      # ALSA card index, e.g. "0"
    card_id: str         # ALSA card short id, e.g. "sofhdadsp" (stable across boots)
    numid: str           # control numid, e.g. "3"
    name: str            # control name, e.g. "Speaker Force Firmware Load"
    on: bool             # current state


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
                channels=2 if "Stereo" in block.split("\n", 1)[0] else 1,
            ))


# A smart-amp firmware-load gate is an ALSA control (not a DAX field) that
# must be on before TI TAS2563/2781 amplifiers will drive the woofers.
# Matched by name: "Speaker Force Firmware Load" is the one Lenovo laptops
# expose (issue #17); the pattern is loosened to the "...Force Firmware Load"
# family so sibling controls match too. Extend here if more turn up.
_FIRMWARE_GATE_NAME_RE = re.compile(r"force firmware load", re.I)
_AMIXER_CONTROL_HEAD_RE = re.compile(r"numid=(\d+),.*?name='([^']*)'")


def parse_firmware_gate_controls(amixer_contents: str) -> list[tuple[str, str, bool]]:
    """Extract firmware-load gate controls from ``amixer -c N contents`` text.

    Each control prints as a block:

        numid=3,iface=MIXER,name='Speaker Force Firmware Load'
          ; type=BOOLEAN,access=rw------,values=1
          : values=off

    Returns ``(numid, name, on)`` per name-matched control. Pure text parsing
    so it can be unit-tested without hardware.
    """
    gates: list[tuple[str, str, bool]] = []
    for block in re.split(r"(?=^numid=)", amixer_contents, flags=re.MULTILINE):
        head = _AMIXER_CONTROL_HEAD_RE.match(block)
        if not head:
            continue
        numid, name = head.group(1), head.group(2)
        if not _FIRMWARE_GATE_NAME_RE.search(name):
            continue
        val = re.search(r":\s*values=(\w+)", block)
        on = val is not None and val.group(1).lower() in ("on", "1", "true")
        gates.append((numid, name, on))
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
        for numid, name, on in parse_firmware_gate_controls(result.stdout):
            gates.append(FirmwareGate(
                card_index=idx, card_id=card_id, numid=numid, name=name, on=on,
            ))
    return gates


def warn_speaker_firmware_gate(gates: list[FirmwareGate]) -> None:
    """Warn — with copy-paste fixes — about any firmware-load gate that's off.

    Silent when no gate is off (the gate is either absent or already enabled,
    so the speakers aren't muted on its account).
    """
    off = [g for g in gates if not g.on]
    if not off:
        return
    g0 = off[0]  # representative gate for the verify examples

    cprint("warn", f"\n{'=' * 60}")
    cprint("warn", "⚠  Smart-amp firmware gate is OFF — your bass/woofer speakers")
    cprint("warn", "   may be silent even though the EasyEffects preset is correct.")
    cprint("dim", "Many laptops drive their woofers through a TI TAS2563/2781 smart")
    cprint("dim", "amplifier whose firmware does not auto-load; the amp stays muted")
    cprint("dim", "upstream of the preset until this ALSA control is switched on.")
    print()
    # Enable now: no root needed — the active logind session already holds an
    # ACL on /dev/snd/control*. Persist with `alsactl store`, which saves the
    # state that alsa-restore.service replays at boot (the standard ALSA path).
    cprint("dim", "1. Enable it now (no root needed) and listen for the bass to return:")
    for g in off:
        cprint("cta", f"     amixer -c {g.card_id} cset name='{g.name}' on")
    print()
    cprint("dim", "2. If that worked, persist it across reboots — saves the ALSA state")
    cprint("dim", "   that alsa-restore replays at boot:")
    cprint("cta", "     sudo alsactl store")
    print()
    cprint("dim", "   (If it still doesn't survive a reboot — alsa-restore can race the")
    cprint("dim", "   driver on some setups — fall back to a systemd --user oneshot that")
    cprint("dim", "   runs the amixer command above at login.)")
    print()
    cprint("dim", "3. Self-check — confirm the control stuck and the firmware loaded:")
    cprint("cta", f"     amixer -c {g0.card_id} cget name='{g0.name}'")
    cprint("cta", "     journalctl -k -b | grep -iE 'tas2|firmware'")
    cprint("cta", "     ls -l /lib/firmware/TAS2*.bin")
    cprint("dim", "   (no journal access? try:  sudo dmesg | grep -i tas2)")
    print()
    cprint("dim", "   Still no bass, and the log shows 'Direct firmware load for")
    cprint("dim", "   TAS2XXX….bin failed' or no such file exists? The per-device blob")
    cprint("dim", "   is missing — distro linux-firmware lags newer laptops. Extract it")
    cprint("dim", "   from your Windows audio driver or TI's TAS2781-LINUX package and")
    cprint("dim", "   drop it into /lib/firmware.")
    print()
    # Keep the feedback ask (it gates whether we automate this) but as a dim
    # line, not a second call-to-action — main()'s general "report back" CTA
    # right below is the single prominent one, and covers this too.
    cprint("dim", "Did toggling this fix your bass (or not)? A note on issue #17 lets us")
    cprint("dim", "gauge whether to automate it — use the report-back link just below.")


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

    # Speaker detection — branch by bus type
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
    for g in info.firmware_gates:
        mark = "" if g.on else "⚠ "
        state = "on" if g.on else "OFF — bass speakers may be silent"
        lines.append(f"  {mark}{g.name}: {state} (card {g.card_id})")

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
    elif info.bus_type == "hda" and info.speakers:
        sections.append(("HDA internal speakers", [
            f"  {s.node}: {s.control_name} ({s.role}, {'stereo' if s.channels == 2 else 'mono'})"
            for s in info.speakers
        ]))

    # PCM playback devices
    sections.append(("PCM playback devices",
                      [f"  pcm{dev}p: {name}" for dev, name in info.pcm_devices]))

    # Merged, bus-agnostic amplifier status: per-amp bind/channels/runtime, the
    # #17 TI firmware gate, driver-keyed firmware presence, and kernel-log
    # evidence — one section, kept terse (detail only when something's wrong).
    sections.append(("Speaker amplifier status", _amp_status_lines(info)))

    # Speaker layout estimate
    sections.append(("Speaker layout estimate", [f"  {info.layout_summary}"]))

    for title, lines in sections:
        cprint("head", f"=== {title} ===")
        print("\n".join(lines))
        print()


def report_speaker_info():
    """Report detected audio hardware and speaker layout."""
    # Version-stamp the block: users paste this verbatim into the device-report
    # issue form, so the maintainer can see which build was tested.
    cprint("head", f"speaker-tuning-to-easyeffects {get_version()}")
    print()
    info = _gather_speaker_info()
    _print_speaker_info(info)


# --- Environment self-diagnostics (--doctor) ---------------------------------
# A generated preset can be flawless yet inaudible because of the *environment*
# it lands in: EasyEffects 7 (which can't read the v8 preset format), presets
# written to the Flatpak path while EE runs native (or vice-versa), a missing
# impulse file so the speaker-correction convolver loads nothing, no Dolby
# preset selected, or a kernel series so old it mis-drives the speaker
# amplifier itself (issue #33). --doctor surfaces those deterministically (#22),
# and warn_ee_environment() reuses the same probes to warn at the end of a
# normal run. The pure helpers below take plain inputs so they're unit-tested
# without touching the system; the _probe_/_gather_ wrappers do the I/O.

DOCTOR_PASS, DOCTOR_WARN, DOCTOR_FAIL, DOCTOR_UNKNOWN = "PASS", "WARN", "FAIL", "?"

# EE names stacked instances of a plugin "convolver#0", "equalizer#1", … —
# match the speaker-correction convolver regardless of its index. Keep the
# "kernel-name" literal in step with make_convolver().
_CONVOLVER_KEY_RE = re.compile(r"^convolver#\d+$")


@dataclass
class CheckResult:
    """One diagnostic line: a status, a short label, and an actionable detail."""
    status: str          # DOCTOR_PASS / WARN / FAIL / UNKNOWN
    label: str
    detail: str


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


def ee_version_status(version: tuple[int, int, int] | None,
                      found: bool) -> CheckResult:
    """Verdict for the EasyEffects version. FAIL — the only loud error — is
    reserved for a *cleanly parsed* major < 8, so an EE-8 user is never told
    they're on 7. ``found`` distinguishes "no EE at all" (a valid
    generating-for-another-machine case → WARN) from "installed but version
    unreadable" (→ UNKNOWN)."""
    if version is None:
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
            f"{vstr} detected — these presets need EasyEffects 8. Version 8 "
            f"changed the preset format; on {vstr} the speaker-correction filter "
            "loads nothing, so you'll hear little or no difference. Install "
            "EasyEffects 8 (the Flathub Flatpak, or your distro's package if it "
            "ships 8.x).")
    return CheckResult(DOCTOR_PASS, "EasyEffects version", f"{vstr} (compatible).")


# Upstream release month per kernel series (issue #33: a preset can be perfect
# while an *old kernel* mis-drives the speaker amp — that report was fixed by a
# 6.12→7.0 kernel upgrade, not a preset change). Month precision is enough for
# an age hint. Dates are historical facts, so an aging copy of this tool still
# ages old kernels correctly; a series newer than the table is assumed recent.
# Append new series as they release.
_KERNEL_SERIES_RELEASES = {
    (5, 10): "2020-12", (5, 11): "2021-02", (5, 12): "2021-04",
    (5, 13): "2021-06", (5, 14): "2021-08", (5, 15): "2021-10",
    (5, 16): "2022-01", (5, 17): "2022-03", (5, 18): "2022-05",
    (5, 19): "2022-07", (6, 0): "2022-10", (6, 1): "2022-12",
    (6, 2): "2023-02", (6, 3): "2023-04", (6, 4): "2023-06",
    (6, 5): "2023-08", (6, 6): "2023-10", (6, 7): "2024-01",
    (6, 8): "2024-03", (6, 9): "2024-05", (6, 10): "2024-07",
    (6, 11): "2024-09", (6, 12): "2024-11", (6, 13): "2025-01",
    (6, 14): "2025-03", (6, 15): "2025-05", (6, 16): "2025-07",
    (6, 17): "2025-09", (6, 18): "2025-11", (6, 19): "2026-02",
    (7, 0): "2026-04", (7, 1): "2026-06",
}

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
    released = _KERNEL_SERIES_RELEASES.get(series)
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
    if series > max(_KERNEL_SERIES_RELEASES):
        return CheckResult(DOCTOR_PASS, label,
            f"{sstr} — newer than any series this tool knows about.")
    aged = _kernel_series_age(series, today)
    if aged is None:
        if series < min(_KERNEL_SERIES_RELEASES):
            return CheckResult(DOCTOR_WARN, label,
                f"{sstr} is very old (pre-2021). Laptop speaker-amp support "
                "lands kernel-side; strongly consider a newer kernel.")
        return CheckResult(DOCTOR_UNKNOWN, label, f"{sstr} — unknown series.")
    released, months = aged
    if months <= _KERNEL_OLD_MONTHS:
        plural = "" if months == 1 else "s"
        return CheckResult(DOCTOR_PASS, label,
            f"{sstr} (released {released}, ~{months} month{plural} old).")
    return CheckResult(DOCTOR_WARN, label,
        f"{sstr} was released {released} (~{months} months ago). Speaker-amp "
        "fixes (new amp drivers, power-management quirks) land kernel-side and "
        "are not always backported to older series — if your speakers sound "
        "thin, muffled or garbled even with EasyEffects off, a newer kernel "
        "(your distro's backports or hardware-enablement/HWE kernel) may fix "
        "that.")


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


def _doctor_summary(checks) -> tuple[int, int, int, int]:
    """Count (FAIL, WARN, PASS, UNKNOWN) across the checks. UNKNOWN is counted
    so an unverifiable run isn't silently summarised as clean."""
    fail = sum(1 for c in checks if c.status == DOCTOR_FAIL)
    warn = sum(1 for c in checks if c.status == DOCTOR_WARN)
    ok = sum(1 for c in checks if c.status == DOCTOR_PASS)
    unknown = sum(1 for c in checks if c.status == DOCTOR_UNKNOWN)
    return fail, warn, ok, unknown


def _flatpak_version_text(info_output: str) -> str:
    """Pull just the ``Version:`` line out of `flatpak info` output. The full
    blob has other numeric tokens (sizes, refs) that would mis-parse, so we
    isolate the one line; absent → "" (→ UNKNOWN, never a wrong version)."""
    for line in info_output.splitlines():
        if line.strip().lower().startswith("version:"):
            return line
    return ""


def _probe_ee_version() -> tuple[tuple[int, int, int] | None, bool, str, bool | None]:
    """Probe the installed EasyEffects version. Read-only, time-bounded, never
    raises. Returns (version|None, found, source, ee_is_flatpak).

    Probes the install the script writes to (per _USE_FLATPAK) first, then the
    other, and prefers a *parseable* version over a found-but-unreadable answer
    — so a stale/shim binary on one install can't mask a healthy version on the
    other (issue #22 review). ``found`` means an EE binary actually answered, so
    version=None with found=True means 'installed but version unreadable'."""
    def run(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except (subprocess.SubprocessError, OSError):
            return None
        return ((r.stdout or "") + "\n" + (r.stderr or "")) if r.returncode == 0 else None

    def native():
        out = run(["easyeffects", "--version"])
        return (parse_ee_version(out), True) if out is not None else (None, False)

    def flatpak():
        out = run(["flatpak", "info", _FLATPAK_APP_ID])
        return (parse_ee_version(_flatpak_version_text(out)), True) if out is not None else (None, False)

    probes = ([(True, flatpak), (False, native)] if _USE_FLATPAK
              else [(False, native), (True, flatpak)])
    fallback = (None, False, "", None)   # best found-but-unparseable, in order
    for is_flatpak, probe in probes:
        version, found = probe()
        if not found:
            continue
        src = "flatpak info" if is_flatpak else "easyeffects --version"
        if version is not None:
            return version, True, src, is_flatpak
        if not fallback[1]:              # remember the first install that answered
            fallback = (None, True, src, is_flatpak)
    return fallback


def _gather_doctor_report(output_dir: Path, irs_dir: Path, rc_path: Path,
                          custom_dirs: bool = False) -> DoctorReport:
    """Run every probe and assemble a DoctorReport. All I/O is wrapped so a
    missing binary / unreadable file degrades to a soft line, never a crash."""
    report = DoctorReport()

    # 1. EasyEffects version / compatibility
    version, found, source, ee_is_flatpak = _probe_ee_version()
    report.checks.append(ee_version_status(version, found))

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

    # 6. Kernel age — speaker-amp fixes land kernel-side (issue #33)
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


def _print_doctor_report(report: DoctorReport) -> None:
    """Print a compact, paste-safe diagnostic report."""
    style = {DOCTOR_PASS: "ok", DOCTOR_WARN: "warn",
             DOCTOR_FAIL: "err", DOCTOR_UNKNOWN: "dim"}

    def emit(c):
        cprint(style.get(c.status, "dim"), f"  [{c.status:^4}] {c.label}")
        for line in textwrap.wrap(c.detail, width=72):
            cprint("dim", f"         {line}")

    cprint("head", f"speaker-tuning-to-easyeffects {get_version()}")
    cprint("head", "=== EasyEffects doctor ===")
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
                    cprint("ok", f"  [{DOCTOR_PASS:^4}] Presets "
                                 f"({ok_n}/{len(preset_checks)} load their impulse file)")
                for pc in preset_problems:
                    emit(pc)
            continue
        emit(c)
    print()
    fail, warn, ok, unknown = _doctor_summary(report.checks)
    parts = [f"{fail} FAIL", f"{warn} WARN", f"{ok} PASS"]
    if unknown:
        parts.append(f"{unknown} UNKNOWN")
    summary = "Summary: " + ", ".join(parts)
    cprint("err" if fail else ("warn" if (warn or unknown) else "ok"), summary)
    print()

    # Raw probed facts — always shown so an issue can be diagnosed remotely even
    # when a heuristic verdict is UNKNOWN or wrong.
    f = report.facts
    cprint("head", "=== Environment (paste this into your issue) ===")
    print(f"  Tool:         speaker-tuning-to-easyeffects {get_version()}")
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
    if not fail and not unknown:
        cprint("ok", "No blocking problems detected.")
    elif unknown and not fail:
        cprint("warn", "Some checks couldn't be verified (the [ ? ] lines above); "
                       "the rest look OK.")
    cprint("dim", "If you still hear no difference between the preset and bypass:")
    cprint("dim", "  • In EasyEffects, toggle the preset off/on to A/B it.")
    cprint("dim", "  • Make sure global bypass (the power-button icon, top bar) is OFF.")
    cprint("dim", "  • Confirm system output is the speaker sink and volume is up.")
    print()

    if report.speaker_info is not None:
        _print_speaker_info(report.speaker_info)

    cprint("cta", "Still stuck? Paste everything above into an issue:")
    cprint("cta", f"  {_REPORT_FORM_URL}")


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
    version, found, _source, ee_is_flatpak = _probe_ee_version()
    ver = ee_version_status(version, found)

    if ver.status == DOCTOR_FAIL:
        vstr = ".".join(str(x) for x in version)
        cprint("err", f"\n{'=' * 60}")
        cprint("err", f"⚠  EasyEffects {vstr} detected — these presets need EasyEffects 8.")
        print()
        cprint("dim", "EasyEffects changed its preset (filter-chain) format in version 8,")
        cprint("dim", "and the presets this tool generates use the new format. On version 7")
        cprint("dim", "they won't load correctly — in particular the speaker-correction")
        cprint("dim", "filter loads nothing, so you'll hear little or no difference.")
        print()
        cprint("dim", "To fix, install EasyEffects 8:")
        cprint("cta", "  • Easiest on any distro — the Flathub Flatpak:")
        cprint("cta", "      flatpak install flathub com.github.wwmm.easyeffects")
        cprint("dim", "  • Or your distro's own package if it already ships 8.x")
        cprint("dim", "    (Debian trixie, Ubuntu 24.04+ and Fedora ≤43 still ship 7.x).")
        return

    if not found:
        cprint("warn", "\n⚠  Couldn't find EasyEffects — install version 8 to use these "
                       "presets (e.g. the Flathub Flatpak). Ignore if you're "
                       "generating for another machine.")

    # Install-location mismatch (only meaningful for the default EE dirs): the
    # detected EE build differs from where we wrote. Warn so the user can point
    # --output-dir/--irs-dir at the install they actually run.
    if (args.output_dir == DEFAULT_OUTPUT_DIR and args.irs_dir == DEFAULT_IRS_DIR
            and ee_is_flatpak is not None and ee_is_flatpak != _USE_FLATPAK):
        run_where = "Flatpak" if ee_is_flatpak else "native"
        where = "Flatpak" if _USE_FLATPAK else "native"
        cprint("warn", f"\n⚠  Presets were written to the {where} EasyEffects "
                       f"location, but the {run_where} install was detected — if "
                       "that's the one you use, it won't see them (run --doctor).")


def warn_old_kernel(release: str | None = None) -> None:
    """End-of-run hint: an old kernel series can mis-drive laptop speaker
    amplifiers no matter how good the preset is — issue #33 was fixed by a
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

    cprint("warn", f"\n⚠  Your kernel series {sstr} is old{when}.")
    cprint("dim", "Laptop speaker amplifiers often need kernel-side fixes (new amp")
    cprint("dim", "drivers, power-management quirks) that land in newer kernels and")
    cprint("dim", "are not always backported to older series. If your speakers sound")
    cprint("dim", "thin, muffled or garbled even with EasyEffects disabled, a newer")
    cprint("dim", "kernel (your distro's backports or hardware-enablement/HWE kernel)")
    cprint("dim", "may fix that.")


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
            cprint("ok", f"Auto-detected Windows mount: {winner}")
        else:
            cprint("ok", f"Auto-detected extracted DriverStore: {winner}")

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
            warn(
                "Multiple tunings match the codec subsystem but none match its "
                "device id; selecting the highest tuning_version. Pass the XML "
                "path explicitly if the result sounds wrong."
            )

    # No exact (man, part) / HDA / Apple match: accept the PCI-subsystem fallback.
    if not candidates and sdw_pci_only:
        candidates = sdw_pci_only
        if len(sdw_pci_only) > 1:
            warn(
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
            cprint("ok", f"Matched tuning XML (by security-key PCI subsystem): {path}")
            return path

        if best_guess and len(guesses) == 1:
            path, man, subsys = guesses[0]
            warn(
                f"--best-guess: no exact hardware match; using the only "
                f"internal-speaker tuning for manufacturer {man} — {path.name} "
                f"(SUBSYS_{subsys}). Unverified: matched by manufacturer only, "
                f"not by device id."
            )
            cprint("ok", f"Matched tuning XML (best-guess): {path}")
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
        cprint("head", "Multiple matching XMLs found, using highest tuning version:")
        for i, (c, _version, ver) in enumerate(ranked):
            if i == 0:
                cprint("ok", f"  → {c} (tuning_version={ver})")
            else:
                print(f"    {c} (tuning_version={ver})")
    else:
        cprint("ok", f"Matched tuning XML: {candidates[0]}")

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
        cprint("dim", f"  [{i}] {_sink_diag_line(s)}")


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
        cprint("warn", f"  Not a number: {raw!r} — skipping autoload.")
        return None
    if not (1 <= idx <= len(candidates)):
        cprint("warn", f"  Out of range: {idx} — skipping autoload.")
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
                cprint("warn", f"  --autoload-sink {name!r}: not currently in "
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
        cprint("warn", "\nNo sink is tagged as an internal speaker "
                       "(device.icon_name=audio-speakers).")
        if len(candidates) == 1:
            sink = candidates[0]
            cprint("warn", "  Falling back to the only internal analog output found:")
            cprint("dim", f"    {_sink_diag_line(sink)}")
            cprint("dim", "  If this is wrong, re-run with --autoload-sink <node.name>.")
            return [sink]
        # Ambiguous: list, then prompt on a TTY (never under --dry-run).
        cprint("warn", f"  Found {len(candidates)} internal analog sinks:")
        _print_sink_candidates(candidates)
        chosen = None if dry_run else _prompt_pick_sink(candidates)
        if chosen is not None:
            return [chosen]
        cprint("dim", "  Re-run with --autoload-sink <node.name> (repeatable) to choose.")
        return []

    # tier == "none"
    all_sinks = sel["all_sinks"]
    if not all_sinks:
        cprint("warn", "\nWarning: no Audio/Sink nodes found via pw-dump; "
                       "cannot configure autoload.")
        cprint("dim", "  Is PipeWire running? Run this from your logged-in "
                      "desktop session.")
    else:
        cprint("warn", "\nWarning: no internal-speaker sink found (none tagged "
                       "device.icon_name=audio-speakers, and no internal analog "
                       "output).")
        cprint("head", "  Audio/Sink nodes seen:")
        _print_sink_candidates(all_sinks)
        cprint("dim", "  Re-run with --autoload-sink <node.name> to bind autoload manually.")
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
        "_generator": f"dolby_to_easyeffects.py {get_version()}",
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


def resolve_channel_or_direct(element, constants):
    """Resolve a CSV array that may live directly on ``element`` or on a
    per-channel ``<ch_00>..<ch_07>`` sub-element.

    Older/flat DAX3 regulator tunings put the array directly on
    ``threshold_high``/``threshold_low`` via ``value=``/``preset=``. The newer
    SoundWire schema (e.g. ``SUBSYS_37A317AA``) nests it per channel instead::

        <threshold_high>
          <ch_00 value="-282,-294,..." />
          <ch_01 value="-282,-294,..." />
          <ch_02 preset="array_20_zero" /> ...
        </threshold_high>

    Returns the direct value when present, otherwise the ``ch_00`` channel
    (resolved through the same ``value=``/``preset=`` mechanism as the audio
    optimizer's ch_00/ch_01), otherwise "". ``ch_00`` is the stereo-limiter
    reference; callers warn if ``ch_01`` diverges.
    """
    direct = resolve_xml_value(element, constants)
    if direct:
        return direct
    if element is None:
        return ""
    ch0 = element.find("ch_00")
    if ch0 is not None:
        return resolve_xml_value(ch0, constants)
    return ""


def _int_attr(element, default=None, name="value"):
    """Read an integer ``name=`` attribute, degrading to ``default``.

    Returns ``default`` when ``element`` is None or the attribute is absent
    or empty. Centralises the ``int(el.get("value"))`` idiom, which
    otherwise raises ``TypeError`` on a present element with a missing or
    blank ``value=`` — a plausible hand-edited or schema-variant shape that
    the CLI top-level did not catch. A present, non-empty but non-integer
    value still raises ``ValueError`` (surfaced cleanly by the CLI handler).
    """
    if element is None:
        return default
    raw = element.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass
class ParsedTuning:
    """Everything parse_xml extracts from one DAX3 endpoint/profile.

    Field order matches the legacy 12-tuple this replaced. Values are a mix of
    raw schema ints (freqs, curves, ao_left/ao_right, ieq_amount) and
    already-dB-scaled fields (vol_leveler, surround, volmax_boost); main() and
    the converters apply the remaining /16 and /100 scalings (see M-COUP in
    docs for the layering).
    """
    freqs: list[int]
    curves: dict[str, list[int]]
    ieq_amount: int
    ao_left: list[int]
    ao_right: list[int]
    peq_filters: list[dict]
    vol_leveler: dict | None
    dialog_enhancer: dict | None
    surround: dict | None
    mb_comp: dict | None
    regulator: dict | None
    volmax_boost: float


# DAX3 stores most dB-valued fields as integers in 1/16-dB fixed point
# (gains, thresholds, targets, slope/timbre); divide by this to get dB.
DB_FIXED_POINT_SCALE = 16.0


def parse_xml(path: Path, endpoint_type="internal_speaker",
              operating_mode="normal", profile_type=None) -> ParsedTuning:
    """Parse a DAX3 tuning XML into a ``ParsedTuning`` (see that dataclass for
    the fields and their units). Raises ``ValueError`` with an actionable
    message for unsupported schema variants or missing required elements."""
    tree = ET.parse(path)
    root = tree.getroot()
    constant = root.find("constant")

    if constant is None:
        # Dolby Fusion (microphone AEC / noise-suppression) XMLs share the
        # ``DEV_*_SUBSYS_*`` filename shape but carry a completely different
        # schema — no ``<constant>``, no ``<endpoint>``. They ship under
        # ``fusion_ext_*`` or ``ext_*_*/fusion/`` with ``_dmic.xml`` /
        # ``_amic.xml`` suffixes. The probe filters them by suffix; this
        # guard catches the case where the user passes one explicitly.
        raise ValueError(
            f"{path.name}: no <constant> element at XML root. This looks "
            "like a Dolby Fusion (microphone AEC) tuning, not a DAX3 "
            "playback tuning. Pick an XML without a '_dmic' / '_amic' "
            "suffix — those live alongside the DAX3 tunings in the same "
            "driver package but are for mic processing only."
        )

    band_20_freq = constant.find("band_20_freq")
    if band_20_freq is None:
        raise ValueError(
            f"{path.name}: <constant> has no <band_20_freq> child — cannot "
            "read the 20-band frequency grid. This XML uses a DAX3 schema "
            "variant this script does not support."
        )
    freqs = parse_csv_ints(band_20_freq.get("fs_48000"))

    curves = {}
    for el in constant:
        if el.tag.startswith("ieq_"):
            curves[el.tag] = parse_csv_ints(el.get("target"))

    endpoint = root.find(
        f".//endpoint[@type='{endpoint_type}'][@operating_mode='{operating_mode}']"
    )
    if endpoint is None:
        available = sorted({
            f"{ep.get('type')}/{ep.get('operating_mode')}"
            for ep in root.findall(".//endpoint")
        })
        available_str = ", ".join(available) if available else "(none)"
        raise ValueError(
            f"Endpoint type='{endpoint_type}' operating_mode='{operating_mode}' "
            f"not found. Available endpoint/mode pairs: {available_str}. "
            f"Pass --endpoint TYPE --mode MODE to pick one."
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
        if profile is None:
            raise ValueError(
                f"{path.name}: endpoint type='{endpoint_type}' "
                f"operating_mode='{operating_mode}' has no <profile> "
                "elements to default to. Pass --profile TYPE, or use "
                "--list to see this XML's endpoints and profiles."
            )

    # IEQ amount from the selected profile's tuning-cp (or first with IEQ enabled)
    ieq_amount = 10  # innovation-EQ weight assumed when ieq-amount is absent
    cp = profile.find("tuning-cp")
    if cp is not None:
        enable = cp.find("ieq-enable")
        if enable is not None and enable.get("value") == "1":
            ieq_amount = _int_attr(cp.find("ieq-amount"), default=ieq_amount)

    vlldp = profile.find("tuning-vlldp")
    if vlldp is None:
        raise ValueError(
            f"{path.name}: profile '{profile.get('type') or '(first)'}' has "
            "no <tuning-vlldp> — no audio-optimizer, PEQ, or MBC data to read. "
            "This XML uses a DAX3 schema variant this script does not support."
        )

    ao_bands = vlldp.find("audio-optimizer-bands")
    if ao_bands is None:
        raise ValueError(
            f"{path.name}: tuning-vlldp has no <audio-optimizer-bands>. "
            "This XML uses a DAX3 schema variant this script does not support."
        )
    # Per-channel audio-optimizer correction. Full-schema DAX3 names the
    # channels <ch_00>..<ch_07>; simplified-schema XMLs (older Lenovo drivers,
    # xml_version ~3.2.x — e.g. ThinkPad X1 Carbon Gen 8, see issue #22) store
    # the same 20-band, 1/16-dB arrays under a <gain_l>/<gain_r>/<gain_c>/…
    # surround layout instead. Both resolve through the identical value=/preset=
    # mechanism, so for a 2-channel speaker gain_l→left, gain_r→right. The
    # simplified variant also omits the MBC and speaker-PEQ blocks; those are
    # handled by the enable-gates below (absent element → block skipped).
    left_band = ao_bands.find("ch_00")
    right_band = ao_bands.find("ch_01")
    simplified_ao = left_band is None or right_band is None
    if simplified_ao:
        left_band = ao_bands.find("gain_l")
        right_band = ao_bands.find("gain_r")
    if left_band is None or right_band is None:
        found_tags = sorted({c.tag for c in ao_bands})
        raise ValueError(
            f"{path.name}: audio-optimizer-bands has neither ch_00/ch_01 nor "
            f"gain_l/gain_r — found {found_tags or '[]'} instead. This XML uses "
            "a DAX3 schema variant this script does not support. Pick another "
            "endpoint/profile, or open an issue if you need this variant "
            "supported."
        )
    if simplified_ao:
        cprint("warn", f"  {path.name}: simplified-schema DAX3 "
                       "(gain_l/gain_r audio-optimizer) — this variant has no "
                       "multi-band compressor or speaker PEQ.")
    ao_left = parse_csv_ints(resolve_xml_value(left_band, constant))
    ao_right = parse_csv_ints(resolve_xml_value(right_band, constant))

    peq_filters = []
    peq_enable = vlldp.find("speaker-peq-enable")
    if peq_enable is None or peq_enable.get("value") != "0":
        for f in vlldp.findall(".//speaker-peq-filters/filter"):
            if f.get("enabled") == "0":
                continue
            try:
                ftype = int(f.get("type"))
            except (TypeError, ValueError):
                warn(f"PEQ filter has missing/garbage type {f.get('type')!r}, skipping")
                continue
            if ftype not in (1, 3, 4, 6, 7, 8, 9):
                warn(f"unknown PEQ filter type {ftype}, skipping")
                continue
            try:
                peq_filters.append({
                    "speaker": int(f.get("speaker")),
                    "type": ftype,
                    "f0": float(f.get("f0")),
                    "gain": float(f.get("gain", "0")),
                    "q": float(f.get("q", "0.707")),
                    "s": float(f.get("s", "1.0")),
                    "order": int(f.get("order", "0")),
                })
            except (TypeError, ValueError):
                warn("PEQ filter has missing/garbage f0/speaker/order, skipping")
                continue

    # Volume leveler settings (from tuning-cp of the selected profile)
    vol_leveler = None
    if cp is not None:
        vl_enable = cp.find("volume-leveler-enable")
        if vl_enable is not None:
            vl_amount = cp.find("volume-leveler-amount")
            vl_in = cp.find("volume-leveler-in-target")
            vl_out = cp.find("volume-leveler-out-target")
            VOL_LEVELER_TARGET_DEFAULT = -320  # -320/16 = -20.0 dBFS in/out target when absent
            vol_leveler = {
                "enable": _int_attr(vl_enable, default=0),
                "amount": _int_attr(vl_amount, default=0),
                "in_target": _int_attr(vl_in, default=VOL_LEVELER_TARGET_DEFAULT) / DB_FIXED_POINT_SCALE,
                "out_target": _int_attr(vl_out, default=VOL_LEVELER_TARGET_DEFAULT) / DB_FIXED_POINT_SCALE,
            }

    # volmax-boost (tuning-cp) — Dolby's loudness-maximiser ceiling: the
    # maximum gain above the volume leveler's out-target. Parsed outside
    # the MBC block because the regulator is the preferred injection point
    # and MBC may be disabled on some profiles.
    volmax_boost = 0.0
    if cp is not None:
        volmax = cp.find("volmax-boost")
        if volmax is not None:
            volmax_boost = _int_attr(volmax, default=0) / DB_FIXED_POINT_SCALE

    # Dialog enhancer settings (from tuning-cp)
    dialog_enhancer = None
    if cp is not None:
        de_enable = cp.find("dialog-enhancer-enable")
        if de_enable is not None and de_enable.get("value") == "1":
            dialog_enhancer = {
                # dialog-enhancer-amount: assume 5 when the field is absent
                "amount": _int_attr(cp.find("dialog-enhancer-amount"), default=5),
            }

    # Surround virtualizer settings (from tuning-cp)
    surround = None
    if cp is not None:
        sr_enable = cp.find("surround-decoder-enable")
        if sr_enable is not None and sr_enable.get("value") == "1":
            surround = {
                "boost": _int_attr(cp.find("surround-boost"), default=0) / DB_FIXED_POINT_SCALE,
            }

    # Multi-band compressor settings (from tuning-vlldp)
    mb_comp = None
    mbc_enable = vlldp.find("mb-compressor-enable")
    if mbc_enable is not None and mbc_enable.get("value") == "1":
        mbc_tuning = vlldp.find("mb-compressor-tuning")
        if mbc_tuning is not None:
            band_groups = []
            for i in range(4):
                bg = mbc_tuning.find(f"band_group_{i}")
                if bg is not None:
                    group = parse_csv_ints(bg.get("value"))
                    if len(group) != 6:
                        raise ValueError(
                            f"{path.name}: band_group_{i} has {len(group)} "
                            "values, expected 6 (xover, threshold, ratio, "
                            "attack, release, makeup)."
                        )
                    band_groups.append(group)
            # group_count is present on every corpus XML; default to the
            # number of band groups actually found if a variant omits it.
            group_count = _int_attr(mbc_tuning.find("group_count"),
                                    default=len(band_groups))
            target_power = vlldp.find("mb-compressor-target-power-level")
            # Also grab regulator stress for additional context (same
            # regulator-stress-amount element the regulator block re-reads
            # below for its own `stress`; named distinctly to keep the two
            # consumers' intent clear).
            mbc_reg_stress_el = vlldp.find("regulator-stress-amount")
            mb_comp = {
                "group_count": group_count,
                "band_groups": band_groups,
                "target_power": _int_attr(target_power, default=-80) / DB_FIXED_POINT_SCALE,   # -80/16 = -5.0 dB
                "reg_stress": parse_csv_ints(mbc_reg_stress_el.get("value")) if mbc_reg_stress_el is not None else [],
            }

    # Regulator settings (per-band limiter from tuning-vlldp)
    regulator = None
    reg_dist = vlldp.find("regulator-speaker-dist-enable")
    if reg_dist is not None and reg_dist.get("value") == "1":
        reg_tuning = vlldp.find("regulator-tuning")
        if reg_tuning is not None:
            th_el = reg_tuning.find("threshold_high")
            tl_el = reg_tuning.find("threshold_low")
            # The newer SoundWire schema nests per-channel <ch_00>..<ch_07>
            # arrays under threshold_high/low; resolve_channel_or_direct reads
            # ch_00. make_regulator is a single stereo limiter that consumes
            # only threshold_high, so ch_00 is the reference. Warn (rather than
            # silently picking one) when ch_01 diverges so a future genuinely
            # L/R-asymmetric device surfaces — ch_00==ch_01 on the only device
            # with this schema today. (per-band-min would protect both channels
            # but can over-limit the one that didn't need it — left XML-only for
            # a later call once such a device exists.)
            th_val = resolve_channel_or_direct(th_el, constant)
            tl_val = resolve_channel_or_direct(tl_el, constant)
            for _el, _name in ((th_el, "threshold_high"), (tl_el, "threshold_low")):
                _c0 = _el.find("ch_00") if _el is not None else None
                _c1 = _el.find("ch_01") if _el is not None else None
                if (_c0 is not None and _c1 is not None
                        and resolve_xml_value(_c0, constant) != resolve_xml_value(_c1, constant)):
                    cprint("warn", f"  {path.name}: regulator {_name} ch_00 ≠ ch_01 "
                                   "(L/R asymmetric); using ch_00 for the stereo limiter.")
            if not th_val:
                cprint("warn", f"  {path.name}: regulator enabled but threshold_high "
                               "has no value/preset/ch_00 — no per-band limiting applied.")
            th = [x / DB_FIXED_POINT_SCALE for x in parse_csv_ints(th_val)] if th_val else [0.0] * len(freqs)
            tl = [x / DB_FIXED_POINT_SCALE for x in parse_csv_ints(tl_val)] if tl_val else [-12.0] * len(freqs)
            # make_regulator walks `th` and indexes `freqs` at positions
            # derived from it; a length mismatch would IndexError deep in the
            # zone loop. Fail loud here instead.
            if len(th) != len(freqs):
                raise ValueError(
                    f"{path.name}: regulator threshold_high has {len(th)} "
                    f"values but the band grid has {len(freqs)} — the "
                    "regulator zone mapping requires one threshold per band."
                )
            reg_stress_el = vlldp.find("regulator-stress-amount")
            stress = parse_csv_ints(reg_stress_el.get("value")) if reg_stress_el is not None else [0] * 8
            reg_slope = vlldp.find("regulator-distortion-slope")
            slope = _int_attr(reg_slope, default=16) / DB_FIXED_POINT_SCALE   # 16/16 = 1.0
            reg_timbre = vlldp.find("regulator-timbre-preservation")
            timbre = _int_attr(reg_timbre, default=12) / DB_FIXED_POINT_SCALE   # 12/16 = 0.75
            # `regulator-overdrive` and `regulator-relaxation-amount` are read
            # for visibility (debug print + watch warn) but not yet mapped to
            # any LSP plugin parameter — the corpus shows them as constants
            # (overdrive=0, relaxation=96 in 1/16-dB units) so we have no
            # signal to disambiguate the right mapping.
            reg_overdrive = vlldp.find("regulator-overdrive")
            overdrive = _int_attr(reg_overdrive, default=0)
            reg_relax = vlldp.find("regulator-relaxation-amount")
            relaxation = _int_attr(reg_relax, default=96)
            regulator = {
                "threshold_high": th,
                "threshold_low": tl,
                "stress": [x / DB_FIXED_POINT_SCALE for x in stress],
                "distortion_slope": slope,
                "timbre_preservation": timbre,
                "overdrive": overdrive,
                "relaxation": relaxation,
            }

    warn_unmodeled_features(profile)

    return ParsedTuning(
        freqs, curves, ieq_amount, ao_left, ao_right, peq_filters,
        vol_leveler, dialog_enhancer, surround, mb_comp, regulator, volmax_boost,
    )


# Newer-pipeline DSP blocks observed in the corpus that the script does not
# model. Warn when they're enabled so users can correlate with audible gaps.
# The list intentionally omits features that are universally present (e.g.
# `output-mode-partial-{surround,height}-virtualizer-enable`, MI steering)
# — those are documented in CLAUDE.md / docs/ and warning on every run would
# be noise. Only flag rare, enabled-only feature blocks here.
#
# Each entry is (xpath, predicate, message). Predicate takes the matched
# element and returns True if the feature is *active* in this profile;
# message takes the same element and returns the warning text.
_REPORT_URL = "https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues"
# The device-report issue form (.github/ISSUE_TEMPLATE/device-report.yml). The
# end-of-run CTA points here so "works on my hardware" reports arrive in a
# consistent shape; _REPORT_URL above stays the generic target for the mid-run
# feature-gap warnings, which aren't device reports.
_REPORT_FORM_URL = (
    "https://github.com/antoinecellerier/speaker-tuning-to-easyeffects"
    "/issues/new?template=device-report.yml"
)

_UNMODELED_FEATURES = [
    (".//dynamic_speaker_optimization_enable",
     lambda el: el.get("value") == "1",
     lambda el: "Dynamic Speaker Optimization (excursion-aware bass limiting) "
                "is set in the XML but not modeled — silently dropped."),
    (".//advanced-speaker-virtualizer-rendering-config",
     lambda el: True,  # presence implies the newer virtualizer pipeline is configured
     lambda el: "advanced speaker virtualizer (newer FFT-domain spatializer) "
                "is set in the XML but not modeled — silently dropped."),
    # Watching-only fields below: the corpus shows these as effectively
    # constants and the script doesn't act on them. If a future XML breaks
    # the assumption we'd like to know — the warnings nudge users to report.
    (".//peak-level",
     lambda el: (el.get("value") or "0") != "0",
     lambda el: (
         f"peak-level={el.get('value')} (≈ {int(el.get('value', '0')) / 16:+.2f} dB "
         "at the standard 1/16-dB convention) — this is 0 on every device in our "
         "corpus, and the script does not currently map it to the limiter "
         "threshold (interpretation unverified). If audio sounds off, please "
         f"report at {_REPORT_URL}"
     )),
    (".//ieq-bands-set",
     lambda el: (el.get("preset") or "ieq_balanced") != "ieq_balanced",
     lambda el: (
         f"ieq-bands-set preset={el.get('preset')!r} — this XML names a "
         "non-balanced curve as the profile default, but every device in our "
         "corpus uses 'ieq_balanced'. We still emit the usual "
         "Balanced/Detailed/Warm presets; you may want to start with the "
         f"matching variant. Please report at {_REPORT_URL}"
     )),
    (".//regulator-overdrive",
     lambda el: (el.get("value") or "0") != "0",
     lambda el: (
         f"regulator-overdrive={el.get('value')} — this is 0 on every "
         "device we've seen. The script does not currently map it (the "
         "schema interpretation is unverified for non-zero values). "
         f"Please report at {_REPORT_URL}"
     )),
    (".//regulator-relaxation-amount",
     lambda el: (el.get("value") or "96") != "96",
     lambda el: (
         f"regulator-relaxation-amount={el.get('value')} — this is 96 on "
         "every device we've seen. The script does not currently map it "
         "(the schema interpretation is unverified for other values). "
         f"Please report at {_REPORT_URL}"
     )),
]


def warn_unmodeled_features(profile: ET.Element) -> None:
    """Emit a one-line warning per unmodeled-but-enabled DSP block."""
    for xpath, active, message in _UNMODELED_FEATURES:
        el = profile.find(xpath)
        if el is not None and active(el):
            cprint("warn", f"  Note: {message(el)}")


# --- FIR generation ---

def interpolate_curve_db(band_freqs: np.ndarray, band_gains_db: np.ndarray,
                         fft_freqs: np.ndarray) -> np.ndarray:
    """Interpolate a gain curve (in dB) to FFT frequency bins.

    Uses log-frequency interpolation with linear dB values.
    Extrapolates flat beyond the band edges.
    """
    log_bands = np.log(np.maximum(band_freqs, 1.0))
    log_fft = np.log(np.maximum(fft_freqs, 1.0))
    return np.interp(log_fft, log_bands, band_gains_db,
                     left=band_gains_db[0], right=band_gains_db[-1])


# Floor added to a linear magnitude before 20*log10 so a true zero maps to a
# large finite negative dB instead of -inf (keeps FIR peak/verification finite).
LOG_MAG_FLOOR = 1e-12


def make_fir(band_freqs: np.ndarray, gains_db: np.ndarray,
             normalize: bool = True) -> tuple[np.ndarray, float]:
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
    peak_db = 20.0 * np.log10(peak_mag + LOG_MAG_FLOOR)

    if normalize:
        if peak_mag > 0:
            fir /= peak_mag

    return fir, peak_db


def save_wav_stereo(path: Path, fir_left: np.ndarray,
                    fir_right: np.ndarray) -> None:
    """Save stereo impulse response as 32-bit float WAV."""
    stereo = np.column_stack([fir_left, fir_right]).astype(np.float32)
    with _atomic_write(path) as tmp:
        wavfile.write(str(tmp), SAMPLE_RATE, stereo)


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

    gain = round(amount / DB_FIXED_POINT_SCALE * 6.0, 2)
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
                  conservative: bool = False) -> dict | None:
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
    blocks_per_sec = SAMPLE_RATE / block_size
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
        threshold = thresh_raw / DB_FIXED_POINT_SCALE
        # gain_coeff → ratio: 32767 = 1:1 (bypass), lower = more compression
        gain_frac = gain_raw / Q15_SCALE
        # out-of-range gain → clamp to practical max (builder warns)
        ratio = 1.0 / gain_frac if gain_frac > 0.01 else 100.0
        attack_ms = decode_mbc_time_constant(attack_raw)
        release_ms = decode_mbc_time_constant(release_raw)
        makeup = makeup_raw / DB_FIXED_POINT_SCALE
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
            warn(f"MBC band {i} gain coeff {gain_raw} "
                 f"out of range — clamping ratio to {b['ratio']:.0f}:1")
        if not 0 < attack_raw < Q15_SCALE:
            warn(f"MBC band {i} attack coeff {attack_raw} "
                 f"out of range — using {b['attack_ms']:.0f} ms fallback")
        if not 0 < release_raw < Q15_SCALE:
            warn(f"MBC band {i} release coeff {release_raw} "
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
                   volmax_slot: str = "input-gain") -> dict | None:
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
            is_active = threshold < 0
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
DISABLEABLE_FILTERS = {
    "volmax": ("too loud, pumping/squash on loud content",
               "drops the +volmax-boost static loudness gain"),
    "mbc": ("compressed or \"squashed\" character",
            "drops the Dolby multi-band compressor"),
    "regulator": ("unusual spectral pumping or narrow-band breathing",
                  "drops the per-band limiter"),
    "bass-enhancer": ("bass sounds artificial/distorted (SoundWire only)",
                      "drops the harmonic bass generator"),
    "dialog": ("vocals over-boosted or harsh in the presence region",
               "drops the 2.5 kHz speech-band EQ"),
    "high-shelf": ("harsh or sibilant high frequencies",
                   "drops Dolby's type-3 high-shelf boost (experimental)"),
    "lo-pass": ("highs sound rolled off / dull",
                "drops Dolby's type-6/8 low-pass rolloff (experimental)"),
}

# Emission paths that are numerically verified but not yet user-validated
# on real hardware. Keys that overlap with DISABLEABLE_FILTERS are turned
# off with --disable <key>; "mbc-1band" is a marker-only name (no separate
# flag — users who want it off should pass --disable mbc instead). Used
# to trigger a targeted "please report" prompt at end-of-run when any of
# these fired for the current preset.
EXPERIMENTAL_MARKERS = {
    "high-shelf": "type-3 high-shelf",
    "lo-pass": "type-6/8 low-pass",
    "mbc-1band": "1-band MBC (group_count=1)",
}


def make_preset(kernel_name: str, peq_filters: list[dict],
                vol_leveler: dict | None = None,
                dialog_enhancer: dict | None = None,
                mb_comp: dict | None = None, regulator: dict | None = None,
                freqs: list[int] | None = None,
                is_soundwire: bool = False, volmax_boost: float = 0.0,
                volmax_slot: str = "input-gain",
                disabled: set[str] | None = None) -> tuple[dict, set[str]]:
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
        "_generator": f"dolby_to_easyeffects.py {get_version()}",
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
        hp_filters = [f for f in peq_filters if f["type"] in (7, 9)]
        hp_freq = hp_filters[0]["f0"] if hp_filters else 100.0
        preset["output"]["bass_enhancer#0"] = make_bass_enhancer(hp_freq)
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
    reg = None
    if "regulator" not in disabled:
        reg = make_regulator(regulator, freqs, volmax_boost=apply_volmax,
                             volmax_slot=volmax_slot)
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
                           volmax_slot="input-gain"):
    """Print the human-readable per-profile diagnostics for a parsed tuning
    (audio-optimizer / PEQ / dialog / surround / leveler / MBC / regulator /
    volmax). Side-effect-free apart from stdout — split out of main() so the
    orchestration there stays legible."""
    ieq_amount = tuning.ieq_amount
    peq_filters = tuning.peq_filters
    dialog_enhancer = tuning.dialog_enhancer
    surround = tuning.surround
    vol_leveler = tuning.vol_leveler
    mb_comp = tuning.mb_comp
    regulator = tuning.regulator
    volmax_boost = tuning.volmax_boost
    freqs = tuning.freqs

    print(f"ieq-amount: {ieq_amount}% (scale: {scale:.2f})")

    # Audio-optimizer curves in dB
    print("\nAudio-optimizer (dB):")
    print(f"  Left:  {[f'{x:+.1f}' for x in ao_db_left]}")
    print(f"  Right: {[f'{x:+.1f}' for x in ao_db_right]}")

    print("\nPEQ filters (kept as parametric EQ):")
    for pf in peq_filters:
        spk = "L" if pf["speaker"] == 0 else "R"
        if pf["type"] in (7, 9):
            print(f"  [{spk}] HP @ {pf['f0']} Hz, order {pf['order']} ({pf['order'] * 6} dB/oct)")
        elif pf["type"] in (6, 8):
            print(f"  [{spk}] Lo-pass @ {pf['f0']} Hz, order {pf['order']} ({pf['order'] * 6} dB/oct)  [experimental]")
        elif pf["type"] == 4:
            print(f"  [{spk}] Lo-shelf @ {pf['f0']} Hz, {pf['gain']:+.1f} dB, S={pf['s']}")
        elif pf["type"] == 3:
            print(f"  [{spk}] Hi-shelf @ {pf['f0']} Hz, {pf['gain']:+.1f} dB, S={pf['s']}  [experimental]")
        elif pf["type"] == 1:
            print(f"  [{spk}] Bell @ {pf['f0']} Hz, {pf['gain']:+.1f} dB, Q={pf['q']}")

    if dialog_enhancer:
        gain = dialog_enhancer["amount"] / DB_FIXED_POINT_SCALE * 6.0
        print(f"\nDialog enhancer: amount={dialog_enhancer['amount']}, "
              f"mapped to +{gain:.1f} dB @ 2.5 kHz")

    if surround:
        print(f"\nSurround virtualizer: boost={surround['boost']:.1f} dB — "
              "not mapped (DAX applies no stereo widening on 2-ch content; "
              "see design-notes entry 2)")

    if vol_leveler:
        print(f"\nVolume leveler: {'enabled' if vol_leveler['enable'] else 'disabled'}")
        print(f"  amount: {vol_leveler['amount']}")
        print(f"  in-target: {vol_leveler['in_target']:.1f} dB")
        print(f"  out-target: {vol_leveler['out_target']:.1f} dB")

    if mb_comp:
        tag = "  [experimental]" if mb_comp["group_count"] == 1 else ""
        print(f"\nMulti-band compressor: {mb_comp['group_count']} band(s){tag}")
        print(f"  target-power-level: {mb_comp['target_power']:.1f} dB")
        # Print FROM the single-source decode — no inline re-decode, no
        # warnings (those fire in make_multiband_compressor). xover_hz is a
        # display concern derived here from the stored xover_idx + band
        # position, exactly as before.
        decoded = decode_mbc_bands(mb_comp)
        n_bands_print = len(decoded)
        for i, b in enumerate(decoded):
            xover_idx = b["xover_idx"]
            if i == n_bands_print - 1:
                # Sentinel in the last band — the band runs to Nyquist
                xover_hz = "full-band" if n_bands_print == 1 else "Nyquist"
            elif 0 <= xover_idx < len(freqs):
                xover_hz = f"{freqs[xover_idx]} Hz"
            else:
                xover_hz = "?"
            print(f"  band {i}: xover={xover_hz}, thresh={b['threshold']:+.1f} dB, "
                  f"ratio={b['ratio']:.2f}:1, attack={b['attack_ms']:.2f} ms, "
                  f"release={b['release_ms']:.2f} ms, makeup={b['makeup']:+.1f} dB")

    if regulator:
        print("\nRegulator (per-band limiter):")
        print(f"  threshold_high (dB): {[f'{x:+.1f}' for x in regulator['threshold_high']]}")
        print(f"  threshold_low (dB):  {[f'{x:+.1f}' for x in regulator['threshold_low']]}")
        print(f"  stress (dB):         {[f'{x:+.1f}' for x in regulator['stress']]}")
        print(f"  distortion-slope:    {regulator.get('distortion_slope', 1.0):.2f}")
        print(f"  timbre-preservation: {regulator.get('timbre_preservation', 0.75):.2f}")
        print(f"  overdrive (raw):     {regulator.get('overdrive', 0)}  (watched; not yet mapped)")
        print(f"  relaxation (raw):    {regulator.get('relaxation', 96)}  (watched; not yet mapped)")

    if volmax_boost <= 0:
        slot = "value is 0, no boost to apply"
    elif "volmax" in disabled:
        slot = "disabled via --disable volmax"
    elif regulator and "regulator" not in disabled:
        slot = f"applied as regulator {volmax_slot}"
    else:
        slot = "applied as limiter input-gain"
    print(f"\nvolmax-boost: {volmax_boost:+.1f} dB ({slot})")
    # A band with threshold >= 0 dBFS never triggers, so make_regulator
    # disables it; if every band is like that, the regulator carries the
    # volmax boost but tames nothing — the issue-#23 "per-band compression
    # tames the boost before the brickwall" rationale doesn't apply, and
    # both volmax slots degenerate to the same untamed brickwall feed
    # (issue #27 field report; see design-notes).
    if (volmax_boost > 0 and "volmax" not in disabled
            and regulator and "regulator" not in disabled
            and all(t >= 0 for t in regulator["threshold_high"])):
        cprint("warn", "⚠  This tuning's regulator never engages (every band "
                       "threshold is >= 0 dB), so the volmax boost reaches "
                       "the brickwall limiter untamed. If loud content "
                       "sounds squashed, re-run with --disable volmax.")
    print()


def _emit_ieq_presets(tuning, name_base, ao_db_left, ao_db_right, float_freqs,
                      scale, is_soundwire, disabled, args, profile_label,
                      all_preset_names, filters_by_profile):
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

    ieq_presets = {
        f"{name_base}-Balanced": "ieq_balanced",
        f"{name_base}-Detailed": "ieq_detailed",
        f"{name_base}-Warm": "ieq_warm",
    }

    for preset_name, curve_key in ieq_presets.items():
        if curve_key not in curves:
            cprint("warn", f"  Skipping {preset_name}: curve '{curve_key}' not found in XML")
            continue

        gains_raw = curves[curve_key]
        ieq_db = np.array(gains_raw) / DB_FIXED_POINT_SCALE * scale

        # Combined target: IEQ + audio-optimizer (summed in dB)
        combined_left = ieq_db + ao_db_left
        combined_right = ieq_db + ao_db_right

        # Generate FIR impulse responses
        fir_left, _ = make_fir(float_freqs, combined_left, normalize=True)
        fir_right, _ = make_fir(float_freqs, combined_right, normalize=True)

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
                                      disabled=disabled)
        for name in emitted:
            filters_by_profile.setdefault(name, set()).add(profile_label)
        out_path = args.output_dir / f"{preset_name}.json"
        if not args.dry_run:
            _atomic_write_text(out_path, json.dumps(preset, indent=4) + "\n")

        all_preset_names.append(preset_name)

        verb = "Would write" if args.dry_run else "Wrote"
        cprint("ok", f"{verb} {irs_path}")
        cprint("ok", f"{verb} {out_path}")
        print(f"  {curve_key} combined IEQ+AO curve (left channel):")
        print(f"  {'freq':>8}  {'IEQ':>6}  {'AO':>6}  {'combined':>8}")
        for i, f in enumerate(freqs):
            print(f"  {f:>7} Hz  {ieq_db[i]:+5.1f}  {ao_db_left[i]:+5.1f}  {combined_left[i]:+7.1f}")

        # Verify FIR frequency response
        H = np.fft.rfft(fir_left, n=FIR_LENGTH)
        fft_freqs = np.fft.rfftfreq(FIR_LENGTH, d=1.0 / SAMPLE_RATE)
        mag_db = 20.0 * np.log10(np.abs(H) + LOG_MAG_FLOOR)
        cprint("dim", "\n  FIR verification (left, normalized to peak=0):")
        for i, f in enumerate(freqs):
            idx = np.argmin(np.abs(fft_freqs - f))
            cprint("dim", f"  {f:>7} Hz  target: {combined_left[i] - np.max(combined_left):+6.1f}  "
                  f"actual: {mag_db[idx]:+6.1f}  "
                  f"error: {mag_db[idx] - (combined_left[i] - np.max(combined_left)):+5.2f}")
        print()


def main():
    # --no-color must be honored before argparse prints --help; pre-scan
    # argv so the formatter falls back to plain when requested.
    formatter_class = argparse.HelpFormatter if "--no-color" in sys.argv else _HelpFormatter
    epilog = None
    if _MISSING_COLOR_DEPS:
        epilog = (
            f"Tip: install {' and '.join(_MISSING_COLOR_DEPS)} for colored output "
            "(see README for distro packages)."
        )
    parser = _HelpHintParser(
        description="Convert Dolby DAX3 tuning XML to EasyEffects output presets.",
        epilog=epilog,
        formatter_class=formatter_class,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {get_version()}",
        help="show version (git describe) and exit",
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
             "codec subsystem ID from /proc/asound. Omit this flag to let the "
             "script probe /proc/mounts and the current directory for a "
             "suitable source",
    )
    parser.add_argument(
        "--best-guess",
        action="store_true",
        help="if auto-detection finds no exact hardware match, fall back to the "
             "only internal-speaker tuning whose manufacturer is present "
             "(unverified — matched by manufacturer, not device id). With "
             "several such candidates it lists them so you can pass one as the "
             "positional XML path. No effect when an exact match is found",
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
        "--autoload-sink",
        action="append",
        default=[],
        metavar="NODE_NAME",
        help="explicit PipeWire sink node.name to bind autoload to, bypassing "
             "speaker-sink detection (repeatable). Use this when auto-detection "
             "picks the wrong output or finds none — e.g. a laptop whose "
             "internal speaker is mis-tagged (no audio-speakers device icon). "
             "Find the name with 'pw-dump | grep node.name', or run with "
             "--autoload to print the candidate list. Mirrors "
             "ee_to_pipewire.py's --target-sink.",
    )
    parser.add_argument(
        "--no-autoload-bypass",
        dest="autoload_bypass",
        action="store_false",
        help=f"with --autoload, do not write a '{BYPASS_PRESET_NAME}' bypass "
             "preset or enable EasyEffects' global Fallback Preset. Use if "
             "you manage the fallback yourself. Existing user setups are "
             "preserved even without this flag.",
    )
    parser.add_argument(
        "--speaker-info",
        action="store_true",
        help="report detected audio hardware and speaker layout, then exit",
    )
    parser.add_argument(
        "--doctor", "--diagnose",
        dest="doctor",
        action="store_true",
        help="run environment self-diagnostics (EasyEffects version, install "
             "location, preset/impulse-file integrity, selected preset, "
             "hardware) and exit — paste the output into an issue if a preset "
             "seems inaudible",
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
    parser.add_argument(
        "--volmax-slot",
        choices=["output-gain", "input-gain"],
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
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable colored terminal output",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run without writing any files to disk (presets, IRs, autoload); "
             "useful for debugging script execution and output",
    )
    args = parser.parse_args()
    if args.no_color:
        _disable_color()
    disabled = set(args.disable)

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
        cprint("ok", f"Auto-detected: {xml_path}")
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
        cprint("ok", f"Auto-detected: {xml_path}")

    xml_basename = Path(xml_path).name.upper()
    is_soundwire = "SOUNDWIRE" in xml_basename or xml_basename.startswith("SDW_")

    if args.list:
        cprint("head", f"Endpoints and profiles in {xml_path}:")
        list_endpoints(xml_path)
        return

    if args.dry_run:
        cprint("head", "Dry run: no files will be written to disk.")
    else:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        args.irs_dir.mkdir(parents=True, exist_ok=True)

    # Determine which profiles to process
    if args.all_profiles:
        profile_types = get_profile_types(xml_path, args.endpoint, args.mode)
        if not profile_types:
            cprint("warn", f"No profiles found for endpoint={args.endpoint} mode={args.mode}")
            return
        cprint("head", f"Generating presets for all {len(profile_types)} profiles: {', '.join(profile_types)}")
    else:
        profile_types = [args.profile]  # None means "first profile"

    all_preset_names = []
    # filter name → set of profile labels that emitted it. Lets the
    # end-of-run --disable hint say *which* profiles each suggestion
    # actually touches, so a user autoloading one preset isn't misled
    # into thinking a filter applies to them when it only runs in other
    # profiles.
    filters_by_profile: dict[str, set[str]] = {}

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
                warn(f"sanitizing profile name {profile_type!r} -> {safe_profile!r} for use in filenames")
            name_parts.append(safe_profile.title())
        name_base = "-".join(name_parts)

        cprint("head", f"\n{'='*60}")
        if is_soundwire:
            cprint("head", "SoundWire device detected — using enhanced preset generation")
        cprint("head", f"Endpoint: {args.endpoint} (mode={args.mode})")
        cprint("head", f"Profile: {profile_type or '(first)'}")

        tuning = parse_xml(
            xml_path,
            endpoint_type=args.endpoint,
            operating_mode=args.mode,
            profile_type=profile_type,
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
        ao_db_left = np.array(tuning.ao_left) / DB_FIXED_POINT_SCALE
        ao_db_right = np.array(tuning.ao_right) / DB_FIXED_POINT_SCALE
        float_freqs = np.array(tuning.freqs, dtype=float)

        _report_parsed_profile(tuning, ao_db_left, ao_db_right, scale, disabled,
                               args.volmax_slot)

        _emit_ieq_presets(tuning, name_base, ao_db_left, ao_db_right,
                          float_freqs, scale, is_soundwire, disabled, args,
                          profile_label, all_preset_names, filters_by_profile)

    # Autoload configuration
    if args.autoload and all_preset_names:
        autoload_preset = args.autoload if isinstance(args.autoload, str) else all_preset_names[0]
        sinks = _resolve_autoload_sinks(args.autoload_sink, args.dry_run)
        if sinks:
            cprint("head", f"\nConfiguring autoload → '{autoload_preset}':")
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
                    cprint("warn", f"  Skipping {sink['name']}: couldn't determine "
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
                cprint("ok", f"  {verb} {path}")
                print(f"  Device: {sink['description'] or sink['name']} ({route})")

        # Fallback preset: neutralize the Dolby chain on any non-speaker sink
        # (HDMI, USB headset, Bluetooth, etc.) that lacks its own autoload
        # entry. Without this, EE keeps the last-loaded preset applied and
        # mangles audio on outputs the Dolby tuning wasn't designed for.
        if args.autoload_bypass:
            cprint("head", f"\nConfiguring fallback preset → '{BYPASS_PRESET_NAME}':")
            bypass_path, bypass_status = write_bypass_preset(
                args.output_dir, BYPASS_PRESET_NAME, dry_run=args.dry_run,
            )
            if bypass_status == "kept":
                cprint("ok", f"  Kept existing {bypass_path}")
            elif bypass_status == "would-write":
                cprint("ok", f"  Would write {bypass_path}")
            else:
                cprint("ok", f"  Wrote {bypass_path}")

            fallback_status, existing = set_autoload_fallback(
                DEFAULT_EASYEFFECTS_RC, BYPASS_PRESET_NAME, dry_run=args.dry_run,
            )
            if fallback_status == "already-configured":
                cprint("ok", f"  Fallback preset already configured "
                              f"('{existing}') in {DEFAULT_EASYEFFECTS_RC} — leaving as-is")
            elif fallback_status == "would-patch":
                cprint("ok", f"  Would enable fallback preset in {DEFAULT_EASYEFFECTS_RC}")
            else:
                cprint("ok", f"  Enabled fallback preset in {DEFAULT_EASYEFFECTS_RC}")
                if easyeffects_is_running():
                    cprint("warn", "  EasyEffects is currently running — restart it for "
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
            cprint("warn", "  Tip: enable Background Service + Autostart on login in "
                           "EasyEffects' preferences so this autoloads on every login.")

    # End-of-run troubleshooting hint. Only list filters that actually
    # got emitted this run — no point suggesting --disable for
    # something the user couldn't hear anyway.
    shown = [k for k in DISABLEABLE_FILTERS if k in filters_by_profile]
    total_profiles = len(profile_types)
    if shown:
        cprint("head", f"\n{'=' * 60}")
        cprint("dim", "If anything sounds off on your hardware, you can rebuild")
        cprint("dim", "without specific filters instead of editing the chain in")
        cprint("dim", "EasyEffects. Re-run adding one or more of:")
        print()
        for name in shown:
            symptom, effect = DISABLEABLE_FILTERS[name]
            using = sorted(filters_by_profile[name])
            if total_profiles <= 1:
                scope = ""
            elif len(using) == total_profiles:
                scope = "; used in all profiles"
            else:
                scope = f"; used in profiles: {', '.join(using)}"
            cprint("dim", f"  --disable {name:<14}  # if you hear: {symptom}")
            cprint("dim", f"  {'':<24}    ({effect}{scope})")
        print()
        cprint("dim", "Flags are repeatable, e.g. --disable volmax --disable mbc.")

    experimental = [EXPERIMENTAL_MARKERS[k]
                    for k in EXPERIMENTAL_MARKERS
                    if k in filters_by_profile]
    if experimental:
        print()
        cprint("dim", f"Experimental path(s) exercised: {', '.join(experimental)}")
        cprint("dim", "These emissions are reproduced directly from the Dolby tuning and")
        cprint("dim", "verified numerically, but have not yet been audibly validated by")
        cprint("cta", "a user with an affected device. Feedback is especially helpful.")

    # Capability warning: some laptops gate their woofers behind a smart-amp
    # firmware-load ALSA control (issue #17). Only relevant when tuning the
    # internal speakers — irrelevant for headphone/other endpoints.
    if args.endpoint == "internal_speaker":
        warn_speaker_firmware_gate(detect_speaker_firmware_gates())
        # An old kernel can mis-drive the speaker amp below any preset
        # (issue #33) — hint at it, softly, when the series is old.
        warn_old_kernel()

    # Proactively flag an EasyEffects install that can't use what we just wrote
    # — the failure mode #22 surfaced (a correct preset silently inaudible
    # because of the environment, e.g. EE 7 or a wrong install location).
    # Silent on the happy path; reuses --doctor's probes.
    warn_ee_environment(args)

    print()
    cprint("cta", "How does it sound? Please report back (good or bad):")
    cprint("cta", f"  {_REPORT_FORM_URL}")


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, RuntimeError, ValueError, ET.ParseError) as e:
        cprint("err", f"Error: {e}")
        cprint("cta", "Run with --help to see usage and all options.")
        sys.exit(1)
