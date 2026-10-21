"""Removing what earlier runs installed: the list, the question, the removal.

Shared by the two routes' planners, ``lib/preset/uninstall.py`` for
EasyEffects and ``lib/pipewire/uninstall.py`` for the filter chain. Each
builds a `Plan` without touching anything; this module prints it, asks, and
carries it out. That split is what makes the safety properties testable: the
planner decides *what* is ours, and a plan test needs no write at all.

Three rules hold for every route, because ``--uninstall`` deletes files:

- **Nothing changes before the answer.** The full list prints first, with
  every file left in place and why. ``--dry-run`` stops there, and so does
  a run with no terminal to answer on and no ``--yes``.
- **Each step is re-checked as it runs.** The reader may sit at the prompt
  while EasyEffects re-saves a preset or another run rewrites a conf. A step
  whose ownership test no longer holds is skipped, not acted on from a list
  that has gone stale.
- **Removed files go to the desktop Trash** when ``gio`` is installed, so a
  re-saved preset holding the user's own edits can be got back. Without it
  they are deleted, and the question says so before it is asked.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from lib import console, doctor, tool_env


# The three entry points, for spelling a sibling the way this one was run.
_ENTRY_SCRIPTS = {"dolby_to_easyeffects.py", "ee_to_pipewire.py",
                  "dolby_to_pipewire.py"}


def command(script: str, *args: str) -> str:
    """A pasteable command line for one of the entry scripts.

    ``python3`` plus the script's path as the reader typed this run's (a
    sibling of ``sys.argv[0]``), so the line runs from where they are; a
    bare name when this run wasn't started as an entry script."""
    typed = Path(sys.argv[0]) if sys.argv and sys.argv[0] else Path()
    path = (typed.with_name(script) if typed.name in _ENTRY_SCRIPTS
            else Path(script))
    return shlex.join(["python3", str(path), *args])


def add_args(add, uninstall_help: str) -> None:
    """``--uninstall`` and ``--yes``, through a ``console._make_adder`` adder.

    One definition for the three entry points; only what ``--uninstall``
    removes differs between routes, so that help is the caller's."""
    add("--uninstall", action="store_true", help=uninstall_help)
    add("--yes", action="store_true", help="with --uninstall, don't ask first")


# What every route's --uninstall honours; each caller adds where to look.
_ALWAYS_KEPT = {"uninstall", "yes", "dry_run", "no_color"}


def requested(parser, args, keep: set[str]) -> bool:
    """Whether this run is ``--uninstall``, refusing what it doesn't honour.

    Called ahead of every other mode, so none of them can win over it
    silently. ``keep`` names the dests the route honours besides
    `_ALWAYS_KEPT`, and is an allowlist so it fails closed: a flag added to
    the parser later is refused here until someone decides the mode honours
    it, where a list of conflicting flags would let it through. "Given"
    means it differs from its default, the same test ``rebuild_argv``
    forwards on.
    """
    if not args.uninstall:
        if args.yes:
            parser.error("--yes only applies to --uninstall")
        return False
    kept = keep | _ALWAYS_KEPT
    given = [action.option_strings[0] if action.option_strings
             else action.dest
             for action in parser._actions
             if action.dest not in kept and action.default != argparse.SUPPRESS
             and getattr(args, action.dest, action.default) != action.default]
    if given:
        parser.error(f"--uninstall takes no {', '.join(given)}: it removes "
                     "everything this tool installed, so run it without "
                     f"{'that flag' if len(given) == 1 else 'those flags'}")
    return True


@dataclass
class Step:
    """One change a plan makes, in the order it makes them.

    ``path`` is what it removes, or for an ``edit``/``run`` the file or
    program it acts on. ``label`` replaces the path on the list where a path
    alone would not say what happens (an rc key flipped, a preset loaded).
    ``recheck`` re-asks the planner's ownership test just before acting; a
    step whose test fails is skipped. ``do`` carries out an ``edit``/``run``
    and returns whether it worked; a ``remove`` needs none. ``required``
    marks a step whose failure would leave a later one dangling (an rc
    still pointing at the preset about to go), so the run stops there.
    """
    group: str
    path: Path | None
    action: str = "remove"            # "remove" | "edit" | "run"
    label: str = ""
    note: str = ""
    recheck: Callable[[], bool] | None = None
    do: Callable[[], bool] | None = None
    required: bool = False


@dataclass
class Plan:
    """Everything a run would do, and everything it leaves on purpose.

    ``left`` holds (path, why) for files that look like ours but fail an
    ownership test, or that something staying still uses. ``after`` holds
    steps only the reader can take (a setting a running EasyEffects owns),
    printed once the run is done. ``then`` names what the route does once
    the removals are through, like a PipeWire restart, so the list the
    reader says yes to is the whole run.
    """
    steps: list[Step] = field(default_factory=list)
    left: list[tuple[Path, str]] = field(default_factory=list)
    after: list = field(default_factory=list)
    then: list[str] = field(default_factory=list)
    # Run once the reader has said yes, before the first step: a request a
    # removal needs made first but that is not itself a change to list.
    prepare: Callable[[], None] | None = None
    # The directories looked in, for the nothing-to-do line: a reader who
    # installed with --output-dir learns from it to pass it again.
    where: str = ""


def trash_available() -> bool:
    """Whether removed files can go to the Trash rather than be deleted.

    ``gio trash`` is the freedesktop Trash every GLib desktop's file
    manager reads, and ``gio`` ships with GLib itself, so it is the existing
    tool for this (CLAUDE.md: wrap it rather than write a Trash spec
    implementation)."""
    return tool_env.which("gio") is not None


def _trash(path: Path) -> str:
    """Move ``path`` to the Trash; "" on success, else why not."""
    try:
        proc = tool_env.run(["gio", "trash", "--", str(path)],
                            capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError) as exc:
        return str(exc)
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout).strip() or f"gio exit {proc.returncode}"
    return ""


def _remove(path: Path, trash: bool) -> str:
    """Remove one file; "" on success, else why not.

    A Trash failure is reported, never retried as a delete: the reader was
    told the file could be got back, and a silent unlink would break that.
    """
    if trash:
        return _trash(path)
    try:
        path.unlink()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return exc.strerror or str(exc)
    return ""


def confirm(question: str, assume_yes: bool) -> bool | None:
    """Ask ``question`` as y/N. True only on a yes; None with nobody to ask.

    Asks only where `console.interactive` allows. EOF and Ctrl-C answer no. None is not a no: the caller says how to
    answer without a terminal (``--yes``), which a no must not suggest.
    """
    if assume_yes:
        return True
    if not console.interactive():
        return None
    try:
        raw = input(f"{question} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return raw in ("y", "yes")


def print_plan(plan: Plan, trash: bool) -> None:
    """The list the reader answers: every step, then what stays and why."""
    group = None
    for step in plan.steps:
        if step.group != group:
            group = step.group
            console.cprint("head", f"{group}:")
        what = step.label or doctor.tilde(step.path)
        line = f"  - {what}"
        if step.note:
            line += f"  ({step.note})"
        print(line)
    for line in plan.then:
        console.cprint("head", f"Then: {line}")
    if plan.left:
        # Yellow, not dim: the one part of the list that says the machine
        # won't be fully back to before.
        console.cprint("head", "Left in place:")
        for path, why in plan.left:
            console.cprint("warn", f"  - {doctor.tilde(path)} — {why}")
    if any(s.action == "remove" for s in plan.steps):
        console.cprint("dim", "Removed files go to the Trash; restore them "
                       "from your file manager's Trash if you need them back."
                       if trash else
                       "gio is not installed, so removed files are deleted "
                       "permanently rather than moved to the Trash.")


def apply(plan: Plan, trash: bool) -> tuple[int, int]:
    """Carry out ``plan`` in order. Returns (removed, failed).

    Stops at the first failed ``required`` step, before anything after it
    could point at a file that is gone."""
    removed = failed = 0
    for step in plan.steps:
        what = step.label or doctor.tilde(step.path)
        if step.recheck is not None and not step.recheck():
            # Not "it changed": the re-check also fails when something the
            # run meant to remove first is still there and still uses it.
            console.cprint("warn", f"  Kept {what} — it failed a last check "
                           "before removal, so it is left as it is")
            continue
        if step.action == "remove" and not (step.path.exists()
                                            or step.path.is_symlink()):
            # Gone already, which is what was asked; but not this run's
            # doing, so it doesn't count toward what finish() unloads.
            console.cprint("dim", f"  Already gone: {what}")
            continue
        if step.action == "remove":
            why = _remove(step.path, trash)
            ok = not why
            if ok:
                removed += 1
                verb = "Moved to Trash" if trash else "Removed"
                console.cprint("dim", f"  {verb}: {what}")
            elif trash:
                # gio refuses some mounts outright ("Trashing on system
                # internal mounts is not supported"); the delete it would not
                # do is the reader's to choose, so the command is given.
                console.cprint("err", f"  Could not move {what} to the Trash: {why}")
                console.cprint("cta", "    To delete it instead: rm "
                               + shlex.quote(str(step.path)))
            else:
                console.cprint("err", f"  Could not remove {what}: {why}")
        else:
            try:
                ok = bool(step.do and step.do())
            except OSError as exc:
                console.cprint("err", f"  {exc.strerror or exc}")
                ok = False
            if ok:
                console.cprint("dim", f"  Changed: {what}")
            else:
                console.cprint("err", f"  Failed: {what}")
        if not ok:
            failed += 1
            if step.required:
                console.cprint("err", "  Stopped here, so nothing after this "
                               "step points at a file that is gone.")
                break
    return removed, failed


def run(plan: Plan, *, dry_run: bool, assume_yes: bool,
        finish: Callable[[], int] | None = None) -> int:
    """List, ask, apply. Returns the exit code.

    ``finish`` completes the route once anything was removed (restart
    PipeWire), even when some other step failed: the files that did go
    must not stay loaded.
    """
    if not plan.steps:
        # "Left to remove", not "installed": a file kept because something
        # of the reader's uses it was installed by this tool, and stays.
        console.cprint("ok", f"Nothing left to remove in {plan.where}.")
        print_plan(plan, trash=False)
        _print_after(plan)
        return 0
    trash = trash_available()
    console.cprint("head", "This will change:")
    print_plan(plan, trash)
    print()
    if dry_run:
        console.cprint("head", "Dry run — nothing was changed. Run it again "
                       "without --dry-run to make these changes; it asks "
                       "first unless you add --yes.")
        _print_after(plan, dry_run=True)
        return 0
    answer = confirm("Make these changes? Nothing outside this list is touched.",
                     assume_yes)
    if answer is None:
        # confirm() needs both stdin and stdout on a terminal; a pipe into
        # tee is the everyday way to lose one.
        console.cprint("err", "Can't ask for confirmation: this run's input "
                       "or output isn't a terminal (piped into tee, for "
                       "example), so nothing was changed. Run it from a "
                       "terminal without the pipe, or add --yes.")
        return 1
    if not answer:
        console.cprint("head", "Nothing was changed.")
        return 1
    if plan.prepare is not None:
        plan.prepare()
    removed, failed = apply(plan, trash)
    if removed:
        _print_after(plan)
    rc = 1 if failed else 0
    if removed and finish is not None:
        rc = max(rc, finish())
    if rc == 0:
        print()
        moved = "moved to the Trash" if trash else "deleted"
        console.cprint("ok", f"Done — {removed} file(s) {moved}"
                       + (", and the files under \"Left in place\" kept."
                          if plan.left else "."))
    return rc


def _print_after(plan: Plan, dry_run: bool = False) -> None:
    if not plan.after:
        return
    print()
    # Conditional under --dry-run: nothing was removed, and a reader who
    # acted on these now would switch off a setting that is still in use.
    console.cprint("head", "After a real run, still to do by hand:" if dry_run
                   else "Still to do by hand:")
    for item in plan.after:
        # A (prose, command) pair prints the command on its own line, never
        # folded: wrapped across two lines it stops being pasteable.
        prose, command = item if isinstance(item, tuple) else (item, "")
        console._cprint_wrapped("cta", f"  - {prose}", indent="    ")
        if command:
            console.cprint("cta", f"    {command}")
