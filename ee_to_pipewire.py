#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""Convert an EasyEffects output preset (the JSON `dolby_to_easyeffects.py`
emits) into a PipeWire `filter-chain` `.conf`.

Scope (see docs/ee-to-pipewire.md for full detail):
  - convolver, equalizer (PEQ), equalizer (dialog), multiband_compressor
    (MBC + regulator), limiter, and autogain are translated (LSP-backed);
    bass_enhancer and stereo_tools are translated (Calf-backed).
  - autogain (EE-native libebur128 volume leveler) → LSP autogain_stereo,
    a K-weighted loudness AGC. Bypassed instances (the HDA default) are
    skipped silently; active ones (SoundWire, or HDA with
    --enable autogain) are translated.
  - Stereo only; no 4-channel upmix. By default the conf is a
    WirePlumber 0.5+ smart filter pinned to the auto-detected
    internal-speaker sink (--target-sink overrides; '' gives a plain
    v1 virtual sink).

Reads the EE preset JSON, walks `plugins_order`, dispatches each plugin
to a stage emitter, generates pair-wise stereo links, and writes a
PipeWire SPA-JSON conf. Stage parameters round-trip with the source
preset to 4 decimals (dB → linear conversions are explicit).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from lib import console, ee_paths, version
from lib.hardware import sinks
from lib.pipewire import checks, install
# Aliased: main() binds a local named `conf` for the rendered conf text, which
# would shadow the module for every later line that reads through it.
from lib.pipewire import conf as pw_conf

# Optional tab-completion (README "Shell tab-completion"). Absent argcomplete, this
# module stays stdlib-only and behaves exactly as before.
try:
    import argcomplete
except ImportError:
    argcomplete = None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _make_adder(container, only):
    """Shared-group plumbing: an ``add_argument`` wrapper that skips flags not
    selected by ``only`` (keyed by primary option string) and records the
    added actions so dolby_to_pipewire.py can rebuild a child argv from them.
    Deliberate double of dolby_to_easyeffects._make_adder — importing it here
    would drag that script's NumPy/SciPy into this one's dependency-free path.
    """
    added = []

    def add(*names, **kwargs):
        if only is None or names[0] in only:
            added.append(container.add_argument(*names, **kwargs))

    return add, added


def add_routing_args(container, *, only=None):
    """Routing flags (dolby_to_pipewire.py shares --target-sink)."""
    add, added = _make_adder(container, only)
    add(
        "--target-sink",
        default=None,
        help="hardware sink (node.name) the filter should attach to as "
             "a WirePlumber smart filter. When set, apps target this "
             "sink as usual and the filter inserts itself into the path; "
             "no virtual-sink stacking, automatic bypass on HDMI / "
             "Bluetooth / USB outputs. Default: auto-detect the "
             "internal-speaker sink via pw-dump (same probe "
             "dolby_to_easyeffects.py --autoload uses). Pass an empty "
             "string ('') to disable smart-filter mode and emit the "
             "v1 virtual-sink conf (apps target effect_input.<name> "
             "directly).",
    )
    add(
        "--target-object",
        default=None,
        help="bind the chain's playback to a specific downstream node "
             "(node.name) instead of letting WirePlumber choose. Useful "
             "for routing into a measurement null sink. End users "
             "usually want --target-sink instead, which is set by "
             "default and uses WirePlumber 0.5+ smart-filter routing "
             "so apps don't see the chain as a separate sink.",
    )
    return added


def default_conf_path(node_name: str) -> Path:
    """Where the conf lands when ``--output`` doesn't say.

    Three readers have to agree on this: the real write, the ``--dry-run``
    preview — whose whole job is to name the file a real run would produce —
    and the ``--output`` help text below. The help gets it from here too, by
    passing the literal ``<node-name>`` as the stem: the placeholder survives
    ``expanduser()`` untouched (it only ever rewrites a leading ``~``), so the
    sentence a user reads is rendered by the code it describes instead of
    restating it, and cannot drift from it.
    """
    return (checks.DEFAULT_OUTPUT_DIR / f"{node_name}.conf").expanduser()


def add_output_args(container, *, only=None):
    """Output naming/location flags (dolby_to_pipewire.py shares --force)."""
    add, added = _make_adder(container, only)
    add(
        "--output",
        type=Path,
        default=None,
        help=f"output .conf path (default: {default_conf_path('<node-name>')})",
    )
    add(
        "--node-name",
        default=None,
        help=f"PipeWire node-name suffix; sanitised to [A-Za-z0-9_]. "
             f"Default: derived from the preset filename stem "
             f"(e.g. Dolby-Balanced.json → Dolby_Balanced), so "
             f"converting multiple presets produces distinct sink "
             f"names without collision. Falls back to "
             f"{pw_conf.DEFAULT_NODE_NAME!r} if the stem is empty after "
             f"sanitisation.",
    )
    add(
        "--node-description",
        default=None,
        help=f"human-readable node description. Default: derived "
             f"from the preset filename stem (e.g. \"Dolby-Balanced\"), "
             f"falling back to {pw_conf.DEFAULT_NODE_DESCRIPTION!r}.",
    )
    add(
        "--force",
        action="store_true",
        help="overwrite the output file if it already exists",
    )
    return added


def add_impulse_response_args(container, *, only=None):
    """Impulse-response flags — never shared with the wrapper (it stages the
    .irs in a tempdir and must keep the default copy-beside-conf behavior)."""
    add, added = _make_adder(container, only)
    add(
        "--irs-dir",
        type=Path,
        default=ee_paths.DEFAULT_IRS_DIR,
        help=f"directory containing the .irs file referenced by the "
             f"preset's convolver (default: {ee_paths.DEFAULT_IRS_DIR})",
    )
    add(
        "--no-copy-irs",
        action="store_true",
        help="don't copy the .irs next to the generated conf. By default "
             "the converter copies the impulse response from --irs-dir "
             "into the conf's directory and rewrites the convolver "
             "filename, so the PipeWire chain has no runtime dependency "
             "on the EasyEffects path layout. Pass this flag to keep the "
             "conf pointing at the original EE-side .irs (which lets EE "
             "preset regenerations propagate automatically, at the cost "
             "of a brittle cross-tree dependency).",
    )
    return added


def add_general_args(container, *, only=None):
    """General flags (dolby_to_pipewire.py shares --no-validate)."""
    add, added = _make_adder(container, only)
    add(
        "--no-validate",
        action="store_true",
        help="skip the schema self-check against lv2info port metadata. "
             "By default, after generating the conf, ee_to_pipewire shells "
             "out to tools/measure_pw/validate_conf.py to catch unknown "
             "port symbols and out-of-range values; pass this flag to "
             "skip it (e.g. on systems without lv2info installed).",
    )
    add(
        "--dry-run",
        action="store_true",
        help="report where the conf and impulse response would be written "
             "without writing them; missing IRS files become warnings "
             "rather than errors. To get the conf itself without "
             "installing it, run without this flag and point --output at a "
             "path of your own",
    )
    add(
        "--skip-next-steps",
        action="store_true",
        help="drop the post-write next-steps checklist (restart PipeWire, "
             "verify the sink, quit EasyEffects) — for callers that handle "
             "activation themselves. Standalone, a one-line activation "
             "pointer replaces it; dolby_to_pipewire.py passes this "
             "automatically and prints its own steps instead",
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
    # --no-color must be honored before argparse renders --help, so pre-scan
    # argv to pick the help formatter (color itself is disabled after parsing).
    _argv = sys.argv[1:] if argv is None else argv
    formatter_class = (argparse.HelpFormatter
                       if "--no-color" in _argv else console._HelpFormatter)
    epilog = None
    if console._MISSING_COLOR_DEPS:
        epilog = ("Tip: install " + " and ".join(console._MISSING_COLOR_DEPS)
                  + " for colored output (see README for distro packages).")
    parser = argparse.ArgumentParser(
        description="Convert an EasyEffects output preset to a PipeWire "
                    "filter-chain .conf (see docs/ee-to-pipewire.md).",
        formatter_class=formatter_class,
        epilog=epilog,
    )
    parser.add_argument(
        "preset",
        type=Path,
        nargs="?",
        help="path to the EasyEffects preset JSON (the output of "
             "dolby_to_easyeffects.py, e.g. ~/.local/share/easyeffects/output/"
             "Dolby-Balanced.json)",
    )
    group = parser.add_argument_group("inspection")
    group.add_argument(
        "--doctor",
        action="store_true",
        help="report the state of the installed filter chain — stacked "
             "chains, confs that didn't load, a missing impulse response, a "
             "target sink that no longer exists — then exit. Takes no preset; "
             "it inspects what is already installed.",
    )
    add_routing_args(parser.add_argument_group("routing"))
    add_output_args(parser.add_argument_group("output"))
    add_impulse_response_args(parser.add_argument_group("impulse response"))
    add_general_args(parser.add_argument_group("general"))
    return parser


def _complete_sink_names(prefix: str, **_kwargs) -> list[str]:
    """Tab-completion for --target-sink / --target-object: PipeWire node.name
    values, from the same pw-dump boundary _autodetect_speaker_sink() uses."""
    try:
        names = [s.get("name", "") for s in sinks._enumerate_audio_sinks()]
    except Exception:  # a wedged or absent PipeWire must never break TAB
        return []
    return [n for n in names if n.startswith(prefix)]


def _attach_completers(parser: argparse.ArgumentParser) -> None:
    """Tell argcomplete what each value-taking option means — argparse records
    `type=Path` for the preset JSON, the output conf and the IRS directory
    alike, and nothing at all for PipeWire node names."""
    from argcomplete.completers import DirectoriesCompleter, FilesCompleter

    completers = {
        "preset":        FilesCompleter(("json",)),
        "output":        FilesCompleter(("conf",)),
        "irs_dir":       DirectoriesCompleter(),
        "target_sink":   _complete_sink_names,
        "target_object": _complete_sink_names,
    }
    for action in parser._actions:
        completer = completers.get(action.dest)
        if completer is not None:
            action.completer = completer


def main(argv: list[str] | None = None, wrapped: bool = False) -> int:
    """``wrapped`` marks an in-process dolby_to_pipewire.py run: the wrapper
    owns all activation messaging ([3/3] activates, lists the steps, or says
    dry run), so the --skip-next-steps "To activate:" fallback is dropped —
    it printed the identical restart command two lines above [3/3]'s step 1
    (user-review round 5)."""
    parser = build_parser(argv)
    if argcomplete is not None:
        _attach_completers(parser)
        argcomplete.autocomplete(parser)
    args = parser.parse_args(argv)
    if args.no_color:
        console._disable_color()

    # Inspection mode: reads the installed state, converts nothing, so the
    # preset positional is optional — and required for everything else.
    if args.doctor:
        return checks.report_pw_doctor()
    if args.preset is None:
        parser.error("a preset path is required (or --doctor to inspect what "
                     "is already installed)")

    preset_path: Path = args.preset
    if not preset_path.is_file():
        console.cprint("err", f"error: preset not found: {preset_path}")
        return 2
    try:
        preset = json.loads(preset_path.read_text())
    except json.JSONDecodeError as e:
        console.cprint("err", f"error: preset JSON is malformed: {e}")
        return 2

    # Default node-name / -description are derived from the preset
    # filename stem so converting multiple presets produces distinct
    # sinks (e.g. Dolby-Balanced.json → Dolby_Balanced; Dolby-Detailed.json
    # → Dolby_Detailed). Without this, every conversion lands on the
    # same conf path and PW node name. The DEFAULT_NODE_NAME fallback
    # only kicks in for pathological stems (empty after sanitisation).
    if args.node_name is None:
        derived = pw_conf._sanitize_name(preset_path.stem).strip("_")
        node_name = derived if derived else pw_conf.DEFAULT_NODE_NAME
    else:
        node_name = args.node_name
    if args.node_description is None:
        node_description = preset_path.stem or pw_conf.DEFAULT_NODE_DESCRIPTION
    else:
        node_description = args.node_description

    safe_node_name = pw_conf._sanitize_name(node_name)
    default_conf = default_conf_path(safe_node_name)
    output_path: Path | None
    if args.dry_run:
        output_path = None
    elif args.output is not None:
        output_path = args.output.expanduser()
    else:
        output_path = default_conf

    try:
        chain = pw_conf.build_chain(preset, args.irs_dir.expanduser(),
                                    must_exist=not args.dry_run)
    except FileNotFoundError as e:
        console.cprint("err", f"error: {e}")
        return 2

    if not chain.stages:
        console.cprint("err", "error: no stages emitted (preset is empty or every "
                      "plugin was skipped)")
        return 1

    # Resolve where the IRS will live. By default we copy it next to the
    # conf so the PW chain is self-contained; --no-copy-irs keeps the
    # original EE-side absolute path baked into the conf. In dry-run we
    # still compute the destination so the printed conf reflects what a
    # real run would produce.
    if output_path is not None:
        target_irs_dir = output_path.parent
    elif args.output is not None:
        target_irs_dir = args.output.expanduser().parent
    else:
        target_irs_dir = default_conf.parent
    target_irs = target_irs_dir / f"{safe_node_name}.irs"
    src_irs: Path | None = None
    if not args.no_copy_irs:
        src_irs = install._retarget_convolver_irs(chain.stages, target_irs)

    # Resolve the smart-filter target sink. ``--target-sink ''`` (empty
    # string) explicitly disables smart-filter mode; an unset flag falls
    # through to autodetection.
    if args.target_sink == "":
        target_sink: str | None = None
        console.cprint("dim", "[smart-filter] disabled by --target-sink ''; emitting "
                      "v1 virtual-sink conf (apps will target effect_input."
                      f"{safe_node_name} directly)")
    elif args.target_sink:
        target_sink = args.target_sink
        console.cprint("ok", f"[smart-filter] target sink: {target_sink} (from "
                     "--target-sink)")
    else:
        target_sink, detect_warnings = install._autodetect_speaker_sink()
        # Warnings print on both paths: a relaxed-tier match returns a name
        # *and* a warning explaining the fallback.
        for w in detect_warnings:
            console.cprint("warn", f"[smart-filter] {w}")
        if target_sink:
            # Names the override (round 7): without it a reader whose
            # detection picked the wrong device assumed their only path
            # was filing a report. And how to find NAME (round 8): the
            # flag alone left them with no way to discover a value.
            console.cprint("ok", f"[smart-filter] your built-in speakers: "
                         f"{target_sink} "
                         "(autodetected — wrong device? --target-sink NAME "
                         "overrides)")
            console.cprint("dim", "  (list sink names with: pw-cli ls Node | grep "
                          "alsa_output)")
        else:
            console.cprint("warn", "[smart-filter] falling back to v1 virtual-sink "
                           "conf (apps will target effect_input."
                           f"{safe_node_name}); pass --target-sink "
                           "<node.name> to enable smart-filter routing.")

    links = pw_conf.emit_links(chain.stages)
    conf = pw_conf.format_conf(chain.stages, links, node_name,
                               node_description,
                               target_object=args.target_object,
                               target_sink=target_sink,
                               warnings=chain.warnings)

    for w in chain.warnings:
        console.cprint("warn", f"[warn] {w}")

    if not args.no_validate:
        rc, output = install._validate_conf(conf)
        if rc == -1:
            console.cprint("dim", f"[validate] skipped: {output.strip()}")
            # Without lv2info the plugin set can't be checked, so a missing
            # runtime dependency would otherwise pass unnoticed — remind the
            # user what the chain needs.
            console.cprint("warn", "[validate] the plugin set wasn't checked — make "
                           "sure the LV2 plugins this conf uses are installed: "
                           "LSP (lsp-plugins-lv2) for the PEQ / MBC / limiter, "
                           "plus Calf (calf-plugins) if it includes "
                           "bass_enhancer / stereo_tools. Otherwise the chain "
                           "won't load.")
        elif rc == 2:
            # Setup error inside validate_conf.py — degraded gracefully.
            console.cprint("dim", f"[validate] skipped (setup): {output.strip()}")
        elif rc != 0:
            if output.strip():
                console.cprint("err", output.rstrip())
            console.cprint("err", "error: schema validation failed; conf not written")
            return 1
        elif output.strip():
            # Validation passed, but validate_conf still emits warnings — most
            # importantly "no lv2info schema available for <uri>" when a
            # referenced LSP/Calf plugin isn't installed, so its ports
            # couldn't be checked. Surface them; otherwise the conf writes
            # "successfully" while the chain silently fails to load for a
            # missing runtime dependency.
            for line in output.strip().splitlines():
                console.cprint("warn", f"[validate] {line}")

    if args.dry_run:
        # The conf itself is not shown: on a terminal it is a few hundred
        # lines of JSON between the troubleshooting menu and the end of the
        # run, which buries everything either side of it. Someone who wants
        # the text has --output, which writes it wherever they point it
        # without installing anything into the PipeWire drop-in directory.
        would_conf = (args.output.expanduser() if args.output is not None
                      else default_conf)
        would_irs = (target_irs if (src_irs is not None
                     and src_irs.resolve() != target_irs.resolve())
                     else None)
        install._print_results(would_conf, would_irs, dry_run=True)
        return 0

    assert output_path is not None
    if output_path.exists() and not args.force:
        console.cprint("err", f"error: {output_path} exists; pass --force to overwrite")
        return 1

    # IRS copy: skip when source and target are the same path (no-op),
    # otherwise honour the same --force semantics as the conf write.
    copied_irs: Path | None = None
    if src_irs is not None and src_irs.resolve() != target_irs.resolve():
        if target_irs.exists() and not args.force:
            console.cprint("err", f"error: {target_irs} exists; pass --force to "
                          "overwrite")
            return 1
        target_irs.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_irs, target_irs)
        copied_irs = target_irs

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(conf)
    install._print_results(output_path, copied_irs, dry_run=False)
    checks.warn_if_stacked(output_path, target_sink)
    if args.skip_next_steps:
        # Checklist suppressed, but a freshly written conf must never go
        # unmentioned as inactive — keep the one action that makes it live.
        # Unless a wrapper is driving: its [3/3] owns activation.
        if not wrapped:
            console.cprint("cta", f"To activate: {pw_conf.PIPEWIRE_RESTART_CMD}")
    else:
        install._print_next_steps(node_name, target_object=args.target_object)
    return 0


if __name__ == "__main__":
    sys.exit(main())
