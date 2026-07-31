# Design notes

> Interpretive analysis of the parameter schema used by Dolby DAX3 tuning XML (distributed publicly as part of Windows audio driver packages), for the purpose of Linux interoperability. No verbatim tuning arrays are reproduced.

Why the generated EasyEffects preset looks the way it does. [reference.md](reference.md)
covers *what* the script emits (mappings, plugin chain, units, what's not implemented);
this doc covers the architectural *why*, so future readers don't have to
reverse-engineer it from commit history.

> **This file is the research log** — findings in roughly the order they were
> established, including superseded hypotheses kept for the audit trail (see the
> "Superseded by Finding 9" banners below). For the settled current-state
> summary, start with [reference.md](reference.md); for the open threads worth
> picking up, see "Unvalidated converter scaling factors" and "Follow-ups to
> close the gap to DAX" further down.

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

### Simplified-schema XMLs: `gain_l`/`gain_r` audio-optimizer (issue [#22](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/22))

Some Lenovo drivers (xml_version ~3.2.x — e.g. ThinkPad X1 Carbon Gen 8) ship a
*simplified* DAX3 schema the converter now supports (`parse_xml`, audio-optimizer
block). Two things differ from the full schema:

- **`<audio-optimizer-bands>` names the channels `gain_l`/`gain_r`/`gain_c`/…**
  (a 10-channel surround layout) instead of `ch_00`..`ch_07`. They are the same
  20-band, 1/16-dB correction arrays resolved through the same `value=`/`preset=`
  mechanism, so for a 2-channel speaker `gain_l`→left, `gain_r`→right. The
  measured value range matches the full schema's `ch_00`/`ch_01` (single-digit dB
  typical, up to ~30 dB on a worst-case band), corroborating the shared encoding.
  **Units and channel assignment confirmed on device 2026-07-30** — a DAX
  capture battery from a simplified-schema machine matches the converter's
  curve to ~0.7 dB mean, including the per-channel L/R split (Finding 10,
  issue [#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44)).
- **No `mb-compressor-*` and no `speaker-peq-*` blocks** — the simplified variant
  omits MBC and speaker PEQ entirely. The existing enable-gates skip them
  gracefully (no `equalizer#0` / `multiband_compressor#0` in the output). The
  regulator and every `tuning-cp` block (dialog, surround, leveler, volmax) are
  unchanged; `regulator-tuning` is still one threshold per band, so
  `make_regulator` needs no special-casing.

**Gotcha:** a simplified profile carries *two* `<audio-optimizer-bands>` — a
zeroed one under `tuning-cp` and the real correction under `tuning-vlldp`.
`parse_xml` reads the `tuning-vlldp` one (correct). A `.//audio-optimizer-bands`
XPath matches the `tuning-cp` zeros first and will wrongly read the AO as flat —
match the container (`tuning-vlldp`) explicitly when inspecting these files.

**What else we don't read — audited (issue [#22](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/22)).** Beyond MBC/PEQ (absent), the
simplified `tuning-cp`/`tuning-vlldp` carry many elements the converter ignores.
A sweep of the simplified corpus (~2,100 profiles) confirms none is active,
derivable tuning being silently dropped — each falls into one of three buckets:

- **Gated off in every profile** (inert defaults): `bass-enhancer-*`,
  `bass-extraction-*`, `virtual-bass-*` (`virtual-bass-mode=0`),
  `graphic-equalizer-*`, `volume-modeler-*`, `process-optimizer-*`,
  `dialog-enhancer-ducking`, `height-filter-mode` — all `*-enable=0`. The level
  controls `pregain`/`postgain`/`system-gain`/`calibration-boost` are all `0`.
- **Active but not modelable in a static LV2 chain** (already out of scope): the
  legacy `output-mode-partial-{surround,height}-virtualizer-enable` (the surround
  one was once approximated by `stereo_tools` from `surround-boost`, but that
  mapping was removed 2026-06-13 — DAX applies no stereo widening on 2-ch
  content, entry 2; the FFT-domain part isn't reproducible) and the
  `mi-*-steering-enable` flags (Media-Intelligence real-time content steering —
  nothing static to bake).
  `virtualizer-*-speaker-angle` is inert geometry without an active virtualizer.
- **Already covered via another element**: `regulator-enable` (CP) — the
  regulator is mapped from the VLLDP `regulator-tuning`; `ieq-bands-set` — a
  per-profile selector pointing at one of the IEQ curves already read from
  `<constant>` (we emit all three; we just don't honour the per-profile default).

On bass enhancement specifically: `bass-enhancer-*` and `virtual-bass-*` are not
merely disabled in every profile (`bass-enhancer-enable`/`virtual-bass-mode` are
`0` across all ~38k occurrences in the corpus) — their supporting fields
(`cutoff`, `width`, `mix-freqs`, `src-freqs`, `subgains`) are *frozen identical*
across hundreds of speaker designs, so unlike the AO / MBC / regulator values
they carry no per-device signal to derive. The bass enhancement DAX audibly
applies is a non-XML engine baseline, not per-device tuning — investigated at
length in **Finding 8** (incl. the `--enable-vbe` experiment), with the opt-in
baseline tracked in issue [#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14). So there is nothing simplified-schema-specific to
add here; the explicit `boost`/`cutoff`/`width` look mappable to Calf
`bass_enhancer` but, being corpus-frozen, would be a hardcoded baseline rather
than derived tuning.

**Field follow-up — "loads but inaudible" (issue [#22](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/22), UNCONFIRMED root cause).**
The reporter (X1 Carbon Gen 8) found the generated preset loaded but produced no
audible difference. What we have *established*: the generator is correct for his
hardware. Re-deriving from his actual XML (`DEV_0257_SUBSYS_17AA22B4_…`) shows
the `gain_l`/`gain_r` curves are substantial (~15 dB p-p, comparable to a
full-schema device), and `make_fir` on the combined IEQ+AO target yields a
realized convolver magnitude of ~13 dB p-p across 100 Hz–16 kHz — strongly
audible; the simplified path shares the validated full-schema FIR/convolver code
(same `kernel-name`, same min-phase FIR). That the preset *should* be audible
points away from the script — but the actual reason he hears nothing is **not yet
confirmed** (awaiting his report). Candidate environmental causes, in rough order
of likelihood: **EasyEffects 7** (the v8 preset format is incompatible, and the
convolver key changed `kernel-path`→`kernel-name` between 7 and 8, so on EE 7 the
convolver — the dominant block — would silently load no kernel while subtler
blocks still appear "loaded"); Flatpak/native write-vs-run mismatch; a
missing/misplaced `.irs`; no Dolby preset selected; or global bypass on. Rather
than hand-hold each user through GUI questions, `--doctor` (and a proactive
end-of-run warning in normal mode) now surfaces these deterministically — turning
the hypotheses into something the user's own machine can confirm or rule out. The
`kernel-path`→`kernel-name` mechanism is intentionally kept out of user-facing
text (it lives in code/tests/this note); users see a plain-language "install
EasyEffects 8" message.

### Per-channel regulator thresholds: newer SoundWire schema (`SUBSYS_37A317AA`)

A second SoundWire schema-variant surfaced in the 2483-XML re-derivation, in the
regulator block this time rather than the audio optimizer. The newer Lenovo
IdeaPad-5x-2-in-1 SoundWire tuning (`SUBSYS_37A317AA`) nests
`regulator-tuning/threshold_high` (and `threshold_low`) under per-channel
`<ch_00>…<ch_07>` elements instead of carrying a flat `value=`/`preset=` on the
element itself:

```xml
<threshold_high>
  <ch_00 value="-282,-294,-243,-160,0,…,0" />   <!-- real per-band thresholds -->
  <ch_01 value="-282,-294,-243,-160,0,…,0" />
  <ch_02 preset="array_20_zero" /> …            <!-- unused channels -->
</threshold_high>
```

This is the same per-channel shape as the audio optimizer's `ch_00`/`ch_01`
(resolved through the identical `value=`/`preset=` mechanism). The flat
`resolve_xml_value` read nothing off the parent element, so `threshold_high`
resolved to `""` and the regulator fell back to `[0.0]*20` — **no per-band
limiting**, the worst failure mode for a speaker-excursion guard. `make_regulator`
consumes only `threshold_high`, so this alone disabled the device's protection.

`resolve_channel_or_direct` now reads `ch_00` (the stereo limiter is a single
instance, so ch_00 is the left-channel reference; `make_regulator` is unchanged).
It warns if `ch_01` diverges and if the tuning is genuinely empty. **Per-band-min**
across ch_00/ch_01 would protect both channels but can over-limit the one that
didn't need it, so the choice was left to a future device that actually shows L/R
asymmetry — on the only device with this schema today, ch_00 == ch_01.

**Why this ships default (XML-only, but single-sample).** The threshold→limiter
mapping is the same one already validated on the development device; only the parse
*source* is new, so reading `ch_00` is not a new param hypothesis — and the prior
behaviour (no limiting) is unambiguously wrong for a protection feature. The
honesty caveat: this `ch_00`→`threshold_high` reading rests on **exactly one corpus
device** (no second device with the schema exists to cross-check), and it has not
been verified on the hardware. Scope was re-derived with `corpus_audit`'s
`threshold_schema` classifier (9 profiles / 1 device carry the dropped form; 33,113
other reg-enabled internal_speaker profiles use the flat form and are untouched).
DSO and the advanced virtualizer for this device remain unmodeled (see
cross-device-findings §14), so its preset is still incomplete — the regulator fix
closes the most dangerous gap, not all of them.

## Plugin chain order

Current order (see `make_preset` in `dolby_to_easyeffects.py`):

```
Convolver → [Bass Enhancer] → Equalizer (PEQ)
    → Dialog Enhancer EQ → Autogain → MB Compressor → Regulator → Limiter
```

(`Bass Enhancer` is emitted only for SoundWire devices — see the harmonic
bass restoration discussion under Finding 8. A `Stereo Tools` widener used
to sit after the bass enhancer, mapped from `surround-boost`; it was removed
2026-06-13 after a DAX capture showed Dolby applies no stereo widening on
2-channel content — see unvalidated-scaling entry 2.)

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
  (`docs/cross-device-findings.md` §6/§13, 2483-XML cohort) shows ~97% of
  devices use `regulator-distortion-slope=16` — a true brickwall — while the
  rest use a softer slope. (The original 196-file cohort suggested a 53/47
  split; the expanded corpus revised it.) The explicit LSP limiter is redundant
  on the brickwall-slope devices and essential on the rest.

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
| Regulator `input-gain` (volmax) | +6 dB typical (device/profile-specific) | Dolby's `volmax-boost` (volume-leveler loudness ceiling), applied statically. Default slot: `multiband_compressor#1.input-gain` (pre-band-limiting, so the regulator tames the boosted bass before the brickwall; falls back to `limiter#0.input-gain` when the regulator is absent). `--disable volmax` turns it off; `--volmax-slot output-gain` re-routes it after the regulator (opt-out — the pre-#23 placement, which on loud low frequencies could drive the brickwall into distortion). Neither slot is Dolby-derived. Full finding, on-device metrics, and corpus verdict: **["volmax-boost slot" below](#volmax-boost-slot-issue-23).** |
| MBC upward compression | **0 dB** | LSP plugin defaults enable upward compression below `boost-threshold=-72 dB`. Dolby's compressor is purely downward. Commit `e454711` disables it on both MBC instances. |
| Regulator upward compression | **0 dB** | Same LSP default issue — upward compression on a *limiter* is especially wrong. Also fixed in `e454711`. |
| Output limiter | −1 dBFS | Final catch-all for inter-sample peaks after everything else. |

With these fixes in place, the normal-operation surplus is small enough that content
sits at target loudness without the regulator triggering, and worst-case quiet-input
scenarios are caught by the brickwall limiter rather than clipping the output.

### `volmax-boost` slot: `input-gain` (default) vs `output-gain` (opt-out, issue [#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23)) {#volmax-boost-slot-issue-23}

**Symptom.** [#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23)
(ThinkPad X13 Gen 6) reported audible distortion on loud *low* frequencies with volmax
on, gone with `--disable volmax`.

**Mechanism.** `volmax-boost` is Dolby's volume-leveler ceiling — a +6 dB *dynamic*
gain, rendered here as a *static* gain (no MI-steered leveler to replicate). On the
`output-gain` slot (the original default) it's added *after* the regulator's per-band
limiting, so it feeds the −1 dBFS brickwall directly; on loud content the loudest band
clips. The current default `input-gain` (below) avoids this.

**The output-gain placement is not Dolby-derived** (corrects a prior claim). Commit
`a50f61d` called it "mirroring Dolby's VolMax placement inside the VLLDP pipeline" — an
assertion with no Dolby source. `volmax-boost` lives in `tuning-cp` (the CP stage, next
to `volume-leveler-*`), so applying it at the *output* of a VLLDP-stage regulator is
upside-down vs CP→VLLDP order. Output-gain *is* defensible on **loudness delivery**
(last stage before the brickwall → the makeup reaches the output, the issue [#9](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/9) goal),
not topology. (The XML child-element order is canonical/alphabetical, not signal-flow,
so it doesn't source the within-stage order either.)

**`--volmax-slot input-gain`** moves the boost *ahead* of the regulator's per-band
downward compression, so the boosted low end is tamed before the brickwall. Both
backends carry it (EE `input-gain` ↔ LSP `g_in` in `ee_to_pipewire`).

**On-device A/B** (dev device = X1 Yoga G7 `17AA22E6`; 2026-06-22; live-EE loopback via
`tools/measure_ee/`).

*Distortion* — sustained 234 Hz tone at the FIR peak (−2 dBFS): output-gain
**11.6% THD** (brickwall clipping), input-gain **0.06% THD**, for a **1.46 dB** level
cost at that band. Audibly decisive on a swept tone (clean hum vs reedy buzz).

*Loudness* (integrated LUFS — the proper #9 check), 3-way on broadband pink:

| pink stimulus | no-volmax | output-gain | input-gain |
|---|---|---|---|
| Loud master (peak −0.5 dBFS) | −19.8 | −13.8 (+6.0) | −13.8 (+6.0) |
| Moderate (peak −5.4 dBFS) | −24.7 | −18.7 (+6.0) | −18.7 (+6.0) |

Both slots add the **full +6.0 dB** over no-volmax and are **identical (0 dB give-back)**
at both levels — **no loudness impact on normally-loud program material**. Input-gain's
cost is confined to sustained FIR-peak bass (the 1.46 dB above); broadband content never
engages the regulator enough to lose it.

**Why the 0 dB give-back is a best-case artifact** (`corpus_audit.py`, 2026-06-22; 7620
active-band FOCUS = dynamic/movie/music/game rows):
- **slope=16** (hard brickwall) on 72–92% of profiles → ratio isn't the differentiator,
  thresholds are.
- **threshold_high active-min**: corpus median **−18 dB** (p10 −26, p90 −12). The dev
  device sits at **−10 dB** — *less* aggressive than **91–94%** of FOCUS profiles, and
  its regulator independently under-engages (entries 6/11). It barely grabs the boost →
  0 dB loss. On a typical/aggressive regulator, input-gain compresses the boost harder →
  real loudness-loss / pumping we did **not** measure.
- **The X13 itself**: active-min **−24 dB**, 10 active bands, slope 16 — far more
  aggressive than the dev device; near the corpus median-to-aggressive band.
- **High boost (+8/+9 dB)** concentrates in `voice`, which usually has no active
  band-limiting → the worst boost×aggressiveness overlap is limited; the risk population
  is `dynamic`/`movie`/`game` at +8 dB over an aggressive regulator.
- **Same X13 subsys (`17AA2344`) ships two tunings in the wild**: the device-specific
  package (`tuning_version=24`, active −24/−19 regulator — what we analyzed) and a
  generic `dax3_ext_rtk` copy (`tuning_version=1`, all-zero/inert regulator). Input-gain
  only does anything when the regulator is active.

**Decision.** **Default is now `input-gain`** (flipped 2026-06-22). The original ship
kept `output-gain` default with `input-gain` as a documented opt-in, because one
best-case-device measurement (dev device, 0 dB give-back) didn't clear the ≥2-device bar
and the corpus analysis above flagged a real loudness/pumping risk on aggressive
regulators. **Issue [#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23)'s X13 reporter closed that gap.** On their device — active-min
−24 dB, *more* aggressive than the dev device and than 91–94% of FOCUS profiles — they
confirmed `input-gain` removes the distortion ("in most cases I don't notice distortions
anymore") while staying loud and uncompressed ("loud enough and definitely not
overcompressed"). That is the aggressive-regulator, clean-*and*-loud confirmation the
promotion gate required: dev device (best case, clean either way) + X13 (aggressive case,
the risk population) = the ≥2-device win. `output-gain` survives as `--volmax-slot
output-gain`, an opt-out for A/B or for recovering loudness if a device's regulator
over-tames the bass.

Residual to keep honest: the reporter said "in most cases" — `input-gain` substantially
reduces but may not 100% eliminate distortion on the most extreme content; it is strictly
better than `output-gain` there, and `--disable volmax` remains the full escape hatch.
The reporter runs their own filter-chain converter (output "similar" to `ee_to_pipewire`),
so the confirmation is on the PipeWire-backend staging, not EE. This does not touch
XML-only-derivability: both slots emit the same XML-derived +6 dB — only the chain
position differs, and neither position was Dolby-derived to begin with.

**Inert-regulator caveat (2026-07-21, issue [#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27) follow-up).** The Galaxy Book6 Ultra
field report hit the corner the corpus note above predicted ("input-gain only does
anything when the regulator is active"): its tuning's `threshold_high` is flat 0 dB,
so `make_regulator` disables every band and *both* slots degenerate to the same
untamed feed into the brickwall — the taming rationale doesn't apply at all, and the
+6 dB boost is pure brickwall drive on loud content (reported as "degrades the sound
dramatically", though confounded with bass-enhancer and regulator disables in the
same run; cross-device-findings §15 addendum). The generator now prints a heads-up
when volmax rides a regulator whose bands are all threshold ≥ 0 dB, pointing at
`--disable volmax`. The corner is common, not exotic — roughly 1 in 7 default runs
corpus-wide, half the corpus counting voice profiles (prevalence sweep and
methodology in the §15 addendum).

### Why the PEQ `output-gain` stays a single global `max(L,R)` (not per-channel)

Every corpus file is an `internal_speaker` per-speaker acoustic correction, and the
L and R curves legitimately differ where the two physical speakers do. The 2483-XML
re-derivation surfaced this as real per-channel divergence in the *peak* boost
(`corpus_audit`'s L/R peak-asymmetry tally, 2026-06-17): **131 profiles / 10 devices**,
all on Lenovo convertible/AIO SKUs (ALC257/287), never the symmetric clamshells. It
splits cleanly: ~119 are matched-filter ~1 dB gain trims (median 1.0 dB — ordinary
per-speaker HF correction), and 12 are *structural* 7 dB cases in convertible `stand`
pose on `voice_onlinecourse`, where one speaker is high-passed and the other gets a
+15 dB low-mid bell (a per-orientation correction that only appears because the
expanded cohort added pose-aware tunings).

The converter's anti-clipping trim (`make_peq_eq`) negates the **global `max(L,R)`**
effective boost into the equalizer's single `output-gain`. That is the correct — and
only representable — choice: EE's equalizer has per-channel `left`/`right` bands but
just one `output-gain`. Applying `max(L,R)` equally to both channels shifts them
together, preserving the Dolby-tuned L/R relationship at every frequency (including
the 7 dB worst case). A *per-channel* trim (e.g. `-6 dB` L / `-2 dB` R) would impose a
broadband L-vs-R level tilt Dolby never intended — corrupting the stereo image — and
isn't expressible as one `output-gain` anyway. The only cost of global-max is extra
headroom on the quieter channel, which the downstream leveler restores. Follow-up #2
is therefore resolved as *no code change*; the regression test
`test_peq_output_gain_uses_global_max_across_asymmetric_channels` locks it in.

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
| ~~stereo_tools#0~~ | — | (not emitted) | — | **Removed 2026-06-13.** The converter no longer emits a stereo widener; `surround-boost` is not mapped (entry 2). `ee_to_pipewire.py` keeps `emit_stereo_tools` as a translator for any preset that still carries the block. |
| equalizer#0 | `mode` | `"IIR"` | AUDIBLE | Biquad realisation of the per-band PEQ. Alternatives FIR / FFT / SPM. FFT mode would reproduce the band targets exactly at every FFT bin instead of analytically (open: candidate test). |
| equalizer#0 | `q-mode` | (none) | AUDIBLE | Resolved (2026-06): there is no separate q-mode key in the EE 8.x equalizer schema we emit — the Q convention is a property of the per-band filter family (`mode`). The convention question lives in the row below. |
| equalizer#0 | per-band `mode` | `"RLC (BT)"` | AUDIBLE | Filter family. Verified for HP-slope behavior (commit `944a8f3`). **Bell-width convention quantified from LSP source** ([Filter.cpp], `FLT_BT_RLC_BELL` vs `FLT_DR_APO_PEAKING`): `APO (DR)` is exactly the RBJ-cookbook biquad (`α=sin(ω0)/2Q`, reciprocal `√gain` scaling); `RLC (BT)` uses a different prototype (`kt = 2√(1+g²)/(1+2Q)`) — identical peak gain, wider bell at q>1. On the dev-device bells: realized half-gain Q 3.43 for q=4.6 (≈25% wide), 1.72 for q=2.0, 1.35 for q=1.5; max in-band deviation vs cookbook 0.58 dB (q=4.6), ≤0.23 dB (q≤2). Whether Dolby's `q` is cookbook-convention is undecided — measured across two DAX sessions: fitting the RLC−RBJ signature to the EE−DAX pink residual (150–800 Hz) gives a≈0.78/0.76/0.95 (2026-06) and a≈0.71/0.71/0.90 (2026-06-13 Windows session) on `dynamic`/`movie`/`game` — consistent across sessions and leaning cookbook — but the signature (0.23 dB rms) explains only ~2% of the ~1.1 dB voicing residual; a stepped-tone check is *confounded* (DAX's leveler adapts per held tone, ±3 dB ≫ the 0.43 dB bell signature). The convention delta is smaller than this device's content-adaptive variability, so settling it likely needs a device with higher-Q / higher-gain bells. Candidate fix if cookbook is ever confirmed: emit bells as `APO (DR)` (HP stays `RLC (BT)`, verified); second-device bar applies. Note `compare_ee_analytical.py` models bells as RBJ — i.e. the offline model and the live plugin disagree by up to the 0.58 dB above; part of the vsXML baseline, not a DAX-side effect. |
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
not implementation drift. (At the time of this measurement it was
attributed, per Findings 6/7, to fixed DAX-internal behavior outside
the published XML; Finding 9 later showed it was dominated by the
converter's own `ieq-amount` scaling error, since fixed — the
remaining EE-vs-DAX residual is ~1 dB RMS at HF plus the LF/leveler
gap.)

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

Field confirmation: issue [#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25) (ThinkPad E14 Gen 2 AMD, HDA) — with autogain
manually enabled, the reporter heard crackle exactly and only on short system
event sounds arriving after silence, i.e. reason 2's quiet→loud case; the
mitigation is raising `silence-threshold` toward the −50 dB the conservative
SoundWire path ships. Since the 2026-07 flip attempt below, the HDA block
also stores −50 dB (it previously kept EE's −70 dB plugin default — never a
schema hypothesis, just "keep the plugin default"), so enabling by flag or
GUI now gets the fix without hand-editing.

Field note: issue [#36](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/36) (Lenovo IdeaPad Pro 5 14IMH9, HDA) — the reporter
recommends manually enabling autogain on this device; no further detail
(root cause, specific content) captured yet, so this isn't a second
confirmation of the issue [#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25) mitigation, just a pointer for whoever
investigates next.

### The 2026-07 default-flip attempt: measured, then rejected (issue [#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25))

After the #25 reporter confirmed enable + −50 dB gate ≈ Windows loudness on
the E14 (2026-07-27, no crackle re-report), we attempted to flip the HDA
default to enabled and gated adoption on an on-device listening pass
(ThinkPad X1 Yoga Gen 7, ALC287 HDA — the device whose artifacts motivated
the original bypass). Null-sink captures of the full chain:

- **The gate fix is real.** Over a 30 s −60 dBFS noise floor (between the
  two gates), the leveler wound up **+41.8 dB** at gate −70 vs **+1.7 dB**
  at −50. A −6 dBFS notification burst after the floor came out pinned at
  the −1 dBFS brickwall ceiling under −70 (heavy transient limiting = the
  #25 crackle) vs ~4 dB below ceiling under −50. No digital clipping in
  either case — reconfirming the "quality, not safety" verdict below.
- **The loudness win is real too:** ~+9 dB on −20 dBFS RMS program material
  vs bypassed.
- **But reason 2 has a second case the gate cannot fix.** On a
  speech-pattern stimulus (−38 dBFS background + intermittent −18 dBFS
  speech bursts), the leveler boosts the *legitimate* quiet background
  (above any sane silence gate) by ~14 dB, and each voice onset rides
  ~4 dB of overshoot into the MBC/regulator. `maximum-history` is **not a
  reaction-speed lever**: 20/32/40 s all measured 3.9–4.2 dB overshoot,
  because the Geometric Mean (MSI) reference reacts through its Momentary
  component regardless of history. Dropping the target to −23 traded ~3 dB
  of loudness for no overshoot improvement (5.7 dB onset-vs-tail delta).
- **Listening verdict (adoption gate): rejected.** On real content
  (stream with low background + intermittent loud speech) the overshoot is
  audible saturation on the loud onsets — the same artifact class that
  motivated the original bypass, now with numbers.

Shipped instead: the HDA block stores `silence-threshold: −50` even while
bypassed, and `--enable autogain` activates the leveler for users who want
the loudness (the E14 case) without GUI edits. The conservative SoundWire
branch is unchanged. Net: the crackle failure mode is fixed for everyone
who enables the leveler; the quiet-background overshoot is structural to
EE's non-content-aware autogain and keeps the HDA default bypassed.

The measurement protocol is committed as
`tools/measure_ee/autogain_dynamics.py` (crackle + speech protocols,
self-generated preset variants) — rerun it before changing any autogain
default or when EE/LSP leveler behaviour changes upstream.

## Translating active autogain to LSP `autogain_stereo` (PW converter)

The "no LV2 equivalent" rationale that kept `ee_to_pipewire.py` from translating
a non-bypassed autogain was stale. LSP's `autogain_stereo` is a K-weighted (LUFS)
loudness AGC — the same EBU R 128 weighting EE's native libebur128 autogain uses
— so the volume leveler *can* be reproduced in the PW filter-chain. Active
autogain is emitted only on SoundWire devices (HDA bypasses it — see "Why
autogain is bypassed by default" above), so this closes a gap that only ever
affected the SoundWire PW path; the bypassed-HDA case is still skipped silently.

**Port mapping** (`emit_autogain`; the EE block comes from `make_autogain`'s
conservative path):

| EE autogain field | `autogain_stereo` port | Value |
|---|---|---|
| `target` (LUFS) | `level` | direct, clamped [−60, 0] |
| `silence-threshold` (dB) | `silence` | direct, clamped [−84, −36] |
| EBU R 128 weighting | `weight` | `5` (K-weighted) |
| (latency constraint) | `lkahead` | `0.0` |
| `maximum-history` (s) | `tfall_l` (ms) — gain *down* | `maximum-history · 200 ms/s`, clamped [10, 10000] |
| `maximum-history` (s) | `tgrow_l` (ms) — gain *up* | `maximum-history · 500 ms/s`, clamped [10, 10000] |

`level`/`silence` are dB-domain ports passed **directly** — not linear gains, so
no `db_to_lin` (the easy bug; contrast the limiter's `th`). EE
`input-gain`/`output-gain` are always 0.0 and have no main-path port (`preamp`
is sidechain-only), so they're structurally identity and not written.

**Zero added latency.** `autogain_stereo` reports latency only through its
lookahead; `lkahead=0` keeps it at zero over the PipeWire quantum (the hard
constraint). `validate_conf.py`/lv2info confirms all six emitted controls are in
range.

**The `maximum-history`→ride-time scaling, and why it's asymmetric.** EE's
`maximum-history` is a libebur128 *integration window* in seconds (15–40 s on the
active SoundWire path; unvalidated-scaling entry 7); `autogain_stereo` has no
equivalent window port (`lperiod` caps at 2 s), so a longer EE history is mapped
onto a slower gain ride via the gain time-constants `tgrow_l`/`tfall_l`
(longer = gentler; monotonic). The on-device proof (below) showed EE's leveler is
**asymmetric** — it attenuates loud content quickly but boosts quiet content very
slowly (anti-pumping) — so the two directions get different global scales:
`tfall_l = maximum-history · 200 ms/s` (gain down) and `tgrow_l =
maximum-history · 500 ms/s` (gain up), both a single device-independent transfer
(so XML-derivable, no per-device tuning), clamped to the [10, 10000] ms port range.

**On-device proof (2026-06-22, EE-vs-PW, X1 Yoga ALC287 HDA rig).** Method: an
*autogain-only* preset (every other stage stripped from `plugins_order`, so only
the leveler acts — no convolver/MBC/limiter confounds), `target=-22 LUFS`,
`maximum-history=20 s`, played as a loud(-16)→quiet(-34)→loud(-16)→silence pink
battery through both the live EE chain and the PW filter-chain rendering of the
*same* preset, captured via the `tools/measure_ee` + `tools/measure_pw`
null-sink route, compared as output integrated-LUFS / RMS-envelope trajectories
(`localresearch/measure_ee/autogain_proof/`). Results:

- **Loudness target — exact.** Both chains settled the loud segments to **−22.00
  LUFS** (= target) and matched each other to **0.2 dB** RMS. Validates `level`.
- **Silence gate — identical.** Both fully gated silence (−199.9 dBFS, no
  noise-floor boost). Validates `silence`.
- **Attenuation — matched.** Loud-content gain reduction agreed (~−8.5 dB, 0.2 dB
  apart) at `tfall_l`=4 s (history 20 s · 200 ms/s).
- **Boost — close, port-limited.** The 12 s quiet segment: EE boosted +2.1 dB, PW
  +3.5 dB after splitting grow from fall and pushing `tgrow_l` to its 10 s ceiling
  (symmetric 4 s grow had given +9.9 dB). Residual EE−PW ≈ 1.4 dB (0.4 LUFS on the
  settled tail), erring slightly *faster*: LSP's 10 s `tgrow_l` ceiling cannot
  reach EE's ~50 s effective grow, a bounded, documented divergence.
- **Zero added latency — confirmed.** EE and PW capture onsets were identical
  (0.30 s); the autogain node adds no latency over the quantum (`lkahead=0`,
  also lv2info-confirmed).

**Verdict: adopt, on by default.** The leveler is faithfully reproduced on the two
properties that determine loudness delivery (target convergence, silence gating)
and on attenuation dynamics; the only residual is a ≤1.4 dB faster boost on
sustained quiet content, capped by the LSP grow-time ceiling. Related EE-side
autogain scalings: unvalidated-scaling entries 7 (window formula) and 10
(conservative offsets).

**Full-chain clipping check (2026-06-22) — does this re-introduce the HDA
distortion?** A second EE-vs-PW capture on the *full* HDA chain (steep IEQ+AO
convolver → … → autogain → MBC → regulator → limiter@−1 dBFS) with autogain
**force-enabled** — recreating the exact scenario that motivated the HDA bypass
("Why autogain is bypassed by default" above). Stimulus: 6 s settle → 18 s
deep-quiet (−45 dBFS, gain ramps up) → hard loud onset (−8 dBFS, leftover gain
overshoots). Findings:

- **No clipping on either chain.** Both EE and PW: zero full-scale samples, peak
  pinned at −1.00 dBFS. The brickwall limiter holds the output regardless of
  autogain. So the HDA-bypass motivation is **loudness pumping** (the leveler
  over-boosts quiet content, the onset then slams the limiter/MBC), **not digital
  clipping** — refining the hedged "saturation/pumping" wording above.
- **PW does not worsen it; it's gentler.** On the loud-onset transient EE≈PW
  (both −1.00 peak, ~0 % ceiling-pinned, crest 14.8 vs 14.9 dB). On the deep-quiet
  boost PW applied *less* gain than EE (+12.5 vs +18.4 dB) — the **reverse** of the
  isolated near-target result. Cause: LSP autogain's `drift` dead-band (12 dB
  default) stops correcting within ~12 dB of target, so on very-quiet content
  (>12 dB below target) PW under-boosts vs EE's full correction. Less boost → less
  leftover gain → less overshoot, so PW is no harsher than EE in the worst case.
- **Non-monotonic vs EE:** PW≈EE near target (isolated test), PW<EE far below it
  (drift dead-band). Bounded both ways.

Net: translating autogain adds no clip risk, and the HDA default-bypass stays a
*quality* (anti-pumping) choice, not a safety one — and is untouched by this work,
which only translates the already-active SoundWire case (quiet typically near
target, where the drift dead-band barely bites). The `drift`/`max_amp` knobs
(left at LSP defaults) are the levers if PW's deep-quiet tracking is ever revisited.
Captures/driver: `localresearch/measure_ee/autogain_fullchain/`.

## Verified math (sanity checks)

The sanity-checks behind the values catalogued in [reference.md](reference.md) —
the derivations and accuracy measurements they rest on:

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
audio-optimizer impulse response is **exact on its 4096-point design grid**
(error < 1e-6 dB — though design and evaluation share that grid, so the number
is circular by construction). Evaluated at the exact 20 Dolby band-center
frequencies the error is **≤ 0.06 dB**, all of it FFT-bin quantization (e.g.
the 47 Hz center snaps to the 46.875 Hz bin on a steep per-band slope) —
consistent with Finding 9's ~0.06 dB figure. The FIR is properly minimum-phase (100% of
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

Issue [#11](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/11) raised an interesting side question: how does the FIR our converter
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
matches the development tuning XML, `DEV_0287` keyed `SUBSYS_17AA22E6`).
Findings 1–9 are from that device; a second device's battery (Yoga Slim 7
14ARE05, simplified schema) arrived 2026-07-30 via issue [#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44) — Finding 10.

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
quiet sweep, not a real phase shift. (The same caveat applies, in lesser
degree, to the absolute ratios: Finding 1 shows these sweeps carry
time-varying gain, which Farina deconvolution folds into pre-/post-peak
energy, biasing the ratio toward linear-phase-looking. The multitone
capture's per-band phase is the cleaner signal if this characterization
ever becomes load-bearing; no converter decision rests on it — min-phase
is forced by the latency constraint regardless.)

This rules out our generated FIR matching DAX3's exact phase behaviour
in any profile. Per the no-added-latency constraint we don't switch our
converter to linear-phase regardless of this finding — minimum-phase is
the right trade-off for an EQ correction filter, and we accept that this
diverges from Dolby's choice.

### Finding 3: DAX3 doesn't faithfully implement the published XML curves

> **Superseded in part by Finding 9.** These captures predate the
> `ieq-amount`/100 correction, and the hypothesis list below omitted the
> branch that later proved true — a field we *do* read but mis-scaled.
> The HF residual described here was largely the converter's own
> interpretation error, not a DAX-side stage.

Each profile's captured spectrum vs **its own** balanced FIR target,
between-band magnitude residual on a 200-point log grid (47–19688 Hz):

| profile | sweep | sweep_quiet | pink | pink_quiet |
|---------|-------|-------------|------|-----------:|
| dynamic | 9.2 / 31.4 dB | 6.9 / 24.2 | 7.2 / 27.1 | 7.5 / 25.9 |
| movie   | 11.9 / 37.2 | 7.3 / 25.4 | 7.5 / 28.1 | 7.6 / 26.3 |
| music   | **7.3 / 21.6** | **5.1 / 19.6** | 5.9 / 20.4 | 6.5 / 20.4 |
| game    | 11.8 / 37.2 | 7.3 / 24.6 | 7.5 / 28.1 | 7.8 / 26.7 |
| voice   | 9.8 / 30.8 | 9.6 / 30.2 | 9.6 / 31.8 | 9.9 / 33.1 |

(`RMS / max` in dB; captured spectrum minus our FIR's frequency response.
Caveat: the reference is the IEQ + AO FIR only — it excludes the XML's own
speaker-PEQ block (a 100 Hz 4th-order HP plus ±3–4 dB bells at 280/400/516 Hz),
which DAX presumably also applies. Below ~100 Hz the XML itself mandates up
to ~26 dB of deviation from this reference, so part of the RMS/max figures at
the 47 Hz end of the grid is expected by construction. The HF conclusions are
unaffected — the speaker-PEQ has no content above 516 Hz.)

For comparison, the synthetic LTI test (apply our FIR to the stimulus,
deconvolve, compare to original) recovers within **0.06 dB RMS / 0.36 dB
max** — two orders of magnitude tighter. The captured DAX3 response is
genuinely far from what our FIR predicts, not a measurement artifact.

The bulk of the residual sits at HF (>5 kHz). At 19688 Hz the captured
magnitude is typically 20–40 dB above what the XML's combined IEQ + AO
target predicts. **DAX3 does not apply the deep HF rolloff that the
published XML implies.** This is the most actionable finding — it
suggests either (a) DAX3 ships a separate HF-shaping stage we're not
modelling, (b) the audio_optimizer block is a target-response curve that
DAX3 inverts internally rather than applying directly, or (c) the
specific IEQ "Balanced" curve in Dolby Access doesn't correspond to the
`ieq_balanced` block in the XML. Finding 4 (below) shows (c) cannot
explain the gap; disambiguating (a) vs (b) still needs either a
Dolby-side reference or a stripped-down single-block tuning XML.

The Music profile fits its XML target most closely (RMS 5–7 dB).
Dynamic, Movie, Game cluster around 7–12 dB RMS. Voice deviates the
most (9–10 dB RMS).

### Finding 4: EE-on-Linux follows the XML; the gap is on DAX's side

> **Superseded in part by Finding 9.** The EE column below was captured
> under the old `ieq-amount`/10 scaling; Finding 9's /100 correction — an
> XML-only fix — later collapsed the 19.7 kHz residual from −28 dB to
> −1.5 dB. "The gap is on DAX's side" did not survive: the dominant term
> was ours.

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
in `ieq_balanced + audio_optimizer`): at 19.7 kHz the combined target
under the then-current scaling predicts ≈ −26 dB **relative to 1 kHz**,
EE applies −27.5 dB — the FIR realises the target — and DAX applies
+0.7 dB. (An earlier revision quoted the target as "−43 dB, which the
FIR doesn't reach"; that figure was normalized to the curve peak at
234 Hz while this table is normalized at 1 kHz — a mixed-normalization
slip that manufactured a 16 dB FIR shortfall which doesn't exist.)

Hypothesis (c) from Finding 3 (the wrong `ieq_*` curve) cannot explain
the gap — though not for the reason an earlier revision gave ("our
converter and EE agree on which curve is in play": EE applies whatever
curve the converter chose, so their agreement is exactly the `vsXML`
circularity Finding 7's note on metrics warns about, and no Linux-side
capture can show which curve Dolby Access selects). The sound argument
is curve similarity: all three `ieq_*` curves in this XML carry the
same deep HF rolloff (−37…−43 dB at 19688 Hz rel-peak under the
then-current scaling: balanced −43.3, warm −40.6, detailed −37.3), so
no curve swap could close a ~28 dB HF residual. The remaining hypotheses are (a) DAX
ships a separate HF-shaping stage we're not modeling, or (b) DAX
treats `audio_optimizer` as a target-response that it inverts before
applying. Loopback can't distinguish them without a controlled
single-block A/B (e.g., a tuning XML stripped down to a single block
at a time). At the time this read as a DAX-side gap; per Finding 9 the
dominant term was the converter's own `ieq-amount` scaling.

The 47 Hz deviation (−8 dB EE vs −28 dB DAX, both relative to 1 kHz)
is partly the EE chain's `equalizer#0 band0` HP at 100 Hz / x2 slope
(≈4th-order rolloff that takes us deeper than the XML target alone)
and partly DAX's volume regulator boosting LF tones at low input
levels — the multitone capture, where the leveler can lock onto a
single 47 Hz sine for 12 s, shows DAX at −14 dB (vs EE −37 dB), a
23 dB gap that's much bigger than the pink-noise gap and consistent
with leveler boost rather than steady-state EQ.

### Finding 5: No HF-shaping XML block was missed

> **Superseded in part by Finding 9.** "Cannot be falsified without data
> outside the XML" did not survive: the decisive fix (`ieq-amount` read
> as a percentage) was XML-only. The schema audit below remains valid —
> no skipped element carries HF data — but the gap it was trying to
> explain was largely converter-side.

A schema audit of the corpus XMLs (every element appearing under
`tuning-cp` and `tuning-vlldp` across the local corpus of device
XMLs, checked against what `parse_xml` reads) found **no candidate
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

> **Superseded in part by Finding 9.** The "fixed DAX-side HF behavior"
> conclusion below was falsified: the profile-independent HF residual was
> the converter mis-scaling the *shared* `ieq_balanced` component
> (`ieq-amount` read as /10 instead of /100) — a candidate this finding's
> own data hints at (the music-profile parenthetical below shows the
> residual tracking IEQ curve content) but which never made the hypothesis
> list. The variant-matrix rejection of hypothesis (b) — the AO sign —
> stands and remains load-bearing.

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
This was read at the time as the canonical signature of a **fixed** HF
behavior on DAX's side not parameterised in the published tuning XML —
"the only remaining explanation". That inference had a gap: a mis-scaled
*shared* curve component produces the same profile-independent signature,
and the music parenthetical above (residual tracking the IEQ curve
content) pointed that way. Finding 9 confirmed it: the residual was
dominated by the converter's own `ieq-amount` scaling.

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

Our `make_fir` produces a faithful min-phase FIR of `IEQ + audio_optimizer` —
exact on its design grid, ≤0.06 dB at the exact band centers (bin
quantization) — the math is correct. What we cannot reproduce on Linux
without additional reverse-engineering:

1. **DAX3's hybrid-phase character.** Out of scope: linear-phase costs
   ~42 ms of group delay, ruled out by the no-added-latency constraint.
2. **DAX3's apparent flatter HF response — since closed by Finding 9.**
   Findings 5/6 narrowed this to "a fixed DAX-internal stage outside
   the XML" (after rejecting the AO-sign hypothesis (b) via the variant
   matrix); Finding 9 then showed the dominant term was the converter's
   own `ieq-amount` scaling — an XML-only fix that took the EE−DAX HF
   residual from ~12 dB to ~1 dB RMS. What genuinely remains outside
   the XML: that last ~1 dB at HF, and the LF/leveler gap (Finding 9's
   residual table).
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

(Reproduce: a per-variant spec TSV driving `tools/measure_ee/capture_battery.py`
plus a converter patched to re-introduce the four flags. Use unique per-variant
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
Finding 6 saw with the AO-sign and phase variants, and it was
read as consistent with Finding 6's conclusion: the residual is
dominated by DAX-internal behavior outside the published XML
(a fixed HF voicing + the leveler), not a single wrong XML
rule on our side. (Superseded: Finding 9 found exactly that —
a single wrong XML rule on our side, `ieq-amount` read /10
instead of /100. β above was the near-miss: right mechanism,
wrong field semantics.)

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

Per-variant captures were retained in the local (gitignored) research
area; note they predate Finding 9's scaling fix, so per the capture-validity
rule they are stale for any new EE↔DAX comparison — re-capture instead.

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

(Reproduce: a per-profile spec TSV + the temporary
`--ieq-amount-as-cap` flag, then overlay the DAX vs baseline vs β
pink-noise spectra across all five profiles.)

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

**Updated stance on β.** (Calibration, added in review: the ≤0.1 dB
cross-profile consistency is close to guaranteed by construction — β
perturbs only the shared `ieq_balanced` component, so the EE-side
delta is identical per profile, and Finding 6 had already shown the
baseline residual is profile-independent. The genuinely new
information in this table is the two sign-crossing regressions and a
re-confirmation of capture repeatability, not independent evidence
for the cap reading.) The single-profile result was already
the cleanest of the five hypotheses; the cross-profile result
confirms the improvement is *structural*, not coincidental. We
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
(Follow-ups item 5 below) surfaced an unrelated finding on the DAX side. The
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
that closed the regulator-stress investigation (Follow-ups item 5).

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
issue [#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14) for the open follow-up.

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

The flag was reverted from the converter's CLI surface, along with the
band-bounds plumbing it used. (An earlier revision of this note claimed
`make_bass_enhancer` retains a `src_freqs` parameter — it does not, and
never did in a committed revision; the current signature is
`(hp_freq, amount)`.) A future architectural revisit shipping VBE-on-HDA
via a different plugin/topology would need to re-derive the band bounds
from the XML's `bass-enhancer-*` fields.

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
Calf Saturator additionally leaks 2nd-harmonic ≈15 dB stronger than
DAX's profile at 50 Hz (3rd-vs-2nd ratio +11 dB for Saturator,
+26 dB for DAX, with comparable 3rd-harmonic levels) — Calf's
distortion model is not purely symmetric.

Even harder drive (`drive=8`) and tighter post-band (`lp_post=600`)
amplify the harmonic complex but don't change the relative ratios.

**Cascading LSP filters + Saturator escapes the single-plugin ceiling.**
A follow-up PoC chained two LSP `filter_stereo` stages in front of
Calf Saturator (BWC mode at slope `x16` ≈192 dB/oct on input edges,
`x8` ≈96 dB/oct on output edges) for a true brick-wall band-pass at
[35, 160] Hz, with Calf Saturator's internal pre/post filters disabled
to avoid double-filtering. Final output Δ3 (3rd harmonic vs
fundamental) at 180 Hz: **−56 dB** — vs Calf BassEnhancer's −18 dB
ceiling, a 38 dB improvement, and within 18 dB of DAX's −74 dB clean
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
decision rather than a measurement one. Issue [#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14) is left open as the
canonical reference for the gap; no converter change ships from this
investigation. A follow-up that extends `ee_to_pipewire.py` to inject
the LSP-cascade-plus-Saturator chain when the XML carries
`virtual-bass-mode=0` and `is_soundwire=False` is the natural next
step if a listening test confirms the captured improvement is
audible.

**Where this sits after the 2026-06 PW-path review.** The converter is
kept a faithful 1:1 translation — the PW conf reproduces the EE preset
and nothing more (the `tools/measure_pw/` equivalence is the contract)
— so closing the VBE gap is a deliberate-divergence decision, not a
measurement one. Two shapes if ever taken: (a) *cheap, both paths* —
enable Calf BassEnhancer on HDA in `make_preset` (today SoundWire-only);
it keeps EE and PW equal and matches DAX within ~5 dB at 50/80 Hz (the
bass-burst table above), but it over-synthesises at 180 Hz where DAX is
clean (the −18 dB regression) and leaks 2nd harmonics where DAX is
odd-dominated — a decent-but-imperfect approximation, not a faithful
one; (b) *PW-only, more selective* — the LSP-cascade + Saturator
injection above, which suppresses the 180 Hz over-synthesis BassEnhancer
can't, and is expressible only in the PW path. Both stay deferred.

### Finding 9: The IEQ is over-applied — `ieq-amount` reads as a percentage, and that closes the HF gap (issue [#13](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/13))

Issue [#13](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/13) (taprobane99) opened arguing our 20-band → FIR construction was
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
temporary scaffolding (since reverted — see Status below).

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

### Finding 10: simplified-schema AO units confirmed on a second device (issue [#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44))

The issue [#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44) reporter (Yoga Slim 7 14ARE05, Realtek ALC287,
`SUBSYS_17AA380D`) ran the **full** `tools/measure_dax/` battery on Windows — all
four stimulus kinds, loud and quiet variants, Dolby off vs profile `dynamic` —
giving us the first DAX captures from a second device, and the first from a
*simplified-schema* XML (`gain_l`/`gain_r` audio-optimizer, no PEQ/MBC; the
convolver is the entire static correction there). All numbers below were
re-derived from the capture set this session (`analyze.py` spectra +
band-mean deltas; both curves referenced at the 234 Hz band, the AO curve's
0 dB point).

**The 1/16-dB unit hypothesis is confirmed.** The pink steady-state
dynamic−off delta tracks the converter's predicted IEQ+AO curve (profile
`dynamic`, curve `balanced`) with mean |error| 0.72 dB (L) / 0.73 dB (R),
median 0.52 dB, worst 2.11 dB at the 19.7 kHz edge band (pink SNR +
smoothing limit). The 2250–5813 Hz bands match to ≤0.1 dB — tight enough to
pin the unit scale: a 1/8-dB reading would miss those bands by their full
2.2–3.5 dB depth, a 1/32-dB reading by half of it — both ≫ the ≤0.1 dB
observed. This also generalises the `/16` convention (verified on the
full schema via issue [#15](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/15)'s settings-file experiment) to the simplified
variant's differently-named arrays.

**The per-channel `gain_l`/`gain_r` assignment is confirmed within a single
capture pair.** The one band where this XML's L/R arrays differ — 1688 Hz,
1.0 dB apart — shows a measured L−R delta difference of 0.8 dB in the same
direction (L −8.28 dB vs R −9.08 dB re 234 Hz).

![Measured Dolby on−off delta vs the converter's predicted curve, both
channels — the curves overlay within a fraction of a dB, splitting L/R only
at the 1688 Hz notch](images/finding10-measured-vs-predicted.png)

**Dolby-off is a true bypass, and loopback taps post-APO.** The off-state
stepped and multitone captures are flat to −0.05 dB with zero cross-pass
adaptive span — so an off/on pair is a clean A/B, and the capture method
needs no correction for the off leg.

**Non-LTI behaviour reproduces on device 2** (consistent with Finding 1):

- The sweep-derived delta is corrupted by the leveler's time-varying gain
  (mean band error 4.5 dB vs the pink-derived curve, +10 dB apparent gain at
  234 Hz vs +4 dB steady-state) — sweeps stay unusable for EQ extraction
  through DAX.
- Multitone shows an extra 3–5 dB of compression above ~850 Hz relative to
  the static curve; the stepped battery's cross-pass adaptive span reaches
  5.8 dB. Tonal stimuli drive the multiband dynamics hard; pink remains the
  EQ-shape reference.
- Level dependence: the quiet-pink delta realises only ~70% of the curve
  depth mid-band (a uniform ~+2 dB shallowing) plus an extra ~5 dB cut at
  47 Hz — level-adaptive bass management. Our static FIR reproduces the
  loud/nominal operating point, which is the right anchor.

![The measured curve at normal vs quiet input level against the static
prediction — the quiet curve is uniformly shallower mid-band and cuts deep
bass harder](images/finding10-level-dependence.png)

**Leveler magnitude, measured on this device:** broadband pink RMS delta
(dynamic − off) is **+8.2 dB** at the loud level and **+21.8 dB** at the
quiet level. Two implications: our XML-derived volmax `input-gain` (+7.0 dB
on this XML) lands within ~1 dB of DAX's loud-level makeup — retroactive
support for the issue [#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23) slot default — and the quiet-content gap our
bypassed-by-default autogain leaves is an order of magnitude larger than the
static-EQ residuals, i.e. the leveler dominates any remaining
"Windows sounds louder/fuller" impression (issue [#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25)'s conclusion, now
quantified on a second device).

**Open question — adaptive activity with no decoded mechanism.** This
XML's `threshold_high` decodes to +0.0 dB (never engages, per our mapping)
on bands 10 and 13–20 (1688 Hz, 3750–19688 Hz), yet the stepped captures
show up to ~5 dB of cross-pass adaptive span on several of them (5.1 dB at
3750 Hz; 4.3–5.3 dB at probes owned by bands 14, 17 and 19) — bands where
the decoded parameters provide no mechanism at all. The span is
frequency-selective (near zero at 469–656 Hz; concentrated at 1.9–4.2 kHz
plus spots near 8 and 12.5 kHz), so it is multiband dynamics, not broadband
leveler drift — most plausibly the Media-Intelligence layer (this profile
enables five `mi-*-steering` flags) or another engine-side adaptive block
we classify as non-modelable. Notably the span does **not** pattern on
either decoded parameter: the *strongest* span on the device (5.0–5.8 dB)
sits in band 11 (2250 Hz), which is threshold-active and `isolated_band=1`,
while the inert `isolated_band=0` band 10 (1688 Hz) spans only 1.6–3.2 dB —
so the previously-unread `isolated_band` array, despite real per-device
contrast corpus-wide (59 distinct patterns; exact threshold-activity mirror
on 18,369 profiles, ≥1-band divergence on 11,548 — ad-hoc scan 2026-07-30,
2,741 XMLs), is evidence-wise *not* the gate for this behaviour, and its
semantics stay unknown (entry 11 (f)). Relates to the regulator
under-engagement thread (entries 6/11 below); no converter change
indicated — chasing a content-adaptive layer with a static chain is the
same trade rejected in Finding 6.

**Verdict:** the simplified-schema static mapping is validated end-to-end at
the loud operating point; the measured residual vs Windows on this device is
Dolby's adaptive layer (leveler + HF dynamics), not the EQ. No converter
change indicated.

### Unvalidated converter scaling factors (the `ieq-amount` class)

Finding 9 corrected a scaling *interpretation*, not an arithmetic slip:
`ieq-amount` was read as `amount/10` when the field is a percentage,
`amount/100`. The lesson generalises. The converter carries a cluster of
scaling factors that map an XML field onto a filter parameter through a
constant we *invented* rather than confirmed. The `/16`-dB convention is the
one such constant we have actually verified (issue [#15](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/15): a user set ±12 dB in
DAX on Windows and the settings file stored ±192). The rest below are adopted
defaults that ship in the audible path but have never been individually checked
against a DAX capture. They are catalogued here as a class so a capture
campaign can attack them deliberately. This is distinct from the "Follow-ups"
list further down, which tracks ideas we considered and did *not* adopt — these
are live, shipping defaults.

| # | Factor (`dolby_to_easyeffects.py`) | XML field | Why it's a guess | Path status | What would falsify it |
|---|---|---|---|---|---|
| 1 | Dialog-enhancer gain ceiling: `amount/16 * 6.0` dB, bell centered 2.5 kHz, Q≈0.7 (`make_dialog_enhancer`) — the SoundWire-only `* 8.0` dB variant + 4 kHz clarity bell at `*0.6` was **REMOVED 2026-07-03** (see end of this row) | `dialog-enhancer-amount` (0–16) | the XML gives only an amount; the dB ceiling, center and Q are converter-chosen — nothing in the schema says "6 dB" | **default audible** when `dialog-enhancer-enable=1` (X1 Yoga: `dynamic`/`movie` amount=5, `voice` amount=3; off on `music`/`game`) | a **pink-noise** pre-screen is null/confounded (see roadmap): the cleanest DE contrast (`movie` amount=5 vs `game` amount=0, identical IEQ+AO target) shows ~0.01 dB RMS in-band — no static speech bell — while same-target profiles differ up to ~4 dB RMS from per-profile MI voicing (Finding 1). DAX's DE is evidently speech-gated, so it needs a **speech / speech-shaped stimulus** and ideally a same-profile DE-on-vs-off capture. The battery now carries `stimulus_speech` (espeak-ng synthesis when installed; LTASS-shaped-noise fallback that may not trip an MI speech classifier — the capture protocol must verify a nonzero DE-on-vs-off contrast before concluding anything). **Measured 2026-06-13 with espeak speech on DAX: no DE signature found.** `movie` (DE=5, enabled) vs `game` (DE=0) DAX output is identical to ±0.00 dB on *both* speech and pink (shared IEQ+AO target); our static chain adds the modelled bell (EE `movie`−`game` = +1.3 dB @ 1.5–3.5 kHz). Two unresolved readings: the espeak voice ("fairly robotic" per the capture notes) may not trigger DAX's MI dialogue classifier, OR DAX's DE isn't a static speech-band boost. Dolby Access exposed **no DE toggle** for the movie profile (only an Intelligent-EQ switch, left off), so a same-profile on/off contrast wasn't capturable. Verdict: 6 dB ceiling still **unconfirmed**, and our bell appears to over-apply vs DAX on this content — needs a speech source that demonstrably engages DAX's DE. **SoundWire `*8` arm + 4 kHz bell removed 2026-07-03:** introduced `2f4d0b8` (2026-04-12) to add "consonant clarity" on a chain whose 10×-over-applied IEQ (fixed `eeecc4a`/#13, 2026-05-28) was crushing treble by up to 28 dB — compensation for a since-fixed bug, and the measured over-application above argues for less dialog gain, not 33% more. It also made the generation banner (which always printed the ×6 figure) wrong on SoundWire. Both device families now share the ×6 single-bell mapping; field evidence issue [#29](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/29) ("dynamic wonky, music better" — DE is the main dynamic-vs-music audible difference). Restore via git history (`2f4d0b8`) if a SoundWire speech capture ever shows a stronger DE |
| 2 ✅ | Surround→stereo-base: `min(boost/20.0, 0.5)` — **REMOVED 2026-06-13**, the converter no longer maps `surround-boost` to any widening | `surround-boost` (1/16 dB) | the `/20` divisor and 0.5 cap were invented; Dolby surround is a spatial renderer, EE `stereo_tools` is a linear M/S balance | **resolved — widening dropped** (was: emitted when surround present, `surround-boost=96` on `dynamic`/`movie`) | **not testable on in-hand data:** the captured battery uses *correlated* pink (`stimulus_pink.wav`, corr +1.0, no Side), so the widener is a no-op — and indeed surr=96 (`dynamic`/`movie`) and surr=0 (`game`) loopbacks show identical residual side/mid (≈ −35 dB). EE half measured 2026-06: the live chain widens decorrelated pink by **+4.10 dB** S/M on `dynamic`/`movie` (surr=96 → stereo-base 0.5) and +0.02 dB on surr=0 profiles (analyzer `sm_delta_db`, which it previously skipped as an unknown kind). **DAX half measured 2026-06-13 (decorrelated pink captured on Windows): DAX applies essentially ZERO widening — surr=96 (`dynamic`/`movie`) S/M-delta is +0.01 dB, byte-for-band identical to surr=0 (`game`, +0.02) and to OFF (+0.02), flat across 250 Hz–8 kHz.** So our `/20` mapping adds +4 dB of static S/M width on the 2-channel speaker output that DAX does not produce — a clear over-application. Candidate fix: drop or sharply reduce the surround→`stereo_tools` widening (subject to the second-device bar). The phase-widening loophole is closed electrically too: on *correlated* pink (L/R corr +0.998, room to decorrelate) DAX holds the correlation at +0.997 (game and dynamic alike) — no inter-channel decorrelation, so not a phase/XTC widener either; our chain drops it to +0.991 (corr input) / −0.45 (decorr input). What a loopback still can't represent: crosstalk-cancellation's *acoustic* effect at the ears, or a virtualizer that's content-gated and dormant on stereo noise (engaging only on multichannel/object Atmos). Settling those needs a binaural capture / listening test, or an XML A/B with `surround-boost` edited to 0 (the risky single-block test) — but for the magnitude M/S rebalance our mapping actually performs, DAX demonstrably does nothing. **Provenance:** widening has shipped since 2026-02-28 (`82d7f3d`), but no DAX battery before 2026-06-13 (`measure_dax_3`) contained a decorrelated-stereo stimulus — the Apr/May sets were pink/sweep/multitone/stepped only (correlated pink = no Side, widener a no-op), so 2026-06-13 is the *first* DAX widening measurement; there is no earlier DAX stereo data to compare against. **Why DAX's effect is ~nil (leading hypothesis):** `surround-boost` is almost certainly a *virtualization/surround-render depth* parameter, not a stereo-width knob — it gates with `surround-decoder-enable` / `output-mode-partial-surround-virtualizer-enable` (an FFT-domain upmix→virtualize stage we already classify as non-modelable). Fed plain 2-channel PCM with no surround/object bed, that renderer has nothing to synthesise, so the boost scales ≈nothing: `movie` (boost=96) ≡ `game` (boost=0) to 0.01 dB RMS / ≤0.07 dB max in **both** L and R, not just in S/M. So our mapping is likely wrong *in kind* (a static width knob for what is really a multichannel-render gain), not merely over-scaled — and on the stereo playback path the converter targets, the faithful behaviour is to not widen. **Resolution:** the `surround-boost → stereo_tools` widening was removed from the converter (`make_stereo_tools`, the emission branch, the `surround` param of `make_preset`, and the `--disable stereo` flag are all gone); `surround` is still parsed and reported as intentionally-unmapped. This is a one-device decision (no second-device DAX capture), justified because it *removes* an unvalidated invented scaling that the only falsifying signal (a DAX capture) contradicted, rather than adopting a new mapping; the mechanism (render-depth param, dormant on stereo) is structural, not per-device. If a future device's DAX capture shows real widening, restore via git history (`82d7f3d`). **Validated on-device 2026-06-13** (re-captured the new no-widener chain through live EE): decorrelated-pink S/M widening dropped +4.10 → **+0.02 dB** on every profile (dynamic/movie/game), matching DAX's +0.01; correlated-pink fell +4.41 → +0.32 dB (residual = the device's L/R-asymmetric FIR/PEQ, DAX +0.12, not widening). No mono regression: the rest of the chain's preset JSON is byte-identical (golden harness), and live mono-pink matched pre-fix within capture repeatability (~0.45 dB RMS), EE−DAX pink steady at 1.35–1.67 dB RMS (Finding-9 baseline). Verdict: adopt |
| 3 ✅ | Convolver SoundWire headroom restore: `peak_db * 0.5` — **REMOVED 2026-07-03**, the convolver emits 0 dB gain on every device family | (none — post-normalisation heuristic for the IEQ-only, no-AO SoundWire curve) | the 0.5 was chosen to "recover brightness"; not XML-derived | **resolved — restore dropped** (was: default audible on SoundWire) | **Provenance:** introduced `2f4d0b8` (2026-04-12, the first SoundWire user's PR) to restore "+6-7 dB" of the level FIR peak-normalization removed — but that large peak was an artifact of the pre-#13 chain over-applying `ieq-amount` 10× (fixed `eeecc4a`, 2026-05-28, on-device validated): after the fix the same formula self-scaled to ~+0.7 dB (issue [#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27)'s pasted generation runs: FIR peaks +1.1…+1.5 dB → restores +0.6/+0.7 dB), i.e. the restore was tracking the bug's magnitude, not a property of SoundWire curves. Field evidence: issue [#29](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/29) (Zenbook S14) found the SoundWire preset over-loud (reporter manually set −5 dB output). Removal follows the entry-2 precedent — it *drops* an invented non-XML gain rather than adopting a new mapping, so the second-device bar doesn't apply; loudness makeup is volmax-boost's job (XML-derived, entry on volmax slots). Not locally measurable (dev device is HDA); the #29 reporter's regenerate-and-listen is the field check. If a SoundWire DAX capture ever shows DAX applying net positive gain vs OFF that our chain lacks, restore via git history (`2f4d0b8`) |
| 4 | Regulator slope→ratio: slope read `/16` (`parse_xml`), then `ratio = 1/(1−slope)` (`make_regulator`) | `regulator-distortion-slope` | the `/16` reading is assumed by analogy to the dB fields; `1/(1−slope)` is inferred from how corpus values cluster | regulator only engages at high level | **not testable on this device:** the X1 Yoga is `distortion-slope=16` on *every* profile, so there is no operating-point variation to fit `1/(1−slope)`. Needs a device with differing slope values + a bass-burst capture comparing gain-reduction-vs-level (Phase 4) |
| 5 | Regulator timbre→knee: timbre read `/16` (`parse_xml`), then `knee = −6·timbre` dB (`make_regulator`) | `regulator-timbre-preservation` (corpus-frozen at 0.75) | the `−6` dB maximum knee is a pure guess; the field is constant across the corpus, so we have no signal to disambiguate | regulator, high level | **not testable on this device:** the X1 Yoga is `timbre-preservation=12` (=0.75) on *every* profile, so the `−6·timbre` scaling has a single operating point. Needs a device whose XML carries `timbre≠0.75`, plus a capture (Phase 4) |
| 6 | MBC ratio `1/(coeff/32768)` (`decode_mbc_bands`); time constants via Q15 with `block_size=256` → 187.5 blocks/s (`decode_mbc_time_constant`) | `mb-compressor-tuning` 6-tuples | the Q15 format and 256-sample block size are assumed from common DSP practice and only sanity-checked numerically, never measured | **dormant** — the MBC doesn't engage on the −10 dBFS test stimuli (Finding 3) | **Woken 2026-06-13** with `stimulus_stepped_loud` (−2 dBFS peak): comparing loud-vs-normal static gain (aligned @1 kHz), **DAX compresses far harder than our chain** — DAX −10.6 dB GR @234 Hz (EE −5.5), −10.4 @277 (EE −1.7), −5.9 @141 (EE 0), −7.4 @2.25 kHz (EE −3.2). Adaptive cross-pass span ≤1.5 dB, so this is the compressor/regulator, not the leveler. (Re-verified 2026-07-01 from the raw held-tone envelopes: at the 234/277 Hz diagnostic bands the within-tone drift is ≤0.16 dB and all three passes agree to ~0.03 dB, so the leveler's per-tone adaptation does not contaminate the GR readout there — the adaptation is visible only elsewhere, −1.1 dB early-tone at 141 Hz and a 1.5 dB cross-pass span at 3 kHz.) **Diagnosed 2026-06-13** (`localresearch/scaling-campaign/dynamics_gap_analysis.py`; agent analysis, key numbers re-verified from the converter): the gap is **neither** the upstream bass-level gap **nor** a wrong MBC decode. (i) 1 kHz-referenced, the level each chain delivers to its dynamics agrees within ±3 dB at every diagnostic band (141–4193 Hz) — the "DAX delivers +16–22 dB more" reading was a reference artifact (our FIR is peak-normalised to a different anchor than DAX's OFF-flat baseline); the real 22–30 dB Finding-4 bass gap sits below ~120 Hz, pre-attenuated by the 100 Hz HP before either chain's dynamics. (ii) The MBC decode is internally faithful but *conservative*: a 3-level fit (−42/−18/−2) shows EE realises its nominal 1.67 ratio only at the one band that clears threshold well (234 Hz, R≈1.54); elsewhere the −6 dB soft knee + RMS detection keep it sub-slope. (iii) The actual driver is the **regulator under-engaging**: DAX's effective ratio at 234/277 Hz ≈ 2.95 = its near-100:1 regulator stacking on the MBC, yet our regulator — though it maps the same −10/−9/−8/−5 dB thresholds + slope (entry 11) — barely fires there. So the lever is the regulator, not the MBC ratio/threshold (which stays XML-derived and unchanged). See entry 11 |
| 7 | Volume-leveler→autogain window: `max-history = 40−amount·4` / `30−amount·5` (`make_autogain`) | `volume-leveler-amount` (0–10) | the window formula is invented — and measured to be no reaction-speed lever at all (20/32/40 s gave identical ~4 dB onset overshoot; see "The 2026-07 default-flip attempt") | **bypassed by default** (HDA, `--enable autogain` opts in); active in the conservative SoundWire path | a capture of DAX's MI-steered leveler (non-LTI — hard) |
| 8 | PEQ anti-clipping trim: `effective boost ≈ gain·min(1, 2/Q)` per positive bell (full gain for shelves), peak negated into `equalizer#0.output-gain` (`make_peq_eq`) | (none — headroom heuristic over the XML's PEQ gains) | the `2.0` bandwidth weighting and "compensate exactly the peak effective boost" rule are converter-invented; nothing says DAX trims broadband level at all — and "over-conservative PEQ output-gain" is a listed listen-for trap | **default audible** on every XML whose PEQ has boost bells/shelves | the dev device has no cross-profile Q contrast (PEQ identical in every profile: +3 dB/Q2 @280, +4 dB/Q4.6 @400, −4 dB/Q1.5 @516), but the hypotheses predict distinct broadband offsets there — `min(1, 2/Q)` → −3 dB trim, full compensation → −4 dB, no trim → 0 — discriminable by an **absolute-level** EE↔DAX pink compare (`compare_ee_vs_dax.py --absolute`, volumes pinned; the default 1 kHz normalization destroys exactly this observable). Tried 2026-06 on the archived DAX captures: confounded — the absolute EE−DAX offset is −11.5 dB on `dynamic`/`movie`/`game` but −1.0 dB on `voice`, i.e. dominated by DAX's profile-dependent leveler/volmax staging. **Re-tried 2026-06-13 with pinned/recorded 50% volume: still confounded** — DAX's leveler drives `dynamic`/`movie`/`music`/`game` to a single loudness target (raw transfer all within 0.01 dB), giving a flat ≈ −8 dB EE−DAX offset (leveler boost + our −3 dB trim + convolver peak-normalisation, inseparable), while `voice` (leveled to a quieter target) shows −0.06 dB. The 3 dB PEQ trim is buried under the leveler target. Useful byproduct: the DAX OFF raw transfer is −0.01 dB at 50% master volume, i.e. **WASAPI loopback taps the engine mix bus pre-volume** — the master-volume term never enters the captures. Validating the `min(1, 2/Q)` *shape* still needs a wide-vs-narrow-Q second device |
| 9 | SoundWire Calf BassEnhancer constants: `amount=12 dB`, `harmonics=10`, `blend=−10`, `floor=10`, `scope = min(2·hp_freq, 300)` (`make_bass_enhancer`) | (none — the XML's `bass-enhancer-*`/VBE fields are corpus-frozen; Finding 8) | every knob is converter-chosen; the `2×` scope multiplier derives an emitted parameter from the PEQ HP corner; the constants were also tuned (`bc12c2e`, 2026-04-12) against the pre-#13 over-applied-IEQ chain, so the 12 dB drive may compensate a since-fixed deficit | **default audible** on SoundWire (the most audible invented stage on those devices). First field evidence of over-drive: issue [#29](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/29) (Zenbook S14) — "too bass boosted" + occasional chassis resonance, reporter manually raised `floor` 10→50 Hz and cut output 5 dB. Kept default-on for now (Finding 8 shows DAX genuinely runs VBE, so removal re-opens a real gap); the #29 A/B (`--disable bass-enhancer` vs default) was the intended discriminator, but its **round-2 result (2026-07-05) is ambiguous:** disabling it did *not* fix `dynamic` (still bad without it), and `music` lands close to Windows *with* it on (the stage rides every profile preset, `music` included) — so the report neither condemns nor vindicates the whole stage. The reporter's concrete complaint is the `floor=10 Hz` constant (drive below the woofer's usable range → chassis resonance; he set `floor`≈80 Hz + cut the amount), a hardware-dependent value the XML doesn't carry. **Second negative field report (2026-07-21, issue [#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27) follow-up, Galaxy Book6 Ultra):** with the machine's Cirrus amp firmware finally installed (amp DSP now doing real bass management), the reporter needed `--disable bass-enhancer --disable volmax --disable regulator` to avoid "dramatic" degradation — also confounded (three flags disabled at once, and that run's volmax rode an inert all-0 dB-threshold regulator; cross-device-findings §15 addendum), so it tilts toward opt-in without deciding it. Note the SoundWire-*only* gate is contribution-historical (`bc12c2e`, the first SoundWire user's path), **not** a principled HDA/SoundWire split: on Linux the HDA path equally lacks Dolby's Windows-driver VBE, and Finding 8 measured DAX running VBE on an HDA device — so the *missing*-on-HDA side is issue [#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14) while the *present*-on-SoundWire side is what #29 questions. **Follow-up gated on #29's XML + capture:** revisit (a) flipping this stage to opt-in and (b) whether `floor` can be tied to the PEQ HP corner (like `scope`) instead of a hardcoded 10 Hz | a SoundWire-device DAX capture with the bass-burst stimuli (Snapdragon X / Yoga Slim 7x / the #29 Zenbook) |
| 10 | Conservative-autogain offsets: `target = out_target − 6.0` dB, `silence-threshold = −50` dB (`make_autogain`; since 2026-07 the −50 gate is stored on both paths — the HDA block previously kept EE's −70 plugin default) | `volume-leveler-out-target` | the −6 dB safety offset and −50 dB threshold are invented; entry 7 covers only the window formula. The −50 gate is field-confirmed (#25) and capture-measured (+1.7 dB silence wind-up vs +41.8 dB at −70 — see "The 2026-07 default-flip attempt") | active on SoundWire; audible on HDA only via `--enable autogain` or manual GUI enable | same as entry 7 (MI-steered leveler capture — hard) |
| 11 | Fixed dynamics constants: MBC active-band `knee = −6.0` dB (`make_multiband_compressor` — the Dolby 6-tuple has no knee field); regulator `attack 1.0 ms` / `release 50.0 ms` (`make_regulator`) | (none) | chosen from limiting practice, not decoded | **dormant at nominal levels** (the dynamics-dormant measurement above); engaged on loud content | **Engaged 2026-06-13** by `stimulus_stepped_loud` (see entry 6): the dynamics diagnosis lands *here* — on the regulator's fixed constants, not the MBC decode. Our `make_regulator` maps the XML thresholds/slope correctly (−10/−9/−8/−5 dB, near-100:1 on the 4 lowest bands) yet under-engages vs DAX, which clearly hard-limits those bands. ~~Leading hypothesis: the hard-coded `attack 1.0 ms` / Peak detection / `1 ms lookahead` / `release 50 ms` make our regulator *release between* the stepped tones and under-read steady-state GR~~ — **falsified 2026-07-01** by re-analysis of the same captures: the within-tone envelope (single-bin DFT over early/mid/late windows of each held tone) shows EE's response is *time-flat* (drift ≤0.14 dB — no attack ramp, no release decay), and the stepped analyzer's readout already skips the 0.4 s settle, so a 1 ms-attack regulator cannot under-read a steady-state window by releasing in the gaps. The under-engagement is **static**, which points away from the invented time constants entirely. **New leading suspect — gain staging:** at capture time the dev device's volmax `+6 dB` sat in the preset `output-gain` slot, *after* the dynamics; the 2026-06-22 `--volmax-slot input-gain` default flip (`4213d5f`, #23) now feeds the MBC/regulator a 6 dB hotter signal. **Measured 2026-07-01** (fresh 3-level stepped battery through the regenerated input-gain-default preset, vs the archived DAX stepped captures): the flip helps but does **not** close the gap — loud-vs-normal GR at 234 Hz −5.5 → −6.9 dB (DAX −10.6), 141 Hz 0 → −1.4 (DAX −5.9), 277 Hz −1.7 → −2.1 (DAX −10.4); 2.25/3 kHz unchanged (the regulator is inactive above 328 Hz on this XML, so that part of the gap is the MBC's knee/RMS conservatism, as diagnosed). The sharper residual finding: even 6 dB hotter, the realized regulator curve fits an effective ratio ≈1.8 at 234 Hz against the configured 100:1 — the LSP MBC-as-limiter realization (band detection mode / knee / boost interplay) under-realizes the intended hard limit by an order of magnitude. So the remaining lever is the regulator's *plugin realization*, not signal level and not timing. Stage interaction (the MBC's +2 dB makeup re-inflating the signal the regulator then sees) stays a secondary suspect. **First step done 2026-07-01** — the post-flip stepped re-capture above; a material gap survives, so settling the remainder needs (a) a regulator-only EE capture (MBC bypassed) at the 3 levels to deconfound the two stages — now specifically to characterise the *realized* limiter curve against the LSP settings (`make_regulator`'s detection mode, knee, lookahead) and find why 100:1 configured realizes as ≈1.8, (b) a second-device loud capture before any default change (corpus invariant), and ideally (c) an EE OFF/flat stepped capture to put EE on DAX's absolute-dBFS footing. **XML-grounded angles to try first** (regulator-tuning carries no time constants — unlike the MBC's Q15 coeffs — so timing is invented by necessity, but two currently-ignored regulator fields might inform the engagement): (d) **re-examine `regulator-stress-amount` as an engagement/aggressiveness modifier, not a threshold offset.** It's the only per-device-varying regulator field and on `dynamic` it's `144,144,0,…` — non-zero on exactly bands 0–1 (47/141 Hz), the under-engaging bands. Follow-up 5 rejected it only under the *threshold-offset* reading (lowering threshold moved EE away from DAX); the new framing (DAX intensifies limiting on "stressed" bands → effective ratio ~2.95) is untested and could both explain DAX and stay XML-only. (e) ~~`regulator-relaxation-amount` (=96) as the release control~~ — **dropped 2026-06-18**: not XML-derivable (frozen at 96 across the whole corpus, so there is no contrast to decode against), and the 2026-07-01 time-flat finding removes its motivation, since release timing isn't the under-engagement driver. (f) **`regulator-tuning/isolated_band` (added 2026-07-30, Finding 10):** a previously-unread per-band 0/1 array with genuine per-device contrast (59 corpus patterns; mirrors threshold-activity exactly on 18,369 profiles but diverges on ≥1 band on 11,548). Semantics unknown — and probe-level span attribution on the #44 stepped data argues it does *not* gate the measured adaptive layer: that device carries the discriminating contrast (band 11 iso=1 vs band 12 iso=0, both threshold-active) and both span ~5 dB alike, while the inert iso=0 band 10 spans least (Finding 10). **Experimental opt-in shipped 2026-07-30** (`--enable coupled-bands`): zero-threshold zones whose bands are all `isolated_band=0` join the limiter at face value (0 dBFS) — the iso=0 scoping is a conservative gating choice, not established causation — so upstream gain (volmax on input-gain) gets tamed there before the brickwall; default output unchanged, awaiting the #44 reporter's on-device A/B. **Corpus-swept same day** (36,371 regulator profiles / 913 devices through the real parse + both regulator modes): zero crashes, zero default-output deviations, `isolated_band` is *universal* (present on every regulator profile, always 20×{0,1}); 99.93% of profiles are flag-eligible, so the end-of-run `--enable` hint is effectively unconditional; on all-zero-threshold tunings (2,961 profiles — the issue-#27 class) the flag yields a single full-band 0 dBFS limiter, incidentally restoring the "volmax tamed before the brickwall" property those tunings otherwise lack. Threshold-inert-but-`iso=1` bands exist on 134 devices (mixed zones correctly declined). **Scope honesty (offline staging check, same session):** during the −18 dBFS capture battery our chain's level at every coupled band is −12…−19 dBFS (FIR + dialog bell + volmax 7), so the captures can neither confirm nor falsify the mapping's audible effect — and DAX's measured 4–6 dB spans at those levels cannot be a static 0 dBFS limiter either (even +8 dB leveler makeup leaves ~−8 dBFS in-band). The flag is a loud-content protection hypothesis (engages when in-band level crosses full scale, i.e. content peaks above ≈ −5 dBFS in the 3–6 kHz range on this XML), not a reproduction of the measured moderate-level spans; the A/B must use loud material. **Second-device datapoint (2026-07-30, Finding 10):** the issue [#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44) stepped battery shows DAX applying 4–6 dB of frequency-selective adaptive span in bands whose `threshold_high` decodes as inert (+0.0) on that XML — so part of DAX's band dynamics demonstrably lives outside the regulator parameters we decode, and the "close the regulator gap" ceiling may be lower than the DAX reference implies. The MBC knee/attack/release themselves still need gated-burst transients to characterise — deferred |

Confirmed for contrast: the `/16`-dB convention is verified (issue [#15](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/15)) and the
`/32768` Q15 decode is at least numerically consistent with first-order
time-constant theory; everything else above is unverified.

**Validation roadmap.** Ordered by how closely each mirrors the `ieq-amount`
case — a fixed scaling in the default path, measurable against a DAX capture:

1. *Offline pre-screen, on data already in hand.* Recompute the current
   converter's target without the stage under test, subtract from the matching
   DAX capture, and read the residual — the same offline screen that flagged
   the `ieq-amount` weight before any new measurement. **Dialog enhancer (entry
   1) — done, result negative/refining.** Across the X1 Yoga pink battery, the
   profiles that differ only in DE amount do not differ in steady-state
   magnitude: `movie` (amount=5) vs `game` (amount=0) is ~0.01 dB RMS in-band,
   and `dynamic`/`movie`/`game` all sit within ~1 dB RMS despite DE 5/5/0 — far
   below the modelled ~1.25 dB bell. Meanwhile profiles with *identical* IEQ+AO
   (`music` vs `game`, both DE off) differ by ~4 dB RMS, i.e. per-profile MI
   voicing (Finding 1, non-LTI) dwarfs and is uncorrelated with DE amount. So
   the DE is content-adaptive (speech-gated): pink cannot excite it, and
   cross-profile differencing on pink is the wrong test. Validating the 6/8 dB
   ceiling needs a speech / speech-shaped stimulus and, because MI voicing
   differs per profile, a *same-profile* DE-on-vs-off capture rather than a
   cross-profile comparison. **Surround (entry 2) — screened, not testable
   offline.** The captured battery uses correlated pink (`stimulus_pink`,
   corr +1.0), which has no Side component for the widener to act on; the
   surr=96 (`dynamic`/`movie`) and surr=0 (`game`) loopbacks show identical
   residual side/mid (≈ −35 dB). Falsifying the `/20` mapping needs the
   decorrelated `stimulus_stereo_pink` captured in stereo (Phase 3).
   **Regulator (entries 4/5) — not testable on this device.** The X1 Yoga is
   `distortion-slope=16` and `timbre-preservation=12` (=0.75) on every profile,
   so there is no operating-point variation to fit `1/(1−slope)` or `−6·timbre`;
   these need a device with non-default values (Phase 4), independent of
   stimulus. Net: of the offline pre-screens, only the dialog enhancer had
   screenable in-hand data, and it came back negative/refining.
2. *New in-house captures (X1 Yoga, HDA).* **Linux side done 2026-06-12:**
   the post-Finding-9 EE battery was regenerated for all five profiles
   (pink residuals reproduce Finding 9's table — `dynamic` 1.03 dB RMS —
   and `music`'s ~3.5 dB MI-voicing outlier is unchanged), plus the new
   stimuli: speech (EE treats speech and pink identically, confirming our
   DE is static where DAX's is gated), decorrelated stereo (entry 2 EE
   half: +4.10 dB S/M at surr=96), and `stimulus_stepped_loud` (−2 dBFS
   peak — crosses the ≈ −6.4 dBFS MBC knee; the EE dynamics wake exactly
   at the high-chain-gain bands, −5.5 dB GR @234 Hz / −3.2 dB @2.25 kHz,
   entries 6/11). **Windows side done 2026-06-13** (post a minor Dolby
   Access update; the residuals barely moved, so it reads as a clean
   second capture, not a confound): 26 DAX captures at pinned 50% volume
   (speech / pink / decorrelated-stereo per profile + `stepped_loud` on
   off/dynamic). Headline results, scored EE↔DAX:
   - **Finding 9 confirmed on a second DAX session** — pink EE−DAX RMS
     0.97–1.49 dB across profiles, 19.7 kHz Δ −1.5…−3.2 dB (vs the
     pre-Finding-9 −28 dB). The `/100` HF fix holds. (`music`, the prior
     3.5 dB outlier, is now 1.09 dB.)
   - **Entry 2 — over-application found:** DAX widening at surr=96 is zero
     (S/M-delta +0.01 dB, identical to surr=0/off); our chain adds +4.10 dB.
   - **Entries 6/11 — DAX compresses ~2× harder** at loud level
     (−10.6 dB GR @234 Hz vs EE −5.5; strong over 140–400 Hz and
     1.9–4.7 kHz), entangled with the bass-level gap.
   - **Entry 1 — no DE signature** on espeak speech (`movie` DE=5 ≡ `game`
     DE=0 to ±0.00 dB); unresolved (robotic voice may not trigger MI, and
     Dolby Access offered no movie DE toggle).
   - **Entry 8 — still confounded** by the leveler's common loudness target
     even at pinned volume.
   The remaining open asks are now second-*device* ones (below), not this
   device's. Reproduce: `localresearch/scaling-campaign/analyze_dax_results.py`.
   Tooling: `tools/measure_dax/` (Windows capture) and `tools/measure_ee/`
   (Linux loopback).
3. *User-contributed data.* The X1 Yoga is an HDA device and is corpus-frozen
   on several fields, so some entries can only be falsified by other
   hardware/XMLs: a SoundWire device for entries 9/10 (entry 3 and the
   SoundWire dialog arm of entry 1 were instead *removed* 2026-07-03 —
   invented gains compensating the since-fixed #13 IEQ over-application, so
   no capture was owed); a device with `ieq-amount≠10` for the Finding 9
   residual; a device with `regulator-timbre-preservation≠0.75` or a differing
   `regulator-distortion-slope` for entries 4/5. The converter's
   `_UNMODELED_FEATURES` warning already nudges users to report XMLs whose
   `regulator-overdrive`/`relaxation-amount` deviate from the corpus constants.
   Track the asks on a GitHub issue (cf. issue [#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14) for the VBE follow-up).
   Entries 8–11 (added in the 2026-06 review) piggyback on the same campaign:
   entry 8 needs narrow-vs-wide-Q profile captures on existing HDA hardware,
   entries 9/10 need a SoundWire-device DAX capture (the #29 Zenbook S14 is
   the first candidate), and entry 11 folds into the loud-content captures
   that wake the dynamics stages (entry 6).

Any EE↔DAX measurement is decided on on-device ground truth, the offline
pre-screens are only a filter, and changing a default mapping requires a
second-device confirmation. (Finding 9 met it via convergent second-device
evidence — two independent methods on a Yoga Slim 7x — though not yet a
second DAX *capture*; its "residual open question" tracks what a capture
would add.)

### Follow-ups to close the gap to DAX

When this list was assembled, the cheap, deterministic, XML-only
experiments looked exhausted — hypothesis (b) rejected (Finding 6), no
missed XML block (Finding 5), 5-profile coverage in (Finding 6).
Finding 9 then closed most of the HF gap with exactly such an experiment
(a re-reading of a field we already parsed), so "exhausted" was wrong.
What remains *after* Finding 9 — the ~1 dB HF residual and the
LF/leveler gap — needs either data outside the XML or a relaxation of
the determinism / latency constraints. (Item numbering is stable across
revisions and referenced elsewhere; items are grouped by status, not
numeric order.)

**Still actionable, no constraint change:**

1. **Stripped-down single-block tuning XML A/B on Windows.** Disable
   everything except IEQ in a tuning XML and capture DAX, then add
   AO, then add per-band PEQ, etc. Originally motivated by the
   pre-Finding-9 HF/mid gap, which is now mostly closed; still the
   sharpest tool for what remains — pinpointing which DAX stage
   carries the ~1 dB HF residual and the LF/leveler behavior — but
   weigh the (unchanged) risk against that much smaller payoff.
   Risk: needs driver-level XML replacement, could brick DAX on
   the test machine until restoration. Scope before attempting.

**Closed, no constraint change — kept as a permanent finding:**

5. **`regulator-stress-amount` mapping investigated and rejected.**
   Issue [#11](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/11) raised whether DAX's "tier-2" adaptive sub-models
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

   **Reopened 2026-06-13 (different reading).** The dynamics-gap
   diagnosis (entries 6/11) found the regulator under-engages on
   exactly bands 0–1 (47/141 Hz) — precisely where `stress=144,144`
   sits — and that DAX's effective ratio there (~2.95) far exceeds our
   1.67. That points at the "stress may not be a threshold offset at
   all" door above: re-test `stress-amount` as an **engagement /
   aggressiveness modifier** (intensify limiting — ratio/attack — on
   stressed bands), which the original threshold-offset experiment
   never tried. Queue alongside the regulator-only capture (entry 11).
   (The `regulator-relaxation-amount` companion-decode was dropped
   2026-06-18 — not XML-derivable, frozen 96 corpus-wide — and the
   2026-07-01 re-analysis found the under-engagement is static, not
   release-timing; see entry 11.)

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
   6 identified per-band dB targets *under the pre-Finding-9 scaling*
   (flatten HF above ~10 kHz, soften +4 dB at 2.25 kHz, lift 5–6 kHz)
   — those targets are obsolete, and tuning to them today would
   re-introduce the error Finding 9 removed. A tuner now would fit
   against fresh post-Finding-9 captures (target: the ~1 dB HF
   residual and the LF/leveler gap in Finding 9's table).

**Closed by the variant sweep (Finding 6 / 7) — kept here as
historical record; do not re-litigate without new evidence. (All
pre-Finding-9: the HF residual these variants traded against is now
largely closed by the `/100` reading.)**

  - "Try `IEQ − AO`" — rejected; +7–20 dB worse on every profile.
  - "Run on the other 4 profiles" — done; HF gap is profile-independent.
  - "Audit the XML schema for missed HF-shaping blocks" — done;
    none found (Finding 5).
  - "Soften the HP at 100 Hz from `x2` to `x1`" — the test XML's
    HP is XML-driven (order=4 → x2), not the `make_peq_eq` filler
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
    the 19.7 kHz gap was still 18 dB after applying it. Since
    settled by Finding 9: the percentage reading (`/100`) achieves
    the down-weight through a simpler XML-grounded rule and closes
    the residual β couldn't. Re-litigating the cap reading on top
    of `/100` would double-count the down-weight — closed, not
    "worth revisiting".
  - "Apply IEQ only inside a frequency window (γ)" — Finding 7;
    pareto trade — biggest HF reduction (−10.5 dB at 19.7 kHz),
    but 47 Hz blows out from −8 to −18 dB EE−DAX.

## Bad sound with a perfect preset: the kernel layer below (issue [#33](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/33))

The IdeaPad Pro 5 14APH8 report ([#33]) had "a lot worse than Windows" sound
with *every* tuning XML in its driver store — bass mostly missing, the rest
garbled — and was ultimately fixed by a kernel upgrade (Debian's 6.12 LTS →
7.0), not by any preset change. The reporter's `--speaker-info` output is
identical on the broken 6.12 and the working 7.0 (same ALC287 codec, one
stereo speaker pin, no smart-amp driver bound; Lenovo's spec lists plain
2 W × 2 stereo), so the fix was not new hardware support — the older kernel
was mis-*configuring* the same codec/speaker path, and 7.0 repaired it. The
reporter's own research points at power-management changes breaking 6.6-era
codec/amp setup ("flat sound"), and his analog controller's PCI SSID
`17AA:3881` matches the kernel's "YB9 dual power mode2" quirk entry, so a PM
regression or mis-firing quirk is plausible — the exact mechanism isn't
identifiable from userspace. (An earlier draft of this entry blamed a
mis-driven TAS2781 smart amp — the SSID is the TAS2781 fixup's match key —
but the identical working-kernel topology rules that out: this SKU very
likely has no smart amp at all.) Either way, the preset's treble-forward
correction on top made the broken baseline sound *worse* than stock.

Lesson: symptoms indistinguishable from a bad preset can originate a layer
below anything XML-derived — check the drive path before re-litigating the
mapping. This motivated the old-kernel hint (end-of-run banner, `--doctor`
check, `--speaker-info` annotation). Design choices:

- Kernel series ages are computed from a release-month table
  (`_KERNEL_SERIES_RELEASES`) rather than a "latest known kernel" constant:
  release dates are historical facts, so an aging copy of the tool still ages
  old kernels correctly, and a series newer than the table is assumed recent
  (never flag a brand-new kernel).
- Cutoff `_KERNEL_OLD_MONTHS = 18`: a stable distro's kernel is at most
  ~9 months old on the distro's release day (Debian 13 shipped 6.12 at
  9 months; Ubuntu LTS GA kernels at ~1 month), so 18 months keeps every
  fresh install quiet for 9+ months and never fires for rolling/HWE users,
  while still catching the one real case (#33 fired at 6.12 + 20 months —
  a 24-month cutoff would have missed it). LTS point releases backport
  one-line `Cc: stable` quirks, but not the driver-rework /
  power-management fixes of the class seen here.
- Keeping the table current is automated (`tools/update_kernel_releases.py`,
  run weekly by `.github/workflows/kernel-release-table.yml`, one PR per new
  series). Staleness is the failure mode worth engineering against: since a
  series above the table's max is treated as recent, a table that stops
  growing doesn't warn *less accurately*, it silently stops warning at all.
  The month is taken from the `vX.Y` tag's tagger date on Linus' tree — the
  only source checked that reproduces all 32 hand-entered rows exactly. The
  `cdn.kernel.org` tarball mtime was rejected: it lags the tag by up to a
  day, which lands 5.19 in `2022-08` and 6.18 in `2025-12`, both a month
  late. The updater is append-only and refuses an implausible batch, an
  out-of-order date or a partial parse, so a hand correction below the
  newest entry survives and a broken run fails loudly instead of writing a
  plausible-looking wrong table.

[#33]: https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/33

## Rejected approaches

Things that were investigated and explicitly declined, recorded so they don't get
re-proposed:

- **`filter_coefficients` as an audio EQ source.** The base64-encoded biquad
  blob in `tuning-vlldp` was investigated as a possible speaker-correction EQ,
  but the decoded coefficients don't produce sensible audio curves — they are
  almost certainly VLLDP-internal analysis filters, not an audio-path
  equaliser. The audio-optimizer + speaker-PEQ parameters already capture the
  same speaker correction, so nothing is lost by ignoring it. (Listed under
  "Not implemented" in [reference.md](reference.md).)
- **Noise gate before the compressor.** Would prevent noise-floor amplification, but
  real content rarely has an audible noise floor at the levels that trigger the
  compressor. Adds complexity for no practical benefit.
- **GPU compute for FIR convolution.** See `docs/alternative-pipelines.md` Option 5.
  The FIR convolver uses <0.1% of a single CPU core; there's no CPU pressure, and
  CPU→GPU round-trip latency is unacceptable for realtime audio.
- **Custom SOF DSP topology with FIR + DRC modules.** See `docs/alternative-pipelines.md`
  Option 2. Highest offload potential, but requires rebuilding signed firmware and
  custom topology files — too much maintenance burden for a workstation tool.
- **Parametric-EQ approximation of the IEQ curve** (instead of FIR). The
  20-value IEQ arrays (e.g. `ieq_balanced`) are the desired *composite*
  frequency response, not individual filter gains — applied directly as
  parametric bell gains they stack to +20–30 dB at mid frequencies. Every
  solver tried left large inter-band ripple (numbers reproduced on the X1
  Yoga Gen 7 / Realtek 17AA:22E6 dynamic / balanced curve; comparable shape
  on the IdeaPad XML from issue [#4](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/4)):

  | Approach | Peak error vs target | RMS error vs target |
  |---|---:|---:|
  | Raw values as bell gains (Q=1.5) | ~34 dB (cumulative mid boost from overlapping filters) | ~20 dB |
  | Iterative solver (center-freq only) | ~16 dB between bands (0.1 dB at the 20 centres) | ~2 dB |
  | Least-squares solver (dense grid) | ~11 dB | ~1.6 dB |
  | **FIR convolution (current)** | **0.34 dB across audible band, 0.07 dB at the 20 band centres** | **<0.1 dB** |

  (All figures are peak and RMS on the same dense log-frequency grid,
  20 Hz–22 kHz, 800 points. An earlier revision of this table quoted
  "±5 / ±4 dB ripple" for the biquad-fit rows and "≤0.06 dB everywhere" for
  FIR — those mixed peak and RMS metrics across rows.) Those fits were
  measured against the pre-Finding-9 full-weight IEQ target; today's `/100`
  target is far flatter in its IEQ component, so the quoted magnitudes
  overstate the current gap. The conclusion stands regardless: the AO
  component keeps full per-band swings, and the min-phase FIR realises the
  composite target exactly at zero latency and negligible CPU, so a PEQ
  approximation has no upside.
- **Auto-trimming the convolver IR to its audible length.** Issue [#11](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/11) noted that the
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
- **An *unused* EasyEffects built-in to cover a dropped DAX feature**
  (EE-built-in plugin gap audit, 2026-06-22 — the primary-converter counterpart
  to the PW-converter audit that closed the autogain gap, see "Translating
  active autogain to LSP `autogain_stereo`"). The PW converter can host any LV2
  plugin, so that audit's question was "is there a better plugin?" — answered
  once (autogain). The EE preset can't: EasyEffects is **not** a generic LV2/
  LADSPA host — it draws each effect's controls by hand and exposes only a
  curated built-in set (the experimental "Native window of effects" /
  "Update frequency — Related to LV2 plugins" toggles only surface the *bundled*
  LSP/Calf plugins' own GUIs, not arbitrary loading; EE
  [Discussion #2928](https://github.com/wwmm/easyeffects/discussions/2928),
  [Issue #1433](https://github.com/wwmm/easyeffects/issues/1433)). So the only
  question is "does an unused built-in better represent a DAX feature we drop?"
  — and across all ~19 unused effects (Exciter, Crystalizer, Maximizer,
  Loudness, Bass Loudness, Crossfeed, Speech Processor, …) the answer is no, for
  three reasons that recur:
  - **Corpus-dormant** — virtual-bass, graphic-EQ, volume-modeler are disabled
    on every corpus XML with frozen params (no per-device signal); a Bass
    Loudness / Loudness / Exciter mapping would be pure invention against
    XML-only derivability.
  - **Validated to zero effect** — surround/height widening: a DAX capture
    showed Dolby applies no stereo widening on 2-ch content, so the old
    `stereo_tools` mapping was *removed* (see "Unvalidated converter scaling
    factors" entry 2). Re-adding via Crossfeed/Stereo Tools re-introduces a
    falsified effect.
  - **Content-gated, not static** — DAX's dialog enhancer is MI-steered, not a
    static spectral boost (EE treats speech and pink identically; see
    "Unvalidated converter scaling factors" entry 1). Our static PEQ bell
    already *over-applies*; an Exciter is *more* static invention, not a better
    match.

  The one genuine functional gap is **Dynamic Speaker Optimization** (DSO —
  excursion-aware bass limiting, active on 1 newer SoundWire XML; see
  [cross-device-findings.md](cross-device-findings.md) newer-pipeline DSP
  blocks). It is a real, enabled feature with no representation, but it fails
  both bars: the `dynamic-speaker-optimization-amount`/`-speaker-interval` →
  MBC-band-0 threshold transfer is opaque (Dolby driver-size excursion model →
  no derivable mapping), it exists on a single device (unvalidatable), and a
  crude MBC band-0 limiter would add the pumping DSO is built to avoid. Left
  warned-at-parse, not mapped — the correct state. Net: the remaining fidelity
  work is device-gated *tuning* of plugins already in the chain ("Unvalidated
  converter scaling factors"), not new plugins.

[ee-conv]: https://github.com/wwmm/easyeffects/blob/dc14767e8bcf/src/convolver_zita.cpp#L103
[Filter.cpp]: https://github.com/lsp-plugins/lsp-dsp-units/blob/master/src/main/filters/Filter.cpp
