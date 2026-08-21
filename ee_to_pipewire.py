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

from lib import console, doctor, ee_paths
from lib.hardware import sinks
from lib.pipewire import checks, install, validate, vbe
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

def add_routing_args(container, *, only=None):
    """Routing flags (dolby_to_pipewire.py shares --target-sink)."""
    add, added = console._make_adder(container, only)
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
    restating it, and cannot drift from it. The help collapses $HOME back to
    ``~`` on the way out — that is the help's business, not this function's,
    which owes its other two readers a real ``Path``.
    """
    return (checks.DEFAULT_OUTPUT_DIR / f"{node_name}.conf").expanduser()


def add_output_args(container, *, only=None):
    """Output naming/location flags (dolby_to_pipewire.py shares --force)."""
    add, added = console._make_adder(container, only)
    add(
        "--output",
        type=Path,
        default=None,
        help="output .conf path "
             f"(default: {doctor.tilde(default_conf_path('<node-name>'))})",
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
    add, added = console._make_adder(container, only)
    add(
        "--irs-dir",
        type=Path,
        default=ee_paths.DEFAULT_IRS_DIR,
        help=f"directory containing the .irs file referenced by the "
             f"preset's convolver (default: {doctor.tilde(ee_paths.DEFAULT_IRS_DIR)})",
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
    add, added = console._make_adder(container, only)
    add(
        "--no-validate",
        action="store_true",
        help="skip the schema self-check against lv2info port metadata. "
             "By default, after generating the conf, ee_to_pipewire reads "
             "the port metadata of every LV2 plugin the conf names and "
             "checks the conf's control values against it, catching unknown "
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
    console.add_color_and_version_args(add)
    return added


def build_parser(argv: list[str] | None = None) -> argparse.ArgumentParser:
    formatter_class, epilog = console.help_style(argv)
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


# What to do after a schema self-check that raised rather than reporting, in
# place of the generic --help pointer: no flag list fixes an LV2 plugin that
# will not answer. Validation runs before the conf is written, on every path
# including --dry-run, so the first half is true wherever it is raised from.
_VALIDATE_NEXT_STEP = ("The conf was not written — re-run with --no-validate "
                       "to skip the schema self-check.")


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
    console.refuse_root()

    # Inspection mode: reads the installed state, converts nothing, so the
    # preset positional is optional — and required for everything else.
    if args.doctor:
        return checks.report_pw_doctor()
    if args.preset is None:
        parser.error("a preset path is required (or --doctor to inspect what "
                     "is already installed)")

    preset_path: Path = args.preset
    if not preset_path.is_file():
        console.cprint("err", f"error: preset not found: {doctor.tilde(preset_path)}")
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
    # Decorated further down, once the routing mode is known: only a smart
    # filter is the "don't pick me" case, and only a derived description is
    # ours to change — someone who passed --node-description asked for that
    # name verbatim.
    describe_as_filter = (args.node_description is None
                          and bool(preset_path.stem))

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
        # The message names the .irs it looked for; the Path itself stays
        # absolute inside the chain, where it becomes a convolver filename
        # module-filter-chain reads without expanding ~.
        console.cprint("err", f"error: {doctor.tilde(e)}")
        return 2

    if not chain.stages:
        console.cprint("err", "error: no stages emitted (preset is empty or every "
                      "plugin was skipped)")
        return 1

    # The generator's --enable virtual-bass records its values as a `_vbe`
    # block; the branch itself only exists here — EasyEffects' serial
    # pipeline can't express it, so this conf is the one place it plays.
    vbe_links: list[dict] = []
    vbe_meta = preset.get("_vbe")
    if isinstance(vbe_meta, dict):
        chain.stages, vbe_links = vbe.wrap_chain(chain.stages, vbe_meta)
        console.cprint("ok", f"[virtual-bass] experimental: deep bass "
                      f"({vbe_meta['src_lo_hz']:g}-{vbe_meta['mix_lo_hz']:g} Hz) "
                      f"your speakers can't physically play is filled in with "
                      f"quieter higher tones ({vbe_meta['mix_lo_hz']:g}-"
                      f"{vbe_meta['mix_hi_hz']:g} Hz) your ear reads as bass "
                      f"(issue #14)")
        console.cprint("dim", "  (sounds wrong? re-run without "
                      "--enable virtual-bass)")

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
        # Detail at the detection site, where the flag that caused it is still
        # in view: this is the mode where the reader has to pick the chain as
        # their output, which is what puts two volume controls in the path.
        console._cprint_wrapped(
            "dim", f"  ({install.V1_SECOND_VOLUME_HINT}; drop --target-sink '' "
            "to attach it to your speakers instead, with nothing to select and "
            "one volume control)", indent="   ")
        # The reading, where the flag that causes it is still in view. Advice
        # ("leave them at 100%") is not the same as a diagnosis ("they are at
        # 40%"), and once this sink is selected the speaker's own level is
        # invisible from the slider the reader will be moving.
        attenuated = install.speaker_attenuation()
        if attenuated:
            console._cprint_wrapped(
                "warn", f"  ({attenuated} — that comes off everything on top "
                "of this sink's own control)", indent="   ")
        # Which control to use has a measured consequence, so say which half is
        # affected: the speaker correction is linear and identical either way;
        # only the compressor's behaviour on loud content moves with it
        # (docs/design-notes.md, issue #63).
        # "compressor and limiter", not "the compressor": the measurement says
        # the MBC, regulator and limiter all see the attenuated signal, and
        # naming one of the three reads as a promise about the other two.
        console._cprint_wrapped(
            "dim", "  (the speaker correction is identical either way — only "
            "the compression and limiting follow this control, on loud "
            "content)", indent="   ")
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
            # Redacted: this name came from the graph, not from the reader. A
            # Bluetooth speaker reaches the strict tier (lib/hardware/sinks.py
            # `_classify_sink`), so "your built-in speakers" can name a headset
            # and print its address. The `--target-sink` echo above is left
            # verbatim on purpose — that one the reader typed.
            console.cprint("ok", f"[smart-filter] your built-in speakers: "
                         f"{doctor.no_bt_address(target_sink)} "
                         "(autodetected — wrong device? --target-sink NAME "
                         "overrides)")
            console.cprint("dim", "  (list sink names with: pw-cli ls Node | grep "
                          "alsa_output)")
        else:
            console.cprint("warn", "[smart-filter] falling back to v1 virtual-sink "
                           "conf (apps will target effect_input."
                           f"{safe_node_name}); pass --target-sink "
                           "<node.name> to enable smart-filter routing.")
            # This path reaches the same v1 conf without the reader having asked
            # for it, so it needs the consequence spelled out too — and it is
            # the one path that never said the chain does nothing until picked.
            console._cprint_wrapped(
                "dim", "  (until you pick it as your output it processes "
                f"nothing, and {install.V1_SECOND_VOLUME_HINT})", indent="   ")
            # No speaker reading on this branch, deliberately: we are here
            # *because* _autodetect_speaker_sink() found nothing, so there is no
            # sink to read a level from. --doctor still catches it later, from
            # the graph, where the chain's actual downstream is visible.

    if target_sink and describe_as_filter:
        node_description += pw_conf.SMART_DESCRIPTION_SUFFIX

    links = pw_conf.emit_links(chain.stages) + vbe_links
    conf = pw_conf.format_conf(chain.stages, links, node_name,
                               node_description,
                               target_object=args.target_object,
                               target_sink=target_sink,
                               warnings=chain.warnings)

    for w in chain.warnings:
        console.cprint("warn", f"[warn] {w}")

    if not args.no_validate:
        try:
            report = validate.run(conf)
        except Exception as e:
            e.next_step = _VALIDATE_NEXT_STEP
            raise
        if report.status == validate.NO_TOOLING:
            console.cprint("dim", f"[validate] skipped: {report.reason}")
            # Without lv2info the plugin set can't be checked, so a missing
            # runtime dependency would otherwise pass unnoticed — remind the
            # user what the chain needs.
            console.cprint("warn", "[validate] the plugin set wasn't checked — make "
                           "sure the LV2 plugins this conf uses are installed: "
                           "LSP (lsp-plugins-lv2) for the PEQ / MBC / limiter "
                           "and the virtual-bass filters, plus Calf "
                           "(calf-plugins) if it includes bass_enhancer / "
                           "stereo_tools or the virtual-bass saturator. "
                           "Otherwise the chain won't load.")
        elif report.status == validate.UNCHECKED:
            # The check could not run at all — degraded gracefully, and the
            # conf is still written.
            console.cprint("dim", f"[validate] skipped (setup): {report.reason}")
        else:
            # Warnings print on a pass too — most importantly "no lv2info
            # schema available for <uri>" when a referenced LSP/Calf plugin
            # isn't installed, so its ports couldn't be checked. Surface them;
            # otherwise the conf writes "successfully" while the chain
            # silently fails to load for a missing runtime dependency.
            #
            # They print before the errors and in their own style, and say
            # which they are: a run that fails renders both, and a warning
            # that arrives in the failure's colour with no word to correct it
            # reads as one of the reasons the conf was refused.
            for w in report.warnings:
                console.cprint("warn", f"[validate] warning: {w}")
            if report.status == validate.ERRORS:
                for err in report.errors:
                    console.cprint("err", f"[validate] error: {err}")
                console.cprint("err", "error: schema validation failed; conf "
                               "not written")
                return 1

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
        console.cprint("err", f"error: {doctor.tilde(output_path)} exists; pass "
                      "--force to overwrite")
        return 1

    # IRS copy: skip when source and target are the same path (no-op),
    # otherwise honour the same --force semantics as the conf write.
    copied_irs: Path | None = None
    if src_irs is not None and src_irs.resolve() != target_irs.resolve():
        if target_irs.exists() and not args.force:
            console.cprint("err", f"error: {doctor.tilde(target_irs)} exists; pass "
                          "--force to overwrite")
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
        install._print_next_steps(node_name, target_object=args.target_object,
                                  selectable=target_sink is None)
    return 0


def run_cli(argv: list[str] | None = None) -> int:
    """main() under the shared failure rendering — the generator's run_cli by
    the same name and for the same reason.

    Not what dolby_to_pipewire.py calls: it drives ``main(wrapped=True)``
    directly, so a conversion failure travels up as an exception and is
    rendered once, by the wrapper's own guard, rather than here and again
    there."""
    return console.run_guarded(lambda: main(argv))


if __name__ == "__main__":
    sys.exit(run_cli())
