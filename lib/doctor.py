"""The shape of a diagnostic report, shared by both doctors.

`dolby_to_easyeffects.py --doctor` checks the environment an EasyEffects
preset lands in; `ee_to_pipewire.py --doctor` checks the PipeWire chain. They
report different things but must read as one tool — same status boxes, same
counts, same verdict wording — so the vocabulary lives here rather than in
either of them.

Stdlib-only, like `version.py` and `ee_paths.py`: `ee_to_pipewire.py` must
not pull the generator's numpy/scipy into a report that is mostly about
PipeWire. Each printer takes the caller's own ``cprint``, because the two
scripts hold separate consoles — identically built, both on stdout, but
neither reachable from here without importing a script this module must stay
below.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DOCTOR_PASS", "DOCTOR_WARN", "DOCTOR_FAIL", "DOCTOR_UNKNOWN",
           "tilde",
           "CheckResult", "summarize", "emit_check",
           "print_summary",
           "print_verdict"]

DOCTOR_PASS, DOCTOR_WARN, DOCTOR_FAIL, DOCTOR_UNKNOWN = "PASS", "WARN", "FAIL", "?"

_STYLE = {DOCTOR_PASS: "ok", DOCTOR_WARN: "warn",
          DOCTOR_FAIL: "err", DOCTOR_UNKNOWN: "dim"}


def tilde(path) -> str:
    """Render a path with $HOME collapsed to ~ — paste-safe (no username).

    Part of the shared vocabulary for the same reason the status boxes are:
    both reports are written to be pasted into an issue, and a home path is
    the one line in them that carries something the reporter didn't mean to
    send. The separator is the literal "/" rather than ``os.sep`` — the only
    platform either doctor describes is the one PipeWire and EasyEffects run
    on, and spelling it costs this module no import it doesn't already have.
    """
    s = str(path)
    home = str(Path.home())
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


@dataclass
class CheckResult:
    """One diagnostic line: a status, a short label, and an actionable detail."""
    status: str          # DOCTOR_PASS / WARN / FAIL / UNKNOWN
    label: str
    detail: str
    steps: tuple[tuple[str, str], ...] = ()
                         # the fix, as ``(cprint style, text)`` lines printed
                         # verbatim under the detail. Styled pairs rather than
                         # one string because a procedure interleaves prose and
                         # commands, and the same list is what the caller's own
                         # end-of-run block prints — one builder, no drift.


def summarize(checks) -> tuple[int, int, int, int]:
    """Count (FAIL, WARN, PASS, UNKNOWN). UNKNOWN is counted so an
    unverifiable run isn't silently summarised as clean."""
    return (
        sum(1 for c in checks if c.status == DOCTOR_FAIL),
        sum(1 for c in checks if c.status == DOCTOR_WARN),
        sum(1 for c in checks if c.status == DOCTOR_PASS),
        sum(1 for c in checks if c.status == DOCTOR_UNKNOWN),
    )


def emit_check(check: CheckResult, cprint, width: int) -> None:
    """Print one check: the status box, the label, wrapped detail, then steps.

    ``width`` is required, and passed in for the same reason ``cprint`` is:
    this module is stdlib-only (``tests/test_layout.py``'s ``STDLIB_ONLY``),
    so it cannot reach ``lib.console`` to ask what measure the rest of the run
    prints at. A default here could only be a second wrap policy — which is
    what it was, and the two disagreed whenever output was redirected.

    The split is what makes a fix reachable from here at all. ``detail`` is
    prose and wraps; ``steps`` is printed exactly as given, because a command
    folded across two lines is not runnable — a line wider than the terminal
    is soft-wrapped by the terminal instead, which still copy-pastes. So a
    check's prose belongs in the detail and its commands in the steps, and
    checks used to send readers elsewhere for the fix only because this
    printer had nowhere to put one.
    """
    cprint(_STYLE.get(check.status, "dim"),
           f"  [{check.status:^4}] {check.label}")
    for line in textwrap.wrap(check.detail, width=width - 9):
        cprint("dim", f"         {line}")
    for style, text in check.steps:
        # A blank step is a paragraph break; indenting it would leave trailing
        # whitespace in output people paste into issues.
        cprint(style, f"         {text}" if text else "")


def print_summary(checks, cprint) -> None:
    """The counted one-line summary. Separate from the verdict because the two
    reports put different things between them."""
    fail, warn, ok, unknown = summarize(checks)
    parts = [f"{fail} FAIL", f"{warn} WARN", f"{ok} PASS"]
    if unknown:
        parts.append(f"{unknown} UNKNOWN")
    cprint("err" if fail else ("warn" if (warn or unknown) else "ok"),
           "Summary: " + ", ".join(parts))


def print_verdict(checks, cprint) -> None:
    """The one-line verdict.

    A WARN suppresses the all-clear. Every warning either report can raise
    names something that plausibly explains "I hear no difference", so
    "no blocking problems" printed beside one contradicts the lines above it —
    in the output the issue form asks people to paste when something is wrong.
    """
    fail, warn, ok, unknown = summarize(checks)
    if not (fail or warn or unknown):
        cprint("ok", "No blocking problems detected.")
    elif warn and not fail:
        cprint("warn", "Nothing failed outright — the ⚠ lines above are what "
                       "to fix first.")
    elif unknown and not fail:
        cprint("warn", "Some checks couldn't be verified (the [ ? ] lines "
                       "above); the rest look OK.")
