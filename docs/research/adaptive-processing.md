# Adaptive processing: leveler, autogain, MBC, dialog enhancer and surround

## Where this stands

[reference.md](../reference.md) covers what the converter emits, and
[ee-to-pipewire.md](../ee-to-pipewire.md) the PipeWire path.

- **Autogain** ships bypassed on HDA, and `--enable autogain` opts in. An HDA
  default flip failed the listening gate on a ThinkPad X1 Yoga Gen 7
  ([#25](#r-autogain-default-flip)).
- **The PipeWire converter** translates active autogain to LSP
  `autogain_stereo` ([PW](#r-autogain-pw-translation)).
- **DAX's leveler and regulator** are time-varying and content-adaptive, so a
  sweep recovers no true linear impulse response ([LTI](#r-dax-lti-behaviour)).
- **The dynamics plugins** are [passive](#r-dynamics-dormancy) at nominal levels
  on the test XML. On loud stepped tones DAX compresses far harder than our
  chain; the diagnosed lever is the [regulator](#r-mbc-ratio-time-constants).
- **`surround-boost`** is unmapped: on one device DAX widened 2-channel
  content by essentially zero ([surround](#r-surround-boost-stereo-base)).

Open:

- The dialog enhancer's 6 dB ceiling is unconfirmed, and our bell appears to
  over-apply vs DAX. Settling it needs a speech source that demonstrably
  engages DAX's DE, ideally a same-profile DE-on-vs-off capture
  ([dialog](#r-dialog-enhancer-gain-ceiling)).
- The leveler window formula and the conservative path's −6 dB target offset
  are invented. Falsifying them needs a capture of DAX's MI-steered leveler,
  hard because it is non-LTI ([window](#r-leveler-autogain-window),
  [offsets](#r-conservative-autogain-offsets)).
- The MBC's Q15 format and 256-sample block size are assumed and only
  sanity-checked numerically, never measured
  ([MBC](#r-mbc-ratio-time-constants)).
- The limiter and MBC character knobs need a stimulus that engages them, such
  as clipping-engaging sustained tones or live program material
  ([dormancy](#r-dynamics-dormancy)).
- A loopback cannot show crosstalk cancellation at the ears or a content-gated
  virtualizer. A binaural capture or listening test would
  ([surround](#r-surround-boost-stereo-base)).
- Parked: approximating DAX's leveler, out of scope unless a constraint changes
  ([follow-up](#r-dax-leveler-approximation)).

<a id="r-dynamics-dormancy"></a>

## Measurement outcome: dynamics plugins on the test stimuli

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
cannot characterise the parameters in the
[audit table](../design-notes.md#plugin-parameter-audit) flagged as "AUDIBLE"
but living in those plugins: `limiter mode/oversampling`, `MBC compressor-mode`,
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
[XML-interpretation hypotheses](../design-notes.md#r-xml-interpretation-hypotheses)
explains why this distinction matters. The 11.84 dB EE-vs-DAX residual recorded
in the [AO sign variant matrix](../design-notes.md#r-ao-sign-variant-matrix) is
therefore not implementation drift. At the time of this measurement it was
attributed, per the
[AO sign variant matrix](../design-notes.md#r-ao-sign-variant-matrix) and the
[XML-interpretation hypotheses](../design-notes.md#r-xml-interpretation-hypotheses),
to fixed DAX-internal behavior outside the published XML. The
[`ieq-amount` scaling finding](../design-notes.md#r-ieq-amount-scaling) later
showed it was dominated by the converter's own `ieq-amount` scaling error, since
fixed. The remaining EE-vs-DAX residual is ~1 dB RMS at HF plus the LF/leveler
gap.

For the rows marked "open" in the
[table](../design-notes.md#plugin-parameter-audit) (MBC and limiter character
knobs), defaults are safe at nominal levels. Revisit them if a future
investigation focuses on transient or peak-engaging content.

<a id="r-autogain-bypassed-by-default"></a>

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

<a id="r-autogain-default-flip"></a>

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

<a id="r-autogain-pw-translation"></a>

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
[`tools/measure_pw/autogain_proof.py`](../../tools/measure_pw/autogain_proof.py)
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
[`tools/measure_pw/autogain_fullchain.py`](../../tools/measure_pw/autogain_fullchain.py).

<a id="r-mbc-time-constant-decode"></a>

## Q15 block-rate time constants

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

<a id="r-dax-lti-behaviour"></a>

## DAX3 LTI behaviour for our stimuli

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

<a id="r-dialog-enhancer-gain-ceiling"></a>

## Dialog-enhancer gain ceiling

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
speech bell (see
[roadmap](../design-notes.md#verification-status-and-the-validation-roadmap)):
DAX's DE is evidently speech-gated. The battery carries `stimulus_speech`:
espeak-ng synthesis when installed, else an LTASS-shaped-noise fallback that may
not trip an MI speech classifier. The capture protocol must therefore verify a
nonzero DE-on-vs-off contrast before concluding anything.

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

## Surround→stereo-base

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
[`ieq-amount` scaling finding](../design-notes.md#r-ieq-amount-scaling)).

<a id="r-mbc-ratio-time-constants"></a>

## MBC ratio and time constants

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
([`tools/measure_ee/dynamics_gap.py`](../../tools/measure_ee/dynamics_gap.py);
agent analysis, key numbers re-verified from the converter):

- (i) 1 kHz-referenced, the level each chain delivers to its dynamics agrees
  within ±3 dB at every diagnostic band (141–4193 Hz). The "DAX delivers +16–22
  dB more" reading was a reference artifact: our FIR is peak-normalised to a
  different anchor than DAX's OFF-flat baseline. The real 22–30 dB bass gap (the
  [EE response vs XML](../design-notes.md#r-ee-response-vs-xml)) sits below ~120
  Hz, pre-attenuated by the 100 Hz HP before either chain's dynamics.
- (ii) The MBC decode is internally faithful but *conservative*. A 3-level fit
  (−42/−18/−2) shows EE realises its nominal 1.67 ratio only at the one band
  that clears threshold well (234 Hz, R≈1.54). Elsewhere the −6 dB soft knee and
  RMS detection keep it sub-slope.
- (iii) DAX's effective ratio at 234/277 Hz is ≈ 2.95, its near-100:1 regulator
  stacking on the MBC. Our regulator maps the same −10/−9/−8/−5 dB thresholds
  and slope (the
  [fixed dynamics constants](../design-notes.md#r-fixed-dynamics-constants)),
  yet barely fires there.

So the lever is the regulator, not the MBC ratio/threshold, which stays
XML-derived and unchanged. See the
[fixed dynamics constants](../design-notes.md#r-fixed-dynamics-constants).

<a id="r-leveler-autogain-window"></a>

## Volume-leveler→autogain window

- **Factor (generator):** `max-history = 40−amount·4` / `30−amount·5`
  (`make_autogain`).
- **Why it's a guess:** The window formula is invented. It measured as no
  reaction-speed lever at all: 20/32/40 s gave identical ~4 dB onset overshoot
  (see "The 2026-07 default-flip attempt").
- **What would falsify it:** A capture of DAX's MI-steered leveler (non-LTI, so
  hard).

<a id="r-conservative-autogain-offsets"></a>

## Conservative-autogain offsets

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

<a id="r-dax-leveler-approximation"></a>

## Approximate DAX's leveler / regulator

Status: out of scope unless a constraint changes.

It would close the multitone-LF gap and the −18 vs −42 dBFS sweep difference, at
substantial RE effort. Naively re-enabling EE autogain reintroduces the pumping
trap (see "Why autogain is bypassed by default").

## Rejected approaches

<a id="r-compressor-noise-gate"></a>

### Noise gate before the compressor

**Noise gate before the compressor.** It would prevent noise-floor
amplification, but real content rarely has an audible noise floor at the
levels that trigger the compressor. It adds complexity for no practical
benefit.

<a id="r-unused-ee-builtins"></a>

### An *unused* EasyEffects built-in to cover a dropped DAX feature

**An *unused* EasyEffects built-in to cover a dropped DAX feature**
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
[cross-device-findings.md](../cross-device-findings.md) newer-pipeline DSP
blocks. It is a real, enabled feature with no representation, but it fails both
bars. The `dynamic-speaker-optimization-amount`/`-speaker-interval` → MBC-band-0
threshold transfer is opaque: Dolby's driver-size excursion model leaves no
derivable mapping. It exists on a single device, which makes it unvalidatable.
And a crude MBC band-0 limiter would add the pumping DSO is built to avoid. It
stays warned-at-parse, not mapped, which is the correct state. Net: the
remaining fidelity work is device-gated *tuning* of plugins already in the chain
([Unvalidated converter scaling factors](../design-notes.md#unvalidated-converter-scaling-factors-the-ieq-amount-class)),
not new plugins.

## Elsewhere

- The MBC and autogain rows of the plugin parameter audit:
  [design-notes](../design-notes.md#plugin-parameter-audit).
- The loudness side of the MBC diagnosis, the regulator's under-engagement:
  [fixed dynamics constants](../design-notes.md#r-fixed-dynamics-constants).
- The validation roadmap's dialog and surround pre-screens:
  [design-notes](../design-notes.md#verification-status-and-the-validation-roadmap).
- MI steering on #46's T495 and the profile choice:
  [design-notes](../design-notes.md#a-tuning-pinned-at-the-gain-rail-the-t495-issue-46).
- DAX's leveler gain on the dev device, measured for `--enable level-restore`:
  [design-notes](../design-notes.md#giving-back-what-normalisation-removed---enable-level-restore-issue-50).
