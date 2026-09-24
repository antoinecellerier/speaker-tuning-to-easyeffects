# Measuring against Windows: capture method, metrics and the validation bar

## Where this stands

[reference.md](../reference.md) covers what the converter emits.

- **The capture pair**: `tools/measure_dax/` captures DAX3's output on Windows
  via WASAPI loopback, and `tools/measure_ee/` runs the same stimulus battery
  through a live EasyEffects instance ([method](#r-dax-capture-method)).
- **The devices**: nine findings, from DAX's LTI behaviour through the
  `ieq-amount` scaling, come from a ThinkPad X1 Yoga Gen 7 that matches the
  development tuning XML. The simplified-schema AO units finding covers a
  second device, #44's Yoga Slim 7 14ARE05 ([method](#r-dax-capture-method)).
- **The validation bar**: on-device ground truth decides any EE↔DAX
  measurement; offline pre-screens are only a filter. Changing a default mapping
  requires a second-device confirmation ([roadmap](#r-validation-roadmap)).
- **The X1 Yoga campaign**: both sides are done, the EE battery on 2026-06-12
  and 26 DAX captures at pinned 50% volume on 2026-06-13
  ([roadmap](#r-validation-roadmap)).

Open:

- A SoundWire-device DAX capture, for the SoundWire bass-enhancer constants and
  the conservative-autogain offsets. The #29 Zenbook S14 is the first candidate
  ([roadmap](#r-validation-roadmap)).
- A device with `ieq-amount≠10`, and one with
  `regulator-timbre-preservation≠0.75` or a differing
  `regulator-distortion-slope` ([roadmap](#r-validation-roadmap)).
- The `ieq-amount` fix met the second-device bar through two independent
  methods on a Yoga Slim 7x, not yet through a DAX capture on a second device
  ([roadmap](#r-validation-roadmap)).
- The PEQ anti-clipping trim's `min(1, 2/Q)` shape needs a wide-vs-narrow-Q
  second device: the dev device's PEQ is identical in every profile
  ([trim](loudness-and-limiting.md#r-peq-anti-clipping-trim)).
- The single-block tuning-XML A/B on Windows is the sharpest tool left for
  pinpointing which DAX stage carries the ~1 dB HF residual and the LF/leveler
  behaviour. Its payoff is much smaller now that the HF/mid gap that motivated
  it is mostly closed. It needs driver-level XML replacement, and could brick
  DAX on the test machine until restoration
  ([single-block](#r-single-block-xml-ab)).

<a id="r-dax-capture-method"></a>

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
the same XML and profile. The original suite (`27822c3`) had five stimulus
kinds; `tools/measure_dax/make_stimulus.py` now builds the full set. The five:

- **sweep** (exponential 20 Hz–22 kHz, −18 dBFS peak): Farina deconvolution
  recovers an LTI IR if the system is LTI.
- **sweep_quiet** (−42 dBFS peak): the same sweep at a much lower input level.
- **pink / pink_quiet**: stationary pink noise, for the steady-state magnitude
  after the leveler settles.
- **multitone**: 20 pure tones at the Dolby band centers, for per-band
  amplitude *and phase* via single-bin DFT.

The nine findings from
[DAX LTI behaviour](adaptive-processing.md#r-dax-lti-behaviour) through
the
[`ieq-amount` scaling finding](eq-and-frequency-response.md#r-ieq-amount-scaling)
come from a ThinkPad X1 Yoga Gen 7 with a Realtek ALC287, subsystem 17AA:22E6,
which matches the development tuning XML, `DEV_0287` keyed `SUBSYS_17AA22E6`.
The
[simplified-schema AO units finding](eq-and-frequency-response.md#r-simplified-schema-ao-units)
covers a second device, a Yoga Slim 7 14ARE05 with the simplified schema, whose
battery arrived 2026-07-30 via issue
[#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44).

<a id="r-validation-roadmap"></a>

## Validation roadmap

The steps are ordered by how closely each mirrors the `ieq-amount` case: a fixed
scaling in the default path, measurable against a DAX capture.

1. *Offline pre-screen, on data already in hand.* Only the dialog enhancer had
   screenable in-hand data, and it came back negative/refining. The screen
   recomputes the current converter's target without the stage under test,
   subtracts it from the matching DAX capture and reads the residual. It is
   the same offline screen that flagged the `ieq-amount` weight before any new
   measurement.
   - **Dialog enhancer
     ([dialog-enhancer gain ceiling](adaptive-processing.md#r-dialog-enhancer-gain-ceiling)):
     done, result negative/refining.** Across the X1 Yoga pink battery, the
     profiles that differ only in DE amount do not differ in steady-state
     magnitude. `movie` (amount=5) vs `game` (amount=0) is ~0.01 dB RMS in-band,
     and `dynamic`/`movie`/`game` all sit within ~1 dB RMS despite DE 5/5/0, far
     below the modelled ~1.25 dB bell. Profiles with *identical* IEQ+AO (`music`
     vs `game`, both DE off) differ by ~4 dB RMS. So per-profile MI voicing
     ([DAX LTI behaviour](adaptive-processing.md#r-dax-lti-behaviour),
     non-LTI) dwarfs and is uncorrelated with DE amount. So the DE is
     content-adaptive (speech-gated): pink cannot excite it, and cross-profile
     differencing on pink is the wrong test. Validating the 6/8 dB ceiling needs
     a speech / speech-shaped stimulus. Because MI voicing differs per profile,
     it also needs a *same-profile* DE-on-vs-off capture rather than a
     cross-profile comparison.
   - **Surround
     ([surround→stereo-base factor](adaptive-processing.md#r-surround-boost-stereo-base)):
     screened, not testable offline.** The captured battery uses correlated pink
     (`stimulus_pink`, corr +1.0), which has no Side component for the widener
     to act on. Falsifying the `/20` mapping needs the decorrelated
     `stimulus_stereo_pink` captured in stereo (Phase 3).
   - **Regulator
     ([regulator slope→ratio factor](loudness-and-limiting.md#r-regulator-slope-ratio),
     [regulator timbre→knee factor](loudness-and-limiting.md#r-regulator-timbre-knee)):
     not testable on this device.** The X1 Yoga's slope and timbre values are
     identical on every profile, so there is no operating-point variation to fit
     `1/(1−slope)` or `−6·timbre`. These need a device with non-default values
     (Phase 4), independent of stimulus.
2. *New in-house captures (X1 Yoga, HDA).* Both sides are done, and the
   remaining open asks are second-*device* ones (below), not this device's.
   Reproduce:
   [`tools/measure_ee/scaling_report.py`](../../tools/measure_ee/scaling_report.py).
   Tooling: `tools/measure_dax/` (Windows capture) and `tools/measure_ee/`
   (Linux loopback).
   - **Linux side, done 2026-06-12.** The EE battery was regenerated after the
     [`ieq-amount` scaling finding](eq-and-frequency-response.md#r-ieq-amount-scaling)
     for all five profiles. Pink residuals reproduce the
     [`ieq-amount` scaling finding](eq-and-frequency-response.md#r-ieq-amount-scaling)'s
     table (`dynamic` 1.03 dB RMS), and `music`'s ~3.5 dB MI-voicing outlier is
     unchanged. On the new speech stimulus, EE treats speech and pink
     identically, confirming our DE is static where DAX's is gated. The new
     decorrelated stereo gives the EE half of the
     [surround→stereo-base factor](adaptive-processing.md#r-surround-boost-stereo-base):
     +4.10 dB S/M at surr=96. The new `stimulus_stepped_loud` (−2 dBFS peak)
     crosses the ≈ −6.4 dBFS MBC knee. The EE dynamics wake exactly at the
     high-chain-gain bands, −5.5 dB GR @234 Hz / −3.2 dB @2.25 kHz
     ([MBC ratio and time constants](adaptive-processing.md#r-mbc-ratio-time-constants),
     [fixed dynamics constants](loudness-and-limiting.md#r-fixed-dynamics-constants)).
   - **Windows side, done 2026-06-13:** 26 DAX captures at pinned 50% volume,
     speech / pink / decorrelated-stereo per profile plus `stepped_loud` on
     off/dynamic. They followed a minor Dolby Access update, but the
     residuals barely moved, so it reads as a clean second capture, not a
     confound. Headline results, scored EE↔DAX:
     - **[`ieq-amount` scaling finding](eq-and-frequency-response.md#r-ieq-amount-scaling)
       confirmed on a second DAX session.** Pink EE−DAX RMS is 0.97–1.49 dB
       across profiles, and the 19.7 kHz Δ is −1.5…−3.2 dB (vs −28 dB before the
       [`ieq-amount` scaling finding](eq-and-frequency-response.md#r-ieq-amount-scaling)).
       The `/100` HF fix holds. `music`, the prior 3.5 dB outlier, is now 1.09
       dB.
     - **[Surround→stereo-base factor](adaptive-processing.md#r-surround-boost-stereo-base),
       over-application found:** DAX widening at surr=96 is zero (S/M-delta
       +0.01 dB, identical to surr=0/off); our chain added +4.10 dB, and the
       widening was removed the same day.
     - **[MBC ratio and time constants](adaptive-processing.md#r-mbc-ratio-time-constants)
       and
       [fixed dynamics constants](loudness-and-limiting.md#r-fixed-dynamics-constants),
       DAX compresses ~2× harder** at loud level: −10.6 dB GR @234 Hz vs EE
       −5.5, strong over 140–400 Hz and 1.9–4.7 kHz. The same-day diagnosis
       rules out the bass-level gap as its cause: the regulator under-engages
       ([MBC ratio and time constants](adaptive-processing.md#r-mbc-ratio-time-constants)).
     - **[Dialog-enhancer gain ceiling](adaptive-processing.md#r-dialog-enhancer-gain-ceiling),
       no DE signature** on espeak speech (`movie` DE=5 ≡ `game` DE=0 to ±0.00
       dB), unresolved: the robotic voice may not trigger MI, and Dolby Access
       offered no movie DE toggle.
     - **[PEQ anti-clipping trim](loudness-and-limiting.md#r-peq-anti-clipping-trim),
       still confounded** by the leveler's common loudness target even at pinned
       volume.
3. *User-contributed data.* The X1 Yoga is an HDA device and is corpus-frozen
   on several fields, so some entries can only be falsified by other
   hardware/XMLs:
   - a SoundWire device for the
     [SoundWire bass-enhancer constants](virtual-bass.md#r-soundwire-bass-enhancer-constants)
     and the
     [conservative-autogain offsets](adaptive-processing.md#r-conservative-autogain-offsets),
     which need a SoundWire-device DAX capture (the #29 Zenbook S14 is the first
     candidate);
   - a device with `ieq-amount≠10` for the
     [`ieq-amount` scaling finding](eq-and-frequency-response.md#r-ieq-amount-scaling)
     residual;
   - a device with `regulator-timbre-preservation≠0.75` or a differing
     `regulator-distortion-slope` for the
     [regulator slope→ratio factor](loudness-and-limiting.md#r-regulator-slope-ratio)
     and the
     [regulator timbre→knee factor](loudness-and-limiting.md#r-regulator-timbre-knee).

   The
   [convolver headroom restore](loudness-and-limiting.md#r-convolver-headroom-restore)
   and the SoundWire dialog arm of the
   [dialog-enhancer gain ceiling](adaptive-processing.md#r-dialog-enhancer-gain-ceiling)
   owed no capture: they were instead *removed* 2026-07-03 as invented gains
   compensating the since-fixed #13 IEQ over-application. The
   [PEQ anti-clipping trim](loudness-and-limiting.md#r-peq-anti-clipping-trim),
   the
   [SoundWire bass-enhancer constants](virtual-bass.md#r-soundwire-bass-enhancer-constants),
   the
   [conservative-autogain offsets](adaptive-processing.md#r-conservative-autogain-offsets)
   and the
   [fixed dynamics constants](loudness-and-limiting.md#r-fixed-dynamics-constants)
   (added in the 2026-06 review) piggyback on the same campaign. The
   [PEQ anti-clipping trim](loudness-and-limiting.md#r-peq-anti-clipping-trim)
   needs a wide-vs-narrow-Q second device, and the
   [fixed dynamics constants](loudness-and-limiting.md#r-fixed-dynamics-constants)
   folds into the loud-content captures that wake the dynamics stages (the
   [MBC ratio and time constants](adaptive-processing.md#r-mbc-ratio-time-constants)).
   The converter's `_UNMODELED_FEATURES` warning already nudges users to report
   XMLs whose `regulator-overdrive`/`relaxation-amount` deviate from the corpus
   constants. Track the asks on a GitHub issue (cf. issue
   [#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14)
   for the VBE follow-up).

On-device ground truth decides any EE↔DAX measurement, and the offline
pre-screens are only a filter. Changing a default mapping requires a
second-device confirmation. The
[`ieq-amount` scaling finding](eq-and-frequency-response.md#r-ieq-amount-scaling)
met it via convergent second-device evidence, two independent methods on a Yoga
Slim 7x, though not yet a second DAX *capture*. Its "residual open question"
tracks what a capture would add.

<a id="r-dax-gap-follow-ups"></a>

## Follow-ups to close the gap to DAX

After the
[`ieq-amount` scaling finding](eq-and-frequency-response.md#r-ieq-amount-scaling),
two gaps remain: the ~1 dB HF residual and the LF/leveler gap. Closing them
needs either data outside the XML or a relaxation of the determinism / latency
constraints. When this list was assembled, the cheap, deterministic, XML-only
experiments looked exhausted: hypothesis (b) rejected (the
[AO sign variant matrix](eq-and-frequency-response.md#r-ao-sign-variant-matrix)),
no missed XML block (the
[HF-shaping block audit](eq-and-frequency-response.md#r-hf-shaping-block-audit)),
all 5 profiles covered (the
[AO sign variant matrix](eq-and-frequency-response.md#r-ao-sign-variant-matrix)).
The
[`ieq-amount` scaling finding](eq-and-frequency-response.md#r-ieq-amount-scaling)
then closed most of the HF gap with exactly such an experiment, a re-reading of
a field we already parsed, so "exhausted" was wrong. Items are cited by their
`r-` tags. Items are grouped by status.

**Closed by the variant sweep (the
[AO sign variant matrix](eq-and-frequency-response.md#r-ao-sign-variant-matrix)
and the
[XML-interpretation hypotheses](eq-and-frequency-response.md#r-xml-interpretation-hypotheses)):**
listed under
[The variant sweep](eq-and-frequency-response.md#r-variant-sweep).

<a id="r-single-block-xml-ab"></a>

### Stripped-down single-block tuning XML A/B on Windows

Status: still actionable, no constraint change.

It remains the sharpest tool for what is left: pinpointing which DAX stage
carries the ~1 dB HF residual and the LF/leveler behavior. Disable everything
except IEQ in a tuning XML and capture DAX, then add AO, then add per-band PEQ,
and so on. The HF/mid gap from before the
[`ieq-amount` scaling finding](eq-and-frequency-response.md#r-ieq-amount-scaling)
that motivated it is now mostly closed. The risk is unchanged, so weigh it
against that much smaller payoff. The A/B needs driver-level XML replacement,
and could brick DAX on the test machine until restoration. Scope it before
attempting.

## Elsewhere

- The scaling-factor catalogue the roadmap validates:
  [design-notes](../design-notes.md#unvalidated-converter-scaling-factors-the-ieq-amount-class).
- Why a sweep through DAX recovers no true linear impulse response:
  [adaptive-processing.md](adaptive-processing.md#r-dax-lti-behaviour).
- The `vsDAX` and `vsXML` residuals the variant sweep reports:
  [eq-and-frequency-response.md](eq-and-frequency-response.md#r-xml-interpretation-hypotheses).
- The closed follow-ups, listed under the variant sweep:
  [eq-and-frequency-response.md](eq-and-frequency-response.md#r-variant-sweep).
- The VBE chain's scoring protocol against DAX captures:
  [virtual-bass.md](virtual-bass.md#r-dax-virtual-bass).
