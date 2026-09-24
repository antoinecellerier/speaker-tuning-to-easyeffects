"""Make what a run just wrote audible: load it into a running EasyEffects.

EasyEffects does not watch preset files. The running instance keeps its
in-memory chain until a preset is loaded again. The impulse's name follows
its content (``lib/preset/emit.py`` ``kernel_name``), so one load over
EasyEffects' local socket (``lib/ee_socket.py``) makes the change audible.
This module decides when that load is the right thing to do. The policy and
its declines: `r-irs-in-place-rewrite`.

It sits beside ``autoload.py``, the other thing a run does to EasyEffects
itself. It is kept apart from ``--doctor``, which never sends a mutating
request, and ``tests/test_layout.py`` keeps that so by name.
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
    """What the run got EasyEffects to do.

    ``loaded`` is the preset it now reports. ``playing`` is the same name
    only when that is audible: global bypass leaves a preset loaded and
    silent. ``finding`` is set only when there is something for the user to
    do. A success has no action, and .claude/rules/user-messages.md forbids
    a no-action entry in a block that exists to prompt action."""
    loaded: str = ""
    playing: str = ""
    finding: Finding | None = None


# What DEMO_EE_RELOAD may fabricate. Anything else is a typo, and a typo
# must not waive a gate: the hook then does nothing, and the real gates and
# a real socket decide.
_DEMO_OUTCOMES = frozenset({"refreshed", "loaded", "bypassed", "mismatch", "silent"})


# EasyEffects merged the Convolver-page crash fix (wwmm/easyeffects#5306)
# after 8.2.9, so anything newer than that release needs no hiding. Assuming
# the next tag carries it is the bet the maintainer took, watching releases:
# an intermediate release without it is not how this project has cut them.
_LAST_EE_RELEASE_WITH_CONVOLVER_CRASH = (8, 2, 9)


def _easyeffects_fixed_the_convolver_crash() -> bool:
    """True only when the answered version is past the Convolver crash.

    A version must answer *and* be past the last release whose Convolver
    page crashes. Fails closed, because an unreadable version is
    ordinary: ``easyeffects --version`` wants a display and Flatpak answers
    through ``flatpak info``, so None means "don't know", never "fixed"."""
    from lib.report import doctor_run  # local: the run path is not the doctor
    version = doctor_run._probe_ee_version().version
    return version is not None and version > _LAST_EE_RELEASE_WITH_CONVOLVER_CRASH


def hide_window_before_writing(args) -> bool:
    """Ask a running EasyEffects to hide its window before the run writes.

    Its Convolver page crashes EasyEffects 8.2.8–8.2.9 on the impulse-file
    writes (issue #95, reproduced). Hide unconditionally rather than only for
    that page: the rc records the last-shown page on a 30 s timer that runs
    only while the window is open, so a page opened moments ago still reads as
    the old one, and that error is the one that costs the crash. Hiding a
    hidden window is a no-op, since EasyEffects calls ``hide()`` without
    checking. Nothing reports whether the window was open, so never show it
    again.
    Skipped on an EasyEffects past the last release that crashes.
    Silent under ``--dry-run``, and when neither directory is EasyEffects'
    own. Returns True when a daemon took the request. That is not the same
    as a window having been open, so the copy hedges.
    """
    if args.dry_run or getattr(args, "staged", False):
        return False
    # Not uses_custom_dirs: --output-dir alone still drops the impulse burst
    # into the watched irs directory, which is what crashes EasyEffects.
    if not ee_paths.writes_into_ee_tree(args.output_dir, args.irs_dir):
        return False
    # A fabricated reload outcome must not reach the socket either, or
    # rendering the docs would hide the renderer's own window. A value the
    # hook doesn't know is no hook, exactly as below.
    if (os.environ.get("DEMO_EE_RELOAD") or "").strip().lower() in _DEMO_OUTCOMES:
        return False
    # Cheapest gate last but one: the version probe shells out (twice on a
    # Flatpak machine, 5 s apiece) and runs before the first write, so it
    # must not be paid by the many runs with no EasyEffects listening at all.
    if not ee_socket.daemon_listening():
        return False
    if _easyeffects_fixed_the_convolver_crash():
        return False
    if not ee_socket.hide_window():
        return False
    # Its own paragraph: it prints inside the run's opening banner, and run
    # together with the endpoint and profile lines it read as one more fact
    # about the device rather than something the tool just did.
    print()
    console._cprint_wrapped(
        "dim",
        "Asked EasyEffects to hide its window before writing, to work around "
        "a potential crash (issue #95). EasyEffects keeps running and your "
        "audio is unaffected — reopen the window from your app menu if it "
        "was showing.")
    print()
    return True


def reload_generated_preset(args, preset_names: list[str],
                            kernel_by_preset: dict[str, str],
                            starting: str) -> Reloaded:
    """Load this run's preset into a running EasyEffects and say what happened.

    Prints its own one-line result; prints nothing when EasyEffects isn't
    reachable, because the closing block's "open EasyEffects and pick it" is
    then exactly right.

    Refresh what is playing if it is one of ours. Otherwise load
    ``starting``, the preset the run points at everywhere
    (``autoload.starting_preset``, resolved once by the caller so a bare
    ``--autoload`` and this load can't name different presets). That load is
    skipped when EasyEffects is on the `Nothing` bypass preset (`--autoload`'s
    non-speaker fallback) or its default sink is visibly not an internal
    speaker: a speaker tuning on a headset is harm this run would have
    caused. An unknown sink loads: the reader just ran a speaker-tuning
    tool.

    ``DEMO_EE_RELOAD`` = refreshed | loaded | bypassed | mismatch | silent
    fabricates that outcome without touching a socket, and waives only the
    live-tree gate. The review tooling renders the copy from a tempdir.
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
        # is sent. A load onto an unknown state is not a refresh.
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
            # pinned in its own settings. So name it as the user's, not
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
    # wordings for "it is on our preset now" read as inconsistent behaviour).
    # The clause after the dash is what differs: a refresh, or a switch away
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
    """Tell a --no-reload run what to pick to hear the changes.

    --no-reload still owes the reader the state it leaves: a running
    EasyEffects keeps its in-memory chain, and under --autoload the closing
    block is silent, so this is the only line that says what to do. Reads
    what is playing; never loads."""
    current = ee_socket.last_loaded_output_preset()
    if not current.reached:
        return
    # A restart only helps when something will load ours: autoload. On its
    # own EasyEffects rebuilds from its settings db, not the preset file,
    # and comes back as it was, even on the preset it was already playing
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
