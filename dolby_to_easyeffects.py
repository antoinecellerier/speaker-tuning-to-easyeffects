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
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from lib import console, doctor, ee_paths, packages
from lib.dax import discover, parse
from lib.hardware import speakers
# Aliased: _configure_autoload binds a local named `sinks` for the resolver's
# result, which would shadow the module for every later line that reads
# through it.
from lib.hardware import sinks as hardware_sinks
from lib.preset import autoload
# Aliased: `findings` is what main()'s RunTally calls the same collection, so
# an unqualified `findings.` in this file would read as that, not the module.
from lib.report import findings as report_findings
# Aliased: one letter apart from lib.hardware.speakers above, which this file
# still reads on the lines that hand it a SpeakerInfo to report on.
from lib.report import speaker as report_speaker
from lib.report import doctor_run, environment, messages

# Optional tab-completion (README "Shell tab-completion"). Absent argcomplete, the
# script behaves exactly as before — same contract as rich in lib/console.py.
try:
    import argcomplete
except ImportError:
    argcomplete = None


# Annotated and printed here, raised across lib/dax/ and lib/report/, so the
# record type is shared rather than owned (see lib/report/findings.py). Kept
# under the names the rest of this file already uses.
Finding = report_findings.Finding
_print_finding_detail = report_findings._print_finding_detail


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


TUNING_INPUT_DESCRIPTION = (
    "with neither an XML path nor --windows, the script auto-discovers: it "
    "probes mounted Windows partitions (/proc/mounts) and the current "
    "directory for a tuning source"
)


def add_tuning_input_args(container, *, only=None):
    """Tuning-input flags (group shared with dolby_to_pipewire.py)."""
    add, added = console._make_adder(container, only)
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
    add, added = console._make_adder(container, only)
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
    add, added = console._make_adder(container, only)
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
    add, added = console._make_adder(container, only)
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
        default=ee_paths.DEFAULT_AUTOLOAD_DIR,
        help="EasyEffects autoload directory "
             f"(default: {doctor.tilde(ee_paths.DEFAULT_AUTOLOAD_DIR)})",
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
             "--autoload, which names the sink it picked (and lists the "
             "candidates when it isn't sure). Mirrors "
             "ee_to_pipewire.py's --target-sink.",
    )
    add(
        "--no-autoload-bypass",
        dest="autoload_bypass",
        action="store_false",
        help=f"with --autoload, do not write a '{autoload.BYPASS_PRESET_NAME}' bypass "
             "preset or enable EasyEffects' global Fallback Preset. Use if "
             "you manage the fallback yourself. An already-enabled fallback "
             "preset is left alone even without this flag.",
    )
    return added


def add_output_args(container, *, only=None):
    """Output naming/location flags (dolby_to_pipewire.py shares --prefix)."""
    add, added = console._make_adder(container, only)
    add(
        "--prefix",
        default="Dolby",
        help="prefix for preset names (default: Dolby → Dolby-Balanced, etc.)",
    )
    add(
        "--output-dir",
        type=Path,
        default=ee_paths.DEFAULT_OUTPUT_DIR,
        help="EasyEffects output preset directory "
             f"(default: {doctor.tilde(ee_paths.DEFAULT_OUTPUT_DIR)})",
    )
    add(
        "--irs-dir",
        type=Path,
        default=ee_paths.DEFAULT_IRS_DIR,
        help="EasyEffects impulse response directory "
             f"(default: {doctor.tilde(ee_paths.DEFAULT_IRS_DIR)})",
    )
    return added


def add_filter_tweak_args(container, *, only=None):
    """Filter-tweak flags (group shared with dolby_to_pipewire.py)."""
    add, added = console._make_adder(container, only)
    add(
        "--disable",
        action="append",
        default=[],
        choices=list(messages.DISABLEABLE_FILTERS),
        metavar="NAME",
        # #44 is NOT a coupled-bands case — measured inert there
        # (design-notes 2026-08-22); don't re-add the pointer here. Its fix
        # was --volmax-slot output-gain, which E-022 below names.
        help="drop a filter from the generated preset (repeatable). "
             f"Valid names: {', '.join(messages.DISABLEABLE_FILTERS)}. "
             "Try --disable volmax if output sounds too loud / saturated, "
             "--disable mbc if you dislike the compressor character, or "
             "--disable coupled-bands if the loudest moments feel clamped.",
    )
    add(
        "--enable",
        action="append",
        default=[],
        choices=list(messages.ENABLEABLE_FILTERS),
        metavar="NAME",
        # Only autogain ships a stage the preset leaves bypassed;
        # level-restore and virtual-bass add nothing EasyEffects can see, so
        # "ships present but inactive" was true of one name in three.
        help="switch on an optional stage the preset leaves off "
             f"(repeatable). Valid names: {', '.join(messages.ENABLEABLE_FILTERS)}. "
             "Try --enable autogain if the preset sounds right but quieter "
             "than Windows (issue #25), or --enable level-restore "
             "(experimental) if the preset is quieter than switching it off "
             "altogether (issue #50).",
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
             "costs too much loudness on a device with an aggressive regulator "
             "(issue #44 measured it, and the reporter confirmed by ear, as the "
             "fix for bass the default placement loses). "
             "Neither placement is Dolby-documented; no effect when the regulator "
             "is disabled/absent (the boost then lands on limiter#0 input-gain).",
    )
    return added


def add_general_args(container, *, only=None):
    """General flags — dolby_to_pipewire.py takes only --verbose from here
    (which it forwards to the generator it runs), authors its own --dry-run,
    and adds the shared --no-color/--version itself so neither is recorded
    as forwardable."""
    add, added = console._make_adder(container, only)
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
    console.add_color_and_version_args(add)
    return added


def build_parser(argv: list[str] | None = None) -> argparse.ArgumentParser:
    formatter_class, epilog = console.help_style(argv)
    parser = console._HelpHintParser(
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
        stems = [p.stem for p in ee_paths.DEFAULT_OUTPUT_DIR.glob("*.json")]
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


def _configure_autoload(args, all_preset_names) -> None:
    """Write the autoload entries, the bypass fallback, and the
    persistence tip — the whole of what --autoload sets up.

    No-op unless --autoload was passed and something was generated, so
    main() calls it unconditionally and the guard lives with the work.
    """
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
                    console.cprint("warn", "  Skipping "
                                   f"{doctor.no_bt_address(sink['name'])}: couldn't determine "
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
                # `tilde` alone is not enough on this one: the filename
                # write_autoload builds embeds the node name, so a Bluetooth
                # sink's address rides inside the path.
                console.cprint("ok", "  " + verb + " "
                               + doctor.no_bt_address(doctor.tilde(path)))
                print("  Device: " + doctor.no_bt_address(
                    f"{sink['description'] or sink['name']} ({route})"))

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
                console.cprint("ok", f"  Kept existing {doctor.tilde(bypass_path)}")
            elif bypass_status == "would-write":
                console.cprint("ok", f"  Would write {doctor.tilde(bypass_path)}")
            else:
                console.cprint("ok", f"  Wrote {doctor.tilde(bypass_path)}")

            fallback_status, existing = autoload.set_autoload_fallback(
                ee_paths.DEFAULT_EASYEFFECTS_RC, autoload.BYPASS_PRESET_NAME, dry_run=args.dry_run,
            )
            rc_shown = doctor.tilde(ee_paths.DEFAULT_EASYEFFECTS_RC)
            if fallback_status == "already-configured":
                console.cprint("ok", f"  Fallback preset already configured "
                              f"('{existing}') in {rc_shown} — leaving as-is")
            elif fallback_status == "would-patch":
                console.cprint("ok", f"  Would enable fallback preset in {rc_shown}")
            else:
                console.cprint("ok", f"  Enabled fallback preset in {rc_shown}")
                if doctor_run.easyeffects_is_running():
                    console.cprint("warn", "  EasyEffects is currently running — restart it for "
                                   "the fallback setting to take effect (EE rewrites "
                                   "this file when it quits and on its autosave "
                                   "timer while its window is open).")

        # Autoload only persists across logins if EasyEffects both starts at
        # login (autostart) and stays alive in the background (service mode);
        # nudge toward the prefs, but only when one is off so the fully
        # configured case stays quiet.
        try:
            _rc_text = ee_paths.DEFAULT_EASYEFFECTS_RC.read_text(encoding="utf-8")
        except OSError:
            _rc_text = ""
        _rc = autoload.read_ee_rc(_rc_text)
        if not (_rc.get("autostart_on_login") and _rc.get("service_mode")):
            console.cprint("warn", "  Tip: enable Background Service + Autostart on login in "
                           "EasyEffects' preferences so this autoloads on every login.")


def _speaker_environment_findings(endpoint: str) -> list[Finding]:
    """Probe the speaker environment — smart-amp firmware gate, hidden woofer
    pin, unlisted pin count, kernel age — printing what each finds where it
    finds it, and return the findings raised, in that order.

    Returns them rather than merging into main()'s dict so the caller's merge
    stays pure bookkeeping: setdefault prints nothing, so the inline output
    below keeps exactly the order it is written in.

    Empty off the internal-speaker endpoint, so main() calls it
    unconditionally and the guard lives with the work.
    """
    found: list[Finding] = []
    # Some laptops gate their woofers behind a smart-amp firmware-load ALSA
    # control (issue #17). Only relevant when tuning the internal speakers —
    # irrelevant for headphone/other endpoints.
    if endpoint == "internal_speaker":
        gate_finding = report_speaker.warn_speaker_firmware_gate(
            speakers.detect_speaker_firmware_gates())
        if gate_finding is not None:
            found.append(gate_finding)
        # A hidden woofer pin leaves half the speakers unconfigured, so the
        # preset shapes the tweeters alone (issue #53). Gathering speaker info
        # is a handful of /proc reads; only reached on the speaker endpoint.
        speaker_info = report_speaker._gather_speaker_pins()
        pin_finding = report_speaker.warn_hidden_speaker_pin(
            speakers.find_hidden_speaker_pin(speaker_info), speaker_info)
        if pin_finding is not None:
            found.append(pin_finding)
        # The negative signal: no fixup exists for this machine, so we can't
        # tell a hidden woofer from a plain stereo pair. Only its owner can.
        count_finding = report_speaker.unlisted_speaker_pin_finding(speaker_info)
        if count_finding is not None:
            _print_finding_detail(count_finding)
            found.append(count_finding)
        # An old kernel can mis-configure the speaker path below any preset
        # (issue #33) — hint at it, softly, when the series is old.
        environment.warn_old_kernel()
    return found


@dataclass
class RunTally:
    """Everything the per-profile loop accumulates for the closing block to
    read. One record rather than five parallel locals: the loop visits up to
    nine profiles and every one of them adds to all five, so they are one
    thing with five faces, and naming them as one is what keeps the loop's
    inputs readable at its head."""

    # Preset names in emission order. --autoload with no name takes the first
    # (see _configure_autoload), so the order is part of the contract.
    all_preset_names: list[str] = field(default_factory=list)
    # preset name → the stem of the impulse it references (the name carries a
    # content hash, so it is only known once the FIR is built).
    kernel_by_preset: dict[str, str] = field(default_factory=dict)
    # filter name → set of profile labels that emitted it. Lets the
    # end-of-run --disable hint say *which* profiles each suggestion
    # actually touches, so a user autoloading one preset isn't misled
    # into thinking a filter applies to them when it only runs in other
    # profiles.
    filters_by_profile: dict[str, set[str]] = field(default_factory=dict)
    # Findings raised across every profile built this run, in first-seen order
    # and de-duplicated by slug: --all-profiles would otherwise repeat the same
    # one nine times. The key is the slug rather than the rendered text because
    # several findings embed a per-profile value (peak-level=-3), which made
    # text-keyed de-duplication miss them.
    findings: dict[str, Finding] = field(default_factory=dict)
    # slug → profiles that raised it, so the closing block can say when one
    # applies to some profiles and not the preset the user will autoload.
    raised_in: dict[str, list[str]] = field(default_factory=dict)
    # Leveler substages seen anywhere this run. A dict keyed to None rather
    # than a set, because the closing block reports it as a list and insertion
    # order is the only order that means anything.
    leveler_substages: dict[str, None] = field(default_factory=dict)


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
    # Serve a shell tab-completion request: on a TAB press argcomplete answers
    # on fd 8 and exits inside autocomplete(), so nothing below here runs.
    # Written out rather than kept behind a helper because the wrapper's own
    # copy differs — it attaches both converters' completer tables to its
    # composed parser — so one helper for the two call sites would need a
    # parameter to say which.
    if argcomplete is not None:
        _attach_completers(parser)
        argcomplete.autocomplete(parser)
    args = parser.parse_args(argv)
    args.staged = staged
    report_findings._TAG_CONVENTION_SHOWN = False
    if args.no_color:
        console._disable_color()
    # Below --no-color so the refusal honours it, above everything else: this
    # run has nowhere useful to write and nothing true to report, so it should
    # not reach --speaker-info, --doctor or a flag conflict first.
    console.refuse_root()
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
        report_speaker.report_speaker_info()
        return

    if args.doctor:
        doctor_run.report_doctor(args)
        return

    # Resolve the XML file path
    if args.xml_file and args.windows:
        parser.error("specify either xml_file or --windows, not both")
    elif args.windows:
        xml_path = discover.find_tuning_xml(args.windows, best_guess=args.best_guess)
        console.cprint("ok", f"Auto-detected: {doctor.tilde(xml_path)}")
    elif args.xml_file:
        xml_path = args.xml_file
    else:
        # An auto-detection miss/ambiguity is an environment condition, not
        # CLI misuse — let it propagate to the top-level handler so it prints
        # as a clean error (no usage banner) that points at --help. Routing it
        # through parser.error() would slap the usage synopsis on top and exit
        # 2, framing it as a syntax error the user can't fix by reading usage.
        windows_root = discover.autoprobe_dolby_source()
        xml_path = discover.find_tuning_xml(windows_root, best_guess=args.best_guess)
        console.cprint("ok", f"Auto-detected: {doctor.tilde(xml_path)}")

    # Handed over the moment it is known, not at the end: a run that fails
    # further down still leaves the caller able to say which file it was
    # working from.
    if resolved is not None:
        resolved["xml_path"] = xml_path

    is_soundwire = discover.is_soundwire_xml(Path(xml_path).name)

    if args.list:
        console.cprint("head", f"Endpoints and profiles in {doctor.tilde(xml_path)}:")
        parse.list_endpoints(xml_path)
        return

    if args.dry_run:
        console.cprint("head", "Dry run: no files will be written to disk.")

    # Determine which profiles to process
    if args.all_profiles:
        profile_types = parse.get_profile_types(xml_path, args.endpoint, args.mode)
        if not profile_types:
            console.cprint("warn", f"No profiles found for endpoint={args.endpoint} mode={args.mode}")
            return
        console.cprint("head", f"Generating presets for all {len(profile_types)} profiles: {', '.join(profile_types)}")
    else:
        profile_types = [args.profile]  # None means "first profile"

    # The DSP stack, imported here and not at the top of the file: numpy is
    # ~0.35 s of a ~0.5 s start-up, and every path that returns above reaches
    # none of it — --version, --list, --doctor, --speaker-info, an argparse
    # error, and a tab completion (argcomplete re-runs the whole script on
    # *every* TAB press, exiting inside autocomplete()). This file names no
    # numpy of its own; `emit` and `profile` are what pull it in, and they are
    # here rather than at the top for that reason — between them they reach
    # numpy, scipy and lib.preset.{fir,build,plugins}, so importing either
    # eagerly would undo the deferral. Everything this file does import at the
    # top — console, doctor, ee_paths; dax.{discover,parse};
    # hardware.{speakers,sinks}; preset.autoload; report.{findings,speaker,
    # doctor_run,environment,messages} — reaches no numpy. That is the whole
    # predicate, and it is not "stdlib-only": console owns the optional rich
    # import and still belongs at the top, because rich costs milliseconds
    # where the DSP stack costs ~0.35 s.
    #
    # Before the loop rather than inside it, which is the same thing minus the
    # repetition: profile_types is never empty by here (the empty case
    # returned above) and always holds at least one entry, so the loop body
    # runs on exactly the paths that reach this line.
    try:
        from lib.preset import emit
        # Aliased on report_findings/report_speaker's precedent: profile_type,
        # profile_label and profile_findings are all bound within a few lines
        # of the one call this module serves.
        from lib.report import profile as report_profile
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] == "lib":
            # Two of the three imports above are first-party, and a module
            # missing from inside their graph — `lib/preset/build.py` does
            # `from lib.preset.bands import …`, two hops down — arrives here as
            # the same exception numpy does. There is nothing to install for
            # one. Re-raised as itself: a traceback naming the module is what
            # says so, where the message below would send someone to
            # requirements.txt for a bug in this repo. It is also what keeps
            # that message honest, since it can now only ever name a real
            # dependency.
            #
            # Narrower than it looks, and deliberately not relied on: `emit`
            # itself going missing raises `ImportError`, which this never sees,
            # because the import system swallows `ModuleNotFoundError` for a
            # name in the fromlist.
            raise
        # Someone who cloned the repo and skipped the install step lands here,
        # and on the default single-profile path nothing has been printed yet:
        # this is the whole screen, so it has to say what to install on its own.
        #
        # ModuleNotFoundError, not ImportError: a numpy that is installed but
        # fails to load is a different problem, and its traceback is the only
        # thing that says so. exc.name, not the word "numpy": emit pulls scipy
        # too, and telling someone to install numpy when scipy is the missing
        # one sends them round in a circle.
        #
        # Raised rather than printed here, per the convention the auto-detection
        # failure above documents — the top-level handler in run_cli() renders
        # it as a clean error and returns 1. The remedy rides on `next_step`
        # rather than in the sentence: it is a command, and on a machine
        # os-release cannot place it is one command per distribution, neither
        # of which survives being folded into prose. Not `no_next_step`, whose
        # empty string exists for a sentence that already ends on the thing to
        # do — this one hands over the thing to do itself, and the generic
        # --help pointer still never appears.
        failure = RuntimeError(
            f"{exc.name} is not installed, and generating a preset needs it.")
        # Both DSP dependencies in the command, though the sentence names only
        # the one that stopped this run: both are hard requirements, so a
        # reader who installs just the named one meets the other on the next
        # run. The sentence stays on `exc.name` for the reason above — it is
        # what their machine actually reported.
        failure.next_step = (
            ("cta", "Install them:"),
            # Indented under the lead-in: run_guarded gives every line of a
            # next_step the same margin, so the command needs its own to read
            # as the thing "Install them:" is pointing at.
            *packages.install_steps([packages.NUMPY, packages.SCIPY],
                                    packages.README_INSTALL_SECTION, "  "),
            # The answer on a distribution the table doesn't list, and for
            # anyone who would rather not touch system packages at all.
            ("dim", "or in a virtualenv:  pip install -r requirements.txt"),
        )
        raise failure from exc

    # Created here rather than beside the dry-run banner above: below every
    # early return, and below the import that a machine without numpy dies
    # on, so a run that produces nothing leaves nothing behind in the user's
    # EasyEffects tree either. The banner itself stays up there, ahead of the
    # first line the run prints.
    if not args.dry_run:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        args.irs_dir.mkdir(parents=True, exist_ok=True)

    tally = RunTally()

    for profile_type in profile_types:
        profile_label = profile_type or "default"
        # Build name base: prefix[-Mode][-Profile]
        # When --all-profiles is used, always include the profile name.
        name_parts = [args.prefix]
        if args.mode != "normal":
            name_parts.append(args.mode.title())
        if profile_type or args.all_profiles:
            safe_profile = parse.sanitize_profile_type(profile_type or "default")
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

        profile_findings = report_profile._report_parsed_profile(
            tuning, disabled,
            args.volmax_slot, enabled=set(args.enable),
            is_soundwire=is_soundwire, verbose=args.verbose)

        for finding in [*tuning.findings, *profile_findings]:
            tally.findings.setdefault(finding.slug, finding)
            tally.raised_in.setdefault(finding.slug, []).append(profile_label)
        tally.leveler_substages.update(dict.fromkeys(tuning.leveler_substages))

        emit._emit_ieq_presets(tuning, name_base, is_soundwire, disabled,
                               args, profile_label, tally.all_preset_names,
                               tally.filters_by_profile, tally.kernel_by_preset,
                               # ⚠ hints print warn-styled above; the check
                               # verdict goes dim on those runs so green never
                               # reads as cancelling a warning (round 9).
                               warned=any(f.kind == "hint"
                                          for f in [*tuning.findings,
                                                    *profile_findings]))

        # The closing block, ~120 lines down, wants two scalars off `tuning`.
        # Bound here rather than read off the loop variable down there, so
        # that "the last profile's" is a choice made where the loop makes it:
        # under --all-profiles this runs up to nine times, and reaching back
        # into `tuning` afterwards said nothing about which one it meant.
        #
        # Only "last" for profile_used, and only nominally: its one reader is
        # gated on a single-profile run, where last is also only.
        last_profile_used = tuning.profile_used
        # No "last" for this one — <setting><default_profile> is read off the
        # document root, not the profile, so every iteration parses the same
        # value out of the same file.
        default_profile = tuning.default_profile

    _configure_autoload(args, tally.all_preset_names)

    # A requested --enable that never produced an active stage is silent
    # otherwise: make_autogain returns None when the XML's volume leveler is
    # disabled, so the flag can't do anything and the preset is unchanged.
    # First of the closing output because it answers something the user typed,
    # rather than something we noticed.
    if ("autogain" in args.enable
            and "autogain-active" not in tally.filters_by_profile):
        print()
        console._cprint_wrapped("warn", "--enable autogain had no effect: this "
                                "tuning's volume leveler is disabled in the "
                                "XML, so there is no leveler stage to "
                                "activate. The preset is unchanged.")
    if "virtual-bass" in args.enable:
        if "virtual-bass-active" in tally.filters_by_profile:
            # The flag worked, but the audible half lives elsewhere: EE's
            # serial pipeline can't express the parallel branch, so the
            # preset only records the values for the PipeWire converter.
            # Under the wrapper (staged=True) that converter is the very
            # next step, so "run dolby_to_pipewire.py" would tell the user
            # to run the command they are already inside.
            print()
            if staged:
                # No EasyEffects aside here: the wrapper's reader chose the
                # PipeWire path to avoid EE, and the next step is where the
                # stage becomes real for them (reviewer round, 2026-08-21).
                console._cprint_wrapped("", "--enable virtual-bass: recorded "
                                        "— the next step builds it into the "
                                        "PipeWire chain.")
            elif args.dry_run:
                # "recorded" would contradict the dry-run banner's "nothing
                # was written" (reviewer round, 2026-08-21) — use the same
                # would-style as the rest of the dry-run copy.
                console._cprint_wrapped("", "--enable virtual-bass: the real "
                                        "run's presets would carry it for "
                                        "dolby_to_pipewire.py, which builds "
                                        "the audible stage into a PipeWire "
                                        "chain — EasyEffects itself can't "
                                        "express it.")
            else:
                console._cprint_wrapped("", "--enable virtual-bass: recorded "
                                        "for the PipeWire converter. "
                                        "EasyEffects itself can't express "
                                        "this stage — run "
                                        "dolby_to_pipewire.py to hear it.")
        else:
            print()
            if is_soundwire and "bass-enhancer" in args.disable:
                console._cprint_wrapped("warn", "--enable virtual-bass had no "
                                        "effect: it isn't built for SoundWire "
                                        "tunings (their bass-enhancer "
                                        "stand-in was dropped by --disable "
                                        "bass-enhancer). The preset is "
                                        "unchanged.")
            elif is_soundwire:
                # "our stage", not the tuning's: bass_enh_enable is 0 on
                # every corpus row, so the XML never asks for one. Load-
                # bearing — "SoundWire tunings ship a bass-enhancer" credited
                # Dolby with a stage this converter invents from the PEQ.
                console._cprint_wrapped("warn", "--enable virtual-bass had no "
                                        "effect: it isn't built for SoundWire "
                                        "tunings — their presets already "
                                        "carry our bass-enhancer stage "
                                        "standing in for it (an "
                                        "approximation, not the Dolby one). "
                                        "The preset is unchanged.")
            else:
                console._cprint_wrapped("warn", "--enable virtual-bass had no "
                                        "effect: this XML has no usable "
                                        "virtual-bass block to derive the "
                                        "stage from. The preset is unchanged.")
    if ("coupled-bands" in disabled
            and "coupled-bands-dropped" not in tally.filters_by_profile):
        print()
        # Three ways to get here, and only the last is about the tuning.
        # Blaming the XML for the reader's own second flag is the mistake
        # `disabled_by_flag` avoids on the leveler gap below.
        if "regulator" in args.disable:
            console._cprint_wrapped("warn", "--disable coupled-bands had no "
                                    "effect: --disable regulator already "
                                    "dropped the limiter it extends.")
        elif "regulator" not in tally.filters_by_profile:
            console._cprint_wrapped("warn", "--disable coupled-bands had no "
                                    "effect: this tuning has no regulator to "
                                    "extend.")
        else:
            # "full-scale", not "0 dBFS": the eligibility predicate is
            # >= 0, so a zone above full scale qualifies too.
            console._cprint_wrapped("warn", "--disable coupled-bands had no effect: this "
                                    "tuning's regulator has no full-scale zone "
                                    "whose bands are all marked non-isolated "
                                    "(isolated_band), so there was nothing to "
                                    "drop. The preset is unchanged.")

    # Environment blockers first within the troubleshooting band: each means
    # the system won't play this correctly whatever the preset says, so there
    # is no point offering filter tweaks above them.
    for finding in _speaker_environment_findings(args.endpoint):
        tally.findings.setdefault(finding.slug, finding)

    # Proactively flag an EasyEffects install that can't use what we just wrote
    # — the failure mode #22 surfaced (a correct preset silently inaudible
    # because of the environment, e.g. EE 7 or a wrong install location).
    # Silent on the happy path; reuses --doctor's probes.
    if not args.skip_ee_check:
        doctor_run.warn_ee_environment(args)

    # The two findings raised after the per-profile loop rather than inside
    # it. They have no mid-run site to report from, so their detail prints
    # here, where they are worked out; only their one-line ask goes on to the
    # closing block.
    #
    # Experimental emissions are numerically verified but have never been
    # confirmed by ear, and a user with an affected device is the only way
    # that changes — so they ask rather than merely announcing themselves.
    fired = [k for k in messages.EXPERIMENTAL_MARKERS
             if k in tally.filters_by_profile]
    experimental = [messages.EXPERIMENTAL_MARKERS[k] for k in fired]
    if experimental:
        # Only the markers that are also --disable names give the user an A/B;
        # "mbc-1band" and "coupled-bands-active" have no flag of their own.
        tally.findings.setdefault(
            "unconfirmed-by-ear", report_findings._experimental_finding(
                ", ".join(experimental),
                [k for k in fired if k in messages.DISABLEABLE_FILTERS]))
        _print_finding_detail(tally.findings["unconfirmed-by-ear"])

    # --enable level-restore is the one experimental path that HAS been
    # heard (dev device, 2026-08-18: loud speech picked up artifacts), so it
    # gets its own finding rather than the never-heard wording above. Gated
    # on the marker, not the flag: the flag does nothing on a tuning whose
    # peak the convolver never removed.
    if "level-restore-active" in tally.filters_by_profile:
        tally.findings.setdefault(
            "level-restore", report_findings._level_restore_finding())
        _print_finding_detail(tally.findings["level-restore"])

    # Gated on the leveler actually running, not on the flag being passed:
    # --enable autogain does nothing when the XML disables the leveler, and
    # escalating on the flag alone contradicted the "had no effect" warning
    # printed a few lines above on exactly those devices.
    substage_finding = report_findings._leveler_gap_finding(
        list(tally.leveler_substages),
        autogain_on="autogain-active" in tally.filters_by_profile,
        # "autogain" is the marker for a leveler that shipped bypassed but
        # could be switched on; absent means the XML disabled it outright —
        # or that --disable autogain cleared it, which the flag branch owns
        # so the tuning doesn't get blamed for the reader's own choice.
        autogain_available="autogain" in tally.filters_by_profile,
        disabled_by_flag="autogain" in args.disable)
    if substage_finding is not None:
        tally.findings.setdefault(substage_finding.slug, substage_finding)
        _print_finding_detail(substage_finding)

    # Stamp the scope on last, once every profile has been seen. Findings
    # raised everywhere carry none, so a single-profile run — the default —
    # never shows one.
    def _scope(finding):
        seen = list(dict.fromkeys(tally.raised_in.get(finding.slug, [])))
        if not seen or len(seen) == len(profile_types):
            return finding
        # Naming them beats counting them right up until the list is longer
        # than the sentence it annotates; nine profiles listed in full is
        # noise where "6 of 9 profiles" is the same answer.
        label = (", ".join(seen) if len(seen) <= 3
                 else f"{len(seen)} of {len(profile_types)} profiles")
        return replace(finding, scope=label)

    scoped = [_scope(f) for f in tally.findings.values()]

    # A wrapper takes the menu along with the closing ask (round 4: printed
    # at [1/3] it told the reader what to re-run before setup had finished,
    # with two more phases of output below it) — stashed here, printed by
    # the wrapper at its own end.
    menu_printed = False
    if troubleshooting is not None:
        troubleshooting.update(
            findings=scoped,
            filters_by_profile=tally.filters_by_profile,
            enabled_by_flag=frozenset(args.enable))
    else:
        menu_printed = messages.print_troubleshooting(
            scoped, tally.filters_by_profile,
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
            profile_used = last_profile_used
            n_modes = len(parse.get_profile_types(xml_path, args.endpoint,
                                                  args.mode))
        messages.print_what_now(tally.all_preset_names, bool(args.autoload), args.dry_run,
                       output_dir=args.output_dir,
                       profile_used=profile_used, n_modes=n_modes or 0,
                       default_unknown=(args.profile is None
                                        and default_profile is None),
                       # "autogain" marker = leveler present but bypassed
                       # (the --enable-menu state); -active = running.
                       autogain_off=("autogain" in tally.filters_by_profile
                                     and "autogain-active"
                                     not in tally.filters_by_profile),
                       menu_printed=menu_printed,
                       declared_default=(default_profile
                                         if args.profile is None else None),
                       virtual_bass_pw=("virtual-bass-active"
                                        in tally.filters_by_profile))

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
    inline, as a return code — the seam dolby_to_pipewire.py calls in-process.

    The handling itself is console.run_guarded, shared with the other two
    entry points. Guarded here rather than under ``__main__`` because this is
    the seam: the wrapper calls it directly, and a failure rendered by any
    other name would reach it as an exception instead of a return code."""
    return console.run_guarded(
        lambda: main(argv, closing=closing, troubleshooting=troubleshooting,
                     resolved=resolved, staged=staged))


if __name__ == "__main__":
    sys.exit(run_cli())
