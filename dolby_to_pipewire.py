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
import shlex
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
from lib import console, version
from dolby_to_easyeffects import _HelpHintParser
from ee_to_pipewire import DEFAULT_NODE_NAME, _sanitize_name
from lib.report import findings as report_findings
from lib.report import messages


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
                       if "--no-color" in _argv else console._HelpFormatter)
    epilog = None
    if console._MISSING_COLOR_DEPS:
        epilog = ("Tip: install " + " and ".join(console._MISSING_COLOR_DEPS)
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
        help="which IEQ voicing to convert (default: balanced — Dolby's "
             "default voicing; the three voicings are Dolby-global, the "
             "device-specific correction applies under every one). 'all' "
             "converts each into its own PipeWire sink so you can A/B them "
             "from sound settings, and requires --target-sink '' (see that "
             "flag).",
    )

    group = parser.add_argument_group("routing")
    step2_actions += ee_to_pipewire.add_routing_args(
        group, only={"--target-sink", "--target-object"})

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
        help="stage and convert without touching the system: report where "
             "each conf would be written, write nothing outside the "
             "temporary staging directory, and don't restart PipeWire. To "
             "get the confs themselves without installing them, replace "
             "this flag with --output-dir DIR --no-activate",
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
        version=f"%(prog)s {version.get_version()}",
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


def _run_generator(child_argv: list[str], closing=None,
                   troubleshooting=None, resolved=None,
                   staged: bool = False) -> int:
    try:
        return dolby_to_easyeffects.run_cli(child_argv, closing=closing,
                                            troubleshooting=troubleshooting,
                                            resolved=resolved,
                                            staged=staged)
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
# Conditional on purpose: this script's audience chose it to avoid
# EasyEffects, and an unconditional "quit EasyEffects" read as "did
# something install it behind my back?" (round-3 review). The
# double-processing consequence rides the activation warning, which
# appends it.
# "and stop it starting again" rather than a bare "quit it": EasyEffects
# ships a background service and an autostart entry — both recommended in
# our own README — so quitting the window ends double-processing for this
# session only, and it comes back at the next login.
QUIT_EE_HINT = ("If you also run EasyEffects on this device, quit it and "
                "stop it starting again (its Background Service and "
                "autostart, or remove its autoload)")


def _print_undo(written: list[Path]) -> None:
    """How to get back. Everything else here asks the reader to restart their
    sound server with a config file they can't read, and never said what to do
    if the result is worse — or if PipeWire won't come back. Deleting the conf
    and restarting is the whole answer; it just has to be written down."""
    # Only files that exist: the .irs copy is skipped when the source
    # already sits at the target, and an rm over a missing file aborts the
    # pasted command halfway.
    paths = [p for p in written if p.exists()]
    if not paths:
        return
    files = " ".join(f"'{p}'" for p in paths)
    console.cprint("dim", "  To undo: rm " + files)
    console.cprint("dim", f"           {PIPEWIRE_RESTART_CMD}")


def _print_manual_activation(node_names: list[str],
                             written: list[Path],
                             selectable: bool = False) -> None:
    # The EasyEffects caveat is a footnote, not a numbered step (round
    # 10): this script's reader chose it to avoid EasyEffects, and seeing
    # EE in the critical path made them doubt they had.
    console.cprint("head", "[3/3] Activation skipped (--no-activate) — to finish:")
    console.cprint("cta", f"  1. Restart PipeWire:        {PIPEWIRE_RESTART_CMD}")
    # Numbered per sink rather than all "2.": with --variant all this loop
    # printed three consecutive steps sharing one number, under a note
    # referring back to "step 1".
    for i, name in enumerate(node_names, start=2):
        console.cprint("cta", f"  {i}. Verify the sink:         pw-cli ls Node | grep "
                      f"{name}")
    # What success looks like (round 6): with no expected output stated, an
    # empty grep couldn't be told apart from "this step doesn't matter".
    # "Pinned ... automatically": the verify step proved existence, not
    # routing, and nothing said whether to go pick it in Settings (round
    # 10) — the smart filter pins it, so say so.
    # Names the usual cause of an empty grep instead of blaming the restart:
    # when an LSP or Calf plugin is missing, module-filter-chain fails to
    # load the whole conf and no node ever appears, so a reader told only
    # "the restart didn't load it" re-restarts forever. The automated path
    # already says this (_verify_sinks); the manual one didn't.
    # "pinned automatically" is true of smart-filter routing only. Under
    # --target-sink '' the chain is an ordinary output that does nothing until
    # it is selected, and this sentence promised the opposite.
    tail = ("once the line is there, pick it as your output in sound settings"
            if selectable else
            "once the line is there, it's pinned to your speakers automatically")
    console.cprint("dim", "     (it should print a line, showing node.name = \"...\"; "
                  "nothing usually means the LSP or Calf LV2 plugins are "
                  f"missing, so the whole file failed to load — {tail})")
    console.cprint("dim", f"  Note: {QUIT_EE_HINT}.")
    print()
    _print_undo(written)


def _verify_sinks(node_names: list[str], timeout=6.0, interval=0.5) -> int:
    """Poll pw-cli until every expected node shows up — the chain takes a
    moment to load after the restart. Missing after the timeout usually
    means a missing LV2 plugin."""
    if shutil.which("pw-cli") is None:
        console.cprint("warn", "pw-cli not found — can't verify the sinks loaded; "
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
            console.cprint("ok", f"Sink loaded: {name}")
    if missing:
        for name in missing:
            console.cprint("err", f"error: sink {name} did not appear after the "
                          "restart")
        console.cprint("cta", "Check that the LSP/Calf LV2 plugins are installed "
                      "(README: Plugin dependencies and validation), then "
                      f"retry: {PIPEWIRE_RESTART_CMD}")
        return 1
    return 0


def _activate(node_names: list[str], selectable: bool) -> int:
    console.cprint("head", "[3/3] Activating: restarting PipeWire")
    # Not only "otherwise both chains process the audio": the restart itself
    # stops a running EasyEffects (it doesn't survive its server going away),
    # so whatever EasyEffects was applying stops here whether or not the
    # reader acts. Said plainly, because the alternative is discovering it as
    # "the update broke my audio".
    console.cprint("warn", f"{QUIT_EE_HINT} — otherwise both chains process the "
                   "audio at once. The restart below stops it for this "
                   "session either way, so anything it was applying goes "
                   "with it.")
    try:
        proc = subprocess.run(PIPEWIRE_RESTART_CMD.split())
    except FileNotFoundError:
        console.cprint("warn", "systemctl not found (not a systemd system?) — "
                       "restart PipeWire yourself; the systemd equivalent "
                       f"is: {PIPEWIRE_RESTART_CMD}")
        return 0
    if proc.returncode != 0:
        console.cprint("err", f"error: PipeWire restart failed (rc "
                      f"{proc.returncode}) — run it manually: "
                      f"{PIPEWIRE_RESTART_CMD}")
        return 1
    rc = _verify_sinks(node_names)
    if rc == 0:
        _print_selection_step(node_names, selectable)
    return rc


def _print_selection_step(node_names: list[str], selectable: bool) -> None:
    """Say what still has to happen for the chain to be in the audio path.

    With smart-filter routing (the default) nothing does — WirePlumber
    inserts it. With --target-sink '' the chain is an ordinary output that
    processes nothing until it is selected, and a run that stops at
    "Sink loaded" leaves the reader believing it is already working.
    """
    if not selectable:
        console.cprint("dim", "     (pinned to your speakers automatically — apps "
                      "keep playing to the speaker as usual)")
        return
    console.cprint("cta", "  Now pick it as your output in sound settings — until "
                  "you do, it processes nothing:")
    for name in node_names:
        console.cprint("cta", f"    {name}")


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
        console._disable_color()

    # Pre-check the one cross-flag rule the generator enforces post-parse,
    # so the error is framed by the wrapper instead of mid-run by the child.
    if args.xml_file and args.windows:
        parser.error("specify either xml_file or --windows, not both")

    # Chains that share a filter.smart.target are run in SERIES by
    # WirePlumber, not offered as alternatives: get_filter_from_target
    # returns the first filter matching the target and get_filter_target
    # "the next filter with matching target" (scripts/lib/filter-utils.lua,
    # 0.5.15). Measured — three voicings installed that way gave
    # app → Balanced → Detailed → Warm → speakers, so every stage ran three
    # times. Marking them filter.smart.targetable doesn't help: picking one
    # in sound settings resolves back to its target and re-enters at the
    # first. Turning smart-filter routing off is what makes them independent
    # sinks you can actually choose between, so anything that installs more
    # than one chain requires it — this is about the count, not about which
    # flag produced it.
    multi = [f for f, on in (("--variant all", args.variant == "all"),
                             ("--all-profiles", args.all_profiles)) if on]
    if multi and args.target_sink != "":
        parser.error(
            f"{' and '.join(multi)} installs more than one chain, which needs "
            "--target-sink '' (an empty string): with smart-filter routing "
            "on, PipeWire would run them in series instead of offering a "
            "choice")

    # The generator's own closing output (fix-flags menu + asks) would land
    # under [1/3], with two more phases of output below it; we collect its
    # findings and menu inputs and print them last.
    step1_common = (rebuild_argv(step1_actions, args)
                    + ["--skip-ee-check", "--skip-closing"])
    if args.no_color:
        step1_common.append("--no-color")

    # --doctor goes to the PipeWire-side doctor, not the generator's.
    # EasyEffects on this path is a temporary implementation detail — the
    # preset is staged in a tempdir and deleted — so the generator's checks
    # don't just read as noise here, they give wrong advice: "no presets found
    # in ~/.local/share/easyeffects/output — run the script on your tuning XML
    # first" describes a directory this script will never write to. The
    # hardware sections a reader does need come along with the PipeWire report.
    if args.doctor:
        return ee_to_pipewire.report_pw_doctor()

    # The rest are straight pass-throughs: no tempdir, no conversion.
    if args.list or args.speaker_info:
        return _run_generator(step1_common)

    variants = (list(VARIANT_STEMS.values()) if args.variant == "all"
                else [VARIANT_STEMS[args.variant]])

    # Virtual sinks whose playback streams have no target of their own follow
    # the *default* sink — so the moment you pick one of them as your output,
    # the others follow it and chain into it (measured: Balanced → Warm and
    # Detailed → Warm, which is every stage twice). Pinning each playback
    # side to the real speaker sink is what keeps them independent, and
    # picking one is the whole point of the mode, so it can't be left to the
    # reader to remember. Resolved before anything is generated: a run that
    # can't keep them apart should fail before it writes.
    pin_target = args.target_object
    if multi and pin_target is None:
        pin_target, detect_warnings = ee_to_pipewire._autodetect_speaker_sink()
        for w in detect_warnings:
            console.cprint("warn", f"[routing] {w}")
        if pin_target is None:
            console.cprint("err", "error: couldn't tell which sink drives your "
                          "speakers, so the chains can't be kept apart. Name "
                          "it with --target-object <node.name> (pw-cli ls "
                          "Node lists them), or install one at a time")
            return 1

    node_names: list[str] = []
    # Where each conf landed, so the run can say how to undo itself.
    written: list[Path] = []
    closing: list = []
    # The fix-flags menu travels the same way as the closing findings:
    # printed at [1/3] it told the reader what to re-run before setup had
    # even finished (round 4), so the generator stashes its inputs here and
    # we render it at the end, after the [3/3] steps.
    troubleshooting: dict = {}
    # The XML may have been auto-discovered on a mounted Windows partition, so
    # only the generator knows which file this run actually read. The closing
    # block names it as the thing to attach to a report; without it the reader
    # is told to attach their tuning XML and never shown which one.
    resolved: dict = {}
    with tempfile.TemporaryDirectory(prefix="dolby_to_pipewire-") as tmp:
        # Not "no EasyEffects files are installed": the reader picked this
        # script to avoid EasyEffects, and opening the run by naming it made
        # a reviewer stop and re-check they'd run the right one.
        console.cprint("head", f"[1/3] Generating tuning presets (staged in {tmp}; "
                       "deleted when done — nothing is installed on your "
                       "system in this step)")
        # Echo the invocation (round 8): the closing's "add any of the
        # flags above to the same command you ran" had no referent unless
        # the reader saved their own command line. shlex keeps Dolby's
        # $-laden paths copy-pasteable.
        # sys.executable, not a guessed "python3" and not the bare basename:
        # echoing Path(sys.argv[0]).name dropped whatever launched us, so the
        # line wasn't runnable as shown, and hardcoding an interpreter would
        # just invent a different command from the one that was typed (the
        # scripts are executable, so ./dolby_to_pipewire.py is equally
        # likely). argv[0] keeps the path the reader used.
        console.cprint("dim", "      (your command: "
                      + shlex.join([sys.executable, *sys.argv]) + ")")
        rc = _run_generator(step1_common
                            + ["--output-dir", tmp, "--irs-dir", tmp],
                            closing=closing,
                            troubleshooting=troubleshooting,
                            resolved=resolved,
                            staged=True)
        if rc != 0:
            return rc

        presets = [p for v in variants
                   for p in sorted(Path(tmp).glob(f"*-{v}.json"))]
        if not presets:
            wanted = ", ".join(variants)
            console.cprint("err", f"error: no {wanted} preset was generated — see "
                          "the log above (the XML may lack that IEQ curve)")
            return 1

        # Name what is being converted and what isn't. Step 1 lists all three
        # voicings as generated, so converting one without saying which — or
        # that the others are reachable — read as two of them being silently
        # dropped, with no way to try Warm if Balanced sounds wrong.
        if len(presets) == 1:
            console.cprint("head", f"[2/3] Converting {presets[0].stem} to a PipeWire "
                           "filter-chain conf")
            # Why this one (round 7): the profile pick explains itself, so
            # an unexplained Balanced default read as arbitrary next to it.
            # "Dolby's default voicing", not "the voicing Windows engages by
            # default": on the ~45% of profiles that set ieq-enable=0,
            # Windows engages no voicing at all, so the stronger claim was
            # wrong for them.
            if args.variant == "balanced":
                console.cprint("dim", "      (Balanced is Dolby's default voicing)")
        else:
            console.cprint("head", f"[2/3] Converting {len(presets)} presets to "
                           "PipeWire filter-chain confs")
            for preset in presets:
                console.cprint("dim", f"      {preset.stem}")
            # Each is a separate output you choose in sound settings, not a
            # stage in one chain — the distinction the smart-filter default
            # hides, and the reason this mode needs --target-sink ''.
            console.cprint("dim", "      Each becomes its own output; pick one in "
                          "your sound settings. They don't stack.")
            if args.target_object is None:
                console.cprint("dim", f"      (each plays into {pin_target} — pinned "
                              "so choosing one doesn't route the others "
                              "through it)")
        if args.variant != "all" and not args.all_profiles:
            others = [v for v in VARIANT_STEMS if v != args.variant]
            # Prose gets the capitalized names; the Pass sentence keeps
            # the lowercase flag values (round 10).
            console.cprint("dim", "      The other voicings are not converted: "
                          + ", ".join(o.capitalize() for o in others) + ".")
            # Says what --variant all gets the user ("a sink each" named an
            # internal object; what they see is another output to switch to
            # in sound settings) and covers both alternatives, not just the
            # first.
            alts = " or ".join(f"--variant {o}" for o in others)
            # "another voicing" ties the --variant flag to the word every
            # explanation above uses (round 10: a reader would guess
            # --voicing next week).
            console.cprint("dim", f"      Pass {alts} to convert another voicing, or "
                          "--variant all --target-sink '' to get all three as "
                          "outputs you can switch between in your sound "
                          "settings.")
        step2_common = (rebuild_argv(step2_actions, args)
                        + ["--irs-dir", tmp, "--skip-next-steps"])
        # Only when we resolved it ourselves — a user-supplied --target-object
        # is already in the rebuilt argv, and passing it twice would win an
        # argument with itself.
        if pin_target is not None and args.target_object is None:
            step2_common += ["--target-object", pin_target]
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
            # The .irs the converter copies beside the conf is part of the
            # install too — an undo that only removes the conf strands one
            # stray file per variant (round-3 review).
            written.append(out_dir / f"{node_name}.irs")
            rc = ee_to_pipewire.main(child_argv, wrapped=True)
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
        console.cprint("head", "[3/3] Dry run — nothing was installed; re-run without "
                       "--dry-run to install and activate")
        rc = 0
    elif args.no_activate:
        _print_manual_activation(node_names, written,
                                 selectable=args.target_sink == "")
        rc = 0
    else:
        rc = _activate(node_names, selectable=args.target_sink == "")
        # The path where the sound just changed under them, so this is where
        # knowing the way back matters most.
        print()
        _print_undo(written)

    # The generator's closing output, held back from [1/3] so it lands here —
    # last on screen, whichever of the three ways this run ended. Menu before
    # asks, the generator's own order, so the one link stays last. Not on the
    # failure paths above: they return early, and an ask is the wrong thing to
    # close on when nothing was installed.
    #
    # Both scripts print through a console on stdout, so "after [3/3]" is an
    # ordering guarantee rather than a coincidence of two streams happening to
    # share a terminal.
    if troubleshooting:
        messages.print_troubleshooting(
            troubleshooting["findings"],
            troubleshooting["filters_by_profile"],
            installs_presets=False,
            enabled_by_flag=troubleshooting["enabled_by_flag"],
            dry_run=args.dry_run)
    report_findings.print_project_asks(
        closing, dry_run=args.dry_run, pipewire_native=True,
        xml_path=resolved.get("xml_path"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
