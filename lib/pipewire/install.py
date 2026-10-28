"""Where the conf and its impulse response go, and what to do once they're there.

The write itself stays in ``ee_to_pipewire.py``'s ``main``. This module is
everything around it: the smart-filter target sink to pin to, the convolver
retarget that makes the conf self-contained, and the two blocks a user reads
afterwards. The ``lv2info`` schema self-check the conf is run through before
it is written is not here either. That is ``lib.pipewire.validate.run``,
called and rendered by the same ``main``. Where the impulse response is read
*from* is not decided here: ``--irs-dir`` defaults to
``lib.ee_paths.DEFAULT_IRS_DIR``, the same attribute the generator's
``--irs-dir`` writes to, so the two cannot drift apart.

``_autodetect_speaker_sink`` shares its probe with the EasyEffects autoload
pathway (``lib.hardware.sinks``). Importing it is free: ``pw-dump`` is read
inside ``_enumerate_audio_sinks``, so a converter run that never needs a sink
never shells out, whether the import sits here or in the function.

``_sanitize_name`` comes in bare from ``lib.pipewire.conf`` for
``_print_next_steps``: a regex over its argument, with no state a patch would
have to reach. ``PIPEWIRE_RESTART_CMD`` rides the same import. It lives in
``conf`` because ``checks.py`` and ``ee_to_pipewire.py`` spell it too, and
``conf`` is a module all three already import, and the one whose subject the
command belongs to (``console``, the other they share, owns printing). Not
stdlib-only, because ``lib.console`` owns the optional rich dependency, but
nothing here reaches the DSP stack.

The block from ``QUIT_EE_HINT`` down is the same seam for
``dolby_to_pipewire.py``: restart, poll ``pw-cli`` until the nodes appear,
say what is still to be picked, and say how to undo the whole thing. Its one
edge to ``lib.pipewire.checks`` is the function-local import in
``speaker_attenuation``. It is also where this module's
``shutil``/``subprocess``/``time`` bindings are read, which is what the
wrapper's tests patch through.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from lib import console, doctor, packages, tool_env, uninstall
from lib.hardware import sinks
from lib.pipewire import checks, session
from lib.pipewire.conf import PIPEWIRE_RESTART_CMD, _sanitize_name


def _retarget_convolver_irs(stages: list["Stage"],
                            target_irs: Path) -> Path | None:
    """Rewrite every convolver node's `filename` to ``target_irs``.

    Returns the original source path, or ``None`` if the chain has no
    convolver. All convolver nodes share one source IRS, so one copy at
    the destination serves both channels.
    """
    src: Path | None = None
    for stage in stages:
        for node in stage.nodes:
            if node.get("label") != "convolver":
                continue
            if src is None:
                src = Path(node["config"]["filename"])
            node["config"]["filename"] = str(target_irs)
    return src


def _autodetect_speaker_sink() -> tuple[str | None, list[str]]:
    """Return (chosen_sink_name, warnings) for the smart-filter target.

    Uses ``lib.hardware.sinks.select_speaker_sinks()`` (same probe as the
    EasyEffects autoload pathway): ``pw-dump`` filtered to ``Audio/Sink``
    nodes, preferring those tagged ``device.icon_name == audio-speakers`` (the
    "strict" tier, which excludes HDMI / Bluetooth / headsets). When nothing is
    tagged as a speaker, e.g. a laptop whose UCM2 profile omits the speaker
    icon (issue #18), it falls back to a "relaxed" tier of internal analog
    sinks. A single relaxed candidate is used, with a warning. Returns
    ``(None, [reasons])`` when no unique sink can be chosen. The caller then
    surfaces that, falls back to the v1 virtual-sink conf, and asks the user to
    pass ``--target-sink``.
    """
    # Terse, description-less form of the shared diagnostic (console warnings).
    def _diag(sink: dict) -> str:
        return sinks._sink_diag_line(sink, with_description=False)

    sel = sinks.select_speaker_sinks()
    tier, selected, all_sinks = sel["tier"], sel["selected"], sel["all_sinks"]

    if tier == "strict":
        if len(selected) == 1:
            return selected[0]["name"], []
        # Redacted like the relaxed-tier lines below, which reach the same
        # printers through `_diag`. A Bluetooth *speaker* lands in this tier,
        # because `_classify_sink` returns "strict" on
        # device.icon_name=audio-speakers before it excludes bluez. So this
        # list is not speakers-only.
        return None, [
            doctor.no_bt_address(
                f"multiple speaker sinks found ({len(selected)}): "
                + ", ".join(s["name"] for s in selected))
            + "; pass --target-sink to pick one"
        ]

    if tier == "relaxed":
        if len(selected) == 1:
            sink = selected[0]
            return sink["name"], [
                "no sink tagged device.icon_name=audio-speakers; using the "
                f"only internal analog sink {_diag(sink)}; pass --target-sink "
                "to override"
            ]
        return None, [
            f"multiple internal analog sinks found ({len(selected)}): "
            + ", ".join(_diag(s) for s in selected)
            + "; pass --target-sink to pick one"
        ]

    # tier == "none"
    if not all_sinks:
        # The two causes are told apart because their remedies share nothing,
        # and an empty sink list looks identical either way. This function
        # returns its reason rather than printing, so the package name cannot
        # ride along. Naming the tool is what lets the caller's hint be right.
        if tool_env.which("pw-dump") is None:
            return None, ["pw-dump isn't installed, so this run can't see any "
                          "sinks"]
        return None, [
            "no Audio/Sink nodes found via pw-dump (no PipeWire daemon running?)"
        ]
    return None, [
        "no internal-speaker sink found (none tagged "
        "device.icon_name=audio-speakers, and no internal analog output); "
        "sinks seen: " + ", ".join(_diag(s) for s in all_sinks)
        + "; pass --target-sink to pick one"
    ]


def _print_results(conf_path: Path, irs_path: Path | None,
                   *, dry_run: bool) -> None:
    """Report where the conf (and copied IRS) landed, or would land under
    --dry-run."""
    # "impulse response (.irs)", not the bare acronym: it is never expanded
    # anywhere else a user reads (round 5).
    #
    # ~ here and absolute in _print_undo below: this line is read, that one is
    # pasted into a shell inside quotes, which never expand ~.
    if dry_run:
        console.cprint("ok", f"Would write conf: {doctor.tilde(conf_path)}")
        if irs_path is not None:
            console.cprint("ok", "Would copy impulse response (.irs): "
                           f"{doctor.tilde(irs_path)}")
    else:
        console.cprint("ok", f"Wrote conf: {doctor.tilde(conf_path)}")
        if irs_path is not None:
            console.cprint("ok", "Copied impulse response (.irs): "
                           f"{doctor.tilde(irs_path)}")


def _grep_expectation(tail: str = "") -> str:
    """Return the line printed under a ``pw-cli ls Node | grep`` step.

    The line says what success looks like. Both finish paths render it from
    here, so the two cannot tell a user different things about the same grep.

    It states the expected output (round 6), because without it an empty grep
    couldn't be told apart from "this step doesn't matter". It names the usual
    cause of an empty grep instead of blaming the restart. When an LSP or Calf
    plugin is missing, module-filter-chain fails to load the whole conf and no
    node ever appears, so a reader told only "the restart didn't load it"
    re-restarts forever. The automated path says it too, in its own words
    (``_verify_sinks``).

    ``tail`` appends what happens *once* the line shows up: the only part that
    differs between the callers, and empty where the caller says nothing about
    it.

    PipeWire's own LV2 support is the first cause it names: some
    distributions package it apart from PipeWire (issue #123), and without it
    no LV2 plugin loads however many are installed.

    It names the *category*, not LSP and Calf by name. The converter says
    which packages this particular chain needs: a chain with no Calf stage is
    told LSP only. A generic "LSP or Calf" a few lines later would read as a
    second, contradictory requirement appearing from nowhere. The names belong
    in the message that knows which ones apply.
    """
    once = f" — {tail}" if tail else ""
    return ('     (it should print a line, showing node.name = "..."; '
            "nothing means the whole file failed to load — PipeWire's LV2 "
            "support, an LV2 plugin it needs, or its impulse file isn't "
            f"there{once})")


def _print_next_steps(node_name: str,
                      target_object: str | None = None,
                      selectable: bool = False) -> None:
    """The actions to take after a real (non-dry-run) write.

    ``selectable`` mirrors the wrapper's ``_print_selection_step``. With
    --target-sink '' the chain is an ordinary output that processes nothing
    until it is chosen, and the checklist must say so. That is the exact gap
    the wrapper's traps exist to prevent. On this path the trap is
    ``test_next_steps_tell_a_v1_user_to_select_the_sink``.
    """
    console.cprint("head", "Next steps:")
    console.cprint("cta", f"  1. Restart PipeWire:        {PIPEWIRE_RESTART_CMD}")
    # Stays a numbered step here, unlike the wrapper's footnote: this script
    # converts an existing EasyEffects preset, so its caller almost certainly
    # runs EasyEffects. "Stop it starting again" because quitting the window
    # ends double-processing for this session only — the background service
    # and autostart entry bring it back at the next login.
    console.cprint("cta", "  2. Avoid double-processing: quit EasyEffects and stop "
                  "it starting again (its Background Service and autostart, "
                  "or remove its autoload for this device)")
    console.cprint("cta", "  3. Verify the sink:         pw-cli ls Node | grep "
                  f"{_sanitize_name(node_name)}")
    console.cprint("dim", _grep_expectation())
    # Counted rather than hard-coded: both tails below are optional, and as two
    # literal "4."s they would collide on a run that printed both.
    step = 4
    if selectable:
        console.cprint("cta", f"  {step}. Select it as output:     pactl "
                      f"set-default-sink effect_input.{_sanitize_name(node_name)}")
        console._cprint_wrapped(
            "dim", "     (or pick it in sound settings — until you do, no "
            f"audio goes through it; {V1_SECOND_VOLUME_HINT})", indent="     ")
        step += 1
    else:
        console.cprint("dim", "     (nothing to select afterwards: it attaches to "
                      "your speakers by itself — leave your speakers selected as "
                      "the output)")
    if target_object:
        console.cprint("cta", f"  {step}. Verify routing:          "
                      f"pw-link -l | grep {target_object}")


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

# What running the chain as its own output costs, in the words of the symptom it
# produces. Measured for issue #63: the chain and the speaker are two sinks in
# series, each with its own control, so the levels multiply. The chain's lands
# *ahead of* the filter graph (indistinguishable from turning the source
# content down), so on loud material it also changes how hard the tuning's
# compressor and limiter work.
#
# v1 only, hence the name: this is the mode where selecting the chain is the
# instruction, so the caveat rides with it. In smart-filter mode selecting it is
# the *mistake*, so that path says something else entirely, at its own site.
# Five v1 sites share this wording, across both entry points.
V1_SECOND_VOLUME_HINT = ("it has a volume control of its own, on top of your "
                         "speakers' — both apply, so leave your speakers at "
                         "100% and use this one")


def speaker_attenuation() -> str:
    """Return a reading of the speakers' level when muted or turned down, else "".

    The reading is "your speakers are at 40 % (-23.8 dB) right now", or
    "… are muted (40 % once unmuted) right now".

    Telling someone to leave their speakers at 100 % is advice; telling them
    what those speakers are set to right now is a reading, and it is the half
    they cannot see once the chain is their selected output. Costs a pw-dump on
    the v1 path only, which is the fallback mode, not the default one.
    """
    from lib.pipewire import checks  # local: install's one edge to checks
    name, _ = _autodetect_speaker_sink()
    if not name:
        return ""
    level = sinks.sink_levels(checks._pw_dump()).get(name)
    if level is None or not level.attenuates:
        return ""
    return f"your speakers are {sinks.level_phrase(level)} right now"


def _print_undo(written: list[Path], style: str = "dim") -> None:
    """Print how to get back: delete the written files, restart PipeWire.

    Everything else here asks the reader to restart their sound server with a
    config file they can't read, and without this nothing would say what to
    do if the result is worse. Deleting the conf and restarting is the whole
    answer; it just has to be written down.

    `style` is "dim" on a run that worked, where the way back is a footnote.
    On a run that did not, it is the most useful line on screen, and a
    footnote is the wrong shape for it."""
    # Only files that exist: the .irs copy is skipped when the source
    # already sits at the target, and an rm over a missing file aborts the
    # pasted command halfway.
    # Absolute, alone among the lines a run prints: the quoting below is what
    # survives a Dolby-shaped path, and POSIX does not expand ~ inside quotes,
    # so a collapsed path here would delete nothing and say it did.
    paths = [p for p in written if p.exists()]
    if not paths:
        return
    files = " ".join(f"'{p}'" for p in paths)
    console.cprint(style, "  To undo: rm " + files)
    console.cprint(style, f"           {PIPEWIRE_RESTART_CMD}")
    # The rm line is this run's files only; this one is every chain, earlier
    # runs' included, so it is offered as the wider option, not a synonym.
    # With --output-dir the uninstall must be told the same directory, or it
    # looks in the default one and finds nothing.
    out_dir = paths[0].parent
    extra = (() if out_dir == checks.DEFAULT_OUTPUT_DIR.expanduser()
             else ("--output-dir", str(out_dir)))
    where = "" if not extra else " there"
    console.cprint(style, f"  Or remove every chain this tool installed{where}: "
                   + uninstall.command("dolby_to_pipewire.py", "--uninstall",
                                       *extra))


def _print_manual_activation(node_names: list[str],
                             written: list[Path],
                             selectable: bool = False) -> None:
    # The EasyEffects caveat is a footnote, not a numbered step (round
    # 10): this script's reader chose it to avoid EasyEffects, and seeing
    # EE in the critical path made them doubt they had.
    console.cprint("head", "[3/3] Activation skipped (--no-activate) — to finish:")
    console.cprint("cta", f"  1. Restart PipeWire:        {PIPEWIRE_RESTART_CMD}")
    # Numbered per sink rather than all "2.": with --variant all, one shared
    # number would print three consecutive steps as "2.", under a note
    # referring back to "step 1".
    for i, name in enumerate(node_names, start=2):
        console.cprint("cta", f"  {i}. Verify the sink:         pw-cli ls Node | grep "
                      f"{name}")
    # "Pinned ... automatically": the verify step proves existence, not
    # routing, and without the tail nothing says whether to go pick it in
    # Settings (round 10). The smart filter pins it, so the tail says so. This
    # path carries a tail where _print_next_steps has none.
    # "pinned automatically" is true of smart-filter routing only. Under
    # --target-sink '' the chain is an ordinary output that does nothing until
    # it is selected, and that sentence would promise the opposite.
    tail = ("once the line is there, pick it as your output in sound settings"
            if selectable else
            "once the line is there, it's pinned to your speakers automatically")
    console.cprint("dim", _grep_expectation(tail))
    if selectable:
        console.cprint("dim", f"  Note: {V1_SECOND_VOLUME_HINT}.")
    console.cprint("dim", f"  Note: {QUIT_EE_HINT}.")
    print()
    _print_undo(written)


# How `conf.format_conf` writes an LV2 node; a conf without it is builtins only
# (a convolver-only preset), and PipeWire's LV2 loader is nothing to it.
_LV2_NODE = 'type = "lv2"'


def _confs_use_lv2(confs) -> bool | None:
    """Whether any conf this run wrote has an LV2 node, or None if unread."""
    if not confs:
        return None
    try:
        return any(_LV2_NODE in Path(c).read_text(encoding="utf-8")
                   for c in confs)
    except OSError:
        return None


def _verify_sinks(node_names: list[str], timeout=6.0, interval=0.5,
                  uses_lv2: bool | None = None) -> int:
    """Poll pw-cli until every expected node shows up.

    The chain takes a moment to load after the restart. Missing after the
    timeout means PipeWire skipped the conf: its LV2 support, an LV2 plugin
    or an impulse file is missing (`_grep_expectation`). The first of those
    PipeWire can answer for itself, so it is asked before the other two are
    offered."""
    if tool_env.which("pw-cli") is None:
        # The check command comes *after* the install, not beside the "not
        # found". Beside it, a "check with: pw-cli ls Node" (an earlier
        # wording) would sit in the same breath as saying pw-cli was missing,
        # an instruction the reader cannot follow. Nothing failed here either.
        # Say so, or an unverified chain reads as a broken one.
        console.cprint("warn", "pw-cli isn't installed, so this run can't "
                       "confirm the sink loaded. Nothing failed — the chain "
                       "may well be running.")
        console.cprint("cta", "Install PipeWire's command-line tools:")
        packages.print_install_hint([packages.PW_TOOLS], console.cprint)
        console.cprint("cta", "Then check yourself with:  pw-cli ls Node | "
                       f"grep {node_names[0] if node_names else '<name>'}")
        return 0
    deadline = time.monotonic() + timeout
    missing = list(node_names)
    # Whether we ever got a graph back at all. `pw-cli` timing out or erroring
    # leaves `listing` empty, which marks every node missing. That is
    # indistinguishable from a chain that really didn't load, and the diagnosis
    # below is only honest about the second.
    answered = False
    while missing:
        try:
            listing = tool_env.run(["pw-cli", "ls", "Node"],
                                   capture_output=True, text=True,
                                   timeout=10).stdout
        except (subprocess.TimeoutExpired, OSError):
            listing = ""
        answered = answered or bool(listing.strip())
        missing = [n for n in missing if n not in listing]
        if not missing or time.monotonic() >= deadline:
            break
        time.sleep(interval)
    for name in node_names:
        if name not in missing:
            console.cprint("ok", f"Sink loaded: {name}")
    if missing:
        # Before the per-sink error, not after it: `missing` is every node we
        # did not see, and a `pw-cli` that never answered leaves all of them
        # in it. Printed first, "error: sink X did not appear" would assert as
        # fact the very thing this branch exists to say we do not know. Its
        # non-zero return would also make the wrapper promote "To undo: rm …"
        # to the loudest line on screen, over a chain that is probably running
        # (test_routing_unreadable_graph_is_not_diagnosed_as_a_dead_chain).
        if not answered:
            console.cprint("warn", "pw-cli never returned a node list, so "
                          "whether the chain loaded couldn't be checked — a "
                          "check that didn't run, not a chain that failed. "
                          "Read it yourself with: pw-cli ls Node")
            return 0
        for name in missing:
            console.cprint("err", f"error: sink {name} did not appear after the "
                          "restart")
        # What the absent sink *means*, in the reader's terms: the thing this
        # run generated is not running. The conf carries `nofail`
        # (conf.format_conf), so a chain PipeWire cannot load is skipped
        # rather than aborting the daemon. Without it, this same message could
        # equally mean the machine had no sound at all.
        console.cprint("warn", "So the filter chain this run generated isn't "
                      "running. PipeWire skips a chain it can't load rather "
                      "than refusing to start, so your speakers still work — "
                      "just without the tuning.")
        # PipeWire's own answer first, when it has one and the chain has LV2
        # nodes for it to matter to: without the loader no LSP or Calf
        # package makes the chain load, so offering them would be wrong.
        loader = (session.lv2_loader() if uses_lv2 is not False
                  else session.Lv2Loader(present=None))
        if loader.present is False:
            console.cprint("cta", "PipeWire can't load LV2 plugins on this "
                           "machine, so it skips the whole conf:")
            for style, text in packages.lv2_loader_steps(
                    loader.built_without, line=loader.line,
                    then=(("cta", f"Then retry: {PIPEWIRE_RESTART_CMD}"),)):
                console.cprint(style, f"  {text}")
            return 1
        # Both packages, and said to be both: this step sees only that a node
        # is absent, so it cannot narrow it to one the way the converter's
        # pre-write check does. Naming them without that sentence reads as a
        # diagnosis, and contradicts a run that named only one. The message
        # also says why it cannot narrow, because a run that names two plugins
        # elsewhere and shrugs here reads as one of them holding something
        # back.
        console.cprint("cta", "A missing LV2 plugin stops the whole conf "
                      "loading. All this step sees is a node that isn't "
                      "there, so install both — or run --doctor, which checks "
                      "them one by one, and PipeWire's LV2 support:")
        packages.print_install_hint([packages.LSP_LV2, packages.CALF_LV2],
                                    console.cprint)
        console.cprint("cta", f"Then retry: {PIPEWIRE_RESTART_CMD}")
        return 1
    return 0


def restart_pipewire() -> int | None:
    """Restart PipeWire. None once it restarted, else the run's exit code.

    0 on a machine with no systemctl, where the reader is told to restart it
    their own way: not a failure of this run. Shared by activation and by
    ``dolby_to_pipewire.py --uninstall``, which restarts to unload what it
    removed."""
    try:
        proc = tool_env.run(PIPEWIRE_RESTART_CMD.split())
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
    return None


def _activate(node_names: list[str], selectable: bool, confs=()) -> int:
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
    rc = restart_pipewire()
    if rc is not None:
        return rc
    rc = _verify_sinks(node_names, uses_lv2=_confs_use_lv2(confs))
    if rc == 0:
        _print_selection_step(node_names, selectable)
        # After, not between: the selection step's first line hangs off
        # "Sink loaded", and a warning in between read as part of it
        # (/user-review 2026-10-28).
        _warn_chain_levels(node_names)
    return rc


def _warn_chain_levels(node_names: list[str], timeout: float = 5.0,
                       interval: float = 0.25) -> None:
    """Say so when a chain sink that just loaded is muted or turned down.

    "Sink loaded" is otherwise the last word on a chain that can be silent:
    a new sink's mute and volume are whatever WirePlumber restores for its
    description, and in issue #152 the chain came up muted with every control
    the reporter was using reading unmuted. The scope is --doctor's "Chain
    volume" check's: a smart chain muted or turned down, a v1 chain only
    muted, because a v1 chain's level is the reader's own volume control.

    The read waits for WirePlumber to link each chain's output, because its
    restore lands after the node is listed, not with it. Measured on the dev
    device after a restart: the restored mute appeared 0.34-1.06 s after
    the node did (n=9), and the link 0.48-0.58 s after the mute, with the
    mute already in place at every link (n=5). So a fixed wait would be a
    guess, and the link says WirePlumber has handled the node.
    (`r-chain-as-system-output`.) Past *timeout* it reads what is there.
    """
    deadline = time.monotonic() + timeout
    while True:
        dump = checks._pw_dump()
        if (all(checks.downstream_sink(dump, n) for n in node_names)
                or time.monotonic() >= deadline):
            break
        time.sleep(interval)
    levels = sinks.sink_levels(dump)
    smart = {c.name for c in checks.live_chains(dump) if c.smart}
    for node_name in node_names:
        name = f"effect_input.{node_name}"
        level = levels.get(name)
        if level is None or not (level.muted or (level.turned_down
                                                 and node_name in smart)):
            continue
        # The symptom leads: a reader who hears nothing is reading for why
        # (/user-review 2026-10-28).
        if level.muted and node_name in smart:
            text = (f"Your speakers will stay silent: {name}, the filter this "
                    "run put in front of them, started muted. Its mute is "
                    "separate from your speakers', so your volume keys don't "
                    "reach it.")
        elif level.muted:
            text = (f"{name}, the output this run installed, started muted, "
                    "so anything played to it is silent.")
        else:
            text = (f"Everything you play will be quieter: {name}, the filter "
                    "this run put in front of your speakers, started at "
                    f"{sinks.level_reading(level)}. Its volume is separate "
                    "from your speakers', so your volume keys don't reach it.")
        console._cprint_wrapped("warn", text)
        console.cprint("dim", "Unmute it and put it back to full:"
                       if level.muted and level.turned_down
                       else "Unmute it:" if level.muted
                       else "Put it back to full:")
        for style, step in sinks.fix_commands(name, level):
            console.cprint(style, step)

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
        # The chain is still listed as an output, so it can be selected by
        # mistake. It looks like an ordinary device in the list. Selecting
        # it works, but it is then two sinks in series (issue #63).
        #
        # The trailing clause is not padding. The [2/3] block offers
        # `--variant all --target-sink ''` as a way to "switch between them in
        # your sound settings", so without it this run says both "pick the
        # chain there" and "never pick the chain there", and a reader cannot
        # tell which applies to them. It matches the predicate exactly:
        # `selectable` is `target_sink is None`.
        console._cprint_wrapped(
            "dim", "     Leave your speakers selected as the output: this chain "
            "attaches to them by itself, so picking it in sound settings would "
            "only add a second volume control on top of theirs. (Only "
            "--target-sink '' installs are meant to be picked that way.)",
            indent="     ")
        return
    console.cprint("cta", "  Now pick it as your output in sound settings — until "
                  "you do, no audio goes through it:")
    for name in node_names:
        console.cprint("cta", f"    {name}")
    console._cprint_wrapped("dim", f"     ({V1_SECOND_VOLUME_HINT})",
                            indent="     ")
