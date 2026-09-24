# Loudness and limiting: volmax, normalisation, regulator, brickwall and PEQ trim

## Where this stands

[reference.md](../reference.md) covers what the converter emits, and
[ee-to-pipewire.md](../ee-to-pipewire.md) the PipeWire path.

- **`volmax-boost`** rides the regulator's `input-gain` by default, or
  `limiter#0`'s without a regulator. On the PipeWire backend, #23's X13
  reporter found it clean in most cases and loud ([#23](#r-volmax-boost-slot)).
  On #44's deep-threshold tuning, `output-gain` was closest to DAX on loud bass
  and confirmed by ear, on one device ([#44](#r-deep-threshold-bass-loss)).
- **The regulator** under-engages vs DAX on the dev device's shallow thresholds,
  and over-engages on #44's deep ones relative to DAX at the one level both were
  measured ([dynamics](#r-fixed-dynamics-constants)).
- **Peak normalisation** is the ≈−8…−12 dB absolute EE−DAX offset on the dev
  device at nominal level (pink, −17.8 dBFS). `--enable level-restore`,
  opt-in, gives it back; on the one device heard, loud speech produced audible
  artifacts ([level restore](#r-level-restore)).

Open:

- The volmax slot default needs a second device. The dev device can serve,
  with a DAX and EE burst capture whose tones sit inside its regulator's active
  zone. Whether an XML-derivable predictor could pick the slot per tuning stays
  a hypothesis ([bass loss](#r-deep-threshold-bass-loss)).
- Why the regulator's configured 100:1 realizes as ≈1.8 on the dev device needs
  a regulator-only EE capture. A DAX capture of `stimulus_stepped_loud` would
  validate or kill the coupled-bands mapping
  ([dynamics](#r-fixed-dynamics-constants)).
- The regulator's 1 ms attack against DAX's ~100–150 ms settle, one device's
  number, waits on a second DAX attack curve
  ([dynamics](#r-fixed-dynamics-constants)).
- `regulator-stress-amount` as an engagement modifier is untested
  ([stress](#r-regulator-stress-amount)).
- The slope→ratio and timbre→knee mappings need a device with other values. The
  PEQ trim's `min(1, 2/Q)` shape needs a wide-vs-narrow-Q second device
  ([slope](#r-regulator-slope-ratio), [timbre](#r-regulator-timbre-knee),
  [trim](#r-peq-anti-clipping-trim)).
- Whether `--disable volmax` clears level-restore's artifacts, and whether a
  `peak − volmax`-aware restore stays under the limiter
  ([level restore](#r-level-restore)).
- Unheard: the T495's tuning at the gain rail ([T495](#r-gain-rail-tuning)), and
  the regulator's distortion on the second deep-threshold tuning
  ([captures](#r-deep-threshold-distortion)).
- The per-channel threshold read rests on one corpus device and is unverified on
  hardware ([per-channel](#r-per-channel-regulator-thresholds)).

<a id="r-per-channel-regulator-thresholds"></a>

## Per-channel regulator thresholds: newer SoundWire schema (`SUBSYS_37A317AA`)

The newer Lenovo IdeaPad-5x-2-in-1 SoundWire tuning (`SUBSYS_37A317AA`) nests
`regulator-tuning/threshold_high` (and `threshold_low`) under per-channel
`<ch_00>…<ch_07>` elements, instead of a flat `value=`/`preset=` on the element
itself:

```xml
<threshold_high>
  <ch_00 value="-282,-294,-243,-160,0,…,0" />   <!-- real per-band thresholds -->
  <ch_01 value="-282,-294,-243,-160,0,…,0" />
  <ch_02 preset="array_20_zero" /> …            <!-- unused channels -->
</threshold_high>
```

This schema variant surfaced in the 2483-XML re-derivation. It is the second the
parser handles, after the HDA
[simplified schema](eq-and-frequency-response.md#r-simplified-schema-gain-arrays),
and it sits in the regulator block rather than the audio optimizer.

This is the same per-channel shape as the audio optimizer's `ch_00`/`ch_01`,
resolved through the identical `value=`/`preset=` mechanism. The flat
`resolve_xml_value` read nothing off the parent element. `threshold_high`
therefore resolved to `""`, and the regulator fell back to `[0.0]*20`: no
per-band limiting, the worst failure mode for a speaker-excursion guard.
`make_regulator` consumes only `threshold_high`, so this alone disabled the
device's protection.

`resolve_channel_or_direct` reads `ch_00`. The stereo limiter is a single
instance, so ch_00 is the left-channel reference. `make_regulator` is unchanged.
It warns if `ch_01` diverges and if the tuning is genuinely empty. Per-band-min
across ch_00/ch_01 would protect both channels but can over-limit the one that
didn't need it, so the choice was left to a future device that actually shows
L/R asymmetry. On the only device with this schema today, ch_00 == ch_01.

**Why this ships default (XML-only, but single-sample).** The threshold→limiter
mapping is the same one already validated on the development device. Only the
parse *source* is new, so reading `ch_00` is not a new param hypothesis. The
prior behaviour, no limiting, is unambiguously wrong for a protection feature.
This `ch_00`→`threshold_high` reading rests on **exactly one corpus device**. No
second device with the schema exists to cross-check. The reading has not been
verified on the hardware. Scope was re-derived with `corpus_audit`'s
`threshold_schema` classifier: 9 profiles / 1 device carry the dropped form, and
33,113 other reg-enabled internal_speaker profiles on that 2483-XML cohort use
the flat form and are untouched (36,620 on the 2795-XML cohort,
cross-device-findings §12). DSO and the advanced virtualizer for this device
remain unmodeled (see cross-device-findings §14), so its preset is still
incomplete. The regulator fix closes the most dangerous gap, not all of them.

<a id="r-gain-staging-budget"></a>

## Gain-staging budget

With the gains below, the normal-operation surplus is small enough that content
sits at target loudness without the regulator triggering. Worst-case quiet-input
scenarios are caught by the brickwall limiter rather than clipping the output.
Each stage in the chain is a potential gain trap:

| Stage | Gain | Reason |
|-------|------|--------|
| Convolver (FIR peak-normalized) | 0 dB | `make_fir` divides the IR by its peak magnitude, so the convolver only ever attenuates and cannot clip on a boost-heavy curve. |
| Convolver plugin `autogain` | explicitly `false` | EasyEffects' default is `true`, which re-normalizes by RMS power. Commit `5973326` disables it. |
| PEQ `output-gain` | narrowband-scaled | Compensates for the highest PEQ bell gain, scaled down for narrow-Q bells because a Q=4.6 bell only boosts a thin slice of spectrum. Commit `c36907c` relaxed this from full compensation. |
| Regulator `input-gain` (volmax) | +6 dB typical (device/profile-specific) | Dolby's `volmax-boost`, the volume-leveler loudness ceiling, applied statically. |
| MBC upward compression | 0 dB | LSP plugin defaults enable upward compression below `boost-threshold=-72 dB`. Dolby's compressor is purely downward. Commit `e454711` disables it on both MBC instances. |
| Regulator upward compression | 0 dB | Same LSP default issue. Upward compression on a *limiter* is especially wrong. Also fixed in `e454711`. |
| Output limiter | −1 dBFS | Final catch-all for inter-sample peaks after everything else. |

- **Convolver (FIR peak-normalized).** It is the first stage, fed at unity, with
  nothing but the −1 dBFS brickwall downstream. The peak normalization is a
  scalar on the IR, so it is a constant dB offset at every frequency and the
  correction *shape* is untouched. The XML-derived `volmax-boost` restores the
  level it removes; there is no invented makeup gain. Present since `9eb5871`.
- **Convolver plugin `autogain`.** Our minimum-phase FIR concentrates energy at
  the peak sample, so RMS power ≈ 0.00001 and the EasyEffects default would
  apply a **+50 dB boost**.
- **Regulator `input-gain` (volmax).** Default slot:
  `multiband_compressor#1.input-gain`. Fallback: `limiter#0.input-gain` when the
  regulator is absent. `--disable volmax` turns it off. Why this slot, and the
  `--volmax-slot output-gain` opt-out: the
  [`volmax-boost` slot](#r-volmax-boost-slot).

<a id="r-volmax-boost-slot"></a>

### `volmax-boost` slot: `input-gain` vs `output-gain` (issue #23)

`input-gain` has been the default slot since 2026-06-22. Issue
[#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23)
(ThinkPad X13 Gen 6) reported audible distortion on loud *low* frequencies with
volmax on, gone with `--disable volmax`.

`volmax-boost` is Dolby's volume-leveler ceiling, a +6 dB *dynamic* gain. Here
it is a *static* gain, since there is no MI-steered leveler to replicate. On the
`output-gain` slot, the original default, it is added *after* the regulator's
per-band limiting, so it feeds the −1 dBFS brickwall directly. On loud content
the loudest band clips. `input-gain` avoids this.

The output-gain placement is not Dolby-derived, which corrects a prior claim.
Commit `a50f61d` called it "mirroring Dolby's VolMax placement inside the VLLDP
pipeline", with no Dolby source. `volmax-boost` lives in `tuning-cp`, the CP
stage, next to `volume-leveler-*`. Applying it at the *output* of a VLLDP-stage
regulator is upside-down against the CP→VLLDP order. Output-gain *is* defensible
on loudness delivery, not topology: as the last stage before the brickwall, it
gets the makeup to the output, the issue
[#9](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/9)
goal. The XML child-element order is canonical/alphabetical, not signal-flow, so
it doesn't source the within-stage order either.

`--volmax-slot input-gain` moves the boost *ahead* of the regulator's per-band
downward compression, so the boosted low end is tamed before the brickwall. Both
backends carry it: EE `input-gain` translates to LSP `g_in` in `ee_to_pipewire`.

On-device A/B on the dev device (X1 Yoga G7 `17AA22E6`), 2026-06-22, live-EE
loopback via `tools/measure_ee/`:

- *Distortion*: on a sustained 234 Hz tone at the FIR peak (−2 dBFS),
  output-gain gives 11.6% THD (brickwall clipping) and input-gain 0.06% THD.
  Input-gain costs 1.46 dB of level at that band. On a swept tone the difference
  is audibly decisive: clean hum vs reedy buzz.
- *Loudness*: integrated LUFS, the proper #9 check, 3-way on broadband pink:

| pink stimulus | no-volmax | output-gain | input-gain |
|---|---|---|---|
| Loud master (peak −0.5 dBFS) | −19.8 | −13.8 (+6.0) | −13.8 (+6.0) |
| Moderate (peak −5.4 dBFS) | −24.7 | −18.7 (+6.0) | −18.7 (+6.0) |

Both slots add the full +6.0 dB over no-volmax and are identical, with **0 dB
give-back**, at both levels. So on the dev device the slot choice has no
loudness impact on normally-loud program material. Input-gain's cost is confined
to sustained FIR-peak bass, the 1.46 dB above. Broadband content never engages
the regulator enough to lose it.

The 0 dB give-back is a best-case artifact (`corpus_audit.py`, 2026-06-22; 7620
active-band FOCUS rows, FOCUS = dynamic/movie/music/game):

- **slope=16** (hard brickwall) on 72–92% of profiles, so ratio isn't the
  differentiator; thresholds are.
- **threshold_high active-min**: corpus median −18 dB (p10 −26, p90 −12). The
  dev device sits at −10 dB, *less* aggressive than 91–94% of FOCUS profiles.
  Its regulator independently under-engages (the
  [MBC ratio and time constants](adaptive-processing.md#r-mbc-ratio-time-constants)
  and the [fixed dynamics constants](#r-fixed-dynamics-constants)). It barely
  grabs the boost, hence 0 dB loss. On a typical or aggressive regulator,
  input-gain compresses the boost harder: real loudness loss or pumping, which
  we did not measure.
- **The X13 itself**: active-min −24 dB, 10 active bands, slope 16. That is far
  more aggressive than the dev device, near the corpus median-to-aggressive
  band.
- **High boost (+8/+9 dB)** concentrates in `voice`, which usually has no active
  band-limiting, so the worst boost×aggressiveness overlap is limited. The risk
  population is `dynamic`/`movie`/`game` at +8 dB over an aggressive regulator.
- **Same X13 subsys (`17AA2344`), two tunings in the wild**: the device-specific
  package (`tuning_version=24`, active −24/−19 regulator) is what we analyzed. A
  generic `dax3_ext_rtk` copy (`tuning_version=1`) has an all-zero, inert
  regulator. Input-gain only does anything when the regulator is active.

The original ship kept `output-gain` as default, with `input-gain` as a
documented opt-in, because one best-case-device measurement (dev device, 0 dB
give-back) didn't clear the ≥2-device bar, and the corpus analysis above flagged
a real loudness/pumping risk on aggressive regulators. Issue
[#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23)'s
X13 reporter closed that gap. Their device has active-min −24 dB, *more*
aggressive than the dev device and than 91–94% of FOCUS profiles. They confirmed
`input-gain` removes the distortion ("in most cases I don't notice distortions
anymore") while staying loud and uncompressed ("loud enough and definitely not
overcompressed"). The dev device (best case, clean either way) plus the X13
(aggressive case, the risk population) give the ≥2-device, aggressive-regulator,
clean-*and*-loud confirmation the promotion gate required. `output-gain`
survives as `--volmax-slot output-gain`, an opt-out for A/B or for recovering
loudness if a device's regulator over-tames the bass.

The reporter said "in most cases": `input-gain` substantially reduces but may
not 100% eliminate distortion on the most extreme content. It is strictly better
than `output-gain` there, and `--disable volmax` remains the full escape hatch.
The reporter runs their own filter-chain converter, with output "similar" to
`ee_to_pipewire`, so the confirmation is on the PipeWire-backend staging, not
EE. The flip does not touch XML-only-derivability: both slots emit the same
XML-derived +6 dB. Only the chain position differs, and neither position was
Dolby-derived to begin with.

**Inert-regulator caveat (2026-07-21, issue
[#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27)
follow-up).** On an inert regulator, *both* slots degenerate to the same untamed
feed into the brickwall. The Galaxy Book6 Ultra field report hit this corner,
which the corpus note above predicted ("input-gain only does anything when the
regulator is active"). Its tuning's `threshold_high` is flat 0 dB, so
`make_regulator` disables every band. The taming rationale doesn't apply at all,
and the +6 dB boost is pure brickwall drive on loud content. It was reported as
"degrades the sound dramatically", though confounded with bass-enhancer and
regulator disables in the same run (cross-device-findings §15 addendum). The
generator prints a heads-up pointing at `--disable volmax` when volmax rides a
regulator whose bands are all threshold ≥ 0 dB and the coupled-bands mapping
limits no zone. Before coupled-bands became the default on 2026-08-11, the
corner was common, not exotic: roughly 1 in 7 default runs corpus-wide, half the
corpus counting voice profiles. The prevalence sweep and methodology are in the
§15 addendum. Under the coupled-bands default, such a tuning gets a full-band
0 dBFS limiter ([fixed dynamics constants](#r-fixed-dynamics-constants) (f)),
and the heads-up fires on no default run: 0 of 897 unique corpus tunings,
against 137 under the pre-flip condition (ad-hoc `parse_xml` sweep, first
profile, 2026-09-24).

### Why the PEQ `output-gain` stays a single global `max(L,R)` (not per-channel)

Keeping a single global `max(L,R)` resolves cross-device-findings.md "Open
follow-ups" item 2 (asymmetric L/R PEQ peak gain) as *no code change*. The
regression test
`test_peq_output_gain_uses_global_max_across_asymmetric_channels` locks it in.

Every corpus file is an `internal_speaker` per-speaker acoustic correction. The
L and R curves legitimately differ where the two physical speakers do. The
2483-XML re-derivation surfaced this as real per-channel divergence in the
*peak* boost (`corpus_audit`'s L/R peak-asymmetry tally, 2026-06-17): 131
profile rows / 10 devices, all on Lenovo convertible/AIO SKUs (ALC257/287),
never the symmetric clamshells. It splits cleanly:

- ~119 are matched-filter ~1 dB gain trims (median 1.0 dB), ordinary per-speaker
  HF correction.
- 12 are *structural* 7 dB cases in convertible `stand` pose on
  `voice_onlinecourse`. One speaker is high-passed and the other gets a +15 dB
  low-mid bell. This per-orientation correction only appears because the
  expanded cohort added pose-aware tunings.

The converter's anti-clipping trim (`make_peq_eq`) negates the **global
`max(L,R)`** effective boost into the equalizer's single `output-gain`. That is
the correct choice, and the only representable one: EE's equalizer has
per-channel `left`/`right` bands but just one `output-gain`. Applying `max(L,R)`
equally to both channels shifts them together. That preserves the Dolby-tuned
L/R relationship at every frequency, including the 7 dB worst case. A
*per-channel* trim (e.g. `-6 dB` L / `-2 dB` R) would impose a broadband L-vs-R
level tilt Dolby never intended, corrupting the stereo image. It isn't
expressible as one `output-gain` anyway. The only cost of global-max is extra
headroom on the quieter channel, which the downstream leveler restores.

<a id="r-deep-threshold-bass-loss"></a>

## Why bypass has more bass than the preset (issue #44, round 3, 2026-08-22)

Two causes explain the reporter's round-3 observation that *"disabling the
preset completely … adds a lot of bass, although everything else does not sound
as good"*, and only the second is ours. Neither cause is the "quieter than
bypass" mechanism of the `--enable level-restore` section below, for three
reasons. This tuning's peak-normalisation deficit is −1.4 dB. This device
carries no protective PEQ high-pass, and no PEQ at all. The
[simplified-schema AO units finding](eq-and-frequency-response.md#r-simplified-schema-ao-units)
matched its static curve to DAX within 0.72 dB.

**1. The static half is the tuning, and it is faithful.** The preset's measured
transfer against bypass (pink @ −18 dBFS, EE → null sink) is −1.11 dB at 47 Hz,
+2.16 at 141, +5.02 at 234, +6.28 at 2250 and +6.56 at 3000. The FIR is
peak-normalised, so the only level give-back is the broadband `volmax-boost` of
+7.0 dB. This tuning's own curve is −6.6 dB at 47 Hz relative to its 234 Hz
peak. That +7 dB therefore cancels at the bottom and lands in full above 200 Hz.
Switching the preset off removes a ~6 dB lift from everything *except* the
bottom octave. The reporter's own DAX on-minus-off delta does the same or more:
47 Hz sits 5.2 dB below 234 Hz on pink and 9.2 dB below on stepped tones. Both
halves of the report are one fact, and it reproduces on Windows.

**2. The dynamic half is the regulator, it is ours, and it is large.** This
tuning's regulator has eleven active bands, the deepest at −30.9 dBFS, against
four bands and −10.0 dBFS on the dev X1 Yoga. Its `threshold_high` runs −18.375
/ −16.0 / −16.0 / −30.875 / −22.375 / −18.5 / −24.3125 / −19.625 / −13.5 / − /
−17.1875 / −11.875 dB over bands 47 Hz…3 kHz. The raw 1/16-dB ints are −294 /
−256 / −256 / −494 / …. `distortion_slope` 1.0 gives ratio 100:1. The
`--volmax-slot input-gain` default (#23) feeds the regulator all of that +7 dB.

The measured consequence on a −5 dBFS bass burst is **10.3 dB of bass
attenuation below 300 Hz and the bursts flattened**. Subtracting the static
model (FIR + dialog bell) from the pink capture leaves a residual of ≈0 above
1 kHz (mean +0.23 dB). Below that it is −0.57 / −1.46 / −1.67 / −3.24 / −0.48 dB
at 47 / 182 / 277 / 328 / 656 Hz. The deepest reduction lands exactly on the
−30.875 dBFS band. On *bass* content (`stimulus_bass_burst`: −5 dBFS peak,
−8.82 dBFS below 300 Hz, crest 3.8 dB), the default chain delivers −19.07 dBFS
below 300 Hz at crest 17.5 dB.

Variant sweep of the reporter's XML, 2026-08-22, on the same EE → null sink
route, so the speakers never enter it:

| variant | <300 Hz RMS | vs default | crest | out-of-band (stimulus −105.6 dBFS) | pink broadband |
| --- | --- | --- | --- | --- | --- |
| default | −19.07 dBFS | — | 17.5 dB | −81.9 dBFS | — |
| `--volmax-slot output-gain` | −12.27 dBFS | +6.79 dB | 11.2 dB | −83.4 dBFS | +1.03 dB |
| `--disable regulator` | −6.41 dBFS | +12.66 dB | 5.4 dB | −61.3 dBFS | +1.08 dB |
| `--disable dialog` | −19.06 dBFS | +0.01 dB | — | — | −0.40 dB |
| `--disable coupled-bands` | −19.05 dBFS | +0.02 dB | — | — | +0.01 dB |

`output-gain` wins on every axis measured here. It recovers 6.8 dB of bass,
restores crest factor, and costs 0.00 dB on quiet content and ≤0.11 dB above 1
kHz. It produces *less* out-of-band energy than the default, because the bands
no longer see the boost. `--disable regulator` recovers more bass, but moves the
whole +7 dB into `limiter#0`, which then does all the work. Out-of-band products
rise 20 dB, and the flag drops the protection entirely. `--disable dialog` is a
separate axis, the static presence bell DAX applies only on speech: −2.06 dB
@2250, −1.48 @3000, ~0 in the bass. `--disable coupled-bands` is inert at these
levels, as the scope-honesty note in the
[fixed dynamics constants](#r-fixed-dynamics-constants) predicts.

This reverses [#23's trade](#r-volmax-boost-slot) on this tuning. There,
`output-gain` distorted on loud low frequencies; here it is cleaner *and*
louder than the default. Both readings can hold, because #23's device and this
one sit at opposite ends of regulator aggressiveness. `--volmax-slot`'s own help
already names "input-gain costs too much loudness on a device with an aggressive
regulator" as the exception. The reversal is not a default-flip signal on its
own: that needs the bar in `.claude/rules/xml-derivability.md`, and this is one
device measured on one stimulus.

**What would settle it.** A DAX capture of `stimulus_bass_burst` from this
device. Without one, whether Windows also strips ~10 dB off loud bass is
unmeasured. That is the difference between "our staging is wrong" and "the
tuning asks for this and Windows sounds the same". Round 3 asked the reporter
for it, and the stimulus file ships in `tools/measure_dax/`. Round 3 also wrote
here that none existed from any device and that every archive capture was −18
dBFS. That was wrong: the dev X1 Yoga's −5 dBFS Dynamic capture from 2026-05-06
is the [DAX virtual-bass finding](virtual-bass.md#r-dax-virtual-bass)'s
VBE reference. Round 4 below uses both.

**Round 4 (2026-08-24): the listener verdict, and the capture.** The reporter
ran `--volmax-slot output-gain` and confirmed it by ear: *"does add the bass
back to the preset … way more balanced this way and I would say it's on par with
what I heard in Windows"*
([comment](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44#issuecomment-5399420807)).
It is the first listener confirmation of the `output-gain` placement, from a
deeper-threshold tuning than [#23's](#r-volmax-boost-slot): −30.9 dBFS across
eleven bands here, against the X13's −24 dB active minimum. The two field
verdicts pick opposite slots because the devices differ, exactly as the flag's
help text predicts, not because either measurement is wrong. So the default
stays `input-gain`, and the README's tested table carries this device with the
flag. Whether an XML-derivable predictor, such as active-band count or deepest
`threshold_high`, could pick the slot per tuning stays a hypothesis until a
third device lands on one side or the other.

The same comment attached the Windows `stimulus_bass_burst` captures asked for
above: Dolby off and Dynamic, 48 kHz, from `capture_dax.py`. The reporter's
stimulus file is byte-identical to ours. The pair is the first loud-stimulus DAX
capture with an `off` counterpart, and the first from a second device. The dev
X1 Yoga's capture, the
[DAX virtual-bass finding](virtual-bass.md#r-dax-virtual-bass)'s
reference, is compared at the end of this round. An ad-hoc band-RMS pass
analysed them on 2026-08-25, alongside the round-3 EE captures on the same
stimulus, so every column is the same arithmetic: the mono mean of L/R.
`analyze.py` has no `bass_burst` handler; adding one is the tooling follow-up.
DAX's L−R is ≤ 0.01 dB throughout, and the round-3 figures reproduce within 0.2
dB.

| signal | <300 Hz RMS | vs stimulus | crest | >1 kHz | THD @ 50 Hz |
| --- | --- | --- | --- | --- | --- |
| stimulus | −8.82 dBFS | — | 3.8 dB | −105.6 dBFS | 0.00 % |
| DAX off | −8.82 | 0.00 | 3.8 | −92.6 | 0.00 |
| DAX dynamic | −9.99 | −1.17 | 10.0 | −72.6 | 10.1 |
| EE default | −18.89 | −10.07 | 17.4 | −81.7 | 2.6 |
| EE `--volmax-slot output-gain` | −12.10 | −3.28 | 11.1 | −83.2 | 2.3 |
| EE `--disable regulator` | −6.25 | +2.57 | 5.3 | −61.1 | — |

Per tone, fundamental level relative to the input (DAX: dynamic − off; EE:
capture − stimulus):

| tone | DAX dynamic | EE default | EE `output-gain` | EE `--disable regulator` |
| --- | --- | --- | --- | --- |
| 50 Hz | −2.83 | −10.22 | −3.41 | −0.22 |
| 80 Hz | −2.20 | −10.33 | −3.39 | +1.58 |
| 120 Hz | −0.24 | −10.43 | −3.50 | +3.09 |
| 180 Hz | −0.19 | −9.64 | −2.95 | +4.52 |

**Answer: no, Windows does not strip ~10 dB off loud bass.** Dolby off is a
faithful bypass: levels match the stimulus within 0.01 dB, with 0 clipped
samples. Dolby on lands 0.2–2.8 dB *below* the input on sustained bass, the
default placement 10 dB below and `output-gain` 3–3.5 dB below. `output-gain` is
the closest shipped variant to DAX at every tone: 0.6 dB off at 50 Hz, 1.2 at
80, 3.3 at 120/180. The default is 7–9 dB too quiet, and `--disable regulator`
overshoots by 2.6–4.7 dB. The listener's "on par with Windows" is what the
capture says too.

How DAX gets there:

- *The onset passes at full static gain.* The first 10 ms of every burst reads
  −4.3 dBFS RMS, 3.7 dB *above* the input's steady −8.0 despite the stimulus's 5
  ms fade-in. The peak hits 0.00 dBFS, and every clipped sample falls inside the
  first 2–5.5 ms of a burst (the sidecar counts 55). Gain reduction then builds
  over ~100–150 ms: at 50 Hz, −4.3 → −8.5 at 40 ms → −10.4 at 100 ms → −10.8
  steady. DAX's protection limiter is at least as busy as ours, with ≥ 6.5 dB of
  reduction at 50 Hz from the level it lets the onset through at. But it is
  slow-attack and lookahead-free, and it keeps the transient. Our regulator
  (`attack 1.0 ms`, the [fixed dynamics constants](#r-fixed-dynamics-constants))
  clamps the same onset instantly: the EE variants show no overshoot at all
  (output-gain −11.2 → −11.4). That is the "punch" difference the reporter
  described, measured. On the dev X1 Yoga's capture the ride is slower still and
  leveler-like: the 120 and 180 Hz bursts drift down a further ~3.5 dB between
  0.3 s and the steady window. So ~150 ms is one device's number, and part of it
  may be the leveler rather than the regulator.
- *The steady-state ceilings track the decoded thresholds plus the makeup.* DAX
  settles at −10.8 / −10.2 / −8.2 / −8.2 dBFS RMS on the 50 / 80 / 120 / 180 Hz
  tones. The XML's `threshold_high` is −18.375 dBFS for the 47 Hz band and −16.0
  for the 141 and 234 Hz bands. That is a 2.4 dB step where DAX shows a 2.6 dB
  step, at an offset of +7.6…+7.8 dB, and the tuning's `volmax-boost` is +7.0.
  The net DAX behaviour therefore reads as *limit at the decoded threshold, then
  apply the makeup*, which is the `output-gain` order. A threshold decode ~7 dB
  too deep would give the same net numbers. So this is consistency with the
  1/16-dB threshold read (the
  [simplified-schema AO units finding](eq-and-frequency-response.md#r-simplified-schema-ao-units))
  plus the output-gain placement, not proof of either. One loose end is open:
  the 81 and 182 Hz bands are not among the eleven active ones in our reading,
  yet DAX limits 80 and 180 Hz like their neighbours. Either its band split is
  coarser than the 20 centres, or an inactive band inherits a neighbour's
  ceiling.
- *DAX distorts deep bass far more than we do.* THD at 50 Hz is 10.1 % (H3
  −21 dB, H5 −28), against 2.3–2.6 % for either EE placement; 3.6 % at 80 Hz,
  0.4 % at 120, 0.08 % at 180. Odd harmonics dominate: limiting or soft
  clipping, not a virtual-bass stage, since #14 established VBE is off on this
  hardware. Out-of-band energy is −72.6 dBFS against −83 for EE. Whatever the
  mechanism, "cleaner than DAX" is not a constraint our chain is failing on this
  content. The defect is "quieter than DAX by 7–9 dB".

**Decision.** This is still not a default flip on its own. The xml-derivability
bar is two devices, and this is one, while #23's evidence for `input-gain` is a
listener verdict with no DAX capture behind it. This one is the first loud-bass
DAX capture with an `off` counterpart, and the first from a second device. It
does settle the round-3 dichotomy: "our staging is wrong on this tuning" wins
over "the tuning asks for this and Windows sounds the same".

The dev X1 Yoga cannot serve as the second device on this stimulus. The check
(2026-08-25) used its own 2026-05-06 DAX capture, which is Dynamic only. `off`
is a verified bypass on that device from the pink/stepped batteries. DAX lands
−0.37 dB below the input below 300 Hz, and per tone −25.2 / −2.3 / +0.2 / +2.8
dB at 50 / 80 / 120 / 180 Hz, with the 50 Hz fundamental replaced by VBE
harmonics (the [DAX virtual-bass finding](virtual-bass.md#r-dax-virtual-bass)).
The EE default, `output-gain` and the 2026-08-11 default chain all land
−4.2…−4.6 dB, within 0.5 dB of *each other*, because its 100 Hz PEQ high-pass
takes 50–120 Hz below the regulator's thresholds before the slot can matter. The
dev capture does show a static low-end gap. EE lands −43 / −20.9 / −7.8 / +0.9
dB against DAX's numbers above, a gap of 18.6 dB at 80 Hz and 8 dB at 120 Hz.
The −25 dBFS quiet burst shows the same shape under the leveler's +21.7 dB
makeup. It is the HP-slope / LF-leveler deviation that
["The 47 Hz deviation"](eq-and-frequency-response.md#r-ee-response-vs-xml)
records, with numbers added at 80 and 120 Hz.

The second device for the slot question therefore needs a burst stimulus inside
the dev regulator's active zone. Its four active bands are 47 / 141 / 234 /
328 Hz at −10 / −9 / −8 / −5 dBFS. Tones at 180 / 234 / 280 / 328 Hz at −5 dBFS
peak clear the high-pass and sit above threshold with the +6 dB boost.
`make_bass_burst` already takes `tone_freqs_hz`, so that is a generator entry,
one Windows capture pair and one EE A/B. If DAX there sits near `output-gain`,
the placement is DAX-faithful across the axis and the flip clears the bar. If it
sits near `input-gain`, the slot is tuning-dependent and the XML-derivable
predictor above becomes the fix.

The reporter also observed that Windows is far louder with Dolby on than off,
while the Linux preset barely changes level. That is the leveler's +8.2 dB loud
/ +21.8 dB quiet makeup (broadband pink RMS) measured in the
[simplified-schema AO units finding](eq-and-frequency-response.md#r-simplified-schema-ao-units).
As a 100 Hz–10 kHz mean it is +9.13 / +25.25 dB
([level-restore](#r-level-restore)). It is `--enable autogain` /
`--enable level-restore` territory, not this subsection's, and was raised with
the reporter in the same thread.

<a id="r-deep-threshold-distortion"></a>

## Second deep-threshold tuning: issue #84's Yoga Slim 7 Pro 14ACH5 (2026-08-30)

> The crackle in this report is explained by EasyEffects running the preset
> +11.8 dB hot at the reporter's 192 kHz graph
> ([resample gain](easyeffects-and-pipewire.md#r-convolver-resample-gain)). The
> captures below still bound what our DSP adds on this XML.

On this tuning the regulator engages on ordinary −18 dBFS pink and on a −5 dBFS
bass burst, as on #44, and the distortion it adds measures 0.4–2.2 %: grit on
paper, not crackle. The report was "constant crackle on every preset" on a Yoga
Slim 7 Pro 14ACH5: 82MS, ALC287 `17AA384F`, full schema, no Dolby MBC in the
XML. Its regulator is the #44 class: nine active bands 47–1313 Hz, deepest −29.8
dB at 469 Hz, `distortion-slope` 1.0 → 100:1, and volmax +5.1 dB on the input.
So before the reporter's own A/B came back, this XML went through the same EE →
null-sink route as the #44 sweep above. It was built with `--prefix` beside the
dev machine's presets. `tools/measure_ee/sweep_variants.sh` takes a `STIMULI`
subset, so the battery was `bass_burst`, `multitone`, `speech` and `pink`. Three
variants ran:

- `default`
- `--disable regulator --disable volmax`, the linear reference. The two flags
  remove the per-band regulator and the static volmax boost. The brickwall
  `limiter#0` stays, idle on this content at a peak of −6.9 dBFS.
  `--disable regulator` alone moves the boost into the brickwall, the confounded
  shape the #44 table shows.
- `--volmax-slot output-gain`

| readout (channel L) | default | no dynamics | `output-gain` |
| --- | --- | --- | --- |
| `bass_burst` <300 Hz RMS / crest | −18.1 dBFS / 16.2 dB | −13.3 / 6.3 | −13.2 / 11.4 |
| `bass_burst` Δ3 at 50 / 80 / 120 / 180 Hz | −33 / −36 / −39 / −49 dB | (none: −155) | −33 / −40 / −43 / −53 |
| `multitone` (−18 dBFS) out-of-band vs tones | 37.9 dB down | 125.7 dB down | 42.1 dB down |
| `pink` residual vs XML, RMS / max | 0.88 / 4.73 dB | 0.79 / 2.93 | 0.75 / 3.14 |

The evidence that it engages is 4.8 dB off a −5 dBFS bass burst against the
linear build, and a 4.7 dB pink excursion where the linear build has 2.9. The
burst's shape says how: its body is held 4–6 dB down, while its onset passes
about 5 dB *above* the linear build (peak −1.8 vs −6.9 dBFS). That is why the
crest factor rises from 6 to 16 dB rather than falling. An unclamped onset is
the better crackle candidate of the two. It is the chain's only nonlinearity:
odd-order products 33–49 dB below the fundamental on the four bass tones (2.2 %
at 50 Hz, 1.1 % at 120 Hz), and intermodulation ~38 dB below a multitone
(1.3 %). `output-gain` is 4 dB cleaner on the multitone while restoring the bass
level, consistent with #44. The `speech` capture yielded no usable distortion
number. A whole-signal residual against the linear build is dominated by the
regulator's band-selective, time-varying gain, which a static gain match cannot
remove. A per-band envelope comparison would be needed. Listening to the
captures is the gate that has not run.

These captures bound what our DSP adds on this XML. They cannot reproduce a
graph-level crackle (xruns, quantum) on the reporter's machine, and the doctor
could not see one either. Hence the `=== PipeWire ===` section (output sink,
clock, dropouts), added the same day. A three-rung split is drafted for the
reporter: EasyEffects bypass → quit EasyEffects → the linear rebuild, each with
a GUI and a terminal route. If the linear build is what clears it, that is a
second listener saying our regulator and volmax boost are audible. It is not yet
the second DAX attack curve the regulator attack-time question (the
[fixed dynamics constants](#r-fixed-dynamics-constants)) is parked on, since the
two flags also remove the thresholds, ratio and boost.

<a id="r-convolver-headroom-restore"></a>

## Convolver SoundWire headroom restore

- **Factor (generator):** `peak_db * 0.5`. **REMOVED 2026-07-03**: the convolver
  emits 0 dB gain on every device family.
- **Why it's a guess:** The 0.5 was chosen to "recover brightness". It is not
  XML-derived.

The restore was tracking the #13 bug's magnitude, not a property of SoundWire
curves. **Provenance:** `2f4d0b8` (2026-04-12, the first SoundWire user's PR)
introduced it to restore the "+6-7 dB" of level FIR peak-normalization removed.
That large peak was an artifact of the pre-#13 chain over-applying `ieq-amount`
10×, fixed in `eeecc4a` (2026-05-28, on-device validated). After the fix the
same formula self-scaled to ~+0.7 dB. Issue
[#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27)'s
pasted generation runs show FIR peaks +1.1…+1.5 dB → restores +0.6/+0.7 dB.

Field evidence: issue
[#29](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/29)
(Zenbook S14) found the SoundWire preset over-loud, and the reporter manually
set −5 dB output. Removal follows the
[surround→stereo-base factor](adaptive-processing.md#r-surround-boost-stereo-base)
precedent: it *drops* an invented
non-XML gain rather than adopting a new mapping, so the second-device bar
doesn't apply. Loudness makeup is volmax-boost's job (XML-derived, entry on
volmax slots). It is not locally measurable (dev device is HDA); the #29
reporter's regenerate-and-listen is the field check. If a SoundWire DAX capture
ever shows DAX applying net positive gain vs OFF that our chain lacks, restore
via git history (`2f4d0b8`).

<a id="r-regulator-slope-ratio"></a>

## Regulator slope→ratio

- **Factor (generator):** slope read `/16` (`parse_xml`), then
  `ratio = 1/(1−slope)` (`make_regulator`).
- **Why it's a guess:** The `/16` reading is assumed by analogy to the dB
  fields. `1/(1−slope)` is inferred from how corpus values cluster.
- **What would falsify it:** **Not testable on this device.** The X1 Yoga is
  `distortion-slope=16` on *every* profile, so there is no operating-point
  variation to fit `1/(1−slope)`. It needs a device with differing slope values
  and a bass-burst capture comparing gain-reduction-vs-level (Phase 4).

<a id="r-regulator-timbre-knee"></a>

## Regulator timbre→knee

- **Factor (generator):** timbre read `/16` (`parse_xml`), then
  `knee = −6·timbre` dB (`make_regulator`).
- **Why it's a guess:** The `−6` dB maximum knee is a pure guess. The field is
  constant across the corpus, so we have no signal to disambiguate.
- **What would falsify it:** **Not testable on this device.** The X1 Yoga is
  `timbre-preservation=12` (=0.75) on *every* profile, so the `−6·timbre`
  scaling has a single operating point. It needs a device whose XML carries
  `timbre≠0.75`, plus a capture (Phase 4).

<a id="r-peq-anti-clipping-trim"></a>

## PEQ anti-clipping trim

- **Factor (generator):** `effective boost ≈ gain·min(1, 2/Q)` per positive bell
  (full gain for shelves), peak negated into `equalizer#0.output-gain`
  (`make_peq_eq`).
- **Why it's a guess:** The `2.0` bandwidth weighting and the "compensate
  exactly the peak effective boost" rule are converter-invented. Nothing says
  DAX trims broadband level at all. And "over-conservative PEQ output-gain" is a
  listed listen-for trap.

**Still confounded** after two tries: DAX's leveler/volmax staging buries the 3
dB PEQ trim. The dev device has no cross-profile Q contrast: its PEQ is
identical in every profile (+3 dB/Q2 @280, +4 dB/Q4.6 @400, −4 dB/Q1.5 @516).
The hypotheses still predict distinct broadband offsets there: `min(1, 2/Q)` →
−3 dB trim, full compensation → −4 dB, no trim → 0.

An absolute-level EE↔DAX pink compare can discriminate them
(`compare_ee_vs_dax.py --absolute`, volumes pinned; the default 1 kHz
normalization destroys exactly this observable).

- Tried 2026-06 on the archived DAX captures: confounded. The absolute EE−DAX
  offset is −11.5 dB on `dynamic`/`movie`/`game` but −1.0 dB on `voice`, i.e.
  dominated by DAX's profile-dependent leveler/volmax staging.
- Re-tried 2026-06-13 with pinned/recorded 50% volume: still confounded. DAX's
  leveler drives `dynamic`/`movie`/`music`/`game` to a single loudness target,
  with raw transfer all within 0.01 dB. That gives a flat ≈ −8 dB EE−DAX offset
  (leveler boost + our −3 dB trim + convolver peak-normalisation, inseparable).
  `voice`, leveled to a quieter target, shows −0.06 dB.

Useful byproduct: the DAX OFF raw transfer is −0.01 dB at 50% master volume. So
WASAPI loopback taps the engine mix bus pre-volume, and the master-volume term
never enters the captures.

Validating the `min(1, 2/Q)` *shape* still needs a wide-vs-narrow-Q second
device.

<a id="r-fixed-dynamics-constants"></a>

## Fixed dynamics constants

- **Factor (generator):** MBC active-band `knee = −6.0` dB
  (`make_multiband_compressor`; the Dolby 6-tuple has no knee field), regulator
  `attack 1.0 ms` / `release 50.0 ms` (`make_regulator`).
- **Why it's a guess:** Chosen from limiting practice, not decoded.

**Engaged 2026-06-13** by `stimulus_stepped_loud` (see the
[MBC ratio and time constants](adaptive-processing.md#r-mbc-ratio-time-constants)).
The dynamics diagnosis lands *here*, on the regulator's fixed constants, not the
MBC decode. Our `make_regulator` maps the XML thresholds/slope correctly
(−10/−9/−8/−5 dB, near-100:1 on the 4 lowest bands) yet under-engages vs DAX,
which clearly hard-limits those bands.

~~Leading hypothesis: the hard-coded `attack 1.0 ms` / Peak detection /
`1 ms lookahead` / `release 50 ms` make our regulator *release between* the
stepped tones and under-read steady-state GR~~ **Falsified 2026-07-01** by
re-analysis of the same captures. The within-tone envelope (single-bin DFT over
early/mid/late windows of each held tone) shows EE's response is *time-flat*:
drift ≤0.14 dB, no attack ramp, no release decay. The stepped analyzer's readout
already skips the 0.4 s settle, so a 1 ms-attack regulator cannot under-read a
steady-state window by releasing in the gaps. The under-engagement is static,
which points away from the invented time constants entirely.

**New leading suspect, gain staging:** at capture time the dev device's volmax
`+6 dB` sat in the preset `output-gain` slot, *after* the dynamics. The
2026-06-22 `--volmax-slot input-gain` default flip (`4213d5f`, #23) feeds the
MBC/regulator a 6 dB hotter signal. **Measured 2026-07-01:** the flip helps but
does not close the gap, on a fresh 3-level stepped battery through the
regenerated input-gain-default preset vs the archived DAX stepped captures.
Loud-vs-normal GR moved at 234 Hz −5.5 → −6.9 dB (DAX −10.6), 141 Hz 0 → −1.4
(DAX −5.9) and 277 Hz −1.7 → −2.1 (DAX −10.4). 2.25/3 kHz are unchanged. The
regulator is inactive above 328 Hz on this XML, so that part of the gap is the
MBC's knee/RMS conservatism, as diagnosed. Even 6 dB hotter, the realized
regulator curve fits an effective ratio ≈1.8 at 234 Hz against the configured
100:1. The LSP MBC-as-limiter realization (band detection mode / knee / boost
interplay) under-realizes the intended hard limit by an order of magnitude. So
the remaining lever is the regulator's *plugin realization*, not signal level
and not timing. Stage interaction (the MBC's +2 dB makeup re-inflating the
signal the regulator then sees) stays a secondary suspect.

Settling the remaining gap needs more captures.

- (a) A regulator-only EE capture (MBC bypassed) at the 3 levels would
  deconfound the two stages, specifically to characterise the *realized* limiter
  curve against the LSP settings (`make_regulator`'s detection mode, knee,
  lookahead) and find why 100:1 configured realizes as ≈1.8.
- (b) A second-device loud capture comes before any default change (corpus
  invariant).
- (c) Ideally, an EE OFF/flat stepped capture would put EE on DAX's
  absolute-dBFS footing.

**XML-grounded angles to try first:** regulator-tuning carries no time
constants, unlike the MBC's Q15 coeffs, so timing is invented by necessity. Two
currently-ignored regulator fields might inform the engagement.

- (d) Re-examine `regulator-stress-amount` as an engagement/aggressiveness
  modifier, not a threshold offset. Of the top-level `regulator-*` fields the
  converter leaves unmapped, it's the only one that varies per device. Inside
  `regulator-tuning`, `threshold_low` and `isolated_band` (f) vary too. On
  `dynamic` it's `144,144,0,…`, non-zero on exactly the
  under-engaging bands 0–1 (47/141 Hz). The
  [`regulator-stress-amount` follow-up](#r-regulator-stress-amount) holds the
  case for this framing. It is untested and could both explain DAX and stay
  XML-only.
- (e) ~~`regulator-relaxation-amount` (=96) as the release control~~ **Dropped
  2026-06-18.** It is not XML-derivable: it is frozen at 96 across the whole
  corpus, so there is no contrast to decode against. The 2026-07-01 time-flat
  finding also removes its motivation, since release timing isn't the
  under-engagement driver.
- (f) `regulator-tuning/isolated_band` (added 2026-07-30, the
  [simplified-schema AO units finding](eq-and-frequency-response.md#r-simplified-schema-ao-units))
  is a previously-unread per-band 0/1 array with genuine per-device contrast. It
  has 59 corpus patterns, and mirrors threshold-activity exactly on 18,369
  profiles but diverges on ≥1 band on 11,548. Its semantics are unknown.
  Probe-level span attribution on the #44 stepped data argues it does *not* gate
  the measured adaptive layer. That device carries the discriminating contrast
  (band 11 iso=1 vs band 12 iso=0, both threshold-active), and both span ~5 dB
  alike, while the inert iso=0 band 10 spans least (the
  [simplified-schema AO units finding](eq-and-frequency-response.md#r-simplified-schema-ao-units)).

  **Shipped as an experimental opt-in 2026-07-30** (`--enable coupled-bands`),
  **made the default 2026-08-11** (`--disable coupled-bands` opts out).
  Zero-threshold zones whose bands are all `isolated_band=0` join the limiter at
  face value (0 dBFS), so upstream gain (volmax on input-gain) gets tamed there
  before the brickwall. The iso=0 scoping is a conservative gating choice, not
  established causation. The flip knowingly did not clear the
  second-device-capture bar in `.claude/rules/xml-derivability.md`. No capture
  in the −18 dBFS battery can reach it (see the scope-honesty note below). Two
  things replaced that bar. A two-device software A/B (below) bounds the audible
  cost. The other is the argument that the opposite reading, discarding a stated
  0 dBFS threshold, leaves the volmax boost feeding the brickwall untamed on
  exactly the tunings where this fires, which is the failure #23 measured.

  **Corpus-swept same day** (36,371 regulator profiles / 913 devices through the
  real parse + both regulator modes): zero crashes and zero default-output
  deviations. `isolated_band` is *universal*: present on every regulator
  profile, always 20×{0,1}. On all-zero-threshold tunings the flag yields a
  single full-band 0 dBFS limiter. That incidentally restores the "volmax tamed
  before the brickwall" property those tunings otherwise lack.
  Threshold-inert-but-`iso=1` bands exist on 134 devices (mixed zones correctly
  declined).

  **Re-derived against the current corpus for the default flip** (2026-08-11, a
  walk over the same population the corpus tier uses): 2,842 files hold 37,976
  regulator profiles across 979 devices. Of these, 37,675 (99.2%) actually
  change output under the flip, over zones {1: 29,960, 2: 7,400, 3: 315}, and
  2,955 profiles are the all-zero-threshold #27 class. So the flip reaches all
  but ~0.8% of profiles. That is why the `-active` marker left
  `EXPERIMENTAL_MARKERS` on the flip: an ask that fires on every run is an ask
  nobody reads. The same walk caught a real defect. `_coupled_bands_eligible`
  was a band-level `any()` while activation is per zone, so the run announced a
  limit it had not added on 274 profiles whose qualifying band shared a zone
  with an isolated one. The predicate is now zone-level and the two agree
  exactly.

  **Scope honesty (offline staging check, same session):** during the −18 dBFS
  capture battery our chain's level at every coupled band is −12…−19 dBFS (FIR +
  dialog bell + volmax 7). So the captures can neither confirm nor falsify the
  mapping's audible effect. DAX's measured 4–6 dB spans at those levels cannot
  be a static 0 dBFS limiter either: even +8 dB leveler makeup leaves ~−8 dBFS
  in-band. The mapping is a loud-content protection hypothesis, not a
  reproduction of the measured moderate-level spans. It engages when in-band
  level crosses full scale, i.e. content peaks above ≈ −5 dBFS in the 3–6 kHz
  range on this XML. The A/B must use loud material.

  **That A/B ran 2026-08-11, on both shapes, and is what the default flip rests
  on.** The capture route is EE → null sink, so the speakers never enter it and
  any XML's DSP is measurable on one machine. *Dev X1 Yoga* (one zone, 392 Hz–20
  kHz): against `stimulus_stepped_loud` (−2 dBFS peak, 16 dB hotter than the
  battery) the largest per-tone excursion is −0.38 dB, confined to 1.9–4.7 kHz.
  Onset and steady state are identical to two decimals: a small static soft-knee
  offset, not a limiter riding. `pink14` and `bass_burst` come back
  bit-identical. So the mapping is *inert* on this device even on the hottest
  single-band excitation possible. That is why weeks of listening were
  unremarkable, and why this device cannot validate the mapping either. *Galaxy
  Book6 `F020144D`* (the #27 all-zero class, full-band zone, the biggest change
  the flip makes anywhere): −0.43 dB median, −0.91 dB worst on `multitone`, on a
  signal already arriving at the brickwall.

  Two methodological notes. The first attempt was invalid: that device is
  SoundWire, so the *leveler* is active and non-LTI. 14% of frames came out
  louder with the limiter on (max +12.2 dB), which a Downward band with
  `makeup 0` cannot do. Re-running with `--disable autogain` on both sides fixed
  it. Pink-noise rows keep positive excursions even then, so their large
  negative minima are residual misalignment rather than gain reduction. On a
  comparison like this, only stimuli that align tightly (a tone complex,
  envelope correlation 0.9998) can be quoted. The standing residual: none of
  this covers how the #27 shape sounds on *that* laptop's transducers.

  **The experiment that would validate or kill the mapping is a DAX capture of
  `stimulus_stepped_loud` on Windows.** The file already exists, and
  `tools/measure_dax/` + `CLAUDE_WINDOWS.md` carry the protocol. Every DAX
  capture in the archive is −18 dBFS, exactly the level the scope-honesty note
  above says cannot decide this. A −2 dBFS peak run would show whether DAX
  itself limits in bands whose `threshold_high` decodes as 0 dBFS. Read it
  against the known confound: the 4–6 dB spans DAX shows at moderate level are
  adaptive/MI-steered, not a static ceiling. So the discriminator is whether a
  *hard* knee appears at full scale, not whether any gain reduction does.

**Second-device datapoint (2026-07-30, the
[simplified-schema AO units finding](eq-and-frequency-response.md#r-simplified-schema-ao-units)):**
the issue
[#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44)
stepped battery shows DAX applying 4–6 dB of frequency-selective adaptive span
in bands whose `threshold_high` decodes as inert (+0.0) on that XML. So part of
DAX's band dynamics demonstrably lives outside the regulator parameters we
decode, and the "close the regulator gap" ceiling may be lower than the DAX
reference implies. The MBC knee/attack/release themselves still need gated-burst
transients to characterise, which is deferred.

**"Dormant at nominal levels" is device-specific, not a property of the mapping
(2026-08-22, issue
[#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44)
round 3).** That reading came from the dev X1 Yoga, whose regulator has four
active bands at −10/−9/−8/−5 dBFS. `17AA380D` has eleven active bands, deepest
−30.875 dBFS. There the regulator measurably engages on the ordinary −18 dBFS
pink battery, and removes 10.3 dB below 300 Hz on a −5 dBFS bass burst (the
[#44 round-3 measurement](#r-deep-threshold-bass-loss)). So the
under-engagement thread above is a statement about shallow-threshold tunings. On
deep-threshold ones the same mapping over-engages relative to what DAX shows at
the one level both were measured. That also gives the volmax-slot question a
second device pointing the opposite way to #23; see
[Why bypass has more bass than the preset](#r-deep-threshold-bass-loss). Round 4
of that section measured DAX's own limiter on a −5 dBFS bass burst. The onset
passes at full static gain (0 dBFS peak, every clipped sample in the first
2–5.5 ms of a burst), and the
reduction settles over ~100–150 ms. That is the first direct measurement of a
DAX limiter time constant, and it puts this entry's 1 ms attack about two orders
of magnitude too fast on deep bass.

<a id="r-regulator-stress-amount"></a>

## `regulator-stress-amount` mapping investigated and rejected

Status: the threshold-offset mapping is rejected and kept as a permanent
finding, with no constraint change. The engagement-modifier reading, reopened
2026-06-13, is untested and queued with the regulator-only capture.

The threshold-offset mapping, tested under alignment hypothesis A, is
directionally falsified at 180 Hz. The item reopened 2026-06-13 under a
different reading.

Issue
[#11](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/11)
raised whether DAX's "tier-2" adaptive sub-models could explain part of the
EE-vs-DAX gap. A corpus audit across ~2,900 XMLs (2026-05-06, `4425a6e`)
settled the schema-prevalence side of that question:

| field | enabled / non-default in any XML |
|---|---|
| `sliding-bass-enable=1` | 5 IdeaPad-3 XMLs (all `max-gain=0`, dormant) |
| `volume-modeler-enable=1` | 0 |
| `process-optimizer-enable=1` | 0 (bands always `array_20_zero`) |
| `regulator-stress-amount` | non-zero on bass bands in 86% of XMLs |
| `regulator-overdrive` | always `0` (35,654 profile slots) |
| `regulator-relaxation-amount` | always `96` (13,042 profile slots) |

> Re-derived 2026-09-24 over the grown corpus: `volume-modeler-enable` and
> `process-optimizer-enable` are still 0 on every row, `regulator-overdrive` 0
> and `regulator-relaxation-amount` 96 on every slot. Sliding bass does not
> fit its row: it is enabled with real gain on 63 devices of the 2795-XML
> corpus (2026-08-03, unchanged 2026-09-24;
> [cross-device §14](../cross-device-findings.md#band-a--has-parameters-so-implementable)).

Of the candidates, only `regulator-stress-amount` carries live,
device-varying values, so it is the only one worth testing. The remainder are
dormant or constant across shipped tunings, so implementing them blind would
change zero output samples on real XMLs.

The natural mapping for `regulator-stress-amount` lowers the per-band
regulator threshold by `stress[i]` dB, so the limiter engages earlier on
stressed bands. A temporary `--enable-regulator-stress` flag wired it up
under alignment hypothesis A, where `stress[i]` indexes the post-grouping
zone i. The sign convention was stress > 0 → a tighter limiter. Validation
against the X1 Yoga DAX captures used a bass-burst stimulus added to
`tools/measure_dax/make_stimulus.py`: sustained sine tones at
50/80/120/180 Hz, at -5 and -25 dBFS.

The captures show:

- 180 Hz is the only diagnostic tone. The FIR + PEQ attenuate 50/80/120 Hz by
  11-42 dB before they reach the regulator, so the regulator never engages on
  them in either EE config. At 50 Hz the DAX side also showed a 23 dB crest
  factor, indicating Virtual Bass Enhancement adding harmonics that
  contaminate any regulator-only comparison.
- At 180 Hz, DAX captured -6.14 dBFS, EE-off -8.93 and EE-on -9.71.
  |DAX - EE_off| = 2.79 dB and |DAX - EE_on| = 3.57 dB, so stress-on moved EE
  *away* from DAX, not toward it.
- DAX's regulator engages ~19 dB of GR at 180 Hz: its loud-quiet diff is
  1.13 dB instead of the 20 dB a dormant regulator would give. EE-on engages
  0.78 dB. Whatever DAX is doing is an order of magnitude stronger than the
  9 dB threshold drop our `stress=144` produces. The 1/16-dB convention may
  be wrong, stress may not be a threshold offset at all, or DAX's bass
  control runs through a stage we can't approximate.

**Reopened 2026-06-13 (different reading).** The dynamics-gap diagnosis (the
[MBC ratio and time constants](adaptive-processing.md#r-mbc-ratio-time-constants)
and the [fixed dynamics constants](#r-fixed-dynamics-constants)) found the
regulator under-engages on exactly bands 0–1 (47/141 Hz), precisely where
`stress=144,144` sits. The same diagnosis found DAX's effective ratio there
(~2.95) far exceeds our 1.67. That points at the "stress may not be a threshold
offset at all" reading above. Re-test `stress-amount` as an engagement /
aggressiveness modifier, intensifying limiting (ratio/attack) on stressed bands,
which the original threshold-offset experiment never tried. Queue it alongside
the regulator-only capture (the
[fixed dynamics constants](#r-fixed-dynamics-constants)). The
`regulator-relaxation-amount` companion-decode was dropped 2026-06-18 (the
[fixed dynamics constants](#r-fixed-dynamics-constants) (e)).

The flag has been reverted, per CLAUDE.md "Investigation flags are
scaffolding". The mapping math is documented here as a permanent finding
rather than carried as a CLI switch future readers would feel obliged to keep
correct.

What remains in committed code:

- `regulator-stress-amount` is parsed into the regulator dict and printed in
  the debug summary. It is visibility only, with no behavioural effect.
- `regulator-overdrive` and `regulator-relaxation-amount` are parsed,
  printed, and on the `_UNMODELED_FEATURES` watch list. Any XML where they
  deviate from the corpus constants (`overdrive=0`, `relaxation=96`) will
  trigger a "report this XML" warning, so we can re-investigate if the corpus
  assumption changes.
- The bass-burst stimuli (`stimulus_bass_burst.wav` /
  `stimulus_bass_burst_quiet.wav`) ship as part of the standard measurement
  suite. They remain a useful diagnostic for any future bass-region work,
  even though the stress hypothesis closed.

Bigger picture, linking back to the
[EE response vs XML](eq-and-frequency-response.md#r-ee-response-vs-xml): DAX
delivers 22-30 dB more bass to its regulator than our chain delivers to ours,
then runs a much more active regulator on top. That is the gap to close, and the
stress field can't reach it. Two architectural levers might:

- Less aggressive bass attenuation in the FIR/PEQ stages, so our regulator sees
  content above its threshold. This is currently a layer-2 IEQ/AO interpretation
  question (the
  [AO sign variant matrix](eq-and-frequency-response.md#r-ao-sign-variant-matrix)
  and the
  [XML-interpretation hypotheses](eq-and-frequency-response.md#r-xml-interpretation-hypotheses)).
- A level-dependent / VBE / leveler stage upstream of the regulator. No LSP
  equivalent of DAX's leveler exists, so it would need custom DSP or a
  different plugin pipeline.

Both are larger pieces of work than this follow-up's scope.

`volume-modeler-*` and `process-optimizer-bands` are out of scope: XML-zeroed
across the corpus, they would change zero output samples on shipped tunings.
They are listed here so they don't get re-proposed. `sliding-bass-*` was
recorded dormant on the 2026-05-06 corpus from `max-gain=0` alone. Counting a
non-zero `gain-curve` too, it is enabled with real gain on 63 devices of the
2795-XML corpus; it waits on a capture
([cross-device §14](../cross-device-findings.md#band-a--has-parameters-so-implementable)).

<a id="r-gain-rail-tuning"></a>

## A tuning pinned at the gain rail: the T495 (issue #46)

The ThinkPad T495 report describes the preset as tinny, robotic, and
clipped/overblown at normal volume. Its `--doctor` was clean: 0 FAIL, 0 WARN,
right sink, preset loaded. The arithmetic below is all offline. It explains what
the chain does with this XML and predicts which knob should move which symptom.
None of it has been heard on a device, so the reporter's A/B decides.

**The values are the XML's own.** `gain_l` is `0,192,185,72,17,-18,-115,-187,…`.
The same file declares `<geq_maximum_range value="192"/>`, so 192/16 = +12.0 dB
is the largest gain this file expresses. 141 Hz sits exactly there on both
channels, and 234 Hz too on the right. The 1/16-dB scale is the one the
[simplified-schema AO units finding](eq-and-frequency-response.md#r-simplified-schema-ao-units)
confirmed against DAX captures on a simplified-schema device. No clamp exists on
our side. At 23.7 dB p-p this tuning is wider than 95% of simplified files; see
the corpus context in
[cross-device-findings.md](../cross-device-findings.md#curves-pinned-at-the-declared-gain-range).

The tonal symptom and the loudness symptom have different sources:

- **The midrange hole is in the tuning, not in our filter design.** Relative to
  the 141 Hz rail the curve sits 19.0 dB down at 844 Hz and 23.5 dB down at
  1031 Hz, against −2.1 dB at 3750 Hz. Peak normalisation, `fir /= peak_mag`, is
  a scalar on the impulse response: a constant dB offset at every frequency. So
  it decides only where that spread sits, 0 dB → −23.5 rather than +13 → −10.5,
  and cannot change the shape. A ~20 dB notch through the formant region is
  level-independent. That matches the reporter's note that the character
  persists at an acceptable volume. Normalisation *does* matter downstream. With
  the whole curve anchored 13 dB lower, less signal reaches the regulator's
  thresholds, and `volmax-boost` is what puts the level back.
- **The boost lands where the regulator isn't.** The 141/234 Hz peak takes the
  +9 dB volmax boost (`volmax-boost` 144 on every profile but `off`) straight
  into the −1 dBFS brickwall unprotected. This XML's `threshold_high` is 0 dBFS
  on bands 0–3 and −6.4…−15.4 dB on bands 4–8. So per-band limiting covers
  roughly 469–1313 Hz, exactly the region the FIR already cut by 10–23 dB.
  `isolated_band` marks bands 0–3 non-isolated, i.e. DAX couples them to the
  limited bands. That is the gap the coupled-bands mapping addresses, on by
  default since 2026-08-11 (`--disable coupled-bands` opts out).

The sibling tuning is the control, and it makes this a per-tuning outlier, not a
schema or codec problem. `17AA5081` is the T14 Gen 1 AMD, the T495's successor,
with the same ALC257 and the same simplified schema. It is far gentler in both
revisions we hold. The T495's own driver package carries `tuning_version` 2,
which *cuts* 47/141 Hz by 30 dB instead of boosting. Every newer package in the
corpus carries `tuning_version` 4, which boosts 141 Hz by +6.5 dB for a 14.6 dB
spread.

The file identity trap: the same `SUBSYS` ships *different* tunings in
different driver packages. So "the XML for device X" is not well defined without
naming the package. Issue
[#45](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/45)
reported that machine working from `v7.623.439.38`, which we don't hold. The
packages we do hold bracket it: `v6.108.104.39` and `v9.1127.1236.0` both carry
`tuning_version` 4. So that reporter almost certainly ran the +6.5 dB revision.
That is an inference from the bracket, not a fact, and nothing here should rest
on it.

The T495's own file, by contrast, was never revised. `17AA5125` is
byte-identical in every package we hold, md5 `d678efd7…`, from `v5.204.651.25`
(2019) through `v9.1127.1236.0` (2024) to `v10.1022.826.17` (2025), at
`tuning_version` 50 throughout. So it is a long-lived, heavily-iterated tuning
rather than an early draft. There is no newer Lenovo file for an affected user
to try. The packages we hold:

| package | `17AA5081` | `17AA5125` |
|---|---|---|
| `v5.204.651.25` (the T495's own driver) | `tuning_version` 2 (−30 dB at 47/141 Hz) | 50 |
| `v6.108.104.39` | 4 (+6.5 dB at 141 Hz) | — |
| `v9.1127.1236.0` | 4 | 50 |
| `v10.1022.826.17` | 4 | 50 |

**Nothing else active in this file is silently dropped.** A field-by-field pass
over the built profile found each unmodelled block either switched off or
already accounted for:

- `bass-enhancer-enable`, `bass-extraction-enable`, `graphic-equalizer-enable`,
  `volume-modeler-enable` and `process-optimizer-enable` are all 0. There is no
  DSO field at all.
- The surround stack is on and deliberately not mapped (the
  [surround→stereo-base factor](adaptive-processing.md#r-surround-boost-stereo-base)):
  `surround-boost` +6 dB, decoder, virtualizer angles.
- The `virtual-bass-*` parameters are populated but carry no enable flag in this
  profile. That is the open question in issue
  [#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14).

`regulator-speaker-dist-enable` appears twice with different values: 0 in
`tuning-cp`, 1 in `tuning-vlldp`. `parse_xml` correctly gates on the vlldp one,
so the regulator is read.

The one genuinely unmodelled thing that *is* active is MI steering: all five
`mi-*-steering-enable` flags are set. That matters for profile choice more than
for this device's tone. Per
[cross-device-findings §11](../cross-device-findings.md#11-mi-steering), MI
steering is a `dynamic`-profile feature almost everywhere, in 4205 of 4225
dynamic rows. It is "the key feature that the EasyEffects pipeline cannot
replicate". So our "first profile" default systematically picks the profile
whose Windows behaviour depends most on what we cannot reproduce. It applies
statically what DAX steers by content. On this XML `music` switches all five
off, along with the surround decoder and the dialog enhancer, and drops the
leveler from 7 to 4. That makes it the profile whose static translation is most
faithful. Issue #29's reporter independently preferred `music` on a different
device. That is the real argument for following `<default_profile>`, and it
generalises beyond issue #46.

What shipped from this: the unlimited-boost warning, the `default_profile`
report and the headless EasyEffects probe fix. This XML declares `music`, and
we build `dynamic`. What deliberately did not ship is any knob that
scales or clamps the AO curve, because that would be a hand-tuned offset. The
in-GUI per-effect bypass already brackets the question: switching off
`convolver#0` isolates the curve from the dynamics without a rebuild. If the
curve is confirmed as the cause, the answer is to find what DAX does that we
don't, not a fudge factor.

<a id="r-level-restore"></a>

## Giving back what normalisation removed: `--enable level-restore` (issue #50)

The Yoga 7 2-in-1 16IML9 report describes the preset as quieter than bypass and
thin, with the convolver specifically sounding muffled. Unlike the T495 above,
this tuning is nowhere near the rail: its AO peaks at +8.0 dB. That is what
makes the mechanism visible on its own.

**Which reports this actually covers, and which it does not.** The symptom,
*quieter than expected*, covers two reports. Issue
[#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25)
is quieter than Windows, answered with `--enable autogain`. This one is quieter
than bypass. The T495 of issue
[#46](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/46)
is not in this class and **must not be offered the flag**. That reporter's
symptoms are clipping, tinny and overblown, and they said the level itself was
fine. Their tuning peaks at the +12 dB rail on a band their regulator leaves
unlimited. Restoring it would drive the brickwall harder and make the one
thing they complained about worse. The two reports share a *mechanism* and not
a *symptom*. Conflating them would ship a fix as a regression.

**The arithmetic.** `make_fir(normalize=True)` divides each channel by its own
realised peak, so the emitted response is
`combined(f) − max(combined) + volmax`. Two different numbers fall out of that,
and they are easy to confuse. The shortfall against what the tuning asks for is
the whole peak, uniform across the band. `volmax − peak` is how far a band the
tuning leaves flat sits below *bypass*. On this XML `max(combined) = +9.21 dB`,
at 3750 Hz with `ieq_balanced`, against a `volmax-boost` of `+6.0 dB`. Every
band lands 9.2 dB short of the tuning, and an untouched band plays 3.2 dB below
bypass. That is why the low end ends up under bypass outright:

| | 47 Hz | 469 Hz | 3750 Hz | 19688 Hz |
|---|---|---|---|---|
| tuning + volmax | +7.0 | +0.3 | +15.2 | +5.2 |
| what we emit | −2.2 | −8.9 | +6.0 | −4.0 |

The uniform −9.2 dB is consistent with the ≈−8 dB absolute EE−DAX offset
measured on the dev device in the unvalidated
[PEQ anti-clipping trim](#r-peq-anti-clipping-trim) entry.

**Why normalisation was right when it shipped, and why restoring is defensible
now.** Peak normalisation is not a decision anyone revisited and waved through.
It dates from the very first commit, `9eb5871` on 2026-02-27. At that moment it
was the *only* thing standing between a boost-heavy curve and a clipped output.
Three things have changed since:

- **There was no limiter.** `1b14bc1` added the brickwall the day after,
  2026-02-28. `5973326` the same day forced `convolver#0.autogain` off, killing
  the +50 dB re-normalisation. Restoring level into a chain with no final
  limiter would have clipped; into today's it does not.
- **The peak was twice as big.** Until `eeecc4a` (#12/#13) the converter read
  `ieq-amount` as `amount/10` and applied the IEQ at full weight. On the dev
  device that put the combined peak at +20.1 dB instead of +9.2. So "restore
  the peak" would have meant handing back 10.9 dB more than it does now, on a
  curve that was itself wrong.
- **The boost lands somewhere safer.** `4213d5f` (#23, 2026-06-22) moved
  `volmax-boost` to the regulator's `input-gain`. A static boost now passes the
  per-band limiter before the brickwall rather than after it. The restore
  inherits that placement.

So the original constraint was real and has since lifted. A separate failure
kept it unexamined afterwards, recorded in the absolute-offset note below.

**Why this is not the fudge factor the section above declined.** The restored
amount is `make_fir`'s own returned `peak_db`: the exact scalar it divided out,
derived from the XML curve and nothing else. It is the identity, not a
proportion of it. The removed SoundWire makeup (the
[convolver headroom restore](#r-convolver-headroom-restore)) was
`peak_db * 0.5`, a half-measure whose magnitude tracked the pre-#13 `ieq-amount`
bug. The restore also does not touch the curve's *shape*, which is what "scales
or clamps the AO curve" would have meant.

**Placement.** It rides the same slot as `volmax-boost`, regulator `input-gain`
by default, so the per-band limiter sees it before the brickwall. Issue #23's
dev-device A/B measured that placement at 0.06% THD against 11.6% for the
post-band alternative ([`volmax-boost` slot](#r-volmax-boost-slot)).

**Channel re-referencing.** Normalising each channel to its own peak also
flattens the L/R level relationship the two AO curves ask for. Over the 3051
parsed corpus XMLs, re-derived 2026-08-04, the two combined-curve peaks diverge
on 19.1% of files: median 0.93 dB, p90 2.62, max 5.56. So on roughly one device
in five the default path shifts the stereo balance by an amount the tuning did
not ask for. Under the flag both channels are referenced to the louder peak, so
the relationship survives and no channel exceeds full scale. This is a property
of the *default* path that the flag happens to correct. On its own it is a
candidate fix independent of the level question, and it has not been heard
either.

**The coupled hypothesis — proposed, then measured, then dropped.** The
2026-08-04 capture battery below falsified it. The idea was that
restore-the-level and treat-0 dB-as-a-real-limit were one question, since DAX
applies the boost and catches it with per-band limits at 0 dBFS while we treat a
0 dB `threshold_high` as inactive. So restoring level without
`--enable coupled-bands` would feed the peak band straight into the brickwall,
and the flag would catch it. In that battery, `LRC` and `LR` are identical to
0.00 dB in both fitted gain and residual on every stimulus. `coupled-bands`
alone is bit-identical to the default preset on five of seven. It cannot
mitigate here: after +11.3 dB the in-band levels at the coupled bands are still
under the 0 dBFS threshold that would engage them. The extra level lands on the
final limiter, and nothing upstream catches it. The `boost-unlimited` warning
still fires on that path. The exposure is real, but its `ask` should not be read
as pointing at a fix that works.

Read "dropped" narrowly. What died is *coupled-bands as the remedy for
level-restore's loud-content cost*, on this device, at these levels. The mapping
itself became the default on 2026-08-11 on separate grounds (the
[fixed dynamics constants](#r-fixed-dynamics-constants) (f)). This measurement
is part of why that was safe rather than an argument against it: a mapping that
is bit-identical to the default on most stimuli cannot do much harm when it is
switched on. With the `--enable coupled-bands` flag gone, the `ask` from
`_untamed_boost_ask` offers only `--disable volmax`.

**How a 12 dB offset stayed unexamined for months.** Absolute tooling existed:
`compare_ee_vs_dax.py --absolute` shipped 2026-06-12 (`7aefebd`), and the
[PEQ anti-clipping trim](#r-peq-anti-clipping-trim) names the trap outright:
"the default 1 kHz normalization destroys exactly this observable". That entry
measured the offset twice. Two things buried it anyway:

- **The flagship comparison was normalised.** Issue
  [#12](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/12)
  is the project's most detailed EE↔DAX study. It reports throughout as "EE −
  DAX, RMS dB, 200–18 kHz, **normalized at 1 kHz**". That view is built to find
  *shape* mismatches, and it found a large one: the IEQ over-application, ~28 dB
  at 19.7 kHz. A broadband offset is exactly what it cannot show.
- **"Inseparable" closed the question.** Both absolute attempts were hunting a
  ~3 dB PEQ trim. The 8–11.5 dB offset was written up as the confound burying
  the target, "leveler boost + our −3 dB trim + convolver peak-normalisation,
  inseparable", and no follow-up was scheduled. The word did the damage: it
  turned a measurement into a settled fact.

A switch made the term separable, not a better measurement. An opt-in flag on
one of three entangled terms converts an inseparable sum into a controlled
experiment. When a note says "inseparable", that is a design task, not a
conclusion. `compare_ee_vs_dax.py` now reports the absolute offset
unconditionally, in normalised mode too, so it cannot be mis-filed again.

**Why the warning's default gate did not move.** The obvious widening would warn
whenever the peak lands on a 0 dB-threshold band. As re-derived 2026-08-04, the
widening reaches 54.4% of the 3051 parsed corpus XMLs against the current gate's
10.6%, which is a nag rather than a warning. Leaving the gate alone is safe. On
the default path the peak-normalised band arrives at the brickwall at exactly
`volmax_boost` above bypass whatever its peak, so the AO peak measures spectral
contrast there, not drive. The existing gate has a caveat: only 172 of 3051
files declare `<geq_maximum_range>`, 30 of the 1661 that reach the branch. So
"the boost reaches this XML's full gain range" is in practice a comparison
against our assumed +12.0 dB.

### Measured on the dev device, 2026-08-04

The full battery ran four presets through the live-EE null-sink route: default,
`--enable coupled-bands`, `--enable level-restore`, and both. Device: X1 Yoga G7
(`17AA22E6`), `dynamic`/balanced. Restore is +11.3 dB and volmax +6.0, so the
regulator `input-gain` reads +17.3. The smoke gate passed at −219 dB residual.

**1. The level claim is confirmed, exactly.** Below the limiter the flag is a
pure broadband gain of precisely the amount `make_fir` divided out. Fitted gain
and residual against the default capture:

| stimulus | fitted gain | residual | limiter engaged |
|---|---|---|---|
| `pink_quiet` | **+11.30 dB** | −75.0 dB | no |
| `sweep_quiet` | **+11.30 dB** | −74.7 dB | no |
| `pink` | +11.25 dB | −32.3 dB | yes |
| `stereo_pink` | +11.27 dB | −35.6 dB | yes |
| `stereo_correlated` | +11.25 dB | −32.5 dB | yes |
| `multitone` | +10.65 dB | −21.9 dB | yes |
| `sweep` | +10.58 dB | −17.6 dB | yes |

The two quiet rows are the control: +11.30 dB against a +11.3 dB restore, with
the residual at the capture noise floor. Nothing else changed.

**2. Absolute agreement with DAX improves by an order of magnitude.** EE−DAX
per band on `pink`, absolute rather than normalised, against the archived
Windows captures of the same XML and profile:

| | mean | median | range | mean abs |
|---|---|---|---|---|
| default, ch L | −12.09 | −11.82 | −17.65…−9.28 | **12.09 dB** |
| level-restore, ch L | −0.41 | −0.31 | −4.92…+2.15 | **1.27 dB** |
| default, ch R | −12.10 | −11.84 | −17.69…−9.29 | 12.10 dB |
| level-restore, ch R | −0.42 | −0.14 | −4.92…+2.14 | 1.27 dB |

The ≈−8…−12 dB absolute EE−DAX offset is the convolver's peak normalisation.
This run separates the term the
[PEQ anti-clipping trim](#r-peq-anti-clipping-trim) called inseparable. Giving
the peak back closes the offset to ~1 dB mean error on both channels. The two
worst residual bands are 141 Hz (−3.6) and 328 Hz (−4.9). Both sit inside the
regulator's active range on this XML.

**But only at that input level, and this splits the gap in two.** The
*archived* DAX wavs were analysed before `eq_gain_db_raw` existed, so no
recapture was needed. Re-running the current `analyze.py` over them recovers
absolute data for `pink_quiet` too, at −41.8 dBFS against `pink`'s −17.8 dBFS:

| stimulus (input RMS) | default mean abs | level-restore mean abs | restore mean |
|---|---|---|---|
| `pink` (−17.8 dBFS) | 12.09 dB | **1.27 dB** | −0.41 |
| `pink_quiet` (−41.8 dBFS) | 21.12 dB | **9.80 dB** | −9.80 |

At −41.8 dBFS a **9.8 dB** gap survives the restore. The restore is worth the
same ~11.3 dB at both levels, as it must be for a static gain. The same archive
measures the cause directly, as DAX's own on-versus-off gain (100 Hz–10 kHz,
`dynamic` − `off`):

| stimulus | DAX(dynamic) − DAX(off) |
|---|---|
| `pink` | mean **+7.05 dB** (median +5.62, −0.5…+18.5) |
| `pink_quiet` | mean **+16.39 dB** (median +14.75, +8.8…+27.0) |

DAX rides **+9.3 dB** more gain on quiet content than on normal content. That is
the 9.8 dB residual almost exactly. So the two gaps are independent and
additive. Peak normalisation is a *static* offset that `--enable level-restore`
removes. The volume leveler is a *level-dependent* one that only
`--enable autogain` can address. Neither flag substitutes for the other, and
this is the first measurement that separates them. It also puts a number on what
the
[leveler→autogain window](adaptive-processing.md#r-leveler-autogain-window)
and the
[conservative-autogain offsets](adaptive-processing.md#r-conservative-autogain-offsets)
call invented. On this device DAX's leveler gain is +7.1 dB at −17.8 dBFS and
+16.4 dB at −41.8 dBFS. Those are two real points on a curve our autogain
currently approximates with chosen constants.

**3. The cost is on loud content, and nothing in the chain catches it.** The
limiter absorbs 0.05–0.72 dB. On dense material the drive produces real new
frequency content, not just gain riding. On the multitone, energy at
non-stimulus bins relative to the tones rises from **−44.4 dB to −22.6 dB**.
The `pink` case is mild, with 0.05 dB absorbed. `sweep` and `multitone` are the
worst, as their sustained in-band energy is highest.

### Measured on a second device, 2026-08-04/05

The issue-[#44] device is **`DEV_0287_SUBSYS_17AA380D`**. The reporter's
`--speaker-info` reports `17AA:380D`, and their run matched that XML. Identify a
device by (DEV, SUBSYS), never by the model folder a driver package was
extracted into. The first run of this A/B built `DEV_0230_SUBSYS_17AA3839`, a
different codec, from the same `Yoga-Slim-7-14ARE05` package and measured it
against DAX captures of `17AA380D`. Cross-tuning results are not evidence about
either tuning, and the numbers below replace them. The tell was available
offline: fitting each candidate's static AO+IEQ target against the DAX shape
gives 0.74 dB mean error for `380D` and 4.27 dB for `3839`.

**Its DAX side comes from the reporter's archive.** Those user-supplied captures
carry `off` baselines at both pink levels. The same re-analysis recovers
absolute data from them. Two things come out:

| device | DAX gain @ −17.8 dBFS | @ −41.8 dBFS | slope |
|---|---|---|---|
| X1 Yoga G7 (`17AA22E6`) | +7.05 dB | +16.39 dB | +0.39 dB per dB |
| Yoga Slim 7 14ARE05 (`17AA380D`) | +9.13 dB | +25.25 dB | +0.67 dB per dB |

The gain columns are the 100 Hz–10 kHz mean, as above. The broadband pink-RMS
figure for `17AA380D` is +8.2 / +21.8 dB
([simplified-schema AO units finding](eq-and-frequency-response.md#r-simplified-schema-ao-units));
the metrics differ.

The level dependence reproduces, but **its magnitude and slope are
device-specific**: 0.39 against 0.67 dB per dB. One set of autogain constants
cannot fit both. That is the sharpest argument yet that the
[leveler→autogain window](adaptive-processing.md#r-leveler-autogain-window)
and the
[conservative-autogain offsets](adaptive-processing.md#r-conservative-autogain-offsets)
need measuring rather than choosing, and a reason to run the ladder on more than
one machine.

The peak-versus-volmax mismatch reproduces too, across three tunings. Its size
varies, and on `17AA380D` the tuning's own `volmax-boost` nearly covers it:

| tuning | combined peak | volmax | untouched band vs bypass |
|---|---|---|---|
| X1 Yoga G7 (`17AA22E6`) | +11.4 dB | +6.0 | −5.4 dB |
| Yoga Slim 7 14ARE05 (`17AA380D`) | +8.4 dB | +7.0 | −1.4 dB |
| Yoga 7 2-in-1 16IML9 (#50) | +9.2 dB | +6.0 | −3.2 dB |

**Why another device can be measured here at all.** Neither capture contains
the transducer. The null-sink route taps `ee_soe_output_level`, a digital tap
ahead of the speaker, whose smoke gate reads 0.00 dB gain and −219 dB residual.
The Windows side is a loopback of the engine mix bus. So building someone
else's XML locally and running the battery is a genuine measured A/B of the
*chain*. The only thing it cannot speak to is how that chain sounds through
their speakers. A second device therefore costs their XML plus one archived DAX
battery, not their laptop. It does **not** buy directly comparable absolute
levels; that needs the referencing below.

**Measured on the second device, 2026-08-05.** The `17AA380D` XML was built here
and run through the same battery. Restore on that tuning is +8.40 dB.

| | dev `17AA22E6` | second `17AA380D` |
|---|---|---|
| EE−DAX, default | −11.18 dB | −5.65 dB |
| EE−DAX, `level-restore` | **+0.10 dB** | **+2.08 dB** |
| mean abs error, default → restored | 12.09 → 1.27 dB | 5.65 → 2.95 dB |
| shift achieved vs asked | +11.7 / +11.3 | +7.7 / +8.4 |

The table uses `pink` over 100 Hz–10 kHz, with each DAX side referenced to its
own bypass capture.

**A partial confirmation, not a clean one.** The flag improves absolute
agreement on both devices, but it only *lands* on the dev device. Mean error
falls 12.09 → 1.27 dB on the dev device and 5.65 → 2.95 dB on the second. On
`17AA380D` it overshoots by +2.08 dB, because that tuning's own `volmax-boost`
of +7.0 dB already covers most of its +8.4 dB peak, leaving just −5.65 dB to
recover. Restoring the full peak there is too much by about the amount volmax
was already contributing. The mechanism is sound on both: at the quiet level,
where nothing limits, the second device delivers +8.40 dB against +8.40 asked,
exactly. But "restore the whole peak" is evidently the right *size* only when
volmax is not already compensating. That argues for keeping the flag opt-in, and
hints that the eventual default may need to be `peak − volmax`-aware rather than
the raw peak.

**What the level gap does to the regulator, and what it does not explain.** Our
default chain runs **14.97 dB below DAX** across the four threshold-active
bands, range 12.11–18.70. With the flag that closes to **3.47 dB**, range
0.91–6.33. Re-derived 2026-08-05 from the dev-device `pink` captures by
integrating in-band power between band edges placed at the geometric means of
adjacent centres, and comparing each band's absolute level against its own
`threshold_high`:

| 47 / 141 / 234 / 328 Hz | level (dBFS) | headroom to threshold |
|---|---|---|
| EE default | −59.8 / −34.9 / −27.2 / −34.0 | mean **−30.96 dB** |
| EE `level-restore` | −47.4 / −23.7 / −16.0 / −22.8 | mean **−19.46 dB** |
| DAX `dynamic` | −41.1 / −20.5 / −15.0 / −19.3 | mean **−15.99 dB** |

At 234 Hz specifically the restored chain sits 1.0 dB from DAX. So
`level-restore` does not merely match DAX's output level. It puts our regulator
on roughly DAX's footing, meaning it would begin to engage on roughly the
content DAX's engages on.

**It does not explain the under-engagement** recorded in the
[MBC ratio and time constants](adaptive-processing.md#r-mbc-ratio-time-constants)
and the [fixed dynamics constants](#r-fixed-dynamics-constants). On this
stimulus neither side crosses a threshold. For EE default, EE restored *and*
DAX, 0 of 4 active bands are above threshold. DAX's closest approach is −7.04
dB. A dormant compressor cannot exhibit a ratio. So `pink` at −17.8 dBFS carries
no information either way about the 100:1-realises-as-1.8 finding. That figure
came from a *loud* stepped capture where both sides did engage, and nothing here
touches it. What this does establish is narrower and still useful: at nominal
level our regulator idles 31 dB under its own thresholds. Any attempt to
characterise it on ordinary-level content is therefore measuring silence.

**Caveats on what these runs can and cannot say.**

- Nothing here tests the boost-into-brickwall shape that made #50 report
  crackling. Both measured tunings put their AO peak on a band their regulator
  *limits*, 234 Hz on each. Issue #50's peak sits on one its regulator leaves
  open.
- Both tunings also have channels whose combined peaks are equal. So the L/R
  re-referencing path ran with nothing to correct and stays untested on
  hardware.
- The EE−DAX tables rest on the `pink` stimulus at two levels. `multitone` and
  the sweeps carry no absolute data on the DAX side. The `tones`/`ir` npz store
  normalised amplitudes, so `--absolute` skips them by design, not by omission.

[#44]: https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44

### Heard on the dev device, 2026-08-18

The listening gate closed against the flag. On loud-talking content the
restored preset produces **audible artifacts** on the dev device. Content: a
loudish podcast. Device: X1 Yoga G7 (`17AA22E6`), restore +11.3 dB.

The battery above predicted this content would be worst, and the two agree.
Sustained dense mid-band energy is what drives the limiter. On the multitone
the same flag raised non-stimulus energy from −44.4 dB to −22.6 dB. Point 3 of
that battery, "the cost is on loud content, and nothing in the chain catches
it", is confirmed by ear as well as by number. It holds on the device where the
restore is largest, and where every EE–DAX table above says the *level* is
right.

**What it settles.** Restoring the whole peak cannot become the default. The
flag stays opt-in, as it shipped. Its copy names what was heard rather than
saying nobody has heard it. That is also why `level-restore-active` left
`EXPERIMENTAL_MARKERS` for a finding of its own.

**What it does not settle.** The test covers one device, one kind of content
and one volume setting, which was not recorded. Untested:

- Whether `--disable volmax` clears the artifacts. That is −6.0 dB of drive on
  this tuning, and the mitigation both the README and the `boost-unlimited` ask
  already offer.
- Whether a `peak − volmax`-aware restore stays under the limiter on this
  content. It is the smaller correction the second-device overshoot argued for.

Reports from other devices are still worth having. The restore that closed a
12 dB absolute gap here is +8.4 dB on `17AA380D`, where the tuning's own
`volmax-boost` already covered most of it. So how much extra drive the flag
actually adds varies device to device. A device where it stays clean would say
the size is the problem rather than the idea.

**Status: opt-in, measured on two devices, heard on one, where loud speech
costs audible artifacts.** Default output is unchanged and pinned by the
`level-restore-available-but-off` golden digest. The XML-only bar's
second-device requirement is met, and the listening gate is answered
negatively. So the flag stays opt-in, and a default flip is off the table until
a smaller restore or a mitigation is measured.

## Elsewhere

- The level-dependent half of the loudness gap, DAX's leveler and our autogain:
  [adaptive-processing.md](adaptive-processing.md#r-autogain-bypassed-by-default).
- The MBC side of the loud-level dynamics gap:
  [adaptive-processing.md](adaptive-processing.md#r-mbc-ratio-time-constants).
- The AO units and leveler magnitude the #44 and #46 units build on:
  [eq-and-frequency-response.md](eq-and-frequency-response.md#r-simplified-schema-ao-units).
- Where the brickwall and the regulator sit in the chain:
  [design-notes](../design-notes.md#plugin-chain-order).
- The scaling-factor catalogue:
  [design-notes](../design-notes.md#unvalidated-converter-scaling-factors-the-ieq-amount-class).
- The validation roadmap:
  [measuring-against-windows.md](measuring-against-windows.md#r-validation-roadmap).
- #84's other thread, EasyEffects playing hot above 48 kHz:
  [easyeffects-and-pipewire.md](easyeffects-and-pipewire.md#r-convolver-resample-gain).
