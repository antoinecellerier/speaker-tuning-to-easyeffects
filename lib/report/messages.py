"""The closing block: the run worked, and what to do if it does not sound right.

Two screens, in the order a reader meets them. `print_troubleshooting` offers
the symptom→flag menus — one row per stage this run actually emitted, keyed on
what someone can *hear* rather than on what the stage is called. `print_what_now`
closes on success and the one instruction most people need, which is that a
preset is a thing you go and select in EasyEffects.

The two menu tables live here rather than beside the stages they switch, and
that is load-bearing twice over. They are copy — a symptom in the user's words
and a one-clause effect, whose wording is the product of a dozen review rounds
(`.claude/rules/user-messages.md`) — and they are read by `argparse` while it
builds `--disable`/`--enable`'s choices, which happens on the completion path
where numpy has deliberately not been imported yet. Keeping them beside
`lib/preset/plugins.py`'s builders would drag the DSP stack onto every TAB
press.

`VOICING_CURVES` is here on the same argument one step further out. It is the
Balanced/Detailed/Warm table, and no reader of it can be its home. The
per-profile report and the emit loop are in packages that may not import each
other, and both reach numpy — a shared table is no reason to drag scipy across
a package boundary. `dolby_to_pipewire.py`'s `--variant` choices are the third
reader and reach neither: this module is numpy-free, which is what lets the
wrapper derive those choices from the table with the DSP stack still out of
its `sys.modules`. It
is copy as much as it is data: `print_what_now` right below already names two
of the three voicings in the hint it derives from what was built. Its
insertion order is the order voicings are built in, so a reader that renders
the list — `--variant`'s choices, and `--help` behind it — inherits it.

`Finding`'s asks print through `lib/report/findings.py`; this module renders
the menus around them. `_print_ask` comes in as a bare name rather than
through its module because `print_troubleshooting` interleaves the run's own
hints with the menu rows, and a move commit may not re-point a body it
carries.
"""

from __future__ import annotations

from lib import console, doctor
from lib.report.findings import Finding, _print_ask


# Single source of truth for the --disable flag. Adding a new entry here
# automatically extends the argparse choices and the end-of-run hint
# block; each emission branch in `make_preset` is responsible for
# recording its name into the returned `emitted` set when it actually
# runs, so there is no separate plugin-key → name map to keep in sync.
# The symptoms must not overlap. They used to share vocabulary — volmax said
# "pumping/squash", mbc "squashed character", regulator "spectral pumping" —
# so a user who hears squashed sound gets three candidates and no way to
# choose, which is the same as getting none. Each one now claims a distinct
# thing you can hear, in words someone who has never read an audio manual can
# match against, and they are ordered most-likely-to-help first.
DISABLEABLE_FILTERS = {
    "volmax": ("loud parts distort or sound crushed",
               "drops the +volmax-boost static loudness gain"),
    "mbc": ("music sounds flat and lifeless, with no light and shade",
            "drops the Dolby multi-band compressor"),
    "regulator": ("the volume audibly wobbles or surges on its own",
                  "drops the per-band limiter"),
    "autogain": ("quiet passages swell, then duck when things get loud",
                 "drops the volume leveler"),
    "bass-enhancer": ("bass sounds artificial or buzzy",
                      "drops the harmonic bass generator"),
    "dialog": ("voices are too forward or shouty",
               "drops the 2.5 kHz speech-band EQ"),
    "high-shelf": ("cymbals and 's' sounds are piercing",
                   "drops Dolby's type-3 high-shelf boost (experimental)"),
    "lo-pass": ("the top end sounds dull or muffled",
                "drops Dolby's type-6/8 low-pass rolloff (experimental)"),
}

# One stage sits in both menus: the volume leveler ships active on SoundWire
# (--disable autogain switches it off) and bypassed on HDA (--enable autogain
# switches it on). Its --disable row must key off the -active marker, not the
# "autogain" marker that means "present but bypassed" and feeds the --enable
# menu — otherwise every HDA run would offer to disable a stage that is
# already off.
_DISABLE_MENU_MARKER = {"autogain": "autogain-active"}

# Mirror of DISABLEABLE_FILTERS for stages that ship present but inactive:
# --enable NAME activates them on a rebuild. Same contract — adding an
# entry extends the argparse choices and the end-of-run hint block.
# The caveat is one short clause, not an explanation: this menu sits beside
# the one-line --disable menu and reads as its twin. What the stage actually
# does, and why the mapping is what it is, live in the README and design-notes
# behind the issue number — which stays, because switching a stage ON is the
# direction that carries a risk worth naming before someone tries it.
ENABLEABLE_FILTERS = {
    # "enabling may…" marks the second clause as the flag's side effect —
    # run together with the trigger it read as one continuous symptom. The
    # risk wording is the leveler family's one phrasing ("swell then
    # duck"); three variants for one risk read as three different risks
    # (round 3).
    # Autogain says its piece three times in a run — here, at the stage that
    # detects it, and in the closing block's guaranteed-differences line — and
    # that is deliberate (user decision, round 12, after a reviewer called the
    # third one padding). The three serve different readers: one scrolled back
    # to the detection site, one reading only the closing block, one scanning
    # this menu. Trimming any of them leaves that reader with nothing.
    "autogain": ("it sounds right but quieter than it did on Windows",
                 "enabling may make quiet passages swell then duck "
                 "(issue #25)"),
    # Describes what you'd hear, not where in the chain it happens: "where the
    # limiter is inactive" names an internal state the listener has no access
    # to, so it can't be matched against anything.
    # No region claim ("in the treble"): the flag extends limiting to
    # whichever zero-threshold non-isolated bands the tuning has — treble
    # on the two examined devices (3-6 kHz dev XML, 13.9 kHz #44), but
    # full-band on the issue-#27 class, so naming treble over-claims.
    # "(issue #NN)", not the bare "(#NN)": reviewers guessed the numbers
    # were GitHub issues but had no confirmation. Still no URL — the one
    # link rule.
    "coupled-bands": ("loud music turns harsh",
                      "experimental (issue #44)"),
    # Trigger says "than with the preset off", not "than on Windows": that
    # second phrasing is autogain's, and the two flags sit in the same menu.
    # The distinction is the whole diagnosis — autogain closes a gap against
    # Windows, this one closes a gap against bypass, which is the symptom
    # that identifies a curve whose peak outruns its volmax-boost.
    "level-restore": ("it sounds quieter than with the preset switched off",
                      "experimental; loud content may distort (issue #50)"),
}

# Emission paths that are numerically verified but not yet user-validated
# on real hardware. Keys that overlap with DISABLEABLE_FILTERS are turned
# off with --disable <key>; "mbc-1band" is a marker-only name (no separate
# flag — users who want it off should pass --disable mbc instead), and
# "coupled-bands-active" is the marker make_preset emits when --enable
# coupled-bands actually engaged a zone (drop the --enable flag to turn it
# off). Used to trigger a targeted "please report" prompt at end-of-run
# when any of these fired for the current preset.
# Plain name first, the tuning's own token in parentheses (round 8:
# "type-3 high-shelf" bare read as an undefined severity level).
EXPERIMENTAL_MARKERS = {
    "high-shelf": "a treble shelf boost (the tuning's type-3 high-shelf)",
    "lo-pass": "a top-end rolloff (type-6/8 low-pass)",
    "mbc-1band": "the compressor running as a single band (group_count=1)",
    "coupled-bands-active": "the coupled-bands limiter (isolated_band)",
    "level-restore-active": "the level the impulse response was normalised by, "
                            "handed back as a static gain",
}


# The three IEQ voicings a run can build, in build order — single source
# for the emit loop and every line of copy that names them. A voicing whose
# curve the XML lacks is skipped, so copy derives its list from this ∩ the
# parsed curves rather than promising all three (round 7).
VOICING_CURVES = {
    "Balanced": "ieq_balanced",
    "Detailed": "ieq_detailed",
    "Warm": "ieq_warm",
}


def print_what_now(preset_names: list[str], autoloaded: bool,
                   dry_run: bool, output_dir=None,
                   profile_used: str | None = None,
                   n_modes: int = 0,
                   default_unknown: bool = False,
                   autogain_off: bool = False,
                   menu_printed: bool = False,
                   declared_default: str | None = None) -> None:
    """Say the run worked and how to start using it.

    ``profile_used``/``n_modes`` let the closing say the presets voice one
    sound mode of several (round 5: the pick was explained at the top, but
    the closing never said the other modes exist or that this run built
    only this one — --all-profiles is the answer, and it was never
    mentioned anywhere a user reads). ``default_unknown`` adds the guess
    caveat to that line (round 6: the caveat lived only at the top banner,
    which a Done-stopper never rereads). ``autogain_off`` adds the one
    guaranteed audible difference from Windows — the tuning's leveler
    shipping off — for the same reason: it never reached the last screen
    (round 6).

    The run reports each file as it writes it, hundreds of lines before the
    end, and then closed on troubleshooting advice for problems the user
    hasn't had yet — so the last screen never confirmed success and never
    said what to do with any of it. Someone running this once has no idea
    that a preset is a thing you go and select in EasyEffects.

    Silent under --autoload, which already wired the preset to the speakers
    and printed its own confirmation: repeating "go and select it" there
    would be wrong.
    """
    if not preset_names or autoloaded:
        return
    # One wording for both branches. The swell/duck caveat rides the
    # suggestion (round 7): alone on the last screen, "add --enable
    # autogain" read as a no-downside fix while its known side effect sat
    # scrolled away. Same risk phrasing as the leveler family everywhere.
    autogain_note = ("  Likely quieter than on Windows: your tuning's "
                     "volume leveler ships off here — --enable autogain "
                     "turns it on (may make quiet passages swell then "
                     "duck).")
    # The mismatch echo mirrors the autogain-note pattern (round 10): the
    # most actionable fix in the run lived only at the top and in the ask
    # small-print, never on the screen people act from.
    mismatch_note = None
    if (declared_default and profile_used
            and declared_default != profile_used):
        mismatch_note = (f"  Windows ships this device on "
                         f"'{declared_default}'; these voice "
                         f"'{profile_used}' — --profile "
                         f"{declared_default} rebuilds.")
    # Derived from what was actually built (round 7, user catch): a
    # tuning lacking a voicing curve skips that preset, so the hint must
    # not describe a preset that doesn't exist.
    hints = []
    if any(n.endswith("-Detailed") for n in preset_names):
        hints.append("Detailed is brighter")
    if any(n.endswith("-Warm") for n in preset_names):
        hints.append("Warm softer")
    voicing_hint = f" ({', '.join(hints)})" if hints else ""
    console.cprint("head", f"\n{'=' * 60}")
    if dry_run:
        # cta, not ok: green is this run's "check passed, nothing to do"
        # color, and the one line that still demands a re-run read as "all
        # done" in the same green (round-2 color finding).
        console.cprint("cta", f"Dry run — nothing was written. Re-run without "
                      f"--dry-run to install these {len(preset_names)} presets:")
        # One comma-separated line, not one name per line (round 7): the
        # vertical list ate the last screen's budget.
        console._cprint_wrapped("dim", "    " + ", ".join(preset_names),
                        indent="    ")
        # One clause on what installing gets them: a dry-run reader asked
        # "do I hear the change after re-running, or is there another step?"
        # and had nothing to go on until the real run printed its answer.
        # Only reached without --autoload (the early return above owns that
        # case), so "pick one yourself" is true here — and naming --autoload
        # gives the reader the self-loading default before the re-run, not
        # after it.
        console._cprint_wrapped("dim", "  You'll then pick one in EasyEffects — "
                               f"start with {preset_names[0]}"
                               f"{voicing_hint}; the real run "
                               "prints the exact steps. (Or add --autoload "
                               "and it loads itself for your speakers.)",
                        indent="  ")
        if profile_used and n_modes > 1:
            caveat = (" (we assume it is your Windows default)"
                      if default_unknown else "")
            console._cprint_wrapped("dim", f"  These voice the '{profile_used}' "
                                   f"sound mode only{caveat} — "
                                   "--all-profiles builds every mode.",
                            indent="  ")
        if mismatch_note:
            console._cprint_wrapped("dim", mismatch_note, indent="  ")
        if autogain_off:
            console._cprint_wrapped("dim", autogain_note, indent="  ")
        return
    # "starting in": each preset is two files and only the .json lands in
    # output_dir — the .irs impulse response goes to --irs-dir, a different
    # directory by default. "wrote N presets to <dir>" named half of what
    # the run had just listed above.
    console.cprint("ok", f"Done — wrote {len(preset_names)} presets"
                 + (f", starting in {doctor.tilde(output_dir)}:"
                    if output_dir else ":"))
    # Name them all — naming only the first left the reader wondering what
    # the other two were — but on one comma-separated line (round 7): the
    # vertical list ate the last screen's budget. No blank after (round
    # 10, user-picked): the closing had grown exactly one line past a
    # 26-line window, scrolling the green "Done" off the last screen.
    console._cprint_wrapped("dim", "    " + ", ".join(preset_names), indent="    ")
    # "Brighter"/"softer" measured against ieq_balanced on the corpus
    # curves (Dolby-global): detailed ≈ +4 dB treble, warm ≈ −2.5 dB
    # treble. Round 5: the closing named a starting preset but never said
    # what the other two are for, so nobody would try them.
    console._cprint_wrapped("dim", "  To use them: open EasyEffects, go to Output, and "
                           f"pick '{preset_names[0]}' from the Presets menu — "
                           f"that's the one to start with{voicing_hint}. "
                           "Or re-run with "
                           "--autoload to have it load itself for your "
                           "speakers.", indent="  ")
    if profile_used and n_modes > 1:
        caveat = (" (we assume it is your Windows default)"
                  if default_unknown else "")
        console._cprint_wrapped("dim", f"  These voice the '{profile_used}' sound "
                               f"mode only{caveat} — --all-profiles builds "
                               "every mode.", indent="  ")
    if mismatch_note:
        console._cprint_wrapped("dim", mismatch_note, indent="  ")
    if autogain_off:
        console._cprint_wrapped("dim", autogain_note, indent="  ")
    # The one-line map back to the menu (round 7): with the Done block
    # grown, the symptom→flag menu scrolls off a 26-line screen and the
    # reader said they'd never think to scroll. The pointer puts the
    # menu's existence on the last screen without re-breaking the round-3
    # order (success last, not troubleshooting).
    # "(re-running ... reprints it)": scrollback is gone once the terminal
    # closes, and the pointer alone was a dead end then (round 9).
    if menu_printed:
        console._cprint_wrapped("dim", "  Something sound off later? Scroll up to "
                               "\"If something doesn't sound right\" "
                               "(re-running this command reprints it).",
                        indent="  ")


# Width of the "    --disable volmax      " gutter each flag row hangs from,
# so a wrapped symptom lines up under the text it continues rather than under
# the flag.
_FLAG_GUTTER = 30


def _print_flag_hint(flag: str, comment: str, effect: str = "") -> None:
    """One row of a flag menu: the flag, its symptom, optionally its effect.

    Wrapped explicitly, because cprint hands text to the console verbatim so
    that URLs survive — which means anything long enough to need folding has
    to ask for it.
    """
    gutter = " " * _FLAG_GUTTER
    # Continuations indent two past the gutter so they land under the
    # comment text, not under its "#" — flush with the marker they read as
    # stray fragments (round 2).
    # Plain, not dim (round 5): fully dimmed rows read as less important
    # than the report asks below — these are the fix a user with bad audio
    # needs. Plain keeps them a step below the bold asks, which stay the
    # block's emphasis (user decision).
    console._cprint_wrapped("", f"    {flag:<{_FLAG_GUTTER - 4}}{comment}",
                    indent=gutter + "  ")
    if effect:
        console._cprint_wrapped("", f"{gutter}({effect})", indent=gutter + " ")


def print_troubleshooting(findings: list[Finding],
                          filters_by_profile: dict[str, set[str]],
                          installs_presets: bool = True,
                          enabled_by_flag: frozenset[str] = frozenset(),
                          dry_run: bool = False) -> bool:
    """Print what the user can do about their own audio, most specific first.

    Someone with a symptom scans until something matches and stops reading, so
    the findings this run actually raised come before the generic menu — and a
    hint that says "re-run with --disable volmax" turns that menu into context
    rather than arriving as a repeat of it.

    The menu is the longest, least targeted block in the tail, so it is one
    line per filter: the symptom is what someone picks a flag by, and the
    effect clause ("drops the per-band limiter") restates what the flag name
    already says. It used to carry that clause plus a per-profile scope note
    and shrink only once a hint had named a flag — two renderings of one menu,
    for a reason no single user could see, since each one sees one run.

    The symptom text stays rather than deferring to --help: --help lists the
    valid names and two examples, not the per-filter symptom, so pointing at
    it would be a claim that isn't true.
    """
    hints = [f for f in findings if f.kind == "hint" and f.ask]
    # A stage the user switched on with --enable never gets a --disable row:
    # both flags at once is a hard error, and the undo for a flag you typed
    # is removing it, not stacking its opposite. Only a stage active by the
    # device's own default (the leveler on SoundWire) is offered here.
    shown = [k for k in DISABLEABLE_FILTERS
             if _DISABLE_MENU_MARKER.get(k, k) in filters_by_profile
             and k not in enabled_by_flag]
    # Don't offer to switch off a stage this run already reported as never
    # engaging: "the volume wobbles on its own — --disable regulator" under a
    # warning that the regulator never does anything is a straight
    # contradiction, and the reader can't tell which half to believe.
    if any(f.slug == "loudness-untamed" for f in hints):
        shown = [k for k in shown if k != "regulator"]
    enable_hints = [k for k in ENABLEABLE_FILTERS if k in filters_by_profile]
    if not hints and not shown and not enable_hints:
        # Returns whether the menu printed, so the closing's scroll-up
        # pointer never points at a menu that isn't there.
        return False

    console.cprint("head", f"\n{'=' * 60}")
    console.cprint("head", "If something doesn't sound right")
    if hints:
        print()
        for finding in hints:
            _print_ask("warn", finding)

    # The menu lists every filter this run emitted, including any a hint above
    # already named. Omitting those looked tidier and read as a bug: a hint
    # says "re-run with --disable volmax" and the list of valid filters right
    # under it doesn't contain volmax, so the reader concludes one of the two
    # is stale and trusts neither.
    # Both autogain rows point at [leveler-gap] when that note fired: the
    # note names --disable autogain as the off-switch, and round 4 found
    # the pointer on the --enable row (leveler off by default) but missing
    # from the --disable row (leveler running), where the note is live.
    gap = any(f.slug == "leveler-gap" for f in findings)
    if shown:
        print()
        # Opens on the condition, so the list reads as "only if you hear it"
        # rather than as a to-do for a preset nobody has heard yet — on a
        # clean device this is the first thing under the heading.
        console._cprint_wrapped("dim", "  If anything sounds off on your hardware, you "
                               "can rebuild without specific filters:",
                        indent="  ")
        for name in shown:
            symptom, _effect = DISABLEABLE_FILTERS[name]
            comment = f"# {symptom}"
            if name == "autogain" and gap:
                comment += " — see [leveler-gap]"
            _print_flag_hint(f"--disable {name}", comment)

    # Same one-line shape as the --disable menu above, with the caveat folded
    # into the same line rather than hanging under it. "Shipped present but
    # inactive" was the old heading and could not be parsed cold — it names an
    # internal state (the stage is in the preset, bypassed) rather than
    # anything the reader can act on.
    if enable_hints:
        print()
        console.cprint("dim", "  Optional extras, switched off by default:")
        # On a device whose tuning pairs the leveler with sub-stages we can't
        # reproduce, --enable autogain is the switch that turns them on. The
        # run says so in the leveler-gap note far above; the menu offered the
        # flag with no hint of it, so the two never met.
        for name in enable_hints:
            symptom, caveat = ENABLEABLE_FILTERS[name]
            if name == "autogain" and gap:
                # The flag cannot enable a stage the preset never contains.
                # What it does is run our leveler without the companion
                # compression Dolby pairs with it — which is what the inline
                # [leveler-gap] note says, and what this row said backwards.
                caveat = ("on this device it runs without the companion "
                          "stage we can't reproduce, so quiet passages may "
                          "swell then duck — see [leveler-gap]")
            _print_flag_hint(f"--enable {name}", f"# {symptom} — {caveat}")

    # How to actually apply any of the above. Every suggestion here is a flag
    # on a re-run, and the output never said what to re-run, that flags can be
    # combined, or that EasyEffects keeps serving the old preset until it is
    # reloaded — so a rebuild that silently didn't take effect reads as "the
    # flag didn't help".
    if shown or enable_hints:
        print()
        # Only mention reloading in EasyEffects when this run is the thing
        # that put a preset there. Under dolby_to_pipewire.py these presets
        # are staged and thrown away, and the reader picked that path
        # precisely because they don't run EasyEffects — so the sentence that
        # tells them how to apply a fix ended in something they can't do. The
        # wrapper's own [3/3] steps cover applying it there.
        tail = (" Then reload the preset in EasyEffects to hear the change."
                if installs_presets else "")
        # Under --dry-run, "the same command you ran" would rebuild nothing —
        # the reader is four lines from being told nothing was written, and
        # telling them to reload a preset that doesn't exist read as the two
        # blocks not knowing about each other.
        # "the flags above", not "these": on a terminal whose window folds
        # exactly at this sentence, "these" is the first visible word of the
        # last screen with its antecedent scrolled off (round 4). Naming the
        # referent keeps the sentence whole at any fold.
        lead = ("Add any of the flags above when you re-run without "
                "--dry-run"
                if dry_run else
                "Add any of the flags above to the same command you ran")
        console._cprint_wrapped("dim", f"  {lead}; they combine.{tail}", indent="  ")
    return True
