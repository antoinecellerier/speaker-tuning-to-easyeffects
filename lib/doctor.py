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
           "tag", "tilde", "dollar_user", "no_bt_address",
           "CheckResult", "summarize", "emit_check",
           "another_version_check",
           "print_summary",
           "print_verdict"]

DOCTOR_PASS, DOCTOR_WARN, DOCTOR_FAIL, DOCTOR_UNKNOWN = "PASS", "WARN", "FAIL", "?"

_STYLE = {DOCTOR_PASS: "ok", DOCTOR_WARN: "warn",
          DOCTOR_FAIL: "err", DOCTOR_UNKNOWN: "dim"}


def tag(status: str) -> str:
    """The bracketed status as a check line shows it: ``[WARN]``, ``[ ?  ]``.

    The verdict lines send readers to "the [WARN] lines above", so they build
    that label through this rather than spelling it out. Both used to, and
    both were wrong: the WARN one named a ⚠ that appears nowhere in either
    report, and the UNKNOWN one wrote ``[ ? ]`` where the centring yields
    ``[ ?  ]``. Either way the reader searched for a string that wasn't there.
    """
    return f"[{status:^4}]"


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
    except (OSError, KeyError):  # no login in the environment, no passwd entry
        # `KeyError` as well as `OSError`, because `getuser` only funnels every
        # failure into the latter from Python 3.13 on; before that the
        # `pwd.getpwuid` miss comes out as it is, and Ubuntu 24.04 ships 3.12.
        # Not a corner worth getting wrong: `tilde` calls this on nearly every
        # path either script prints, `console.run_guarded`'s error line
        # included, so this raising turns a run's one readable failure message
        # into a traceback.
        return s
    if not user:
        return s
    parents = "|".join(_MOUNT_PARENTS)
    # The leading "/" is the left boundary: it keeps /multimedia/ann out. The
    # right one is tilde's, so /media/annie and /media/ann.bak stay whole.
    return re.sub(rf"(/(?:{parents}))/{re.escape(user)}(?![\w.~-])",
                  r"\1/$USER", s)


# A Bluetooth node name is `bluez_output.<addr>.<profile>` with the address's
# colons turned into underscores, though the colon form reaches us too (it is
# what WirePlumber's own state files hold). Anchored on the `bluez_` prefix
# rather than on "six hex pairs", so a token that merely looks like an address
# somewhere else in a line is left alone.
_BT_ADDRESS_RE = re.compile(
    r"(?<![\w.-])(bluez_(?:output|input)\.)"
    r"(?:[0-9A-Fa-f]{2}[:_]){5}[0-9A-Fa-f]{2}")


def no_bt_address(text) -> str:
    """Render a node name with no Bluetooth address in it — paste-safe.

    The third of the same promise ``tilde`` and ``dollar_user`` make, for the
    one field they never see: a PipeWire node name, which arrives from
    ``pw-dump`` verbatim and carries the adapter's MAC when the device is a
    headset. Several blocks list *every* sink on the machine, and the issue
    form asks for those blocks whole, so a connected headset's address is
    pasted in public by a reporter who was only sending a speaker log.

    Only the address goes. ``bluez_output.`` and the trailing profile number
    survive, for the reason ``dollar_user`` keeps volume labels: they are the
    part with triage value — a Bluetooth sink exists, on that profile — and the
    address has none, since nothing this tool does is per-device for hardware
    it does not tune.

    **``<mac>`` is not a placeholder a shell expands**, unlike ``~`` and
    ``$USER``. That is deliberate — there is no expansion that reproduces an
    address — and it is why this must never be applied to a command line: a
    redacted name is not runnable. Where a command would name a redacted sink,
    the caller drops the command, not the redaction.

    **Applies to what the tool discovered, not to what the user typed.** A name
    a reader passed on the command line is theirs, and redacting it back at them
    makes a diagnostic lie about its own input: ``--autoload-sink
    'bluez_output.<mac>.1': not currently in pw-dump`` gives someone who
    mistyped a hex digit no way to see it. The enumerations this tool prints
    unbidden — every sink on the machine, the selected output, the sink
    EasyEffects is on — are the ones a reporter never chose to send, and they
    are what this renders.

    Takes a whole name or a line with one inside it, as ``tilde`` does, so a
    caller can hand it an already-rendered line. Non-strings are stringified
    rather than rejected: this runs at the print, and a render is not where a
    surprising type should surface.
    """
    return _BT_ADDRESS_RE.sub(r"\1<mac>", str(text))


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


def another_version_check(label: str, noun: str, versions,
                          running: str, remedy: str) -> "CheckResult | None":
    """WARN when what is installed was written by a different build.

    One sentence shape for both doctors: a conf and a preset are the same
    kind of artefact — a snapshot of what this tool believed when it ran —
    and a reader just told to update the tool must not have to work out that
    the file did not update with it. An empty version is not drift: an
    EasyEffects GUI save rebuilds a preset's JSON from scratch and drops the
    stamp (upstream ``savePresetFile``), and an unreadable conf header is
    not a mismatch either — "" means unknown, never stale. No folder in the
    sentence: the inventory block above already names it, and a path here is
    what leaked the preview harness's staging tree into a rendered block.
    """
    versions = list(versions)
    stale = sorted({v for v in versions if v and v != running})
    if not stale:
        return None
    n = sum(1 for v in versions if v in stale)
    total = len(versions)
    return CheckResult(
        DOCTOR_WARN, label,
        f"{n} of {total} {noun}{'s' if total != 1 else ''} "
        f"{'was' if n == 1 else 'were'} written by {', '.join(stale)} and "
        f"this is {running}. If a fix since then was meant to reach your "
        f"audio, {remedy} — a {noun} is a snapshot, it doesn't update "
        "itself.")


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
    cprint(_STYLE.get(check.status, "dim"), f"  {tag(check.status)} {check.label}")
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
    elif fail:
        # A FAIL used to print no verdict at all: the branches below are each
        # guarded on `not fail`, so the one state that most needs a closing
        # instruction ended on the summary counts alone.
        cprint("err", f"Fix the {tag(DOCTOR_FAIL)} lines above first.")
    elif warn and unknown:
        # Both, because this branch used to print the WARN sentence alone and
        # leave the unknowns unmentioned — beside a check saying a missing
        # package is "the usual reason a conf loads nothing", "the WARN lines
        # are what to fix first" reads as a ruling on the one line it never
        # looked at.
        cprint("warn", f"Nothing failed outright. Start with the "
                       f"{tag(DOCTOR_WARN)} lines above; the "
                       f"{tag(DOCTOR_UNKNOWN)} ones are checks that couldn't "
                       "run, and may be hiding the real fault.")
    elif warn:
        cprint("warn", f"Nothing failed outright — the {tag(DOCTOR_WARN)} lines "
                       "above are what to fix first.")
    elif unknown and not fail:
        cprint("warn", f"Some checks couldn't be verified (the "
                       f"{tag(DOCTOR_UNKNOWN)} lines above); the rest look OK.")
