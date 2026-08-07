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
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import field, replace
from pathlib import Path

from lib import console, ee_paths, version
from lib.data import kernel_releases
from lib.dax import discover, parse
from lib.hardware import speakers
# Aliased: main() binds a local named `sinks` for the resolver's result, which
# would shadow the module for every later line that reads through it.
from lib.hardware import sinks as hardware_sinks
from lib.preset import autoload, bands
# Aliased: main() binds a local named `findings`, which would shadow the
# module for the one line that resets _TAG_CONVENTION_SHOWN through it.
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


def _load_dsp() -> None:
    """Import the DSP stack into module globals.

    NumPy and SciPy are ~0.4 s of this script's ~0.5 s startup, and
    argcomplete re-runs the whole script on *every* TAB press, exiting inside
    autocomplete() long before any DSP code is reached. So the completion path
    skips them and complete_and_load() imports them once it knows this is a
    real run. The `from __future__ import annotations` above is what makes
    that legal: `np.ndarray` in a signature is a string, not a lookup.

    Every lib module that reaches numpy is bound here for the same reason and
    not at the top of this file: importing one of them eagerly would undo the
    deferral this function exists for. lib.preset.fir imports numpy itself,
    lib.preset.build reaches it through lib.preset.plugins (the sample rate
    its MBC time constants decode against), and lib.report.profile through
    both. What this file imports at the top instead — bands, autoload,
    findings, messages, speaker, doctor_run, environment — is stdlib-only.
    """
    global np, wavfile, fir, build, report_profile
    import numpy as np
    from scipy.io import wavfile
    from lib.preset import build, fir
    # Aliased on report_findings/report_speaker's precedent: main() binds
    # profile_type, profile_label and profile_findings within a few lines of
    # the one call this module serves.
    from lib.report import profile as report_profile


if "_ARGCOMPLETE" not in os.environ:
    _load_dsp()


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


# Raised all over this file and consumed by the closing block, so the record
# type is shared rather than owned (see lib/report/findings.py). Kept under
# the names the rest of this file already uses.
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


# Verdict gate for the printed FIR verification: far above the minimum-phase
# design's normal residual (~0.05 dB at the 20 probe points) and below
# anything audible, so it warns only when the reconstruction actually broke.
FIR_VERIFY_OK_DB = 0.5


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
                   for label, key in messages.VOICING_CURVES.items()}

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
        default=ee_paths.DEFAULT_AUTOLOAD_DIR,
        help=f"EasyEffects autoload directory (default: {ee_paths.DEFAULT_AUTOLOAD_DIR})",
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
        default=ee_paths.DEFAULT_OUTPUT_DIR,
        help=f"EasyEffects output preset directory (default: {ee_paths.DEFAULT_OUTPUT_DIR})",
    )
    add(
        "--irs-dir",
        type=Path,
        default=ee_paths.DEFAULT_IRS_DIR,
        help=f"EasyEffects impulse response directory (default: {ee_paths.DEFAULT_IRS_DIR})",
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
        console.cprint("ok", f"Auto-detected: {xml_path}")
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
        console.cprint("ok", f"Auto-detected: {xml_path}")

    # Handed over the moment it is known, not at the end: a run that fails
    # further down still leaves the caller able to say which file it was
    # working from.
    if resolved is not None:
        resolved["xml_path"] = xml_path

    is_soundwire = discover.is_soundwire_xml(Path(xml_path).name)

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

        profile_findings = report_profile._report_parsed_profile(
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
                ee_paths.DEFAULT_EASYEFFECTS_RC, autoload.BYPASS_PRESET_NAME, dry_run=args.dry_run,
            )
            if fallback_status == "already-configured":
                console.cprint("ok", f"  Fallback preset already configured "
                              f"('{existing}') in {ee_paths.DEFAULT_EASYEFFECTS_RC} — leaving as-is")
            elif fallback_status == "would-patch":
                console.cprint("ok", f"  Would enable fallback preset in {ee_paths.DEFAULT_EASYEFFECTS_RC}")
            else:
                console.cprint("ok", f"  Enabled fallback preset in {ee_paths.DEFAULT_EASYEFFECTS_RC}")
                if doctor_run.easyeffects_is_running():
                    console.cprint("warn", "  EasyEffects is currently running — restart it for "
                                   "the fallback setting to take effect (EE rewrites "
                                   "this file on exit).")

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
        gate_finding = report_speaker.warn_speaker_firmware_gate(
            speakers.detect_speaker_firmware_gates())
        if gate_finding is not None:
            findings.setdefault(gate_finding.slug, gate_finding)
        # A hidden woofer pin leaves half the speakers unconfigured, so the
        # preset shapes the tweeters alone (issue #53). Gathering speaker info
        # is a handful of /proc reads; only reached on the speaker endpoint.
        speaker_info = report_speaker._gather_speaker_pins()
        pin_finding = report_speaker.warn_hidden_speaker_pin(
            speakers.find_hidden_speaker_pin(speaker_info), speaker_info)
        if pin_finding is not None:
            findings.setdefault(pin_finding.slug, pin_finding)
        # The negative signal: no fixup exists for this machine, so we can't
        # tell a hidden woofer from a plain stereo pair. Only its owner can.
        count_finding = report_speaker.unlisted_speaker_pin_finding(speaker_info)
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
        doctor_run.warn_ee_environment(args)

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
