#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""One command from Dolby DAX3 tuning XML to an active PipeWire filter chain.

Orchestrates the two existing converters without duplicating them:
dolby_to_easyeffects.py generates the EasyEffects preset + impulse response
into a throwaway temporary directory (nothing is installed under the
EasyEffects tree), ee_to_pipewire.py converts the chosen variant(s) into a
self-contained PipeWire filter-chain conf (the .irs is copied beside it),
then PipeWire is restarted and the sink verified (--no-activate opts out).
See docs/ee-to-pipewire.md.
"""

import argparse
import contextlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Step 1 runs in-process, so this pulls in the generator's NumPy/SciPy —
# acceptable: every non-inspection run executes that DSP anyway. (A tab
# completion is the exception, and the generator skips the DSP import there;
# see its _load_dsp.)
import dolby_to_easyeffects
import ee_to_pipewire
from _version import get_version
from dolby_to_easyeffects import _HelpHintParser
from ee_to_pipewire import (
    DEFAULT_NODE_NAME,
    _HelpFormatter,
    _MISSING_COLOR_DEPS,
    _sanitize_name,
    cprint,
)


def _disable_color() -> None:
    """Both converters hold their own console; silence the pair."""
    dolby_to_easyeffects._disable_color()
    ee_to_pipewire._disable_color()


# The Balanced/Detailed/Warm stems _emit_ieq_presets emits, keyed by the
# --variant choices. The curves are Dolby-global constants (identical arrays
# on every device in the corpus; see docs/cross-device-findings.md), and
# ieq_balanced is the curve every device's profile selects by default —
# hence the default here.
VARIANT_STEMS = {"balanced": "Balanced", "detailed": "Detailed", "warm": "Warm"}


def _compose_parser(argv=None):
    """Build the wrapper parser from the two converters' shared argument
    builders. Returns (parser, step1_actions, step2_actions) — the action
    lists drive rebuild_argv() so forwarding can't drift from the CLI."""
    _argv = sys.argv[1:] if argv is None else argv
    formatter_class = (argparse.HelpFormatter
                       if "--no-color" in _argv else _HelpFormatter)
    epilog = None
    if _MISSING_COLOR_DEPS:
        epilog = ("Tip: install " + " and ".join(_MISSING_COLOR_DEPS)
                  + " for colored output (see README for distro packages).")
    parser = _HelpHintParser(
        description="Convert Dolby DAX3 tuning XML to an active PipeWire "
                    "filter-chain sink — no EasyEffects files installed "
                    "(see docs/ee-to-pipewire.md).",
        formatter_class=formatter_class,
        epilog=epilog,
    )
    step1_actions = []
    step2_actions = []

    group = parser.add_argument_group(
        "tuning input",
        description=dolby_to_easyeffects.TUNING_INPUT_DESCRIPTION)
    step1_actions += dolby_to_easyeffects.add_tuning_input_args(group)

    group = parser.add_argument_group("inspection")
    step1_actions += dolby_to_easyeffects.add_inspection_args(group)

    group = parser.add_argument_group("profile selection")
    step1_actions += dolby_to_easyeffects.add_profile_selection_args(group)

    group = parser.add_argument_group("variant")
    group.add_argument(
        "--variant",
        choices=[*VARIANT_STEMS, "all"],
        default="balanced",
        help="which IEQ voicing to convert (default: balanced — the curve "
             "the Windows driver engages by default; the three voicings are "
             "Dolby-global, the device-specific correction applies under "
             "every one). 'all' converts each into its own PipeWire sink "
             "so you can A/B them from sound settings.",
    )

    group = parser.add_argument_group("routing")
    step2_actions += ee_to_pipewire.add_routing_args(
        group, only={"--target-sink"})

    group = parser.add_argument_group("output")
    step1_actions += dolby_to_easyeffects.add_output_args(
        group, only={"--prefix"})
    group.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=f"directory for the generated .conf and .irs copy (default: "
             f"{ee_to_pipewire.DEFAULT_OUTPUT_DIR}); filenames derive from "
             f"the preset name (e.g. Dolby_Balanced.conf)",
    )
    step2_actions += ee_to_pipewire.add_output_args(group, only={"--force"})

    group = parser.add_argument_group("activation")
    group.add_argument(
        "--no-activate",
        action="store_true",
        help="don't restart PipeWire or verify the sink after writing; "
             "print the manual activation steps instead",
    )

    group = parser.add_argument_group("filter tweaks")
    step1_actions += dolby_to_easyeffects.add_filter_tweak_args(group)

    group = parser.add_argument_group("general")
    step1_actions += dolby_to_easyeffects.add_general_args(
        group, only={"--verbose"})
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="stage and convert without touching the system: prints each "
             "generated conf to stdout (the generator's report moves to "
             "stderr so stdout stays pipeable), writes nothing outside the "
             "temporary staging directory, and does not restart PipeWire",
    )
    step2_actions += ee_to_pipewire.add_general_args(
        group, only={"--no-validate"})
    group.add_argument(
        "--no-color",
        action="store_true",
        help="disable colored terminal output",
    )
    group.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {get_version()}",
        help="show version and exit",
    )
    return parser, step1_actions, step2_actions


def build_parser(argv=None) -> argparse.ArgumentParser:
    """The parser alone — the introspection seam tests/test_readme_cli_sync.py
    uses."""
    parser, _, _ = _compose_parser(argv)
    return parser


def rebuild_argv(actions, args) -> list[str]:
    """Rebuild a child argv for the shared flags in ``actions`` from the
    parsed namespace — the inverse of parse_args for the action shapes the
    shared groups use (positional, store, store_true/false, append). Values
    equal to the default are omitted so each child parser keeps authority
    over its own defaults."""
    positionals = []
    options = []
    for action in actions:
        value = getattr(args, action.dest, None)
        if not action.option_strings:
            if value is not None:
                positionals.append(str(value))
            continue
        if value == action.default:
            continue
        opt = action.option_strings[0]
        if action.nargs == 0:
            options.append(opt)
        elif isinstance(value, list):
            for item in value:
                options += [opt, str(item)]
        else:
            options += [opt, str(value)]
    return positionals + options


def _generator_stdout(dry_run: bool):
    """Where the generator's stdout goes for the duration.

    It prints to stdout while this wrapper prints to stderr, and under
    --dry-run our stdout contract is "the conf(s), nothing else" — so
    everything the generator emits, including the closing block we print on
    its behalf at the end, moves to stderr with the phase banners.
    """
    return (contextlib.redirect_stdout(sys.stderr) if dry_run
            else contextlib.nullcontext())


def _run_generator(child_argv: list[str], closing=None) -> int:
    try:
        return dolby_to_easyeffects.run_cli(child_argv, closing=closing)
    except SystemExit as e:
        # The child argv is wrapper-constructed, so its parser should never
        # error — but never let a stray sys.exit tear down the tempdir scope.
        return e.code if isinstance(e.code, int) else 2


def _safe_node_name(stem: str) -> str:
    """Mirror ee_to_pipewire's default node naming (sanitised preset stem)
    so the wrapper can predict conf filenames and PW node names."""
    derived = _sanitize_name(stem).strip("_")
    return derived if derived else DEFAULT_NODE_NAME


PIPEWIRE_RESTART_CMD = "systemctl --user restart pipewire pipewire-pulse"
QUIT_EE_HINT = ("Avoid double-processing: quit EasyEffects "
                "(or remove its autoload for this device)")


def _print_undo(written: list[Path]) -> None:
    """How to get back. Everything else here asks the reader to restart their
    sound server with a config file they can't read, and never said what to do
    if the result is worse — or if PipeWire won't come back. Deleting the conf
    and restarting is the whole answer; it just has to be written down."""
    if not written:
        return
    confs = " ".join(f"'{p}'" for p in written)
    cprint("dim", "  To undo: rm " + confs)
    cprint("dim", f"           {PIPEWIRE_RESTART_CMD}")


def _print_manual_activation(node_names: list[str],
                             written: list[Path]) -> None:
    cprint("head", "[3/3] Activation skipped (--no-activate) — to finish:")
    cprint("cta", f"  1. Restart PipeWire:        {PIPEWIRE_RESTART_CMD}")
    cprint("cta", f"  2. {QUIT_EE_HINT}")
    for name in node_names:
        cprint("cta", f"  3. Verify the sink:         pw-cli ls Node | grep "
                      f"{name}")
    print()
    _print_undo(written)


def _verify_sinks(node_names: list[str], timeout=6.0, interval=0.5) -> int:
    """Poll pw-cli until every expected node shows up — the chain takes a
    moment to load after the restart. Missing after the timeout usually
    means a missing LV2 plugin."""
    if shutil.which("pw-cli") is None:
        cprint("warn", "pw-cli not found — can't verify the sinks loaded; "
                       "check with: pw-cli ls Node | grep <name>")
        return 0
    deadline = time.monotonic() + timeout
    missing = list(node_names)
    while missing:
        try:
            listing = subprocess.run(["pw-cli", "ls", "Node"],
                                     capture_output=True, text=True,
                                     timeout=10).stdout
        except (subprocess.TimeoutExpired, OSError):
            listing = ""
        missing = [n for n in missing if n not in listing]
        if not missing or time.monotonic() >= deadline:
            break
        time.sleep(interval)
    for name in node_names:
        if name not in missing:
            cprint("ok", f"Sink loaded: {name}")
    if missing:
        for name in missing:
            cprint("err", f"error: sink {name} did not appear after the "
                          "restart")
        cprint("cta", "Check that the LSP/Calf LV2 plugins are installed "
                      "(README: Plugin dependencies and validation), then "
                      f"retry: {PIPEWIRE_RESTART_CMD}")
        return 1
    return 0


def _activate(node_names: list[str]) -> int:
    cprint("head", "[3/3] Activating: restarting PipeWire")
    cprint("warn", f"{QUIT_EE_HINT} — otherwise both chains process the "
                   "audio at once.")
    try:
        proc = subprocess.run(PIPEWIRE_RESTART_CMD.split())
    except FileNotFoundError:
        cprint("warn", "systemctl not found (not a systemd system?) — "
                       "restart PipeWire yourself; the systemd equivalent "
                       f"is: {PIPEWIRE_RESTART_CMD}")
        return 0
    if proc.returncode != 0:
        cprint("err", f"error: PipeWire restart failed (rc "
                      f"{proc.returncode}) — run it manually: "
                      f"{PIPEWIRE_RESTART_CMD}")
        return 1
    return _verify_sinks(node_names)


def main(argv: list[str] | None = None) -> int:
    parser, step1_actions, step2_actions = _compose_parser(argv)
    # The parser is composed from both converters' argument builders, so its
    # completers compose the same way: each table matches on dest and ignores
    # the flags it doesn't own. Nothing to keep in sync here.
    if dolby_to_easyeffects.argcomplete is not None:
        dolby_to_easyeffects._attach_completers(parser)
        ee_to_pipewire._attach_completers(parser)
        dolby_to_easyeffects.argcomplete.autocomplete(parser)
    dolby_to_easyeffects.ensure_dsp()
    args = parser.parse_args(argv)
    if args.no_color:
        _disable_color()

    # Pre-check the one cross-flag rule the generator enforces post-parse,
    # so the error is framed by the wrapper instead of mid-run by the child.
    if args.xml_file and args.windows:
        parser.error("specify either xml_file or --windows, not both")

    # The generator's own closing block would land under [1/3], with two more
    # phases of output below it; we collect its findings and print it last.
    step1_common = (rebuild_argv(step1_actions, args)
                    + ["--skip-ee-check", "--skip-closing"])
    if args.no_color:
        step1_common.append("--no-color")

    # Inspection modes are straight pass-throughs: no tempdir, no conversion.
    if args.list or args.speaker_info or args.doctor:
        return _run_generator(step1_common)

    variants = (list(VARIANT_STEMS.values()) if args.variant == "all"
                else [VARIANT_STEMS[args.variant]])

    node_names: list[str] = []
    # Where each conf landed, so the run can say how to undo itself.
    written: list[Path] = []
    closing: list = []
    with tempfile.TemporaryDirectory(prefix="dolby_to_pipewire-") as tmp:
        # Not "no EasyEffects files are installed": the reader picked this
        # script to avoid EasyEffects, and opening the run by naming it made
        # a reviewer stop and re-check they'd run the right one.
        cprint("head", f"[1/3] Generating tuning presets (staged in {tmp}; "
                       "deleted when done — nothing is installed on your "
                       "system in this step)")
        with _generator_stdout(args.dry_run):
            rc = _run_generator(step1_common
                                + ["--output-dir", tmp, "--irs-dir", tmp],
                                closing=closing)
        if rc != 0:
            return rc

        presets = [p for v in variants
                   for p in sorted(Path(tmp).glob(f"*-{v}.json"))]
        if not presets:
            wanted = ", ".join(variants)
            cprint("err", f"error: no {wanted} preset was generated — see "
                          "the log above (the XML may lack that IEQ curve)")
            return 1

        # Name what is being converted and what isn't. Step 1 lists all three
        # voicings as generated, so converting one without saying which — or
        # that the others are reachable — read as two of them being silently
        # dropped, with no way to try Warm if Balanced sounds wrong.
        if len(presets) == 1:
            cprint("head", f"[2/3] Converting {presets[0].stem} to a PipeWire "
                           "filter-chain conf")
        else:
            cprint("head", f"[2/3] Converting {len(presets)} presets to "
                           "PipeWire filter-chain confs")
            for preset in presets:
                cprint("dim", f"      {preset.stem}")
        if args.variant != "all":
            others = [v for v in VARIANT_STEMS if v != args.variant]
            cprint("dim", "      The other voicings are not converted: "
                          + ", ".join(others) + ".")
            # Says what --variant all gets the user ("a sink each" named an
            # internal object; what they see is another output to switch to
            # in sound settings) and covers both alternatives, not just the
            # first.
            alts = " or ".join(f"--variant {o}" for o in others)
            cprint("dim", f"      Pass {alts} to convert another, or "
                          "--variant all to get all three as outputs you "
                          "can switch between in your sound settings.")
        step2_common = (rebuild_argv(step2_actions, args)
                        + ["--irs-dir", tmp, "--skip-next-steps"])
        if args.dry_run:
            step2_common.append("--dry-run")
        if args.no_color:
            step2_common.append("--no-color")
        for preset in presets:
            node_name = _safe_node_name(preset.stem)
            child_argv = [str(preset)] + step2_common
            if args.output_dir is not None:
                # Absolute, or the conf embeds a relative IRS path that
                # PipeWire would resolve against its own CWD and miss.
                out_dir = args.output_dir.expanduser().resolve()
                child_argv += ["--output", str(out_dir / f"{node_name}.conf")]
            out_dir = (args.output_dir.expanduser().resolve()
                       if args.output_dir is not None
                       else ee_to_pipewire.DEFAULT_OUTPUT_DIR.expanduser())
            written.append(out_dir / f"{node_name}.conf")
            rc = ee_to_pipewire.main(child_argv)
            if rc != 0:
                # Fail fast: a validation failure would repeat identically
                # for every variant, and an existing-conf collision already
                # printed its --force hint (a --force re-run is idempotent
                # for variants written before the failure).
                return rc
            node_names.append(node_name)

    if args.dry_run:
        # "installed", not "written": staging really does write the presets
        # (to the tempdir named at [1/3], which is why "Wrote /tmp/…" lines
        # appear above), and claiming nothing was written contradicted them.
        cprint("head", "[3/3] Dry run — nothing was installed; re-run without "
                       "--dry-run to install and activate")
        rc = 0
    elif args.no_activate:
        _print_manual_activation(node_names, written)
        rc = 0
    else:
        rc = _activate(node_names)
        # The path where the sound just changed under them, so this is where
        # knowing the way back matters most.
        print()
        _print_undo(written)

    # The generator's closing block, held back from [1/3] so it lands here —
    # last on screen, whichever of the three ways this run ended. Not on the
    # failure paths above: they return early, and an ask is the wrong thing to
    # close on when nothing was installed.
    #
    # Always onto stderr, not just under --dry-run: this is the wrapper's
    # closing output now, and the phase banners it has to follow are on
    # stderr. Same stream is what makes "after [3/3]" true rather than a
    # coincidence of the two being the same terminal.
    with contextlib.redirect_stdout(sys.stderr):
        dolby_to_easyeffects.print_project_asks(closing, dry_run=args.dry_run,
                                                pipewire_native=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
