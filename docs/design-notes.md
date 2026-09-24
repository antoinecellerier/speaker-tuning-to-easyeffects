# Design notes

> Interpretive analysis of the parameter schema used by Dolby DAX3 tuning XML (distributed publicly as part of Windows audio driver packages), for the purpose of Linux interoperability. No verbatim tuning arrays are reproduced.

This doc covers the architectural *why* behind the generated EasyEffects preset,
so future readers don't have to reverse-engineer it from commit history.
[reference.md](reference.md) covers *what* the script emits: mappings, plugin
chain, units and what's not implemented.

> **This file and docs/research/ are the research log.** Findings appear in
> roughly the order they were established. Superseded hypotheses stay for the
> audit trail. See the banners below that mark text superseded by the
> [`ieq-amount` scaling finding](#r-ieq-amount-scaling). For the settled
> current-state summary, start with [reference.md](reference.md). For the open
> threads worth picking up, see "Unvalidated converter scaling factors" and
> "Follow-ups to close the gap to DAX" further down.

## Where the research lives

| Class file | What it holds |
|---|---|
| [hardware-and-drivers.md](research/hardware-and-drivers.md) | kernel, codec pins and routing, smart amps, firmware |

The rest of this file is being split by class into `docs/research/`.

### Issues

| Issue | Hardware-and-drivers material | Where |
|---|---|---|
| #18 | single pin, a 2-driver laptop per PSREF | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #27 | amps read as speakers | [hardware-and-drivers.md#r-smart-amp-families](research/hardware-and-drivers.md#r-smart-amp-families) |
| #29 | CS42L43 excluded as a jack codec | [hardware-and-drivers.md#r-amp-parts-rejected](research/hardware-and-drivers.md#r-amp-parts-rejected) |
| #30 | two pins, PSREF names woofers and tweeters | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #33 | kernel 6.12 → 7.0 fix, old-kernel hint | [hardware-and-drivers.md#r-kernel-misconfigured-codec](research/hardware-and-drivers.md#r-kernel-misconfigured-codec) |
| #36 | single pin, a 2-driver laptop per PSREF | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #39 | crackle; rule out the kernel (TAS2781 calibration differs by kernel lineage) | [hardware-and-drivers.md#r-kernel-misconfigured-codec](research/hardware-and-drivers.md#r-kernel-misconfigured-codec) |
| #44 | single pin, a 2-driver laptop per PSREF | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #46 | single pin, a 2-driver laptop per PSREF | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #50 | single pin, a 2-driver laptop per PSREF; its missing `38dc` quirk entry concerns its smart amp, not a bass pin | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #51 | two pins, PSREF names woofers and tweeters | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #53 | hidden woofer pin | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #95 | unlisted machine, firmware mic setting; the EasyEffects crash is under "Rejected approaches" below | [hardware-and-drivers.md#r-fixed-level-speaker-pin](research/hardware-and-drivers.md#r-fixed-level-speaker-pin) |

### Moved sections

| Old heading | Now at |
|---|---|
| Bad sound with a perfect preset: the kernel layer below (issue #33) | [hardware-and-drivers.md#r-kernel-misconfigured-codec](research/hardware-and-drivers.md#r-kernel-misconfigured-codec) |
| Half the speakers, silently: a woofer pin the firmware hides (issue #53) | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| The class next door: pin present, DAC source wrong | [hardware-and-drivers.md#r-speaker-dac-misrouted](research/hardware-and-drivers.md#r-speaker-dac-misrouted) |
| When no table lists the machine (issue #95) | [hardware-and-drivers.md#r-fixed-level-speaker-pin](research/hardware-and-drivers.md#r-fixed-level-speaker-pin) |
| What counts as a smart amp, and which ones we watch for | [hardware-and-drivers.md#r-smart-amp-families](research/hardware-and-drivers.md#r-smart-amp-families) |
| Swept and rejected | [hardware-and-drivers.md#r-amp-parts-rejected](research/hardware-and-drivers.md#r-amp-parts-rejected) |

### Legacy numbers

Commit messages, released CHANGELOG sections and issue comments cite these
numbers, and this table is their resolver. It is frozen: no rows are added.

| Old number | Tag | Heading |
|---|---|---|
| Finding 1 | [`r-dax-lti-behaviour`](#r-dax-lti-behaviour) | DAX3 LTI behaviour for our stimuli |
| Finding 2 | [`r-dax-phase-response`](#r-dax-phase-response) | DAX3's phase response |
| Finding 3 | [`r-dax-response-vs-xml`](#r-dax-response-vs-xml) | DAX3 response vs the published XML curves |
| Finding 4 | [`r-ee-response-vs-xml`](#r-ee-response-vs-xml) | EE-on-Linux response vs the XML |
| Finding 5 | [`r-hf-shaping-block-audit`](#r-hf-shaping-block-audit) | Audit for a missed HF-shaping XML block |
| Finding 6 | [`r-ao-sign-variant-matrix`](#r-ao-sign-variant-matrix) | Testing hypotheses (a) and (b) |
| Finding 7 | [`r-xml-interpretation-hypotheses`](#r-xml-interpretation-hypotheses) | Five XML-interpretation hypotheses |
| Finding 8 | [`r-dax-virtual-bass`](#r-dax-virtual-bass) | DAX runs psychoacoustic VBE; the schema can't drive a per-device mapping |
| Finding 9 | [`r-ieq-amount-scaling`](#r-ieq-amount-scaling) | `ieq-amount` scaling and the HF gap (issue #13) |
| Finding 10 | [`r-simplified-schema-ao-units`](#r-simplified-schema-ao-units) | simplified-schema AO units on a second device (issue #44) |
| entry 1 | [`r-dialog-enhancer-gain-ceiling`](#r-dialog-enhancer-gain-ceiling) | Dialog-enhancer gain ceiling |
| entry 2 | [`r-surround-boost-stereo-base`](#r-surround-boost-stereo-base) | Surround→stereo-base |
| entry 3 | [`r-convolver-headroom-restore`](#r-convolver-headroom-restore) | Convolver SoundWire headroom restore |
| entry 4 | [`r-regulator-slope-ratio`](#r-regulator-slope-ratio) | Regulator slope→ratio |
| entry 5 | [`r-regulator-timbre-knee`](#r-regulator-timbre-knee) | Regulator timbre→knee |
| entry 6 | [`r-mbc-ratio-time-constants`](#r-mbc-ratio-time-constants) | MBC ratio and time constants |
| entry 7 | [`r-leveler-autogain-window`](#r-leveler-autogain-window) | Volume-leveler→autogain window |
| entry 8 | [`r-peq-anti-clipping-trim`](#r-peq-anti-clipping-trim) | PEQ anti-clipping trim |
| entry 9 | [`r-soundwire-bass-enhancer-constants`](#r-soundwire-bass-enhancer-constants) | SoundWire Calf BassEnhancer constants |
| entry 10 | [`r-conservative-autogain-offsets`](#r-conservative-autogain-offsets) | Conservative-autogain offsets |
| entry 11 | [`r-fixed-dynamics-constants`](#r-fixed-dynamics-constants) | Fixed dynamics constants |
| Follow-ups item 1 | [`r-single-block-xml-ab`](#r-single-block-xml-ab) | Stripped-down single-block tuning XML A/B on Windows |
| Follow-ups item 2 | [`r-hybrid-phase-matching`](#r-hybrid-phase-matching) | Match DAX's hybrid phase character |
| Follow-ups item 3 | [`r-dax-leveler-approximation`](#r-dax-leveler-approximation) | Approximate DAX's leveler / regulator |
| Follow-ups item 4 | [`r-fit-to-dax-capture`](#r-fit-to-dax-capture) | Empirically tune the preset to match DAX's *captured* response, not the XML's published curves |
| Follow-ups item 5 | [`r-regulator-stress-amount`](#r-regulator-stress-amount) | `regulator-stress-amount` mapping investigated and rejected |

## Dolby's signal flow: CP → VLLDP

DAX3 splits processing into two stages, which the XML reflects under
`tuning-cp` and `tuning-vlldp`:

```
┌────────────── Content Processing (CP, software) ───────────────┐
│                                                                │
│  Input → Dialog Enhancer → IEQ → Volume Leveler → Regulator    │
│           (MI-steered)     (MI)   (MI-steered)    (CP-level)   │
│                                                                │
└────────────────────────────────┬───────────────────────────────┘
                                 │
                                 ▼
┌─────────── Very Low Latency Driver Path (VLLDP, HW) ───────────┐
│                                                                │
│  → Audio Optimizer → Speaker PEQ → MB Compressor → Regulator   │
│    (speaker corr.)    (biquads)    (dynamics)     (limiter)    │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

- **CP** is content-dependent: dialog enhancement, intelligent EQ and volume
  leveling. Media Intelligence steers it: Dolby analyses the audio content in
  real time and tells these stages when to hold or act.
- **VLLDP** is speaker-dependent: correction curves, per-channel biquads,
  multiband dynamics, and a per-band regulator that clamps specific frequency
  ranges to protect physical drivers.

The generated EasyEffects chain mirrors this split as closely as LV2 plugins
allow.

### Simplified-schema XMLs: `gain_l`/`gain_r` audio-optimizer (issue #22)

`parse_xml`'s audio-optimizer block supports the *simplified* DAX3 schema that
some Lenovo drivers ship, at xml_version ~3.2.x, e.g. ThinkPad X1 Carbon Gen 8.
Two things differ from the full schema:

- **`<audio-optimizer-bands>` names the channels `gain_l`/`gain_r`/`gain_c`/…**
  instead of `ch_00`..`ch_07`. The `gain_*` names follow a 10-channel surround
  layout. They are the same 20-band, 1/16-dB correction arrays, resolved through
  the same `value=`/`preset=` mechanism. For a 2-channel speaker, `gain_l`→left
  and `gain_r`→right. The measured value range matches the full schema's
  `ch_00`/`ch_01`: single-digit dB typical, up to ~30 dB on a worst-case band.
  That match corroborates the shared encoding. **Units and channel assignment
  confirmed on device 2026-07-30**: a DAX capture battery from a
  simplified-schema machine matches the converter's curve to ~0.7 dB mean,
  including the per-channel L/R split (the
  [simplified-schema AO units finding](#r-simplified-schema-ao-units), issue
  [#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44)).
- **No `mb-compressor-*` and no `speaker-peq-*` blocks.** The existing
  enable-gates skip the absent MBC and speaker PEQ gracefully, so the output has
  no `equalizer#0` or `multiband_compressor#0`. The regulator is unchanged, as
  is every `tuning-cp` block: dialog, surround, leveler and volmax.
  `regulator-tuning` is still one threshold per band, so `make_regulator` needs
  no special-casing.

**Gotcha:** a simplified profile carries *two* `<audio-optimizer-bands>`: a
zeroed one under `tuning-cp` and the real correction under `tuning-vlldp`.
`parse_xml` correctly reads the `tuning-vlldp` one. A
`.//audio-optimizer-bands` XPath matches the `tuning-cp` zeros first and will
wrongly read the AO as flat. Match the container (`tuning-vlldp`) explicitly
when inspecting these files.

**What else we don't read — audited (issue
[#22](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/22)).**
A sweep of the simplified corpus (~2,100 profiles) confirms none of the
elements the converter ignores is active, derivable tuning being silently
dropped. Beyond the absent MBC/PEQ, the simplified `tuning-cp`/`tuning-vlldp`
carry many such elements, each in one of three buckets:

- **Gated off in every profile (inert defaults):** `bass-enhancer-*`,
  `bass-extraction-*`, `virtual-bass-*` (`virtual-bass-mode=0`),
  `graphic-equalizer-*`, `volume-modeler-*`, `process-optimizer-*`,
  `dialog-enhancer-ducking` and `height-filter-mode`, all `*-enable=0`. The
  level controls `pregain`/`postgain`/`system-gain`/`calibration-boost` are all
  `0`.
- **Active but not modelable in a static LV2 chain (already out of scope):**
  - The legacy `output-mode-partial-{surround,height}-virtualizer-enable`. The
    surround one was once approximated by `stereo_tools` from `surround-boost`.
    That mapping was removed 2026-06-13: DAX applies no stereo widening on 2-ch
    content (the [surround→stereo-base factor](#r-surround-boost-stereo-base)).
    The FFT-domain part isn't reproducible.
  - The `mi-*-steering-enable` flags: Media-Intelligence real-time content
    steering, with nothing static to bake.
  - `virtualizer-*-speaker-angle`, which is inert geometry without an active
    virtualizer.
- **Already covered via another element:**
  - `regulator-enable` (CP): the regulator is mapped from the VLLDP
    `regulator-tuning`.
  - `ieq-bands-set`: a per-profile selector pointing at one of the IEQ curves
    already read from `<constant>`. We emit all three and don't honour the
    per-profile default.

On bass enhancement, `bass-enhancer-*` and `virtual-bass-*` are not merely
disabled in every profile. `bass-enhancer-enable`/`virtual-bass-mode` are `0`
across all ~38k occurrences in the corpus. Their supporting fields (`cutoff`,
`width`, `mix-freqs`, `src-freqs`, `subgains`) are also *frozen identical*
across hundreds of speaker designs. Unlike the AO / MBC / regulator values, they
therefore carry no per-device signal to derive. The bass enhancement DAX audibly
applies is a non-XML engine baseline, not per-device tuning. The
[DAX virtual-bass finding](#r-dax-virtual-bass) investigates it at length,
including the `--enable-vbe` experiment. Issue
[#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14)
tracks the opt-in baseline. So there is nothing simplified-schema-specific to
add here. The explicit `boost`/`cutoff`/`width` look mappable to Calf
`bass_enhancer`, but being corpus-frozen, they would be a hardcoded baseline
rather than derived tuning.

### Per-channel regulator thresholds: newer SoundWire schema (`SUBSYS_37A317AA`)

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

This second SoundWire schema-variant surfaced in the 2483-XML re-derivation. It
sits in the regulator block rather than the audio optimizer.

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
33,113 other reg-enabled internal_speaker profiles use the flat form and are
untouched. DSO and the advanced virtualizer for this device remain unmodeled
(see cross-device-findings §14), so its preset is still incomplete. The
regulator fix closes the most dangerous gap, not all of them.

## Plugin chain order

`make_preset` in `lib/preset/build.py` builds the chain in this order:

```
Convolver → [Bass Enhancer] → Equalizer (PEQ)
    → Dialog Enhancer EQ → Autogain → MB Compressor → Regulator → Limiter
```

`Bass Enhancer` is emitted only for SoundWire devices (harmonic bass
restoration, the [DAX virtual-bass finding](#r-dax-virtual-bass)). A
`Stereo Tools` widener, mapped from `surround-boost`, used to sit after the bass
enhancer. It was removed 2026-06-13 after a DAX capture showed Dolby applies no
stereo widening on 2-channel content (the
[surround→stereo-base factor](#r-surround-boost-stereo-base)).

Three ordering decisions are non-obvious:

- **Autogain sits before the compressor**, not at the chain end (commit
  `7de8866`). This matches Dolby's CP → VLLDP boundary: the volume leveler is in
  CP, the dynamics stages are in VLLDP, and the VLLDP stages catch overshoot
  from CP. The "autogain always last" EasyEffects convention, followed by
  earlier versions, put the volume leveler downstream of everything with no
  safety net: any post-silence overshoot went straight to the output.

- **A brickwall limiter is appended at the chain end** (commit `1b14bc1`), even
  though the regulator already performs per-band limiting. The explicit LSP
  limiter is redundant on the brickwall-slope devices and essential on the rest.
  Cross-device data (`docs/cross-device-findings.md` §6/§13, 2483-XML cohort)
  shows ~97% of devices use `regulator-distortion-slope=16`, a true brickwall.
  The rest use a softer slope. The original 196-file cohort suggested a 53/47
  split, which the expanded corpus revised.

- **Dialog enhancer runs before the volume leveler** (commit `1709e5d`). Dolby
  boosts speech energy before measuring loudness, so the leveler doesn't
  over-react to dialog-heavy passages.

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
  `multiband_compressor#1.input-gain`, before band limiting, so the regulator
  tames the boosted bass before the brickwall. Fallback: `limiter#0.input-gain`
  when the regulator is absent. `--disable volmax` turns it off.
  `--volmax-slot output-gain` re-routes it after the regulator. That is the
  opt-out, pre-#23 placement, which on loud low frequencies could drive the
  brickwall into distortion. Neither slot is Dolby-derived. Full finding,
  on-device metrics and corpus verdict:
  ["volmax-boost slot" below](#volmax-boost-slot-input-gain-vs-output-gain-issue-23).

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
  [MBC ratio and time constants](#r-mbc-ratio-time-constants) and the
  [fixed dynamics constants](#r-fixed-dynamics-constants)). It barely grabs the
  boost, hence 0 dB loss. On a typical or aggressive regulator, input-gain
  compresses the boost harder: real loudness loss or pumping, which we did not
  measure.
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
regulator whose bands are all threshold ≥ 0 dB. The corner is common, not
exotic: roughly 1 in 7 default runs corpus-wide, half the corpus counting voice
profiles. The prevalence sweep and methodology are in the §15 addendum.

### Why the PEQ `output-gain` stays a single global `max(L,R)` (not per-channel)

Keeping a single global `max(L,R)` resolves cross-device-findings.md "Open
follow-ups" item 2 (asymmetric L/R PEQ peak gain) as *no code change*. The
regression test
`test_peq_output_gain_uses_global_max_across_asymmetric_channels` locks it in.

Every corpus file is an `internal_speaker` per-speaker acoustic correction. The
L and R curves legitimately differ where the two physical speakers do. The
2483-XML re-derivation surfaced this as real per-channel divergence in the
*peak* boost (`corpus_audit`'s L/R peak-asymmetry tally, 2026-06-17): 131
profiles / 10 devices, all on Lenovo convertible/AIO SKUs (ALC257/287), never
the symmetric clamshells. It splits cleanly:

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

## Plugin parameter audit

This table audits every hardcoded knob we ship, to distinguish "we tested this
and it's right" from "we chose this and moved on." The converter sets every JSON
key our generated preset emits explicitly, so no inherited LSP / EasyEffects
default sneaks through. Many of the hardcoded values are **converter-level
judgments** rather than XML-derived. Past LSP-default traps bit exactly there:
convolver `autogain` → +50 dB, MBC upward compression → noise-floor
amplification.

Risk class:

- **AUDIBLE**: a different value would change steady-state magnitude, transient
  behavior, or dynamics character.
- **TOPOLOGY**: affects routing or signal flow but not the in-band magnitude
  under nominal conditions.
- **SAFE**: the choice is constrained to one value (e.g. dithering off on a
  master limiter), or alternatives are obviously inferior.

| plugin | parameter | current | risk | rationale / status |
|---|---|---|---|---|
| convolver#0 | `autogain` | `false` | AUDIBLE | Trap fix (commit `5973326`). LSP default is `true`, which RMS-normalises the FIR and gives a +50 dB boost on our peak-normalised minimum-phase IR. Must stay false. |
| convolver#0 | `ir-width` | `100` | TOPOLOGY | Stereo image width in the convolver's mid/side decode. 100 = pure stereo passthrough. |
| ~~stereo_tools#0~~ | — | (not emitted) | — | **Removed 2026-06-13.** The converter emits no stereo widener; `surround-boost` is not mapped (the [surround→stereo-base factor](#r-surround-boost-stereo-base)). |
| equalizer#0 | `mode` | `"IIR"` | AUDIBLE | Biquad realisation of the per-band PEQ. Alternatives: FIR / FFT / SPM. FFT mode would reproduce the band targets exactly at every FFT bin instead of analytically. Open: candidate test. |
| equalizer#0 | `q-mode` | (none) | AUDIBLE | Resolved (2026-06): the EE 8.x equalizer schema we emit has no separate q-mode key. The Q convention is a property of the per-band filter family (`mode`), covered in the row below. |
| equalizer#0 | per-band `mode` | `"RLC (BT)"` | AUDIBLE | Filter family. Verified for HP-slope behavior (commit `944a8f3`). Bell-width convention: see the note below. |
| equalizer#0 | `split-channels` | `true` | AUDIBLE | Required: the Dolby PEQ is asymmetric L/R on most devices. Linking would force-symmetrise. |
| autogain#0 | `bypass` | `true` (HDA), `false` (SDW) | AUDIBLE | Documented in "Why autogain is bypassed by default": re-enabling reintroduces pumping on quiet→loud transitions. |
| multiband_compressor#0 | `compressor-mode` | `"Modern"` | AUDIBLE | LSP's two compressor algorithms differ in knee shape and ratio behavior. Not measured against the XML's compressor model. Open: candidate test. |
| multiband_compressor#0 | `envelope-boost` | `"None"` | AUDIBLE | A pre-detection EQ tilt. Options include `Pink BT/MT`, `Brown BT/MT`. Open: candidate test. |
| multiband_compressor#0 | `stereo-split` | `false` | TOPOLOGY | Single sidechain across L+R. Dolby's compressor is parameterised globally (one threshold per band, both channels), so a unified sidechain matches. |
| multiband_compressor#0 | per-band `sidechain-mode` | `"RMS"` | AUDIBLE | RMS detection gives smoother level estimation than peak. Reasonable for a music compressor. Not directly tested against Dolby's. |
| multiband_compressor#0 | per-band `sidechain-source` | `"Middle"` | AUDIBLE | Sidechain on `M` of M/S. Could be `"Stereo"` (full stereo image) or per-channel. The choice affects how loud-on-one-side content compresses both sides. Open: not tested. |
| multiband_compressor#0 | per-band `sidechain-reactivity` | `10.0` ms | AUDIBLE | Pre-attack envelope smoothing. LSP default. |
| multiband_compressor#0 | per-band `compression-mode` | `"Downward"` | AUDIBLE | Trap fix (commit `e454711`). LSP default enables upward compression below `boost-threshold=-72 dB`, which amplifies the noise floor during silence. |
| multiband_compressor#1 (regulator) | `sidechain-mode` (limiting band) | `"Peak"` | AUDIBLE | Peak detection on the band that does brickwall limiting, RMS on the others. Matches that band's hard-limit role. |
| limiter#0 | `mode` | `"Herm Thin"` | AUDIBLE | One of LSP's many limiter algorithms. Hermes Thin is a thin-saturation curve. Modern / Classic / Herm Wide variants differ in distortion character. Open: candidate test. |
| limiter#0 | `oversampling` | `"None"` | AUDIBLE | No oversampling. Hard limiting on HF content can alias into-band; 2x or 4x suppresses it but adds latency. Open: candidate test. |
| limiter#0 | `dithering` | `"None"` | SAFE | Off: adding dither here raises the noise floor unconditionally. |
| limiter#0 | `lookahead` | `1.0` ms | TOPOLOGY | Below LSP default (5 ms) but non-zero. Allows correct peak detection without the full-default delay. |
| limiter#0 | `alr` | `false` | AUDIBLE | LSP "auto level release": dynamic relaxation of release time on the limiter. Off keeps behavior predictable. |

- **`stereo_tools#0`.** `emit_stereo_tools` (`lib/pipewire/plugins.py`) stays as
  a translator for any preset that still carries the block.
- **`equalizer#0` per-band `mode`.** Whether Dolby's `q` is cookbook-convention
  is undecided, measured across two DAX sessions.
  - *Bell-width convention*, quantified from LSP source ([Filter.cpp],
    `FLT_BT_RLC_BELL` vs `FLT_DR_APO_PEAKING`). `APO (DR)` is exactly the
    RBJ-cookbook biquad (`α=sin(ω0)/2Q`, reciprocal `√gain` scaling). `RLC (BT)`
    uses a different prototype (`kt = 2√(1+g²)/(1+2Q)`): identical peak gain,
    wider bell at q>1.
  - *Size on the dev-device bells*: realized half-gain Q is 3.43 for q=4.6 (≈25%
    wide), 1.72 for q=2.0 and 1.35 for q=1.5. Max in-band deviation vs cookbook
    is 0.58 dB (q=4.6) and ≤0.23 dB (q≤2).
  - *DAX sessions*: fitting the RLC−RBJ signature to the EE−DAX pink residual
    (150–800 Hz) on `dynamic`/`movie`/`game` gives a≈0.78/0.76/0.95 (2026-06)
    and a≈0.71/0.71/0.90 (2026-06-13 Windows session). That is consistent across
    sessions and leans cookbook, but the signature (0.23 dB rms) explains only
    ~2% of the ~1.1 dB voicing residual. A stepped-tone check is *confounded*:
    DAX's leveler adapts per held tone, ±3 dB ≫ the 0.43 dB bell signature. The
    convention delta is smaller than the dev device's content-adaptive
    variability, so settling it likely needs a device with higher-Q /
    higher-gain bells.
  - *Candidate fix* if cookbook is ever confirmed: emit bells as `APO (DR)`,
    with HP staying `RLC (BT)` (verified); the second-device bar applies.
  - *Offline model*: `compare_ee_analytical.py` models bells as RBJ, so the
    offline model and the live plugin disagree by up to the 0.58 dB above. That
    is part of the vsXML baseline, not a DAX-side effect.
- **`multiband_compressor#0` `envelope-boost`.** A primitive analog to Dolby's
  MI steering: it could shape compressor response on content where, with `None`,
  it engages flat.

Rows flagged "Open: candidate test" are the active audit surface. A
measurement-backed conclusion updates its row with the residual numbers and the
decision: kept, changed, or documented trade-off.

### Recorded contradiction: "nothing takes look-ahead" vs. two 1 ms sites

The mechanism clause of the latency invariant is false as written: two sites in
the shipped chain ask for 1 ms of look-ahead. The *constraint* is not in
question. `CLAUDE.md` states the invariant as a constraint plus a mechanism:
"**Zero added latency** over the PipeWire quantum is a hard constraint (video
lip-sync, interactive use), so the FIR stays **minimum-phase** and nothing in
the chain takes look-ahead". `.claude/rules/dsp-fir.md` puts the second half
harder still: "nothing in the output chain may spend any".

- `make_limiter` (`lib/preset/plugins.py`) writes `"lookahead": 1.0` on
  `limiter#0`. `emit_limiter` (`lib/pipewire/plugins.py`) carries it to the
  filter-chain conf as `lk`.
- `make_regulator` writes `"sidechain-lookahead": 1.0` on every in-zone band of
  `multiband_compressor#1`, with the comment "1 ms head start for transients".
  `emit_mb_compressor` translates it to `sla_N`.

Neither is an LSP default riding through. The music compressor's per-band dict
in `make_multiband_compressor` writes `0.0`. So does the shared band-off
template `_disabled_band`, where it is inert because the band is off. The
regulator's `1.0` is therefore a choice made for that plugin. The audit table
above has no row for it at all.

Three of our own doc claims disagree about what that 1 ms is:

1. **A deliberate trade-off, recorded as one.** The `limiter#0` / `lookahead`
   row above classes 1.0 ms as TOPOLOGY: "Below LSP default (5 ms) but non-zero.
   Allows correct peak detection without the full-default delay". On that
   reading we chose a *smaller* delay, not no delay.
2. **Look-ahead is the latency.** "Translating active autogain to LSP
   `autogain_stereo`" below pins `lkahead` to `0.0` because "`lkahead=0` keeps
   it at zero over the PipeWire quantum (the hard constraint)". The comment at
   that line in `emit_autogain` calls lookahead "the only latency source (port
   41)". On that reading, non-zero look-ahead is exactly what spends latency,
   and the two sites above spend it.
3. **Not ours to answer for.** `.claude/rules/dsp-fir.md` says the limiter's
   `lk` "is whatever the EasyEffects preset already carried rather than a value
   we chose". That holds only from `ee_to_pipewire.py`'s vantage, where the
   preset is an input. This repo *writes* that preset, in `make_limiter`, so at
   the project level the value is ours. The sentence is a third position in the
   disagreement, not a resolution of it.

**What the measurements on record do and don't cover.** Nothing measured here
currently bears on the mechanism clause either way. The 2026-06-22 EE-vs-PW
proof below reports identical capture onsets at 0.30 s. It ran an
*autogain-only* preset with every other stage stripped from `plugins_order`. The
limiter and both MBCs were not in that chain, so it cannot speak to either site.
The full-chain capture from the same session, and the EE↔PW equivalence
residuals in `docs/ee-to-pipewire.md`, are EE-*against*-PW comparisons in which
both sides carry the same look-ahead. They would catch a relative delay between
the two paths, not an absolute one against bypass.

**What would settle it**, named as evidence, not scheduled as work:

- `lv2info` on the LSP limiter and MBC URIs: whether either declares a latency
  output port, and what it reports at `lk=1.0` / `sla_N=1.0` against `0`.
  `lib/pipewire/checks.py` already shells out to `lv2info` for conf validation,
  so the tool is a stated dependency rather than new apparatus.
- Whether LSP's MBC `sidechain-lookahead` delays the main path or only the
  detector. Port metadata plus a measured impulse position would answer it. The
  capture route exists but sits behind the `/audio-validate` gate and an audio
  handoff.
- Live node latency from `pw-top` / `pw-cli` on a loaded chain, and EasyEffects'
  own per-plugin latency readout, at 1 ms against 0.

**A candidate reconciliation, explicitly unverified.** 1 ms is well inside a
typical PipeWire quantum: 256/48000 ≈ 5.3 ms, 1024/48000 ≈ 21 ms. The
perf/equivalence rigs documented here ran at 1024 / 48 kHz. If the look-ahead is
absorbed within a period the node is already being called with, then "zero added
latency *over the quantum*" and "takes 1 ms of look-ahead" are both true, and
only the clause's wording is wrong. If it is added on top, the constraint itself
is at stake. That is the *shape* an answer could take. It is not a finding, and
none of it has been measured.

Recorded 2026-08-08 by decision, with the fix deferred: no code, no invariant
wording, and none of the three claims above were changed.

### Measurement outcome: dynamics plugins on the test stimuli

The dynamics plugins (limiter, MBC, regulator) are passive at our nominal
stimulus levels. A reduced A/B sweep compared current against
`limiter mode = "Herm Wide"` + `oversampling = "Full x4/24 bit"`, captured
against `Dolby-Dynamic-Balanced` on multitone / pink / sweep / sweep_quiet /
pink_quiet. EE-vs-XML-target residual:

| variant | EE-vs-XML rms | EE-vs-XML max |
|---|---:|---:|
| current (Herm Thin / None) | 0.94 dB | 4.40 dB |
| Herm Wide / Full x4/24 bit | 0.93 dB | 4.39 dB |

The two variants agree to **0.01 dB RMS**, within measurement noise. A
level-budget analysis predicted this outcome. Every captured stimulus peaks at ≈
−10 dBFS, well below the limiter's ~ −1 dBFS threshold and below most MBC band
thresholds in the test XML. So the current pink-noise / multitone test rig
cannot characterise the parameters in the audit table flagged as "AUDIBLE" but
living in those plugins: `limiter mode/oversampling`, `MBC compressor-mode`,
`MBC envelope-boost`, `MBC sidechain-source/mode/reactivity`. They affect
transient and loud-content behavior. Characterising that needs a different test
stimulus, e.g. clipping-engaging sustained tones, or live program material with
peak detection. That is out of scope for this audit, which focuses on
frequency-domain fidelity at nominal levels.

The measurement also reads off the EE-vs-XML baseline. At 0.94 dB RMS / 4.4 dB
max in-band residual on `Dolby-Dynamic-Balanced`, the FIR + biquad chain
reproduces the curve our converter intended: the DSP math executes correctly.
`vsXML` is internal consistency, not interpretation correctness; the "note on
metrics" in the
[XML-interpretation hypotheses](#r-xml-interpretation-hypotheses) explains why
this distinction matters. The 11.84 dB EE-vs-DAX residual recorded in the
[AO sign variant matrix](#r-ao-sign-variant-matrix) is therefore not
implementation drift. At the time of this measurement it was attributed, per the
[AO sign variant matrix](#r-ao-sign-variant-matrix) and the
[XML-interpretation hypotheses](#r-xml-interpretation-hypotheses), to fixed
DAX-internal behavior outside the published XML. The
[`ieq-amount` scaling finding](#r-ieq-amount-scaling) later showed it was
dominated by the converter's own `ieq-amount` scaling error, since fixed. The
remaining EE-vs-DAX residual is ~1 dB RMS at HF plus the LF/leveler gap.

For the rows marked "open" in the table above (MBC and limiter character knobs),
defaults are safe at nominal levels. Revisit them if a future investigation
focuses on transient or peak-engaging content.

## Why autogain is bypassed by default

The EasyEffects autogain is configured from Dolby's `volume-leveler` parameters
(target, history window, reference) but shipped with `bypass: true` by default
(commit `19a1f99`). Three reasons:

1. **Dolby's volume leveler is MI-steered.** The XML enables
   `mi-dv-leveler-steering-enable` only on the `dynamic` profile
   (`docs/cross-device-findings.md` §11). So Dolby analyses content to
   hold gain during silence rather than continuously pumping it up.
2. **EasyEffects autogain has no content awareness.** It treats silence as "too
   quiet" and cranks gain up over its integration window (10–30 s). When loud
   content arrives after silence, the first 400 ms–3 s of EBU R 128 integration
   still run with the "quiet-period" gain → audible saturation / pumping.
3. **Bypassing is better than guessing.** Commits `67ac464` (−23 LUFS target)
   and `ec78b0d` (longer history window) softened the effect, but neither fixes
   the root cause. Shipping bypassed keeps the settings available for users who
   want to enable it manually without re-running the script.

Field confirmation: issue
[#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25)
(ThinkPad E14 Gen 2 AMD, HDA). With autogain manually enabled, the reporter
heard crackle exactly and only on short system event sounds arriving after
silence: reason 2's quiet→loud case. The mitigation is raising
`silence-threshold` toward the −50 dB the conservative SoundWire path ships.
Since the 2026-07 flip attempt below, the HDA block also stores −50 dB, so
enabling by flag or GUI gets the fix without hand-editing. Before that it kept
EE's −70 dB plugin default, which was never a schema hypothesis, just "keep the
plugin default".

Field note: issue
[#36](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/36)
(Lenovo IdeaPad Pro 5 14IMH9, HDA). The reporter recommends manually enabling
autogain on this device. No further detail (root cause, specific content) has
been captured yet, so this isn't a second confirmation of the issue
[#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25)
mitigation, just a pointer for whoever investigates next.

<a id="the-2026-07-default-flip-attempt-measured-then-rejected-issue-25"></a>
### The 2026-07 default-flip attempt (issue #25)

The on-device listening gate rejected flipping the HDA default to enabled. The
attempt followed the #25 reporter's confirmation on 2026-07-27 that enable + −50
dB gate ≈ Windows loudness on the E14, with no crackle re-report. The listening
pass ran on a ThinkPad X1 Yoga Gen 7 (ALC287 HDA), the device whose artifacts
motivated the original bypass. Null-sink captures of the full chain:

- **The gate fix is real.** Over a 30 s −60 dBFS noise floor between the two
  gates, the leveler wound up +41.8 dB at gate −70 vs +1.7 dB at −50. A −6 dBFS
  notification burst after the floor came out pinned at the −1 dBFS brickwall
  ceiling under −70, vs ~4 dB below ceiling under −50. The pinned case is heavy
  transient limiting, i.e. the #25 crackle. Neither case clipped digitally,
  which reconfirms the "quality, not safety" verdict below.
- **The loudness win is real too.** Enabling gained ~+9 dB vs bypassed on −20
  dBFS RMS program material.
- **But reason 2 has a second case the gate cannot fix.** On a speech-pattern
  stimulus (−38 dBFS background + intermittent −18 dBFS speech bursts), the
  leveler boosts the *legitimate* quiet background by ~14 dB. That background
  sits above any sane silence gate. Each voice onset rides ~4 dB of overshoot
  into the MBC/regulator. `maximum-history` is **not a reaction-speed lever**:
  histories of 20/32/40 s all measured 3.9–4.2 dB overshoot, because the
  Geometric Mean (MSI) reference reacts through its Momentary component
  regardless of history. Dropping the target to −23 traded ~3 dB of loudness for
  no overshoot improvement (5.7 dB onset-vs-tail delta).
- **Listening verdict (adoption gate): rejected.** On real content, a stream
  with low background and intermittent loud speech, the overshoot is audible
  saturation on the loud onsets: the same artifact class that motivated
  the original bypass, now with numbers.

Shipped instead: the HDA block stores `silence-threshold: −50` even while
bypassed. `--enable autogain` activates the leveler without GUI edits for users
who want the loudness (the E14 case). The conservative SoundWire branch is
unchanged. Net: the crackle failure mode is fixed for everyone who enables the
leveler. The quiet-background overshoot is structural to EE's
non-content-aware autogain and keeps the HDA default bypassed.

The measurement protocol is committed as
`tools/measure_ee/autogain_dynamics.py`, with crackle and speech protocols and
self-generated preset variants. Rerun it before changing any autogain default,
or when EE/LSP leveler behaviour changes upstream.

## Translating active autogain to LSP `autogain_stereo` (PW converter)

The volume leveler *can* be reproduced in the PW filter-chain, so the "no LV2
equivalent" rationale that kept `ee_to_pipewire.py` from translating a
non-bypassed autogain was stale. LSP's `autogain_stereo` is a K-weighted (LUFS)
loudness AGC. It uses the same EBU R 128 weighting as EE's native libebur128
autogain. Active autogain is emitted only on SoundWire devices, because HDA
bypasses it (see "Why autogain is bypassed by default" above). This closes a
gap that only ever affected the SoundWire PW path. The bypassed-HDA case is
skipped silently.

`emit_autogain` maps the EE block from `make_autogain`'s conservative path onto
these ports:

| EE autogain field | `autogain_stereo` port | Value |
|---|---|---|
| `target` (LUFS) | `level` | direct, clamped [−60, 0] |
| `silence-threshold` (dB) | `silence` | direct, clamped [−84, −36] |
| EBU R 128 weighting | `weight` | `5` (K-weighted) |
| (latency constraint) | `lkahead` | `0.0` |
| `maximum-history` (s) | `tfall_l` (ms) — gain *down* | `maximum-history · 200 ms/s`, clamped [10, 10000] |
| `maximum-history` (s) | `tgrow_l` (ms) — gain *up* | `maximum-history · 500 ms/s`, clamped [10, 10000] |

`level`/`silence` are dB-domain ports passed directly, not linear gains, so
there is no `db_to_lin`. Applying `db_to_lin` here is the easy bug: contrast the
limiter's `th`. EE `input-gain`/`output-gain` are always 0.0 and have no
main-path port (`preamp` is sidechain-only), so they're structurally identity
and not written.

`autogain_stereo` reports latency only through its lookahead; `lkahead=0` keeps
it at zero over the PipeWire quantum (the hard constraint).
`validate_conf.py`/lv2info confirms all six emitted controls are in range.

EE's `maximum-history` is a libebur128 *integration window* in seconds: 15–40 s
on the active SoundWire path (the unvalidated
[leveler→autogain window](#r-leveler-autogain-window)). `autogain_stereo` has no
equivalent window port, since `lperiod` caps at 2 s. A longer EE history
therefore maps monotonically onto a slower, gentler gain ride via the gain
time-constants `tgrow_l`/`tfall_l`. The on-device proof below showed EE's
leveler is asymmetric: it attenuates loud content quickly but boosts quiet
content very slowly (anti-pumping). The two directions therefore get different
global scales: `tfall_l = maximum-history · 200 ms/s` for gain down and
`tgrow_l = maximum-history · 500 ms/s` for gain up. Each is a single
device-independent transfer, so XML-derivable with no per-device tuning, clamped
to the [10, 10000] ms port range.

**Verdict: adopt, on by default.** The on-device proof below shows the PW
translation reproduces the leveler faithfully on the two properties that
determine loudness delivery, target convergence and silence gating, and on
attenuation dynamics. The only residual is a ≤1.4 dB faster boost on sustained
quiet content, capped by the LSP grow-time ceiling. Related unvalidated EE-side
autogain scalings: the [leveler→autogain window](#r-leveler-autogain-window) and
the [conservative-autogain offsets](#r-conservative-autogain-offsets).

On-device proof: 2026-06-22, EE-vs-PW, X1 Yoga ALC287 HDA rig. The preset was
*autogain-only*: every other stage was stripped from `plugins_order`, so only
the leveler acts, with no convolver/MBC/limiter confounds. It used
`target=-22 LUFS` and `maximum-history=20 s`. The stimulus, a
loud(-16)→quiet(-34)→loud(-16)→silence pink battery, played through both the
live EE chain and the PW filter-chain rendering of the *same* preset. Captures
went through the `tools/measure_ee` + `tools/measure_pw` null-sink route and
were compared as output integrated-LUFS / RMS-envelope trajectories. Reproduce
with
[`tools/measure_pw/autogain_proof.py`](../tools/measure_pw/autogain_proof.py)
(`build` → `capture --side {ee,pw}` → `analyze`). Captures stay untracked under
its `--out-dir`. Results:

- **Loudness target: exact.** Both chains settled the loud segments to −22.00
  LUFS (= target) and matched each other to 0.2 dB RMS. Validates `level`.
- **Silence gate: identical.** Both fully gated silence (−199.9 dBFS), with no
  noise-floor boost. Validates `silence`.
- **Attenuation: matched.** Loud-content gain reduction agreed (~−8.5 dB, 0.2
  dB apart) at `tfall_l`=4 s (history 20 s · 200 ms/s).
- **Boost: close, port-limited.** Over the 12 s quiet segment EE boosted +2.1
  dB. PW boosted +3.5 dB after splitting grow from fall and pushing `tgrow_l` to
  its 10 s ceiling. A symmetric 4 s PW grow had given +9.9 dB. The residual
  EE−PW ≈ 1.4 dB (0.4 LUFS on the settled tail) errs slightly *faster*, because
  LSP's 10 s `tgrow_l` ceiling cannot reach EE's ~50 s effective grow, a
  bounded, documented divergence.
- **Zero added latency: confirmed.** EE and PW capture onsets were identical
  (0.30 s). The autogain node adds no latency over the quantum (`lkahead=0`,
  also lv2info-confirmed).

A full-chain clipping check (2026-06-22) asked whether this re-introduces the
HDA distortion. It was a second EE-vs-PW capture on the *full* HDA chain (steep
IEQ+AO convolver → … → autogain → MBC → regulator → limiter@−1 dBFS) with
autogain force-enabled, recreating the exact scenario that motivated the HDA
bypass ("Why autogain is bypassed by default" above).
Stimulus: 6 s settle → 18 s deep-quiet (−45 dBFS, gain ramps up) → hard loud
onset (−8 dBFS, leftover gain overshoots). Findings:

- **No clipping on either chain.** Both EE and PW produced zero full-scale
  samples, with the peak pinned at −1.00 dBFS. The brickwall limiter holds the
  output regardless of autogain. So the HDA-bypass motivation is loudness
  pumping, not digital clipping: the leveler over-boosts quiet content, then the
  onset slams the limiter/MBC. This refines the hedged "saturation/pumping"
  wording above.
- **PW does not worsen it; it's gentler.** On the loud-onset transient EE≈PW:
  both −1.00 peak, ~0 % ceiling-pinned, crest 14.8 vs 14.9 dB. On the
  deep-quiet boost PW applied *less* gain than EE (+12.5 vs +18.4 dB), the
  reverse of the isolated near-target result. The cause is LSP autogain's
  `drift` dead-band (12 dB default), which stops correcting within ~12 dB of
  target. On very-quiet content (>12 dB below target) PW therefore under-boosts
  vs EE's full correction. Less boost leaves less leftover gain and less
  overshoot, so PW is no harsher than EE in the worst case.
- **Non-monotonic vs EE.** PW≈EE near target (isolated test) and PW<EE far below
  it (drift dead-band). Bounded both ways.

Net: translating autogain adds no clip risk. The HDA default-bypass stays a
*quality* (anti-pumping) choice, not a safety one. This work leaves it
untouched, since it only translates the already-active SoundWire case, where
quiet is typically near target and the drift dead-band barely bites. The
`drift`/`max_amp` knobs, left at LSP defaults, are the levers if PW's deep-quiet
tracking is ever revisited. Driver:
[`tools/measure_pw/autogain_fullchain.py`](../tools/measure_pw/autogain_fullchain.py).

## Verified math (sanity checks)

These sanity-checks are the derivations and accuracy measurements behind the
values catalogued in [reference.md](reference.md).

Q15 block-rate time constants (MB compressor attack/release coefficients) are
stored as exponential smoothing coefficients operating per block: 256 samples at
48 kHz = 187.5 blocks/sec. Decoded via:

```
tau_seconds = -1 / (blocks_per_sec * ln(coeff / 32768))
```

Verified against the standard first-order LPF time-constant derivation. For the
development device (ALC287 22E6):

| Band | Attack raw | Release raw | Attack ms | Release ms |
|------|-----------|-------------|-----------|------------|
| 0    | 24080     | 32123       | 17.3      | 268.3      |
| 1    | 22641     | 30810       | 14.4      | 86.6       |

These are reasonable values for a two-band music compressor.

FIR accuracy: the minimum-phase cepstral method that generates the IEQ +
audio-optimizer impulse response is exact on its 4096-point design grid, with
error < 1e-6 dB. Design and evaluation share that grid, though, so that number
is circular by construction. Evaluated at the exact 20 Dolby band-center
frequencies, the error is **≤ 0.06 dB**, all of it FFT-bin quantization. For
example, the 47 Hz center snaps to the 46.875 Hz bin on a steep per-band slope.
This is consistent with the
[`ieq-amount` scaling finding](#r-ieq-amount-scaling)'s ~0.06 dB figure. The FIR
is properly minimum-phase: 100% of the energy is in the first half of the 4096
taps. It has no significant tail ringing and extrapolates flat beyond the band
edges. 4096 taps (~85 ms at 48 kHz) is sufficient for 20-band EQ correction.

FIR time-domain envelope: Dolby-Balanced, Dynamic, X1 Yoga Gen 7, channel L,
peak-normalized. Reproduce with `tools/measure_ee/compare_ir_time_domain.py`.

|                              | converter FIR (`Dolby-Balanced.irs`) | EE-captured | DAX-captured |
|------------------------------|---:|---:|---:|
| total samples (file)         | 4096 (85.3 ms) | 8192 (170.7 ms) | 8192 (170.7 ms) |
| 95% cumulative energy        | peak + 1.15 ms | peak + 2.79 ms | peak + 1.29 ms |
| 99% cumulative energy        | peak + 5.50 ms | peak + 7.21 ms | peak + 3.62 ms |
| 99.9% cumulative energy      | peak + 11.19 ms | peak + 13.77 ms | peak + 8.21 ms |
| envelope first &lt; −60 dB   | peak + 19.94 ms | peak + 23.29 ms | peak + 11.40 ms |
| envelope first &lt; −80 dB   | peak + 49.88 ms | peak + 51.19 ms | peak + 23.15 ms |

The converter FIR and the EE-captured IR have nearly identical decay profiles,
as expected, since EE *is* the convolver applying that FIR. The DAX-captured IR
decays roughly 2× faster (−60 dB at 11 ms post-peak vs ~22 ms). What looks like
a "long" loopback IR in a stereogram view is the −60 to −100 dB tail. On a
log-envelope scale, the post-peak tail of all three IRs falls below the audible
threshold within ~25–50 ms.

The 99% cumulative-energy time (peak + 5.5 ms for the converter FIR) is what
matters for "where is the impulse-response actually doing work." The remaining
~80 ms of the 4096-tap file is the natural decay of the lowest-frequency biquads
in the cepstral construction. A 100 Hz HP at Q ≈ 0.7 has a several-ms
time-constant, and the trailing &lt;−60 dB samples encode its asymptotic decay.
Trimming earlier than that loses LF accuracy, not visible "blank space."

## Empirical comparison vs DAX3 on Windows

Issue
[#11](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/11)
asked how the FIR our converter generates from the XML compares with what
Dolby's DAX3 does on Windows. The XML is magnitude-only, with no phase reference
for the combined IEQ + audio-optimizer response, so the question can only be
answered empirically.

`tools/measure_dax/` plays a stimulus through the speaker output, captures the
post-DAX3 signal via WASAPI loopback, and analyses the result. Its Linux-side
counterpart, `tools/measure_ee/`, runs the same stimulus battery through a live
EasyEffects instance with our generated preset and produces analyzer-compatible
captures. The EE-on-Linux and DAX-on-Windows responses can then be overlaid for
the same XML and profile. Five stimulus kinds:

- **sweep** (exponential 20 Hz–22 kHz, −18 dBFS peak): Farina deconvolution
  recovers an LTI IR if the system is LTI.
- **sweep_quiet** (−42 dBFS peak): the same sweep at a much lower input level.
- **pink / pink_quiet**: stationary pink noise, for the steady-state magnitude
  after the leveler settles.
- **multitone**: 20 pure tones at the Dolby band centers, for per-band
  amplitude *and phase* via single-bin DFT.

The nine findings from [DAX LTI behaviour](#r-dax-lti-behaviour) through the
[`ieq-amount` scaling finding](#r-ieq-amount-scaling) come from a ThinkPad X1
Yoga Gen 7 with a Realtek ALC287, subsystem 17AA:22E6, which matches the
development tuning XML, `DEV_0287` keyed `SUBSYS_17AA22E6`. The
[simplified-schema AO units finding](#r-simplified-schema-ao-units) covers a
second device, a Yoga Slim 7 14ARE05 with the simplified schema, whose battery
arrived 2026-07-30 via issue
[#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44).

<a id="r-dax-lti-behaviour"></a>

### DAX3 LTI behaviour for our stimuli

The volume leveler and regulator engage during capture and apply time-varying,
content-adaptive gain:

- The 100 ms RMS envelope of the swept-sine capture varies by 16–34 dB from
  start to end of the sweep, depending on profile, against a flat ±0 dB on the
  OFF baseline. The leveler boosts late-sweep portions where the input fade-out
  drops the level.
- Multitone clipped on 4 of 6 profiles, dynamic, movie, music and game: the
  peak hit 0 dBFS with up to 113 clipped samples. The regulator engaged as a
  hard limiter even at −18 dBFS RMS input.
- The −18 dBFS sweep peaked at ~−0.5 dBFS on the same four aggressive profiles.
  At −42 dBFS the leveler is *more* aggressive, not less, because it targets a
  fixed loudness and brings quiet content up.

The recovered "IR" is therefore not a true linear impulse response: Farina
deconvolution conflates frequency response with the time-varying gain applied
during the sweep. A clean LTI characterization of DAX3 needs either the
leveler and regulator disabled, which Dolby Access doesn't expose, or
continuously-stationary stimuli that give the leveler a fixed level to settle
on. The pink stimuli do the latter.

<a id="r-dax-phase-response"></a>

### DAX3's phase response

Every DAX3-on profile sits between linear-phase and minimum-phase. The metric is
the post-peak vs pre-peak energy ratio of the sweep captures, channel L: pure
minimum-phase would be +∞ dB, linear-phase ~0 dB.

| profile | sweep (−18 dBFS) | sweep_quiet (−42 dBFS) |
|---------|-----------------:|-----------------------:|
| OFF     |  +0.0 dB (linear) |  +0.0 dB (linear) — bandlimited Dirac, expected |
| dynamic | +14.6 dB | +18.7 dB |
| movie   | +10.4 dB | +18.1 dB |
| music   | +15.7 dB | +19.4 dB |
| game    | +10.0 dB | +17.8 dB |
| voice   |  +8.6 dB |  +8.8 dB |

Voice is closest to linear-phase, at +8.6 dB. That is likely a deliberate choice
for speech, where flat group delay preserves consonant transients. The
sweep_quiet variant looks more min-phase-like across profiles, most plausibly an
artifact of the leveler's asymmetric response to a quiet sweep rather than a
real phase shift.

The same caveat applies, in lesser degree, to the absolute ratios. The sweeps
carry time-varying gain ([DAX LTI behaviour](#r-dax-lti-behaviour)), which
Farina deconvolution folds into pre- and post-peak energy, biasing the ratio
toward looking linear-phase. If this characterization ever becomes load-bearing,
the multitone capture's per-band phase is the cleaner signal. No converter
decision rests on it.

So our generated FIR cannot match DAX3's exact phase behaviour in any profile.
The no-added-latency constraint forces min-phase regardless of this finding.
Minimum-phase is the right trade-off for an EQ correction filter, and we accept
that this diverges from Dolby's choice.

<a id="r-dax-response-vs-xml"></a>

### DAX3 response vs the published XML curves

> **Superseded in part by the
> [`ieq-amount` scaling finding](#r-ieq-amount-scaling).** These captures
> predate the `ieq-amount`/100 correction, and the hypothesis list below omits
> the branch that later proved true: a field we *do* read but mis-scaled. The HF
> residual described here was largely the converter's own interpretation error,
> not a DAX-side stage.

The captured DAX3 response is genuinely far from what our FIR predicts, and the
bulk of the residual sits at HF (>5 kHz). The table gives each profile's
captured spectrum minus the frequency response of *its own* balanced FIR target,
as the between-band magnitude residual `RMS / max` in dB on a 200-point log grid
(47–19688 Hz):

| profile | sweep | sweep_quiet | pink | pink_quiet |
|---------|-------|-------------|------|-----------:|
| dynamic | 9.2 / 31.4 dB | 6.9 / 24.2 | 7.2 / 27.1 | 7.5 / 25.9 |
| movie   | 11.9 / 37.2 | 7.3 / 25.4 | 7.5 / 28.1 | 7.6 / 26.3 |
| music   | 7.3 / 21.6 | 5.1 / 19.6 | 5.9 / 20.4 | 6.5 / 20.4 |
| game    | 11.8 / 37.2 | 7.3 / 24.6 | 7.5 / 28.1 | 7.8 / 26.7 |
| voice   | 9.8 / 30.8 | 9.6 / 30.2 | 9.6 / 31.8 | 9.9 / 33.1 |

The reference is the IEQ + AO FIR only. It excludes the XML's own speaker-PEQ
block, a 100 Hz 4th-order HP plus ±3–4 dB bells at 280/400/516 Hz, which DAX
presumably also applies. Below ~100 Hz the XML itself mandates up to ~26 dB of
deviation from this reference, so part of the RMS/max figures at the 47 Hz end
of the grid is expected by construction. The HF conclusions are unaffected,
since the speaker-PEQ has no content above 516 Hz.

The gap is not a measurement artifact. The synthetic LTI test, which applies our
FIR to the stimulus, deconvolves and compares to the original, recovers within
0.06 dB RMS / 0.36 dB max, two orders of magnitude tighter.

At 19688 Hz the captured magnitude is typically 20–40 dB above what the XML's
combined IEQ + AO target predicts: DAX3 does not apply the deep HF rolloff that
the published XML implies. This, the most actionable finding, suggests one of
three hypotheses:

- (a) DAX3 ships a separate HF-shaping stage we're not modelling.
- (b) The `audio_optimizer` block is a target-response curve that DAX3 inverts
  internally rather than applying directly.
- (c) The specific IEQ "Balanced" curve in Dolby Access doesn't correspond to
  the `ieq_balanced` block in the XML.

The [EE response vs XML](#r-ee-response-vs-xml) shows (c) cannot explain the
gap. Disambiguating (a) vs (b) needs either a Dolby-side reference or a
stripped-down single-block tuning XML.

The Music profile fits its XML target most closely, at 5–7 dB RMS. Dynamic,
Movie and Game cluster around 7–12 dB RMS, and Voice deviates the most, at 9–10
dB RMS.

<a id="r-ee-response-vs-xml"></a>

### EE-on-Linux response vs the XML

> **Superseded in part by the
> [`ieq-amount` scaling finding](#r-ieq-amount-scaling).** The EE column below
> was captured under the old `ieq-amount`/10 scaling. The
> [`ieq-amount` scaling finding](#r-ieq-amount-scaling)'s /100 correction, an
> XML-only fix, collapsed the 19.7 kHz residual from −28 dB to −1.5 dB. "The gap
> is on DAX's side" did not survive: the dominant term was ours.

EE follows the converter's XML interpretation within ≤3 dB across most of the
band: same shape, same band centers, same depths. The `tools/measure_ee/`
capture shares the DAX side's 5 stimuli, `analyze.py` and XML reference.
Pink-noise steady-state, Dynamic / Balanced, ThinkPad X1 Yoga Gen 7
(DEV_0287_SUBSYS_17AA22E6), normalized at 1 kHz:

| freq | EE (dB) | DAX (dB) | Δ EE−DAX |
|---:|---:|---:|---:|
| 47 Hz | −36.5 | −28.4 | −8.1 |
| 234 Hz | +19.4 | +16.3 | +3.1 |
| 1 kHz | 0 | 0 | 0 |
| 2.25 kHz | +16.6 | +12.4 | +4.2 |
| 5.8 kHz | +0.1 | +3.3 | −3.2 |
| 11.25 kHz | −6.8 | +2.5 | −9.3 |
| 13.9 kHz | −14.0 | +2.2 | −16.2 |
| 19.7 kHz | −27.5 | +0.7 | −28.1 |

To reproduce, run both batteries through `analyze.py`, then
`tools/measure_ee/compare_ee_vs_dax.py` (steps in `tools/measure_ee/README.md`).

DAX diverges most where the XML target is most extreme, the deep HF rolloff in
`ieq_balanced + audio_optimizer`. At 19.7 kHz, under the then-current scaling,
the combined target predicts ≈ −26 dB relative to 1 kHz. EE applies −27.5 dB, so
the FIR realises the target, and DAX applies +0.7 dB. An earlier revision's
target, "−43 dB, which the FIR doesn't reach", was normalized to the curve peak
at 234 Hz, not 1 kHz: a mixed-normalization slip that manufactured a nonexistent
16 dB FIR shortfall.

Hypothesis (c) from the [DAX response vs XML](#r-dax-response-vs-xml), the wrong
`ieq_*` curve, cannot explain the gap, because all three `ieq_*` curves in this
XML carry the same deep HF rolloff. Under the then-current scaling they reach
−37…−43 dB at 19688 Hz rel-peak: balanced −43.3, warm −40.6, detailed −37.3. No
curve swap could close a ~28 dB HF residual. An earlier revision's reason, "our
converter and EE agree on which curve is in play", is circular: EE applies
whatever curve the converter chose, exactly the `vsXML` circularity that the
note on metrics in the
[XML-interpretation hypotheses](#r-xml-interpretation-hypotheses) warns about.
No Linux-side capture can show which curve Dolby Access selects.

That leaves hypotheses (a) and (b). Loopback can't distinguish them without a
controlled single-block A/B, e.g. a tuning XML stripped down to a single block
at a time.

The 47 Hz deviation (−8 dB EE vs −28 dB DAX, both relative to 1 kHz) is partly
the EE chain's `equalizer#0 band0` HP at 100 Hz / x2 slope, a ≈4th-order rolloff
that takes us deeper than the XML target alone. It is also partly DAX's volume
regulator boosting LF tones at low input levels. In the multitone capture, where
the leveler can lock onto a single 47 Hz sine for 12 s, DAX sits at −14 dB vs EE
−37 dB. That 23 dB gap is much bigger than the pink-noise gap and consistent
with leveler boost rather than steady-state EQ.

<a id="r-hf-shaping-block-audit"></a>

### Audit for a missed HF-shaping XML block

> **Superseded in part by the
> [`ieq-amount` scaling finding](#r-ieq-amount-scaling).** "Cannot be falsified
> without data outside the XML" did not survive: the decisive fix, `ieq-amount`
> read as a percentage, was XML-only. The schema audit below remains valid: no
> skipped element carries HF data. The gap it was trying to explain was largely
> converter-side.

A schema audit found no candidate HF-shaping element that the converter ignores.
It checked every element appearing under `tuning-cp` and `tuning-vlldp` across
the local corpus of device XMLs against what `parse_xml` reads. The elements
`parse_xml` skips fall into five groups, and none of them matches an HF / treble
/ shelf / post-AO role:

- bass-side: `bass-enhancer-*`, `bass-extraction-*`, `virtual-bass-*`
- spatial: virtualizer angles, surround-decoder-center-spreading,
  height-filter-mode
- woofer-specific: `woofer-regulator-*`, `calibration-boost`,
  `customer-woofer-channel-index`
- volume-modeling: `volume-modeler-*`
- graphic-EQ: `graphic-equalizer-*`, which is `enable=0` everywhere in the
  corpus

This narrows hypothesis (a), "DAX ships an HF-shaping stage we're not modeling",
to one of two possibilities:

- A DAX-internal processing stage that does *not* appear in the published
  tuning XML at all, such as a fixed driver-level treble curve baked into DAX3
  and not parameterised per device.
- An XML element whose semantics we have mis-categorised, such as
  `bass-extraction` actually carrying HF data. That is implausible from element
  naming but not strictly ruled out by the corpus.

Either way, hypothesis (a) cannot be falsified without data outside the XML, and
the deterministic "XML-only filter chain" property cannot close it.

<a id="r-ao-sign-variant-matrix"></a>

### Testing hypotheses (a) and (b)

> **Superseded in part by the
> [`ieq-amount` scaling finding](#r-ieq-amount-scaling).** The "fixed DAX-side
> HF behavior" conclusion below was falsified. The profile-independent HF
> residual was dominated by the converter mis-scaling the *shared*
> `ieq_balanced` component, reading `ieq-amount` as /10 instead of /100. The
> variant-matrix rejection of hypothesis (b), the AO sign, stands and remains
> load-bearing.

`add+min`, the current default, wins on every profile, which decisively rejects
hypothesis (b), "DAX inverts AO before applying".

A 2×2 deterministic variant matrix, run across all 5 profiles, tested the
[EE response vs XML](#r-ee-response-vs-xml)'s remaining hypotheses (a) vs (b).
Two temporary patches produced the variants from the same XML:

- `make_fir` accepts a `phase` choice: minimum-phase via cepstral construction,
  or linear-phase via zero-phase IFFT centered at `n/2`.
- The `combined = ieq_db ± ao_db` step in the converter flips the AO sign.

The result was decisive enough that both patches were removed once the matrix
was captured. Pink-noise steady-state RMS residual EE−DAX (dB), 200–18000 Hz,
normalized at 1 kHz, channel L:

| profile | add+min (default) | sub+min | add+lin | sub+lin |
|---------|-----------------:|--------:|--------:|--------:|
| dynamic | 11.95 | 19.53 | 12.15 | 19.94 |
| movie   | 12.53 | 20.26 | 12.84 | 20.66 |
| music   | 8.87  | 16.56 |  9.13 | 16.97 |
| game    | 12.16 | 20.23 | 12.48 | 20.65 |
| voice   | 11.45 | 31.01 | 11.75 | 31.42 |

Reproducing it means re-applying the temporary patches to `make_fir` and the
`ieq_db + ao_db` step, driving `tools/measure_ee/capture_battery.py` once per
(variant, profile), and comparing against the DAX captures with
`tools/measure_ee/compare_ee_vs_dax.py`.

`sub+min` is +7–20 dB worse: subtracting AO moves EE *away* from DAX, not toward
it. Voice is the most extreme, +19.6 dB with sub, because voice has the largest
AO swings. This also confirms the AO contribution is applied with the right sign
at the right magnitude.

`add+lin` is consistently +0.2–0.4 dB worse than `add+min`. Phase character has
minor influence on the magnitude residual, as expected, since pink noise is a
steady-state magnitude measurement; the small delta is leveler interaction with
the changed temporal envelope. Linear-phase doesn't help the magnitude match and
costs ~43 ms of group delay, so it is diagnostic only.

Per-band residuals on `add+min`:

| band | dynamic | movie | music | game | voice |
|---:|---:|---:|---:|---:|---:|
|    47 Hz |  −8.1 |  −7.0 | −14.4 |  −6.5 |  +0.5 |
|   234 Hz |  +3.1 |  +3.0 |  +1.1 |  +3.5 |  +3.0 |
|  2.25 kHz |  +4.2 |  +3.9 |  +4.2 |  +2.7 |  +4.9 |
|  5.81 kHz |  −3.4 |  −3.7 |  −0.4 |  −3.7 |  −3.4 |
| 11.25 kHz |  −9.5 | −10.2 |  −5.3 |  −9.7 |  −8.5 |
| 13.88 kHz | −16.5 | −17.2 | −11.7 | −16.7 | −15.6 |
| 19.69 kHz | −28.2 | −29.2 | −23.5 | −28.7 | −27.6 |

Music's smaller HF gap reflects its less-aggressive HF rolloff in
`ieq_music_balanced`. The rest cluster within ~3 dB at every HF band.

The HF gap above ~10 kHz is profile-independent: same shape and similar
magnitudes whichever `IEQ + AO` target is in play. It was read at the time as
the canonical signature of a *fixed* HF behavior on DAX's side, not
parameterised in the published tuning XML: "the only remaining explanation".
That inference had a gap. A mis-scaled *shared* curve component produces the
same profile-independent signature, a candidate that never made the hypothesis
list, though this finding's own data pointed that way: the music note above
shows the residual tracking the IEQ curve content. The
[`ieq-amount` scaling finding](#r-ieq-amount-scaling) confirmed it.

The mid-frequency biases, +3 dB at 234 Hz and +4 dB at 2.25 kHz, are roughly
profile-independent too. They point at a similar story: EE applies the
XML-implied curve, and DAX softens specific bands with a fixed rather than
per-profile shape. These are the shape of "voicing" choices baked into DAX, not
absorbed into the per-device tuning.

Outcome for the converter: the current `IEQ + AO` minimum-phase FIR is the right
deterministic target for the published XML. No default changed and no permanent
flag was added, because the experiments closed their hypotheses rather than
opening a new tuning surface. Closing the remaining residual further requires
data outside the XML, such as the [single-block XML A/B](#r-single-block-xml-ab)
on Windows below.

### Implications for the converter

Our `make_fir` produces a faithful min-phase FIR of `IEQ + audio_optimizer`,
exact on its design grid and within ≤0.06 dB at the exact band centers from bin
quantization. The math is correct. What we cannot reproduce on Linux without
additional reverse-engineering:

1. **DAX3's hybrid-phase character.** Out of scope: linear-phase costs
   ~43 ms of group delay, ruled out by the no-added-latency constraint.
2. **DAX3's apparent flatter HF response, since closed by the
   [`ieq-amount` scaling finding](#r-ieq-amount-scaling).** After the variant
   matrix rejected the AO-sign hypothesis (b), the
   [HF-shaping block audit](#r-hf-shaping-block-audit) and the
   [AO sign variant matrix](#r-ao-sign-variant-matrix) narrowed this to "a fixed
   DAX-internal stage outside the XML". The
   [`ieq-amount` scaling finding](#r-ieq-amount-scaling) then showed the
   dominant term was the converter's own `ieq-amount` scaling, an XML-only fix
   that took the EE−DAX HF residual from ~12 dB to ~1 dB RMS. What genuinely
   remains outside the XML is that last ~1 dB at HF and the LF/leveler gap in
   the [`ieq-amount` scaling finding](#r-ieq-amount-scaling)'s residual table.
3. **DAX3's non-LTI dynamics**, the leveler and regulator engaging during
   playback. EasyEffects' autogain is bypassed by default, as "Why autogain is
   bypassed by default" above explains. A content-adaptive leveler equivalent
   would require approximating Media Intelligence steering, a substantial
   undertaking.

The captures and analysis tooling under `tools/measure_dax/` are kept for future
debugging: re-running on a new device or after a Dolby driver update is a
one-command repeat.

<a id="r-xml-interpretation-hypotheses"></a>

### Five XML-interpretation hypotheses

None of five further hypotheses closes the residual left after the
[HF-shaping block audit](#r-hf-shaping-block-audit) and the
[AO sign variant matrix](#r-ao-sign-variant-matrix) closed hypothesis (b) and
the missed-block theory:

- **α**: DAX soft-clamps the IEQ+AO target depth.
- **β**: `ieq-amount` is a +/- dB cap, not the linear scale we apply.
- **γ**: DAX applies IEQ only inside a frequency window.
- **δ**: DAX's regulator boosts quiet sustained low tones.
- **ε**: Our 100 Hz × 4th-order HP cuts deeper than DAX at 47 Hz.

α/β/γ/ε were tested as a single-profile (dynamic / balanced) variant sweep
against the DEV_0287 ThinkPad X1 Yoga Gen 7 XML. Four temporary CLI flags on the
converter produced the variants: `--clamp-target-db N`, `--ieq-amount-as-cap`,
`--ieq-window LO:HI`, `--disable-speaker-hp`. δ gets no variant because it is
already the standing
[DAX-leveler approximation follow-up](#r-dax-leveler-approximation). The flags
were reverted in the same commit that landed this finding.

Per-band EE − DAX (dB), pink-noise steady-state, normalized at 1 kHz, positive =
EE louder than DAX:

| variant            |  47 Hz | 234 Hz | 2.25k | 5.8k | 11.25k | 13.9k | 19.7k |
|--------------------|-------:|-------:|------:|-----:|-------:|------:|------:|
| baseline           |  −8.1  |  +3.1  |  +4.2 | −3.2 |  −9.3  | −16.2 | −28.1 |
| clamp ±20 dB       |  −8.0  |  −0.5  |  +3.5 | −3.2 |  −9.3  | −16.2 | −28.1 |
| clamp ±15 dB       |  −8.0  |  −5.5  |  −1.5 | −3.2 |  −9.3  | −16.2 | −23.4 |
| clamp ±10 dB       |  −8.1  | −10.5  |  −6.5 | −3.2 |  −9.3  | −16.2 | −18.5 |
| clamp ±6 dB        |  −7.6  | −14.0  | −10.0 | −3.1 |  −8.8  | −14.1 | −14.0 |
| ieq-amount-as-cap  |  −6.1  |  +1.5  |  +2.5 | −1.2 |  −7.3  | −14.2 | −18.5 |
| ieq-window 100–10k | −18.0  |  +3.1  |  +4.2 | −3.2 | −10.7  | −10.6 | −10.5 |
| no-HP              | +16.9  |  +2.4  |  +4.2 | −3.1 |  −9.3  | −16.1 | −28.1 |

To reproduce, drive `tools/measure_ee/capture_battery.py` with a per-variant
spec TSV and a converter patched to re-introduce the four flags. Use unique
per-variant preset prefixes (`DolbyFG1…DolbyFG8`) to defeat EasyEffects'
convolver IRS-cache by kernel name. Without unique kernel names, EE silently
reuses the previous variant's cached IR even after the .irs file is overwritten
on disk. *Historical since 2026-08:* the converter names each impulse after a
hash of its samples, so a regenerated FIR gets a new kernel name by itself and
unique prefixes are unneeded. See "Rejected approaches → Rewriting
`{preset}.irs` in place".

**A note on metrics.** The `summarise_variants.py` output reports two residuals,
`vsDAX` and `vsXML`. They answer different questions, and `vsXML` is the weaker
signal:

- `vsDAX` is the residual against the captured DAX response, and the only
  external check. DAX captures are imperfect: they cover one device and one
  driver, and DAX itself is non-LTI per
  [DAX LTI behaviour](#r-dax-lti-behaviour). They are still the only data point
  not derived from our own assumptions. A candidate rule that moves EE
  materially closer to DAX *without giving up ground in other bands* is evidence
  our current rule is wrong. That holds even if the new rule lowers `vsXML`,
  since `vsXML` is computed against our own, possibly wrong, interpretation.
- `vsXML` is the residual against the analytical target our converter built from
  its own XML interpretation. It measures internal consistency: whether the
  FIR + biquad chain reproduced the curve our converter intended. It does not
  validate the *interpretation* itself, since the chain and the reference both
  derive from the same `parse_xml`/`make_fir` code path. If we got a field's
  semantics wrong, `vsXML` can report 0 dB while the chain is still wrong.

The verdict for each hypothesis below therefore rests on the `vsDAX` per-band
trade-offs. The `vsXML` deltas are a sanity check that the patched converter did
what we asked, not the deciding criterion.

**α (clamp).** No symmetric N gets closer to DAX in *every* band: there is
always a band where we were nearer DAX before and aren't now. In the vsDAX trade
by clamp depth, each step closes some HF residual but immediately opens an
equivalent or larger mid-band residual:

- ±20 dB shifts only 234 Hz (+3.1 → −0.5).
- ±15 closes 4.7 dB at 19.7 kHz but adds 5.5 dB error at 234 Hz.
- ±6 closes 14 dB at 19.7 kHz, but every mid band is then 10–14 dB off.

Aggregate `vsDAX rms` does drop, 11.84 → 9.59 at ±6. The per-band trade is the
more honest view: the rule isn't shifting the whole curve toward DAX, it's
swapping which bands diverge.

**β (ieq-amount-as-cap).** Cleanest per-band trade in the set: every band moves
*toward* DAX, and no band moves materially away. The 19.7 kHz residual drops
from −28 to −18 dB, +9.7 dB closer to DAX. 13.9 kHz and 11.25 kHz each gain
~2 dB, 5.8 kHz gains 2 dB, and 47 Hz gains 2 dB. 234 Hz and 2.25 kHz both move
~1.6 dB closer to DAX. `vsDAX rms` 11.84 → 9.09; `vsDAX max` 25.15 → 17.52. β is
the most plausible candidate of the five hypotheses, the only one where the
per-band view shows no clear regression. The remaining gap is still 18 dB at
19.7 kHz, so β alone doesn't explain the residual; at most it's part of the
story. `vsXML` worsens 0.94 → 3.06, as expected: β applies a different
interpretation than the converter's reference path uses, so the two should
disagree.

**γ (ieq-window 100 Hz – 10 kHz).** γ is a band-for-band trade, not a strict
improvement: it buys ~17 dB at 19.7 kHz and ~6 dB at 13.9 kHz at the cost of
~10 dB at 47 Hz. Per band, it gives the largest single HF improvement: the
19.7 kHz residual drops to −10.5 and 13.9 kHz to −10.6. The 47 Hz residual blows
out from −8 to −18 dB EE−DAX, and 11.25 kHz worsens by ~1.4. The mid band is
unchanged. Aggregate `vsDAX rms` goes 11.84 → 7.57, the lowest of all variants,
driven entirely by the HF win. The in-band `vsDAX max` summary metric excludes
47 Hz and drops to 10.88 dB, but the actual worst-band error has relocated to
47 Hz at 18 dB.

**δ (leveler).** Confirmed unchanged. The pink-noise gap at 47 Hz is −8 dB
EE−DAX, while the multitone-on-47 Hz gap is −23 dB EE−DAX
([EE response vs XML](#r-ee-response-vs-xml)). The factor-of-3 gap ratio between
stimuli is the canonical signature of a content-adaptive leveler boosting
sustained low tones. Closing this requires modeling DAX's MI-steered leveling,
unchanged from the
[DAX-leveler approximation follow-up](#r-dax-leveler-approximation).

**ε (no speaker HP).** ε is the dominant LF mechanism on EE's side, a decisive
analytical match. A Butterworth-style 4th-order HP at f0 = 100 Hz attenuates
47 Hz by ~26 dB. The captured `no-HP` variant lifts EE at 47 Hz from −36.5 to
−11.6 dB, a +24.9 dB shift. Removing the HP overshoots DAX, though: EE at 47 Hz
is then +16.9 dB vs DAX, against −8.1 dB with HP. So the HP itself is the right
*topology*, and DAX must apply some LF shaping, just softer than ours. Two
stories are consistent, and the variant sweep cannot disambiguate them:

- (i) DAX applies an HP at the same f0 with a shallower slope, ~12 dB/oct
  instead of 24.
- (ii) DAX's leveler boost (δ) compensates for an otherwise-similar HP, and the
  pink-noise EE−DAX gap is leveler-dominated, not filter-dominated.

**Outcome.** None of α/β/γ is a strict per-band improvement against DAX. β is
the closest: every band moves toward DAX, none materially away. But the 19.7 kHz
residual is still 18 dB after applying it, so even if β is part of the right
interpretation it doesn't explain the bulk of the gap. α and γ are pareto
trades: they swap one band's error for another. α/β/γ show the pattern the
[AO sign variant matrix](#r-ao-sign-variant-matrix) saw with the AO-sign and
phase variants: partial movement, and no sweep that lands every band closer. It
was read as consistent with the
[AO sign variant matrix](#r-ao-sign-variant-matrix)'s conclusion: the residual
is dominated by DAX-internal behavior, a fixed HF voicing plus the leveler, that
lies outside the published XML, not a single wrong XML rule on our side.
Superseded: the [`ieq-amount` scaling finding](#r-ieq-amount-scaling) found a
single wrong XML rule on our side, `ieq-amount` read /10 instead of /100. β
above was the near-miss: right mechanism, wrong field semantics.

**Caveat.** This conclusion is conditional on the experimental data we have:
DAX captures for one device / one driver revision. β's ~9 dB HF improvement
*might* generalize, in which case our current `ieq-amount → linear scale` rule
is wrong and the cap reading is right. β is not held back for being worse; on
`vsDAX` it isn't. It is held back because the remaining 18 dB residual at
19.7 kHz means even the best candidate doesn't close the gap, so swapping rules
trades one incomplete model for another. The threshold for revisiting the
default is a second device's DAX captures on which β is also a strict
improvement. To re-run the experiment, re-add the four temporary flags per the
patch in the git history of this finding.

Per-variant captures were retained in the local (gitignored) research area. They
predate the [`ieq-amount` scaling finding](#r-ieq-amount-scaling)'s scaling fix,
so per the capture-validity rule they are stale for any new EE↔DAX comparison:
re-capture instead.

**β follow-up: cross-profile validation.** β, the most plausible candidate from
the single-profile sweep, was re-tested on the other four profiles (movie /
music / game / voice) to see if its per-band improvement signature is
consistent. Same XML, same DAX captures, same harness, fresh EE captures with
`--ieq-amount-as-cap`. The test XML uses `ieq-amount=10` on every profile, so
the cap value itself is not the variable across profiles. The variable is the
IEQ + AO curve content, which differs per profile: different audio-optimizer
per-band gains, same `ieq_balanced` curve shape.

|β| − |baseline| (positive = β is closer to DAX), in dB:

| profile |  47 Hz | 141 Hz | 234 Hz | 469 Hz | 2.25k | 5.8k | 11.25k | 13.9k | 19.7k | total |
|---------|------:|------:|------:|------:|------:|-----:|-------:|------:|------:|------:|
| dynamic | +1.93 | +1.76 | +1.58 | +0.51 | +1.64 | +2.22 | +2.17 | +2.30 | +9.78 | +23.9 |
| movie   | +1.98 | +1.53 | +1.62 | +0.69 | +1.63 | +2.00 | +2.00 | +2.00 | +9.69 | +23.1 |
| music   | +1.89 | +1.53 | +0.53 | +0.69 | +1.63 | −1.13 | +2.00 | +2.00 | +9.69 | +18.8 |
| game    | +1.89 | +1.53 | +1.62 | +0.69 | +1.63 | +2.00 | +2.00 | +2.00 | +9.69 | +23.0 |
| voice   | −1.99 | +1.53 | +1.62 | +0.69 | +1.63 | +2.00 | +2.00 | +2.00 | +9.69 | +19.2 |

To reproduce, run a per-profile spec TSV with the temporary
`--ieq-amount-as-cap` flag, then overlay the DAX vs baseline vs β pink-noise
spectra across all five profiles.

β shifts EE by almost identically the same dB amount per band on every profile.
The +9.69–9.78 column at 19.7 kHz is within 0.1 dB across all five. That's the
expected behavior if β is hitting a *structural* feature of the published IEQ
curve: `ieq_balanced` is shared across profiles, so capping it produces the same
lift in every profile's combined target. If the captured improvement were noise
or coincidence, we'd expect per-profile variability of several dB; we see
≤0.1 dB.

Two regressions stand out: voice at 47 Hz, where β is 2 dB *worse*, and music at
5.8 kHz, where β is 1.1 dB *worse*. In both cases the baseline residual at that
band was already near zero: voice 47 Hz +0.46 dB EE−DAX, music 5.8 kHz −0.43 dB.
So β's lift *overcorrects* through zero rather than degrades the chain. The
underlying lift is the same magnitude as on the other profiles. That's a
side-effect of β's mechanism, which always lifts, rather than a profile-specific
failure of the rule.

In aggregate, β closes 18.8–23.9 dB of total |EE−DAX| residual on every profile,
~80% of which is concentrated at 11.25–19.7 kHz. After β, every profile still
shows an 18 dB residual at 19.7 kHz. That residual is also remarkably
consistent: the post-β `vsDAX` at 19.7 kHz is −18.46 / −19.57 / −13.85 / −19.02
/ −17.96 across the five profiles, clustered ~−18 dB. So β is *part of* the
right reading but not all of it. A second mechanism, likely the fixed HF voicing
in DAX from the [AO sign variant matrix](#r-ao-sign-variant-matrix), accounts
for the remaining ~18 dB.

**Updated stance on β.** Calibration, added in review: the ≤0.1 dB cross-profile
consistency is close to guaranteed by construction. β perturbs only the shared
`ieq_balanced` component, so the EE-side delta is identical per profile, and the
[AO sign variant matrix](#r-ao-sign-variant-matrix) had already shown the
baseline residual is profile-independent. The genuinely new information in this
table is the two sign-crossing regressions and a re-confirmation of capture
repeatability, not independent evidence for the cap reading. The cross-profile
result confirms the improvement is *structural*, not coincidental. We don't
change the default because:

- We have one device's data. The XML schema interpretation might be
  device-specific in a way β happens to fit on this device. `ieq-amount` is
  always 10 in this XML, so we have no direct evidence about the cap-vs-scale
  interpretation when the value differs.
- β cannot close the remaining 18 dB at 19.7 kHz. Even if correct, it has to
  coexist with a second mechanism we haven't modeled. Switching defaults to a
  partially-correct rule is worse than leaving a known-incomplete rule in place.
- β has small per-profile regressions (voice 47 Hz, music 5.8 kHz). While
  explainable as overcorrection, they would audibly tilt those profiles vs the
  current default.

**The bar for adopting β as the default:** a second-device XML where
`ieq-amount` differs from 10 *and* a captured DAX response from that device,
showing that the cap reading predicts the per-band improvement at the new value.
That would distinguish "β is the right rule" from "β's +10 dB HF lift happens to
align with DAX's HF voicing on this device."

<a id="r-dax-virtual-bass"></a>

### DAX runs psychoacoustic VBE; the schema can't drive a per-device mapping

The loud 50 Hz region of the DAX bass-burst capture, at peak −5 dBFS, shows a
textbook missing-fundamental harmonic complex. The stimulus is the bass-burst
battery in `tools/measure_dax/make_stimulus.py:make_bass_burst`: sustained sine
tones at 50 / 80 / 120 / 180 Hz, ±5 / −25 dBFS peak. The same battery closed the
unrelated regulator-stress investigation (the
[`regulator-stress-amount` follow-up](#r-regulator-stress-amount) below).

| freq | magnitude | role |
|---:|---:|---|
| 50 Hz | −36.3 dBFS | fundamental |
| 150 Hz | −37.6 dBFS | 3rd harmonic (1.3 dB below fundamental) |
| 250 Hz | −38.1 dBFS | 5th harmonic (1.8 dB below fundamental) |
| 200 / 300 Hz | −50 dBFS | 4th / 6th (suppressed even harmonics) |
| 350 Hz | −54.3 dBFS | 7th |

The 180 Hz capture in the same battery is a clean sine. All its non-fundamental
peaks are ≥35 dB below the fundamental, and its crest factor is 4.4 dB. The 50
Hz region's crest factor is 22.9 dB. DAX is generating odd harmonics at
near-fundamental amplitude on bass content the speaker can't physically
reproduce. This is the standard psychoacoustic-bass mechanism, as in SRS
TruBass, Waves MaxxBass and Dolby's own Virtual Bass Enhancement. It lets the
auditory system reconstruct a 50 Hz percept from the harmonic complex above the
speaker's HP roll-off.

Our converter emits Calf BassEnhancer only when `is_soundwire=True`. The X1 Yoga
and other HDA devices get no harmonic-generation stage at all.

This is a **non-LTI** processing-stage gap, distinct from the LTI EQ-curve gap
analyzed in the [EE response vs XML](#r-ee-response-vs-xml), the
[HF-shaping block audit](#r-hf-shaping-block-audit), the
[AO sign variant matrix](#r-ao-sign-variant-matrix) and the
[XML-interpretation hypotheses](#r-xml-interpretation-hypotheses). Pink-noise
captures would not show it, because the harmonic complex blends into the
broadband spectrum. It shows up only on tonal bass content.

**XML interpretation question.** The X1 Yoga XML populates every VBE-adjacent
field:

```
<bass-enhancer-enable value="0"/>
<bass-enhancer-cutoff-frequency value="200"/>
<virtual-bass-mode value="0"/>
<virtual-bass-mix-freqs value="94,469"/>
<virtual-bass-subgains value="-32,-144,-192"/>
<virtual-bass-src-freqs value="35,160"/>
<virtual-bass-overall-gain value="0"/>
```

`enable=0` and `mode=0` read as "off", but every supporting field is
non-default. Two readings:

1. `mode=0` is honestly off. DAX has a baseline VBE that always runs
   and is not parameterized in the schema. The supporting fields are
   dead schema slots.
2. `mode=0` is a label, e.g. "auto/default", and the supporting parameters
   configure it.

A `*-mode` survey across the schema shows some mode-attrs varying by device,
such as `height-filter-mode` at `0`/`1` in the corpus. So `0` is not universally
"off". But for a feature with a separate `*-enable` *and* tuning fields, the
precedent is `dialog-enhancer-enable=0` with `dialog-enhancer-amount=7`: tuning
fields persist as dead values when the gate is off.

**Corpus sweep.** It settles the actionable question under either reading.
Across 2,470 XMLs containing VBE fields:

| field | unique values across 2,470 XMLs |
|---|---|
| `virtual-bass-mode` | `0` only |
| `bass-enhancer-enable` | `0` only |
| `virtual-bass-mix-freqs` | `94,469` only — 1 unique value |
| `virtual-bass-src-freqs` | `35,160` only — 1 unique value |
| `virtual-bass-subgains` | `-32,-144,-192` only — 1 unique value |
| `virtual-bass-overall-gain` | `0` only |
| `virtual-bass-slope-gain` | `0` only |

The supporting fields are frozen identical across hundreds of distinct speaker
designs. If they configured the synthesis, as reading 2 holds, they would vary
the way `mb_comp` thresholds, IEQ curves and regulator levels do. They don't.
**The schema cannot drive a per-device VBE mapping** in either reading.

**Companion-file audit.** No text-readable per-device file carries VBE tuning.
Each DAX3 driver package ships:

- a main tuning XML;
- a `_settings.xml`, UI-only: AutoProfile mappings, Dolby Access GUI flags;
- one or more `operator_settings_*.json` files, Dolby Access vendor config:
  mirroring, auto-profile behavior;
- `.inf`/`.cat` driver-install metadata.

The corpus harness already filters the mic-side `_amic.xml` / `_dmic.xml` files.
The DAX3 binaries themselves could carry an internal lookup table keyed on PCI
subsystem ID. Reading that requires reverse-engineering the DLL and is out of
scope.

**Actionable conclusion.** A faithful XML-derived per-device VBE mapping is not
on the table, since no text-readable source we ship against carries a per-device
VBE signal. Any VBE we add for HDA devices is by construction a hardcoded
baseline that does not trace back to per-device XML. Under the project's
XML-only invariant, it lives in opt-in territory. The existing
`make_bass_enhancer(hp_freq)` factory is the natural baseline candidate. Commit
`bc12c2e9` introduced it from the EasyEffects laptop-speaker guide. Gating it on
HDA devices is an investigation question rather than a deterministic-mapping
one. Issue
[#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14)
holds the open follow-up.

**Empirical follow-up.** Calf BassEnhancer hits an architectural ceiling. A
`--enable-vbe` investigation flag was added on top of the SoundWire-only
emission gate. The same change extended `make_bass_enhancer` to accept
`(floor, scope) = virtual-bass-src-freqs` from the XML. That field lives under
`tuning-cp`, not `tuning-vlldp`, and is corpus-frozen at `35,160`. Bass-burst
captures compared the unflagged chain, the flagged chain and DAX, on the X1
Yoga, dynamic/balanced, at 50 / 80 / 120 / 180 Hz and peak −5 dBFS:

| tone | EE off Δ3 | EE+VBE Δ3 (XML-derived) | DAX Δ3 |
|---:|---:|---:|---:|
| 50 Hz | −81 dB | **−8 dB** | **−1 dB** |
| 80 Hz | −102 | **−29** | **−28** ← matches DAX |
| 120 Hz | −102 | −42 | −59 |
| 180 Hz | −112 | **−18** ← regression | **−74 (clean)** |

Calf does generate psychoacoustic harmonics on 50/80 Hz at amplitudes within ~5
dB of DAX: the 50 Hz benefit. It also produces a strong 3rd harmonic at 540 Hz
on the 180 Hz tone, where DAX is essentially clean: the 180 Hz regression. A
parameter sweep confirmed the regression is structural. It covered `harmonics`
(3 / 5 / 10), `blend` (−10 / 0 / +10) and `amount` (6 / 12). Every variant
trades 50 Hz benefit linearly for 180 Hz regression. No parameter point makes
Calf produce DAX-like harmonics on the lowest tones *and* stay clean above the
declared `virtual-bass-src-freqs` upper bound.

The mechanism is Calf BassEnhancer's internal source-band filter. It has a soft
rolloff of ≈12 dB/oct at the `scope` parameter. A 180 Hz input above a
`scope=160` cutoff is only ~4 dB attenuated, so the harmonic generator still
receives a sizable signal. Tightening `scope` below 100 Hz to fully suppress 180
Hz also kills the 80 Hz synthesis that already matches DAX. Calf also leaks 2nd
harmonics where DAX's profile is much weaker on evens. For example, a 50 Hz tone
yields a 100 Hz peak ≈25 dB below the fundamental. DAX's harmonic generator
appears to use a near-symmetric nonlinearity that emphasizes odd harmonics,
while Calf's distortion model produces both.

The flag was reverted from the converter's CLI surface, along with the
band-bounds plumbing it used. The current `make_bass_enhancer` signature is
`(hp_freq, amount)`. An earlier revision of this note claimed it retains a
`src_freqs` parameter. It does not, and no committed revision ever had one. A
future architectural revisit shipping VBE-on-HDA via a different plugin/topology
would need to re-derive the band bounds from the XML's `bass-enhancer-*` fields.

**Calf Saturator (architectural alternative for PipeWire-conf path).**
Saturator's harmonic profile is closer to DAX's than Calf BassEnhancer's, but
the architectural ceiling is *lowered, not escaped*. A PoC offline test used
`lv2apply` to drive Calf Saturator on the bass-burst stimulus at `mix=1.0`,
`drive=4.0`, `hp_pre_freq=35`, `lp_pre_freq=160`, `hp_post_freq=180`,
`lp_post_freq=800`. Wet-only output Δ versus fundamental-leakage:

| tone | calf_v2 (BassEnhancer) Δ3 | sat_v1 (Saturator) Δ3 | DAX Δ3 |
|---:|---:|---:|---:|
| 50 Hz | −8 | +4.5 | −1.3 |
| 80 Hz | −29 | −3.4 | −28 |
| 180 Hz | **−18 (regression)** | **−31** | −74 (clean) |

At 180 Hz, Saturator is ~13 dB cleaner than Calf BassEnhancer: −31 vs −18 below
fundamental. That is meaningful but still ~40 dB above DAX's clean profile. The
reason is identical to BassEnhancer's: Calf Saturator's internal `lp_pre_freq`
is also a soft (~12 dB/oct) rolloff, so out-of-band content above 160 Hz still
passes through enough to generate harmonics. Calf Saturator also leaks
2nd-harmonic ≈15 dB stronger than DAX's profile at 50 Hz, with comparable
3rd-harmonic levels. At 50 Hz its 3rd-vs-2nd ratio is +11 dB, against +26 dB
for DAX. Calf's distortion model is not purely symmetric.

Harder drive (`drive=8`) and a tighter post-band (`lp_post=600`) amplify the
harmonic complex but don't change the relative ratios.

**Cascading LSP filters + Saturator.** This chain escapes the single-plugin
ceiling. A follow-up PoC chained two LSP `filter_stereo` stages in front of Calf
Saturator for a true brick-wall band-pass at [35, 160] Hz. The filters ran BWC
mode at slope `x16` (≈192 dB/oct) on input edges and `x8` (≈96 dB/oct) on output
edges. Calf Saturator's internal pre/post filters were disabled to avoid
double-filtering. Final output Δ3 (3rd harmonic vs fundamental) at 180 Hz is −56
dB. That is a 38 dB improvement on Calf BassEnhancer's −18 dB ceiling, and
within 18 dB of DAX's −74 dB clean profile. The 50 Hz / 80 Hz odd-vs-even ratios
are also strongly odd-dominated, at 3rd-vs-2nd +56 dB / +16 dB respectively.
This confirms Calf Saturator's `drive` produces a near-symmetric saturation when
fed a clean band-passed input. The even-harmonic leakage in the earlier
single-plugin tests was an artefact of out-of-band content reaching the
saturator. So a deeper signal chain can break both walls of the architectural
ceiling. The cost is two extra LV2 stages per channel, 4 LSP filter instances
total in stereo, plus disabled internal Calf filtering.

**Reproducing this PoC.** `tools/measure_ee/render_vbe_chain.py` (2026-08-19)
rebuilds the chain offline, one `lv2apply` subprocess per stage. The run that
produced the −56 dB figure was driven by hand with no saved generator. The
script's defaults reproduce the 2026-05-06 stage renders bit-for-bit, with
lsp-plugins-lv2 1.2.33 and calf-plugins 0.90.9. The BWC filters are the BT
variant. The Saturator ran unity output gain: the `level_out=4.0` of the
single-plugin conf above never applied to the chain.
`tools/measure_ee/analyze_vbe_chain.py` re-derives the harmonic tables from any
labeled render or capture.

An earlier revision of this note claimed `lv2apply` "segfaults on LSP plugins,
which need `work:schedule`". That is wrong on both counts. `lv2info` lists
`worker:schedule` as *optional* for `filter_stereo`, and only `urid:map` is
required. `lv2apply` renders the whole chain, and PipeWire's
`module-filter-chain` hosts it live. The shipped PW path runs four LSP plugins
through it. The quirk behind that claim: Calf Saturator renders its full output,
then hits a glibc heap-corruption abort during host teardown. The render is
complete, so the script validates stages by frame count instead of exit code.

**Caveat.** The ceiling-break is for harmonic structure only, not absolute
magnitude. The PoC measured the wet-only output of the
LSP-cascade-plus-Saturator chain, with `mix=1.0` and a post-HP at 180 Hz that
kills the band-passed fundamental. In a real deployment the wet path is summed
with a parallel dry chain that carries the fundamental. Our dry chain attenuates
50 Hz to ~−66 dBFS, and the wet Saturator residual sits at ~−54 dBFS. The mixed
50 Hz fundamental therefore lands ~9 dB below DAX's captured −45 dBFS. The
harmonic complex is structurally right, with a +56 dB odd-vs-even ratio at 50 Hz
and the 180 Hz harmonic regression gone, but the fundamental remains low. This
separates cleanly into *two* gaps, not one:

1. **Bass-attenuation gap** ([EE response vs XML](#r-ee-response-vs-xml),
   [XML-interpretation hypotheses](#r-xml-interpretation-hypotheses)): our chain
   attenuates 50 Hz ~21 dB more than DAX does. It lives outside the XML. DAX's
   regulator / leveler appears to actively boost quiet sustained low tones,
   which is what hypothesis δ in the
   [XML-interpretation hypotheses](#r-xml-interpretation-hypotheses) was
   tracking. Closing this gap needs a level-dependent / content-adaptive boost
   upstream, not a harmonic synthesizer.
2. **Harmonic-synthesis gap** (this finding): DAX adds odd-dominated
   harmonics on bass content, and our chain doesn't. The
   LSP-cascade-plus-Saturator chain closes this structurally.

In earlier tests Calf BassEnhancer *appeared* to address part of (1), because
its mix structure passes some boosted dry-band signal through. That conflated
the two gaps and came at the 180 Hz regression cost. The Saturator-based path
keeps the gaps separated. That is honest, but it means closing the harmonic gap
doesn't close the magnitude gap. The next concrete step before any
`ee_to_pipewire.py` change ships is the mixed-topology measurement: a parallel
dry + wet Saturator chain summed at output. The wet-only PoC numbers show the
harmonic ceiling can be broken, but not what the integrated chain's absolute
magnitude match looks like.

**Calf MultibandEnhancer.** It was tested in parallel and does not escape the
ceiling. A 24-variant sweep covered crossovers, per-band drive, blend and base
parameters. The best variant (`split_taper_mid`: drive=10/4/1/0,
blend=8/2/0/0, splits 65/100/150) produces a DAX-shaped tapered Δ3 that weakens
with frequency. It caps at 50 Hz Δ3 ≈ −20 dB. The plugin's harmonic generator
is a memoryless wave-shaper with an intensity ceiling that no parameter
combination crosses. It is also even-dominated at the lowest tones, where DAX is
strongly odd-dominated. MultibandEnhancer cannot substitute for the
BassEnhancer / Saturator path.

The cascading-LSP-plus-Saturator approximation can only ship via the PipeWire
filter-chain path, for `ee_to_pipewire.py` users. EE 8.x exposes no saturator
plugin slot and no way to chain LV2 filters in series before its built-in plugin
slots. Splitting VBE behaviour across the two output paths becomes a
maintenance-cost decision rather than a measurement one. EE-mode users get
nothing, and PW-mode users get an approximation that genuinely tracks DAX's
selectivity within ~17 dB at 180 Hz. Issue
[#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14)
is left open as the canonical reference for the gap. No converter change ships
from this investigation. If a listening test confirms the captured improvement
is audible, the natural next step is a follow-up that extends
`ee_to_pipewire.py` to inject the LSP-cascade-plus-Saturator chain when the XML
carries `virtual-bass-mode=0` and `is_soundwire=False`.

**Where this sits after the 2026-06 PW-path review.** Closing the VBE gap is a
deliberate-divergence decision, not a measurement one, because the converter is
kept a faithful 1:1 translation. The PW conf reproduces the EE preset and
nothing more, and the `tools/measure_pw/` equivalence is the contract. Closing
the gap has two shapes if ever taken:

- (a) *Cheap, both paths*: enable Calf BassEnhancer on HDA in `make_preset`,
  which today emits it on SoundWire only. It keeps EE and PW equal and matches
  DAX within ~5 dB at 50/80 Hz, per the bass-burst table above. But it
  over-synthesises at 180 Hz where DAX is clean (the −18 dB regression), and
  leaks 2nd harmonics where DAX is odd-dominated. It is a decent-but-imperfect
  approximation, not a faithful one. Shape (a) stays deferred.
- (b) *PW-only, more selective*: the LSP-cascade + Saturator injection above. It
  suppresses the 180 Hz over-synthesis BassEnhancer can't, and is expressible
  only in the PW path. Shape (b) shipped 2026-08 as the `--enable virtual-bass`
  opt-in. The phase-2 work below is how it got there.

#### Phase 2 (2026-08): decoding `virtual-bass-subgains` and scoring a chain

The VBE fields are corpus-frozen but not dead: they decode, and the decode
predicts the measured DAX behaviour. A scored search over readings of
`virtual-bass-subgains` settled the "dead schema slots or configuration?"
question from the top of this finding. The mixed-topology step the caveat above
named is taken: the parallel dry + wet render is part of
`tools/measure_ee/render_vbe_chain.py`.

**The sixteenth-dB unit is proven, not inferred.** The sliding-bass
`gain-curve[1]` ↔ `max-gain` identity (192→12.0, 288→18.0, 297→18.5625) fixes
the 1/16-dB scale. Cross-device-findings §14 documents the pairs. So
`virtual-bass-subgains="-32,-144,-192"` is exactly −2 / −9 / −12 dB.

**Two readings of subgains are dead:**

- *Per-harmonic*, as (h2,h3,h4) or (h3,h5,h7) weights. The measured DAX table at
  the top of this finding refutes it. −2/−9/−12 would make h2 loudest, and DAX
  has h2 ≈26 dB *below* h3 at 50 Hz. The odd-only variant predicts h5 = h3−7
  where DAX measures h3−0.7.
- *Literal three-sub-band*, which is structurally impossible with any weights.
  The third sub-band (94–160 Hz) lies inside the mix band 94–469, so its
  fundamentals pass both filters into the sum. Every drive/blend combination
  fails the wet-leakage guard (G1 below), on both dry captures.

**Winning reading: sub-band weights, with −192 = that band off.** −192 is the
schema's conventional floored/off magnitude, the one `geq_maximum_range` (192)
and the `array_20_n192` default use. Read that way, the switched-off slot covers
exactly the sources inside the mix band, the ones no subtractive topology can
synthesize from. The live sources partition [`src-freqs[0]`, `mix-freqs[0]`] =
[35, 94] geometrically, at sub-band edges 35 / 57.4 / 94 Hz.

**The scored candidate ("v3").** Two brick-wall arms, 35–57.4 and 57.4–94 Hz,
are premixed at −2 / −9 dB (subgains 1–2) into a Calf Saturator at drive 4,
blend −10. Those are the two measurement-calibrated engine constants; the XML
has no field for either. A double HP@94 + LP@469 mix band follows, summed at
unity to match `virtual-bass-overall-gain=0`. A single ×16 HP leaks enough of
arm 2's 80 Hz fundamental to fail G1; the cascade lands it at ≈−104 dBFS against
a ≈−61 dBFS requirement. The free global-gain fit independently lands at 0 dB,
the strongest self-consistency check in the batch.

**Scoring protocol (red-teamed).** Targets are the 12 measured DAX cells
(50/80/120 Hz tones × harmonics) plus a guard cell, both sides clamped at −80
dBFS. S is the macro-average per tone of |error|, with overshoot ≥200 Hz
weighted ×2. One global wet gain is fitted on a −6..+12 dB grid, and unity is
reported alongside. The guards:

- G1: wet fundamental leakage ≤ dry−20 dB.
- G2: 180 Hz 2nd harmonic ≤ −80.
- G3: mud cap, cells ≤ DAX+6 and the 200–469 Hz integral ≤ DAX+3.
- G4: quiet render clean.
- G5: multitone IMD ≤ DAX+6.
- G6: no grid-edge fit.

Anchors, and repeatability across two dry captures, calibrate the scale:

| candidate | S (unity wet gain) |
|---|---:|
| DAX scored against itself (method noise floor) | 0.07 |
| v3 | 4.43 |
| emitting nothing | 10.01 |
| the historical Calf BassEnhancer capture | 18.43 |

The metric ranks the known candidates correctly. Calf BassEnhancer scores *worse
than doing nothing* under the asymmetric metric, which matches its on-device
rejection above.

**Results.** S(v3) = 4.43 at unity on both dry captures, |ΔS| = 0.00, with all
guards green. The variants around it:

| variant | S | verdict |
|---|---|---|
| mid split at 70 Hz instead of the geometric 57.4 | 4.46 | the split edge is not load-bearing |
| drive 6 | 4.14, but only at the −6 dB grid edge; at unity mud fails | G6 reject |
| blend 0 | ≈2.1 dB worse than v3 | loses |
| drive 4.5 / blend −10, first in a finer drive×blend recalibration sweep (2026-08-21; drive 3.0–6.0 in 0.5 steps, blend −10..0) | 4.38 at unity, 0.05 dB ahead | far under the ≥0.5 dB-on-both-captures adoption rule, so drive 4 / blend −10 stands |

The remaining error is safe-direction undershoot, h5 at 250 Hz (−61 vs DAX −47)
and h4 at 200 Hz, except the 2nd-harmonic cells, ≈9–10 dB hot at 100/160 Hz.
Calf's saturation series decays faster than DAX's above h3. Known soft spot:
sources near the 94 Hz boundary leak ≈−58 dBFS wet. That is inside G1's margin
and ~25 dB under typical dry content there, but it is the topology's weakest
point on real content.

**Acoustic end-to-end confirmation (2026-08-21).** With a 50 Hz tone through the
live chain, the 149–150 Hz product is audible in the room and visible on a phone
spectrum analyzer. It is present with the wet mix gain at 1.0 and gone at 0.0.
This is the first room-air verification; every prior check was a sink-level
digital capture. A music A/B on the dev X1 Yoga reads subtle and artifact-free,
consistent with the effect's size: the products sit 25–30 dB under content
level, and only 35–94 Hz fundamentals trigger them.

**Standing caveat.** DAX's VBE is level-adaptive: the wet product collapses ~2.5
dB per dB of input at low level. A static chain matches at the reference level
only. G4 shows the static chain at least fails safe there: on quiet content it
collapses in the same direction as DAX and stays clean.

**What shipped: `--enable virtual-bass`, PipeWire path only.** The opt-in flag
builds this chain around the translated stages (`lib/pipewire/vbe.py`). It is
all-IIR, takes no look-ahead and adds zero latency. It stays opt-in for the same
two reasons as before: one device scored, and drive/blend are
measurement-calibrated rather than XML-derived. The EE-path half was verified
against the EasyEffects source (2026-08-21, master v8.2.8):

- the pipeline is strictly serial;
- no saturator plugin exists;
- arbitrary LV2 plugins cannot be loaded;
- EE tears down foreign links on its nodes.

So a parallel wet branch can be neither expressed in a preset nor hand-patched
around a running EE.

**Shipped-path verification (2026-08-21).** The shipped artifact reproduces the
measured chain, not just its parameter values. The conf the flag generates (XML
→ `--enable virtual-bass` preset → `ee_to_pipewire.py`) was captured end-to-end
on the same null-sink route as the prototype rig and scored with
`tools/measure_ee/score_vbe_chain.py`. Every scored cell lands within 0.8 dB of
the rig capture above the −80 dBFS clamp. Unity S = 4.43 vs the rig's 4.53,
which is run-to-run variance. The measured cells:

![Measured harmonic cells — doing nothing vs the shipped chain vs
DAX](images/vbe-cells-vs-dax.png)

**Second-device evidence (2026-08-21): whether DAX runs VBE at all is decided
outside the XML.** On the issue
[#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44)
device (Yoga Slim 7 14ARE05), Windows DAX applies no VBE, despite its XML
carrying the identical corpus-frozen `virtual-bass-*` values. Its DAX battery
has no bass-burst loopback, so the chain can't be re-scored against it. Its
multitone stimulus does carry one source-band tone (47 Hz) whose even-harmonic
products land on clean bins: 94/188/282/376 Hz, the bins the G5 guard reads.

| Windows DAX multitone capture | products on those bins | 47 Hz fundamental |
|---|---|---|
| dev X1 Yoga | strong synthesis, 60–75 dB above the processing-off floor | cut ~14 dB |
| #44 | within ~10–15 dB of the capture floor | *boosted* ~11 dB |

The #44 tuning comes from an older 2019-era Dolby package, v6.503. The original
session verified the values on a sibling tuning from the same package; the deep
audit below re-verified them on the device's own file with a full structured
diff. So the frozen fields don't predict whether a given device's DAX engages
VBE. Engagement is engine- or device-side, outside any text-readable source we
ship against: this finding's core claim, proven on a second device's measured
data. On a device we haven't captured, `--enable virtual-bass` therefore means
"add the effect if you like it", not "match your Windows". The default-off
framing is behaviorally, not just formally, correct.

**Deep audit (2026-08-21): no text-readable source — XML, companion file, INF,
or registry — names a VBE switch.** The audit stress-tested the "decided outside
the XML" claim on every adjacent file the converter ignores, looking for
whatever makes the two devices differ:

- *Full structured XML diff*. #44's own tuning (`DEV_0287…17AA380D`,
  `xml_version` 3.2.1, DTT 3.1.7) was diffed against the dev X1 Yoga's
  (`17AA22E6`, 3.5.5, DTT 3.4.0.5) over every value-carrying element of the
  speaker endpoint across all profiles. Nothing bass-adjacent differs. The
  virtual-bass block is uniform across every endpoint × profile of both files.
  The only value differences anywhere are ordinary per-device tuning (regulator
  thresholds, leveler amount, virtualizer angles, volmax-boost) plus three
  surround enables on `voice`.
- *Corpus re-derivation (2026-08-21, 2,836 tuning XMLs):* all six
  `virtual-bass-*` fields are single-valued across all 42,491 profile blocks.
  The full schema (3.4.1+) also carries the one in-XML VBE boolean we had never
  read: `virtual_bass_process_enable`, an engine-init flag inside
  `tuning-cp/init-info`. It is `0` in all 30,724 occurrences across 1,967 files,
  including every profile of the dev device where VBE measurably runs. #44's
  3.2.x schema has no `init-info` at all. Even the schema's own enable bit reads
  "off" on an engaged device.
- *Driver-package INFs* (67 Dolby INFs across the corpus): the DAX extension INF
  assigns each hardware ID a product SKU, and that tier is the one clean
  per-device contrast. #44's `17AA380D` is `DolbyAtmosSpeakerSystem` in every
  package generation (v6.503 through v10.1029). The dev `17AA22E6` is
  `DolbyAccessNoGaming`, verified in the dev machine's installed v9.1127.1236.0
  DriverStore package. The in-XML `<sku>` says `DolbyAtmosSpeakerSystem` for
  *both*, so the effective tier lives in the INF, not the XML. The entire
  INF-writable registry surface contains no VBE key. The only per-device bass
  gate anywhere in it is sliding-bass's
  (`HKR,Streaming_Speaker,DolbySlidingBass`, cross-device findings §14).
- *Offline registry audit of the engaged machine*, covering media-class driver
  instances dumped in full, MMDevices endpoint FX properties, `SOFTWARE\Dolby`
  and Dolby Access's UWP settings store. No VBE enable exists under any name.
  The only bass-adjacent value on the whole surface is `DolbySlidingBass = 0`.
  `SOFTWARE\Dolby\DAX` holds only global state (`DolbyEnable = 1`,
  lid/orientation).

So engagement sits inside the engine/APO binaries. The two text-readable
candidates that differ between the devices are engine generation (v6.503/2019 vs
v9.1127/2024) and product tier (`DolbyAtmosSpeakerSystem` vs `DolbyAccess`).
They covary on our two data points, so neither is established. A capture from a
`DolbyAtmosSpeakerSystem`-tier device on a v8+ package would separate them. 28
such hardware IDs appear in corpus packages, re-derived 2026-08-21. Among them
is `17AA380D` itself, which newer Lenovo packages still list at that tier, so
#44's machine on an updated driver would be the cleanest discriminator. Until
then the finding's conclusion stands, strengthened: whatever enables VBE is not
in any file or registry value we can read.

<a id="r-ieq-amount-scaling"></a>

### `ieq-amount` scaling and the HF gap (issue #13)

Reading `ieq-amount` as a percentage, `amount/100` instead of `amount/10`,
removes a ~10× over-weighting of the IEQ and closes the X1 Yoga's HF gap to a ~1
dB residual.

Issue
[#13](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/13)
(taprobane99) argued for biquad fitting over our 20-band → FIR construction. The
thread settled it in favour of the FIR: the cepstral FIR sits within 0.34 dB of
the target, and biquad fits leave 10–16 dB ripple.

**Construction details don't matter (rejected).** He proposed PCHIP
interpolation instead of our linear-in-dB over log-f, a 65536-point cepstral FFT
instead of `FIR_LENGTH`, and explicit DC/Nyquist anchoring. An offline
pre-screen plus an on-device measured sweep put all three within <0.3 dB of the
current construction. The sweep built the 4096-tap min-phase FIR each way,
captured it through the live EE chain with `tools/measure_ee/sweep_variants.sh`
and scored it against the X1 Yoga DAX pink capture. A "construction-only"
variant measured 12.30 dB EE−DAX RMS against the baseline's 12.04: no movement.
Our cepstral FIR already realises the target within ~0.06 dB, and linear-in-dB
interpolation does not Gibbs-ring. The larger FFT only changes <80 Hz, which the
100 Hz HP masks; truncated back to 4096 taps, it slightly *worsens* band
accuracy. The `--fir-interp/--fir-fftsize/--fir-dc-anchor` test flags were
temporary scaffolding, since reverted (see Status below).

Re-checked on the new, flatter `amount/100` target, the
current-vs-full-construction spread is *smaller*, not larger: audible-band (>100
Hz) RMS 0.47 dB against 0.60 dB on the old curve. So the old full-weight HF
error had not masked a construction benefit. As expected, construction governs
realisation fidelity (already ~0.06 dB to the band points), not the target, so
it cannot close the residual ~1 dB EE−DAX gap, which is a target/DAX-internal
difference. Topic closed regardless of baseline: do not reopen without new
evidence.

**Mixed phase is rejected on latency (corroborates the
[DAX phase response](#r-dax-phase-response)).** His notebook ships mixed phase
(causality=0.4), and his published RePhase IRs carry ~20 ms latency. Both have
identical *magnitude* to minimum phase (0.00 dB) but add 6–20 ms group delay and
pre-ring. We keep pure min-phase (causality=1.0, zero added latency). The
[DAX phase response](#r-dax-phase-response) saw the same hybrid-phase character
in DAX and ruled it out on the no-latency constraint.

**The IEQ is applied at ~10× too much weight (the real finding).** He replaced
our full-weight `IEQ + AO` with `AO + 0.10·(IEQ − mean(IEQ))`, noting the full
IEQ "dominates the impulse." Measured on-device (X1 Yoga, dynamic/balanced,
pink, norm @1 kHz, in-band 200–18 kHz):

| variant | EE−DAX RMS | EE−DAX max |
|---|--:|--:|
| baseline `IEQ+AO` | 12.04 | 26.03 |
| `AO + 0.10·IEQ` | **1.03** | **3.94** |
| `AO + 0.25·IEQ` | 2.38 | 5.30 |

Down-weighting collapses the long-standing EE↔DAX treble gap. Per band:

| band | EE−DAX, baseline `IEQ+AO` | EE−DAX, `AO + 0.10·IEQ` |
|---|--:|--:|
| 19.7 kHz | −28.2 dB | −1.5 dB |
| 13.9 kHz | −16.7 | −0.6 |
| 11.25 kHz | −9.7 | +0.1 |

Down-weighting costs ≤1 dB in two already-good mid bands (328 Hz, 4.7 kHz). The
[EE response vs XML](#r-ee-response-vs-xml), the
[AO sign variant matrix](#r-ao-sign-variant-matrix) and the
[XML-interpretation hypotheses](#r-xml-interpretation-hypotheses) attributed
this gap to "fixed DAX-internal HF voicing outside the XML". It is largely our
own interpretation error: we applied the full IEQ as a static EQ.

**Where the weight comes from: `ieq-amount` is a percentage, not a /10 scale.**
The converter mapped `ieq-amount` → `amount/10`: corpus value 10 → scale 1.0 →
full IEQ. Reading the same field as a *percentage*, `amount/100`, gives 10 →
0.10. That is exactly his weight, and exactly the offline optimum: a fine weight
sweep minimises in-band EE−DAX RMS at w=0.100, rising on both sides. Corpus
support: `ieq-amount` is 10 on every internal-speaker profile but 8 on some
headphone profiles, so it is a real per-endpoint field consistent with a
percentage. The mean-centering he added is not load-bearing for the spectral
match. Centered and uncentered give identical normalised shape: centering shifts
only broadband level, which normalisation and the convolver's peak handling
remove. So the essential candidate correction is one line:
`scale = ieq_amount/100`, not `/10`.

**Why a small static weight works — `mi-ieq-steering-enable`.** The IEQ
profile carries `mi-ieq-steering-enable=1`: DAX applies the IEQ through
content-adaptive Media Intelligence steering, not a static EQ. The converter
already flags that MI steering as non-LTI and unreproducible. A low static
weight (~amount/100) approximates the *steady-state* of that dynamic stage. The
percentage reading is therefore itself a steady-state approximation, but
XML-grounded rather than a magic number.

**Status — ADOPTED.** The default mapping is `scale = ieq_amount/100`
(`dolby_to_easyeffects.py`), down from `amount/10`. Evidence: the device-1 DAX
match above, plus a second device, taprobane99's Yoga Slim 7x, where two
independent methods reach the same down-weight: his cepstral notebook at 10%,
and his RePhase hand-tuning, which lands on flat HF (19.7 kHz at −8.5 dB rel.
234 Hz, not the −43 dB of full-weight IEQ). The generated default IR is
byte-identical to the on-device-validated 0.10 variant. The temporary
investigation flags (`--ieq-weight`/`--ieq-center`,
`--fir-interp`/`--fir-fftsize`/`--fir-dc-anchor`) and the `make_fir`
construction parametrisation are reverted: construction tweaks measured
negligible, and mixed phase is rejected on latency.

**Residual open question (falsifiable).** Every speaker DAX capture we have
uses `amount=10`, so we cannot yet distinguish "amount/100 as a true
percentage" from "≈0.10 constant for speakers". The `/100` form is the
XML-grounded reading. It reduces to the confirmed 0.10 at amount=10 and tracks
the corpus variation: `amount=8` on some headphone profiles → 8%. A DAX capture
from a device with `ieq-amount≠10` would confirm or falsify the percentage
interpretation. Until then `/100` is a hypothesis that fits all current
evidence, per the standing principle that the XML→parameter mappings are
empirically falsifiable.

A second, cheaper falsifier surfaced in issue
[#73](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/73)
(2026-08-25): the voicings. Under the `amount/100` reading the whole 20-band
target, voicing shape included, is weighted 0.10. The generated Detailed /
Balanced / Warm kernels therefore sit at most ~1 dB apart: the Detailed−Warm
spread is 0.96 dB, a broad 800 Hz–6 kHz tilt. That is arithmetic on the
Dolby-global curves, confirmed by FFT of the shipped dev-device kernels. The #73
reporter duly found the three "subtle to non-existent". Every DAX capture in
the archive was taken with Dolby Access left on Balanced, so DAX's own
Detailed−Warm delta is unmeasured. A pink/multitone capture on the dev device,
with the voicing set to Detailed and then Warm, settles it:

- A delta of ≤ ~1 dB confirms the weight applies to the whole target.
- A several-dB delta would mean DAX applies the voicing *shape* at more than the
  steady-state weight (e.g. `ieq-amount` scaling only the MI-steered part), and
  the converter's variants are under-differentiated.

<a id="r-simplified-schema-ao-units"></a>

### simplified-schema AO units on a second device (issue #44)

**Verdict (the 2026-07-30 capture set):** the simplified-schema static mapping
is validated end-to-end at the loud operating point. On those captures, the
measured residual vs Windows on the #44 device is Dolby's adaptive layer
(leveler + HF dynamics), not the EQ. No converter change indicated. The round-3
subsection below finds a separate bass loss on this device from our own
regulator.

The issue
[#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44)
reporter (Yoga Slim 7 14ARE05, Realtek ALC287, `SUBSYS_17AA380D`) ran the full
`tools/measure_dax/` battery on Windows: all four stimulus kinds, loud and quiet
variants, Dolby off vs profile `dynamic`. These are the first DAX captures from
a second device, and the first from a *simplified-schema* XML: `gain_l`/`gain_r`
audio-optimizer and no PEQ/MBC, so the convolver is the entire static correction
there. All numbers below were re-derived from the capture set this session, with
`analyze.py` spectra and band-mean deltas. Both curves are referenced at the 234
Hz band, the AO curve's 0 dB point.

**The 1/16-dB unit hypothesis is confirmed.** The pink steady-state dynamic−off
delta tracks the converter's predicted IEQ+AO curve (profile `dynamic`, curve
`balanced`) with mean |error| 0.72 dB (L) / 0.73 dB (R), median 0.52 dB. The
worst is 2.11 dB at the 19.7 kHz edge band, a pink SNR and smoothing limit. The
2250–5813 Hz bands match to ≤0.1 dB, which is tight enough to pin the unit
scale. A 1/8-dB reading would miss those bands by their full 2.2–3.5 dB depth
and a 1/32-dB reading by half of it, both ≫ the ≤0.1 dB observed. This also
generalises the `/16` convention, verified on the full schema by issue
[#15](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/15)'s
settings-file experiment, to the simplified variant's differently-named arrays.

**The per-channel `gain_l`/`gain_r` assignment is confirmed within a single
capture pair.** The one band where this XML's L/R arrays differ, 1688 Hz, 1.0 dB
apart, shows a measured L−R delta difference of 0.8 dB in the same direction
(L −8.28 dB vs R −9.08 dB re 234 Hz).

![Measured Dolby on−off delta vs the converter's predicted curve, both
channels — the curves overlay within a fraction of a dB, splitting L/R only
at the 1688 Hz notch](images/finding10-measured-vs-predicted.png)

**Dolby-off is a true bypass, and loopback taps post-APO.** The off-state
stepped and multitone captures are flat to −0.05 dB with zero cross-pass
adaptive span. So an off/on pair is a clean A/B, and the capture method needs
no correction for the off leg.

**Non-LTI behaviour reproduces on device 2**, consistent with
[DAX LTI behaviour](#r-dax-lti-behaviour):

- Sweeps stay unusable for EQ extraction through DAX. The leveler's time-varying
  gain corrupts the sweep-derived delta: mean band error 4.5 dB vs the
  pink-derived curve, and +10 dB apparent gain at 234 Hz vs +4 dB steady-state.
- Tonal stimuli drive the multiband dynamics hard, so pink remains the EQ-shape
  reference. Multitone shows an extra 3–5 dB of compression above ~850 Hz
  relative to the static curve. The stepped battery's cross-pass adaptive span
  reaches 5.8 dB.
- Level dependence: the quiet-pink delta realises only ~70% of the curve depth
  mid-band, a uniform ~+2 dB shallowing, plus an extra ~5 dB cut at 47 Hz:
  level-adaptive bass management. Our static FIR reproduces the loud/nominal
  operating point, which is the right anchor.

![The measured curve at normal vs quiet input level against the static
prediction — the quiet curve is uniformly shallower mid-band and cuts deep
bass harder](images/finding10-level-dependence.png)

**Leveler magnitude, measured on this device:** broadband pink RMS delta
(dynamic − off) is +8.2 dB at the loud level and +21.8 dB at the quiet level.
Two implications:

- Our XML-derived volmax `input-gain` (+7.0 dB on this XML) lands within ~1 dB
  of DAX's loud-level makeup. This retroactively supports the issue
  [#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23)
  slot default.
- The quiet-content gap our bypassed-by-default autogain leaves is an order of
  magnitude larger than the static-EQ residuals. So the leveler dominates any
  remaining "Windows sounds louder/fuller" impression, quantifying issue
  [#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25)'s
  conclusion on a second device.

**Open question — adaptive activity with no decoded mechanism.** This XML's
`threshold_high` decodes to +0.0 dB (never engages, per our mapping) on bands 10
and 13–20 (1688 Hz, 3750–19688 Hz). Yet the stepped captures show up to ~5 dB of
cross-pass adaptive span on several of them: 5.1 dB at 3750 Hz, and 4.3–5.3 dB
at probes owned by bands 14, 17 and 19. The decoded parameters provide no
mechanism there at all. The span is frequency-selective: near zero at 469–656
Hz, concentrated at 1.9–4.2 kHz, plus spots near 8 and 12.5 kHz. So it is
multiband dynamics, not broadband leveler drift: most plausibly the
Media-Intelligence layer (this profile enables five `mi-*-steering` flags) or
another engine-side adaptive block we classify as non-modelable.

The span does not pattern on either decoded parameter. The *strongest* span on
the device (5.0–5.8 dB) sits in band 11 (2250 Hz), which is threshold-active and
`isolated_band=1`. The inert `isolated_band=0` band 10 (1688 Hz) spans only
1.6–3.2 dB. The previously-unread `isolated_band` array has real per-device
contrast corpus-wide: 59 distinct patterns, an exact threshold-activity mirror
on 18,369 profiles, and ≥1-band divergence on 11,548 (ad-hoc scan 2026-07-30,
2,741 XMLs). Despite that contrast, it is evidence-wise *not* the gate for this
behaviour, and its semantics stay unknown (the
[fixed dynamics constants](#r-fixed-dynamics-constants) (f)). This relates to
the regulator under-engagement thread (the
[MBC ratio and time constants](#r-mbc-ratio-time-constants) and the
[fixed dynamics constants](#r-fixed-dynamics-constants) below). No converter
change is indicated for it: chasing a content-adaptive layer with a static chain
is the same trade rejected in the
[AO sign variant matrix](#r-ao-sign-variant-matrix).

#### Why bypass has more bass than the preset (issue #44, round 3, 2026-08-22)

Two causes explain the reporter's round-3 observation that *"disabling the
preset completely … adds a lot of bass, although everything else does not sound
as good"*, and only the second is ours. Neither cause is the "quieter than
bypass" mechanism of the `--enable level-restore` section below, for three
reasons. This tuning's peak-normalisation deficit is −1.4 dB. This device
carries no protective PEQ high-pass, and no PEQ at all. The
[simplified-schema AO units finding](#r-simplified-schema-ao-units) above
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

This reverses #23's trade on this tuning. There, `output-gain` was the placement
that distorted on loud low frequencies. Here it is cleaner *and* louder than the
default. Both readings can hold, because #23's device and this one sit at
opposite ends of regulator aggressiveness. `--volmax-slot`'s own help already
names "input-gain costs too much loudness on a device with an aggressive
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
is the [DAX virtual-bass finding](#r-dax-virtual-bass)'s VBE reference. Round 4
below uses both.

**Round 4 (2026-08-24): the listener verdict, and the capture.** The reporter
ran `--volmax-slot output-gain` and confirmed it by ear: *"does add the bass
back to the preset … way more balanced this way and I would say it's on par with
what I heard in Windows"*
([comment](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44#issuecomment-5399420807)).
It is the first listener confirmation of the `output-gain` placement, from a
deeper-threshold tuning than #23's: −30.9 dBFS across eleven bands here, against
the X13's −24 dB active minimum. The two field verdicts pick opposite slots
because the devices differ, exactly as the flag's help text predicts, not
because either measurement is wrong. So the default stays `input-gain`, and the
README's tested table carries this device with the flag. Whether an
XML-derivable predictor, such as active-band count or deepest `threshold_high`,
could pick the slot per tuning stays a hypothesis until a third device lands on
one side or the other.

The same comment attached the Windows `stimulus_bass_burst` captures asked for
above: Dolby off and Dynamic, 48 kHz, from `capture_dax.py`. The reporter's
stimulus file is byte-identical to ours. The pair is the first loud-stimulus DAX
capture with an `off` counterpart, and the first from a second device. The dev
X1 Yoga's capture, the [DAX virtual-bass finding](#r-dax-virtual-bass)'s
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
  [simplified-schema AO units finding](#r-simplified-schema-ao-units)) plus the
  output-gain placement, not proof of either. One loose end is open: the 81 and
  182 Hz bands are not among the eleven active ones in our reading, yet DAX
  limits 80 and 180 Hz like their neighbours. Either its band split is coarser
  than the 20 centres, or an inactive band inherits a neighbour's ceiling.
- *DAX distorts deep bass far more than we do.* THD at 50 Hz is 10.1 % (H3
  −21 dB, H5 −28), against 2.3–2.6 % for either EE placement; 3.6 % at 80 Hz,
  0.4 % at 120, 0.08 % at 180. Odd harmonics dominate: limiting or soft
  clipping, not a virtual-bass stage, since #14 established VBE is off on this
  hardware. Out-of-band energy is −72.6 dBFS against −83 for EE. Whatever the
  mechanism, "cleaner than DAX" is not a constraint our chain is failing on this
  content. The defect is "quieter than DAX by 7–9 dB".

**Decision.** This is still not a default flip on its own. The xml-derivability
bar is two devices, and this is one, while #23's evidence for `input-gain` is a
listener verdict with no DAX capture behind it. This one is the first DAX
loud-bass ground truth anywhere in the project. It does settle the round-3
dichotomy: "our staging is wrong on this tuning" wins over "the tuning asks for
this and Windows sounds the same".

The dev X1 Yoga cannot serve as the second device on this stimulus. The check
(2026-08-25) used its own 2026-05-06 DAX capture, which is Dynamic only. `off`
is a verified bypass on that device from the pink/stepped batteries. DAX lands
−0.37 dB below the input below 300 Hz, and per tone −25.2 / −2.3 / +0.2 / +2.8
dB at 50 / 80 / 120 / 180 Hz, with the 50 Hz fundamental replaced by VBE
harmonics (the [DAX virtual-bass finding](#r-dax-virtual-bass)). The EE default,
`output-gain` and the 2026-08-11 default chain all land −4.2…−4.6 dB, within 0.5
dB of *each other*, because its 100 Hz PEQ high-pass takes 50–120 Hz below the
regulator's thresholds before the slot can matter. The dev capture does show a
static low-end gap. EE lands −43 / −20.9 / −7.8 / +0.9 dB against DAX's numbers
above, a gap of 18.6 dB at 80 Hz and 8 dB at 120 Hz. The −25 dBFS quiet burst
shows the same shape under the leveler's +21.7 dB makeup. It is the HP-slope /
LF-leveler deviation that "The 47 Hz deviation" above records, with numbers
added at 80 and 120 Hz.

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
/ +21.8 dB quiet makeup measured in the
[simplified-schema AO units finding](#r-simplified-schema-ao-units). It is
`--enable autogain` / `--enable level-restore` territory, not this subsection's,
and was raised with the reporter in the same thread.

#### Second deep-threshold tuning: issue #84's Yoga Slim 7 Pro 14ACH5 (2026-08-30)

On this tuning the regulator engages at ordinary level, as on #44, and the
distortion it adds measures 1–2 %: grit on paper, not crackle. The report was
"constant crackle on every preset" on a Yoga Slim 7 Pro 14ACH5: 82MS, ALC287
`17AA384F`, full schema, no Dolby MBC in the XML. Its regulator is the #44
class: nine active bands 47–1313 Hz, deepest −29.8 dB at 469 Hz,
`distortion-slope` 1.0 → 100:1, and volmax +5.1 dB on the input. So before the
reporter's own A/B came back, this XML went through the same EE → null-sink
route as the #44 sweep above. It was built with `--prefix` beside the dev
machine's presets. `tools/measure_ee/sweep_variants.sh` takes a `STIMULI`
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
second listener saying our regulator is audible. It is not yet the second DAX
attack curve the regulator attack-time question (the
[fixed dynamics constants](#r-fixed-dynamics-constants)) is parked on, since the
two flags also remove the thresholds, ratio and boost.

### Unvalidated converter scaling factors (the `ieq-amount` class)

The converter carries a cluster of scaling factors that map an XML field onto a
filter parameter through a constant we *invented* rather than confirmed. The
[`ieq-amount` scaling finding](#r-ieq-amount-scaling) showed the risk: it
corrected a scaling *interpretation*, not an arithmetic slip. `ieq-amount` was
read as `amount/10` when the field is a percentage, `amount/100`. The `/16`-dB
convention is the one such constant we have actually verified. In issue
[#15](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/15),
a user set ±12 dB in DAX on Windows and the settings file stored ±192. The rest
below are adopted defaults that ship in the audible path but have never been
individually checked against a DAX capture. They are catalogued here as a class
so a capture campaign can attack them deliberately. The "Follow-ups" list
further down tracks ideas we considered and did *not* adopt; these are live,
shipping defaults.

| Factor | XML field | Path status |
|---|---|---|
| [Dialog-enhancer gain ceiling](#r-dialog-enhancer-gain-ceiling) | `dialog-enhancer-amount` (0–16) | default audible when `dialog-enhancer-enable=1`. X1 Yoga: `dynamic`/`movie` amount=5, `voice` amount=3, off on `music`/`game` |
| [Surround→stereo-base](#r-surround-boost-stereo-base) ✅ | `surround-boost` (1/16 dB) | resolved: widening dropped. It was emitted when surround was present (`surround-boost=96` on `dynamic`/`movie`) |
| [Convolver SoundWire headroom restore](#r-convolver-headroom-restore) ✅ | (none: a post-normalisation heuristic for the IEQ-only, no-AO SoundWire curve) | resolved: restore dropped. It was default audible on SoundWire |
| [Regulator slope→ratio](#r-regulator-slope-ratio) | `regulator-distortion-slope` | regulator only engages at high level |
| [Regulator timbre→knee](#r-regulator-timbre-knee) | `regulator-timbre-preservation` (corpus-frozen at 0.75) | regulator, high level |
| [MBC ratio and time constants](#r-mbc-ratio-time-constants) | `mb-compressor-tuning` 6-tuples | dormant: the MBC doesn't engage on the −10 dBFS test stimuli (the [DAX response vs XML](#r-dax-response-vs-xml)) |
| [Volume-leveler→autogain window](#r-leveler-autogain-window) | `volume-leveler-amount` (0–10) | bypassed by default on HDA, where `--enable autogain` opts in. Active in the conservative SoundWire path |
| [PEQ anti-clipping trim](#r-peq-anti-clipping-trim) | (none: a headroom heuristic over the XML's PEQ gains) | default audible on every XML whose PEQ has boost bells/shelves |
| [SoundWire Calf BassEnhancer constants](#r-soundwire-bass-enhancer-constants) | (none: the XML's `bass-enhancer-*`/VBE fields are corpus-frozen; the [DAX virtual-bass finding](#r-dax-virtual-bass)) | default audible on SoundWire, the most audible invented stage on those devices |
| [Conservative-autogain offsets](#r-conservative-autogain-offsets) | `volume-leveler-out-target` | active on SoundWire; audible on HDA only via `--enable autogain` or manual GUI enable |
| [Fixed dynamics constants](#r-fixed-dynamics-constants) | (none) | dormant at nominal levels (the dynamics-dormant measurement above; device-specific, see the end of the [fixed dynamics constants](#r-fixed-dynamics-constants)); engaged on loud content |

<a id="r-dialog-enhancer-gain-ceiling"></a>

#### Dialog-enhancer gain ceiling

- **Factor (generator):** `amount/16 * 6.0` dB, bell centered 2.5 kHz, Q≈0.7
  (`make_dialog_enhancer`). The SoundWire-only `* 8.0` dB variant and its 4 kHz
  clarity bell at `*0.6` were **REMOVED 2026-07-03** (see the end of this
  entry).
- **Why it's a guess:** The XML gives only an amount. The dB ceiling, center and
  Q are converter-chosen: nothing in the schema says "6 dB".

Verdict: the 6 dB ceiling is **unconfirmed**, and our bell appears to over-apply
vs DAX on the espeak speech and pink captures below. Settling it needs a speech
source that demonstrably engages DAX's DE, ideally in a same-profile
DE-on-vs-off capture. A pink-noise pre-screen is null/confounded, with no static
speech bell (see roadmap): DAX's DE is evidently speech-gated. The battery
carries `stimulus_speech`: espeak-ng synthesis when installed, else an
LTASS-shaped-noise fallback that may not trip an MI speech classifier. The
capture protocol must therefore verify a nonzero DE-on-vs-off contrast before
concluding anything.

**Measured 2026-06-13 with espeak speech on DAX: no DE signature found.** DAX
output for `movie` (DE=5, enabled) and `game` (DE=0) is identical to ±0.00 dB on
*both* speech and pink, which share an IEQ+AO target. Our static chain adds the
modelled bell: EE `movie`−`game` = +1.3 dB @ 1.5–3.5 kHz. Two readings remain
unresolved. The espeak voice, "fairly robotic" per the capture notes, may not
trigger DAX's MI dialogue classifier. Or DAX's DE isn't a static speech-band
boost. Dolby Access exposed no DE toggle for the movie profile, only an
Intelligent-EQ switch (left off), so a same-profile on/off contrast wasn't
capturable.

**SoundWire `*8` arm + 4 kHz bell removed 2026-07-03.** `2f4d0b8` (2026-04-12)
introduced it to add "consonant clarity" on a chain whose 10×-over-applied IEQ
was crushing treble by up to 28 dB. `eeecc4a`/#13 fixed that IEQ on 2026-05-28,
so the arm compensated a since-fixed bug. The measured over-application above
also argues for less dialog gain, not 33% more. The arm also made the generation
banner wrong on SoundWire, because the banner always printed the ×6 figure. Both
device families share the ×6 single-bell mapping. Field evidence: issue
[#29](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/29)
("dynamic wonky, music better"), where DE is the main dynamic-vs-music audible
difference. Restore via git history (`2f4d0b8`) if a SoundWire speech capture
ever shows a stronger DE.

<a id="r-surround-boost-stereo-base"></a>

#### Surround→stereo-base

- **Factor (generator):** `min(boost/20.0, 0.5)`. **REMOVED 2026-06-13**: the
  converter maps `surround-boost` to no widening.
- **Why it's a guess:** The `/20` divisor and 0.5 cap were invented. Dolby
  surround is a spatial renderer; EE `stereo_tools` is a linear M/S balance.

Verdict: adopt the removal, **validated on-device 2026-06-13**. The in-hand data
could not test the mapping. The captured battery uses *correlated* pink
(`stimulus_pink.wav`, corr +1.0, no Side), so the widener is a no-op: surr=96
(`dynamic`/`movie`) and surr=0 (`game`) loopbacks show identical residual
side/mid (≈ −35 dB).

EE half, measured 2026-06: the live chain widens decorrelated pink by +4.10 dB
S/M on `dynamic`/`movie` (surr=96 → stereo-base 0.5) and +0.02 dB on surr=0
profiles (analyzer `sm_delta_db`). DAX half, measured 2026-06-13 with
decorrelated pink captured on Windows: DAX applies essentially ZERO widening.
Its surr=96 (`dynamic`/`movie`) S/M-delta is +0.01 dB, byte-for-band identical
to surr=0 (`game`, +0.02) and to OFF (+0.02), flat across 250 Hz–8 kHz. So our
`/20` mapping added +4 dB of static S/M width on the 2-channel speaker output
that DAX does not produce: a clear over-application.

The phase-widening loophole is closed electrically too. On *correlated* pink
(L/R corr +0.998, room to decorrelate), DAX holds the correlation at +0.997 on
game and dynamic alike: no inter-channel decorrelation, so no phase/XTC widener
either. Our widening chain dropped it to +0.991 (corr input) / −0.45 (decorr
input). A loopback cannot represent crosstalk cancellation's *acoustic* effect
at the ears, or a virtualizer that is content-gated and dormant on stereo noise,
engaging only on multichannel/object Atmos. Settling those needs a binaural
capture / listening test, or an XML A/B with `surround-boost` edited to 0 (the
risky single-block test). For the magnitude M/S rebalance our mapping actually
performed, DAX demonstrably does nothing.

**Provenance:** widening first shipped 2026-02-28 (`82d7f3d`), but no DAX
battery before 2026-06-13 (`measure_dax_3`) contained a decorrelated-stereo
stimulus. The Apr/May sets were pink/sweep/multitone/stepped only. So 2026-06-13
is the *first* DAX widening measurement, with no earlier DAX stereo data to
compare against.

**Why DAX's effect is ~nil (leading hypothesis):** `surround-boost` is almost
certainly a *virtualization/surround-render depth* parameter, not a stereo-width
knob. It gates with `surround-decoder-enable` /
`output-mode-partial-surround-virtualizer-enable`, an FFT-domain
upmix→virtualize stage we classify as non-modelable. Fed plain 2-channel PCM
with no surround/object bed, that renderer has nothing to synthesise, so the
boost scales ≈nothing. `movie` (boost=96) ≡ `game` (boost=0) to 0.01 dB RMS /
≤0.07 dB max in both L and R, not just in S/M. So our mapping is likely wrong
*in kind*, not merely over-scaled: a static width knob for what is really a
multichannel-render gain. On the stereo playback path the converter targets, the
faithful behaviour is to not widen.

**Resolution:** the `surround-boost → stereo_tools` widening was removed from
the converter. `make_stereo_tools`, the emission branch, the `surround` param of
`make_preset` and the `--disable stereo` flag are all gone. `surround` is parsed
and reported as intentionally-unmapped. This one-device decision (no
second-device DAX capture) is justified because it *removes* an unvalidated
invented scaling that the only falsifying signal, a DAX capture, contradicted,
rather than adopting a new mapping. The mechanism (render-depth param, dormant
on stereo) is structural, not per-device. If a future device's DAX capture shows
real widening, restore via git history (`82d7f3d`).

**Validation:** the no-widener chain was re-captured through live EE on
2026-06-13. Decorrelated-pink S/M widening dropped +4.10 → +0.02 dB on every
profile (dynamic/movie/game), matching DAX's +0.01. Correlated pink fell +4.41 →
+0.32 dB; that residual is the device's L/R-asymmetric FIR/PEQ (DAX +0.12), not
widening. Mono did not regress. The rest of the chain's preset JSON is
byte-identical (preset-digest snapshot, now `tests/test_golden_preset.py`). Live
mono-pink matched pre-fix within capture repeatability (~0.45 dB RMS), with
EE−DAX pink steady at 1.35–1.67 dB RMS (the baseline from the
[`ieq-amount` scaling finding](#r-ieq-amount-scaling)).

<a id="r-convolver-headroom-restore"></a>

#### Convolver SoundWire headroom restore

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
set −5 dB output. Removal follows the entry-2 precedent: it *drops* an invented
non-XML gain rather than adopting a new mapping, so the second-device bar
doesn't apply. Loudness makeup is volmax-boost's job (XML-derived, entry on
volmax slots). It is not locally measurable (dev device is HDA); the #29
reporter's regenerate-and-listen is the field check. If a SoundWire DAX capture
ever shows DAX applying net positive gain vs OFF that our chain lacks, restore
via git history (`2f4d0b8`).

<a id="r-regulator-slope-ratio"></a>

#### Regulator slope→ratio

- **Factor (generator):** slope read `/16` (`parse_xml`), then
  `ratio = 1/(1−slope)` (`make_regulator`).
- **Why it's a guess:** The `/16` reading is assumed by analogy to the dB
  fields. `1/(1−slope)` is inferred from how corpus values cluster.
- **What would falsify it:** **Not testable on this device.** The X1 Yoga is
  `distortion-slope=16` on *every* profile, so there is no operating-point
  variation to fit `1/(1−slope)`. It needs a device with differing slope values
  and a bass-burst capture comparing gain-reduction-vs-level (Phase 4).

<a id="r-regulator-timbre-knee"></a>

#### Regulator timbre→knee

- **Factor (generator):** timbre read `/16` (`parse_xml`), then
  `knee = −6·timbre` dB (`make_regulator`).
- **Why it's a guess:** The `−6` dB maximum knee is a pure guess. The field is
  constant across the corpus, so we have no signal to disambiguate.
- **What would falsify it:** **Not testable on this device.** The X1 Yoga is
  `timbre-preservation=12` (=0.75) on *every* profile, so the `−6·timbre`
  scaling has a single operating point. It needs a device whose XML carries
  `timbre≠0.75`, plus a capture (Phase 4).

<a id="r-mbc-ratio-time-constants"></a>

#### MBC ratio and time constants

- **Factor (generator):** MBC ratio `1/(coeff/32768)` (`decode_mbc_bands`); time
  constants via Q15 with `block_size=256` → 187.5 blocks/s
  (`decode_mbc_time_constant`).
- **Why it's a guess:** The Q15 format and 256-sample block size are assumed
  from common DSP practice and only sanity-checked numerically, never measured.

**Diagnosed 2026-06-13:** the loud-level gap is neither the upstream bass-level
gap nor a wrong MBC decode; its actual driver is the regulator under-engaging.
Woken 2026-06-13 with `stimulus_stepped_loud` (−2 dBFS peak), loud-vs-normal
static gain (aligned @1 kHz) shows DAX compressing far harder than our chain:
DAX −10.6 dB GR @234 Hz (EE −5.5), −10.4 @277 (EE −1.7), −5.9 @141 (EE 0), −7.4
@2.25 kHz (EE −3.2). The adaptive cross-pass span is ≤1.5 dB, so this is the
compressor/regulator, not the leveler. A 2026-07-01 re-verification from the raw
held-tone envelopes confirms the leveler's per-tone adaptation does not
contaminate the GR readout at the 234/277 Hz diagnostic bands. There the
within-tone drift is ≤0.16 dB and all three passes agree to ~0.03 dB. The
adaptation is visible only elsewhere: −1.1 dB early-tone at 141 Hz and a 1.5 dB
cross-pass span at 3 kHz.

The diagnosis
([`tools/measure_ee/dynamics_gap.py`](../tools/measure_ee/dynamics_gap.py);
agent analysis, key numbers re-verified from the converter):

- (i) 1 kHz-referenced, the level each chain delivers to its dynamics agrees
  within ±3 dB at every diagnostic band (141–4193 Hz). The "DAX delivers +16–22
  dB more" reading was a reference artifact: our FIR is peak-normalised to a
  different anchor than DAX's OFF-flat baseline. The real 22–30 dB bass gap (the
  [EE response vs XML](#r-ee-response-vs-xml)) sits below ~120 Hz,
  pre-attenuated by the 100 Hz HP before either chain's dynamics.
- (ii) The MBC decode is internally faithful but *conservative*. A 3-level fit
  (−42/−18/−2) shows EE realises its nominal 1.67 ratio only at the one band
  that clears threshold well (234 Hz, R≈1.54). Elsewhere the −6 dB soft knee and
  RMS detection keep it sub-slope.
- (iii) DAX's effective ratio at 234/277 Hz is ≈ 2.95, its near-100:1 regulator
  stacking on the MBC. Our regulator maps the same −10/−9/−8/−5 dB thresholds
  and slope (the [fixed dynamics constants](#r-fixed-dynamics-constants)), yet
  barely fires there.

So the lever is the regulator, not the MBC ratio/threshold, which stays
XML-derived and unchanged. See the
[fixed dynamics constants](#r-fixed-dynamics-constants).

<a id="r-leveler-autogain-window"></a>

#### Volume-leveler→autogain window

- **Factor (generator):** `max-history = 40−amount·4` / `30−amount·5`
  (`make_autogain`).
- **Why it's a guess:** The window formula is invented. It measured as no
  reaction-speed lever at all: 20/32/40 s gave identical ~4 dB onset overshoot
  (see "The 2026-07 default-flip attempt").
- **What would falsify it:** A capture of DAX's MI-steered leveler (non-LTI, so
  hard).

<a id="r-peq-anti-clipping-trim"></a>

#### PEQ anti-clipping trim

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

<a id="r-soundwire-bass-enhancer-constants"></a>

#### SoundWire Calf BassEnhancer constants

- **Factor (generator):** `amount=12 dB`, `harmonics=10`, `blend=−10`,
  `floor=10`, `scope = min(2·hp_freq, 300)` (`make_bass_enhancer`).
- **Why it's a guess:** Every knob is converter-chosen. The `2×` scope
  multiplier derives an emitted parameter from the PEQ HP corner. The constants
  were also tuned (`bc12c2e`, 2026-04-12) against the pre-#13 over-applied-IEQ
  chain, so the 12 dB drive may compensate a since-fixed deficit.
- **What would falsify it:** A SoundWire-device DAX capture with the bass-burst
  stimuli (Snapdragon X / Yoga Slim 7x / the #29 Zenbook).

Kept default-on for now: the [DAX virtual-bass finding](#r-dax-virtual-bass)
shows DAX genuinely runs VBE, so removal re-opens a real gap.

- **#29 round 2 (2026-07-05):** First field evidence of over-drive: issue
  [#29](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/29)
  (Zenbook S14) reported "too bass boosted" plus occasional chassis resonance.
  The reporter manually raised `floor` 10→50 Hz and cut output 5 dB. The #29 A/B
  (`--disable bass-enhancer` vs default) was the intended discriminator, but its
  round-2 result (2026-07-05) is ambiguous. Disabling the stage did *not* fix
  `dynamic`. `music` lands close to Windows *with* it on, since the stage rides
  every profile preset, `music` included. So the report neither condemns nor
  vindicates the whole stage. The reporter's concrete complaint is the
  `floor=10 Hz` constant, a hardware-dependent value the XML doesn't carry:
  drive below the woofer's usable range → chassis resonance. He set `floor`≈80
  Hz and cut the amount.
- **Second negative field report (2026-07-21, issue
  [#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27)
  follow-up, Galaxy Book6 Ultra):** with the machine's Cirrus amp firmware
  finally installed, the amp DSP does real bass management. The reporter then
  needed `--disable bass-enhancer --disable volmax --disable regulator` to avoid
  "dramatic" degradation. This is also confounded: three flags were disabled at
  once, and that run's volmax rode an inert all-0 dB-threshold regulator
  (cross-device-findings §15 addendum). It tilts toward opt-in without deciding
  it.

The SoundWire-*only* gate is contribution-historical (`bc12c2e`, the first
SoundWire user's path), not a principled HDA/SoundWire split. On Linux the HDA
path equally lacks Dolby's Windows-driver VBE, and the
[DAX virtual-bass finding](#r-dax-virtual-bass) measured DAX running VBE on an
HDA device. So the *missing*-on-HDA side is issue
[#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14),
and the *present*-on-SoundWire side is what #29 questions.

**Follow-up gated on #29's XML + capture:** revisit (a) flipping this stage to
opt-in and (b) whether `floor` can be tied to the PEQ HP corner, like `scope`,
instead of a hardcoded 10 Hz.

**#29's XML arrived 2026-08-27 (capture still pending).** On it, (b) as written
is moot. The only PEQ high-pass is in `voice`/`voice_onlinecourse` (type 7, 100
Hz, order 4). `dynamic` and `music` carry bells only, so the HP-corner
derivation returns the 100 Hz fallback on every profile the reporter uses. The
XML does carry the VBE source band that the HDA `--enable virtual-bass` branch
already reads: `virtual-bass-src-freqs = 35,160` (`mix-freqs = 94,469`; corpus
constants, parsed in `lib/dax/parse.py`). So an XML-anchored `floor = 35 Hz` /
`scope = 160 Hz` is the candidate replacement for the invented `10` /
`min(2·hp, 300)`. It would source both device families' bass stage from one XML
block. Deferred 2026-08-28 (reply-only round): it is [AUDIBLE] on every
SoundWire preset and unheard locally, so it waits for the reporter's A/B or
capture.

The XML also moves the `dynamic` verdict off this stage. `dynamic` is the
profile whose tuning enables the volume leveler (amount 5, DRC on) plus every
`mi-*-steering` switch. Our SoundWire path runs that leveler by default without
steering, the #25 failure mode, while `music`, the profile he likes, has it off.
`--disable autogain` (008b4d6) post-dates his tests, so that A/B is the round-3
ask, and the SoundWire arm of the
[conservative-autogain offsets](#r-conservative-autogain-offsets) is what it
exercises.

<a id="r-conservative-autogain-offsets"></a>

#### Conservative-autogain offsets

- **Factor (generator):** `target = out_target − 6.0` dB,
  `silence-threshold = −50` dB (`make_autogain`). Since 2026-07 both paths store
  the −50 gate; the HDA block previously kept EE's −70 plugin default.
- **Why it's a guess:** The −6 dB safety offset and −50 dB threshold are
  invented; the [leveler→autogain window](#r-leveler-autogain-window) covers
  only the window formula. The −50 gate is field-confirmed (#25) and
  capture-measured: +1.7 dB silence wind-up vs +41.8 dB at −70 (see "The 2026-07
  default-flip attempt").
- **What would falsify it:** Same as the
  [leveler→autogain window](#r-leveler-autogain-window): an MI-steered leveler
  capture (hard).

<a id="r-fixed-dynamics-constants"></a>

#### Fixed dynamics constants

- **Factor (generator):** MBC active-band `knee = −6.0` dB
  (`make_multiband_compressor`; the Dolby 6-tuple has no knee field), regulator
  `attack 1.0 ms` / `release 50.0 ms` (`make_regulator`).
- **Why it's a guess:** Chosen from limiting practice, not decoded.

**Engaged 2026-06-13** by `stimulus_stepped_loud` (see the
[MBC ratio and time constants](#r-mbc-ratio-time-constants)). The dynamics
diagnosis lands *here*, on the regulator's fixed constants, not the MBC decode.
Our `make_regulator` maps the XML thresholds/slope correctly (−10/−9/−8/−5 dB,
near-100:1 on the 4 lowest bands) yet under-engages vs DAX, which clearly
hard-limits those bands.

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
  modifier, not a threshold offset. It's the only per-device-varying regulator
  field. On `dynamic` it's `144,144,0,…`, non-zero on exactly bands 0–1 (47/141
  Hz), the under-engaging bands. The
  [`regulator-stress-amount` follow-up](#r-regulator-stress-amount) rejected it
  only under the *threshold-offset* reading, where lowering threshold moved EE
  away from DAX. The new framing (DAX intensifies limiting on "stressed" bands →
  effective ratio ~2.95) is untested and could both explain DAX and stay
  XML-only.
- (e) ~~`regulator-relaxation-amount` (=96) as the release control~~ **Dropped
  2026-06-18.** It is not XML-derivable: it is frozen at 96 across the whole
  corpus, so there is no contrast to decode against. The 2026-07-01 time-flat
  finding also removes its motivation, since release timing isn't the
  under-engagement driver.
- (f) `regulator-tuning/isolated_band` (added 2026-07-30, the
  [simplified-schema AO units finding](#r-simplified-schema-ao-units)) is a
  previously-unread per-band 0/1 array with genuine per-device contrast. It has
  59 corpus patterns, and mirrors threshold-activity exactly on 18,369 profiles
  but diverges on ≥1 band on 11,548. Its semantics are unknown. Probe-level span
  attribution on the #44 stepped data argues it does *not* gate the measured
  adaptive layer. That device carries the discriminating contrast (band 11 iso=1
  vs band 12 iso=0, both threshold-active), and both span ~5 dB alike, while the
  inert iso=0 band 10 spans least (the
  [simplified-schema AO units finding](#r-simplified-schema-ao-units)).

  **Shipped as an experimental opt-in 2026-07-30** (`--enable coupled-bands`),
  **made the default 2026-08-11** (`--disable coupled-bands` opts out).
  Zero-threshold zones whose bands are all `isolated_band=0` join the limiter at
  face value (0 dBFS), so upstream gain (volmax on input-gain) gets tamed there
  before the brickwall. The iso=0 scoping is a conservative gating choice, not
  established causation. The flip knowingly did not clear the
  second-device-capture bar in `.claude/rules/xml-derivability.md`. No capture
  can reach it (see the scope-honesty note below). Two things replaced that bar.
  A two-device software A/B (below) bounds the audible cost. The other is the
  argument that the opposite reading, discarding a stated 0 dBFS threshold,
  leaves the volmax boost feeding the brickwall untamed on exactly the tunings
  where this fires, which is the failure #23 measured.

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
[simplified-schema AO units finding](#r-simplified-schema-ao-units)):** the
issue
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
pink battery: −3.24 dB at 328 Hz, the −30.875 band itself, against ≈0 above 1
kHz. It removes 10.3 dB below 300 Hz on a −5 dBFS bass burst. So the
under-engagement thread above is a statement about shallow-threshold tunings. On
deep-threshold ones the same mapping over-engages relative to what DAX shows at
the one level both were measured. That also gives the volmax-slot question a
second device pointing the opposite way to #23; see the
[simplified-schema AO units finding](#r-simplified-schema-ao-units) subsection
"Why bypass has more bass than the preset". Round 4 of that subsection measured
DAX's own limiter on a −5 dBFS bass burst. The onset passes at full static gain
(0 dBFS peak, the first ~3 ms clipped), and the reduction settles over ~100–150
ms. That is the first direct measurement of a DAX limiter time constant, and it
puts this entry's 1 ms attack about two orders of magnitude too fast on deep
bass.

#### Verification status and the validation roadmap

For contrast, the `/16`-dB convention is verified (issue #15, in the section
introduction), and the `/32768` Q15 decode is at least numerically consistent
with first-order time-constant theory. Everything else above is unverified.

**Validation roadmap.** The steps are ordered by how closely each mirrors the
`ieq-amount` case: a fixed scaling in the default path, measurable against a DAX
capture.

1. *Offline pre-screen, on data already in hand.* Only the dialog enhancer had
   screenable in-hand data, and it came back negative/refining. The screen
   recomputes the current converter's target without the stage under test,
   subtracts it from the matching DAX capture and reads the residual. It is
   the same offline screen that flagged the `ieq-amount` weight before any new
   measurement.
   - **Dialog enhancer
     ([dialog-enhancer gain ceiling](#r-dialog-enhancer-gain-ceiling)): done,
     result negative/refining.** Across the X1 Yoga pink battery, the profiles
     that differ only in DE amount do not differ in steady-state magnitude.
     `movie` (amount=5) vs `game` (amount=0) is ~0.01 dB RMS in-band, and
     `dynamic`/`movie`/`game` all sit within ~1 dB RMS despite DE 5/5/0, far
     below the modelled ~1.25 dB bell. Profiles with *identical* IEQ+AO (`music`
     vs `game`, both DE off) differ by ~4 dB RMS. So per-profile MI voicing
     ([DAX LTI behaviour](#r-dax-lti-behaviour), non-LTI) dwarfs and is
     uncorrelated with DE amount. So the DE is content-adaptive (speech-gated):
     pink cannot excite it, and cross-profile differencing on pink is the wrong
     test. Validating the 6/8 dB ceiling needs a speech / speech-shaped
     stimulus. Because MI voicing differs per profile, it also needs a
     *same-profile* DE-on-vs-off capture rather than a cross-profile comparison.
   - **Surround ([surround→stereo-base factor](#r-surround-boost-stereo-base)):
     screened, not testable offline.** The captured battery uses correlated pink
     (`stimulus_pink`, corr +1.0), which has no Side component for the widener
     to act on. Falsifying the `/20` mapping needs the decorrelated
     `stimulus_stereo_pink` captured in stereo (Phase 3).
   - **Regulator ([regulator slope→ratio factor](#r-regulator-slope-ratio),
     [regulator timbre→knee factor](#r-regulator-timbre-knee)): not testable on
     this device.** The X1 Yoga's slope and timbre values are identical on every
     profile, so there is no operating-point variation to fit `1/(1−slope)` or
     `−6·timbre`. These need a device with non-default values (Phase 4),
     independent of stimulus.
2. *New in-house captures (X1 Yoga, HDA).* Both sides are done, and the
   remaining open asks are second-*device* ones (below), not this device's.
   Reproduce:
   [`tools/measure_ee/scaling_report.py`](../tools/measure_ee/scaling_report.py).
   Tooling: `tools/measure_dax/` (Windows capture) and `tools/measure_ee/`
   (Linux loopback).
   - **Linux side, done 2026-06-12.** The EE battery was regenerated after the
     [`ieq-amount` scaling finding](#r-ieq-amount-scaling) for all five
     profiles. Pink residuals reproduce the
     [`ieq-amount` scaling finding](#r-ieq-amount-scaling)'s table (`dynamic`
     1.03 dB RMS), and `music`'s ~3.5 dB MI-voicing outlier is unchanged. On the
     new speech stimulus, EE treats speech and pink identically, confirming our
     DE is static where DAX's is gated. The new decorrelated stereo gives the EE
     half of the [surround→stereo-base factor](#r-surround-boost-stereo-base):
     +4.10 dB S/M at surr=96. The new `stimulus_stepped_loud` (−2 dBFS peak)
     crosses the ≈ −6.4 dBFS MBC knee. The EE dynamics wake exactly at the
     high-chain-gain bands, −5.5 dB GR @234 Hz / −3.2 dB @2.25 kHz
     ([MBC ratio and time constants](#r-mbc-ratio-time-constants),
     [fixed dynamics constants](#r-fixed-dynamics-constants)).
   - **Windows side, done 2026-06-13:** 26 DAX captures at pinned 50% volume,
     speech / pink / decorrelated-stereo per profile plus `stepped_loud` on
     off/dynamic. They followed a minor Dolby Access update, but the
     residuals barely moved, so it reads as a clean second capture, not a
     confound. Headline results, scored EE↔DAX:
     - **[`ieq-amount` scaling finding](#r-ieq-amount-scaling) confirmed on a
       second DAX session.** Pink EE−DAX RMS is 0.97–1.49 dB across profiles,
       and the 19.7 kHz Δ is −1.5…−3.2 dB (vs −28 dB before the
       [`ieq-amount` scaling finding](#r-ieq-amount-scaling)). The `/100` HF fix
       holds. `music`, the prior 3.5 dB outlier, is now 1.09 dB.
     - **[Surround→stereo-base factor](#r-surround-boost-stereo-base),
       over-application found:** DAX widening at surr=96 is zero (S/M-delta
       +0.01 dB, identical to surr=0/off); our chain adds +4.10 dB.
     - **[MBC ratio and time constants](#r-mbc-ratio-time-constants) and
       [fixed dynamics constants](#r-fixed-dynamics-constants), DAX compresses
       ~2× harder** at loud level: −10.6 dB GR @234 Hz vs EE −5.5, strong over
       140–400 Hz and 1.9–4.7 kHz. This is entangled with the bass-level gap.
     - **[Dialog-enhancer gain ceiling](#r-dialog-enhancer-gain-ceiling), no DE
       signature** on espeak speech (`movie` DE=5 ≡ `game` DE=0 to ±0.00 dB),
       unresolved: the robotic voice may not trigger MI, and Dolby Access
       offered no movie DE toggle.
     - **[PEQ anti-clipping trim](#r-peq-anti-clipping-trim), still confounded**
       by the leveler's common loudness target even at pinned volume.
3. *User-contributed data.* The X1 Yoga is an HDA device and is corpus-frozen
   on several fields, so some entries can only be falsified by other
   hardware/XMLs:
   - a SoundWire device for the
     [SoundWire bass-enhancer constants](#r-soundwire-bass-enhancer-constants)
     and the [conservative-autogain offsets](#r-conservative-autogain-offsets),
     which need a SoundWire-device DAX capture (the #29 Zenbook S14 is the first
     candidate);
   - a device with `ieq-amount≠10` for the
     [`ieq-amount` scaling finding](#r-ieq-amount-scaling) residual;
   - a device with `regulator-timbre-preservation≠0.75` or a differing
     `regulator-distortion-slope` for the
     [regulator slope→ratio factor](#r-regulator-slope-ratio) and the
     [regulator timbre→knee factor](#r-regulator-timbre-knee).

   The [convolver headroom restore](#r-convolver-headroom-restore) and the
   SoundWire dialog arm of the
   [dialog-enhancer gain ceiling](#r-dialog-enhancer-gain-ceiling) owed no
   capture: they were instead *removed* 2026-07-03 as invented gains
   compensating the since-fixed #13 IEQ over-application. The
   [PEQ anti-clipping trim](#r-peq-anti-clipping-trim), the
   [SoundWire bass-enhancer constants](#r-soundwire-bass-enhancer-constants),
   the [conservative-autogain offsets](#r-conservative-autogain-offsets) and the
   [fixed dynamics constants](#r-fixed-dynamics-constants) (added in the 2026-06
   review) piggyback on the same campaign. The
   [PEQ anti-clipping trim](#r-peq-anti-clipping-trim) needs narrow-vs-wide-Q
   profile captures on existing HDA hardware, and the
   [fixed dynamics constants](#r-fixed-dynamics-constants) folds into the
   loud-content captures that wake the dynamics stages (the
   [MBC ratio and time constants](#r-mbc-ratio-time-constants)). The converter's
   `_UNMODELED_FEATURES` warning already nudges users to report XMLs whose
   `regulator-overdrive`/`relaxation-amount` deviate from the corpus constants.
   Track the asks on a GitHub issue (cf. issue
   [#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14)
   for the VBE follow-up).

On-device ground truth decides any EE↔DAX measurement, and the offline
pre-screens are only a filter. Changing a default mapping requires a
second-device confirmation. The
[`ieq-amount` scaling finding](#r-ieq-amount-scaling) met it via convergent
second-device evidence, two independent methods on a Yoga Slim 7x, though not
yet a second DAX *capture*. Its "residual open question" tracks what a capture
would add.

### Follow-ups to close the gap to DAX

After the [`ieq-amount` scaling finding](#r-ieq-amount-scaling), two gaps
remain: the ~1 dB HF residual and the LF/leveler gap. Closing them needs either
data outside the XML or a relaxation of the determinism / latency constraints.
When this list was assembled, the cheap, deterministic, XML-only experiments
looked exhausted: hypothesis (b) rejected (the
[AO sign variant matrix](#r-ao-sign-variant-matrix)), no missed XML block (the
[HF-shaping block audit](#r-hf-shaping-block-audit)), 5-profile coverage in (the
[AO sign variant matrix](#r-ao-sign-variant-matrix)). The
[`ieq-amount` scaling finding](#r-ieq-amount-scaling) then closed most of the HF
gap with exactly such an experiment, a re-reading of a field we already parsed,
so "exhausted" was wrong. Items are cited by their `r-` tags. Items are grouped
by status.

**Closed by the variant sweep (the
[AO sign variant matrix](#r-ao-sign-variant-matrix) and the
[XML-interpretation hypotheses](#r-xml-interpretation-hypotheses)):** listed
under [The variant sweep](#r-variant-sweep) below.

<a id="r-single-block-xml-ab"></a>

#### Stripped-down single-block tuning XML A/B on Windows

Status: still actionable, no constraint change.

It remains the sharpest tool for what is left: pinpointing which DAX stage
carries the ~1 dB HF residual and the LF/leveler behavior. Disable everything
except IEQ in a tuning XML and capture DAX, then add AO, then add per-band PEQ,
and so on. The HF/mid gap from before the
[`ieq-amount` scaling finding](#r-ieq-amount-scaling) that motivated it is now
mostly closed. The risk is unchanged, so weigh it against that much smaller
payoff. The A/B needs driver-level XML replacement, and could brick DAX on the
test machine until restoration. Scope it before attempting.

<a id="r-regulator-stress-amount"></a>

#### `regulator-stress-amount` mapping investigated and rejected

Status: closed, no constraint change — kept as a permanent finding.

The threshold-offset mapping, tested under alignment hypothesis A, is
directionally falsified at 180 Hz. The item reopened 2026-06-13 under a
different reading.

Issue
[#11](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/11)
raised whether DAX's "tier-2" adaptive sub-models could explain part of the
EE-vs-DAX gap. A corpus audit across ~2,900 XMLs settled the
schema-prevalence side of that question:

| field | enabled / non-default in any XML |
|---|---|
| `sliding-bass-enable=1` | 5 IdeaPad-3 XMLs (all `max-gain=0`, dormant) |
| `volume-modeler-enable=1` | 0 |
| `process-optimizer-enable=1` | 0 (bands always `array_20_zero`) |
| `regulator-stress-amount` | non-zero on bass bands in 86% of XMLs |
| `regulator-overdrive` | always `0` (35,654 profile slots) |
| `regulator-relaxation-amount` | always `96` (13,042 profile slots) |

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
[MBC ratio and time constants](#r-mbc-ratio-time-constants) and the
[fixed dynamics constants](#r-fixed-dynamics-constants)) found the regulator
under-engages on exactly bands 0–1 (47/141 Hz), precisely where `stress=144,144`
sits. The same diagnosis found DAX's effective ratio there (~2.95) far exceeds
our 1.67. That points at the "stress may not be a threshold offset at all"
reading above. Re-test `stress-amount` as an engagement / aggressiveness
modifier, intensifying limiting (ratio/attack) on stressed bands, which the
original threshold-offset experiment never tried. Queue it alongside the
regulator-only capture (the
[fixed dynamics constants](#r-fixed-dynamics-constants)). The
`regulator-relaxation-amount` companion-decode was dropped 2026-06-18: it is not
XML-derivable, frozen at 96 corpus-wide. The 2026-07-01 re-analysis found the
under-engagement is static, not release-timing (the
[fixed dynamics constants](#r-fixed-dynamics-constants)).

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

Bigger picture, linking back to the [EE response vs XML](#r-ee-response-vs-xml):
DAX delivers 22-30 dB more bass to its regulator than our chain delivers to
ours, then runs a much more active regulator on top. That is the gap to close,
and the stress field can't reach it. Two architectural levers might:

- Less aggressive bass attenuation in the FIR/PEQ stages, so our regulator sees
  content above its threshold. This is currently a layer-2 IEQ/AO interpretation
  question (the [AO sign variant matrix](#r-ao-sign-variant-matrix) and the
  [XML-interpretation hypotheses](#r-xml-interpretation-hypotheses)).
- A level-dependent / VBE / leveler stage upstream of the regulator. No LSP
  equivalent of DAX's leveler exists, so it would need custom DSP or a
  different plugin pipeline.

Both are larger pieces of work than this follow-up's scope.

`sliding-bass-*`, `volume-modeler-*` and `process-optimizer-bands` are out of
scope: XML-zeroed across the corpus, they would change zero output samples on
shipped tunings. They are listed here so they don't get re-proposed.

<a id="r-hybrid-phase-matching"></a>

#### Match DAX's hybrid phase character

Status: out of scope unless a constraint changes.

It needs a partial-linear-phase FIR, which adds ~20–40 ms group delay. The
no-added-latency constraint rules it out, and relaxing that needs an explicit
decision. The `--fir-phase=linphase` flag is the upper-bound experiment for
this. The [AO sign variant matrix](#r-ao-sign-variant-matrix) shows pure
linear-phase doesn't help magnitude.

<a id="r-dax-leveler-approximation"></a>

#### Approximate DAX's leveler / regulator

Status: out of scope unless a constraint changes.

It would close the multitone-LF gap and the −18 vs −42 dBFS sweep difference, at
substantial RE effort. Naively re-enabling EE autogain reintroduces the pumping
trap (see "Why autogain is bypassed by default").

<a id="r-fit-to-dax-capture"></a>

#### Empirically tune the preset to match DAX's *captured* response, not the XML's published curves

Status: pragmatic shortcut if determinism is relaxed.

Fit a FIR + biquad chain to the DAX pink-noise capture directly. This loses the
"we faithfully apply the published XML" property, but produces a Linux preset
that audibly matches Windows. It could be opt-in via a flag so the principled
path stays the default. The [AO sign variant matrix](#r-ao-sign-variant-matrix)
identified per-band dB targets *under the scaling before the
[`ieq-amount` scaling finding](#r-ieq-amount-scaling)*: flatten HF above ~10
kHz, soften +4 dB at 2.25 kHz, lift 5–6 kHz. Those targets are obsolete, and
tuning to them today would re-introduce the error that finding removed. A tuner
now would fit against fresh captures taken since, targeting the ~1 dB HF
residual and the LF/leveler gap in its table.

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
[simplified-schema AO units finding](#r-simplified-schema-ao-units) confirmed
against DAX captures on a simplified-schema device. No clamp exists on our side.
At 23.7 dB p-p this tuning is wider than 95% of simplified files; see the corpus
context in
[cross-device-findings.md](cross-device-findings.md#curves-pinned-at-the-declared-gain-range).

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
  +9 dB volmax boost straight into the −1 dBFS brickwall unprotected. This XML's
  `threshold_high` is 0 dBFS on bands 0–3 and −6.4…−15.4 dB on bands 4–8. So
  per-band limiting covers roughly 469–1313 Hz, exactly the region the FIR
  already cut by 10–23 dB. `isolated_band` marks bands 0–3 non-isolated, i.e.
  DAX couples them to the limited bands. That is the gap
  `--enable coupled-bands` addresses.

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
  [surround→stereo-base factor](#r-surround-boost-stereo-base)):
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
[cross-device-findings §11](cross-device-findings.md#11-mi-steering),
MI steering is a `dynamic`-profile feature almost everywhere, in 3805/3825 rows.
It is "the key feature that the EasyEffects pipeline cannot replicate". So our
"first profile" default systematically picks the profile whose Windows behaviour
depends most on what we cannot reproduce. It applies statically what DAX steers
by content. On this XML `music` switches all five off, along with the surround
decoder and the dialog enhancer, and drops the leveler from 7 to 4. That makes
it the profile whose static translation is most faithful. Issue #29's reporter
independently preferred `music` on a different device. That is the real
argument for following `<default_profile>`, and it generalises beyond issue
#46.

What shipped from this: the unlimited-boost warning, the `default_profile`
report and the headless EasyEffects probe fix. This XML declares `music`, and
we build `dynamic`. What deliberately did not ship is any knob that
scales or clamps the AO curve, because that would be a hand-tuned offset. The
in-GUI per-effect bypass already brackets the question: switching off
`convolver#0` isolates the curve from the dynamics without a rebuild. If the
curve is confirmed as the cause, the answer is to find what DAX does that we
don't, not a fudge factor.

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
[PEQ anti-clipping trim](#r-peq-anti-clipping-trim) entry. That entry records
the offset as "leveler boost + our −3 dB trim + convolver peak-normalisation,
inseparable".

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
by default, so the per-band limiter sees it before the brickwall. Issue #23
measured that placement at 0.06% THD against 11.6% for the post-band
alternative.

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
"the default 1 kHz normalization destroys exactly this observable". The offset
was measured twice, at −11.5 dB and then ≈−8 dB. Two things buried it anyway:

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
the [leveler→autogain window](#r-leveler-autogain-window) and the
[conservative-autogain offsets](#r-conservative-autogain-offsets) call invented.
On this device DAX's leveler gain is +7.1 dB at −17.8 dBFS and +16.4 dB at −41.8
dBFS. Those are two real points on a curve our autogain currently approximates
with chosen constants.

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

The level dependence reproduces, but **its magnitude and slope are
device-specific**: 0.39 against 0.67 dB per dB. One set of autogain constants
cannot fit both. That is the sharpest argument yet that the
[leveler→autogain window](#r-leveler-autogain-window) and the
[conservative-autogain offsets](#r-conservative-autogain-offsets) need measuring
rather than choosing, and a reason to run the ladder on more than one machine.

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

**It does not explain the entries-6/11 under-engagement.** On this stimulus
neither side crosses a threshold. For EE default, EE restored *and* DAX, 0 of 4
active bands are above threshold. DAX's closest approach is −7.04 dB. A dormant
compressor cannot exhibit a ratio. So `pink` at −17.8 dBFS carries no
information either way about the 100:1-realises-as-1.8 finding. That figure came
from a *loud* stepped capture where both sides did engage, and nothing here
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

## Rejected approaches

Ideas investigated and explicitly declined, recorded so they don't get
re-proposed:

- **`filter_coefficients` as an audio EQ source.** The base64-encoded biquad
  blob in `tuning-vlldp` is almost certainly VLLDP-internal analysis filters,
  not an audio-path equaliser. It was investigated as a possible
  speaker-correction EQ, but the decoded coefficients don't produce sensible
  audio curves. The audio-optimizer + speaker-PEQ parameters already capture the
  same speaker correction, so nothing is lost by ignoring it.
  It is listed under "Not implemented" in [reference.md](reference.md).
- **Noise gate before the compressor.** It would prevent noise-floor
  amplification, but real content rarely has an audible noise floor at the
  levels that trigger the compressor. It adds complexity for no practical
  benefit.
- **GPU compute for FIR convolution.** The FIR convolver uses <0.1% of a single
  CPU core, so there's no CPU pressure. CPU→GPU round-trip latency is
  unacceptable for realtime audio. See `docs/alternative-pipelines.md` Option 5.
- **Custom SOF DSP topology with FIR + DRC modules.** It has the highest offload
  potential, but requires rebuilding signed firmware and custom topology files.
  That is too much maintenance burden for a workstation tool. See
  `docs/alternative-pipelines.md` Option 2.
- **Parametric-EQ approximation of the IEQ curve** (instead of FIR). Every
  solver tried left large inter-band ripple. The 20-value IEQ arrays (e.g.
  `ieq_balanced`) are the desired *composite* frequency response, not individual
  filter gains. Applied directly as parametric bell gains, they stack to
  +20–30 dB at mid frequencies. The numbers below are reproduced on the X1 Yoga
  Gen 7 / Realtek 17AA:22E6 dynamic / balanced curve. The IdeaPad XML from issue
  [#4](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/4)
  gives a comparable shape.

  | Approach | Peak error vs target | RMS error vs target |
  |---|---:|---:|
  | Raw values as bell gains (Q=1.5) | ~34 dB (cumulative mid boost from overlapping filters) | ~20 dB |
  | Iterative solver (center-freq only) | ~16 dB between bands (0.1 dB at the 20 centres) | ~2 dB |
  | Least-squares solver (dense grid) | ~11 dB | ~1.6 dB |
  | **FIR convolution (current)** | **0.34 dB across audible band, 0.07 dB at the 20 band centres** | **<0.1 dB** |

  All figures are peak and RMS on the same dense log-frequency grid, 20 Hz–22
  kHz, 800 points. An earlier revision of this table quoted "±5 / ±4 dB ripple"
  for the biquad-fit rows and "≤0.06 dB everywhere" for FIR; those mixed peak
  and RMS metrics across rows. The fits were measured against the full-weight
  IEQ target from before the
  [`ieq-amount` scaling finding](#r-ieq-amount-scaling). Today's `/100` target
  is far flatter in its IEQ component, so the quoted magnitudes overstate the
  current gap. The conclusion stands regardless, because the AO component keeps
  full per-band swings and the min-phase FIR realises the composite target
  exactly at zero latency and negligible CPU, so a PEQ approximation has no
  upside.
- **Auto-trimming the convolver IR to its audible length.** Issue
  [#11](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/11)
  noted that the 4096-tap (~85 ms) IR has a long sub-noise-floor tail. A sweep
  measured trim length across 729 FIRs from 11 device groups: Realtek HDA,
  Senary, Qualcomm, AMD, and ThinkPad / IdeaPad / AIO variants. Trim length is
  the smallest cutoff beyond which every tail sample is below the FIR's peak by
  ≥ N dB, rounded up to a 64-sample boundary. Distributions of trimmed length as
  % of the original 4096 taps:

  | threshold | mean | p10 | p50 | p90 | max | mean ms saved |
  |-----------|-----:|----:|----:|----:|----:|--------------:|
  | −80 dB    |  48% | 34% | 52% | 55% | 66% |       ~44 ms  |
  | −90 dB    |  54% | 50% | 53% | 63% | 78% |       ~39 ms  |
  | −100 dB   |  60% | 52% | 56% | 73% | 94% |       ~34 ms  |
  | −110 dB   |  69% | 53% | 66% | 91% |100% |       ~26 ms  |
  | −120 dB   |  81% | 63% | 80% | 98% |100% |       ~17 ms  |

  Per-device means at −100 dB clustered tightly, at 56–69% across all codecs
  except one 3-FIR outlier at 88%. The trim is not device-specific, so it would
  be safe to ship. But EasyEffects' Convolver wraps `libzita-convolver` directly
  and calls
  `Convproc::configure(2, 2, kernel.sampleCount(), bufferSize, bufferSize, Convproc::MAXPART, density)`
  ([EE source][ee-conv]), i.e. `minpart == quantum == bufferSize`.
  zita-convolver is a non-uniform partitioned FFT convolver. The first
  (smallest) partition sets I/O latency, and progressively larger partitions
  process the tail. With `minpart` pegged to the audio quantum, the convolver
  adds zero latency on top of the PipeWire buffer for any IR length up to
  multi-second IRs. So trimming would save ~½ of an already <0.1%-of-a-core
  convolver workload and ~16 KB per file, with no audible or perceptible-latency
  change. Not worth the maintenance cost of a threshold parameter that would
  invite future "is this audible?" re-litigation each time the cepstral
  construction is touched.
- **An *unused* EasyEffects built-in to cover a dropped DAX feature**
  (EE-built-in plugin gap audit, 2026-06-22). The audit is the primary-converter
  counterpart to the PW-converter audit that closed the autogain gap: see
  "Translating active autogain to LSP `autogain_stereo`". The PW converter can
  host any LV2 plugin, so that audit asked "is there a better plugin?", and
  answered it once (autogain). The EE preset can't: EasyEffects is not a generic
  LV2/LADSPA host. It draws each effect's controls by hand and exposes only a
  curated built-in set. The experimental "Native window of effects" / "Update
  frequency — Related to LV2 plugins" toggles only surface the *bundled*
  LSP/Calf plugins' own GUIs, not arbitrary loading (EE
  [Discussion #2928](https://github.com/wwmm/easyeffects/discussions/2928),
  [Issue #1433](https://github.com/wwmm/easyeffects/issues/1433)). So the only
  question is "does an unused built-in better represent a DAX feature we drop?"
  Across all ~19 unused effects (Exciter, Crystalizer, Maximizer, Loudness, Bass
  Loudness, Crossfeed, Speech Processor, …) the answer is no, for three reasons
  that recur:
  - **Corpus-dormant.** Virtual-bass, graphic-EQ and volume-modeler are disabled
    on every corpus XML with frozen params, so there is no per-device signal. A
    Bass Loudness / Loudness / Exciter mapping would be pure invention against
    XML-only derivability.
  - **Validated to zero effect.** For surround/height widening, a DAX capture
    showed Dolby applies no stereo widening on 2-ch content, so the old
    `stereo_tools` mapping was *removed*: see the
    [surround→stereo-base factor](#r-surround-boost-stereo-base). Re-adding it
    via Crossfeed/Stereo Tools re-introduces a falsified effect.
  - **Content-gated, not static.** DAX's dialog enhancer is MI-steered, not a
    static spectral boost. EE treats speech and pink identically: see the
    [dialog-enhancer gain ceiling](#r-dialog-enhancer-gain-ceiling). Our static
    PEQ bell already *over-applies*, and an Exciter is *more* static invention,
    not a better match.

  The one genuine functional gap is Dynamic Speaker Optimization (DSO). It is
  excursion-aware bass limiting, active on 1 newer SoundWire XML: see
  [cross-device-findings.md](cross-device-findings.md) newer-pipeline DSP
  blocks. It is a real, enabled feature with no representation, but it fails
  both bars. The `dynamic-speaker-optimization-amount`/`-speaker-interval` →
  MBC-band-0 threshold transfer is opaque: Dolby's driver-size excursion model
  leaves no derivable mapping. It exists on a single device, which makes it
  unvalidatable. And a crude MBC band-0 limiter would add the pumping DSO is
  built to avoid. It stays warned-at-parse, not mapped, which is the correct
  state. Net: the remaining fidelity work is device-gated *tuning* of plugins
  already in the chain ("Unvalidated converter scaling factors"), not new
  plugins.

- **Caching the LV2 port schemas anywhere but in memory** (corpus tier,
  2026-08-08). What `lv2info` reports for a URI is a property of the installed
  plugin, not of the XML under test. So the corpus tier hands
  `lib.pipewire.validate.run` a session-scoped dict and pays for each URI once.
  Measured across `pytest tests/corpus/ --run-slow` on a 3,057-XML corpus:

  | | Before | After |
  |---|---:|---:|
  | `lv2info` execs | 8,082 | 21 |
  | Wall clock, whole tier (one figure per run) | 385/384 s | 243/269/271 s |
  | Wall clock, the `ee_to_pipewire` module on its own (all that changed) | 170 s | 24 s |

  The memo is an argument rather than an `lru_cache` inside `lib/`, because the
  two production callers, the converter and the `validate_conf.py` CLI, validate
  one conf each and would never see a hit. A module-level cache would be
  process-global mutable state shipped for a test's benefit. Entries are
  `(schema_or_None, note)`, not bare schemas, so a URI whose `lv2info` failed is
  warned about on *every* conf it leaves unchecked rather than only the first.
  Two further steps were declined:
  - **An on-disk cache.** The key is the real obstacle. The URI alone goes stale
    the moment a distro updates LSP, so the key would have to hash the plugin's
    `.ttl` bundles, most of the cost the cache set out to avoid. It would also
    have to carry our own parser's version, or a `git bisect` across
    `_parse_lv2info` would read entries some other parser wrote. Every other
    cache failure in this repo costs a slow run or a false alarm. This one would
    be false clearance on the check that exists to catch a silently muted band:
    "a check that stops checking without stopping"
    ([code-organisation.md](code-organisation.md)). And the prize is not close:
    the in-memory memo already takes the tier to 21 execs, where disk would take
    it to 3.
  - **Skipping structurally identical confs.** With the execs gone, what remains
    of the per-XML cost mostly *is* the out-of-range and toggled-port checks,
    the entire remaining signal. Deduplicating confs by shape would trade the
    check itself for a handful of CPU-seconds.

## Closed follow-ups

Closed groups moved from "Follow-ups to close the gap to DAX" above. Each
leaves a one-line entry there that links here.

<a id="r-variant-sweep"></a>

### The variant sweep

**Closed by the variant sweep (the
[AO sign variant matrix](#r-ao-sign-variant-matrix) and the
[XML-interpretation hypotheses](#r-xml-interpretation-hypotheses)).** These stay
as historical record; do not re-litigate them without new evidence. All predate
the [`ieq-amount` scaling finding](#r-ieq-amount-scaling): the `/100` reading
has since largely closed the HF residual these variants traded against.

  - "Try `IEQ − AO`": rejected, +7–20 dB worse on every profile.
  - "Run on the other 4 profiles": done. The HF gap is profile-independent.
  - "Audit the XML schema for missed HF-shaping blocks": done, none found
    (the [HF-shaping block audit](#r-hf-shaping-block-audit)).
  - "Soften the HP at 100 Hz from `x2` to `x1`": the test XML's HP is XML-driven
    (order=4 → x2), not the `make_peq_eq` filler path, so softening would
    diverge from the deterministic mapping. The `no-HP` variant in the
    [XML-interpretation hypotheses](#r-xml-interpretation-hypotheses) confirms
    the HP is responsible for ~25 dB at 47 Hz. Removing it overshoots DAX, so
    the HP topology is correct; only the slope might differ.
  - "Drop a 2.25 kHz attenuation bell in `equalizer#1`": it would work as an
    empirical fix for the +4 dB band but loses XML-determinism. Folded into
    the [fit-to-DAX-capture follow-up](#r-fit-to-dax-capture) above.
  - "Soft-clamp the IEQ+AO target depth (α)": the
    [XML-interpretation hypotheses](#r-xml-interpretation-hypotheses), a pareto
    trade. Every clamp depth swaps HF residual for mid-band residual, and no
    setting moves every band toward DAX.
  - "Reinterpret `ieq-amount` as a +/- dB cap (β)": the
    [XML-interpretation hypotheses](#r-xml-interpretation-hypotheses), the
    *cleanest candidate*. Every band moves toward DAX with no regression, and
    19.7 kHz gains +9.7 dB. It was not adopted because the 19.7 kHz gap was
    still 18 dB after applying it. The
    [`ieq-amount` scaling finding](#r-ieq-amount-scaling) has since settled it:
    the percentage reading (`/100`) achieves the down-weight through a simpler
    XML-grounded rule and closes the residual β couldn't. Re-litigating the cap
    reading on top of `/100` would double-count the down-weight, so it is
    closed, not "worth revisiting".
  - "Apply IEQ only inside a frequency window (γ)": the
    [XML-interpretation hypotheses](#r-xml-interpretation-hypotheses), a pareto
    trade. It gives the biggest HF reduction (−10.5 dB at 19.7 kHz), but 47 Hz
    blows out from −8 to −18 dB EE−DAX.

[ee-conv]: https://github.com/wwmm/easyeffects/blob/dc14767e8bcf/src/convolver_zita.cpp#L103
[Filter.cpp]: https://github.com/lsp-plugins/lsp-dsp-units/blob/master/src/main/filters/Filter.cpp
