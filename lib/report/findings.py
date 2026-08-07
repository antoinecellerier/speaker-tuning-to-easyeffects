"""The one thing a run noticed, and the half of it that prints inline.

`Finding` is the record every part of the run raises when it notices
something — an unmodeled DSP block, a profile Dolby names but we don't build,
a stage whose gain nothing bounds. It is deliberately dumb: two strings, a
slug and a kind. `.claude/rules/user-messages.md` is the contract for what
goes in each half and where each half prints.

It lives here rather than in the module that raises the most of them because
it belongs to no one caller: `lib/dax/parse.py` returns them, the hardware
probes return them, and the closing block consumes them. A shared record type
in any one of those makes the other two import a module they have no other
business with.

`_print_finding_detail` comes with it, because the two are one contract —
Finding says what the halves are, this says where the first half lands — and
because `_TAG_CONVENTION_SHOWN` has to sit in the same module as the function
that writes it through `global`. main() resets that flag per run, through
this module rather than its own globals, so an in-process second run (the
`dolby_to_pipewire.py` wrapper, or a test calling main twice) starts fresh.

This is the *first third* of the module `docs/design-notes.md` plans here:
the `_*_finding()` factories and `print_project_asks`/`_print_ask` are still
in `dolby_to_easyeffects.py` and follow later. It exists this early because
`lib/dax/parse.py` could not be extracted without it — `parse_xml` ends by
building and printing findings, and a move commit may not re-point the call
sites it carries across.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib import console


@dataclass(frozen=True)
class Finding:
    """One thing a run noticed, printed in two halves with different readers.

    ``detail`` is the technical why. It prints inline, at the detection site,
    next to the values it explains — the only place it has context. Keeping it
    out of the closing block is what keeps that block scannable when several
    things fire at once.

    ``ask`` is what the user reads at the end: ONE short sentence in their
    terms, either the fix to try or the question we want answered. It is
    optional, and leaving it empty is the normal case for anything the user
    cannot act on — a "nothing for you to do" line, in a block whose whole
    purpose is to prompt action, only teaches people to skip the block. Those
    findings still print their detail inline, so they still reach us in a
    pasted report.

    ``kind`` picks the section the ask lands in: "hint" fixes the user's own
    audio, "ask" is something the project needs from them.

    ``slug`` ties the two halves together — it leads the inline line and
    trails the end-block one, so either greps to the other — and is the
    de-duplication key. Keying on rendered text (what main() did before)
    silently missed repeats whose text embeds a per-profile value, so
    --all-profiles could print one finding several times with different
    numbers in it.
    """
    slug: str
    detail: str
    ask: str = ""
    kind: str = "hint"
    # Short label for "which profiles this applies to", when it isn't all of
    # them. Empty means "applies throughout", which is every single-profile
    # run. Without it, a finding raised only in `movie` reads under
    # --all-profiles as though it applied to the preset about to be autoloaded.
    # Pre-rendered rather than a list: only main() knows how many profiles the
    # run covered, which is what decides between naming them and counting them.
    scope: str = ""


# One-time [tag] orientation, printed with a run's first finding: the first
# bracketed token a reader meets otherwise looks like an error code, and the
# explanation only arrived in the closing block (round 2). Reset per main()
# call so wrapper-driven and repeated in-process runs behave like fresh ones.
_TAG_CONVENTION_SHOWN = False


def _print_finding_detail(finding: Finding) -> None:
    """Print a finding's technical half where the condition was detected.

    Prints on every detection — the position is the point, and it is what a
    reader scrolls back to from the closing block. The slug leads here (a
    left edge is what makes it findable when scanning back through a couple
    of hundred lines of tables) and trails there. The ask half is
    de-duplicated by slug and printed once at the end, so --all-profiles
    doesn't repeat it for each of nine profiles.
    """
    global _TAG_CONVENTION_SHOWN
    if not _TAG_CONVENTION_SHOWN:
        _TAG_CONVENTION_SHOWN = True
        console._cprint_wrapped("dim", "  (bracketed [tags] like the one below are "
                               "handles — quote one if you report, so we "
                               "know which line you mean)", indent="   ")
    if finding.kind == "hint":
        console._cprint_wrapped("warn", f"  ⚠ [{finding.slug}] {finding.detail}",
                        indent="    ")
    else:
        console._cprint_wrapped("dim", f"  [{finding.slug}] {finding.detail}",
                        indent="    ")
