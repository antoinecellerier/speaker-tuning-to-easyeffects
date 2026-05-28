# Design notes

> Interpretive analysis of the parameter schema used by Dolby DAX3 tuning XML (distributed publicly as part of Windows audio driver packages), for the purpose of Linux interoperability. No verbatim tuning arrays are reproduced.

Why the generated EasyEffects preset looks the way it does. The README covers *what*
the script emits; this doc covers the architectural *why*, so future readers don't
have to reverse-engineer it from commit history.

## Dolby's signal flow: CP → VLLDP

DAX3 splits processing into two stages, reflected in the XML under `tuning-cp` and
`tuning-vlldp`:

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

- **CP** is content-dependent: dialog enhancement, intelligent EQ, volume leveling.
  It's Media-Intelligence-steered — Dolby analyses the audio content in real time
  and tells these stages when to hold or act.
- **VLLDP** is speaker-dependent: correction curves, per-channel biquads, multiband
  dynamics, and a per-band regulator that clamps specific frequency ranges to
  protect physical drivers.

The generated EasyEffects chain mirrors this split as closely as LV2 plugins allow.

## Plugin chain order

Current order (see `make_preset` in `dolby_to_easyeffects.py`):

```
Convolver → Stereo Tools → Equalizer (PEQ) → Dialog Enhancer EQ
    → Autogain → MB Compressor → Regulator → Limiter
```

Rationale for the non-obvious ordering decisions:

- **Autogain sits before the compressor**, not at the chain end (commit `7de8866`).
  Earlier versions followed the "autogain always last" EasyEffects convention, but
  that put the volume leveler downstream of everything with no safety net — any
  post-silence overshoot went straight to the output. Moving autogain upstream of
  the compressor and regulator matches Dolby's CP → VLLDP boundary: the volume
  leveler is in CP, the dynamics stages are in VLLDP, and the VLLDP stages catch
  overshoot from CP.

- **A brickwall limiter is appended at the chain end** (commit `1b14bc1`) even
  though the regulator already performs per-band limiting. Cross-device data
  (`docs/cross-device-findings.md` §6) shows 53% of devices use
  `regulator-distortion-slope=16` — a true brickwall — while the other 47% use a
  softer slope. The explicit LSP limiter is redundant on the brickwall-slope
  devices and essential on the rest.

- **Dialog enhancer runs before the volume leveler** (commit `1709e5d`). Dolby
  boosts speech energy before measuring loudness so the leveler doesn't over-react
  to dialog-heavy passages.

## Gain-staging budget

Each stage in the chain is a potential gain trap. The key decisions:

| Stage | Gain | Reason |
|-------|------|--------|
| Convolver (FIR peak-normalized) | 0 dB | Script normalizes the FIR so peak frequency response = 0 dB |
| Convolver plugin `autogain` | **explicitly `false`** | EasyEffects' default is `true`, which re-normalizes by RMS power. Our minimum-phase FIR concentrates energy at the peak sample → RMS power ≈ 0.00001 → the default would apply a **+50 dB boost**. Commit `5973326` disables it. |
| PEQ `output-gain` | narrowband-scaled | Compensates for the highest PEQ bell gain, but scaled down for narrow-Q bells because a Q=4.6 bell only boosts a thin slice of spectrum. Commit `c36907c` relaxed this from full compensation. |
| Regulator `output-gain` (volmax) | +6 dB typical (device/profile-specific) | Dolby's `volmax-boost` (the volume-leveler's loudness-maximiser ceiling) is applied here as a static approximation of VolMax. Primary slot: `multiband_compressor#1.output-gain`. If the regulator is disabled or absent, the gain falls back to `limiter#0.input-gain`. Can be turned off with `--disable volmax`. Commit `19a1f99` had removed a prior (wrongly-placed) mapping to MBC output-gain; this re-adds it in a topologically correct spot. |
| MBC upward compression | **0 dB** | LSP plugin defaults enable upward compression below `boost-threshold=-72 dB`. Dolby's compressor is purely downward. Commit `e454711` disables it on both MBC instances. |
| Regulator upward compression | **0 dB** | Same LSP default issue — upward compression on a *limiter* is especially wrong. Also fixed in `e454711`. |
| Output limiter | −1 dBFS | Final catch-all for inter-sample peaks after everything else. |

With these fixes in place, the normal-operation surplus is small enough that content
sits at target loudness without the regulator triggering, and worst-case quiet-input
scenarios are caught by the brickwall limiter rather than clipping the output.

## Plugin parameter audit

Every JSON key our generated preset emits is set explicitly by the
converter — there are no inherited LSP / EasyEffects defaults sneaking
through. But many of the values we hardcode are **converter-level
judgments** rather than XML-derived, and judgment calls are exactly
where past LSP-default traps have bitten (convolver `autogain` → +50 dB,
MBC upward compression → noise-floor amplification). This table is the
audit of every hardcoded knob we currently ship, so future readers can
distinguish "we tested this and it's right" from "we chose this and
moved on."

Risk class:
  - **AUDIBLE** — a different value would change steady-state magnitude,
    transient behavior, or dynamics character.
  - **TOPOLOGY** — affects routing or signal flow but not the
    in-band magnitude under nominal conditions.
  - **SAFE** — choice is constrained to one value (e.g. dithering off
    on a master limiter), or alternatives are obviously inferior.

| plugin | parameter | current | risk | rationale / status |
|---|---|---|---|---|
| convolver#0 | `autogain` | `false` | AUDIBLE | Trap fix (commit `5973326`). LSP default is `true`, which RMS-normalises the FIR — gives a +50 dB boost on our peak-normalised minimum-phase IR. Must stay false. |
| convolver#0 | `ir-width` | `100` | TOPOLOGY | Stereo image width in convolver's mid/side decode. 100 = pure stereo passthrough. |
| stereo_tools#0 | `mode` | `"LR > LR (Stereo Default)"` | TOPOLOGY | Plain L/R passthrough. M/S processing isn't relevant — Dolby's surround widener is parameterised on stereo width, not M/S. |
| equalizer#0 | `mode` | `"IIR"` | AUDIBLE | Biquad realisation of the per-band PEQ. Alternatives FIR / FFT / SPM. FFT mode would reproduce the band targets exactly at every FFT bin instead of analytically (open: candidate test). |
| equalizer#0 | `q-mode` | (LSP default) | AUDIBLE | We don't set this; LSP default applies. Convention question: "traditional" vs "mathematical" Q definition — same numeric `q` value produces different effective bandwidth. Open: numeric verification needed. |
| equalizer#0 | per-band `mode` | `"RLC (BT)"` | AUDIBLE | Filter family. LSP also offers LRX, BWC, APO — different group-delay / steepness profiles. Has been verified for HP-slope behavior (commit `944a8f3`); other types not formally tested against XML target. |
| equalizer#0 | `split-channels` | `true` | AUDIBLE | Required: the Dolby PEQ is asymmetric L/R on most devices. Linking would force-symmetrise. |
| autogain#0 | `bypass` | `true` (HDA), `false` (SDW) | AUDIBLE | Documented in "Why autogain is bypassed by default" — re-enabling reintroduces pumping on quiet→loud transitions. |
| multiband_compressor#0 | `compressor-mode` | `"Modern"` | AUDIBLE | LSP's two compressor algorithms differ in knee shape and ratio behavior. Not measured against the XML's compressor model. Open: candidate test. |
| multiband_compressor#0 | `envelope-boost` | `"None"` | AUDIBLE | A pre-detection EQ tilt. Options include `Pink BT/MT`, `Brown BT/MT`. Primitive analog to Dolby's MI steering — could shape compressor response on content where it currently engages flat. Open: candidate test. |
| multiband_compressor#0 | `stereo-split` | `false` | TOPOLOGY | Single sidechain across L+R. Dolby's compressor is parameterised globally (one threshold per band, both channels), so unified sidechain matches. |
| multiband_compressor#0 | per-band `sidechain-mode` | `"RMS"` | AUDIBLE | RMS detection — gives smoother level estimation than peak. Reasonable for a music compressor. Not directly tested against Dolby's. |
| multiband_compressor#0 | per-band `sidechain-source` | `"Middle"` | AUDIBLE | Sidechain on `M` of M/S. Could be `"Stereo"` (full stereo image) or per-channel — the choice affects how loud-on-one-side content compresses both sides. Open: not tested. |
| multiband_compressor#0 | per-band `sidechain-reactivity` | `10.0` ms | AUDIBLE | Pre-attack envelope smoothing. LSP default. |
| multiband_compressor#0 | per-band `compression-mode` | `"Downward"` | AUDIBLE | Trap fix (commit `e454711`). LSP default enables upward compression below `boost-threshold=-72 dB`, which amplifies noise floor during silence. |
| multiband_compressor#1 (regulator) | `sidechain-mode` (limiting band) | `"Peak"` | AUDIBLE | Peak detection on the band that does brickwall limiting; RMS on others. Matches the hard-limit role of that band. |
| limiter#0 | `mode` | `"Herm Thin"` | AUDIBLE | One of LSP's many limiter algorithms. Hermes Thin is a thin-saturation curve. Modern / Classic / Herm Wide variants differ in distortion character. Open: candidate test. |
| limiter#0 | `oversampling` | `"None"` | AUDIBLE | No oversampling. Hard limiting on HF content can alias into-band; 2x or 4x suppresses it. Adds latency. Open: candidate test. |
| limiter#0 | `dithering` | `"None"` | SAFE | Off — adding dither here raises the noise floor unconditionally. |
| limiter#0 | `lookahead` | `1.0` ms | TOPOLOGY | Below LSP default (5 ms) but non-zero. Allows correct peak detection without the full-default delay. |
| limiter#0 | `alr` | `false` | AUDIBLE | LSP "auto level release" — dynamic relaxation of release time on the limiter. Off keeps behavior predictable. |

Items flagged **open: candidate test** are the active audit surface. Where
those experiments produce measurement-backed conclusions, the relevant row
will be updated to cite the residual numbers and the decision (kept,
changed, or documented trade-off).

### Measurement outcome: dynamics plugins are dormant on the test stimuli

A reduced A/B sweep (current vs `limiter mode = "Herm Wide"` +
`oversampling = "Full x4/24 bit"`, captured against `Dolby-Dynamic-Balanced`
on multitone / pink / sweep / sweep_quiet / pink_quiet) measured the
EE-vs-XML-target residual at:

| variant | EE-vs-XML rms | EE-vs-XML max |
|---|---:|---:|
| current (Herm Thin / None) | 0.94 dB | 4.40 dB |
| Herm Wide / Full x4/24 bit | 0.93 dB | 4.39 dB |

The two variants agree to **0.01 dB RMS** — within measurement noise.
This is the predicted outcome from a level-budget analysis: every
captured stimulus peaks at ≈ −10 dBFS, well below the limiter's
~ −1 dBFS threshold and below most MBC band thresholds in the test
XML. **The dynamics plugins (limiter, MBC, regulator) are passive at
our nominal stimulus levels**, so the parameters in the audit table
flagged as "AUDIBLE" but living in those plugins (`limiter mode/
oversampling`, `MBC compressor-mode`, `MBC envelope-boost`, `MBC
sidechain-source/mode/reactivity`) cannot be characterised by the
current pink-noise / multitone test rig. They affect transient and
loud-content behavior; characterising those needs a different test
stimulus (e.g. clipping-engaging sustained tones, or live program
material with peak detection) — out of scope for this audit, which
focuses on frequency-domain fidelity at nominal levels.

The measurement also reads off the **EE-vs-XML baseline**: at
0.94 dB RMS / 4.4 dB max in-band residual on `Dolby-Dynamic-Balanced`,
the FIR + biquad chain reproduces the curve our converter intended
to produce — i.e. the DSP math executes correctly. (`vsXML` is
internal consistency, not interpretation correctness; see the
"note on metrics" in Finding 7 for why this distinction matters.)
The 11.84 dB EE-vs-DAX residual recorded in Finding 6 is therefore
not implementation drift; per Findings 6 / 7 it's dominated by
fixed DAX-internal behavior outside the published XML.

For the rows still marked "open" in the table above (MBC and limiter
character knobs), the practical guidance is: defaults are safe at
nominal levels; revisit if a future investigation focuses on
transient or peak-engaging content.

## Why autogain is bypassed by default

The EasyEffects autogain is configured from Dolby's `volume-leveler` parameters
(target, history window, reference) but shipped with `bypass: true` by default
(commit `19a1f99`). Three reasons:

1. **Dolby's volume leveler is MI-steered.** The XML enables
   `mi-dv-leveler-steering-enable` only on the `dynamic` profile
   (`docs/cross-device-findings.md` §11), meaning Dolby analyses content to hold
   gain during silence rather than continuously pumping it up.
2. **EasyEffects autogain has no content awareness.** It treats silence as "too
   quiet" and cranks gain up over its integration window (10–30 s). When loud
   content arrives after silence, the first 400 ms–3 s of EBU R 128 integration
   are still running with the "quiet-period" gain → audible saturation / pumping.
3. **Bypassing is better than guessing.** Commits `67ac464` (−23 LUFS target) and
   `ec78b0d` (longer history window) softened the effect, but neither fixes the
   root cause. Shipping bypassed keeps the settings available for users who want
   to enable it manually without re-running the script.

## Verified math (sanity checks)

A few numerical things the script depends on that aren't documented in the README:

**Q15 block-rate time constants** (MB compressor attack/release coefficients). Stored
as exponential smoothing coefficients operating per block (256 samples at 48 kHz =
187.5 blocks/sec). Decoded via:

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

**FIR accuracy**. The minimum-phase cepstral method used to generate the IEQ +
audio-optimizer impulse response produces **exact** frequency response at all 20
Dolby band centers (error < 0.001 dB). The FIR is properly minimum-phase (100% of
the energy is in the first half of the 4096 taps), has no significant tail ringing,
and extrapolates flat beyond the band edges. 4096 taps (~85 ms at 48 kHz) is
sufficient for 20-band EQ correction.

**FIR time-domain envelope** (Dolby-Balanced, Dynamic, X1 Yoga Gen 7, channel L,
peak-normalized; reproduce with `tools/measure_ee/compare_ir_time_domain.py`):

|                              | converter FIR (`Dolby-Balanced.irs`) | EE-captured | DAX-captured |
|------------------------------|---:|---:|---:|
| total samples (file)         | 4096 (85.3 ms) | 8192 (170.7 ms) | 8192 (170.7 ms) |
| 95% cumulative energy        | peak + 1.15 ms | peak + 2.79 ms | peak + 1.29 ms |
| 99% cumulative energy        | peak + 5.50 ms | peak + 7.21 ms | peak + 3.62 ms |
| 99.9% cumulative energy      | peak + 11.19 ms | peak + 13.77 ms | peak + 8.21 ms |
| envelope first &lt; −60 dB   | peak + 19.94 ms | peak + 23.29 ms | peak + 11.40 ms |
| envelope first &lt; −80 dB   | peak + 49.88 ms | peak + 51.19 ms | peak + 23.15 ms |

The converter FIR and the EE-captured IR have nearly identical decay profiles —
expected, since EE *is* the convolver applying that FIR. The DAX-captured IR
decays roughly 2× faster (−60 dB at 11 ms post-peak vs ~22 ms). What looks
like a "long" loopback IR in a stereogram view is the −60 to −100 dB tail; on
a log-envelope scale the post-peak tail of all three IRs falls below the
audible threshold within ~25–50 ms.

The 99% cumulative-energy time (peak + 5.5 ms for the converter FIR) is what
matters for "where is the impulse-response actually doing work." The remaining
~80 ms of the 4096-tap file is the natural decay of the lowest-frequency
biquads in the cepstral construction (a 100 Hz HP at Q ≈ 0.7 has a several-ms
time-constant; the trailing &lt;−60 dB samples encode its asymptotic decay).
Trimming earlier than that loses LF accuracy, not visible "blank space."

## Empirical comparison vs DAX3 on Windows

Issue #11 raised an interesting side question: how does the FIR our converter
generates from the XML compare to what Dolby's DAX3 implementation actually
does on Windows? The XML is magnitude-only — there's no phase reference for
the IEQ + audio-optimizer combined response — so the question can only be
answered empirically.

The measurement tooling lives at `tools/measure_dax/`. It plays a stimulus
through the speaker output, captures the post-DAX3 signal via WASAPI loopback,
and analyses the result. A Linux-side counterpart at `tools/measure_ee/`
runs the same stimulus battery through a live EasyEffects instance with our
generated preset and produces analyzer-compatible captures, so the EE-on-Linux
and DAX-on-Windows responses can be overlaid for the same XML and profile.
Five stimulus kinds:

- **sweep** (exponential 20 Hz–22 kHz, −18 dBFS peak): Farina deconvolution
  recovers an LTI IR if the system is LTI.
- **sweep_quiet** (−42 dBFS peak): same sweep at much lower input level.
- **pink / pink_quiet**: stationary pink noise; steady-state magnitude after
  the leveler settles.
- **multitone**: 20 pure tones at the Dolby band centers; per-band amplitude
  *and phase* via single-bin DFT.

Captured on a ThinkPad X1 Yoga Gen 7 (Realtek ALC287, subsystem 17AA:22E6 —
matches the development XML at `localresearch/DEV_0287_SUBSYS_17AA22E6_*`).

### Finding 1: DAX3 is non-LTI for our stimuli

The volume leveler / regulator engage during capture and apply time-varying,
content-adaptive gain. Symptoms:

- 100 ms RMS envelope of the swept-sine capture varies by 16–34 dB from
  start to end of the sweep, depending on profile (vs flat ±0 dB on the
  OFF baseline). The leveler boosts late-sweep portions where the input
  fade-out drops the level.
- Multitone clipped on 4 of 6 profiles (dynamic, movie, music, game) —
  peak hit 0 dBFS with up to 113 clipped samples. The regulator engaged
  as a hard limiter even at −18 dBFS RMS input.
- For sweep at −18 dBFS, captured peaks reached ~−0.5 dBFS on the
  aggressive profiles (dynamic / movie / music / game). At −42 dBFS the
  leveler is *more* aggressive, not less (it's targeting a fixed loudness
  and brings quiet content up).

This means the recovered "IR" is not a true linear impulse response —
Farina deconvolution conflates frequency response with the time-varying
gain applied during the sweep. A clean LTI characterization of DAX3 is not
possible without disabling the leveler / regulator (which Dolby Access
doesn't expose), or sending continuously-stationary stimuli that give the
leveler a fixed level to settle on (which is what the pink stimuli do).

### Finding 2: DAX3's phase is hybrid, not pure min-phase or linear-phase

Sweep captures, post-peak vs pre-peak energy ratio (channel L). Pure
minimum-phase would be +∞ dB; linear-phase would be ~0 dB.

| profile | sweep (−18 dBFS) | sweep_quiet (−42 dBFS) |
|---------|-----------------:|-----------------------:|
| OFF     |  +0.0 dB (linear) |  +0.0 dB (linear) — bandlimited Dirac, expected |
| dynamic | +14.6 dB | +18.7 dB |
| movie   | +10.4 dB | +18.1 dB |
| music   | +15.7 dB | +19.4 dB |
| game    | +10.0 dB | +17.8 dB |
| voice   |  +8.6 dB |  +8.8 dB |

Every DAX3-on profile sits between linear-phase and minimum-phase. Voice
is closest to linear-phase (+8.6 dB) — likely a deliberate choice for
speech, where flat group delay preserves consonant transients. The
sweep_quiet variant looks more min-phase-like across profiles, but this
is most plausibly an artifact of the leveler's asymmetric response to a
quiet sweep, not a real phase shift.

This rules out our generated FIR matching DAX3's exact phase behaviour
in any profile. Per the no-added-latency constraint we don't switch our
converter to linear-phase regardless of this finding — minimum-phase is
the right trade-off for an EQ correction filter, and we accept that this
diverges from Dolby's choice.

### Finding 3: DAX3 doesn't faithfully implement the published XML curves

Each profile's captured spectrum vs **its own** balanced FIR target,
between-band magnitude residual on a 200-point log grid (47–19688 Hz):

| profile | sweep | sweep_quiet | pink | pink_quiet |
|---------|-------|-------------|------|-----------:|
| dynamic | 9.2 / 31.4 dB | 6.9 / 24.2 | 7.2 / 27.1 | 7.5 / 25.9 |
| movie   | 11.9 / 37.2 | 7.3 / 25.4 | 7.5 / 28.1 | 7.6 / 26.3 |
| music   | **7.3 / 21.6** | **5.1 / 19.6** | 5.9 / 20.4 | 6.5 / 20.4 |
| game    | 11.8 / 37.2 | 7.3 / 24.6 | 7.5 / 28.1 | 7.8 / 26.7 |
| voice   | 9.8 / 30.8 | 9.6 / 30.2 | 9.6 / 31.8 | 9.9 / 33.1 |

(`RMS / max` in dB; captured spectrum minus our FIR's frequency response.)

For comparison, the synthetic LTI test (apply our FIR to the stimulus,
deconvolve, compare to original) recovers within **0.06 dB RMS / 0.36 dB
max** — three orders of magnitude tighter. The captured DAX3 response is
genuinely far from what our FIR predicts, not a measurement artifact.

The bulk of the residual sits at HF (>5 kHz). At 19688 Hz the captured
magnitude is typically 20–40 dB above what the XML's combined IEQ + AO
target predicts. **DAX3 does not apply the deep HF rolloff that the
published XML implies.** This is the most actionable finding — it
suggests either (a) DAX3 ships a separate HF-shaping stage we're not
modelling, (b) the audio_optimizer block is a target-response curve that
DAX3 inverts internally rather than applying directly, or (c) the
specific IEQ "Balanced" curve in Dolby Access doesn't correspond to the
`ieq_balanced` block in the XML. Finding 4 (below) rules (c) out via a
Linux-side EE-loopback capture of the same XML; disambiguating (a) vs
(b) still needs either a Dolby-side reference or a stripped-down
single-block tuning XML.

The Music profile fits its XML target most closely (RMS 5–7 dB).
Dynamic, Movie, Game cluster around 7–12 dB RMS. Voice deviates the
most (9–10 dB RMS).

### Finding 4: EE-on-Linux follows the XML; the gap is on DAX's side

With the new `tools/measure_ee/` Linux-side capture (same 5 stimuli,
same `analyze.py`, same XML reference), we can place EE and DAX side by
side for the same profile. Pink-noise steady-state, Dynamic / Balanced,
ThinkPad X1 Yoga Gen 7 (DEV_0287_SUBSYS_17AA22E6), normalized at 1 kHz:

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

(Reproduce with `tools/measure_ee/compare_ee_vs_dax.py` after running
the EE battery and the DAX battery through `analyze.py` — see
`tools/measure_ee/README.md`.)

EE follows the converter's XML interpretation within ≤3 dB across most
of the band — same shape, same band centers, same depths. DAX
diverges most where the XML target is most extreme (deep HF rolloff
in `ieq_balanced + audio_optimizer`): at 19.7 kHz the XML target
predicts roughly −43 dB, EE applies −27 dB (the FIR doesn't reach the
target's depth), and DAX applies +1 dB.

This rules out hypothesis (c) from Finding 3 (the wrong `ieq_*` curve)
— our converter and EE agree on which curve is in play, and they
agree on its magnitude shape. The remaining hypotheses are (a) DAX
ships a separate HF-shaping stage we're not modeling, or (b) DAX
treats `audio_optimizer` as a target-response that it inverts before
applying. Loopback can't distinguish them without a controlled
single-block A/B (e.g., a tuning XML stripped down to a single block
at a time), but the gap is unambiguously on DAX's side, not the
converter's.

The 47 Hz deviation (−8 dB EE vs −28 dB DAX, both relative to 1 kHz)
is partly the EE chain's `equalizer#0 band0` HP at 100 Hz / x2 slope
(≈4th-order rolloff that takes us deeper than the XML target alone)
and partly DAX's volume regulator boosting LF tones at low input
levels — the multitone capture, where the leveler can lock onto a
single 47 Hz sine for 12 s, shows DAX at −14 dB (vs EE −37 dB), a
23 dB gap that's much bigger than the pink-noise gap and consistent
with leveler boost rather than steady-state EQ.

### Finding 5: No HF-shaping XML block was missed

A schema audit of the corpus XMLs (every element appearing under
`tuning-cp` and `tuning-vlldp` across `localresearch/` devices,
checked against what `parse_xml` reads) found **no candidate
HF-shaping element that the converter ignores**. The
elements `parse_xml` skips are bass-side
(`bass-enhancer-*`, `bass-extraction-*`, `virtual-bass-*`),
spatial (virtualizer angles, surround-decoder-center-spreading,
height-filter-mode), woofer-specific
(`woofer-regulator-*`, `calibration-boost`,
`customer-woofer-channel-index`), volume-modeling
(`volume-modeler-*`), or graphic-EQ (`graphic-equalizer-*`,
which is `enable=0` everywhere in the corpus). None of these
match an HF / treble / shelf / post-AO role.

This narrows hypothesis (a) — "DAX ships an HF-shaping stage we're
not modeling" — to one of two possibilities: either a DAX-internal
processing stage that does **not** appear in the published tuning
XML at all (e.g. a fixed driver-level treble curve baked into DAX3,
not parameterised per device), or an XML element whose semantics
we have mis-categorised (e.g. `bass-extraction` actually carrying
HF data, which is implausible from element naming but not strictly
ruled out by the corpus). Either way, hypothesis (a) cannot be
falsified without data outside the XML; the deterministic
"XML-only filter chain" property cannot close it.

### Finding 6: Hypothesis (b) is rejected; hypothesis (a) lives outside the XML

A 2×2 deterministic variant matrix was run across all 5 profiles to
disambiguate Finding 4's remaining hypotheses (a) vs (b). The variants
were produced from the same XML by patching `make_fir` to accept a
`phase` choice (minimum-phase via cepstral construction, or
linear-phase via zero-phase IFFT centered at `n/2`) and patching the
`combined = ieq_db ± ao_db` step in the converter to flip the AO
sign. Both patches were temporary scaffolding for this experiment —
the final result is decisive enough that they were removed once the
matrix was captured. Pink-noise steady-state RMS residual EE−DAX
(dB), 200–18000 Hz, normalized at 1 kHz, channel L:

| profile | add+min (default) | sub+min | add+lin | sub+lin |
|---------|-----------------:|--------:|--------:|--------:|
| dynamic | **11.95** | 19.53 | 12.15 | 19.94 |
| movie   | **12.53** | 20.26 | 12.84 | 20.66 |
| music   | **8.87**  | 16.56 |  9.13 | 16.97 |
| game    | **12.16** | 20.23 | 12.48 | 20.65 |
| voice   | **11.45** | 31.01 | 11.75 | 31.42 |

(Reproducing requires re-applying the temporary patches to
`make_fir` and the `ieq_db + ao_db` step, then driving
`tools/measure_ee/capture_battery.py` once per (variant, profile)
and comparing against the DAX captures with
`tools/measure_ee/compare_ee_vs_dax.py`.)

`add+min` (the current default) wins on every profile; `sub+min` is
+7–20 dB worse. Hypothesis (b) — "DAX inverts AO before applying" —
is decisively **rejected**: subtracting AO moves EE *away* from DAX,
not toward it. Voice profile is the most extreme (+19.6 dB with sub),
because voice has the largest AO swings; this also confirms the AO
contribution is being applied with the right sign at the right
magnitude.

`add+lin` is consistently +0.2–0.4 dB worse than `add+min`. Phase
character has minor influence on the magnitude residual (as expected
— pink noise is a steady-state magnitude measurement; the small
delta is leveler interaction with the changed temporal envelope).
Linear-phase doesn't help magnitude match; it costs ~42 ms group
delay; it's diagnostic only.

Per-band residuals on `add+min` reveal the gap structure:

| band | dynamic | movie | music | game | voice |
|---:|---:|---:|---:|---:|---:|
|    47 Hz |  −8.1 |  −7.0 | −14.4 |  −6.5 |  +0.5 |
|   234 Hz |  +3.1 |  +3.0 |  +1.1 |  +3.5 |  +3.0 |
|  2.25 kHz |  +4.2 |  +3.9 |  +4.2 |  +2.7 |  +4.9 |
|  5.81 kHz |  −3.4 |  −3.7 |  −0.4 |  −3.7 |  −3.4 |
| 11.25 kHz |  −9.5 | −10.2 |  −5.3 |  −9.7 |  −8.5 |
| 13.88 kHz | −16.5 | −17.2 | −11.7 | −16.7 | −15.6 |
| 19.69 kHz | −28.2 | −29.2 | −23.5 | −28.7 | −27.6 |

(Music's smaller HF gap reflects its less-aggressive HF rolloff in
`ieq_music_balanced`; the rest cluster within ~3 dB at every HF band.)

The HF gap above ~10 kHz is **profile-independent** — same shape,
similar magnitudes regardless of which `IEQ + AO` target is in play.
This is the canonical signature of a **fixed** HF behavior on
DAX's side that is not parameterised in the published tuning XML.
Combined with Finding 5 (no candidate HF-shaping XML element was
missed), the only remaining explanation for the HF residual is a
DAX-internal processing stage outside the XML — likely a built-in
treble-region behavior calibrated per-platform but not exposed
through the tuning files we consume.

The mid-frequency biases (+3 dB at 234 Hz, +4 dB at 2.25 kHz,
roughly profile-independent) point at a similar story: EE applies
the XML-implied curve, DAX softens specific bands, and the shape
of the softening is fixed rather than per-profile. These are the
shape of "voicing" choices baked into DAX, not absorbed into the
per-device tuning.

**Outcome for the converter:** the current `IEQ + AO` minimum-phase
FIR is the right deterministic target for the published XML. No
default change, no permanent flag added — the experiments closed
their hypotheses, not opened a new tuning surface. Closing the
remaining residual further requires data outside the XML (e.g.
follow-up #1 below — a stripped-down single-block tuning XML A/B
on Windows).

### Implications for the converter

Our `make_fir` produces a faithful min-phase FIR of `IEQ + audio_optimizer`
within ≤0.001 dB of the band-center target — the math is correct. What
we cannot reproduce on Linux without additional reverse-engineering:

1. **DAX3's hybrid-phase character.** Out of scope: linear-phase costs
   ~42 ms of group delay, ruled out by the no-added-latency constraint.
2. **DAX3's apparent flatter HF response.** Finding 6 rejects
   hypothesis (b): the variant matrix shows `IEQ − AO` is +7–20 dB
   *worse* than `IEQ + AO` on every profile. Finding 5 makes
   hypothesis (a) implausible within the XML (no missed element).
   What remains is a fixed DAX-internal processing stage outside
   the published tuning XML. Closing it requires data we don't
   have (e.g. a stripped-down single-block A/B on Windows).
3. **DAX3's non-LTI dynamics** (leveler, regulator engaging during
   playback). EasyEffects' autogain is bypassed by default already
   (see "Why autogain is bypassed by default" above) — adding a
   content-adaptive leveler equivalent would require approximating
   Media Intelligence steering, which is a substantial undertaking.

The captures + analysis tooling under `tools/measure_dax/` are kept for
future debugging — re-running on a new device or after a Dolby driver
update is a one-command repeat.

### Finding 7: Five XML-interpretation hypotheses tested; none closes the gap

After Findings 5/6 closed hypothesis (b) and the missed-block theory,
five further hypotheses were brainstormed to explain the residual:

  - **α** — DAX soft-clamps the IEQ+AO target depth.
  - **β** — `ieq-amount` is a +/- dB cap, not the linear scale we apply.
  - **γ** — DAX applies IEQ only inside a frequency window.
  - **δ** — DAX's regulator boosts quiet sustained low tones.
  - **ε** — Our 100 Hz × 4th-order HP cuts deeper than DAX at 47 Hz.

α/β/γ/ε were tested as a single-profile (dynamic / balanced) variant
sweep against the DEV_0287 ThinkPad X1 Yoga Gen 7 XML, with four
temporary CLI flags on the converter: `--clamp-target-db N`,
`--ieq-amount-as-cap`, `--ieq-window LO:HI`, `--disable-speaker-hp`.
δ does not get a variant — it's already the standing follow-up #3.
The flags were reverted in the same commit that landed this finding.

Per-band EE − DAX (dB), pink-noise steady-state, normalized at 1 kHz,
positive = EE louder than DAX:

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

(Reproduce: `localresearch/measure_ee/spec_freqgap.tsv` + a converter
patched to re-introduce the four flags. The TSV uses unique per-variant
preset prefixes (`DolbyFG1…DolbyFG8`) to defeat EasyEffects' convolver
IRS-cache by kernel name — without unique kernel names, EE silently
reuses the previous variant's cached IR even after the .irs file is
overwritten on disk.)

**A note on metrics.** Two residuals are reported in the
`summarise_variants.py` output: `vsDAX` (against the captured
DAX response) and `vsXML` (against the analytical target our
converter built from its own XML interpretation). They answer
different questions and `vsXML` is the weaker signal:

  - `vsDAX` is the only **external** check. DAX captures are
    imperfect (they're for one device, one driver, and DAX itself
    is non-LTI per Finding 1) but they're the only data point not
    derived from our own assumptions. If a candidate rule moves
    EE materially closer to DAX *without giving up ground in other
    bands*, that's evidence our current rule is wrong — even if
    the new rule lowers `vsXML`, since `vsXML` is computed
    against our own (possibly wrong) interpretation.
  - `vsXML` is **internal consistency**. It tells us whether
    the FIR + biquad chain reproduced the curve our converter
    intended (i.e. did the math execute?). It does not validate
    the *interpretation* itself, since both the chain and the
    reference are derived from the same `parse_xml`/`make_fir`
    code path. If we got a field's semantics wrong, `vsXML` can
    happily report 0 dB while the chain is still wrong.

  The verdict for each hypothesis below is therefore framed
  around `vsDAX` per-band trade-offs. `vsXML` deltas are
  reported as a sanity check that the patched converter did
  what we asked, not as the deciding criterion.

**α (clamp).** vsDAX trade by clamp depth: each step closes some
HF residual but immediately opens an equivalent (or larger) mid-band
residual. ±20 dB shifts only 234 Hz (+3.1 → −0.5); ±15 closes 4.7
dB at 19.7 kHz but adds 5.5 dB error at 234 Hz; ±6 closes 14 dB at
19.7 kHz but every mid band is now 10–14 dB off. No symmetric N
gets closer in *every* band — there's always a band where we were
nearer DAX before and aren't now. Aggregate `vsDAX rms` does drop
(11.84 → 9.59 at ±6) but the per-band trade is the more honest
view: the rule isn't shifting the whole curve toward DAX, it's
swapping which bands diverge.

**β (ieq-amount-as-cap).** Cleanest per-band trade in the set:
19.7 kHz residual drops from −28 to −18 dB (+9.7 dB closer to
DAX), 13.9 kHz and 11.25 kHz each gain ~2 dB, 5.8 kHz gains 2 dB,
47 Hz gains 2 dB; 234 Hz and 2.25 kHz both move ~1.6 dB closer to
DAX. Every band moves *toward* DAX, no band moves materially
away. `vsDAX rms` 11.84 → 9.09; `vsDAX max` 25.15 → 17.52. This
is the most plausible candidate of the five — it's the only one
where the per-band view shows no clear regression. But the
remaining gap is still 18 dB at 19.7 kHz, so β alone doesn't
explain the residual; at most it's part of the story. (`vsXML`
worsens 0.94 → 3.06, but that's expected — we're applying a
different interpretation than the converter's reference path
uses, so they should disagree.)

**γ (ieq-window 100 Hz – 10 kHz).** Per-band: largest single
HF improvement (19.7 kHz residual drops to −10.5, 13.9 kHz to
−10.6) but 47 Hz residual blows out from −8 to −18 dB EE−DAX
and 11.25 kHz worsens by ~1.4. Mid-band unchanged. So γ buys
~17 dB at 19.7 kHz and ~6 dB at 13.9 kHz at the cost of ~10 dB
at 47 Hz — a band-for-band trade, not a strict improvement.
Aggregate `vsDAX rms` 11.84 → 7.57 (lowest of all variants),
driven entirely by the HF win; the in-band `vsDAX max` summary
metric (which excludes 47 Hz) drops to 10.88 dB but the actual
worst-band error has just relocated to 47 Hz at 18 dB.

**δ (leveler).** Confirmed unchanged. The pink-noise gap at 47 Hz
is −8 dB EE−DAX while the multitone-on-47 Hz gap is −23 dB EE−DAX
(Finding 4); the factor-of-3 gap ratio between stimuli is the
canonical signature of a content-adaptive leveler boosting
sustained low tones. Closing this requires modeling DAX's
MI-steered leveling, unchanged from follow-up #3.

**ε (no speaker HP).** Decisive analytical match: a Butterworth-style
4th-order HP at f0 = 100 Hz attenuates 47 Hz by ~26 dB; the captured
`no-HP` variant lifts EE at 47 Hz from −36.5 to −11.6 dB, a +24.9 dB
shift. ε is the dominant LF mechanism on EE's side. But removing
the HP overshoots DAX (EE at 47 Hz is now +16.9 dB vs DAX, vs
−8.1 dB with HP), so the HP itself is the right *topology* — DAX
must apply some LF shaping, just softer than ours. Two consistent
stories: (i) DAX applies an HP at the same f0 with a shallower
slope (~12 dB/oct instead of 24), or (ii) DAX's leveler boost (δ)
compensates for an otherwise-similar HP, and the pink-noise
EE−DAX gap is leveler, not filter, dominated. The variant sweep
cannot disambiguate.

**Outcome.** None of α/β/γ is a strict per-band improvement
against DAX. β is the closest thing — every band moves toward
DAX, none materially away — but the 19.7 kHz residual is still
18 dB after applying it, which means even if β is part of the
right interpretation it doesn't explain the bulk of the gap. α
and γ are pareto trades: they swap one band's error for
another. The pattern across α/β/γ — partial movement, no
sweep that lands every band closer — is the same pattern
Finding 6 saw with the AO-sign and phase variants, and it's
consistent with Finding 6's conclusion: the residual is
dominated by DAX-internal behavior outside the published XML
(a fixed HF voicing + the leveler), not a single wrong XML
rule on our side.

**Caveat.** This conclusion is conditional on the experimental
data we have. We have DAX captures for one device / one driver
revision; β's ~9 dB HF improvement *might* generalize, in which
case our current `ieq-amount → linear scale` rule is wrong and
the cap reading is right. The reason we're not adopting β is
not that it's worse — it isn't, on `vsDAX` — but that the
remaining 18 dB residual at 19.7 kHz means even the best
candidate doesn't close the gap, so swapping rules trades one
incomplete model for another. If a second device's DAX captures
become available and β is a strict improvement on that device
too, that's the threshold for revisiting the default. The four
temporary flags were removed; if the experiment ever needs to
be re-run, re-add them per the patch in the git history of this
finding.

Per-variant captures retained under
`localresearch/measure_ee/variants/{baseline, clamp_pm{20,15,10,6},
ieq_cap10, ieq_window_100_10k, no_hp}/`.

**β follow-up: cross-profile validation.** β was the most
plausible candidate from the single-profile sweep, so it was
re-tested on the other four profiles (movie / music / game /
voice) to see if the per-band improvement signature is
consistent. Same XML, same DAX captures, same harness, fresh
EE captures with `--ieq-amount-as-cap`. The test XML uses
`ieq-amount=10` on every profile, so the cap value itself is
not the variable across profiles; the variable is the IEQ +
AO curve content, which differs per profile (different
audio-optimizer per-band gains, same `ieq_balanced` curve
shape).

|β| − |baseline| (positive = β is closer to DAX), in dB:

| profile |  47 Hz | 141 Hz | 234 Hz | 469 Hz | 2.25k | 5.8k | 11.25k | 13.9k | 19.7k | total |
|---------|------:|------:|------:|------:|------:|-----:|-------:|------:|------:|------:|
| dynamic | +1.93 | +1.76 | +1.58 | +0.51 | +1.64 | +2.22 | +2.17 | +2.30 | +9.78 | +23.9 |
| movie   | +1.98 | +1.53 | +1.62 | +0.69 | +1.63 | +2.00 | +2.00 | +2.00 | +9.69 | +23.1 |
| music   | +1.89 | +1.53 | +0.53 | +0.69 | +1.63 | −1.13 | +2.00 | +2.00 | +9.69 | +18.8 |
| game    | +1.89 | +1.53 | +1.62 | +0.69 | +1.63 | +2.00 | +2.00 | +2.00 | +9.69 | +23.0 |
| voice   | −1.99 | +1.53 | +1.62 | +0.69 | +1.63 | +2.00 | +2.00 | +2.00 | +9.69 | +19.2 |

(Reproduce: `localresearch/measure_ee/spec_beta_profiles.tsv` +
the temporary `--ieq-amount-as-cap` flag. Plot:
`localresearch/_beta_cross_profile.png` — pink-noise overlay
of DAX vs baseline vs β across all five profiles.)

The signature is striking: β shifts EE by **almost identically
the same dB amount per band on every profile** (the +9.69–9.78
column at 19.7 kHz is within 0.1 dB across all five). That's
the expected behavior if β is hitting a *structural* feature of
the published IEQ curve — `ieq_balanced` is shared across
profiles, so capping it produces the same lift in every
profile's combined target. If the captured improvement were
noise or coincidence, we'd expect per-profile variability of
several dB; we see ≤0.1 dB.

Two regressions stand out: voice at 47 Hz (β is 2 dB *worse*)
and music at 5.8 kHz (β is 1.1 dB *worse*). In both cases the
baseline residual at that band was already near zero (voice 47
Hz: +0.46 dB EE−DAX; music 5.8 kHz: −0.43 dB), so β's lift
*overcorrects* through zero rather than degrades the chain. The
underlying lift is the same magnitude as on the other profiles
— it just happens to land on the wrong side of zero where the
profile was already close. That's a side-effect of β's
mechanism (it always lifts) rather than a profile-specific
failure of the rule.

In aggregate, β closes 18.8–23.9 dB of total |EE−DAX| residual
on every profile, ~80% of which is concentrated at 11.25–19.7
kHz. After β, every profile still shows an 18 dB residual at
19.7 kHz (also remarkably consistent: the post-β `vsDAX` at
19.7 kHz is −18.46 / −19.57 / −13.85 / −19.02 / −17.96 across
the five profiles, clustered ~−18 dB). So β is *part of* the
right reading but not all of it; a second mechanism (likely the
fixed HF voicing in DAX from Finding 6) accounts for the
remaining ~18 dB.

**Updated stance on β.** Cross-profile consistency materially
strengthens the case that β reflects how DAX actually
interprets `ieq-amount`. The single-profile result was already
the cleanest of the five hypotheses; the cross-profile result
shows the improvement is *structural*, not coincidental. We
still don't change the default because:

  - We have one device's data. The XML schema interpretation
    might be device-specific in a way β happens to fit on this
    device. (`ieq-amount` is always 10 in this XML; we have no
    direct evidence about the cap-vs-scale interpretation when
    the value differs.)
  - β cannot close the remaining 18 dB at 19.7 kHz, so even if
    correct it has to coexist with a second mechanism we
    haven't modeled. Switching defaults to a partially-correct
    rule is worse than leaving a known-incomplete rule in
    place.
  - β has small per-profile regressions (voice 47 Hz, music
    5.8 kHz) that, while explainable as overcorrection, would
    audibly tilt those profiles vs the current default.

**The bar for adopting β as the default:** a second-device
XML where `ieq-amount` differs from 10 *and* a captured DAX
response from that device, showing that the cap reading
predicts the per-band improvement at the new value. That
would distinguish "β is the right rule" from "β's +10 dB
HF lift happens to align with DAX's HF voicing on this
device."

### Finding 8: DAX runs psychoacoustic VBE; the schema can't drive a per-device mapping

The bass-burst stimulus that closed the regulator-stress investigation
(Finding 7 item 1) surfaced an unrelated finding on the DAX side. The
50 Hz capture region (loud, peak −5 dBFS) shows a textbook
missing-fundamental harmonic complex:

| freq | magnitude | role |
|---:|---:|---|
| 50 Hz | −36.3 dBFS | fundamental |
| 150 Hz | −37.6 dBFS | 3rd harmonic (1.3 dB below fundamental) |
| 250 Hz | −38.1 dBFS | 5th harmonic (1.8 dB below fundamental) |
| 200 / 300 Hz | −50 dBFS | 4th / 6th (suppressed even harmonics) |
| 350 Hz | −54.3 dBFS | 7th |

For comparison, the 180 Hz capture in the same battery has all
non-fundamental peaks ≥35 dB below the fundamental (clean sine, crest
factor 4.4 dB). The 50 Hz region's crest factor is 22.9 dB — DAX is
generating odd harmonics at near-fundamental amplitude on bass content
the speaker can't physically reproduce. This is the standard
psychoacoustic-bass mechanism (SRS TruBass / Waves MaxxBass / Dolby's
own Virtual Bass Enhancement), letting the auditory system reconstruct
a 50 Hz percept from the harmonic complex above the speaker's HP roll-off.

Our converter only emits Calf BassEnhancer when `is_soundwire=True`;
the X1 Yoga (and other HDA devices) get no harmonic-generation stage
at all. The capture used the bass-burst stimulus in
`tools/measure_dax/make_stimulus.py:make_bass_burst` (sustained sine
tones at 50 / 80 / 120 / 180 Hz, ±5 / −25 dBFS peak) — the same battery
that closed Finding 7 item 1.

This is a **non-LTI** processing-stage gap, distinct from the LTI EQ-curve
gap analyzed in Findings 4–7. It would be invisible to pink-noise
captures (the harmonic complex blends into the broadband spectrum) and
shows up only on tonal bass content.

**XML interpretation question.** The X1 Yoga XML has every VBE-adjacent
field populated:

```
<bass-enhancer-enable value="0"/>
<bass-enhancer-cutoff-frequency value="200"/>
<virtual-bass-mode value="0"/>
<virtual-bass-mix-freqs value="94,469"/>
<virtual-bass-subgains value="-32,-144,-192"/>
<virtual-bass-src-freqs value="35,160"/>
<virtual-bass-overall-gain value="0"/>
```

`enable=0` / `mode=0` read as "off" but every supporting field is
non-default. Two readings:

1. `mode=0` is honestly off. DAX has a baseline VBE that always runs
   and is not parameterized in the schema. The supporting fields are
   dead schema slots.
2. `mode=0` is a label (e.g. "auto/default") and the supporting
   parameters configure it.

A `*-mode` survey across the schema shows that some mode-attrs do vary
by device (`height-filter-mode` is `0`/`1` in the corpus), so `0` is
not universally "off" — but the precedent for a feature with a
separate `*-enable` *and* tuning fields is `dialog-enhancer-enable=0`
with `dialog-enhancer-amount=7`: tuning fields persist as dead values
when the gate is off.

**Corpus sweep settles the actionable question regardless of which
reading is correct.** Across 2,470 XMLs containing VBE fields:

| field | unique values across 2,470 XMLs |
|---|---|
| `virtual-bass-mode` | `0` only |
| `bass-enhancer-enable` | `0` only |
| `virtual-bass-mix-freqs` | `94,469` only — 1 unique value |
| `virtual-bass-src-freqs` | `35,160` only — 1 unique value |
| `virtual-bass-subgains` | `-32,-144,-192` only — 1 unique value |
| `virtual-bass-overall-gain` | `0` only |
| `virtual-bass-slope-gain` | `0` only |

Across hundreds of distinct speaker designs, the supporting fields are
**frozen identical**. If reading 2 were correct — that they configure
the synthesis — they would vary the way `mb_comp` thresholds, IEQ
curves, and regulator levels do. They don't. **The schema cannot drive
a per-device VBE mapping** in either reading.

**Companion-file audit.** Each DAX3 driver package ships a main tuning
XML, a `_settings.xml` (UI-only: AutoProfile mappings, Dolby Access GUI
flags), one or more `operator_settings_*.json` files (Dolby Access
vendor config: mirroring, auto-profile behavior), and `.inf`/`.cat`
driver-install metadata. Mic-side `_amic.xml` / `_dmic.xml` files are
already filtered by the corpus harness. **No text-readable per-device
file carries VBE tuning.** The DAX3 binaries themselves could carry an
internal lookup table keyed on PCI subsystem ID, but reading that
requires reverse-engineering the DLL and is out of scope.

**Actionable conclusion.** A faithful XML-derived per-device VBE mapping
is not on the table — there's no per-device VBE signal in any
text-readable source we ship against. Any VBE we add for HDA devices
is, by construction, a hardcoded baseline that does not trace back to
per-device XML, so it lives in opt-in territory under the project's
XML-only invariant. The existing `make_bass_enhancer(hp_freq)` factory
(introduced in commit `bc12c2e9` from the EasyEffects laptop-speaker
guide) is the natural baseline candidate; gating it on HDA devices is
an investigation question rather than a deterministic-mapping one. See
issue #14 for the open follow-up.

**Empirical follow-up: Calf BassEnhancer hits an architectural ceiling.**
A `--enable-vbe` investigation flag was added on top of the
SoundWire-only emission gate, with `make_bass_enhancer` extended to
accept `(floor, scope) = virtual-bass-src-freqs` from the XML (the
field lives under `tuning-cp`, not `tuning-vlldp`, and is corpus-frozen
at `35,160`). Bass-burst captures (X1 Yoga, dynamic/balanced, 50 / 80 /
120 / 180 Hz at peak −5 dBFS) compared the unflagged chain, the
flagged chain, and DAX:

| tone | EE off Δ3 | EE+VBE Δ3 (XML-derived) | DAX Δ3 |
|---:|---:|---:|---:|
| 50 Hz | −81 dB | **−8 dB** | **−1 dB** |
| 80 Hz | −102 | **−29** | **−28** ← matches DAX |
| 120 Hz | −102 | −42 | −59 |
| 180 Hz | −112 | **−18** ← regression | **−74 (clean)** |

Calf does generate psychoacoustic harmonics on 50/80 Hz at amplitudes
within ~5 dB of DAX (50 Hz benefit) but also produces a strong 3rd
harmonic at 540 Hz on the 180 Hz tone, where DAX is essentially clean
(180 Hz regression). A parameter sweep across `harmonics` (3 / 5 / 10),
`blend` (−10 / 0 / +10), and `amount` (6 / 12) confirmed the
regression is structural: every variant trades 50 Hz benefit linearly
for 180 Hz regression — there's no parameter point where Calf produces
DAX-like harmonics on the lowest tones *and* stays clean above the
declared `virtual-bass-src-freqs` upper bound.

The mechanism is Calf BassEnhancer's internal source-band filter: it
has a soft (≈12 dB/oct) rolloff at the `scope` parameter, so a 180 Hz
input above a `scope=160` cutoff is only ~4 dB attenuated and the
harmonic generator still receives a sizable signal. Tightening `scope`
below 100 Hz to fully suppress 180 Hz also kills the 80 Hz synthesis
that already matches DAX. Calf also leaks 2nd harmonics (e.g. a 100 Hz
peak when a 50 Hz tone is present, ≈25 dB below the fundamental) where
DAX's profile is much weaker on evens — DAX's harmonic generator
appears to use a near-symmetric nonlinearity that emphasizes odd
harmonics, while Calf's distortion model produces both.

The flag was reverted from the converter's CLI surface. The `make_bass_enhancer`
function retains its `src_freqs` parameter (XML-derived, corpus-frozen) so
that if a future architectural revisit ships VBE-on-HDA via a different
plugin / topology, the band-bounds plumbing is already in place.

**Calf Saturator (architectural alternative for PipeWire-conf path).** A
PoC offline test (`lv2apply` driving Calf Saturator on the bass-burst
stimulus at `mix=1.0`, `drive=4.0`, `hp_pre_freq=35`, `lp_pre_freq=160`,
`hp_post_freq=180`, `lp_post_freq=800`) showed Saturator's harmonic
profile is closer to DAX's than Calf BassEnhancer's, but the
architectural ceiling is *lowered, not escaped*. Wet-only output Δ
versus fundamental-leakage:

| tone | calf_v2 (BassEnhancer) Δ3 | sat_v1 (Saturator) Δ3 | DAX Δ3 |
|---:|---:|---:|---:|
| 50 Hz | −8 | +4.5 | −1.3 |
| 80 Hz | −29 | −3.4 | −28 |
| 180 Hz | **−18 (regression)** | **−31** | −74 (clean) |

Saturator at 180 Hz is ~13 dB cleaner than Calf BassEnhancer (−31 vs
−18 below fundamental) — meaningful but still ~40 dB above DAX's clean
profile. The reason is identical to BassEnhancer's: Calf Saturator's
internal `lp_pre_freq` is also a soft (~12 dB/oct) rolloff, so out-of-band
content above 160 Hz still passes through enough to generate harmonics.
Calf Saturator additionally leaks 2nd-harmonic ≈11 dB stronger than
DAX's profile at 50 Hz (3rd-vs-2nd ratio +11 dB for Saturator,
+26 dB for DAX) — Calf's distortion model is not purely symmetric.

Even harder drive (`drive=8`) and tighter post-band (`lp_post=600`)
amplify the harmonic complex but don't change the relative ratios.

**Cascading LSP filters + Saturator escapes the single-plugin ceiling.**
A follow-up PoC chained two LSP `filter_stereo` stages in front of
Calf Saturator (BWC mode at slope `x16` ≈192 dB/oct on input edges,
`x8` ≈96 dB/oct on output edges) for a true brick-wall band-pass at
[35, 160] Hz, with Calf Saturator's internal pre/post filters disabled
to avoid double-filtering. Final output Δ3 (3rd harmonic vs
fundamental) at 180 Hz: **−56 dB** — vs Calf BassEnhancer's −18 dB
ceiling, a 38 dB improvement, and within 17 dB of DAX's −74 dB clean
profile. The 50 Hz / 80 Hz odd-vs-even ratios are also strongly
odd-dominated (3rd-vs-2nd at +56 dB / +16 dB respectively), confirming
Calf Saturator's `drive` produces a near-symmetric saturation when fed
a clean band-passed input — the even-harmonic leakage seen in earlier
single-plugin tests was artefactual of out-of-band content reaching
the saturator. So both walls of the architectural ceiling can be
broken with a deeper signal chain; the cost is two extra LV2 stages
per channel (4 LSP filter instances total in stereo) plus disabled
internal Calf filtering.

**Caveat: ceiling-break is for harmonic structure only, not absolute
magnitude.** The PoC measured the wet-only output of the
LSP-cascade-plus-Saturator chain (`mix=1.0`, post-HP at 180 Hz kills the
band-passed fundamental). In a real deployment the wet path is summed
with a parallel dry chain that carries the fundamental — but our dry
chain attenuates 50 Hz to ~−66 dBFS, and the wet Saturator residual
sits at ~−54 dBFS, so the mixed 50 Hz fundamental lands ~9 dB below
DAX's captured −45 dBFS. The harmonic complex is now structurally
right (+56 dB odd-vs-even ratio at 50 Hz, 180 Hz harmonic regression
gone) but the fundamental remains low. This separates cleanly into
*two* gaps, not one:

1. **Bass-attenuation gap** (Findings 4, 7): our chain attenuates 50 Hz
   ~21 dB more than DAX does. Lives outside the XML — DAX's regulator /
   leveler appears to actively boost quiet sustained low tones, which
   is what hypothesis δ in Finding 7 was tracking. Closing this gap
   needs a level-dependent / content-adaptive boost upstream, not a
   harmonic synthesizer.
2. **Harmonic-synthesis gap** (this finding): DAX adds odd-dominated
   harmonics on bass content; our chain doesn't. The
   LSP-cascade-plus-Saturator chain closes this structurally.

Calf BassEnhancer in earlier tests *appeared* to address part of (1)
because its mix structure passes some boosted dry-band signal through —
but that conflated the two gaps and came at the 180 Hz regression
cost. The Saturator-based path keeps the gaps separated, which is
honest but means closing the harmonic gap doesn't close the magnitude
gap. The mixed-topology measurement (parallel dry + wet Saturator
chain summed at output) is the next concrete step before any
`ee_to_pipewire.py` change ships; the wet-only PoC numbers tell us
the harmonic ceiling can be broken but not what the integrated
chain's absolute magnitude match looks like.

**Calf MultibandEnhancer was tested in parallel and does not escape
the ceiling.** A 24-variant sweep over crossovers, per-band drive,
blend, and base parameters found that the best variant
(`split_taper_mid`: drive=10/4/1/0, blend=8/2/0/0, splits 65/100/150)
produces a DAX-shaped tapered Δ3 (weakening with frequency) but caps
at 50 Hz Δ3 ≈ −20 dB — the plugin's harmonic generator is a
memoryless wave-shaper with an intensity ceiling that no parameter
combination crosses. It is also even-dominated at the lowest tones
where DAX is strongly odd-dominated. MultibandEnhancer cannot
substitute for the BassEnhancer / Saturator path.

EE 8.x exposes no saturator plugin slot and no way to chain LV2
filters in series before its built-in plugin slots, so the
cascading-LSP-plus-Saturator approximation can only ship via the
PipeWire filter-chain path (`ee_to_pipewire.py` users). Splitting VBE
behaviour across the two output paths (EE-mode users get nothing;
PW-mode users get an approximation that genuinely tracks DAX's
selectivity within ~17 dB at 180 Hz) becomes a maintenance-cost
decision rather than a measurement one. Issue #14 is left open as the
canonical reference for the gap; no converter change ships from this
investigation. A follow-up that extends `ee_to_pipewire.py` to inject
the LSP-cascade-plus-Saturator chain when the XML carries
`virtual-bass-mode=0` and `is_soundwire=False` is the natural next
step if a listening test confirms the captured improvement is
audible.

### Finding 9: The IEQ is over-applied — `ieq-amount` reads as a percentage, and that closes the HF gap (issue #13)

Issue #13 (taprobane99) opened arguing our 20-band → FIR construction was
wrong and biquad-fitting was right. The biquad-vs-FIR question was settled
in the thread (the cepstral FIR sits within 0.34 dB of the target; biquad
fits leave 10–16 dB ripple). But reproducing his follow-up work surfaced
two substantive points — one that doesn't matter, and one that does.

**Construction details don't matter (rejected).** He proposed PCHIP
interpolation (vs our linear-in-dB over log-f), a large cepstral FFT
(65536 vs `FIR_LENGTH`), and explicit DC/Nyquist anchoring. An offline
pre-screen plus an on-device measured sweep (build the 4096-tap min-phase
FIR each way, capture through the live EE chain with
`tools/measure_ee/sweep_variants.sh`, score vs the X1 Yoga DAX pink
capture) put all three within <0.3 dB of the current construction; a
"construction-only" variant measured 12.30 dB EE−DAX RMS vs the baseline's
12.04 — i.e. no movement. Our cepstral FIR already realises the target
within ~0.06 dB and linear-in-dB interpolation does not Gibbs-ring; the
larger FFT only changes <80 Hz (masked by the 100 Hz HP) and, truncated
back to 4096 taps, slightly *worsens* band accuracy. The
`--fir-interp/--fir-fftsize/--fir-dc-anchor` flags added to test this are
temporary scaffolding, to be reverted.

Re-checked after the IEQ down-weight landed (in case the old full-weight
HF error had masked a construction benefit): on the new, flatter
`amount/100` target the current-vs-full-construction spread is *smaller*,
not larger — audible-band (>100 Hz) RMS 0.47 dB vs 0.60 dB on the old
curve. Expected: construction governs realisation fidelity (already
~0.06 dB to the band points), not the target, so it cannot close the
residual ~1 dB EE−DAX gap, which is a target/DAX-internal difference.
Topic closed regardless of baseline — do not reopen without new evidence.

**Mixed phase is rejected on latency (corroborates Finding 2).** His
notebook ships causality=0.4 (mixed phase); his published RePhase IRs
carry ~20 ms latency. Both have identical *magnitude* to minimum phase
(0.00 dB) but add 6–20 ms group delay and pre-ring. We keep pure
min-phase (causality=1.0, zero added latency) — the same hybrid-phase
character Finding 2 saw in DAX and ruled out on the no-latency constraint.

**The IEQ is applied at ~10× too much weight (the real finding).** He
replaced our full-weight `IEQ + AO` with `AO + 0.10·(IEQ − mean(IEQ))`,
noting the full IEQ "dominates the impulse." Measured on-device (X1 Yoga,
dynamic/balanced, pink, norm @1 kHz, in-band 200–18 kHz):

| variant | EE−DAX RMS | EE−DAX max |
|---|--:|--:|
| baseline `IEQ+AO` | 12.04 | 26.03 |
| `AO + 0.10·IEQ` | **1.03** | **3.94** |
| `AO + 0.25·IEQ` | 2.38 | 5.30 |

Down-weighting collapses the long-standing EE↔DAX treble gap: per-band
19.7 kHz goes −28.2 → −1.5 dB, 13.9 kHz −16.7 → −0.6, 11.25 kHz −9.7 →
+0.1, costing ≤1 dB in two already-good mid bands (328 Hz, 4.7 kHz). This
is the gap Findings 4/6/7 attributed to "fixed DAX-internal HF voicing
outside the XML" — but it is largely **our own interpretation error**: we
apply the full IEQ as a static EQ.

**Where the weight comes from: `ieq-amount` is a percentage, not a /10
scale.** The converter maps `ieq-amount` → `amount/10` (corpus value 10 →
scale 1.0 → full IEQ). Reading the same field as a *percentage*,
`amount/100`, gives 10 → 0.10 — exactly his weight, and exactly the
offline optimum (a fine weight sweep minimises in-band EE−DAX RMS at
w=0.100, rising on both sides). Corpus support: `ieq-amount` is 10 on
every internal-speaker profile but 8 on some headphone profiles, so it is
a real per-endpoint field consistent with a percentage. The mean-centering
he added is **not** load-bearing for the spectral match — centered and
uncentered give identical normalised shape (it shifts only broadband
level, removed by normalisation / the convolver's peak handling). So the
essential candidate correction is one line: `scale = ieq_amount/100`, not
`/10`.

**Why a small static weight works — `mi-ieq-steering-enable`.** The IEQ
profile carries `mi-ieq-steering-enable=1`: DAX applies the IEQ through
content-adaptive Media Intelligence steering, not a static EQ (the same MI
steering the converter already flags as non-LTI and unreproducible). A low
static weight (~amount/100) approximates the *steady-state* of that
dynamic stage — so the percentage reading is itself a steady-state
approximation, but XML-grounded rather than a magic number.

**Status — ADOPTED.** The default mapping is now `scale = ieq_amount/100`
(`dolby_to_easyeffects.py`), down from `amount/10`. Evidence: device-1 DAX
match (this finding) plus a second device — taprobane99's Yoga Slim 7x,
where two independent methods (his cepstral notebook at 10% and his RePhase
hand-tuning, the latter landing on flat HF: 19.7 kHz at −8.5 dB rel. 234 Hz,
not the −43 dB of full-weight IEQ) reach the same down-weight. The
generated default IR is byte-identical to the on-device-validated 0.10
variant. The temporary investigation flags (`--ieq-weight`/`--ieq-center`,
`--fir-interp`/`--fir-fftsize`/`--fir-dc-anchor`) and the `make_fir`
construction parametrisation are reverted — construction tweaks measured
negligible, mixed phase is rejected on latency.

**Residual open question (falsifiable).** Every speaker DAX capture we have
uses `amount=10`, so we cannot yet distinguish "amount/100 as a true
percentage" from "≈0.10 constant for speakers". The `/100` form is the
XML-grounded reading (it reduces to the confirmed 0.10 at amount=10 and
tracks the corpus variation — `amount=8` on some headphone profiles → 8%).
A DAX capture from a device with `ieq-amount≠10` would confirm or falsify
the percentage interpretation; until then `/100` is a hypothesis that fits
all current evidence, per the standing principle that the XML→parameter
mappings are empirically falsifiable.

### Unvalidated converter scaling factors (the `ieq-amount` class)

Finding 9 corrected a scaling *interpretation*, not an arithmetic slip:
`ieq-amount` was read as `amount/10` when the field is a percentage,
`amount/100`. The lesson generalises. The converter carries a cluster of
scaling factors that map an XML field onto a filter parameter through a
constant we *invented* rather than confirmed. The `/16`-dB convention is the
one such constant we have actually verified (issue #15: a user set ±12 dB in
DAX on Windows and the settings file stored ±192). The rest below are adopted
defaults that ship in the audible path but have never been individually checked
against a DAX capture. They are catalogued here as a class so a capture
campaign can attack them deliberately. This is distinct from the "Follow-ups"
list further down, which tracks ideas we considered and did *not* adopt — these
are live, shipping defaults.

| # | Factor (`dolby_to_easyeffects.py`) | XML field | Why it's a guess | Path status | What would falsify it |
|---|---|---|---|---|---|
| 1 | Dialog-enhancer gain ceiling: `amount/16 * 6.0` dB (HDA, `:1746`/`:2594`); `* 8.0` dB + a 4 kHz clarity bell at `*0.6` (SoundWire, `:1725`); bell centered 2.5 kHz, Q≈0.7 | `dialog-enhancer-amount` (0–16) | the XML gives only an amount; the dB ceiling (6/8), center, Q and clarity ratio are all converter-chosen — nothing in the schema says "6 dB" | **default audible** when `dialog-enhancer-enable=1` (X1 Yoga: `dynamic`/`movie` amount=5, `voice` amount=3; off on `music`/`game`) | the X1 Yoga DAX capture battery already covers these profiles at three DE amounts (0/3/5) — compare the 2–5 kHz residual DE-on vs DE-off: does DAX lift the speech band, and does the lift scale with `amount`? |
| 2 | Surround→stereo-base: `min(boost/20.0, 0.5)` (`:1681`/`:2599`) | `surround-boost` (1/16 dB) | the `/20` divisor and 0.5 cap are invented; Dolby surround is a spatial renderer, EE `stereo_tools` is a linear M/S balance | emitted when surround present (X1 Yoga: `surround-boost=96` on `dynamic`/`movie`) | an M/S-residual probe on the stereo-correlated / stereo-pink stimuli, but a number→spatial-param mapping is hard to ground-truth — low priority |
| 3 | Convolver SoundWire headroom restore: `peak_db * 0.5` (`:2682`) | (none — post-normalisation heuristic for the IEQ-only, no-AO SoundWire curve) | the 0.5 is chosen to "recover brightness"; not XML-derived | **default audible** on SoundWire | a SoundWire-device DAX capture (Snapdragon X / Yoga Slim 7x) |
| 4 | Regulator slope→ratio: slope read `/16` (`:1268`), then `ratio = 1/(1−slope)` (`:2020`) | `regulator-distortion-slope` | the `/16` reading is assumed by analogy to the dB fields; `1/(1−slope)` is inferred from how corpus values cluster | regulator only engages at high level | a bass-burst capture comparing gain-reduction-vs-level on devices with differing slope values |
| 5 | Regulator timbre→knee: timbre read `/16` (`:1270`), then `knee = −6·timbre` dB (`:2024`) | `regulator-timbre-preservation` (corpus-frozen at 0.75) | the `−6` dB maximum knee is a pure guess; the field is constant across the corpus, so we have no signal to disambiguate | regulator, high level | a device whose XML carries `timbre≠0.75`, plus a capture |
| 6 | MBC ratio `1/(coeff/32768)` (`:1839`/`:1864`); time constants via Q15 with `block_size=256` → 187.5 blocks/s (`:1810`) | `mb-compressor-tuning` 6-tuples | the Q15 format and 256-sample block size are assumed from common DSP practice and only sanity-checked numerically, never measured | **dormant** — the MBC doesn't engage on the −10 dBFS test stimuli (Finding 3) | loud/dynamic content that drives the MBC, then measure attack/release/ratio against DAX |
| 7 | Volume-leveler→autogain window: `max-history = 40−amount·4` / `30−amount·5` (`:1787`/`:1798`) | `volume-leveler-amount` (0–10) | the window formula is invented | **bypassed by default** (HDA); active only in the conservative SoundWire path | a capture of DAX's MI-steered leveler (non-LTI — hard) |

Confirmed for contrast: the `/16`-dB convention is verified (issue #15) and the
`/32768` Q15 decode is at least numerically consistent with first-order
time-constant theory; everything else above is unverified.

**Validation roadmap.** Ordered by how closely each mirrors the `ieq-amount`
case — a fixed scaling in the default path, measurable against a DAX capture:

1. *Offline pre-screen, on data already in hand.* Recompute the current
   converter's target without the stage under test, subtract from the matching
   DAX capture, and read the residual — the same offline screen that flagged
   the `ieq-amount` weight before any new measurement. The dialog enhancer
   (entry 1) is the strongest candidate: the X1 Yoga DAX battery already spans
   DE amounts 0/3/5, so the speech-band residual can be compared across
   profiles now. Surround (entry 2) and the regulator (entries 4/5) have
   in-hand data too, though both are expected to be partly inconclusive (the
   spatial mapping; the known DAX bass-control gap from Finding 4 and the
   regulator-stress follow-up).
2. *New in-house captures (X1 Yoga, HDA).* The Linux-side EE captures must be
   regenerated after Finding 9 — the FIR changed for every profile, so any
   pre-Finding-9 EE capture no longer reflects the shipped chain. Capture all
   profiles through the live EE chain for the confirmatory dialog EE↔DAX
   comparison; add higher-level stimuli to wake the MBC / regulator out of
   dormancy (entries 6, 4/5); capture the stereo stimuli on `dynamic`/`movie`
   for the surround mapping (entry 2). Tooling: `tools/measure_dax/` (Windows
   capture) and `tools/measure_ee/` (Linux loopback).
3. *User-contributed data.* The X1 Yoga is an HDA device and is corpus-frozen
   on several fields, so some entries can only be falsified by other
   hardware/XMLs: a SoundWire device for entry 3 and the SoundWire dialog
   mapping; a device with `ieq-amount≠10` for the Finding 9 residual; a device
   with `regulator-timbre-preservation≠0.75` or a differing
   `regulator-distortion-slope` for entries 4/5. The converter's
   `_UNMODELED_FEATURES` warning already nudges users to report XMLs whose
   `regulator-overdrive`/`relaxation-amount` deviate from the corpus constants.
   Track the asks on a GitHub issue (cf. issue #14 for the VBE follow-up).

Any EE↔DAX measurement is decided on on-device ground truth, the offline
pre-screens are only a filter, and changing a default mapping requires a
second-device confirmation (the bar Finding 9 met).

### Follow-ups to close the gap to DAX

The cheap, deterministic, XML-only experiments have been exhausted —
hypothesis (b) is rejected (Finding 6), no missed XML block (Finding
5), 5-profile coverage is in (Finding 6). What remains needs either
data outside the XML or a relaxation of the determinism / latency
constraints.

**Still actionable, no constraint change:**

1. **Stripped-down single-block tuning XML A/B on Windows.** Disable
   everything except IEQ in a tuning XML and capture DAX, then add
   AO, then add per-band PEQ, etc. Pinpoints which DAX stage is
   adding the fixed HF/mid behavior we cannot derive from the XML.
   Risk: needs driver-level XML replacement, could brick DAX on
   the test machine until restoration. Scope before attempting.

5. **`regulator-stress-amount` mapping investigated and rejected.**
   Issue #11 raised whether DAX's "tier-2" adaptive sub-models
   could explain part of the EE-vs-DAX gap. A corpus audit across
   ~2,900 XMLs settled the schema-prevalence side of that question:

   | field | enabled / non-default in any XML |
   |---|---|
   | `sliding-bass-enable=1` | 5 IdeaPad-3 XMLs (all `max-gain=0`, dormant) |
   | `volume-modeler-enable=1` | 0 |
   | `process-optimizer-enable=1` | 0 (bands always `array_20_zero`) |
   | `regulator-stress-amount` | non-zero on bass bands in 86% of XMLs |
   | `regulator-overdrive` | always `0` (35,654 profile slots) |
   | `regulator-relaxation-amount` | always `96` (13,042 profile slots) |

   So of the candidates, only `regulator-stress-amount` carries
   live, device-varying values. The remainder are dormant or
   constant across shipped tunings — implementing them blind would
   change zero output samples on real XMLs.

   For `regulator-stress-amount`, the only candidate worth testing,
   the natural mapping is "lower the per-band regulator threshold
   by `stress[i]` dB to make the limiter engage earlier on stressed
   bands". This was wired up behind a temporary `--enable-regulator-stress`
   flag (alignment hypothesis A: `stress[i]` indexes the post-grouping
   zone i; sign: stress > 0 → tighter limiter). Validation against
   the X1 Yoga DAX captures used a bass-burst stimulus (sustained
   sine tones at 50/80/120/180 Hz, -5 and -25 dBFS) — added to
   `tools/measure_dax/make_stimulus.py` and now part of the
   standard stimulus suite.

   **Outcome — hypothesis A is directionally falsified at 180 Hz**:

   - 180 Hz is the only diagnostic tone (50/80/120 Hz are
     attenuated 11-42 dB by the FIR + PEQ before reaching the
     regulator, so the regulator never engages on them in either
     EE config; 50 Hz also showed crest factor 23 dB on the DAX
     side, indicating Virtual Bass Enhancement adding harmonics
     that contaminate any regulator-only comparison).
   - At 180 Hz: DAX captured -6.14 dBFS, EE-off -8.93, EE-on -9.71.
     |DAX - EE_off| = 2.79 dB, |DAX - EE_on| = 3.57 dB → stress-on
     moved EE *away* from DAX, not toward it.
   - DAX's regulator engages ~19 dB of GR at 180 Hz (loud-quiet
     diff 1.13 dB instead of the 20 dB a dormant regulator would
     give). EE-on engages 0.78 dB. Whatever DAX is doing, it's an
     order of magnitude stronger than the 9 dB threshold drop our
     `stress=144` produces — the 1/16-dB convention may be wrong,
     stress may not be a threshold offset at all, or DAX's bass
     control runs through a stage we can't approximate.

   **The flag has been reverted** (per CLAUDE.md "investigation
   flags are temporary scaffolding"). The mapping math is documented
   here as a permanent finding rather than carried as a CLI switch
   future readers would feel obliged to keep correct.

   What remains in committed code:
   - `regulator-stress-amount` is parsed into the regulator dict and
     printed in the debug summary (no behavioural effect; visibility
     only).
   - `regulator-overdrive` and `regulator-relaxation-amount` are
     parsed, printed, and on the `_UNMODELED_FEATURES` watch list.
     Any XML where they deviate from the corpus-constants
     (`overdrive=0`, `relaxation=96`) will trigger a "report this
     XML" warning so we can re-investigate if the corpus assumption
     changes.
   - The bass-burst stimuli (`stimulus_bass_burst.wav` /
     `stimulus_bass_burst_quiet.wav`) ship as part of the standard
     measurement suite — useful diagnostic for any future
     bass-region work even though the stress hypothesis closed.

   Bigger picture (links back to Finding 4): DAX delivers 22-30 dB
   more bass to its regulator than our chain delivers to ours,
   then runs a much more active regulator on top. That's the gap
   to close, and it isn't reachable through the stress field. Two
   architectural levers might:

   - Less aggressive bass attenuation in the FIR/PEQ stages, so
     our regulator sees content above its threshold (currently a
     layer-2 IEQ/AO interpretation question — see Findings 6 and 7).
   - A level-dependent / VBE / leveler stage upstream of the
     regulator (no LSP equivalent of DAX's leveler exists; would
     need custom DSP or a different plugin pipeline).

   Both are larger pieces of work than this follow-up's scope.

   Out of scope (XML-zeroed across the corpus, would change zero
   output samples on shipped tunings): `sliding-bass-*`,
   `volume-modeler-*`, `process-optimizer-bands`. Documented here
   so they don't get re-proposed.

**Out of scope unless a constraint changes:**

2. **Match DAX's hybrid phase character** — partial-linear-phase FIR,
   adds ~20–40 ms group delay. Ruled out by no-added-latency
   constraint; needs an explicit decision to relax that. The
   `--fir-phase=linphase` flag is the upper-bound experiment for
   this; Finding 6 shows pure linear-phase doesn't help magnitude.
3. **Approximate DAX's leveler / regulator** — closes the multitone-LF
   gap and the −18 vs −42 dBFS sweep difference. Substantial RE
   effort; naively re-enabling EE autogain reintroduces the pumping
   trap (see "Why autogain is bypassed by default").

**Pragmatic shortcut if determinism is relaxed:**

4. **Empirically tune the preset to match DAX's *captured* response,**
   not the XML's published curves. Fit a FIR + biquad chain to the
   DAX pink-noise capture directly. Loses the "we faithfully apply
   the published XML" property but produces a Linux preset that
   audibly matches Windows. Could be opt-in via a flag so the
   principled path stays the default. The variant matrix in Finding
   6 plus the per-band table identify the specific dB targets such
   a tuner would need to hit (e.g. flatten the HF rolloff above
   ~10 kHz, soften the +4 dB at 2.25 kHz, lift 5–6 kHz).

**Closed by the variant sweep (Finding 6 / 7) — kept here as
historical record; do not re-litigate without new evidence:**

  - "Try `IEQ − AO`" — rejected; +7–20 dB worse on every profile.
  - "Run on the other 4 profiles" — done; HF gap is profile-independent.
  - "Audit the XML schema for missed HF-shaping blocks" — done;
    none found (Finding 5).
  - "Soften the HP at 100 Hz from `x2` to `x1`" — the test XML's
    HP is XML-driven (order=4 → x2), not the line-1581 filler
    path; softening would diverge from the deterministic mapping.
    `no-HP` variant in Finding 7 confirms HP is responsible for
    ~25 dB at 47 Hz — removing it overshoots DAX, so HP topology
    is correct, only the slope might differ.
  - "Drop a 2.25 kHz attenuation bell in `equalizer#1`" — would
    work as an empirical fix for the +4 dB band but loses
    XML-determinism; folded into option 4 above.
  - "Soft-clamp the IEQ+AO target depth (α)" — Finding 7;
    pareto trade — every clamp depth swaps HF residual for
    mid-band residual, no setting moves every band toward DAX.
  - "Reinterpret `ieq-amount` as a +/- dB cap (β)" — Finding 7;
    *cleanest candidate* — every band moves toward DAX (no
    regression), 19.7 kHz gains +9.7 dB. Not adopted because
    the 19.7 kHz gap is still 18 dB after applying it, so β
    alone can't be the rule we're missing. Worth revisiting if
    a second device's DAX captures show the same per-band
    improvement.
  - "Apply IEQ only inside a frequency window (γ)" — Finding 7;
    pareto trade — biggest HF reduction (−10.5 dB at 19.7 kHz),
    but 47 Hz blows out from −8 to −18 dB EE−DAX.

## Rejected approaches

Things that were investigated and explicitly declined, recorded so they don't get
re-proposed:

- **Noise gate before the compressor.** Would prevent noise-floor amplification, but
  real content rarely has an audible noise floor at the levels that trigger the
  compressor. Adds complexity for no practical benefit.
- **GPU compute for FIR convolution.** See `docs/alternative-pipelines.md` Option 5.
  The FIR convolver uses <0.1% of a single CPU core; there's no CPU pressure, and
  CPU→GPU round-trip latency is unacceptable for realtime audio.
- **Custom SOF DSP topology with FIR + DRC modules.** See `docs/alternative-pipelines.md`
  Option 2. Highest offload potential, but requires rebuilding signed firmware and
  custom topology files — too much maintenance burden for a workstation tool.
- **Parametric-EQ approximation of the IEQ curve** (instead of FIR). Produced ±4–5 dB
  ripple between Dolby's 20 band centers regardless of how the solver was tuned.
  See the README's "IEQ target curves are composite targets" table for the full
  comparison.
- **Auto-trimming the convolver IR to its audible length.** Issue #11 noted that the
  4096-tap (~85 ms) IR has a long sub-noise-floor tail. A sweep across 729 FIRs from
  11 device groups (Realtek HDA, Senary, Qualcomm, AMD, ThinkPad / IdeaPad / AIO
  variants) measured trim length as the smallest cutoff beyond which every tail
  sample is below the FIR's peak by ≥ N dB, rounded up to a 64-sample boundary.
  Distributions of trimmed length as % of the original 4096 taps:

  | threshold | mean | p10 | p50 | p90 | max | mean ms saved |
  |-----------|-----:|----:|----:|----:|----:|--------------:|
  | −80 dB    |  48% | 34% | 52% | 55% | 66% |       ~44 ms  |
  | −90 dB    |  54% | 50% | 53% | 63% | 78% |       ~39 ms  |
  | −100 dB   |  60% | 52% | 56% | 73% | 94% |       ~34 ms  |
  | −110 dB   |  69% | 53% | 66% | 91% |100% |       ~26 ms  |
  | −120 dB   |  81% | 63% | 80% | 98% |100% |       ~17 ms  |

  Per-device means at −100 dB clustered tightly (56–69% across all codecs except
  one 3-FIR outlier at 88%) — not device-specific. So the trim would be safe to
  ship. But EasyEffects' Convolver wraps `libzita-convolver` directly and calls
  `Convproc::configure(2, 2, kernel.sampleCount(), bufferSize, bufferSize, Convproc::MAXPART, density)`
  ([EE source][ee-conv]) — i.e. `minpart == quantum == bufferSize`. zita-convolver
  is a non-uniform partitioned FFT convolver where I/O latency is set by the
  first (smallest) partition and progressively larger partitions process the
  tail; with `minpart` pegged to the audio quantum the convolver adds zero
  latency on top of the PipeWire buffer for any IR length up to multi-second IRs.
  So trimming would save ~½ of an already <0.1%-of-a-core convolver workload and
  ~16 KB per file with no audible or perceptible-latency change. Not worth the
  maintenance cost of a threshold parameter that would invite future "is this
  audible?" re-litigation each time the cepstral construction is touched.

[ee-conv]: https://github.com/wwmm/easyeffects/blob/dc14767e8bcf/src/convolver_zita.cpp#L103
