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

from lib import console, doctor, ee_paths, version
from lib.data import kernel_releases
from lib.data import speaker_pin_quirks
from lib.dax import parse
from lib.hardware import amps, codecs, speakers
# Aliased: main() binds a local named `sinks` for the resolver's result, which
# would shadow the module for every later line that reads through it.
from lib.hardware import sinks as hardware_sinks
from lib.preset import autoload, bands
# Aliased: main() binds a local named `findings`, which would shadow the
# module for the one line that resets _TAG_CONVENTION_SHOWN through it.
from lib.report import findings as report_findings
from lib.report import environment, messages

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
    the deferral this function exists for. lib.preset.plugins reaches numpy
    through fir (the sample rate its MBC time constants decode against) and
    lib.preset.build through plugins, so those two ride along. The rest of
    lib/preset — bands, autoload — is stdlib-only and imported at the top.
    """
    global np, wavfile, fir, plugins, build
    import numpy as np
    from scipy.io import wavfile
    from lib.preset import fir
    from lib.preset import build, plugins


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


def warn_speaker_firmware_gate(gates: list[speakers.FirmwareGate]) -> Finding | None:
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
        console.cprint("cta", f"     {speakers.amixer_enable_cmd(g)}")
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
    return report_findings._firmware_gate_finding()


_MODPROBE_CONF = "/etc/modprobe.d/speaker-pin-fix.conf"


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
    running = environment.parse_kernel_series(release or platform.release())
    fixed_in = environment.parse_kernel_series(quirk.since)
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
    module, param = speakers.hda_model_module(uses_sof)

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
        info: speakers.SpeakerInfo) -> Finding | None:
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
                                  speakers._card_uses_sof(info.sound_cards),
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


def unlisted_speaker_pin_finding(info: speakers.SpeakerInfo) -> Finding | None:
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
    if len(info.speakers) != 1 or speakers.find_hidden_speaker_pin(info):
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


def _gather_speaker_pins() -> speakers.SpeakerInfo:
    """Just enough of a SpeakerInfo to answer find_hidden_speaker_pin.

    A few /proc reads and one sysfs walk. Kept apart from
    ``_gather_speaker_info`` deliberately: that one also shells out to
    ``amixer`` per card and to ``journalctl``/``dmesg`` (seconds, on a machine
    with a big journal), globs /lib/firmware, and honours the demo-injection
    env vars — all of it for the amp-status report, which a default run never
    prints. A normal conversion must not pay for it.
    """
    import platform

    info = speakers.SpeakerInfo(kernel=platform.release())
    cards_path = Path("/proc/asound/cards")
    if cards_path.exists():
        info.sound_cards = [l.strip() for l
                            in cards_path.read_text().strip().splitlines()]
    info.hda_codecs = codecs.get_hda_codec_ids()
    info.soundwire_devices = codecs.get_soundwire_ids()   # decides bus_type
    info.pci_subsystem = codecs.get_pci_audio_subsystem()
    # Asked first, not folded into the condition below: injecting the demo is
    # what makes bus_type read "hda", so testing bus_type first would skip it.
    if not speakers._maybe_demo_hidden_speaker_pin(info):
        if info.bus_type == "hda":
            speakers._detect_hda_speakers(info)
    return info


def _gather_speaker_info() -> speakers.SpeakerInfo:
    """Collect all audio hardware information into a SpeakerInfo."""
    import platform

    info = speakers.SpeakerInfo(kernel=platform.release(), distro=get_distro_pretty_name())

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
    info.hda_codecs = codecs.get_hda_codec_ids()
    info.soundwire_devices = codecs.get_soundwire_ids()
    info.pci_subsystem = codecs.get_pci_audio_subsystem()

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
    if not speakers._maybe_demo_hidden_speaker_pin(info):
        if info.bus_type == "soundwire":
            speakers._detect_soundwire_speakers(info)
        elif info.bus_type == "hda":
            speakers._detect_hda_speakers(info)

    # Bus-agnostic: a TI smart-amp firmware gate sits on the SOF/HDA card
    # regardless of how the speakers themselves are wired.
    info.firmware_gates = speakers.detect_speaker_firmware_gates()

    # Merged amp-status evidence (firmware presence + kernel-log markers),
    # unless a demo override is requested for previewing the section.
    if not speakers._maybe_demo_amp_status(info):
        amps._gather_amp_evidence(info)

    return info


def _amp_status_lines(info: speakers.SpeakerInfo) -> list[str]:
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
            lines.append(f"      turn it on:  {speakers.amixer_enable_cmd(g)}")

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


def _print_speaker_info(info: speakers.SpeakerInfo):
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
    series = environment.parse_kernel_series(info.kernel)
    aged = environment._kernel_series_age(series, date.today()) if series else None
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
            found = speakers.find_hidden_speaker_pin(info)
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
    if info.bus_type == "hda" and speakers.find_hidden_speaker_pin(info):
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


def speaker_pin_status(info: speakers.SpeakerInfo) -> CheckResult | None:
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
    found = speakers.find_hidden_speaker_pin(info)
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
                                    speakers._card_uses_sof(info.sound_cards),
                                    console._wrap_width() - 9,
                                    speaker_info_below=True))


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
                          custom_dirs: bool = False) -> environment.DoctorReport:
    """Run every probe and assemble a DoctorReport. All I/O is wrapped so a
    missing binary / unreadable file degrades to a soft line, never a crash."""
    report = environment.DoctorReport()

    # 1. EasyEffects version / compatibility
    probe = _probe_ee_version()
    version, found, source, ee_is_flatpak = (
        probe.version, probe.found, probe.source, probe.is_flatpak)
    report.checks.append(environment.ee_version_status(version, found, probe.silent))

    # 2. Install location (skip the EE-location verdict for custom dirs)
    if custom_dirs:
        report.checks.append(CheckResult(DOCTOR_PASS, "Install location",
            f"custom output dir ({environment._tilde(output_dir)}) — skipping EasyEffects "
            "location checks."))
    else:
        report.checks.append(environment.install_status(
            _FLATPAK_BASE.exists(), _NATIVE_BASE.exists(), _USE_FLATPAK,
            environment._tilde(_EASYEFFECTS_BASE), ee_is_flatpak))

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
    dolby_presets = [p for p in preset_paths if p.stem != autoload.BYPASS_PRESET_NAME]
    if not dolby_presets:
        report.checks.append(CheckResult(DOCTOR_WARN, "Generated presets",
            f"no presets found in {environment._tilde(output_dir)} — run the script on your "
            "tuning XML first."))
    else:
        for p in dolby_presets:
            try:
                data = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                report.checks.append(CheckResult(DOCTOR_FAIL, f"Preset {p.stem}",
                    "could not be read / not valid JSON."))
                continue
            report.checks.append(environment.check_preset_kernel(data, irs_stems, p.stem))

    # 4. EasyEffects runtime state (loaded preset, sink, chain)
    try:
        rc_text = rc_path.read_text(encoding="utf-8")
    except OSError:
        rc_text = ""
    rc = autoload.read_ee_rc(rc_text)
    # The selected-preset check compares against presets in output_dir; that's
    # only meaningful when output_dir is where EE actually loads from (default
    # dirs). Under custom dirs, surface the loaded preset as a fact instead.
    if rc_text and not custom_dirs:
        report.checks.append(environment.loaded_preset_status(rc, generated_names))
    # Background-service / autostart is install-global, not output-dir-specific,
    # so it runs even under custom dirs (unlike the selected-preset check).
    if rc_text:
        report.checks.append(environment.autostart_status(rc))

    # 5. Hardware / codec context (folds in --speaker-info)
    report.speaker_info = _gather_speaker_info()

    # 6. Smart-amp firmware gate — upstream of the whole preset (issue #17)
    gate_check = environment.firmware_gate_status(report.speaker_info.firmware_gates)
    if gate_check is not None:
        report.checks.append(gate_check)

    # 7. A woofer pin the firmware hides, so half the speakers go unused
    #    upstream of the whole preset (issue #53)
    pin_check = speaker_pin_status(report.speaker_info)
    if pin_check is not None:
        report.checks.append(pin_check)

    # 8. Kernel age — speaker-amp fixes land kernel-side (issue #33)
    report.checks.append(environment.kernel_age_status(report.speaker_info.kernel))

    report.facts = {
        "ee_version": (".".join(map(str, version)) if version else "unknown")
                      + (f" (via {source})" if source else ""),
        "ee_running": easyeffects_is_running(),
        "install": "Flatpak" if _USE_FLATPAK else "native",
        "output_dir": environment._tilde(output_dir),
        "irs_dir": environment._tilde(irs_dir),
        "preset_count": len(generated_names),
        "irs_count": len(irs_stems),
        "rc_path": environment._tilde(rc_path),
        "rc_present": bool(rc_text),
        "selected_preset": rc.get("last_output_preset", "")
                           or rc.get("fallback_preset", ""),
        "autostart_on_login": rc.get("autostart_on_login", False),
        "service_mode": rc.get("service_mode", True),
        "output_device": rc.get("output_device", ""),
        "output_plugins": rc.get("output_plugins", []),
    }
    return report


def _print_doctor_report(report: environment.DoctorReport) -> None:
    """Print a compact, paste-safe diagnostic report."""
    emit = environment.emit_check

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
    environment.print_doctor_summary(report.checks)
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
    environment.print_doctor_verdict(report.checks)
    console.cprint("dim", "If you still hear no difference between the preset and bypass:")
    console.cprint("dim", "  • In EasyEffects, toggle the preset off/on to A/B it.")
    console.cprint("dim", "  • Make sure global bypass (the power-button icon, top bar) is OFF.")
    console.cprint("dim", "  • Confirm system output is the speaker sink and volume is up.")
    print()

    if report.speaker_info is not None:
        _print_speaker_info(report.speaker_info)

    console.cprint("cta", "Still stuck? Paste everything above into an issue:")
    console.cprint("cta", f"  {report_findings._REPORT_FORM_URL}")


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
    ver = environment.ee_version_status(version, found, probe.silent)

    if ver.status == DOCTOR_FAIL:
        vstr = ".".join(str(x) for x in version)
        console.cprint("err", f"\n{'=' * 60}")
        console.cprint("err", f"⚠  EasyEffects {vstr} detected — these presets need EasyEffects 8.")
        print()
        console._cprint_wrapped("dim", environment.ee_v7_message(vstr))
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
        console.cprint("warn", "\n⚠  " + environment.ee_silent_message(
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


# --- FIR generation ---
#
# make_fir and friends are in lib/preset/fir.py. This one stayed behind them
# because binding `wavfile` in a module that isn't this one means writing a
# second deferred import, which is new code rather than motion. It is the
# only remaining caller of lib/preset/autoload.py's _atomic_write outside
# that module.


def save_wav_stereo(path: Path, fir_left: np.ndarray,
                    fir_right: np.ndarray) -> None:
    """Save stereo impulse response as 32-bit float WAV."""
    stereo = np.column_stack([fir_left, fir_right]).astype(np.float32)
    with autoload._atomic_write(path) as tmp:
        wavfile.write(str(tmp), fir.SAMPLE_RATE, stereo)


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
        for name in sorted({*messages.DISABLEABLE_FILTERS, *messages.ENABLEABLE_FILTERS},
                           key=len, reverse=True))
    console._HelpFormatter.highlights = [
        *console._HelpFormatter.highlights,
        # "--disable volmax" / "--enable autogain" usage examples
        rf"--(?:disable|enable)\s+(?P<metavar>{_FILTER_NAME_ALTERNATION})",
        # the "Valid names: a, b, c." enumerations — each name sits between
        # ": "/", " and ","/"." there, which prose mentions never do
        rf"(?<=[:,] )(?P<metavar>{_FILTER_NAME_ALTERNATION})(?=[,.])",
    ]


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
        findings.append(report_findings._profile_mismatch_finding(declared,
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
        be = plugins.bass_enhancer_from_peq(peq_filters)
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
                     if plugins.bass_enhancer_scope_is_derived(peq_filters) else
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
        decoded = plugins.decode_mbc_bands(mb_comp)
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
        if plugins._coupled_bands_eligible(regulator):
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
                     and plugins._coupled_bands_eligible(regulator))):
        findings.append(report_findings._loudness_untamed_finding(
            plugins._coupled_bands_eligible(regulator)))
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
                     and plugins._coupled_bands_eligible(regulator))):
        peak_band = max(range(len(ao_db_left)),
                        key=lambda i: max(ao_db_left[i], ao_db_right[i]))
        peak_db = max(ao_db_left[peak_band], ao_db_right[peak_band])
        thresholds = regulator["threshold_high"]
        at_rail = peak_db >= tuning.geq_max_range / parse.DB_FIXED_POINT_SCALE
        restored = "level-restore" in (enabled or set())
        if ((at_rail or restored)
                and peak_band < len(thresholds)
                and thresholds[peak_band] >= 0):
            findings.append(report_findings._boost_unlimited_finding(
                peak_db, freqs[peak_band],
                plugins._coupled_bands_eligible(regulator), restored))
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
        preset, emitted = build.make_preset(preset_name, peq_filters, vol_leveler,
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
            autoload._atomic_write_text(out_path, json.dumps(preset, indent=4) + "\n")

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
        help=f"with --autoload, do not write a '{autoload.BYPASS_PRESET_NAME}' bypass "
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
        choices=list(messages.DISABLEABLE_FILTERS),
        metavar="NAME",
        help="drop a filter from the generated preset (repeatable). "
             f"Valid names: {', '.join(messages.DISABLEABLE_FILTERS)}. "
             "Try --disable volmax if output sounds too loud / saturated, or "
             "--disable mbc if you dislike the compressor character.",
    )
    add(
        "--enable",
        action="append",
        default=[],
        choices=list(messages.ENABLEABLE_FILTERS),
        metavar="NAME",
        help="activate a filter that ships present but inactive "
             f"(repeatable). Valid names: {', '.join(messages.ENABLEABLE_FILTERS)}. "
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
        names = [s.get("name", "") for s in hardware_sinks._enumerate_audio_sinks()]
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
        sinks = hardware_sinks._resolve_autoload_sinks(args.autoload_sink, args.dry_run)
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
                path = autoload.write_autoload(
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
            console.cprint("head", f"\nConfiguring fallback preset → '{autoload.BYPASS_PRESET_NAME}':")
            bypass_path, bypass_status = autoload.write_bypass_preset(
                args.output_dir, autoload.BYPASS_PRESET_NAME, dry_run=args.dry_run,
            )
            if bypass_status == "kept":
                console.cprint("ok", f"  Kept existing {bypass_path}")
            elif bypass_status == "would-write":
                console.cprint("ok", f"  Would write {bypass_path}")
            else:
                console.cprint("ok", f"  Wrote {bypass_path}")

            fallback_status, existing = autoload.set_autoload_fallback(
                DEFAULT_EASYEFFECTS_RC, autoload.BYPASS_PRESET_NAME, dry_run=args.dry_run,
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
        _rc = autoload.read_ee_rc(_rc_text)
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
            speakers.detect_speaker_firmware_gates())
        if gate_finding is not None:
            findings.setdefault(gate_finding.slug, gate_finding)
        # A hidden woofer pin leaves half the speakers unconfigured, so the
        # preset shapes the tweeters alone (issue #53). Gathering speaker info
        # is a handful of /proc reads; only reached on the speaker endpoint.
        speaker_info = _gather_speaker_pins()
        pin_finding = warn_hidden_speaker_pin(
            speakers.find_hidden_speaker_pin(speaker_info), speaker_info)
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
        environment.warn_old_kernel()

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
    fired = [k for k in messages.EXPERIMENTAL_MARKERS if k in filters_by_profile]
    experimental = [messages.EXPERIMENTAL_MARKERS[k] for k in fired]
    if experimental:
        # Only the markers that are also --disable names give the user an A/B;
        # "mbc-1band" and "coupled-bands-active" have no flag of their own.
        findings.setdefault("unconfirmed-by-ear", report_findings._experimental_finding(
            ", ".join(experimental),
            [k for k in fired if k in messages.DISABLEABLE_FILTERS]))
        _print_finding_detail(findings["unconfirmed-by-ear"])

    # Gated on the leveler actually running, not on the flag being passed:
    # --enable autogain does nothing when the XML disables the leveler, and
    # escalating on the flag alone contradicted the "had no effect" warning
    # printed a few lines above on exactly those devices.
    substage_finding = report_findings._leveler_gap_finding(
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
        menu_printed = messages.print_troubleshooting(
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
        messages.print_what_now(all_preset_names, bool(args.autoload), args.dry_run,
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
        report_findings.print_project_asks(scoped, dry_run=args.dry_run, xml_path=xml_path)


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
