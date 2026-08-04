"""The shape of a diagnostic report, shared by both doctors.

`dolby_to_easyeffects.py --doctor` checks the environment an EasyEffects
preset lands in; `ee_to_pipewire.py --doctor` checks the PipeWire chain. They
report different things but must read as one tool — same status boxes, same
counts, same verdict wording — so the vocabulary lives here rather than in
either of them.

Stdlib-only, like `_version.py` and `_ee_paths.py`: `ee_to_pipewire.py` must
not pull the generator's numpy/scipy into a report that is mostly about
PipeWire. Each printer takes the caller's own ``cprint``, because the two
scripts hold separate consoles (the converter's targets stderr so it never
pollutes a conf on stdout).
"""

from __future__ import annotations

import shutil
import textwrap
from dataclasses import dataclass

__all__ = ["DOCTOR_PASS", "DOCTOR_WARN", "DOCTOR_FAIL", "DOCTOR_UNKNOWN",
           "CheckResult", "summarize", "default_width", "emit_check",
           "print_summary",
           "print_verdict"]

DOCTOR_PASS, DOCTOR_WARN, DOCTOR_FAIL, DOCTOR_UNKNOWN = "PASS", "WARN", "FAIL", "?"

_STYLE = {DOCTOR_PASS: "ok", DOCTOR_WARN: "warn",
          DOCTOR_FAIL: "err", DOCTOR_UNKNOWN: "dim"}


@dataclass
class CheckResult:
    """One diagnostic line: a status, a short label, and an actionable detail."""
    status: str          # DOCTOR_PASS / WARN / FAIL / UNKNOWN
    label: str
    detail: str


def summarize(checks) -> tuple[int, int, int, int]:
    """Count (FAIL, WARN, PASS, UNKNOWN). UNKNOWN is counted so an
    unverifiable run isn't silently summarised as clean."""
    return (
        sum(1 for c in checks if c.status == DOCTOR_FAIL),
        sum(1 for c in checks if c.status == DOCTOR_WARN),
        sum(1 for c in checks if c.status == DOCTOR_PASS),
        sum(1 for c in checks if c.status == DOCTOR_UNKNOWN),
    )


def default_width() -> int:
    """Terminal width for wrapped detail, bounded the way the generator bounds
    its prose: below the floor a hanging indent eats the line, above the cap a
    paragraph stretches into one unscannable line."""
    return max(60, min(120, shutil.get_terminal_size(fallback=(100, 24)).columns))


def emit_check(check: CheckResult, cprint, width: int | None = None) -> None:
    """Print one check: the status box, the label, then wrapped detail.

    The detail wraps, which is why no check may put a copy-paste command in
    it — a command folded across two lines is not runnable. Commands belong
    in a section that prints them unwrapped.
    """
    if width is None:
        width = default_width()
    cprint(_STYLE.get(check.status, "dim"),
           f"  [{check.status:^4}] {check.label}")
    for line in textwrap.wrap(check.detail, width=width - 9):
        cprint("dim", f"         {line}")


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
