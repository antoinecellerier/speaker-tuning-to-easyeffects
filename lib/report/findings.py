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

The factories that build the findings raised outside `lib/dax/parse.py`'s own
table came next, with `print_project_asks` and the `_print_ask` bullet
renderer — the *second* half of the contract, where each finding's `ask` lands
at the end of the run. The record type arrived here first, one slice early,
because `lib/dax/parse.py` could not be extracted without it: `parse_xml` ends
by building and printing findings, and a move commit may not re-point the call
sites it carries across.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

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


# The device-report issue form (.github/ISSUE_TEMPLATE/device-report.yml).
# There is exactly one link in the output and this is it. Everything the
# closing block asks about is device-specific, and acting on any of it needs
# what this form requires — model, --speaker-info output, the generation log.
# A second, generic /issues link used to ride the mid-run feature-gap
# warnings; once those moved into the same closing block it was simply a rival
# call to action, pointing somewhere reports arrive stripped of that context.
_REPORT_FORM_URL = (
    "https://github.com/antoinecellerier/speaker-tuning-to-easyeffects"
    "/issues/new?template=device-report.yml"
)


# --- Finding factories -----------------------------------------------------
#
# Every finding raised outside the _UNMODELED_FEATURES table is built here,
# one function each, rather than inline at its raise site. They are the single
# definition of their wording: an earlier arrangement had the strings inline
# and the contract tests restating them, which is the drift e3a7ee4 removed
# from the doctor/warning pair — two copies, edited one at a time.

def _profile_mismatch_finding(declared: str, profile_used: str) -> Finding:
    """Dolby names a different profile than the one we built."""
    # kind="ask": "tell us which sounds better" is something the project
    # needs, and hint-routing left the one ending that solicits the
    # comparison without the Help-the-project block or the attach path
    # (round 8). This is also the confirmation channel the parked
    # build-the-declared-default change waits on.
    return Finding(
        slug="profile-mismatch", kind="ask",
        # The naming note pre-empts a round-6 worry: a reviewer assumed the
        # suggested --profile re-run would overwrite the presets they were
        # told to compare against.
        detail=f"This XML names '{declared}' as the profile the device ships "
               f"on under Windows, but we built '{profile_used}' (this "
               "speaker's first-listed). A --profile re-run writes its own "
               "preset files, so both stay installed.",
        # Names the action and what it gets you. An earlier wording led with
        # "worth an A/B against Windows", which read as though the user had to
        # go and do something in Windows.
        # Names both sides. "the profile this device ships on" alone left the
        # reader unable to tell what they'd be comparing against, and reading
        # as though the tool had knowingly picked the wrong one.
        # Says why the names matter and closes the loop: "re-run to compare"
        # alone left a reviewer comparing with no idea what to do with the
        # result, and the two names connected to nothing else in the block.
        # "the Windows default", not "Windows uses": default_profile is the
        # shipping default; what the user actually ran on Windows may
        # differ.
        ask=f"We built '{profile_used}' but the Windows default is "
            f"'{declared}' — re-run with --profile {declared} and tell us "
            "which sounds better.")


def _untamed_boost_ask(coupled_bands_possible: bool) -> str:
    """The two-step ask both members of the untamed-boost family carry.

    One template for one risk family (round 8: two wordings for the same risk
    left the reader unsure which explanation to trust) — but step 2 only
    exists where `--enable coupled-bands` could actually do something. On a
    tuning with no qualifying band the flag changes nothing, the run's own
    "Optional extras" menu doesn't offer it, and a re-run answers
    "--enable coupled-bands had no effect".

    "(not both)": every compact form — "swap it for" (round 3), "instead"
    (round 5), "just" (round 8), "replace that flag with" (round 9) — kept
    reading ambiguous against the seam line's "they combine". The
    parenthetical says it outright.
    """
    if not coupled_bands_possible:
        return "If loud parts distort, re-run with --disable volmax."
    return ("If loud parts distort, re-run with --disable volmax; if "
            "still harsh, swap to --enable coupled-bands (not both).")


def _loudness_untamed_finding(coupled_bands_possible: bool = True) -> Finding:
    """Every regulator band sits at or above 0 dBFS, so nothing is tamed."""
    return Finding(
        slug="loudness-untamed",
        # Self-contained: it used to say "threshold_high above", pointing at
        # a table that only prints with -v now. The field name stays in
        # parentheses as the grep handle. No limiter noun at all (round 8):
        # "brickwall" → "final safety limiter" → "the preset's own output
        # limiter" each read as a second mystery stage; what the reader
        # needs is the consequence, phrased identically to the
        # boost-unlimited sibling — one template for one risk family.
        # No raw field name (round 9): "threshold_high" read as leaked
        # code and undercut trust. The -v table still prints the field.
        # "band by band" is load-bearing, not filler: limiter#0 ships on
        # every preset, so the bare "nothing limits it" that dropping the
        # noun left behind was false. The qualifier keeps the sentence
        # true without reintroducing a stage the reader has to look up.
        detail="This tuning's regulator never engages — every band's "
               "limit sits at or above full volume — so nothing trims "
               "the loudness boost band by band on its way out.",
        # Same two-step ask as boost-unlimited — one template for one risk
        # family (round 9); coupled-bands is exactly the all-inert class's
        # remedy (issue #27), where it qualifies.
        ask=_untamed_boost_ask(coupled_bands_possible))


def _boost_unlimited_finding(peak_db: float, freq,
                             coupled_bands_possible: bool = True,
                             restored: bool = False) -> Finding:
    """The band carrying the largest boost is one the regulator leaves free."""
    # Name everything riding on that band, not just volmax: under
    # --enable level-restore the peak itself is added back as gain, so
    # "with the volmax boost on top" would describe half the drive. The
    # clause stays one phrase either way — this is a detail line, and the
    # flag's own menu entry carries the "may distort" caveat.
    on_top = ("the volmax boost and the restored level on top" if restored
              else "the volmax boost on top")
    return Finding(
        slug="boost-unlimited",
        # Same closing formula as loudness-untamed — one template for one
        # risk family (round 8: two wordings for the same risk left the
        # reader unsure which explanation to trust).
        detail=f"The biggest correction boost ({peak_db:+.1f} dB at {freq} Hz) "
               f"lands on a band the regulator leaves unlimited, with "
               f"{on_top} — nothing trims it band by band on its "
               "way out.",
        # Sequenced, and step 2 speaks the menu's symptom family for
        # coupled-bands (harshness) instead of inventing its own: with
        # "if they still distort" the same screen sold the flag for
        # distortion while the menu sold it for harshness (rounds 3 and 5
        # — one heard symptom per flag). Not "loud music": the vocabulary
        # trap reserves "music" for the mbc symptom. No region word — the
        # unlimited band's frequency is device-specific and the detail
        # above already names it. Wording and the "(not both)" rationale
        # live in _untamed_boost_ask, shared with loudness-untamed.
        ask=_untamed_boost_ask(coupled_bands_possible))


def _experimental_finding(named: str, flags: list[str]) -> Finding:
    """Emission paths reproduced from the XML but never confirmed by ear.

    The ask has to say what to listen for and how to compare, because the
    reader has no reference: they have never heard this laptop tuned
    correctly, so "does it sound right?" is unanswerable on its own. Naming
    the --disable flag turns it into an A/B they can actually run.
    """
    if len(flags) == 1:
        ask = (f"Re-run with --disable {flags[0]} and tell us which version "
               "sounded better.")
    else:
        ask = "Tell us whether it sounds right — either answer helps."
    # Not slug="experimental": the --enable menu describes coupled-bands as
    # "experimental (issue #44)" on the same screen, so a report quoting
    # "[experimental]" could mean either. The slug states the situation the
    # detail describes; the menus keep "experimental" as an adjective.
    return Finding(
        slug="unconfirmed-by-ear", kind="ask",
        detail=f"Built from your tuning but never confirmed by ear: {named}. "
               "These come straight out of the Dolby file and the numbers "
               "check out, but nobody with a device that uses them has told "
               "us how they sound.",
        ask=ask)


def _firmware_gate_finding() -> Finding:
    """Whether toggling the smart-amp gate actually restored the bass."""
    return Finding(
        slug="firmware-gate", kind="ask",
        detail="Smart-amp firmware gate is off — see the procedure above.",
        ask="Did toggling the smart-amp control change how it sounds? "
            "(issue #17)")


def _leveler_gap_finding(substages: list[str], autogain_on: bool,
                              autogain_available: bool = True,
                              disabled_by_flag: bool = False) -> Finding | None:
    """The Dolby leveler companion stages this converter cannot reproduce.

    Unlike every other mapping these carry no parameters at all — the schema
    has an on/off bit and nothing else, no threshold, ratio, attack or release
    in either tuning block — so no stage can be derived from them, and
    inventing one is the per-device hand-tuning the XML-only rule forbids.

    Two strengths. Where the leveler ships bypassed (HDA default) the
    companions cannot be heard and there is nothing for anyone to do: detail
    only, no ask. Where the leveler runs (SoundWire default, or ``--enable
    autogain``) it runs without the compressor Dolby pairs with it — a
    plausible cause of exactly the pumping that state gets blamed for — so
    that case asks for the one capture that could settle it, and names
    ``--disable autogain`` as the off-switch.

    "May be part of it", not "the most likely reason": the measured driver of
    quiet-swell/loud-duck is EE's own non-content-aware autogain (design-notes,
    "Why autogain is bypassed by default"), and the corpus doc records that the
    companion compressor does not explain the issue-#25 overshoot — neither
    device carrying it. The copy had promoted this docstring's own hedge.

    Every user-review round misread this copy until it said where the
    leveler itself stands: the parsed-XML block above prints the leveler's
    own amount/targets, so "cannot reproduce" without an owner read as the
    converter contradicting itself about the leveler.
    """
    if not substages:
        return None
    named = ", ".join(substages)
    # Plain words first, raw names in parentheses (round 7): the raw
    # volume-leveler-drc/-compressor tokens were the one list without the
    # friendly-name treatment every other stage gets.
    head = ("Also in your tuning but not rebuilt: companion compression "
            f"stages Dolby pairs with its volume leveler ({named}). "
            "Harmless as built: ")
    if not autogain_on:
        # Only point at --enable autogain when it could actually change this.
        # On a tuning whose XML disables the leveler outright the flag does
        # nothing, and suggesting it contradicts the "had no effect" warning
        # printed just above.
        #
        # --disable autogain also clears the marker, so without its own
        # branch this blamed the tuning for the reader's own flag — while
        # the leveler section a few lines up correctly credited the flag.
        if disabled_by_flag:
            tail = ("--disable autogain switched the leveler off in this "
                    "preset, so they cannot be heard.")
        elif autogain_available:
            tail = ("the leveler ships switched off in this preset, so they "
                    "cannot be heard — this only matters if you rebuild with "
                    "--enable autogain.")
        else:
            tail = ("your tuning switches the leveler off outright, so they "
                    "cannot be heard and no flag here changes that.")
        return Finding(slug="leveler-gap", kind="ask", detail=head + tail)
    return Finding(
        slug="leveler-gap", kind="ask",
        detail="The volume leveler itself is rebuilt and running in this "
               "preset. But your tuning pairs it with companion "
               f"compression stage(s) ({named}) this converter cannot "
               "rebuild: the tuning file "
               "records only that they are switched on, not how they are "
               "set. If quiet passages swell then duck when things get "
               "loud, that gap may be part of it (--disable "
               "autogain switches the "
               "leveler off). Settling it needs a capture from a Windows "
               "install with Dolby on this same machine — a few minutes "
               "of scripted recording; if you dual-boot, we'll walk you "
               "through it.",
        # Deliberately does not ask them to go and do the capture, and does
        # not point at the measure_dax README: two rounds of reviewers read
        # the self-serve route as homework that gates help and said they'd
        # give up there — the ask below owns the route ("tell us, we'll
        # walk you through it"), and the procedure link belongs in that
        # conversation. It is a multi-step measurement on a second OS, and
        # most people run this script once. The walk-you-through offer also
        # rides the detail (round 4: read top-down, "settling it needs a
        # capture" arrived 40 lines before the offer and read as an
        # unexplained requirement).
        # Names Windows so anyone who doesn't dual-boot can skip the line
        # rather than reading to the end to find out they can't help — the
        # capture measures what DAX does, so it has to run there.
        # Vocabulary is the autogain row's ("swell then duck"), NOT the
        # regulator's "wobbles or surges" — a round-2 reviewer hearing
        # volume movement couldn't tell which of the two remedies to try
        # because both claimed "surges".
        ask="If quiet passages swell then duck, tell us — a Windows "
            "capture would settle it and we'll walk you through it.")


def _print_ask(style: str, finding: Finding) -> None:
    """One bullet: the sentence first, then the slug, dimmed.

    The slug trails because a first-time reader needs the sentence, not the
    tag — it only matters once they want to scroll back to the detail it was
    raised with, so it should not be the first thing the eye lands on. Dim for
    the same reason.

    Two styles on one line means assembling spans rather than handing cprint a
    string: the console runs with markup off, so bracket syntax in the text is
    literal (which is what keeps ``[slug]`` printable at all). Spans sidestep
    that entirely — nothing is parsed out of the message.
    """
    # Scope rides in the tag, not the sentence: it is bookkeeping, and the
    # sentence has a one-line budget to keep. Silent when the finding applies
    # everywhere, which on a default single-profile run is always — so the
    # common case pays nothing for it.
    tag = (f"[{finding.slug} · {finding.scope}]" if finding.scope
           else f"[{finding.slug}]")
    lines = textwrap.wrap(f"  • {finding.ask}  {tag}", width=console._wrap_width(),
                          subsequent_indent="    ", break_on_hyphens=False)
    for line in lines:
        if console._CONSOLE is not None and line.endswith(tag):
            # Imported here, not beside the console: this is rich's only
            # caller outside cprint, and a live console already proves the
            # import succeeds. Nothing else needs the two-style path.
            from rich.text import Text
            span = Text()
            span.append(line[:-len(tag)], style=style)
            span.append(tag, style="dim")
            console._CONSOLE.print(span, soft_wrap=True)
        else:
            console.cprint(style, line)


def _print_attach_lines(xml_path) -> None:
    """The what-to-send lines, shared by both closing branches.

    cta, not dim: this is the one concrete task the report needs, and it
    printed fainter than the reassurance bullet above it (round-2 color
    finding). "If you report", the intro line's vocabulary: unconditional
    "attach this to your report" left a round-4 reviewer unsure whether
    filing was mandatory. Download link preferred over attaching: a
    driver-package link identifies the exact tuning build and carries
    every sibling XML for the device; "(if you know it)" because a reader
    who found the file on their Windows partition has no download to link
    (round 8).
    """
    if xml_path is None:
        return
    print()
    console._cprint_wrapped("cta", "  If you report, best is a link to "
                           "your device's audio-driver download "
                           "(if you know it) — or just attach the "
                           "XML file:",
                    indent="  ")
    # Absolute and quoted. Dolby's own directory names contain '$'
    # (…/code$GetExtractPath$/…), so an unquoted relative path is
    # eaten by the shell the moment anyone types ls on it and the
    # file looks missing. Same cta as its instruction — the copy
    # target must not be the faintest line in the block.
    console.cprint("cta", f"    '{Path(xml_path).resolve()}'")


def print_project_asks(findings: list[Finding], dry_run: bool = False,
                       xml_path=None, pipewire_native: bool = False) -> None:
    """Print the closing block: what the project needs, then the one ask.

    Always prints. Most people run this script once, on one machine, and
    never again, so whatever we want from them we get on this run or not at
    all — there is no next run to defer to. On a clean run that means three
    lines and no header; a rule and a heading over a bare "how does it sound"
    would be noise on the common path.

    Specifics first and the link last, so the bullets make the case for why
    this particular run is worth reporting and the URL is what is still on
    screen when the run ends.

    ``dry_run`` swaps the closing line, because nothing was installed and
    "how does it sound?" is then an impossible instruction — the announcement
    that this was a dry run is hundreds of lines up by the time anyone reads
    the end, so the last thing on screen has to carry it too.
    """
    asks = [f for f in findings if f.kind == "ask" and f.ask]
    # Every tag shown this run, not just the ones with an ask. A hint like
    # [loudness-untamed] is often the only finding that actually fired for
    # the device, and listing only asks under "quote the tag in brackets"
    # sent reporters to quote the speculative one and never mention it.
    tagged = [f for f in findings if f.slug]
    print()
    if asks:
        console.cprint("head", "=" * 60)
        console.cprint("head", "Help the project")
        print()
        # Say what the bracketed tags are for. Read cold they look like debug
        # labels that leaked out of the code, which is how they get ignored.
        # "these most of all" introduced a list that is usually one item
        # long, and its "these" pointed backwards at nothing on a top-down
        # read.
        console._cprint_wrapped("dim", "Some of this only a real device can answer. "
                               "If you report, quote the [tag] so we know "
                               "which line you mean:")
        # Plain, not cta: bold-magenta bullets read as warnings — a round-4
        # reviewer took the peak-level reassurance ("should sound right")
        # for something being wrong, because it matched the report call's
        # color. The hierarchy is dim intro → plain bullets → cta
        # instructions (attach line, final call), so the calls to action
        # still print brighter than the specifics (round-2 rule).
        for finding in asks:
            _print_ask("", finding)
        # The tool found the tuning XML; the user never went looking for it,
        # so an ask to "send us your tuning XML" is unactionable without the
        # path. Printed once here rather than inside each bullet, which the
        # one-sentence budget has no room for.
        # For every ask, not just ones whose wording mentions the XML
        # (round 6): the file helps triage whatever the report is about,
        # and the old wording-sniffing gate was one rewording away from
        # silently switching the path off.
        _print_attach_lines(xml_path)
        print()
    elif tagged:
        # No ask fired, but something upstream still carries a tag. Say it is
        # worth quoting, or the reader is left holding a bracketed token with
        # no reason to think it means anything to us. The attach lines print
        # here too (round 10, user-picked): a run whose only findings are ⚠
        # warnings is exactly one the project wants the tuning source for,
        # and this branch used to leave its reporter with nothing to attach.
        console.cprint("head", "=" * 60)
        console._cprint_wrapped("dim", "Saw a [tag] above? Quote it if you report — "
                               "it tells us which finding you mean.")
        _print_attach_lines(xml_path)
        print()

    # Stages this tuning has that we drop. They carry no ask, because there is
    # nothing anyone can do about them — but they printed two hundred lines
    # up and never again, so the closing block read as the whole story when a
    # piece of the tuning was missing from it. One line, no bullet list: it is
    # context for a report, not another thing to action.
    dropped = [f.slug for f in findings if f.kind == "ask" and not f.ask]
    if dropped:
        # Not "Not reproduced on this device" — reviewers read that as
        # issue-tracker language ("we couldn't reproduce your bug"), the
        # opposite of what it says. And the mention needs a reason, or it is
        # a nothing-to-do entry that teaches readers to skip the block.
        console._cprint_wrapped("dim", "Parts of your tuning this converter doesn't "
                               "rebuild: "
                               + ", ".join(f"[{s}]" for s in dropped)
                               + " — nothing you need to do, but mention "
                                 "them if you report so we know which "
                                 "devices have them.")
        print()
    # The link prints either way. Suppressing it on a dry run left the block
    # above saying "quote the tag in brackets if you report one" with nowhere
    # to report to — worse than the impossible "how does it sound?" it was
    # meant to fix, because that at least named a destination.
    # For the wrapper's reader the repo name says "easyeffects" — the very
    # thing they chose this path to avoid — and the only link in the run
    # points there, so one clause says their report belongs here too.
    if dry_run:
        # Just the pointer. That nothing was written is said immediately
        # above by whoever ran the dry run — print_what_now here, the [3/3]
        # banner under dolby_to_pipewire.py — and saying it twice in
        # consecutive sentences reads like a stutter.
        lead = ("Reporting anything above? PipeWire-only reports are "
                "welcome — here's where:" if pipewire_native else
                "Reporting anything above? Here's where:")
    else:
        lead = ("How does it sound? Please report back — good or bad, or "
                "if you need help"
                + (" (PipeWire-only reports are welcome)"
                   if pipewire_native else "") + ":")
    console._cprint_wrapped("cta", lead)
    # The URL gets its own line and is never wrapped: broken across lines it
    # can't be clicked or copied, which defeats the whole point of the ask.
    console.cprint("cta", f"  {_REPORT_FORM_URL}")
