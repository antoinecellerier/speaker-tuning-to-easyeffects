"""What a run tells the user about one parsed profile.

Between the parse and the build sits a screen and a half of prose: the voicing
strength, the speaker-correction curve reduced to its deepest cut and boost,
the PEQ rows, the dialog/surround/leveler/compressor/regulator stages and the
loudness boost — each one glossed in what a listener would hear rather than in
what the stage is called. `.claude/rules/user-messages.md` is the contract for
that copy, and a dozen review rounds are recorded in the comments beside it.

Side-effect-free apart from stdout, and it returns the findings it raised
rather than printing their asks: each finding's technical half prints here, in
place, next to the table or value that explains it, and `main()` renders the
one-line asks at the end where the user still has them on screen.

It is a module of its own and not more of `lib/report/messages.py` because it
reaches the DSP stack — numpy for the audio-optimizer summary,
`lib/preset/fir.py` for the sample rate the compressor crossovers print
against, `lib/preset/plugins.py` for the decoders whose answers it reports.
That is also why `dolby_to_easyeffects.py` imports it inside `main()`, beside
`emit`, rather than at the top of the file: numpy and scipy are ~0.35 s of the
generator's ~0.5 s startup, so anything reaching them has to stay off the
import path and out of every early return — `--version`, `--list`, `--doctor`,
`--speaker-info`, an argparse error, and a tab completion, which argcomplete
re-runs the whole script for on every TAB press
(`tests/test_layout.py::test_the_dsp_import_is_deferred_past_every_early_return`).

`findings` keeps the generator's alias (`report_findings`).
`_print_finding_detail` and `Finding` arrive as bare names because neither
holds state a patch would have to reach: a frozen record, and a printer that
reads `_TAG_CONVENTION_SHOWN` out of its own module globals at call time.
`VOICING_CURVES` comes from `messages` for the reason
recorded there: its other readers are `lib/preset/emit.py`, in a package this
one may not import, and `dolby_to_pipewire.py`'s `--variant` choices, in a
root script.
"""

from __future__ import annotations

import numpy as np

from lib import console
from lib.dax import parse
from lib.preset import fir, plugins
from lib.report import findings as report_findings
from lib.report import messages
from lib.report.findings import Finding, _print_finding_detail


def _print_voicing(tuning):
    ieq_amount = tuning.ieq_amount
    # One clause of meaning: this used to print bare ("ieq-amount: 10%
    # (scale: 0.10)") — no heading, nothing tying back to it, and a
    # reviewer couldn't tell whether it mattered.
    # Leads with the plain name (round 3: the bare acronym was the one
    # line still doing it) and ties the three preset files to the profile
    # they voice — reviewers read them as unrelated flavors.
    # "of full strength" anchors the percentage's scale — a bare "10%"
    # gave no way to tell strong from weak (round 6). "Differ in shape,
    # not strength": one number over three differently-described presets
    # left a round-7 reviewer unsure whether it covered all three. No
    # is-this-typical cue — no corpus stat backs one.
    # The list is derived, not hardcoded (round 7, user catch): the emit
    # loop skips any voicing whose ieq_* curve the XML lacks, so the
    # summary must not promise three when fewer will build.
    voicings = [label for label, key in messages.VOICING_CURVES.items()
                if key in tuning.curves]
    if voicings:
        n_voc = ("three" if len(voicings) == 3
                 else str(len(voicings)) if len(voicings) > 1 else "one")
        names = "/".join(voicings)
        plural = "s" if len(voicings) > 1 else ""
        if tuning.ieq_enabled:
            console._cprint_wrapped("", f"Voicing strength (ieq-amount): {ieq_amount}% "
                                f"of full strength — this profile's {n_voc} "
                                f"voicing{plural} ({names}) "
                                + ("all apply" if len(voicings) > 1
                                   else "applies")
                                + " at this strength on top of the speaker "
                                "correction"
                                + ("; they differ in shape, not strength"
                                   if len(voicings) > 1 else ""), indent="  ")
        else:
            # With <ieq-enable> at 0 — about 45% of dynamic-profile corpus
            # rows — the tuning states no strength and Dolby engages none,
            # while ieq_amount still holds our assumed 10 and the build
            # applies it (scale = ieq_amount/100, unconditional). Stating the
            # percentage first and "Windows applies none" after read as a
            # contradiction to two reviewers, so the fact leads and the
            # number arrives as ours. What it buys is worth saying: that
            # scale multiplies the per-voicing curve and nothing else varies
            # between the three presets, so at 0 they would be one file.
            console._cprint_wrapped("", "Voicing strength (ieq-amount): this profile "
                                "switches the voicing off, so Windows applies "
                                f"none. We use {ieq_amount}% of full strength "
                                "instead — without it "
                                + (f"the {n_voc} voicings ({names}) would be "
                                   "identical; they differ in shape, not "
                                   "strength" if len(voicings) > 1 else
                                   f"the {names} voicing would add nothing to "
                                   "the speaker correction"), indent="  ")


def _print_audio_optimizer(tuning, ao_db_left, ao_db_right, verbose):
    freqs = tuning.freqs
    # Audio-optimizer: one triage-grade line by default — deepest cut/boost
    # with its frequency, and channel symmetry, which is what a pasted
    # normal-verbosity report gets read for first. The raw twenty-number
    # arrays read as "my sound is about to be damaged" (round 3, two
    # reviewers) and move behind -v.
    ao_l, ao_r = np.asarray(ao_db_left), np.asarray(ao_db_right)
    if not tuning.ao_enabled:
        print("\nAudio-optimizer: switched off in this profile")
        console.cprint("warn", "  audio-optimizer-enable=0 — the correction curve "
                       "this profile ships is not applied; only the IEQ "
                       "voicing reaches the convolver here.")
    else:
        parts = []
        cut = float(min(ao_l.min(), ao_r.min()))
        boost = float(max(ao_l.max(), ao_r.max()))

        # A register word beside each Hz value: the numbers alone don't say
        # whether the deepest cut lands in bass or treble (round 6), and
        # that is the one thing a listener can check by ear.
        def register(f):
            return ("bass" if f < 250
                    else "midrange" if f <= 4000 else "treble")

        if cut < 0:
            f_cut = freqs[int(np.argmin(np.minimum(ao_l, ao_r)))]
            parts.append(f"cuts to {cut:+.1f} dB (deepest at {f_cut} Hz, "
                         f"{register(f_cut)})")
        if boost > 0:
            f_boost = freqs[int(np.argmax(np.maximum(ao_l, ao_r)))]
            parts.append(f"boosts to {boost:+.1f} dB (at {f_boost} Hz, "
                         f"{register(f_boost)})")
        if not parts:
            parts.append("flat (all 0 dB)")
        # "(normal ...)": two round-9 reviewers read asymmetric correction
        # as a possible fault in their hardware.
        sym = ("same correction for left and right"
               if np.allclose(ao_l, ao_r)
               else "left and right corrected differently (normal — each "
                    "speaker gets its own correction)")
        # Friendly name first, like every other header (round 9).
        print("\nSpeaker correction (audio-optimizer): "
              + ", ".join(parts) + f", {sym}")
    if verbose:
        print(f"  Left:  {[f'{x:+.1f}' for x in ao_db_left]}")
        print(f"  Right: {[f'{x:+.1f}' for x in ao_db_right]}")


def _print_peq(tuning, verbose):
    peq_filters = tuning.peq_filters
    # The row types carry a what-you-hear clause where the name alone says
    # nothing to a non-engineer — the dialog/bass sections had one and this
    # section didn't, which read as "am I supposed to understand this?".
    # Every type gets one (round 3: the glossed and bare rows side by side
    # read worse than all-bare). Header only when there are rows — over
    # nothing it read as a failed section.
    #
    # Most tunings configure L and R identically; printing both channels
    # doubled every row for no information (round 5). When the two channel
    # configurations match and -v is off, each filter prints once. Any L/R
    # difference keeps per-channel rows — the difference is itself the
    # detail worth reading — but the filter-design internals (order, S, Q)
    # are -v-only in every view: an unglossed S=1.0 on a default row was
    # the round-6 nit (freq and gain, the audible knobs, stay).
    def _peq_spec(pf):
        return {k: v for k, v in pf.items() if k != "speaker"}

    left_specs = [_peq_spec(p) for p in peq_filters if p["speaker"] == 0]
    right_specs = [_peq_spec(p) for p in peq_filters if p["speaker"] == 1]
    condensed = not verbose and left_specs == right_specs
    if peq_filters:
        # Plain name leads, acronym trails (round 8) — this header was the
        # one still leading with the acronym; "kept as parametric EQ" was
        # near-tautological next to "EQ filters" and goes.
        print("\nSpeaker EQ filters (PEQ"
              + ("; same for both speakers):  (details with -v)"
                 if condensed else "):"))
    for pf in (peq_filters if not condensed
               else [p for p in peq_filters if p["speaker"] == 0]):
        spk = "" if condensed else ("[L] " if pf["speaker"] == 0 else "[R] ")
        if pf["type"] in (7, 9):
            # Says there is no knob: "bass sounds thin" is the one symptom
            # with no flag in the menu (deliberately — this filter protects
            # the driver), and a round-4 reviewer went hunting for one and
            # settled on --disable bass-enhancer, a different symptom.
            tech = (f", order {pf['order']} ({pf['order'] * 6} dB/oct)"
                    if verbose else "")
            print(f"  {spk}HP @ {pf['f0']} Hz{tech} — cuts bass the speaker can't play (speaker protection; no flag turns it off)")
        elif pf["type"] in (6, 8):
            tech = (f", order {pf['order']} ({pf['order'] * 6} dB/oct)"
                    if verbose else "")
            print(f"  {spk}Lo-pass @ {pf['f0']} Hz{tech} — rolls off the top end  [unconfirmed-by-ear]")
        elif pf["type"] == 4:
            tech = f", S={pf['s']}" if verbose else ""
            print(f"  {spk}Lo-shelf @ {pf['f0']} Hz, {pf['gain']:+.1f} dB{tech} — shapes the low end")
        elif pf["type"] == 3:
            # "High-shelf" in display copy — matching --disable high-shelf;
            # the LSP mode string stays "Hi-shelf" (emitted parameter).
            tech = f", S={pf['s']}" if verbose else ""
            print(f"  {spk}High-shelf @ {pf['f0']} Hz, {pf['gain']:+.1f} dB{tech} — shapes the treble  [unconfirmed-by-ear]")
        elif pf["type"] == 1:
            tech = f", Q={pf['q']}" if verbose else ""
            # "lifts or trims", not "evens out": the same line prints for
            # positive-gain bells, which add a narrow band rather than
            # levelling one.
            print(f"  {spk}Bell @ {pf['f0']} Hz, {pf['gain']:+.1f} dB{tech} — lifts or trims a narrow band")


def _print_bass_enhancer(tuning, disabled, is_soundwire):
    peq_filters = tuning.peq_filters
    if is_soundwire and "bass-enhancer" not in disabled:
        # Converter-added, not XML-derived: SoundWire tunings rely on Dolby's
        # in-driver Virtual Bass Enhancement, which has no XML parameters to
        # translate. It was the one active stage the run never mentioned —
        # so the --disable menu offered to drop something the reader had
        # never heard of (user-review round 1).
        be = plugins.bass_enhancer_from_peq(peq_filters)
        # "Separate from" only when the [speaker-optimizer] note fired this
        # run: a round-4 reviewer couldn't tell this boost and that
        # dropped protection stage apart ("is my bass protected or not?"),
        # but either message can appear without the other, so the clause
        # must not dangle on runs where the note never printed. Named
        # outright (round 5): "the bass-protection stage noted above" was
        # ambiguous against the HP rows' "speaker protection" clause.
        sep = (" (separate from the Dynamic Speaker Optimization stage "
               "noted above)"
               if any(f.slug == "speaker-optimizer" for f in tuning.findings)
               else "")
        # Says where the Hz figure comes from (round 7): a number in the
        # same sentence as "no settings in the XML" read as pulled from
        # nowhere. The scope derives from the PEQ high-pass corner
        # (min(2*hp, 300) — see make_bass_enhancer).
        print()
        # Two corrections to one sentence:
        # - the scope is only device-derived when the tuning ships a PEQ
        #   high-pass. Most SoundWire tunings carry no PEQ at all, so the
        #   200 Hz that prints is 2x the 100 Hz fallback — a constant the
        #   old wording credited to "this speaker's bass cutoff".
        # - the settings are IN the XML: bass-enhancer-enable/-boost/
        #   -cutoff-frequency/-width are present on every corpus row, all
        #   frozen (enable 0). What is missing is a tuning to copy, not the
        #   fields. The +dB is our own choice either way.
        scope_why = ("sized from this speaker's bass cutoff"
                     if plugins.bass_enhancer_scope_is_derived(peq_filters) else
                     "our default range — your tuning sets no bass cutoff")
        console._cprint_wrapped("", f"Bass enhancer: +{be['amount']:.1f} dB "
                            f"harmonics below {be['scope']:.0f} Hz "
                            f"({scope_why}) — our own stand-in for Dolby's "
                            "in-driver bass enhancement, which every tuning "
                            f"we've seen ships switched off{sep}",
                        indent="  ")


def _print_dialog(tuning, disabled):
    dialog_enhancer = tuning.dialog_enhancer
    if dialog_enhancer:
        # dB first: "amount=5" has no knowable scale (it's a raw schema
        # value), so the derived boost leads and the raw stays as the
        # report handle.
        gain = dialog_enhancer["amount"] / parse.DB_FIXED_POINT_SCALE * 6.0
        raw = f"amount {dialog_enhancer['amount']} of 16 in your tuning"
        if "dialog" in disabled:
            # Same shape as the volmax line: a stage the flag dropped says
            # so, instead of describing itself as if it shipped.
            print(f"\nDialog enhancer: {raw} — dropped by --disable dialog")
        else:
            # "about", and "where speech sits" rather than "speech boost":
            # the 6 dB ceiling behind the figure is on the unvalidated list
            # (reference.md "Validated vs unvalidated mappings"), and ours
            # is a static bell — Dolby's is speech-gated, so it lifts that
            # band on everything, not only on dialogue.
            print(f"\nDialog enhancer: about +{gain:.1f} dB around 2.5 kHz, "
                  f"where speech sits ({raw})")


def _print_surround(tuning):
    surround = tuning.surround
    if surround:
        # No "virtualizer" in ANY form here — noun or verb: with the
        # [virtualizer] finding on the same screen, two features sharing
        # the word read as one feature with contradictory verdicts (rounds
        # 2-4; round 3 dropped the noun, the surviving "virtualizing" still
        # read as the contradiction). And no doc citation — three rounds of
        # reviewers called it unfollowable dev-talk on a line whose inline
        # reason stands alone.
        # Verdict first (round 7): leading with the dB figure made the
        # boost read as active for a beat before "skipped" landed.
        #
        # Says what was measured, not what Dolby intends. A DAX capture
        # found surround-boost=96 and =0 identical on 2-channel content
        # (0.01 dB S/M); that the boost applies to *surround* content is
        # the leading hypothesis in design-notes, never captured — no
        # multichannel capture exists. And with the tuning at 0 dB there is
        # nothing to skip, so that case says so instead.
        if surround["boost"] == 0:
            print("\nSurround (multi-channel) rendering boost: your tuning "
                  "sets none, so there is nothing to carry over")
        else:
            print("\nSurround (multi-channel) rendering boost: skipped on "
                  f"purpose — your tuning sets {surround['boost']:.1f} dB, "
                  "but we measured no difference it makes to ordinary "
                  "stereo playback")


def _print_leveler(tuning, disabled, enabled, is_soundwire):
    vol_leveler = tuning.vol_leveler
    if vol_leveler:
        # Says BOTH states — the tuning file's and this preset's — and
        # names the flag that flips it. The label leads with "Autogain"
        # because that is the flag word: a round-4 reviewer got "Volume
        # leveler" from this line and then couldn't find that word anywhere
        # in the flag menus. Each state clause gives the two worlds their
        # own subjects ("your tuning … this preset") — the compressed
        # "enabled — ships switched off" read as the line contradicting
        # itself (rounds 3 and 4).
        enabled_flags = enabled or set()
        if not vol_leveler["enable"]:
            state = "switched off in your tuning"
        elif "autogain" in disabled:
            state = ("on in your tuning — removed from this preset by "
                     "--disable autogain")
        elif "autogain" in enabled_flags:
            state = ("on in your tuning — running in this preset (you "
                     "passed --enable autogain)")
        elif is_soundwire:
            state = ("on in your tuning — running in this preset "
                     "(--disable autogain switches it off)")
        else:
            # Carries its why (round 6): the override of the tuning's own
            # setting was only explained 48 lines later in the flag menu.
            # Same risk phrasing as the menu row — the leveler family's
            # one wording.
            state = ("on in your tuning, but this preset ships with it "
                     "off — it can make quiet passages swell then duck "
                     "(issue #25); add --enable autogain to turn it on")
        print(f"\nAutogain (volume leveler): {state}")
        # Settings only when the stage actually runs in this preset: on a
        # shipped-off build the targets are numbers the reader can't tie
        # to anything they'll hear (round 5).
        running = (vol_leveler["enable"] and "autogain" not in disabled
                   and ("autogain" in enabled_flags or is_soundwire))
        if running:
            # These are the tuning's numbers, and the built stage is not
            # identical to them: the SoundWire path takes 6 dB off the target
            # for headroom (make_autogain, conservative=True), so printing
            # them unlabelled reported a target the preset does not use.
            print(f"  your tuning: amount {vol_leveler['amount']}, targets "
                  f"{vol_leveler['in_target']:.1f} dB in / "
                  f"{vol_leveler['out_target']:.1f} dB out"
                  + ("  (this preset aims 6 dB lower, for headroom)"
                     if is_soundwire else ""))


def _print_mbc(tuning, disabled, verbose):
    mb_comp = tuning.mb_comp
    freqs = tuning.freqs
    if mb_comp and "mbc" in disabled:
        # A dropped stage says so instead of describing itself, the shape
        # the volmax and leveler lines already use.
        print(f"\nMulti-band compressor (mbc): {mb_comp['group_count']} "
              "frequency band(s) in your tuning — dropped by --disable mbc")
    elif mb_comp:
        tag = "  [unconfirmed-by-ear]" if mb_comp["group_count"] == 1 else ""
        # "on loud content": measured dormant on the -10 dBFS stimuli and
        # only waking near -2 dBFS (design-notes, unvalidated-scaling entry
        # 6), so the bare present tense described a stage that mostly isn't
        # doing anything.
        print(f"\nMulti-band compressor (mbc): {mb_comp['group_count']} "
              "frequency band(s) — on loud content, evens out loud vs quiet "
              f"separately per frequency range{tag}")
        # Read-only, like regulator-overdrive and -relaxation: the field is
        # parsed and shown as a report handle but drives no emitted
        # parameter, so "the level it evens toward" credited the preset
        # with behaviour it does not have.
        print(f"  target-power-level: {mb_comp['target_power']:.1f} dB "
              "(read from your tuning; this preset doesn't use it)")
        # Print FROM the single-source decode — no inline re-decode, no
        # warnings (those fire in make_multiband_compressor). xover_hz is a
        # display concern derived here from the stored xover_idx + band
        # position, exactly as before.
        decoded = plugins.decode_mbc_bands(mb_comp)
        # The threshold range is the summary's diagnostic payload: it is
        # the first thing a triage of a squashed-sounding report reaches
        # for, and most reports arrive at normal verbosity.
        thr = [b["threshold"] for b in decoded]
        if len(thr) == 1:
            print(f"  threshold {thr[0]:+.1f} dB (where it kicks in)"
                  + ("" if verbose else "  (full band table with -v)"))
        else:
            print(f"  thresholds {max(thr):+.1f} to {min(thr):+.1f} dB "
                  "(where bands kick in)"
                  + ("" if verbose else "  (full band table with -v)"))
        n_bands_print = len(decoded)
        for i, b in enumerate(decoded if verbose else []):
            xover_idx = b["xover_idx"]
            if i == n_bands_print - 1:
                # Sentinel in the last band — it runs to the top of the
                # range. Printed as a frequency: "Nyquist" was the one word
                # in an otherwise numeric table a reviewer had never seen.
                xover_hz = ("full-band" if n_bands_print == 1
                            else f"{fir.SAMPLE_RATE // 2} Hz (top of range)")
            elif 0 <= xover_idx < len(freqs):
                xover_hz = f"{freqs[xover_idx]} Hz"
            else:
                xover_hz = "?"
            print(f"  band {i}: xover={xover_hz}, thresh={b['threshold']:+.1f} dB, "
                  f"ratio={b['ratio']:.2f}:1, attack={b['attack_ms']:.2f} ms, "
                  f"release={b['release_ms']:.2f} ms, makeup={b['makeup']:+.1f} dB")


def _print_regulator(tuning, disabled, verbose):
    regulator = tuning.regulator
    if regulator and "regulator" in disabled:
        # Dropped stages say so rather than describing themselves; without
        # this the whole section — protective gloss, band counts and the
        # coupled-bands offer — described a limiter the preset doesn't have.
        print("\nRegulator (per-band limiter): in your tuning — dropped by "
              "--disable regulator")
    elif regulator:
        # Plain tail + a triage-grade summary (how many bands limit, and
        # how hard) — the raw arrays were six unexplained lines of numbers
        # (round 3, all three reviewers) and move behind -v. The active-band
        # count and floor are what a report diagnosis reads first.
        th = regulator["threshold_high"]
        active = [x for x in th if x < 0]
        iso = regulator.get("isolated_band")
        # How many raw bands the coupled-bands mapping actually put a limit
        # on. Computed before the headline because the headline depends on
        # it: on a tuning that limits nothing itself, "configured never to
        # engage" stops being true once these zones go live, and the two
        # lines would contradict each other on the same screen.
        coupled = 0
        if "coupled-bands" not in disabled and plugins._coupled_bands_eligible(
                regulator):
            coupled = sum(
                end - start + 1
                for start, end, threshold in plugins._regulator_zones(th)
                if threshold >= 0 and all(iso[k] == 0
                                          for k in range(start, end + 1)))
        # "Steps in only when": distinguishes it from the always-shaping
        # multi-band compressor two sections up, whose gloss otherwise
        # read as the same job (round 5). The inert case leads with the
        # fact instead (round 9, user-picked rendering): the protective
        # gloss followed by "it never engages" read as reassurance
        # retracted in the same breath.
        if active:
            # "at the level this tuning sets", not "when loud parts would
            # distort": the engagement point is whatever threshold_high the
            # tuning carries, which is not a distortion point, and the
            # realised curve is measured well short of the configured limit
            # (design-notes, unvalidated-scaling entry 11).
            print("\nRegulator (per-band limiter): a protective ceiling, "
                  "band by band — steps in on loud content, at the level "
                  "this tuning sets")
            # "your tuning limits": the count is of raw XML bands, while
            # make_regulator merges them into <=8 zones keeping the highest
            # threshold, so some counted bands are not separately limited in
            # the preset. Attributing the count to the tuning keeps it true.
            print(f"  your tuning limits {len(active)} of {len(th)} frequency "
                  f"bands (deepest {min(th):+.1f} dB)"
                  + ("" if verbose else "  (full tables with -v)"))
        elif coupled:
            # The tuning sets no limits of its own, yet the regulator does
            # run: every band sits at full scale and the coupled mapping
            # takes that at face value. Saying "configured never to engage"
            # here — the wording the no-coupling case still uses — would be
            # false, and sat one line above "Added a limit to 20 more
            # bands" (round 12).
            print()
            # "we have it step in", not "it steps in": the coupled-bands
            # mapping is our unvalidated reading of isolated_band
            # (reference.md), so the sentence must not present it as
            # something the tuning asked for.
            console._cprint_wrapped("", "Regulator (per-band limiter): your "
                                "tuning sets no limit of its own, so we "
                                "have it step in only where a band would "
                                "reach full volume"
                                + ("" if verbose
                                   else "  (full tables with -v)"),
                            indent="  ")
        else:
            print()
            console._cprint_wrapped("", "Regulator (per-band limiter): configured "
                                "never to engage on this tuning — every "
                                "band's limit sits at or above full volume"
                                + ("" if verbose
                                   else "  (full tables with -v)"),
                            indent="  ")
        # Gated on the same eligibility test the flag menu and the -active
        # marker use, not on the field merely being present: where every
        # unlimited band is also marked isolated the mapping adds nothing,
        # and this line would describe a stage the run did not build.
        # And on `disabled` too, for the same reason in the other direction:
        # --disable coupled-bands drops those zones, so claiming we added
        # them is the exact "stage keeps describing itself after you
        # switched it off" bug the section trap exists to catch.
        if coupled:
            # Co-located with the fact it explains: the only plain wording
            # for coupled-bands used to sit a screen away in the flag menu
            # (rounds 2–3). Still no isolated_band count (round 7, user
            # decision): "marks N of 20 isolated (limited on their own)"
            # over-claimed a field whose semantics are still open, and read
            # as contradicting the "limits N bands" line above whenever the
            # counts differed. The raw array stays under -v.
            # The count below is a different number and safe where that one
            # wasn't: it is how many raw bands the run actually put a limit
            # on, fully determined by the zones make_regulator built, and it
            # reads *with* the "limits N of 20" line rather than against it.
            # It replaces "some of the bands" — two independent first-time
            # readers (round 12) called that out as the one place the tool
            # changes what the tuning asked for while going vague, next to
            # exact figures everywhere else.
            # Not "the other N": bands can stay unlimited here (a zone
            # holding an isolated band is declined), so the two counts need
            # not sum to 20.
            # "Adds a limit to", not "extends limiting to" (round 10): on
            # all-inert tunings — where it does the most — "extends" read as
            # growing existing limits, of which that reader has none.
            # States what the run did rather than offering a flag: the
            # mapping is the default, so the actionable half is the way out
            # of it, which the --disable menu carries.
            console._cprint_wrapped("", f"  Added a limit to {coupled} more "
                                f"band{'' if coupled == 1 else 's'} the tuning "
                                "leaves unlimited "
                                "(--disable coupled-bands drops them)",
                            indent="    ")
        if verbose:
            print(f"  threshold_high (dB): {[f'{x:+.1f}' for x in regulator['threshold_high']]}")
            print(f"  threshold_low (dB):  {[f'{x:+.1f}' for x in regulator['threshold_low']]}")
            print(f"  stress (dB):         {[f'{x:+.1f}' for x in regulator['stress']]}"
                  f"  ({len(regulator['stress'])} zones, not per-band)")
            print(f"  distortion-slope:    {regulator.get('distortion_slope', 1.0):.2f}")
            print(f"  timbre-preservation: {regulator.get('timbre_preservation', 0.75):.2f}")
            print(f"  overdrive (raw):     {regulator.get('overdrive', 0)}  (recorded for research; no effect on your output)")
            print(f"  relaxation (raw):    {regulator.get('relaxation', 96)}  (recorded for research; no effect on your output)")
            if iso is not None:
                print(f"  isolated_band:       {iso}")


def _print_volmax(tuning, disabled, volmax_slot, verbose):
    volmax_boost = tuning.volmax_boost
    regulator = tuning.regulator
    # Glossed like every other stage; the gain-slot detail is -v only.
    # Round-4 review (all three reviewers): the bare "(applied as
    # regulator input-gain)" was the one summary line with no plain
    # meaning, and it implied the boost dies with --disable regulator —
    # the limiter fallback keeps it, so the slot is an implementation
    # detail, not a dependency.
    # "Loudness boost (volmax-boost):" — the friendly-name-first header
    # shape every other section uses; this was the one lowercase raw-flag
    # header left (round 6).
    if volmax_boost == 0:
        print(f"\nLoudness boost (volmax-boost): {volmax_boost:+.1f} dB "
              "(your tuning asks for none)")
    elif volmax_boost < 0:
        # A negative boost is still applied — it goes into the same gain
        # slot as a positive one — so "asks for none" was wrong about the
        # one case where the tuning asks for a cut.
        print(f"\nLoudness boost (volmax-boost): {volmax_boost:+.1f} dB "
              "from your tuning — a cut, not a boost")
    elif "volmax" in disabled:
        print(f"\nLoudness boost (volmax-boost): {volmax_boost:+.1f} dB "
              "in your tuning — dropped by --disable volmax")
    else:
        # Names its own off-switch, like the leveler line does: the menu
        # row says "--disable volmax" and the reader had to spot the
        # substring match to connect the two (round 5).
        if verbose:
            slot = (f"regulator {volmax_slot}"
                    if regulator and "regulator" not in disabled
                    else "limiter input-gain")
            tail = f"(applied as {slot}; --disable volmax turns it off)"
        else:
            tail = "(--disable volmax turns it off)"
        print()
        console._cprint_wrapped("", "Loudness boost (volmax-boost): "
                            f"{volmax_boost:+.1f} dB from your tuning "
                            f"{tail}", indent="  ")


def _boost_findings(tuning, ao_db_left, ao_db_right, disabled, enabled):
    """Raise the two loudness-boost findings, printing each detail in
    place as the surrounding sections do, and return what was raised."""
    volmax_boost = tuning.volmax_boost
    regulator = tuning.regulator
    freqs = tuning.freqs

    findings: list[Finding] = []
    # A band with threshold >= 0 dBFS never triggers, so make_regulator
    # disables it; if every band is like that, the regulator carries the
    # volmax boost but tames nothing — the issue-#23 "per-band compression
    # tames the boost before the brickwall" rationale doesn't apply, and
    # both volmax slots degenerate to the same untamed brickwall feed
    # (issue #27 field report; see design-notes).
    # coupled-bands is on unless switched off, so on an all-inert tuning that
    # qualifies the zone is now limited and the warning would be false. It
    # survives for the two ways a run can still reach the untamed shape:
    # --disable coupled-bands, and a tuning with no qualifying zone.
    coupled_on = ("coupled-bands" not in disabled
                  and plugins._coupled_bands_eligible(regulator))
    if (volmax_boost > 0 and "volmax" not in disabled
            and regulator and "regulator" not in disabled
            and all(t >= 0 for t in regulator["threshold_high"])
            and not coupled_on):
        findings.append(report_findings._loudness_untamed_finding())
        _print_finding_detail(findings[-1])
    # The partial case: the regulator limits *somewhere*, so the warning above
    # stays quiet, yet the band carrying the tuning's largest boost is one of
    # the bands it leaves alone — the boost and the volmax gain on top of it
    # reach the brickwall unprotected. Two ways in, and they need different
    # gates because the drive level differs:
    #
    #  - Default path: the FIR is peak-normalised, so that band leaves the
    #    convolver at 0 dB and reaches the brickwall at exactly volmax_boost
    #    above bypass — the same drive every tuning gets, whatever its peak.
    #    What the peak measures here is spectral contrast, not level, so the
    #    bar stays where it was: the boost reaching this XML's full gain
    #    range. Re-derived 2026-08-04 over 3051 parsed corpus XMLs — 10.6%,
    #    against the all-inert case's 16% (issue #46's T495 is one). Read
    #    that bar honestly: only 172 of those files declare
    #    <geq_maximum_range> at all (30 of the 1661 that reach this branch),
    #    so for almost every device it compares against our assumed +12.0 dB
    #    rather than a rail the tuning stated.
    #  - --enable level-restore: the peak is handed back to the chain, so
    #    the same band now arrives at volmax_boost + peak_db — 15.2 dB above
    #    bypass on issue #50's tuning. That is the flag's own risk, so it
    #    warns whatever the peak's relation to the rail. It reaches 54% of
    #    the tunings that get this far, which would be a nag as a default
    #    but is the point when someone has opted into the boost.
    elif (volmax_boost > 0 and "volmax" not in disabled
            and regulator and "regulator" not in disabled
            and not coupled_on):
        peak_band = max(range(len(ao_db_left)),
                        key=lambda i: max(ao_db_left[i], ao_db_right[i]))
        peak_db = max(ao_db_left[peak_band], ao_db_right[peak_band])
        thresholds = regulator["threshold_high"]
        at_rail = peak_db >= tuning.geq_max_range / parse.DB_FIXED_POINT_SCALE
        restored = "level-restore" in (enabled or set())
        if ((at_rail or restored)
                and peak_band < len(thresholds)
                and thresholds[peak_band] >= 0):
            findings.append(report_findings._boost_unlimited_finding(
                peak_db, freqs[peak_band], restored))
            _print_finding_detail(findings[-1])
    return findings


def _report_parsed_profile(tuning, disabled, volmax_slot="input-gain",
                           enabled=None, is_soundwire=False, verbose=False):
    """Print the human-readable per-profile diagnostics for a parsed tuning
    (audio-optimizer / PEQ / dialog / surround / leveler / MBC / regulator /
    volmax), and return the findings raised while doing so.

    Side-effect-free apart from stdout — split out of main() so the
    orchestration there stays legible. Each finding prints its technical half
    here, in place; main() collects the returned list and renders the one-line
    asks at the end, where a user still has them on screen."""
    findings: list[Finding] = []

    # Audio-optimizer curves in dB, from the XML's 1/16-dB fixed point.
    # Derived here rather than handed in: both readers are in this module,
    # so no caller has to know the unit to call this.
    ao_db_left = np.array(tuning.ao_left) / parse.DB_FIXED_POINT_SCALE
    ao_db_right = np.array(tuning.ao_right) / parse.DB_FIXED_POINT_SCALE

    declared = tuning.default_profile
    if declared and declared != tuning.profile_used:
        findings.append(report_findings._profile_mismatch_finding(declared,
                                                 tuning.profile_used))
        _print_finding_detail(findings[-1])

    _print_voicing(tuning)
    _print_audio_optimizer(tuning, ao_db_left, ao_db_right, verbose)
    _print_peq(tuning, verbose)
    _print_bass_enhancer(tuning, disabled, is_soundwire)
    _print_dialog(tuning, disabled)
    _print_surround(tuning)
    _print_leveler(tuning, disabled, enabled, is_soundwire)
    _print_mbc(tuning, disabled, verbose)
    _print_regulator(tuning, disabled, verbose)
    _print_volmax(tuning, disabled, volmax_slot, verbose)
    findings += _boost_findings(tuning, ao_db_left, ao_db_right,
                                disabled, enabled)
    print()
    return findings
