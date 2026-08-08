"""The shape of a diagnostic report, shared by both doctors.

`dolby_to_easyeffects.py --doctor` checks the environment an EasyEffects
preset lands in; `ee_to_pipewire.py --doctor` checks the PipeWire chain. They
report different things but must read as one tool — same status boxes, same
counts, same verdict wording — so the vocabulary lives here rather than in
either of them.

Stdlib-only, like `version.py` and `ee_paths.py`: `ee_to_pipewire.py` must
not pull the generator's numpy/scipy into a report that is mostly about
PipeWire. Each printer takes the caller's own ``cprint``, which dates from
when the two scripts held a console each and neither was reachable from here.
There is one console now, in `lib/console.py`, and it is still not reachable
from here — but for a harder reason: it imports this module, for ``tilde``,
to render the failure all three entry points end on. So the arrow points one
way only, and an ``import console`` added below would close a cycle.
"""

from __future__ import annotations

import getpass
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DOCTOR_PASS", "DOCTOR_WARN", "DOCTOR_FAIL", "DOCTOR_UNKNOWN",
           "tilde", "dollar_user",
           "CheckResult", "summarize", "emit_check",
           "print_summary",
           "print_verdict"]

DOCTOR_PASS, DOCTOR_WARN, DOCTOR_FAIL, DOCTOR_UNKNOWN = "PASS", "WARN", "FAIL", "?"

_STYLE = {DOCTOR_PASS: "ok", DOCTOR_WARN: "warn",
          DOCTOR_FAIL: "err", DOCTOR_UNKNOWN: "dim"}


# Where a desktop mounts a removable disk: udisks2 uses ``/run/media/<login>/
# <label>`` (Fedora, Arch) or ``/media/<login>/<label>`` (Debian, Ubuntu), one
# directory per login. ``/run/media`` needs no entry of its own — the pattern
# anchors on the ``/media`` segment, not on the start of the string, which is
# also what lets a test stand a temporary directory in for the real root.
# ``/mnt`` carries no per-login convention, but a hand-mounted ``/mnt/<login>``
# costs nothing to cover once the substitution is gated on the name matching.
_MOUNT_PARENTS = ("media", "mnt")


def dollar_user(path) -> str:
    """Render a mount path's login component as ``$USER`` — paste-safe.

    The other half of what ``tilde`` promises, and unreachable from it:
    ``/run/media/ann/Windows`` carries the login name without living under
    ``$HOME``. That is the path a Windows-partition user's run prints — the
    mount it was auto-detected on, the tuning XML matched inside it — and the
    run log is what gets pasted into a public issue.

    **Only a component that equals this login is replaced.** Substituting
    whatever follows ``/run/media`` instead would mangle a shared mount and
    destroy a volume label that happens to read like a name. The label and any
    ``dax3_ext_*`` wrapper directory survive verbatim on purpose: they are how
    a reporter re-types the path, and how triage tells which extraction layout
    they used.

    ``$USER`` rather than a ``<user>`` placeholder for the reason ``~`` beats
    spelling ``$HOME`` out — a shell expands it back, so the printed path stays
    re-typable, and because the substitution only ever stands in for the string
    it replaced, expanding it cannot produce a different path. It is also why
    the single-quoted copy-paste lines keep the absolute path: a shell expands
    neither ``~`` nor ``$USER`` inside single quotes.

    The login comes from ``getpass.getuser()``, which reads ``LOGNAME``/
    ``USER``/``LNAME``/``USERNAME`` before the passwd file — deliberately not
    from ``Path.home()``, whose last component is a different string on any run
    with ``$HOME`` pointed elsewhere, and identical on none that matters.
    """
    s = str(path)
    try:
        user = getpass.getuser()
    except OSError:         # no login in the environment and no passwd entry
        return s
    if not user:
        return s
    parents = "|".join(_MOUNT_PARENTS)
    # The leading "/" is the left boundary: it keeps /multimedia/ann out. The
    # right one is tilde's, so /media/annie and /media/ann.bak stay whole.
    return re.sub(rf"(/(?:{parents}))/{re.escape(user)}(?![\w.~-])",
                  r"\1/$USER", s)


def tilde(path) -> str:
    """Render a path with no username in it — $HOME as ~, a mount as $USER.

    Part of the shared vocabulary for the same reason the status boxes are:
    both reports are written to be pasted into an issue, and a home path is
    the one line in them that carries something the reporter didn't mean to
    send. Every run prints paths, not just the doctors, so this is the
    renderer for all of them — the separator is the literal "/" rather than
    ``os.sep``, since the only platform either script describes is the one
    PipeWire and EasyEffects run on.

    Two rules, kept apart because they rest on different arguments, and
    applied together here because they are one promise at the print: a caller
    that renders a path must make both, and the site list is the same one.
    ``$HOME`` first, so that a home *inside* a mount collapses as a home
    rather than being split across the two.

    Takes a whole path *or* a message with one inside it: the two scripts'
    top-level error printers hand it a caught exception, whose path arrives
    mid-sentence and, for an OSError, inside repr quotes. Matching only at
    the string's start would leave every one of those absolute.

    The boundaries are what keep that from over-matching: ``/home/ann`` may
    not fire inside ``/home/annie`` (right edge) or ``/mnt/bak/home/ann``
    (left edge), so the characters either side must be ones a path component
    cannot continue through. Apply it at the print, never to the variable —
    a collapsed path that is also *written* somewhere (a convolver filename,
    a file to create) is a path nothing else expands.
    """
    s = str(path)
    home = str(Path.home())
    if home not in ("", "/"):   # else a root-owned or homeless run: no $HOME
        s = re.sub(rf"(?<![\w./~-]){re.escape(home)}(?![\w.~-])", "~", s)
    return dollar_user(s)


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
