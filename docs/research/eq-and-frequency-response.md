# EQ and frequency response: IEQ, audio optimizer, FIR, XML units and phase

## Where this stands

[reference.md](../reference.md) covers what the converter emits.

- **The FIR** is 4096-tap minimum-phase, within ≤0.06 dB at the 20 band
  centres ([accuracy](#r-fir-accuracy)).
- **`ieq-amount`** is read as a percentage, adopted from issue #13's thread. On
  the X1 Yoga's dynamic/balanced it took in-band pink EE−DAX RMS from 12.04 to
  1.03 dB, leaving a ~1 dB HF residual and the LF/leveler gap
  ([scaling](#r-ieq-amount-scaling), [implications](#r-dax-gap-implications)).
  On the #13 reporter's Yoga Slim 7x, a cepstral notebook and RePhase
  hand-tuning reach the same weight; neither is a DAX capture.
- **The audio optimizer** is added: subtracting it was +7–20 dB worse on the X1
  Yoga ([sign](#r-ao-sign-variant-matrix)). On #44's simplified-schema Yoga Slim
  7 14ARE05, DAX's pink on/off delta tracked our curve to ~0.7 dB mean at the
  loud operating point, confirming the 1/16-dB units. One capture pair confirms
  the `gain_l`/`gain_r` assignment, at the one band where they differ (1688 Hz)
  ([units](#r-simplified-schema-ao-units)).

Open:

- Whether `/100` is a true percentage or a ≈0.10 constant for speakers: every
  speaker DAX capture uses `amount=10`, so it needs a capture from a device with
  `ieq-amount≠10` ([scaling](#r-ieq-amount-scaling)).
- DAX's own Detailed−Warm voicing delta is unmeasured. A dev-device pink or
  multitone capture on Detailed, then Warm, shows whether the weight applies to
  the whole target ([scaling](#r-ieq-amount-scaling)).
- At 47 Hz, whether DAX applies a shallower HP or its leveler boost compensates
  a similar one: the variant sweep cannot tell
  ([hypotheses](#r-xml-interpretation-hypotheses)).
- #44's device shows up to ~5 dB of adaptive span on bands whose thresholds
  decode to +0.0 dB, with no decoded mechanism
  ([units](#r-simplified-schema-ao-units)).
- Matching DAX's hybrid phase, seen on the X1 Yoga
  ([phase](#r-dax-phase-response)), is out of scope unless the latency
  constraint changes. Fitting the preset to a DAX capture needs determinism
  relaxed and fresh captures ([hybrid](#r-hybrid-phase-matching),
  [fit](#r-fit-to-dax-capture)).

<a id="r-simplified-schema-gain-arrays"></a>

## Simplified-schema XMLs: `gain_l`/`gain_r` audio-optimizer (issue #22)

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
    content (the
    [surround→stereo-base factor](adaptive-processing.md#r-surround-boost-stereo-base)).
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
[DAX virtual-bass finding](virtual-bass.md#r-dax-virtual-bass)
investigates it at length, including the `--enable-vbe` experiment. Issue
[#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14)
tracks the opt-in baseline. So there is nothing simplified-schema-specific to
add here. The explicit `boost`/`cutoff`/`width` look mappable to Calf
`bass_enhancer`, but being corpus-frozen, they would be a hardcoded baseline
rather than derived tuning.

Issue #22's field follow-up is at
[easyeffects-and-pipewire.md#r-preset-loads-but-inaudible](easyeffects-and-pipewire.md#r-preset-loads-but-inaudible).

<a id="r-fir-accuracy"></a>

## FIR accuracy and time-domain envelope

These sanity-checks are the derivations and accuracy measurements behind the
values catalogued in [reference.md](../reference.md).

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

<a id="r-dax-phase-response"></a>

## DAX3's phase response

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
carry time-varying gain
([DAX LTI behaviour](adaptive-processing.md#r-dax-lti-behaviour)),
which Farina deconvolution folds into pre- and post-peak energy, biasing the
ratio toward looking linear-phase. If this characterization ever becomes
load-bearing, the multitone capture's per-band phase is the cleaner signal. No
converter decision rests on it.

So our generated FIR cannot match DAX3's exact phase behaviour in any profile.
The no-added-latency constraint forces min-phase regardless of this finding.
Minimum-phase is the right trade-off for an EQ correction filter, and we accept
that this diverges from Dolby's choice.

<a id="r-dax-response-vs-xml"></a>

## DAX3 response vs the published XML curves

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

## EE-on-Linux response vs the XML

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

## Audit for a missed HF-shaping XML block

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

## Testing hypotheses (a) and (b)

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
data outside the XML, such as the
[single-block XML A/B](measuring-against-windows.md#r-single-block-xml-ab) on
Windows.

<a id="r-dax-gap-implications"></a>

## Implications for the converter

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
   playback. EasyEffects' autogain is bypassed by default, as
   [Why autogain is bypassed by default](adaptive-processing.md#r-autogain-bypassed-by-default)
   explains. A content-adaptive leveler equivalent
   would require approximating Media Intelligence steering, a substantial
   undertaking.

The captures and analysis tooling under `tools/measure_dax/` are kept for future
debugging: re-running on a new device or after a Dolby driver update is a
one-command repeat.

<a id="r-xml-interpretation-hypotheses"></a>

## Five XML-interpretation hypotheses

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
[DAX-leveler approximation follow-up](adaptive-processing.md#r-dax-leveler-approximation).
The flags were reverted in the same commit that landed this finding.

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
unique prefixes are unneeded. See
[Rewriting `{preset}.irs` in place](easyeffects-and-pipewire.md#r-irs-in-place-rewrite).

**A note on metrics.** The `summarise_variants.py` output reports two residuals,
`vsDAX` and `vsXML`. They answer different questions, and `vsXML` is the weaker
signal:

- `vsDAX` is the residual against the captured DAX response, and the only
  external check. DAX captures are imperfect: they cover one device and one
  driver, and DAX itself is non-LTI per
  [DAX LTI behaviour](adaptive-processing.md#r-dax-lti-behaviour). They
  are still the only data point not derived from our own assumptions. A
  candidate rule that moves EE materially closer to DAX *without giving up
  ground in other bands* is evidence our current rule is wrong. That holds even
  if the new rule lowers `vsXML`, since `vsXML` is computed against our own,
  possibly wrong, interpretation.
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
[DAX-leveler approximation follow-up](adaptive-processing.md#r-dax-leveler-approximation).

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

<a id="r-ieq-amount-scaling"></a>

## `ieq-amount` scaling and the HF gap (issue #13)

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

## simplified-schema AO units on a second device (issue #44)

**Verdict (the 2026-07-30 capture set):** the simplified-schema static mapping
is validated end-to-end at the loud operating point. On those captures, the
measured residual vs Windows on the #44 device is Dolby's adaptive layer
(leveler + HF dynamics), not the EQ. No converter change indicated. The
[#44 round-3 bass-loss unit](loudness-and-limiting.md#r-deep-threshold-bass-loss)
finds a separate bass loss on this device from our own regulator.

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
at the 1688 Hz notch](../images/finding10-measured-vs-predicted.png)

**Dolby-off is a true bypass, and loopback taps post-APO.** The off-state
stepped and multitone captures are flat to −0.05 dB with zero cross-pass
adaptive span. So an off/on pair is a clean A/B, and the capture method needs
no correction for the off leg.

**Non-LTI behaviour reproduces on device 2**, consistent with
[DAX LTI behaviour](adaptive-processing.md#r-dax-lti-behaviour):

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
bass harder](../images/finding10-level-dependence.png)

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
[fixed dynamics constants](loudness-and-limiting.md#r-fixed-dynamics-constants)
(f)). This relates to the regulator under-engagement thread (the
[MBC ratio and time constants](adaptive-processing.md#r-mbc-ratio-time-constants)
and the
[fixed dynamics constants](loudness-and-limiting.md#r-fixed-dynamics-constants)).
No converter change is indicated for it: chasing a content-adaptive layer with a
static chain is the same trade rejected in the
[AO sign variant matrix](#r-ao-sign-variant-matrix).

<a id="r-hybrid-phase-matching"></a>

## Match DAX's hybrid phase character

Status: out of scope unless a constraint changes.

It needs a partial-linear-phase FIR, which adds ~20–40 ms group delay. The
no-added-latency constraint rules it out, and relaxing that needs an explicit
decision. The `--fir-phase=linphase` flag is the upper-bound experiment for
this. The [AO sign variant matrix](#r-ao-sign-variant-matrix) shows pure
linear-phase doesn't help magnitude.

<a id="r-fit-to-dax-capture"></a>

## Empirically tune the preset to match DAX's *captured* response, not the XML's published curves

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

## Rejected approaches

<a id="r-filter-coefficients-blob"></a>

### `filter_coefficients` as an audio EQ source

**`filter_coefficients` as an audio EQ source.** The base64-encoded biquad
blob in `tuning-vlldp` is almost certainly VLLDP-internal analysis filters,
not an audio-path equaliser. It was investigated as a possible
speaker-correction EQ, but the decoded coefficients don't produce sensible
audio curves. The audio-optimizer + speaker-PEQ parameters already capture the
same speaker correction, so nothing is lost by ignoring it.
It is listed under "Not implemented" in [reference.md](../reference.md).

<a id="r-parametric-eq-approximation"></a>

### Parametric-EQ approximation of the IEQ curve

**Parametric-EQ approximation of the IEQ curve** (instead of FIR). Every
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

<a id="r-convolver-ir-trim"></a>

### Auto-trimming the convolver IR to its audible length

**Auto-trimming the convolver IR to its audible length.** Issue
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

## Closed follow-ups

Closed groups moved from
["Follow-ups to close the gap to DAX"](measuring-against-windows.md#r-dax-gap-follow-ups).
Each leaves a one-line entry there that links here.

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

## Elsewhere

- The capture method and stimuli behind these units:
  [measuring-against-windows.md](measuring-against-windows.md#r-dax-capture-method).
- The single-block tuning-XML A/B on Windows:
  [measuring-against-windows.md](measuring-against-windows.md#r-single-block-xml-ab).
- The `ieq-amount` fix re-confirmed on the X1 Yoga's second DAX session:
  [measuring-against-windows.md](measuring-against-windows.md#r-validation-roadmap).
- The equalizer rows of the plugin parameter audit, bell width among them:
  [design-notes](../design-notes.md#plugin-parameter-audit).
- Why a sweep through DAX recovers no true linear impulse response:
  [adaptive-processing.md](adaptive-processing.md#r-dax-lti-behaviour).
- The PEQ anti-clipping trim:
  [loudness-and-limiting.md](loudness-and-limiting.md#r-peq-anti-clipping-trim).
- #46's T495, where no knob scales or clamps the AO curve:
  [loudness-and-limiting.md](loudness-and-limiting.md#r-gain-rail-tuning).
- DAX's harmonic bass synthesis, a non-LTI gap distinct from the EQ curve:
  [virtual-bass.md](virtual-bass.md#r-dax-virtual-bass).

[ee-conv]: https://github.com/wwmm/easyeffects/blob/dc14767e8bcf/src/convolver_zita.cpp#L103
