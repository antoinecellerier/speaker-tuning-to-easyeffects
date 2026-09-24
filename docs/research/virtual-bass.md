# Virtual bass: DAX's VBE, the Calf bass enhancer and `--enable virtual-bass`

## Where this stands

[reference.md](../reference.md) covers what the converter emits, and
[ee-to-pipewire.md](../ee-to-pipewire.md) the PipeWire path.

- **DAX runs VBE** on the dev X1 Yoga, an HDA device: odd harmonics at
  near-fundamental amplitude on a 50 Hz tone ([VBE](#r-dax-virtual-bass)).
- **Whether DAX engages it** is decided outside any file we read. #44's DAX
  applies none with identical VBE fields ([#44](#r-dax-virtual-bass)).
- **`--enable virtual-bass`**, opt-in and PipeWire-only, scores S = 4.43 against
  DAX on one device, the dev X1 Yoga, where emitting nothing scores 10.01
  ([phase 2](#phase-2-2026-08-decoding-virtual-bass-subgains-and-scoring-a-chain)).
- **The historical Calf BassEnhancer capture** on the dev X1 Yoga scores 18.43,
  worse than doing nothing
  ([phase 2](#phase-2-2026-08-decoding-virtual-bass-subgains-and-scoring-a-chain)).
- **SoundWire presets** emit Calf BassEnhancer by default, every knob
  converter-chosen ([constants](#r-soundwire-bass-enhancer-constants)).

Open:

- What enables VBE on one device and not the other: engine generation and
  product tier covary on our two data points. #44's machine on an updated
  driver would be the cleanest discriminator ([#44](#r-dax-virtual-bass)).
- The SoundWire bass-enhancer constants await a SoundWire device's DAX
  bass-burst capture, the test that would falsify them
  ([constants](#r-soundwire-bass-enhancer-constants)).
- Revisiting a flip of the SoundWire stage to opt-in is gated on #29's capture.
  The XML-anchored `floor = 35 Hz` / `scope = 160 Hz` candidate, deferred
  2026-08-28, waits for the reporter's A/B or capture
  ([#29](#r-soundwire-bass-enhancer-constants)).

<a id="r-dax-virtual-bass"></a>

## DAX runs psychoacoustic VBE; the schema can't drive a per-device mapping

The loud 50 Hz region of the DAX bass-burst capture, at peak −5 dBFS, shows a
textbook missing-fundamental harmonic complex. The stimulus is the bass-burst
battery in `tools/measure_dax/make_stimulus.py:make_bass_burst`: sustained sine
tones at 50 / 80 / 120 / 180 Hz, ±5 / −25 dBFS peak. The same battery closed the
unrelated regulator-stress investigation (the
[`regulator-stress-amount` follow-up](../design-notes.md#r-regulator-stress-amount)).

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
analyzed in the [EE response vs XML](../design-notes.md#r-ee-response-vs-xml),
the [HF-shaping block audit](../design-notes.md#r-hf-shaping-block-audit), the
[AO sign variant matrix](../design-notes.md#r-ao-sign-variant-matrix) and the
[XML-interpretation hypotheses](../design-notes.md#r-xml-interpretation-hypotheses).
Pink-noise captures would not show it, because the harmonic complex blends into
the broadband spectrum. It shows up only on tonal bass content.

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

1. **Bass-attenuation gap**
   ([EE response vs XML](../design-notes.md#r-ee-response-vs-xml),
   [XML-interpretation hypotheses](../design-notes.md#r-xml-interpretation-hypotheses)):
   our chain attenuates 50 Hz ~21 dB more than DAX does. It lives outside the
   XML. DAX's regulator / leveler appears to actively boost quiet sustained low
   tones, which is what hypothesis δ in the
   [XML-interpretation hypotheses](../design-notes.md#r-xml-interpretation-hypotheses)
   was tracking. Closing this gap needs a level-dependent / content-adaptive
   boost upstream, not a harmonic synthesizer.
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

### Phase 2 (2026-08): decoding `virtual-bass-subgains` and scoring a chain

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
DAX](../images/vbe-cells-vs-dax.png)

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

<a id="r-soundwire-bass-enhancer-constants"></a>

## SoundWire Calf BassEnhancer constants

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
[conservative-autogain offsets](../design-notes.md#r-conservative-autogain-offsets)
is what it exercises.

## Elsewhere

- The XML field inventory's bass-enhancement paragraph:
  [design-notes](../design-notes.md#simplified-schema-xmls-gain_lgain_r-audio-optimizer-issue-22).
- The 50 Hz bass-attenuation gap:
  [XML-interpretation hypotheses](../design-notes.md#r-xml-interpretation-hypotheses).
- The unused EasyEffects built-ins, Bass Loudness among them:
  [design-notes](../design-notes.md#rejected-approaches) "Rejected approaches".
- #44's loud-bass DAX captures and the dev device's low-end gap:
  [design-notes](../design-notes.md#why-bypass-has-more-bass-than-the-preset-issue-44-round-3-2026-08-22).
