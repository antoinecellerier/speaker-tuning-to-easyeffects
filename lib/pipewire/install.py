"""Where the conf and its impulse response go, and what to do once they're there.

The write itself stays in ``ee_to_pipewire.py``'s ``main`` — this is
everything around it: the smart-filter target sink to pin to, the convolver
retarget that makes the conf self-contained, and the two blocks a user reads
afterwards. The ``lv2info`` schema self-check the conf is run through before
it is written is not here either — that is ``lib.pipewire.validate.run``,
called and rendered by the same ``main``. Where the impulse response is read
*from* is not decided here: ``--irs-dir`` defaults to
``lib.ee_paths.DEFAULT_IRS_DIR``, the same attribute the generator's
``--irs-dir`` writes to, so the two cannot drift apart.

``_autodetect_speaker_sink`` shares its probe with the EasyEffects autoload
pathway (``lib.hardware.sinks``). Importing it is free: ``pw-dump`` is read
inside ``_enumerate_audio_sinks``, so a converter run that never needs a sink
never shells out, whether the import sits here or in the function.

``_sanitize_name`` comes in bare from ``lib.pipewire.conf`` for
``_print_next_steps`` — a regex over its argument, with no state a patch would
have to reach. ``PIPEWIRE_RESTART_CMD`` rides the same import — it was defined
here until ``checks.py`` and
``ee_to_pipewire.py`` had to spell it too, and ``conf`` is a module all three
already import, and the one whose subject the command belongs to (``console``,
the other they share, owns printing). Not stdlib-only —
``lib.console`` owns the optional rich dependency — but nothing here reaches
the DSP stack.

The block from ``QUIT_EE_HINT`` down is the same seam for
``dolby_to_pipewire.py``: restart, poll ``pw-cli`` until the nodes appear,
say what is still to be picked, and say how to undo the whole thing. It has
no edge to ``lib.pipewire.checks`` and adds none — that direction is the one
deliberately left open so the two cannot form a cycle. It is also where this
module's ``shutil``/``subprocess``/``time`` bindings are read, which is what
the wrapper's tests patch through.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from lib import console, doctor, packages
from lib.hardware import sinks
from lib.pipewire.conf import PIPEWIRE_RESTART_CMD, _sanitize_name


def _retarget_convolver_irs(stages: list["Stage"],
                            target_irs: Path) -> Path | None:
    """Rewrite every convolver node's `filename` to ``target_irs`` and
    return the original source path (or ``None`` if the chain has no
    convolver). All convolver nodes share one source IRS, so we copy
    once and point both channels at the same destination.
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
    tagged as a speaker — e.g. a laptop whose UCM2 profile omits the speaker
    icon (issue #18) — it falls back to a "relaxed" tier of internal analog
    sinks; a single relaxed candidate is used, with a warning. Returns
    ``(None, [reasons])`` when no unique sink can be chosen — the caller
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
        # printers through `_diag`. A Bluetooth *speaker* lands in this tier —
        # `_classify_sink` returns "strict" on device.icon_name=audio-speakers
        # before it excludes bluez — so this list is not speakers-only.
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
        # Told apart, because the two have nothing in common to do about them
        # and an empty sink list looks identical either way. This one returns
        # its reason rather than printing, so the package name cannot ride
        # along — naming the tool is what lets the caller's hint be right.
        if shutil.which("pw-dump") is None:
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
    """Report where the conf (and copied IRS) landed — or, under --dry-run,
    where they *would* land."""
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
    """The line printed under a ``pw-cli ls Node | grep`` step, saying what
    success looks like. Both finish paths render it from here, so the two
    cannot tell a user different things about the same grep.

    What success looks like (round 6): with no expected output stated, an
    empty grep couldn't be told apart from "this step doesn't matter". And it
    names the usual cause of an empty grep instead of blaming the restart —
    when an LSP or Calf plugin is missing, module-filter-chain fails to load
    the whole conf and no node ever appears, so a reader told only "the
    restart didn't load it" re-restarts forever. The automated path says it
    too, in its own words (``_verify_sinks``).

    ``tail`` appends what happens *once* the line shows up: the only part that
    differs between the callers, and empty where the caller says nothing about
    it.

    It names the *category*, not LSP and Calf by name. The converter now says
    which packages this particular chain needs — a chain with no Calf stage is
    told LSP only — and a generic "LSP or Calf" a few lines later reads as a
    second, contradictory requirement appearing from nowhere. The names belong
    in the message that knows which ones apply.
    """
    once = f" — {tail}" if tail else ""
    return ('     (it should print a line, showing node.name = "..."; '
            "nothing usually means one of the LV2 plugins it needs isn't "
            f"installed, so the whole file failed to load{once})")


def _print_next_steps(node_name: str,
                      target_object: str | None = None,
                      selectable: bool = False) -> None:
    """The actions to take after a real (non-dry-run) write.

    ``selectable`` mirrors the wrapper's ``_print_selection_step``: with
    --target-sink '' the chain is an ordinary output that processes nothing
    until it is chosen, and this checklist used to end without saying so — the
    exact gap the wrapper's traps exist to prevent, on the path that has none.
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
    # literal "4."s they collided on the run that printed both.
    step = 4
    if selectable:
        console.cprint("cta", f"  {step}. Select it as output:     pactl "
                      f"set-default-sink effect_input.{_sanitize_name(node_name)}")
        console._cprint_wrapped(
            "dim", "     (or pick it in sound settings — until you do it "
            f"processes nothing; {V1_SECOND_VOLUME_HINT})", indent="     ")
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
# series, each with its own control, so the levels multiply — and the chain's
# lands *ahead of* the filter graph (indistinguishable from turning the source
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
    """"your speakers are at 40% (-23.8 dB)" when they are turned down, else "".

    Telling someone to leave their speakers at 100 % is advice; telling them
    what those speakers are set to right now is a reading, and it is the half
    they cannot see once the chain is their selected output. Costs a pw-dump on
    the v1 path only, which is the fallback mode, not the default one.
    """
    from lib.pipewire import checks  # local: checks imports this module
    name, _ = _autodetect_speaker_sink()
    if not name:
        return ""
    level = checks.sink_volumes(checks._pw_dump()).get(name)
    if level is None or level > 0.99:
        return ""
    return f"your speakers are at {checks._volume_reading(level)} right now"


def _print_undo(written: list[Path], style: str = "dim") -> None:
    """How to get back. Everything else here asks the reader to restart their
    sound server with a config file they can't read, and never said what to do
    if the result is worse. Deleting the conf and restarting is the whole
    answer; it just has to be written down.

    `style` is "dim" on a run that worked — there, the way back is a footnote.
    On a run that did not it is the most useful line on screen, and a footnote
    is the wrong shape for it."""
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
    # "Pinned ... automatically": the verify step proved existence, not
    # routing, and nothing said whether to go pick it in Settings (round
    # 10) — the smart filter pins it, so say so. This path carries a tail
    # where _print_next_steps has none.
    # "pinned automatically" is true of smart-filter routing only. Under
    # --target-sink '' the chain is an ordinary output that does nothing until
    # it is selected, and this sentence promised the opposite.
    tail = ("once the line is there, pick it as your output in sound settings"
            if selectable else
            "once the line is there, it's pinned to your speakers automatically")
    console.cprint("dim", _grep_expectation(tail))
    if selectable:
        console.cprint("dim", f"  Note: {V1_SECOND_VOLUME_HINT}.")
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
        # An offer, not a fix: the chain either loaded or it didn't, and the
        # tool only says which. Phrased like the converter's own skipped
        # self-check for that reason — nothing here is blocking the run.
        console.cprint("cta", "To have a later run check for you, install "
                       "PipeWire's command-line tools:")
        packages.print_install_hint([packages.PW_TOOLS], console.cprint)
        return 0
    deadline = time.monotonic() + timeout
    missing = list(node_names)
    # Whether we ever got a graph back at all. `pw-cli` timing out or erroring
    # leaves `listing` empty, which marks every node missing — indistinguishable
    # from a chain that really didn't load, and the diagnosis below is only
    # honest about the second.
    answered = False
    while missing:
        try:
            listing = subprocess.run(["pw-cli", "ls", "Node"],
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
        for name in missing:
            console.cprint("err", f"error: sink {name} did not appear after the "
                          "restart")
        # What the absent sink *means*, in the reader's terms: the thing this
        # run generated is not running. It stopped being ambiguous only
        # recently — the conf now carries `nofail` (conf.format_conf), so a
        # chain PipeWire cannot load is skipped rather than aborting the
        # daemon, and before that this same message could equally have meant
        # the machine had no sound at all.
        if not answered:
            # We never read the graph, so we know nothing about the chain.
            # Claiming it isn't running, and handing over a two-package
            # remedy, would be a diagnosis of a failed `pw-cli` call.
            console.cprint("warn", "pw-cli never returned a node list, so this "
                          "is a check that couldn't run, not a chain that "
                          "failed. Read it yourself with: pw-cli ls Node")
            return 1
        console.cprint("warn", "So the filter chain this run generated isn't "
                      "running. PipeWire skips a chain it can't load rather "
                      "than refusing to start, so your speakers still work — "
                      "just without the tuning.")
        # Both packages, and said to be both: this step sees only that a node
        # is absent, so it cannot narrow it to one the way the converter's
        # pre-write check does. Naming them without that sentence reads as a
        # diagnosis, and contradicts a run that named only one.
        console.cprint("cta", "The usual cause is a missing LV2 plugin. This "
                      "step can't tell which, so install both:")
        packages.print_install_hint([packages.LSP_LV2, packages.CALF_LV2],
                                    console.cprint)
        console.cprint("cta", f"Then retry: {PIPEWIRE_RESTART_CMD}")
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
        # The chain is still listed as an output, so it can be selected by
        # mistake — and it looks like an ordinary device in the list. Selecting
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
                  "you do, it processes nothing:")
    for name in node_names:
        console.cprint("cta", f"    {name}")
    console._cprint_wrapped("dim", f"     ({V1_SECOND_VOLUME_HINT})",
                            indent="     ")
