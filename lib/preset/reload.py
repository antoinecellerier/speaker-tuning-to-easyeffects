"""Make what a run just wrote audible: load it into a running EasyEffects.

EasyEffects does not watch preset files — the running instance keeps its
in-memory chain until a preset is loaded again — so a run used to end on
"then reload the preset in EasyEffects". With the impulse's name now
following its content (``lib/preset/emit.py`` ``kernel_name``), one load
over EasyEffects' local socket (``lib/ee_socket.py``) makes the change
audible, and this module decides when that load is the right thing to do.
The policy and its declines: docs/design-notes.md "Rejected approaches".

Beside ``autoload.py`` — the other thing a run does to EasyEffects itself —
and apart from ``--doctor``, which never sends a mutating request;
``tests/test_layout.py`` keeps that so by name.
"""

from __future__ import annotations

import os

from dataclasses import dataclass

from lib import console, ee_paths, ee_socket
from lib.hardware import sinks
from lib.preset import autoload
from lib.report.findings import (Finding, _ee_bypassed_finding,
                                 _reload_refused_finding, _reload_unanswered_finding)


@dataclass
class Reloaded:
    """What the run got EasyEffects to do. ``loaded`` is the preset it now
    reports; ``playing`` the same name only when that is audible — global
    bypass leaves a preset loaded and silent. ``finding`` only when there is
    something for the user to do: a success has no action, and
    .claude/rules/user-messages.md forbids a no-action entry in a block that
    exists to prompt action."""
    loaded: str = ""
    playing: str = ""
    finding: Finding | None = None


# What DEMO_EE_RELOAD may fabricate. Anything else is a typo, and a typo
# must not waive a gate: the hook then does nothing, and the real gates and
# a real socket decide.
_DEMO_OUTCOMES = frozenset({"refreshed", "loaded", "bypassed", "mismatch", "silent"})


def reload_generated_preset(args, preset_names: list[str],
                            kernel_by_preset: dict[str, str],
                            starting: str) -> Reloaded:
    """Load this run's preset into a running EasyEffects and say what happened.

    Prints its own one-line result; prints nothing when EasyEffects isn't
    reachable, because the closing block's "open EasyEffects and pick it" is
    then exactly right.

    Refresh what is playing if it is one of ours; otherwise load
    ``starting`` — the preset the run points at everywhere
    (``autoload.starting_preset``, resolved once by the caller so a bare
    ``--autoload`` and this load can't name different presets) — unless
    EasyEffects is on the `Nothing` bypass preset (`--autoload`'s
    non-speaker fallback) or its default sink is visibly not an internal
    speaker: a speaker tuning on a headset is harm this run would have
    caused. An unknown sink loads: the reader just ran a speaker-tuning
    tool.

    ``DEMO_EE_RELOAD`` = refreshed | loaded | bypassed | mismatch | silent
    fabricates that outcome without touching a socket, and waives only the
    live-tree gate — the review tooling renders the copy from a tempdir.
    """
    demo = (os.environ.get("DEMO_EE_RELOAD") or "").strip().lower()
    if demo not in _DEMO_OUTCOMES:
        demo = ""
    if args.dry_run or getattr(args, "staged", False) or not preset_names:
        return Reloaded()
    if not demo and ee_paths.uses_custom_dirs(args.output_dir, args.irs_dir):
        return Reloaded()
    if args.no_reload:
        _say_what_to_pick(args, preset_names, starting)
        return Reloaded()

    current = (_demo_current(demo, preset_names) if demo
               else ee_socket.last_loaded_output_preset())
    if not current.reached:
        return Reloaded()
    target = starting
    if not current.answered:
        # Listening but silent: we cannot know what is playing, so nothing
        # is sent — a load onto an unknown state is not a refresh.
        return Reloaded(finding=_reload_unanswered_finding(target, asked_to_load=False))
    refreshed = current.value in preset_names
    if refreshed:
        target = current.value
    elif current.value == autoload.BYPASS_PRESET_NAME:
        # Said, not silent: under --autoload the closing block prints
        # nothing, so this line is the only sign the run left it alone.
        print()
        console._cprint_wrapped("dim", f"EasyEffects is on '{current.value}', "
                                "the bypass preset for non-speaker outputs — "
                                "leaving it as it is.")
        return Reloaded()
    elif not demo:
        sink = sinks.live_default_sink()
        if sink and sinks.sink_kind(sink) == "other":
            # PipeWire's default sink, which EasyEffects follows unless
            # pinned in its own settings — so name it as the user's, not
            # as EasyEffects' (copy audit 2026-08-27).
            print()
            console._cprint_wrapped("dim", f"Your default output is '{sink}' — "
                                    "not loading a speaker tuning onto it.")
            return Reloaded()

    kernel = kernel_by_preset.get(target)
    try:
        result = (_demo_load(demo, target, kernel) if demo
                  else ee_socket.load_output_preset(target, expect_kernel=kernel))
    except ValueError:
        # A name the daemon would drop silently: not attempted, and the
        # closing block's manual step stands.
        return Reloaded()
    if result.outcome == "unreachable":
        return Reloaded()
    if result.outcome == "silent":
        return Reloaded(finding=_reload_unanswered_finding(target))
    if result.outcome != "loaded":
        return Reloaded(finding=_reload_refused_finding(
            target, result.loaded, kernel is None or result.kernel == kernel))

    bypass = (ee_socket.EEReply(value="1" if demo == "bypassed" else "2",
                                reached=True, answered=True)
              if demo else ee_socket.global_bypass())
    if bypass.value == "1":
        console.cprint("ok", f"\nEasyEffects loaded '{target}'.")
        return Reloaded(loaded=target, finding=_ee_bypassed_finding())
    if not bypass.answered:
        # get_global_bypass exists only since EasyEffects 8.1.3; 8.0.9–8.1.2
        # load fine and answer nothing here. "Loaded", not "playing": the
        # effects switch may be off, and this run can't ask.
        console.cprint("ok", f"\nEasyEffects loaded '{target}' (this EasyEffects "
                       "can't say whether its effects are switched on).")
        return Reloaded(loaded=target)
    # One sentence shape for both outcomes (review round 2026-08-27: two
    # wordings for "it is on our preset now" read as inconsistent behaviour);
    # the clause after the dash is what differs — a refresh, or a switch away
    # from what was playing.
    if refreshed:
        console.cprint("ok", f"\nEasyEffects is playing '{target}' again — reloaded "
                       "with this run's changes.")
    else:
        was = (f"it was on '{current.value}'" if current.value
               else "nothing was loaded before")
        console.cprint("ok", f"\nEasyEffects is now playing '{target}' — {was}.")
    return Reloaded(loaded=target, playing=target)


def _say_what_to_pick(args, preset_names: list[str], starting: str) -> None:
    """--no-reload still owes the reader the state it leaves: a running
    EasyEffects keeps its in-memory chain, and under --autoload the closing
    block is silent, so this is the only line that says what to do. Reads
    what is playing; never loads."""
    current = ee_socket.last_loaded_output_preset()
    if not current.reached:
        return
    # A restart only helps when something will load ours: autoload. On its
    # own EasyEffects rebuilds from its settings db, not the preset file,
    # and comes back as it was — even on the preset it was already playing
    # (copy audit 2026-08-27).
    restart = ", or restart it," if args.autoload else ""
    if current.answered and current.value in preset_names:
        text = (f"--no-reload: EasyEffects keeps playing '{current.value}' as it "
                f"was before this run — pick it again in its Presets menu{restart} "
                "to hear the changes.")
    else:
        state = (f"keeps playing '{current.value}'"
                 if current.answered and current.value else "is running")
        text = (f"--no-reload: EasyEffects {state} — pick '{starting}' in its "
                f"Presets menu{restart} to hear this run's tuning.")
    print()
    console._cprint_wrapped("dim", text)


def _demo_current(demo: str, preset_names: list[str]) -> ee_socket.EEReply:
    if demo == "silent":
        return ee_socket.EEReply(reached=True)
    playing = preset_names[0] if demo == "refreshed" else "Podcast"
    return ee_socket.EEReply(value=playing, reached=True, answered=True)


def _demo_load(demo: str, target: str, kernel: str | None) -> ee_socket.LoadResult:
    if demo == "mismatch":
        return ee_socket.LoadResult("mismatch", loaded="Podcast", kernel="Podcast-0123abcd")
    return ee_socket.LoadResult("loaded", loaded=target, kernel=kernel or "")
