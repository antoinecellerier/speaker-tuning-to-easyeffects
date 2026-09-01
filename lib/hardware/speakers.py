"""The ``SpeakerInfo`` record, and every probe that fills it.

What Linux thinks is wired to this laptop: the HDA pin complexes it
configured as internal speakers, the ones it left unconfigured (which is what
a woofer the firmware hides looks like — issue #53), the amplifiers
enumerated on the SoundWire bus, and the ALSA control that gates smart-amp
firmware (issue #17). Parsing is kept apart from reading throughout —
``parse_hda_codec_pins``, ``parse_pin_config_overrides`` and
``parse_firmware_gate_controls`` take text — so every hardware case is
unit-tested without the hardware.

``find_hidden_speaker_pin`` and ``find_misrouted_speaker_pin`` are the pieces
that reason rather than read. Both mirror ``snd_hda_pick_fixup`` closely
enough to only claim a match the kernel could itself make — the shared
``_quirk_for_codec`` is that mirror — against the tables in
``lib/data/speaker_pin_quirks.py`` and ``lib/data/speaker_route_quirks.py``.

Standard library plus ``lib.data.speaker_pin_quirks``,
``lib.data.speaker_route_quirks``, ``lib.hardware.amps`` and
``lib.hardware.codecs``, all stdlib-only themselves, so this module is too.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from lib.data import speaker_pin_quirks
from lib.data import speaker_route_quirks
from lib.hardware import amps
from lib.hardware import codecs  # single source of the SoundWire bus entry point


@dataclass
class SpeakerPin:
    """A single internal speaker output (HDA pin or SoundWire amplifier)."""
    node: str            # HDA node ID or SoundWire device name
    control_name: str    # ALSA control name or driver name
    role: str            # "woofer" / "tweeter" (HDA pins beside a woofer),
                         # "speaker" (HDA pins on a codec with no bass-named
                         # one) or "amplifier" (a SoundWire amp chip)
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


class PinRoute(NamedTuple):
    """Where one pin complex takes its signal from, as /proc renders it.

    Two lists, and the difference between them is the signal: the hardware
    ``Connection:`` list with the current selection starred, and an
    ``In-driver Connection:`` one the kernel prints *only* when the driver's
    cached list differs from it — i.e. when something called
    ``snd_hda_override_conn_list``. A pin whose two disagree is being routed
    by the driver against what the hardware itself reports.
    """
    node: str                        # HDA node id, e.g. "0x17"
    sources: tuple[str, ...] = ()    # hardware list, in order, "*" stripped
    selected: str = ""               # the starred entry, "" when the dump
                                     # doesn't say. A one-entry list is never
                                     # starred — the kernel only reads the
                                     # selector when there is a choice — so
                                     # there the sole entry is the selection.
    driver_sources: tuple[str, ...] = ()   # In-driver list, () when absent


@dataclass
class CodecRouting:
    """One codec dump's signal routing, and where the volume amps sit.

    Kept apart from the pin scan because it answers a different question: not
    "is this pin an internal speaker" but "what is upstream of it, and can
    anything turn that path down". A pin selected onto a converter with no
    output amp is a speaker whose level no mixer control can move.
    """
    codec: str = ""      # subsystem id of the codec, e.g. "17AA22E6"
    # Pin complexes only — those are the widgets a speaker hangs off.
    routes: dict[str, PinRoute] = field(default_factory=dict)
    # Every node the dump printed, pin or not: True when the widget carries an
    # output volume amp. A node *absent* here was not in the dump at all,
    # which is not the same as False — callers must read a missing key as
    # unknown, never as "no volume".
    volume: dict[str, bool] = field(default_factory=dict)


@dataclass
class SpeakerInfo:
    """Collected audio hardware information for --speaker-info."""
    vendor: str = ""     # DMI sys_vendor — the only line that names the OEM
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
    # Per-HDA-codec signal routing, keyed by subsystem id: which converter
    # each pin is selected onto, and which widgets carry an output volume
    # amp. Parsed from the same codec dump the pins above come from.
    routing: dict[str, CodecRouting] = field(default_factory=dict)
    # Smart-amp firmware-load gates (e.g. TAS2781 "Speaker Force Firmware Load")
    firmware_gates: list[FirmwareGate] = field(default_factory=list)
    # False when amixer is absent, which is the difference between "this
    # machine has no gate" and "nothing looked". An empty `firmware_gates`
    # reads as the first either way, and the first is the reassuring one.
    firmware_gates_checked: bool = True
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
        if set(by_role) == {"speaker"}:
            # A mono pin, or several with no bass-named one: not "multi-way",
            # which would contradict the role printed on the pin line above,
            # and not "full-range" either — nothing probed says what they
            # drive, only that Linux shows no separate woofer pin.
            return (f"{total} speaker{'s' if total != 1 else ''}, no separate "
                    "woofer pin")
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
    if not codecs.SDW_BUS.is_dir():
        return

    amp_patterns = amps._AMP_DRIVER_TOKENS  # single source of amp-family identity

    for dev_dir in sorted(codecs.SDW_BUS.iterdir()):
        if not codecs.SDW_SLAVE_RE.match(dev_dir.name):
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

    # Fallback: check ALSA mixer for amp controls when sysfs gives nothing.
    # The part names come from the same registry as everything else — hand-kept
    # here, this list drifted *looser* than the tokens (a bare `max98` catches
    # the max98090 jack codec, a bare `rt\d+` catches rt711, both of which the
    # registry refuses) while missing tas2, aw88 and wsa88 entirely. Since a
    # match here appends a SpeakerPin exactly like the sysfs path, that drift
    # reached the speaker count.
    amp_alt = "|".join(re.escape(t) for t in amps._AMP_DRIVER_TOKENS)
    amp_control_re = re.compile(rf"'((?:{amp_alt})[^']*)\s+DAC'", re.I)
    try:
        result = subprocess.run(
            ["amixer", "-c0", "scontrols"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            m = amp_control_re.search(line)
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


def _codec_ssid(codec_text: str) -> str:
    """``"Subsystem Id: 0x17aa22e6"`` → ``"17AA22E6"``.

    The id everything about a codec is filed under — quirk lookup, per-codec
    pin counts, the routing map below — so it is read the one way, here.
    """
    m = re.search(r"^Subsystem Id: 0x([0-9a-fA-F]+)", codec_text,
                  flags=re.MULTILINE)
    return m.group(1).upper() if m else ""


def _iter_codec_nodes(codec_text: str) -> Iterator[tuple[str, str]]:
    """``(node id, block text)`` for each widget in a codec dump.

    The AFG header above the first ``Node`` is dropped, and that is the point:
    it carries ``Default Amp-Out caps:`` and PCM lines that read exactly like a
    widget's own while belonging to no widget at all.
    """
    for block in re.split(r"(?=^Node 0x[0-9a-fA-F]+ )", codec_text,
                          flags=re.MULTILINE):
        node = re.match(r"Node (0x[0-9a-fA-F]+)", block)
        if node:
            yield node.group(1), block


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
    codec_ssid = _codec_ssid(codec_text)

    speakers: list[SpeakerPin] = []
    unconfigured: list[UnconfiguredPin] = []
    for node, block in _iter_codec_nodes(codec_text):
        if "[Pin Complex]" not in block:
            continue

        default = re.search(r"Pin Default (0x[0-9a-fA-F]+):", block)
        if not default:
            continue
        override = (overrides or {}).get(node)
        cfg = override.cfg if override else int(default.group(1), 16)

        if _pin_is_internal_speaker(cfg):
            ctrl_match = re.search(r'Control: name="([^"]+)"', block)
            ctrl_name = ctrl_match.group(1) if ctrl_match else "Speaker"
            lower = ctrl_name.lower()
            role = "woofer" if ("bass" in lower or "woofer" in lower) else ""
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
    # "tweeter" only means something beside a woofer. A machine whose one
    # speaker pin is the whole speaker was labelled a tweeter, a few lines
    # above a layout estimate that called it full-range stereo (issue #84).
    # Decided per codec, which is per machine in practice — an HDMI codec has
    # no speaker pins, so the analog codec owns them all — and keyed on "no
    # bass-named pin here" rather than on the pin count. The neutral word,
    # not "full-range": the control name is the only thing probed, and it
    # says nothing about what the pin drives — on a machine whose woofer pin
    # the firmware hides (issue #53) this one really is a tweeter, and the
    # report flags that pin separately.
    fill = "tweeter" if any(s.role == "woofer" for s in speakers) else "speaker"
    for s in speakers:
        if not s.role:
            s.role = fill
    return codec_ssid, speakers, unconfigured


# How ``sound/hda/common/proc.c`` renders a widget's connection list: a count
# line, then the list itself on the next line, indented. The star marks the
# selected source and only appears when there is a choice to make — the kernel
# skips the selector read for a one-entry list, so there the sole entry *is*
# the selection. The "In-driver" variant prints only when the driver's cached
# list differs from the hardware's, and is never starred.
#
#   Connection: 4
#      0x02 0x03* 0x06 0x08
#   In-driver Connection: 2
#      0x02 0x03
_CONN_COUNT_RE = re.compile(r"^\s+(In-driver )?Connection: (\d+)\s*$")
_CONN_ENTRY_RE = re.compile(r"^(0x[0-9a-fA-F]+)(\*?)$")

# "Amp-Out caps: ofs=0x57, nsteps=0x57, stepsize=0x02, mute=0". nsteps is the
# whole test, not the line's presence: a pin's mute-only amp prints the same
# line with nsteps=0x00, an amp-less widget prints no line at all, and a
# widget whose caps read zero prints "N/A" — the last two match nothing here.
_AMP_OUT_NSTEPS_RE = re.compile(r"^\s+Amp-Out caps: .*\bnsteps=0x([0-9a-fA-F]+)")


def _parse_conn_list(line: str) -> tuple[tuple[str, ...], str]:
    """One rendered connection list → ``(entries, the starred entry)``.

    Entries that don't render as a node id are dropped rather than raised on:
    this parses whatever a stranger's kernel printed.
    """
    entries: list[str] = []
    selected = ""
    for token in line.split():
        entry = _CONN_ENTRY_RE.match(token)
        if not entry:
            continue
        entries.append(entry.group(1))
        if entry.group(2):
            selected = entry.group(1)
    return tuple(entries), selected


def parse_hda_codec_routing(codec_text: str) -> CodecRouting:
    """Split one ``/proc/asound/card*/codec#*`` dump into what each pin listens
    to and which widgets can turn their output down.

    Pure text parsing, like ``parse_hda_codec_pins`` — same dump, different
    question, so the two are separate passes over one read.

    Read block by block, never with one search over the file: a rendered
    connection list names no widget of its own, so a file-wide match binds a
    mixer's list to whichever pin header last preceded it. Every line here
    means something only relative to the ``Node`` it sits under.

    A truncated dump, a count with no list under it, an entry in a shape we
    have never seen: each degrades to an empty field. This runs on every
    machine that prints a report, against codec dumps we cannot see.
    """
    routing = CodecRouting(codec=_codec_ssid(codec_text))
    for node, block in _iter_codec_nodes(codec_text):
        lines = block.splitlines()
        header, body = lines[0], lines[1:]
        sources: tuple[str, ...] = ()
        selected = ""
        driver_sources: tuple[str, ...] = ()
        volume = False
        i = 0
        while i < len(body):
            line = body[i]
            i += 1
            amp = _AMP_OUT_NSTEPS_RE.match(line)
            if amp:
                volume = int(amp.group(1), 16) != 0
                continue
            count = _CONN_COUNT_RE.match(line)
            if not count:
                continue
            # The list is on the *next* line, and only when the count is
            # non-zero — an HDMI pin prints "Connection: 0" with nothing
            # under it. Anything else there is left for the loop to read as
            # the ordinary line it is.
            if count.group(2) == "0" or i >= len(body):
                continue
            entries, starred = _parse_conn_list(body[i])
            if not entries:
                continue
            i += 1
            if count.group(1):       # "In-driver ": the driver's cached list
                driver_sources = entries
            else:
                sources = entries
                selected = starred or (entries[0] if len(entries) == 1 else "")
        routing.volume[node] = volume
        if "[Pin Complex]" in header:
            routing.routes[node] = PinRoute(node=node, sources=sources,
                                            selected=selected,
                                            driver_sources=driver_sources)
    return routing


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

    It substitutes the machine's *audio* identity — pins, codec list, and an
    emptied SoundWire one — and nothing else: kernel, product and distro stay
    the host's, so anything keyed to those still describes the real machine.
    The codec list and the SoundWire one are part of it because callers pick
    the detection branch off ``bus_type``, so a demo that filled in pins alone
    did nothing on any host that wasn't itself HDA — a SoundWire laptop, or
    CI, where there is no codec to make ``bus_type`` "hda" at all. Returns
    True when a demo was injected (skip real detection then).
    """
    ssid = (os.environ.get("DEMO_SPEAKER_PIN") or "").strip().upper()
    if not ssid:
        return False
    info.hda_codecs = [("10EC0287", ssid, "Realtek ALC287")]
    info.soundwire_devices = []
    # "speaker", as the parser labels the one pin a codec with no bass-named
    # one exposes: the demo reproduces issue #53's *pre-fix* state, so it
    # must render the line that machine really prints.
    info.speakers.append(SpeakerPin(node="0x14",
                                    control_name="Speaker Playback Switch",
                                    role="speaker", channels=2, codec=ssid))
    for node, pincap in (("0x17", "OUT HP Detect"),
                         ("0x1b", "IN OUT EAPD Detect"), ("0x1e", "OUT")):
        info.unconfigured_pins.append(UnconfiguredPin(
            node=node, codec=ssid, pincap=pincap, pin_default="0x411111f0"))
    return True


def _maybe_demo_speaker_route(info: SpeakerInfo) -> bool:
    """Stand in for a machine driving a speaker past its volume control.

    Same demo/preview convention as ``DEMO_SPEAKER_PIN``, for the same
    reason: the warning is keyed to the machine, so no corpus XML can ever
    trigger it for a copy review. ``DEMO_SPEAKER_ROUTE=17AA3906`` reproduces
    the Legion Pro 7i 16IAX10H's pre-fix state — both speaker pins
    configured, the bass pin 0x17 selected onto converter 0x06, which
    carries no output volume amp. That row is codec-keyed and its fixup has
    a forcible name, so the preview walks the full procedure branch.

    Checked *after* the pin demo in both gatherers: the two substitute the
    same audio identity, and injecting both would stack contradictory
    machines into one report.
    """
    ssid = (os.environ.get("DEMO_SPEAKER_ROUTE") or "").strip().upper()
    if not ssid:
        return False
    info.hda_codecs = [("10EC0287", ssid, "Realtek ALC287")]
    info.soundwire_devices = []
    info.speakers += [
        SpeakerPin(node="0x14", control_name="Speaker Playback Switch",
                   role="tweeter", channels=2, codec=ssid),
        SpeakerPin(node="0x17", control_name="Bass Speaker Playback Switch",
                   role="woofer", channels=2, codec=ssid),
    ]
    info.routing[ssid] = CodecRouting(
        codec=ssid,
        routes={
            "0x14": PinRoute("0x14", sources=("0x02",), selected="0x02"),
            "0x17": PinRoute("0x17", sources=("0x02", "0x03", "0x06", "0x08"),
                             selected="0x06"),
        },
        volume={"0x02": True, "0x03": True, "0x06": False, "0x08": False})
    return True


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
        # Second pass over the same text, no second read: routing is keyed by
        # subsystem id, so a dump that names none is parsed and dropped rather
        # than filed under a key another codec would collide with.
        routing = parse_hda_codec_routing(text)
        if routing.codec:
            info.routing[routing.codec] = routing


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


def amixer_present() -> bool:
    """Whether the gate scan below could run at all.

    Split from the scan because its empty result is ambiguous: no gate found
    and no way to look are the same `[]`, and the report was rendering both
    as silence. Nothing in the PipeWire stack pulls `alsa-utils` in — on
    Debian it arrives as a Recommends of the desktop task — so a minimal or
    container install genuinely has no amixer.
    """
    return shutil.which("amixer") is not None


def detect_speaker_firmware_gates() -> list[FirmwareGate]:
    """Scan ALSA cards for smart-amp firmware-load gate controls.

    Reads each card's raw control list via ``amixer -c <N> contents`` (the
    same tool the SoundWire fallback already shells out to) and returns a
    FirmwareGate per matching control. Empty when amixer is absent or no
    gate exists — which is why callers ask `amixer_present()` too, since
    those two mean opposite things to the reader.
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


def _quirk_for_codec(table: dict, codec_ssid: str, owns_speakers: bool,
                     uses_sof: bool, pci_subsystem: tuple[str, str] | None):
    """The row *codec_ssid* matches in *table*, mirroring the parts of
    ``snd_hda_pick_fixup`` both quirk tables share — factored so the pin and
    routing detectors cannot drift on them:

    * every entry can match the *codec's* subsystem id — either because it is
      an ``HDA_CODEC_QUIRK`` or via the codec-SSID fallback the lookup ends on;
    * a PCI-keyed entry can also match the PCI subsystem id, but not on SOF,
      where the id the kernel sees is zeroed. Our own PCI id is read from
      sysfs and is *not* zeroed, so trusting it there would claim a match the
      kernel never makes. And the PCI id belongs to the machine, not to any
      one codec, so it may only stand in for a codec that already owns
      speaker pins — otherwise it lends the analog machine's identity to
      whichever other codec happens to have a spare output pin.
    """
    key = _ssid_key(codec_ssid)
    quirk = table.get(key) if key else None
    if quirk is None and owns_speakers and not uses_sof and pci_subsystem:
        pci_key = _ssid_key("".join(pci_subsystem))
        candidate = table.get(pci_key) if pci_key else None
        if candidate is not None and not candidate.codec_only:
            quirk = candidate
    return quirk


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
        quirk = _quirk_for_codec(speaker_pin_quirks._SPEAKER_PIN_QUIRKS,
                                 codec_ssid, bool(nodes), uses_sof,
                                 info.pci_subsystem)
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


def find_misrouted_speaker_pin(
        info: SpeakerInfo,
) -> tuple[speaker_route_quirks.RouteQuirk, str, SpeakerPin, str] | None:
    """The speaker pin this machine is driving through a widget with no
    volume control, else None — ``(quirk, codec ssid, the pin, its source)``.

    Unlike the hidden-pin detector, the fault here is *visible*: the codec
    dump stars each pin's selected source and says whether that widget
    carries an output volume amp. So the table only supplies the authority
    ("upstream carries a fix for this exact machine") and the dump supplies
    the finding. Firing on the table alone would send a user after a fixup
    that may not be their problem — the objection recorded in design-notes
    when this class was first left unbuilt.

    Every step of the gate fails closed to silence:

    * the quirk's pin must be a *configured internal speaker* on the codec
      that matched — a pin the kernel is not driving cannot be mis-routed,
      and a genuinely spare pin parked on an ampless converter is normal
      (the dev machine's 0x1e);
    * the dump must name a selected source, and it must sit outside the
      fixup's allowed list;
    * that source must be known to carry no output volume amp — a widget the
      dump didn't show stays "unknown", never "no";
    * the driver's own connection list must leave the star meaningful.
      ``/proc`` marks the selected entry by comparing each position against
      ``AC_VERB_GET_CONNECT_SEL``, which indexes the *driver's* cached list
      once ``snd_hda_override_conn_list`` has run — while the list being
      printed is the hardware's. The two agree only while the cached list is
      a prefix of the hardware one (every upstream routing helper truncates,
      so they do today, the dev machine included); an override that
      reordered or skipped entries would land the star on some other widget,
      possibly an ampless one. So an ``In-driver Connection`` that is not a
      prefix means the selector cannot be read, not that the pin is
      mis-routed. Equal to the fixup's own list, it means the override is
      already applied and our star reading contradicts the kernel, which
      outranks the parse. The line's *presence* proves nothing either way:
      the dev machine gets a conn-list override from a pin-signature match
      (``snd_hda_pin_quirk``) with no SSID entry at all, so it is only ever
      a negative guard.

    Silent, too, on any codec the *pin* detector has already claimed. On 11
    of the 28 machines in both tables the two rows name different pins (the
    chain declares 0x14 and reroutes 0x17), so both faults are visible at
    once — but they are one missing fixup with one remedy, and two warnings
    would print two procedures writing the same modprobe file, the second
    silently discarding the first.
    """
    if info.bus_type != "hda":
        return None
    uses_sof = _card_uses_sof(info.sound_cards)
    hidden = find_hidden_speaker_pin(info)
    speakers_by_codec: dict[str, list[SpeakerPin]] = {}
    for pin in info.speakers:
        speakers_by_codec.setdefault(pin.codec, []).append(pin)

    for codec_ssid, pins in sorted(speakers_by_codec.items()):
        if hidden and hidden[1] == codec_ssid:
            continue
        quirk = _quirk_for_codec(speaker_route_quirks._SPEAKER_ROUTE_QUIRKS,
                                 codec_ssid, True, uses_sof,
                                 info.pci_subsystem)
        if quirk is None:
            continue
        pin = next((p for p in pins if p.node.lower() == quirk.pin), None)
        routing = info.routing.get(codec_ssid)
        route = routing.routes.get(quirk.pin) if routing else None
        if pin is None or route is None or not route.selected:
            continue
        allowed = tuple(quirk.sources.split())
        if route.selected in allowed:
            continue
        if routing.volume.get(route.selected, True):
            continue
        if route.driver_sources and (
                route.driver_sources == allowed
                or route.driver_sources
                != route.sources[:len(route.driver_sources)]):
            continue
        return quirk, codec_ssid, pin, route.selected
    return None
