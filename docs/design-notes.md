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
our chain is highly faithful to the XML target. The 11.84 dB
EE-vs-DAX residual recorded in Finding 6 is dominated by fixed-DAX
behavior outside the XML, not by drift in our implementation.

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

**Closed by the variant sweep (Finding 6) — kept here as historical
record; do not re-litigate without new evidence:**

  - "Try `IEQ − AO`" — rejected; +7–20 dB worse on every profile.
  - "Run on the other 4 profiles" — done; HF gap is profile-independent.
  - "Audit the XML schema for missed HF-shaping blocks" — done;
    none found (Finding 5).
  - "Soften the HP at 100 Hz from `x2` to `x1`" — the test XML's
    HP is XML-driven (order=4 → x2), not the line-1581 filler
    path; softening would diverge from the deterministic mapping.
  - "Drop a 2.25 kHz attenuation bell in `equalizer#1`" — would
    work as an empirical fix for the +4 dB band but loses
    XML-determinism; folded into option 4 above.

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
