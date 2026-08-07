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
| Convolver (FIR peak-normalized) | 0 dB | `make_fir` divides the IR by its peak magnitude, so the convolver only ever attenuates and cannot clip on a boost-heavy curve — it is the first stage, fed at unity, with nothing but the −1 dBFS brickwall downstream. Being a scalar on the IR it is a constant dB offset at every frequency, so the correction *shape* is untouched; the level it removes is restored by XML-derived `volmax-boost`, not by an invented makeup gain. Present since `9eb5871`. |
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
null-sink route, compared as output integrated-LUFS / RMS-envelope trajectories.
Reproduce with [`tools/measure_pw/autogain_proof.py`](../tools/measure_pw/autogain_proof.py)
(`build` → `capture --side {ee,pw}` → `analyze`; captures stay untracked under
its `--out-dir`). Results:

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
Driver: [`tools/measure_pw/autogain_fullchain.py`](../tools/measure_pw/autogain_fullchain.py).

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

**Reproducing this PoC means rebuilding the chain from the parameters
above.** The run that produced the −56 dB figure was driven by hand and
no generator for it was saved, so unlike the rest of the measurement
work there is no script to re-run. `lv2apply` is not the route — it
segfaults on LSP plugins, which need the `work:schedule` feature only
EasyEffects' LV2 host provides — so a rebuild means assembling the two
`filter_stereo` stages and the Saturator as an EE chain and capturing
through `tools/measure_ee/`. Worth doing before this number is used to
justify shipping anything.

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
| 2 ✅ | Surround→stereo-base: `min(boost/20.0, 0.5)` — **REMOVED 2026-06-13**, the converter no longer maps `surround-boost` to any widening | `surround-boost` (1/16 dB) | the `/20` divisor and 0.5 cap were invented; Dolby surround is a spatial renderer, EE `stereo_tools` is a linear M/S balance | **resolved — widening dropped** (was: emitted when surround present, `surround-boost=96` on `dynamic`/`movie`) | **not testable on in-hand data:** the captured battery uses *correlated* pink (`stimulus_pink.wav`, corr +1.0, no Side), so the widener is a no-op — and indeed surr=96 (`dynamic`/`movie`) and surr=0 (`game`) loopbacks show identical residual side/mid (≈ −35 dB). EE half measured 2026-06: the live chain widens decorrelated pink by **+4.10 dB** S/M on `dynamic`/`movie` (surr=96 → stereo-base 0.5) and +0.02 dB on surr=0 profiles (analyzer `sm_delta_db`, which it previously skipped as an unknown kind). **DAX half measured 2026-06-13 (decorrelated pink captured on Windows): DAX applies essentially ZERO widening — surr=96 (`dynamic`/`movie`) S/M-delta is +0.01 dB, byte-for-band identical to surr=0 (`game`, +0.02) and to OFF (+0.02), flat across 250 Hz–8 kHz.** So our `/20` mapping adds +4 dB of static S/M width on the 2-channel speaker output that DAX does not produce — a clear over-application. Candidate fix: drop or sharply reduce the surround→`stereo_tools` widening (subject to the second-device bar). The phase-widening loophole is closed electrically too: on *correlated* pink (L/R corr +0.998, room to decorrelate) DAX holds the correlation at +0.997 (game and dynamic alike) — no inter-channel decorrelation, so not a phase/XTC widener either; our chain drops it to +0.991 (corr input) / −0.45 (decorr input). What a loopback still can't represent: crosstalk-cancellation's *acoustic* effect at the ears, or a virtualizer that's content-gated and dormant on stereo noise (engaging only on multichannel/object Atmos). Settling those needs a binaural capture / listening test, or an XML A/B with `surround-boost` edited to 0 (the risky single-block test) — but for the magnitude M/S rebalance our mapping actually performs, DAX demonstrably does nothing. **Provenance:** widening has shipped since 2026-02-28 (`82d7f3d`), but no DAX battery before 2026-06-13 (`measure_dax_3`) contained a decorrelated-stereo stimulus — the Apr/May sets were pink/sweep/multitone/stepped only (correlated pink = no Side, widener a no-op), so 2026-06-13 is the *first* DAX widening measurement; there is no earlier DAX stereo data to compare against. **Why DAX's effect is ~nil (leading hypothesis):** `surround-boost` is almost certainly a *virtualization/surround-render depth* parameter, not a stereo-width knob — it gates with `surround-decoder-enable` / `output-mode-partial-surround-virtualizer-enable` (an FFT-domain upmix→virtualize stage we already classify as non-modelable). Fed plain 2-channel PCM with no surround/object bed, that renderer has nothing to synthesise, so the boost scales ≈nothing: `movie` (boost=96) ≡ `game` (boost=0) to 0.01 dB RMS / ≤0.07 dB max in **both** L and R, not just in S/M. So our mapping is likely wrong *in kind* (a static width knob for what is really a multichannel-render gain), not merely over-scaled — and on the stereo playback path the converter targets, the faithful behaviour is to not widen. **Resolution:** the `surround-boost → stereo_tools` widening was removed from the converter (`make_stereo_tools`, the emission branch, the `surround` param of `make_preset`, and the `--disable stereo` flag are all gone); `surround` is still parsed and reported as intentionally-unmapped. This is a one-device decision (no second-device DAX capture), justified because it *removes* an unvalidated invented scaling that the only falsifying signal (a DAX capture) contradicted, rather than adopting a new mapping; the mechanism (render-depth param, dormant on stereo) is structural, not per-device. If a future device's DAX capture shows real widening, restore via git history (`82d7f3d`). **Validated on-device 2026-06-13** (re-captured the new no-widener chain through live EE): decorrelated-pink S/M widening dropped +4.10 → **+0.02 dB** on every profile (dynamic/movie/game), matching DAX's +0.01; correlated-pink fell +4.41 → +0.32 dB (residual = the device's L/R-asymmetric FIR/PEQ, DAX +0.12, not widening). No mono regression: the rest of the chain's preset JSON is byte-identical (preset-digest snapshot, now `tests/test_golden_preset.py`), and live mono-pink matched pre-fix within capture repeatability (~0.45 dB RMS), EE−DAX pink steady at 1.35–1.67 dB RMS (Finding-9 baseline). Verdict: adopt |
| 3 ✅ | Convolver SoundWire headroom restore: `peak_db * 0.5` — **REMOVED 2026-07-03**, the convolver emits 0 dB gain on every device family | (none — post-normalisation heuristic for the IEQ-only, no-AO SoundWire curve) | the 0.5 was chosen to "recover brightness"; not XML-derived | **resolved — restore dropped** (was: default audible on SoundWire) | **Provenance:** introduced `2f4d0b8` (2026-04-12, the first SoundWire user's PR) to restore "+6-7 dB" of the level FIR peak-normalization removed — but that large peak was an artifact of the pre-#13 chain over-applying `ieq-amount` 10× (fixed `eeecc4a`, 2026-05-28, on-device validated): after the fix the same formula self-scaled to ~+0.7 dB (issue [#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27)'s pasted generation runs: FIR peaks +1.1…+1.5 dB → restores +0.6/+0.7 dB), i.e. the restore was tracking the bug's magnitude, not a property of SoundWire curves. Field evidence: issue [#29](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/29) (Zenbook S14) found the SoundWire preset over-loud (reporter manually set −5 dB output). Removal follows the entry-2 precedent — it *drops* an invented non-XML gain rather than adopting a new mapping, so the second-device bar doesn't apply; loudness makeup is volmax-boost's job (XML-derived, entry on volmax slots). Not locally measurable (dev device is HDA); the #29 reporter's regenerate-and-listen is the field check. If a SoundWire DAX capture ever shows DAX applying net positive gain vs OFF that our chain lacks, restore via git history (`2f4d0b8`) |
| 4 | Regulator slope→ratio: slope read `/16` (`parse_xml`), then `ratio = 1/(1−slope)` (`make_regulator`) | `regulator-distortion-slope` | the `/16` reading is assumed by analogy to the dB fields; `1/(1−slope)` is inferred from how corpus values cluster | regulator only engages at high level | **not testable on this device:** the X1 Yoga is `distortion-slope=16` on *every* profile, so there is no operating-point variation to fit `1/(1−slope)`. Needs a device with differing slope values + a bass-burst capture comparing gain-reduction-vs-level (Phase 4) |
| 5 | Regulator timbre→knee: timbre read `/16` (`parse_xml`), then `knee = −6·timbre` dB (`make_regulator`) | `regulator-timbre-preservation` (corpus-frozen at 0.75) | the `−6` dB maximum knee is a pure guess; the field is constant across the corpus, so we have no signal to disambiguate | regulator, high level | **not testable on this device:** the X1 Yoga is `timbre-preservation=12` (=0.75) on *every* profile, so the `−6·timbre` scaling has a single operating point. Needs a device whose XML carries `timbre≠0.75`, plus a capture (Phase 4) |
| 6 | MBC ratio `1/(coeff/32768)` (`decode_mbc_bands`); time constants via Q15 with `block_size=256` → 187.5 blocks/s (`decode_mbc_time_constant`) | `mb-compressor-tuning` 6-tuples | the Q15 format and 256-sample block size are assumed from common DSP practice and only sanity-checked numerically, never measured | **dormant** — the MBC doesn't engage on the −10 dBFS test stimuli (Finding 3) | **Woken 2026-06-13** with `stimulus_stepped_loud` (−2 dBFS peak): comparing loud-vs-normal static gain (aligned @1 kHz), **DAX compresses far harder than our chain** — DAX −10.6 dB GR @234 Hz (EE −5.5), −10.4 @277 (EE −1.7), −5.9 @141 (EE 0), −7.4 @2.25 kHz (EE −3.2). Adaptive cross-pass span ≤1.5 dB, so this is the compressor/regulator, not the leveler. (Re-verified 2026-07-01 from the raw held-tone envelopes: at the 234/277 Hz diagnostic bands the within-tone drift is ≤0.16 dB and all three passes agree to ~0.03 dB, so the leveler's per-tone adaptation does not contaminate the GR readout there — the adaptation is visible only elsewhere, −1.1 dB early-tone at 141 Hz and a 1.5 dB cross-pass span at 3 kHz.) **Diagnosed 2026-06-13** ([`tools/measure_ee/dynamics_gap.py`](../tools/measure_ee/dynamics_gap.py); agent analysis, key numbers re-verified from the converter): the gap is **neither** the upstream bass-level gap **nor** a wrong MBC decode. (i) 1 kHz-referenced, the level each chain delivers to its dynamics agrees within ±3 dB at every diagnostic band (141–4193 Hz) — the "DAX delivers +16–22 dB more" reading was a reference artifact (our FIR is peak-normalised to a different anchor than DAX's OFF-flat baseline); the real 22–30 dB Finding-4 bass gap sits below ~120 Hz, pre-attenuated by the 100 Hz HP before either chain's dynamics. (ii) The MBC decode is internally faithful but *conservative*: a 3-level fit (−42/−18/−2) shows EE realises its nominal 1.67 ratio only at the one band that clears threshold well (234 Hz, R≈1.54); elsewhere the −6 dB soft knee + RMS detection keep it sub-slope. (iii) The actual driver is the **regulator under-engaging**: DAX's effective ratio at 234/277 Hz ≈ 2.95 = its near-100:1 regulator stacking on the MBC, yet our regulator — though it maps the same −10/−9/−8/−5 dB thresholds + slope (entry 11) — barely fires there. So the lever is the regulator, not the MBC ratio/threshold (which stays XML-derived and unchanged). See entry 11 |
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
   device's. Reproduce: [`tools/measure_ee/scaling_report.py`](../tools/measure_ee/scaling_report.py).
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

## Half the speakers, silently: a woofer pin the firmware hides (issue [#53](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/53))

The Lenovo Yoga 7 16IAH7 (82UF, ALC287 codec SSID `17AA:386A`) reporter filed
a working device report, then added that Linux drives only the tweeters until
they install a modprobe line. Their `--speaker-info` showed one internal
speaker pin (`0x14`); Lenovo PSREF lists the machine as "4 stereo speakers,
3W x2 (woofers), 2W x2 (tweeters)". The preset was being applied to half the
speaker set — a third member of the #33 family, where the fault sits a layer
below anything XML-derived.

Asked how they diagnosed it, the reporter — a self-described new Linux user —
said they deduced it from "the complete lack of bass" and reached the modprobe
line through "a lot of troubleshooting" with an LLM. No tooling told them, and
nothing in our output could have: that is the gap this closes. It also means
their fix was derived independently of the upstream commit below, which they
had not seen.

**Mechanism**, from upstream commit
[`b70f007a9fc6`](https://github.com/torvalds/linux/commit/b70f007a9fc6): the
BIOS reports pin complex `0x17` as unconnected, so the kernel configures only
`0x14` — "mono/tinny audio" in the commit's own words. The fixup
(`alc287_fixup_yoga9_14iap7_bass_spk_pin`) does two things, and the second
matters for triage: it rewrites `0x17`'s default config **and** reassigns DACs
(avoiding `0x06`/`0x08`, which have no volume controls, and pairing
`0x14`+`0x17` on DAC `0x02`). A `hdajackretask` pin override does only the
first half, so it is **not** a substitute — don't suggest one.

The commit also documents a matching detail worth carrying: SOF zeroes the PCI
subsystem id the HDA layer sees, so `SND_PCI_QUIRK` entries cannot match on
those machines and `HDA_CODEC_QUIRK` is required. `snd_hda_pick_fixup`
(`sound/hda/common/auto_parser.c`) confirms the shape — with a zeroed PCI id
every entry falls back to codec-SSID matching, and the lookup ends on a
codec-SSID pass regardless. `find_hidden_speaker_pin` mirrors exactly that, so
we never claim a match the kernel could not make.

**Two discriminators checked and rejected**, so they aren't re-litigated:

- *The DAX XML.* Re-derived over the full corpus (2837 XMLs, 2026-08-05):
  **all 4429 `internal_speaker` endpoints** carry `total_count="2"` +
  `has_subwoofer="0"` or `ch_count="2"`, with `<subwoofer-count>` `0`
  everywhere — zero exceptions, including the development device, which
  physically has `0x14`+`0x17`. Those attributes are logical channels, not
  drivers. Nothing in the repo reads them and nothing here reaches an emitted
  parameter, so the XML-only invariant is untouched: the quirk table is
  host-hardware data, the same category as `_KERNEL_SERIES_RELEASES`.
- *Raw pin scanning.* An output-capable pin with no default config is
  indistinguishable from a hidden woofer: the development machine has two
  such spare pins (`0x1b`, `0x1e`). They are printed as evidence under
  "HDA internal speakers" — never warned on.

**The manufacturer's spec is the discriminator**, and it settles which reports
are affected. Lenovo PSREF publishes static spec PDFs
(`psref.lenovo.com/syspool/Sys/PDF/<Family>/<Slug>/<Slug>_Spec.pdf`) that
extract cleanly; the `/Product/…` page is a JS app that fetches as an empty
shell. Validated in both directions against three machines known to expose two
pins — Yoga Pro 7 14ASP9 (#51), Yoga Pro 7 14APH8 (#30) and the development
X1 Yoga Gen 7 — all of which name woofers *and* tweeters. **Caveat: judge by
whether the line names woofers/tweeters, never by the leading count.** The
X1 Yoga reads "Stereo speakers, 2W x2 woofers and 0.8W x2 tweeters" — four
drivers behind a "Stereo speakers" prefix.

On that basis #53 is the **only** affected device in the tracker. Every other
single-pin report is a genuine 2-driver laptop: #33 and #18 (IdeaPad Pro 5
14APH8 / 14AHP9), #36 (14IMH9), #44 (Yoga Slim 7 14ARE05), #46 (T495) and #50
(Yoga 7 2-in-1 16IML9) all publish "Stereo speakers, 2W x2". In particular
**#50's missing `38dc` quirk-table entry concerns its smart amp, not a bass
pin** (PSREF does list "Smart Amplifier (AMP)" for it), and its pending
level-restore loud-content verdict is unaffected.

Design choices:

- **Table-driven, exact-SSID only.** The warning fires when the codec an
  upstream pin-adding fixup names is missing a pin that fixup declares. A
  sibling-SSID heuristic ("your neighbour model has a quirk") was considered
  and dropped — PSREF refuted the case that motivated it.
- **Which fixups qualify is derived, not listed.** A fixup counts when every
  pin it touches is an internal speaker (`0x9017xxxx`) and there are at most
  two — a surgical add, not a whole-machine pin remap, where "the quirk isn't
  applied" implies nothing about any one pin. That derivation found the class
  is far wider than the ALC287 Lenovos that prompted it: **53 machines across
  Lenovo, HP, Dell, ASUS, Acer, Medion, Infinix, MECHREVO and Lunnen**, from
  Dell Vostro subwoofers to HP Spectre x360 and ASUS ROG rear speakers.
  `HDA_FIXUP_FUNC` fixups run C the parser can't read, so their target pins
  are listed explicitly in the generator, each verified against the helper; an
  unlisted helper is simply uncovered, which is the safe direction.
- **The matching codec must own the pins.** A PCI-keyed entry identifies the
  *machine*, not a codec, so lending that id to whichever codec is being
  iterated let an HDMI codec with one spare output pin raise a warning naming
  the wrong codec — one no user action could clear. Two conditions fix it: the
  PCI id may only stand in for a codec that already owns speaker pins, and
  every pin the fixup declares must exist on that codec, configured or spare.
- **SOF is read from the card's driver field, not by substring.** The first
  version tested `"sof" in card.lower()` over each `/proc/asound/cards` line —
  and *microsoft* contains *sof*, so a plugged-in webcam or headset silently
  disabled the PCI-keyed half of the detector, making the same machine report
  differently between runs.
- **The modprobe line names the driver that owns the codec.** Picking whichever
  module merely exposes `hda_model` writes the option to a module driving
  nothing: SOF modules sit loaded beside `snd_hda_intel` on ordinary Intel
  machines, this one included.
- **Target the named pin, don't count pins.** The first draft fired when a
  codec had "fewer than two" speaker pins. That breaks on the wider family:
  several of these fixups declare a machine's *only* speaker pin (HP Spectre
  x360, ASUS ROG), so the count predicate would have kept firing forever
  after the user applied the fix, and would have skipped those machines
  entirely beforehand — a codec with no speaker pins never entered the loop.
  Node-targeting is correct in both directions, and it also sidesteps the
  ALSA control name, which varies ("Bass Speaker" on one machine, "Speaker
  Front" on #50's) while the fixup's effect does not.
- **A pin fixup is invisible in `/proc` — read the driver's override instead.**
  The first version classified pins from the `Pin Default` line of
  `/proc/asound/card*/codec#*`, which the kernel fills from
  `AC_VERB_GET_CONFIG_DEFAULT` (`sound/hda/common/proc.c`) — the *hardware*
  register, holding the firmware's own value. A fixup never writes that
  register: `snd_hda_apply_pincfgs` stores an override in `codec->driver_pins`
  (`sound/hda/common/auto_parser.c`), and only the driver-side lookup,
  `snd_hda_codec_get_pincfg`, consults it — which is why the fix works while
  the printed line goes on saying "not connected". So a machine that had
  applied the modprobe fix read exactly like one that hadn't: the warning
  would have fired forever, and step 2 of its own procedure asked the user to
  confirm something that could never happen — pushing them toward the undo.
  The fix is to merge `/sys/class/sound/hwC<card>D<addr>/driver_pin_configs`
  (and `user_pin_configs`, which outranks it, exactly as the kernel resolves
  them) over the printed value before classifying. Both files exist on every
  HDA codec — only `user_pin_configs` is gated behind
  `CONFIG_SND_HDA_RECONFIG` — and the card index plus codec address name both
  views, so the two line up without parsing either. `--speaker-info` tags such
  a pin `[kernel fixup]`, since a pin driven against the firmware and one the
  firmware declared are otherwise the same line, leaving the user's
  verification step nothing to look at. Reported state that exposed it: #53's
  own machine pasted three runs, one with the fix off and two with it on, all
  three byte-identical. **Confirmed on that machine** (2026-08-06): after the
  change, `0x17` reads `Bass Speaker Playback Switch (woofer, stereo) [kernel
  fixup]` and has left the unconfigured list — while the development machine,
  whose 0x1b/0x1e really are spare, still gets the plain "spare pins are
  normal" note. Both halves matter: the detector has to go quiet on a fixed
  machine without going quiet on an unfixed one.
- **Never substitute a related fixup's name.** Where the kernel gives a fixup
  no name in its models table, there is nothing a user can force and the
  message says so, offering the upgrade route alone. Borrowing the sibling
  IAP7 name for the Yoga 9 14IMH9 machines would set the pin and skip the
  Cirrus amplifier setup their chain also performs — a half-fix presented as
  a fix. This is why the table carries a `model` that can be empty.
- **The negative signal is collected too** (`unlisted_speaker_pin_finding`).
  The table only knows machines upstream has already been told about, so a
  hidden woofer nobody has reported yet is indistinguishable from a plain
  stereo pair — which is precisely the ambiguity that cost a triage pass on
  #33/#36/#44/#46/#50. When a machine shows exactly one speaker pin, has spare
  output-capable pins, and matches no fixup, an `ask` invites the one check
  only its owner can do quickly: does the laptop actually have more speakers?
  Gated tightly: spare pins alone are ordinary, and a matched quirk means the
  run already has a real fix to offer. The wording is bounded to what we
  actually do — suggest a modprobe setting to test. Not "we'll add the fix"
  (this project doesn't touch the kernel) and not "we'll get it upstreamed"
  (nobody here commits to that); a fix landing in Linux is what the generated
  table then picks up on its own.
- **No message asserts a pin count, and none promises a step it won't print.**
  The detector fires with one pin missing, with two, and with none configured
  at all, so wording like "one pin configured … a fix that configures a second
  one — the woofers" was false on machines the tests deliberately cover. For
  the same reason `--doctor` prints the procedure only where a forcible name
  exists, and the closing-block finding carries an ask only when a procedure
  was printed to ask about.
- **A check that can't print a command sends people away, so the printer had
  to change.** Both surfaces held their own copy of the fix, and `--doctor`'s
  ended in "Re-run without `--doctor` for the one-line modprobe fix" — because
  `emit_check` wraps a check's detail to the terminal, and a command folded
  across two lines does not run. That is a printer limitation, not a stance on
  what a diagnosis should contain: the same report happily gives *prose* fixes
  (the Background service check walks the reader through EasyEffects'
  preferences), since prose survives wrapping. `CheckResult.steps` now carries
  `(style, text)` lines printed verbatim, so a check states its whole fix
  in place — commands soft-wrap in the terminal, which still pastes. One
  builder (`speaker_pin_fix_steps`, the role `amixer_enable_cmd` plays for the
  amp gate) feeds both the end-of-run block and the check, and a test asserts
  the two print the same command. Doing this turned up a *third* copy: the
  converter had its own `emit_check` shadowing the shared one, which is why
  `steps` would have reached the PipeWire doctor and not this one.
- **A report may not contradict its own fix further down.** Role-played
  first-time readers caught two places where it did, both on an affected
  machine: `--speaker-info`'s pin list called the flagged woofer an ordinary
  spare ("spare pins are normal…"), and the layout estimate printed "2
  speakers → full-range stereo" — the exact count the warning above says is
  wrong. Read as the bottom line, each one talks the reader out of the fix
  they were just handed. Both sections are computed from *configured* pins,
  so both now ask the detector and mark what it flagged. The counterpart
  matters as much: on a machine with genuinely spare pins — most of them —
  the plain "spare pins are normal" note has to stay, or the section becomes
  a fault report about nothing.
- **The fixup's name is not the reader's model, and saying so is load-bearing.**
  `hda_model=alc287-yoga9-bass-spk-pin` on a Yoga 7 (upstream's own entry for
  this codec id reads "Lenovo Yoga 7 16IAP7") reads like someone else's fix in
  a line the reader is about to run with sudo — the one place a wrong-looking
  detail stops them. The step now says the name is the kernel's label and the
  match is by hardware id.
- **The confirmation is audible first, and hedged.** "Re-run `--speaker-info`
  and look for the tag" proves the kernel took the pin, not that anything
  plays; the tag exists because /proc can't show it (above). The pin usually
  drives woofers, but several fixups in the table declare a machine's *only*
  speaker pin, where the change is sound where there was none — so the step
  says hear a difference, *usually* the bass, with the tag as the mechanical
  cross-check.
- **Cost is scoped to the question.** The default run builds a
  `_gather_speaker_pins()` SpeakerInfo — a few /proc reads — rather than the
  full `--doctor` gather, which shells out to `amixer` per card and to
  `journalctl`/`dmesg`, and globs `/lib/firmware`, for an amp report a
  conversion never prints.
- **The generator checks each hand-listed helper, not just the table size.**
  Twenty rows hang off one `HDA_FIXUP_FUNC` helper; renaming it upstream would
  drop them all while the total stayed inside its rails and the weekly PR
  looked clean. The check runs against mainline only — a helper legitimately
  does not exist in releases older than the one that introduced it, which
  aborted the first real run until scoped.
- **`since` is a kernel version, not a boolean.** Three situations need
  different advice: no release carries the fix yet (#53's own case as of
  7.2-rc6 — merged for 7.2, so upgrading is a dead end today); a release
  carries it and the user is behind; or the user is *already past* it, where
  the fix is reaching them and something else is stopping it, so "upgrade"
  would send them after what they have. `upgrade_prospect` picks between them
  and is shared with `--doctor` so the two can't drift.
- **The modprobe module is derived, not hardcoded** — scanned from
  `/sys/module/*/parameters/hda_model`. SOF exposes it on
  `snd_sof_intel_hda_generic` today and on `snd_sof_intel_hda_common` before
  the generic split; the legacy path is `snd_hda_intel model=`.
- **Regenerated wholesale, weekly** (`tools/update_speaker_pin_quirks.py`,
  `.github/workflows/speaker-pin-quirks.yml`), unlike the append-only kernel
  table: entries can disappear upstream, and a stale one would tell a user to
  force a fixup their kernel no longer has. `since` is resolved by parsing the
  newest 12 release tags; an entry present in the oldest of them is recorded
  as "since that one", an understatement that can only make the advice more
  conservative. The script fails closed on a partial parse — which it did on
  first run here, correctly refusing to edit a table whose format had changed
  under it.

## A tuning pinned at the gain rail: the T495 (issue [#46](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/46))

The ThinkPad T495 report describes the preset as tinny, robotic, and
clipped/overblown at normal volume, with a clean `--doctor` (0 FAIL, 0 WARN,
right sink, preset loaded). The arithmetic below is all offline: it explains
what the chain does with this XML, and predicts which knob should move which
symptom. None of it has been heard on a device — the reporter's A/B decides.

**The values are the XML's own.** `gain_l` is
`0,192,185,72,17,-18,-115,-187,…`; the same file declares
`<geq_maximum_range value="192"/>`, so 192/16 = +12.0 dB is the largest gain
this file expresses, and 141 Hz sits exactly there on both channels (234 Hz too
on the right). The 1/16-dB scale is the one Finding 10 confirmed against DAX
captures on a simplified-schema device, and no clamp exists on our side — see
the corpus context in
[cross-device-findings.md](cross-device-findings.md#curves-pinned-at-the-declared-gain-range),
where this tuning is wider than 95% of simplified files at 23.7 dB p-p.

The tonal symptom and the loudness symptom have different sources:

- **The midrange hole is in the tuning, not in our filter design.** Relative to
  the 141 Hz rail the curve sits 19.0 dB down at 844 Hz and 23.5 dB down at
  1031 Hz, against −2.1 dB at 3750 Hz. Peak normalisation (`fir /= peak_mag`) is
  a scalar on the impulse response, i.e. a constant dB offset at every
  frequency, so it decides only where that spread sits — 0 dB → −23.5 rather
  than +13 → −10.5 — and cannot change the shape. A ~20 dB notch through the
  formant region is level-independent, matching the reporter's note that the
  character persists at an acceptable volume. Where normalisation *does* matter
  is downstream: with the whole curve anchored 13 dB lower, less signal reaches
  the regulator's thresholds, and `volmax-boost` is what puts the level back.
- **The boost lands where the regulator isn't.** This XML's `threshold_high` is
  0 dBFS on bands 0–3 and −6.4…−15.4 dB on bands 4–8, so per-band limiting
  covers roughly 469–1313 Hz — exactly the region the FIR already cut by
  10–23 dB — while the 141/234 Hz peak takes the +9 dB volmax boost straight
  into the −1 dBFS brickwall unprotected. `isolated_band` marks bands 0–3
  non-isolated, i.e. DAX couples them to the limited bands; that is the gap
  `--enable coupled-bands` addresses.

The sibling tuning is the control: `17AA5081` (T14 Gen 1 AMD, the T495's
successor, same ALC257 and same simplified schema) is far gentler in both
revisions we hold. The T495's own driver package carries `tuning_version` 2,
which *cuts* 47/141 Hz by 30 dB instead of boosting; every newer package in the
corpus carries `tuning_version` 4, which boosts 141 Hz by +6.5 dB for a 14.6 dB
spread. So this is a per-tuning outlier, not a schema or codec problem.

Note the file identity trap here: the same `SUBSYS` ships *different* tunings in
different driver packages, so "the XML for device X" is not well defined without
naming the package. Issue
[#45](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/45)
reported that machine working from `v7.623.439.38`, which we don't hold; the
packages we do hold bracket it (`v6.108.104.39` and `v9.1127.1236.0` both carry
`tuning_version` 4), so that reporter almost certainly ran the +6.5 dB revision
— an inference from the bracket, not a fact, and nothing here should rest on it.

The T495's own file, by contrast, was **never revised**: `17AA5125` is
byte-identical (md5 `d678efd7…`) in every package we hold, from `v5.204.651.25`
(2019) through `v9.1127.1236.0` (2024) to `v10.1022.826.17` (2025), at
`tuning_version` 50 throughout. So it is a long-lived, heavily-iterated tuning
rather than an early draft, and there is no newer Lenovo file for an affected
user to try — the packages we hold are:

| package | `17AA5081` | `17AA5125` |
|---|---|---|
| `v5.204.651.25` (the T495's own driver) | `tuning_version` 2 (−30 dB at 47/141 Hz) | 50 |
| `v6.108.104.39` | 4 (+6.5 dB at 141 Hz) | — |
| `v9.1127.1236.0` | 4 | 50 |
| `v10.1022.826.17` | 4 | 50 |

**Nothing else active in this file is silently dropped.** A field-by-field pass
over the built profile found the unmodelled blocks either switched off
(`bass-enhancer-enable`, `bass-extraction-enable`, `graphic-equalizer-enable`,
`volume-modeler-enable`, `process-optimizer-enable` — all 0; no DSO field at
all) or already accounted for: the surround stack is on (`surround-boost` +6 dB,
decoder, virtualizer angles) and deliberately not mapped (entry 2), and the
`virtual-bass-*` parameters are populated but carry no enable flag in this
profile, which is the open question in issue
[#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14).
Note `regulator-speaker-dist-enable` appears twice with different values — 0 in
`tuning-cp`, 1 in `tuning-vlldp` — and `parse_xml` correctly gates on the vlldp
one, so the regulator is read.

The one genuinely unmodelled thing that *is* active is **MI steering**: all five
`mi-*-steering-enable` flags are set. That matters for profile choice more than
for this device's tone. Per
[cross-device-findings §11](cross-device-findings.md#11-mi-steering--dynamic-profile-only),
MI steering is a `dynamic`-profile feature almost everywhere (3805/3825 rows) and
is "the key feature that the EasyEffects pipeline cannot replicate" — so our
"first profile" default systematically picks the profile whose Windows behaviour
depends most on what we cannot reproduce, and applies statically what DAX steers
by content. On this XML `music` switches all five off, along with the surround
decoder and the dialog enhancer, and drops the leveler from 7 to 4 — i.e. it is
the profile whose static translation is most faithful. Issue #29's reporter
independently preferred `music` on a different device. That is the real argument
for following `<default_profile>`, and it generalises beyond issue #46.

What shipped from this: the unlimited-boost warning, the `default_profile`
report (this XML declares `music`; we build `dynamic`), and the headless
EasyEffects probe fix. What deliberately did **not** ship is any knob that
scales or clamps the AO curve — that would be a hand-tuned offset, and the
in-GUI per-effect bypass already brackets the question (switching off
`convolver#0` isolates the curve from the dynamics without a rebuild). If the
curve is confirmed as the cause, the answer is to find what DAX does that we
don't, not a fudge factor.

## Giving back what normalisation removed: `--enable level-restore` (issue [#50](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/50))

The Yoga 7 2-in-1 16IML9 report describes the preset as quieter than bypass,
thin, and with the convolver specifically sounding muffled. Unlike the T495
above, this tuning is nowhere near the rail — its AO peaks at **+8.0 dB** —
which is what makes the mechanism visible on its own.

**Which reports this actually covers, and which it does not.** The
symptom — *quieter than expected* — is issue
[#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25)
(quieter than Windows, answered with `--enable autogain`) and this one (quieter
than bypass). **The T495 of issue
[#46](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/46)
is not in this class and must not be offered the flag**: that reporter's
symptoms are clipping, tinny and overblown, and they said the level itself was
fine. Their tuning peaks at the +12 dB rail on a band their regulator leaves
unlimited, so restoring it would drive the brickwall harder and make the one
thing they complained about worse. The two reports share a *mechanism* and not
a *symptom*, and conflating them would ship a fix as a regression.

**The arithmetic.** `make_fir(normalize=True)` divides each channel by its own
realised peak, so the emitted response is `combined(f) − max(combined) +
volmax`. Two different numbers fall out of that and they are easy to confuse:
the shortfall against what the tuning asks for is the **whole peak**, uniform
across the band, while `volmax − peak` is how far a band the tuning leaves flat
sits below *bypass*. On this XML `max(combined) = +9.21 dB` (3750 Hz,
`ieq_balanced`) against a `volmax-boost` of `+6.0 dB`: every band lands **9.2 dB
short of the tuning**, and an untouched band plays **3.2 dB below bypass** —
which is why the low end ends up under bypass outright:

| | 47 Hz | 469 Hz | 3750 Hz | 19688 Hz |
|---|---|---|---|---|
| tuning + volmax | +7.0 | +0.3 | +15.2 | +5.2 |
| what we emit | −2.2 | −8.9 | +6.0 | −4.0 |

The uniform −9.2 dB is consistent with the ≈−8 dB absolute EE−DAX offset
measured on the dev device (unvalidated-scaling entry 8), which that entry
records as "leveler boost + our −3 dB trim + convolver peak-normalisation,
inseparable".

**Why normalisation was right when it shipped, and why restoring is defensible
now.** Peak normalisation is not a decision anyone revisited and waved through —
it dates from the very first commit (`9eb5871`, 2026-02-27), and at that moment
it was the *only* thing standing between a boost-heavy curve and a clipped
output. Three things have changed since:

- **There was no limiter.** `1b14bc1` added the brickwall the day after
  (2026-02-28), and `5973326` the same day forced `convolver#0.autogain` off,
  killing the +50 dB re-normalisation. Restoring level into a chain with no
  final limiter would have clipped; into today's it does not.
- **The peak was twice as big.** Until `eeecc4a` (#12/#13) the converter read
  `ieq-amount` as `amount/10` and applied the IEQ at full weight. On the dev
  device that put the combined peak at **+20.1 dB** instead of +9.2 — so
  "restore the peak" would have meant handing back 10.9 dB more than it does
  now, on a curve that was itself wrong.
- **The boost lands somewhere safer.** `4213d5f` (#23, 2026-06-22) moved
  `volmax-boost` to the regulator's `input-gain`, so a static boost now passes
  the per-band limiter before the brickwall rather than after it. The restore
  inherits that placement.

So the original constraint was real and has since lifted. What kept it
unexamined afterwards is a separate failure, recorded in the absolute-offset
note below.

**Why this is not the fudge factor the section above declined.** The restored
amount is `make_fir`'s own returned `peak_db` — the exact scalar it divided
out, derived from the XML curve and nothing else. It is the identity, not a
proportion of it: the removed SoundWire makeup (unvalidated-scaling entry 3)
was `peak_db * 0.5`, a half-measure whose magnitude tracked the pre-#13
`ieq-amount` bug. It also does not touch the curve's *shape*, which is what
"scales or clamps the AO curve" would have meant.

**Placement.** It rides the same slot as `volmax-boost` (regulator
`input-gain` by default), so the per-band limiter sees it before the brickwall
— issue #23 measured that placement at 0.06% THD against 11.6% for the
post-band alternative.

**Channel re-referencing.** Normalising each channel to its own peak also
flattens the L/R level relationship the two AO curves ask for. Re-derived
2026-08-04 over the 3051 parsed corpus XMLs: the two combined-curve peaks
diverge on **19.1%** of files (median 0.93 dB, p90 2.62, max 5.56) — so on
roughly one device in five the default path shifts the stereo balance by an
amount the tuning did not ask for. Under the flag both channels are referenced
to the louder peak, so the relationship survives and no channel exceeds full
scale. Worth noting this is a property of the *default* path that the flag
happens to correct; on its own it is a candidate fix independent of the level
question, and it has not been heard either.

**The coupled hypothesis — proposed, then measured, then dropped.** The idea was
that since DAX applies the boost and catches it with per-band limits at 0 dBFS
while we treat a 0 dB `threshold_high` as inactive, restore-the-level and
treat-0 dB-as-a-real-limit were one question: restoring level without
`--enable coupled-bands` would feed the peak band straight into the brickwall,
and the flag would catch it. **The 2026-08-04 capture battery falsified that**
(measurements below): `LRC` and `LR` are identical to 0.00 dB in both fitted
gain and residual on every stimulus, and `coupled-bands` alone is bit-identical
to the default preset on five of seven. It cannot mitigate here, because after
+11.3 dB the in-band levels at the coupled bands are still under the 0 dBFS
threshold that would engage them. The extra level lands on the final limiter,
and nothing upstream catches it. The `boost-unlimited` warning still fires on
the flag-on path — the exposure is real — but its `ask` should not be read as
pointing at a fix that works.

**How a 12 dB offset stayed unexamined for months.** Not for want of absolute
tooling: `compare_ee_vs_dax.py --absolute` shipped 2026-06-12 (`7aefebd`), and
entry 8 names the trap outright — "the default 1 kHz normalization destroys
exactly this observable". The offset was measured twice, at −11.5 dB and then
≈−8 dB. Two things buried it anyway:

- **The flagship comparison was normalised.** Issue
  [#12](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/12),
  the project's most detailed EE↔DAX study, reports throughout as "EE − DAX,
  RMS dB, 200–18 kHz, **normalized at 1 kHz**". That view is built to find
  *shape* mismatches and it found a large one (the IEQ over-application, ~28 dB
  at 19.7 kHz). A broadband offset is exactly what it cannot show.
- **"Inseparable" closed the question.** Both absolute attempts were hunting a
  ~3 dB PEQ trim; the 8–11.5 dB offset was written up as the confound burying
  the target — "leveler boost + our −3 dB trim + convolver peak-normalisation,
  inseparable" — and no follow-up was scheduled. The word did the damage: it
  turned a measurement into a settled fact.

What made the term separable was not a better measurement but a switch. An
opt-in flag on one of three entangled terms converts an inseparable sum into a
controlled experiment. **When a note says "inseparable", that is a design task,
not a conclusion.** `compare_ee_vs_dax.py` now reports the absolute offset
unconditionally, in normalised mode too, so it cannot be mis-filed again.

**Why the warning's default gate did not move.** The obvious widening — warn
whenever the peak lands on a 0 dB-threshold band — reaches **54.4%** of the
3051 parsed corpus XMLs against the current gate's 10.6% (re-derived
2026-08-04), which is a nag rather than a warning. The reason it is safe to
leave alone: on the default path the peak-normalised band arrives at the
brickwall at exactly `volmax_boost` above bypass whatever its peak, so the AO
peak measures spectral contrast there, not drive. Worth recording about the
existing gate, though: only **172 of 3051** files declare `<geq_maximum_range>`
(30 of the 1661 that reach the branch), so "the boost reaches this XML's full
gain range" is in practice a comparison against our assumed +12.0 dB.

### Measured on the dev device, 2026-08-04

Full battery through the live-EE null-sink route (X1 Yoga G7, `17AA22E6`,
`dynamic`/balanced; restore = +11.3 dB, volmax +6.0, so the regulator
`input-gain` reads +17.3). Smoke gate PASS at −219 dB residual. Four presets:
default, `--enable coupled-bands`, `--enable level-restore`, and both.

**1. The level claim is confirmed, exactly.** Below the limiter the flag is a
pure broadband gain of precisely the amount `make_fir` divided out — fit gain
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
per-band on `pink`, absolute (not normalised), against the archived Windows
captures of the same XML and profile:

| | mean | median | range | mean abs |
|---|---|---|---|---|
| default, ch L | −12.09 | −11.82 | −17.65…−9.28 | **12.09 dB** |
| level-restore, ch L | −0.41 | −0.31 | −4.92…+2.15 | **1.27 dB** |
| default, ch R | −12.10 | −11.84 | −17.69…−9.29 | 12.10 dB |
| level-restore, ch R | −0.42 | −0.14 | −4.92…+2.14 | 1.27 dB |

This is what separates the term entry 8 called inseparable: the ≈−8…−12 dB
absolute EE−DAX offset is the convolver's peak normalisation, and giving it
back closes it to ~1 dB mean error on both channels. The two worst residual
bands are 141 Hz (−3.6) and 328 Hz (−4.9), both inside the regulator's active
range on this XML.

**But only at that input level — and this splits the gap in two.** Re-running
the current `analyze.py` over the *archived* DAX wavs (they were analysed
before `eq_gain_db_raw` existed; no recapture needed) recovers absolute data
for `pink_quiet` too, at −41.8 dBFS against `pink`'s −17.8 dBFS:

| stimulus (input RMS) | default mean abs | level-restore mean abs | restore mean |
|---|---|---|---|
| `pink` (−17.8 dBFS) | 12.09 dB | **1.27 dB** | −0.41 |
| `pink_quiet` (−41.8 dBFS) | 21.12 dB | **9.80 dB** | −9.80 |

The restore is worth the same ~11.3 dB at both levels, as it must be — it is a
static gain. What it cannot touch is the rest: at −41.8 dBFS a **9.8 dB** gap
survives. The cause is measurable directly from the same archive, as DAX's own
on-versus-off gain (100 Hz–10 kHz, `dynamic` − `off`):

| stimulus | DAX(dynamic) − DAX(off) |
|---|---|
| `pink` | mean **+7.05 dB** (median +5.62, −0.5…+18.5) |
| `pink_quiet` | mean **+16.39 dB** (median +14.75, +8.8…+27.0) |

DAX rides **+9.3 dB more gain on quiet content than on normal content**, which
is the 9.8 dB residual almost exactly. So the two gaps are independent and
additive: peak normalisation is a *static* offset that `--enable level-restore`
removes, and the volume leveler is a *level-dependent* one that only
`--enable autogain` can address. Neither flag substitutes for the other, and
this is the first measurement that separates them. It also puts a number on
what entries 7/10 call invented: DAX's leveler gain is +7.1 dB at −17.8 dBFS
and +16.4 dB at −41.8 dBFS on this device, two real points on a curve our
autogain currently approximates with chosen constants.

**3. The cost is on loud content, and nothing in the chain catches it.** The
limiter absorbs 0.05–0.72 dB, and on dense material the drive produces real new
frequency content, not just gain riding: on the multitone, energy at
non-stimulus bins relative to the tones rises from **−44.4 dB to −22.6 dB**.
The `pink` case is mild (0.05 dB absorbed); `sweep` and `multitone` are the
worst, as their sustained in-band energy is highest.

### Measured on a second device, 2026-08-04/05

The issue-[#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44)
device is **`DEV_0287_SUBSYS_17AA380D`** — the reporter's `--speaker-info`
reports `17AA:380D` and their run matched that XML. Identify a device by
(DEV, SUBSYS), never by the model folder a driver package was extracted into:
the first run of this A/B built `DEV_0230_SUBSYS_17AA3839` from the same
`Yoga-Slim-7-14ARE05` package — a different codec — and measured it against
DAX captures of `17AA380D`. Cross-tuning results are not evidence about
either tuning, and the numbers below replace them. The tell was available
offline: fitting each candidate's static AO+IEQ target against the DAX shape
gives 0.74 dB mean error for `380D` and 4.27 dB for `3839`.

**Its DAX side comes from the reporter's archive.** Those user-supplied
captures carry `off` baselines at both pink levels, and the same
re-analysis recovers absolute data from them. Two things come out:

| device | DAX gain @ −17.8 dBFS | @ −41.8 dBFS | slope |
|---|---|---|---|
| X1 Yoga G7 (`17AA22E6`) | +7.05 dB | +16.39 dB | +0.39 dB per dB |
| Yoga Slim 7 14ARE05 (`17AA380D`) | +9.13 dB | +25.25 dB | +0.67 dB per dB |

The level dependence reproduces, but **its magnitude and slope are
device-specific** — 0.39 against 0.67 dB per dB. One set of autogain constants
cannot fit both, which is the sharpest argument yet that entries 7/10 need
measuring rather than choosing, and a reason to run the ladder on more than one
machine.

The peak-versus-volmax mismatch reproduces too, across three tunings — though
its size varies, and on `17AA380D` the tuning's own `volmax-boost` nearly
covers it:

| tuning | combined peak | volmax | untouched band vs bypass |
|---|---|---|---|
| X1 Yoga G7 (`17AA22E6`) | +11.4 dB | +6.0 | −5.4 dB |
| Yoga Slim 7 14ARE05 (`17AA380D`) | +8.4 dB | +7.0 | −1.4 dB |
| Yoga 7 2-in-1 16IML9 (#50) | +9.2 dB | +6.0 | −3.2 dB |

**Why another device can be measured here at all.** The null-sink route taps
`ee_soe_output_level`, a digital tap ahead of the speaker (smoke gate: 0.00 dB
gain, −219 dB residual), and the Windows side is a loopback of the engine mix
bus. Neither capture contains the transducer, so building someone else's XML
locally and running the battery is a genuine measured A/B of the *chain* — the
only thing it cannot speak to is how that chain sounds through their speakers.
A second device therefore costs their XML plus one archived DAX battery, not
their laptop. What it does **not** buy is directly comparable absolute levels;
that needs the referencing below.

**Measured on the second device, 2026-08-05.** The `17AA380D` XML was built
here and run through the same battery. Restore on that tuning is +8.40 dB.

| | dev `17AA22E6` | second `17AA380D` |
|---|---|---|
| EE−DAX, default | −11.18 dB | −5.65 dB |
| EE−DAX, `level-restore` | **+0.10 dB** | **+2.08 dB** |
| mean abs error, default → restored | 12.09 → 1.27 dB | 5.65 → 2.95 dB |
| shift achieved vs asked | +11.7 / +11.3 | +7.7 / +8.4 |

(100 Hz–10 kHz, `pink`, each DAX side referenced to its own bypass capture.)

**A partial confirmation, not a clean one.** The flag improves absolute
agreement on both devices — 12.09 → 1.27 dB of mean error on the dev device,
5.65 → 2.95 dB on the second — but it only *lands* on the dev device. On
`17AA380D` it overshoots by +2.08 dB, because that tuning's own `volmax-boost`
(+7.0 dB) already covers most of its +8.4 dB peak, leaving just −5.65 dB to
recover. Restoring the full peak there is too much by about the amount volmax
was already contributing. The mechanism is sound on both — at the quiet level,
where nothing limits, the second device delivers +8.40 dB against +8.40 asked,
exactly — but "restore the whole peak" is evidently the right *size* only when
volmax is not already compensating. That is an argument for keeping the flag
opt-in, and a hint that the eventual default may need to be
`peak − volmax`-aware rather than the raw peak.

**What the level gap does to the regulator — and what it does not explain.**
Re-derived 2026-08-05 from the dev-device `pink` captures by integrating
in-band power between band edges (geometric means of adjacent centres) and
comparing each band's absolute level against its own `threshold_high`:

| 47 / 141 / 234 / 328 Hz | level (dBFS) | headroom to threshold |
|---|---|---|
| EE default | −59.8 / −34.9 / −27.2 / −34.0 | mean **−30.96 dB** |
| EE `level-restore` | −47.4 / −23.7 / −16.0 / −22.8 | mean **−19.46 dB** |
| DAX `dynamic` | −41.1 / −20.5 / −15.0 / −19.3 | mean **−15.99 dB** |

Our default chain runs **14.97 dB below DAX** across the four threshold-active
bands (range 12.11–18.70); with the flag that closes to **3.47 dB**
(0.91–6.33). At 234 Hz specifically the restored chain sits 1.0 dB from DAX.
So `level-restore` does not merely match DAX's output level — it puts our
regulator on roughly DAX's footing, meaning it would begin to engage on
roughly the content DAX's engages on.

**It does not, however, explain the entries-6/11 under-engagement, and the
tempting inference that it does is wrong.** On this stimulus **neither side
crosses a threshold**: 0 of 4 active bands are above threshold for EE default,
EE restored *and* DAX (DAX's closest approach is −7.04 dB). A dormant
compressor cannot exhibit a ratio, so `pink` at −17.8 dBFS carries no
information about the 100:1-realises-as-1.8 finding either way — that figure
came from a *loud* stepped capture where both sides did engage, and nothing
here touches it. What this does establish is narrower and still useful: at
nominal level our regulator idles 31 dB under its own thresholds, so any
attempt to characterise it on ordinary-level content is measuring silence.

**Caveats on what these runs can and cannot say.** Both measured tunings put
their AO peak on a band their regulator *limits* (234 Hz on each), where issue
#50's sits on one it leaves open — so nothing here tests the
boost-into-brickwall shape that made #50 report crackling. Both also have
channels whose combined peaks are equal, so the L/R re-referencing path ran
with nothing to correct and stays untested on hardware. The EE−DAX tables rest
on the `pink` stimulus at two levels; `multitone` and the sweeps carry no
absolute data on the DAX side (`tones`/`ir` npz store normalised amplitudes,
so `--absolute` skips them by design, not by omission).

**Status: opt-in, measured on two devices, not yet heard.** Default output is
unchanged and pinned by the `level-restore-available-but-off` golden digest.
The second-device requirement in the XML-only bar is now met, so the remaining
gate is a listening pass — specifically on loud, dense material, which is where
the limiter measurably works hardest and where neither device has been
auditioned. The dev-device listening check on 2026-08-04 confirmed "noticeably
louder" but could not reach high volume. Until that is closed the flag stays
opt-in, and the loud-content cost is an argument for leaving it there even
after.

## Splitting the single-file scripts

Both entry points have outgrown one file — `wc -l` said 8,107 and 2,220 the day
this was written, and the whole point of the exercise is that those numbers go
down, so re-run it rather than quote it. The `lib/` package (see
[reference.md](reference.md) "Repository layout") is the destination; this
section records the target shape and, more importantly, the git discipline
that decides whether the result is still readable a year later.

### Why the entry points don't move

The tempting version of this refactor moves the 8,100-line body wholesale into
`lib/`, because a whole-file `git mv` is a rename git detects at 100% —
`git log --follow` and GitHub's web blame both track it, and the bulk of the
history survives the move intact. It was rejected: those three root paths are
quoted in the README, the issue template's paste-this commands, the
`register-python-argcomplete` line, `tools/extract_claims.py`'s `SCRIPTS`
table, the `.claude/rules/*` path globs, relative hyperlinks in
`docs/ee-to-pipewire.md` and `tools/measure_pw/README.md`, the emitted
`_generator` / `# Generated by` stamps, and — until `lib/data/` took the
tables off them — the two weekly workflows that `git add` the generator by
name. A rename buys history for the body and spends it across all of that.

So the root files stay and shrink. The cost is real and worth naming: code
leaving them is an *extraction*, not a rename, so git only finds its origin
with `-C`, and GitHub's web blame won't. That is what the rules below exist to
mitigate.

### The rules that keep provenance

- **A move commit moves only.** No reformatting, no renamed functions, no
  behaviour change alongside it. Rename detection needs ~50% similarity;
  copy detection (`-C`) needs the moved lines to be *unchanged*. Tidy-ups go
  in the commit after.
- **One commit per extracted module** — for readability, not for `git blame`.
  The blame argument was tested and does not hold: extracting `lib/preset/fir.py`
  alone, in its own commit, recovers exactly the same 5 lines of 96 that it does
  when it shares `d498e4d` with two other modules. Commit granularity is not the
  lever. Keep the rule anyway, because a reviewer reading one module's move is
  better served than one reading three. What *is* a lever is the next rule —
  splitting each slice into a seed and a trim, which this bullet used to reject
  on the theory that "one commit that removes the lines and adds them under
  `lib/` is the shape `-C` is looking for, and the separate copy commit buys
  nothing". Measured, it buys nearly all of it.
- **Fan out, then trim** — two commits per slice, and the mechanic the rest of
  this section leans on. The **seed** adds every path the slice creates, each
  holding a byte-identical copy of the root script it comes from, and touches
  nothing else. The **trim** cuts each copy down to its real contents, strips
  those lines out of the root script, and carries the slice's test, tool and doc
  edits. What this buys is a **copy edge**: git pairs a new path against an old
  one by similarity, and a flat extraction hands that pass a hard problem —
  ~800 added lines against an 8,000-line file that shrank in the same commit —
  which it solves winner-takes-all and unpredictably (the table below). An
  exact-content pairing is decided by blob hash instead and cannot fail. Storage
  is not a consideration: the four paths seeded in `330d351` are one 7,904-line
  blob under four names.

  The seed is deliberately *not* `git rm`-then-copy. Deleting the root script
  first scores marginally better — 100% of moved lines traced against 97% — but
  leaves a commit with no entry point, so a `git bisect` landing there has no
  script to run and no suite to run it with. The healthy-tree variant keeps
  every commit runnable: nothing imports the copies, and each seed commit's
  pass count is exactly its parent's.

  `tools/check_move_purity.py` belongs on the **trim** commit. The seed adds
  lines it never removed, which is impurity by that tool's definition and by
  design.

  **Extending a module that already exists needs a seed too — from its own
  earlier seed blob.** Moving code into an existing path pairs with nothing, so
  it lands at 0%: the slice-5 commit put 350 lines into `lib/report/findings.py`
  beside six freshly seeded modules, and those 350 traced at 0% while the six
  traced at 91–99%. The fix is not to seed from the *current* root script —
  that would recover the 350 and spend the lines already in the file, since the
  root script no longer contains them. Seed from the blob **that path held at
  its own first seed**: a full copy of the root script as it stood *before* any
  of it was extracted, so it contains both the code already in the module and
  the code about to arrive. Re-seeding `findings.py` from its slice-3 blob took
  it from 0% to 348/350 with the existing lines untouched at 99%.

  The general form: a seed's job is to give the destination path a parent
  holding every line the commit is about to put there. Reach back to whichever
  blob satisfies that, which for a second helping is not the newest one.

  Worth knowing before hand-building a seed: the loop that writes the copies
  runs under zsh, which does **not** word-split an unquoted `$paths`. Getting
  that wrong produces one file whose name is every path joined by spaces, a
  seed that pairs with nothing, and a trim commit that quietly deletes the
  evidence — the tree ends up correct and the provenance silently does not.
  Check the seed with `git ls-tree -r --name-only <seed> -- lib/`.
- **Name both ends in the subject** — "Move the console into `lib/console.py`",
  not "Tidy up the console". It matters most away from the command line:
  GitHub's web blame implements no `-C` at all, so in a browser the subject *is*
  the trail. One consequence of the seed/trim pair: `git log --diff-filter=A --
  lib/console.py` now lands on the **seed**, not the move, so the seed's subject
  has to name the destination and its body the source — and the move, which
  names both ends, is its immediate child.
- **Never rebase, squash or reformat across a move commit.** Squashing a move
  into its neighbour merges the two haystacks back together and a reformat
  rewrites the very lines `-C` matches on. Either one spends the provenance the
  commit was shaped to keep, and neither is recoverable after the fact. The
  narrow exception is the retrofit that introduced the rule above — inserting a
  seed ahead of a trim whose tree is unchanged, on unpushed commits, gated on
  `git diff <pre-rewrite tip> <new tip>` printing nothing.
- **The incantations, measured rather than assumed.** Four already-landed slices
  were re-shaped as seed/trim pairs and every row re-measured on the same paths
  either side, so the difference is attributable to commit shape and nothing
  else:

  | command | flat extraction | seeded as a copy |
  |---|---|---|
  | `git log --follow -- lib/preset/fir.py` | **no** — stops at the move | **yes** — 267 commits, with a caveat below |
  | `git log -C -C --find-copies-harder -- <path>` | **no** — stops at the move | no — stops at the seed |
  | `git blame -C -C -C lib/preset/fir.py` | 5 lines of 96 | 47 of 96 |
  | `git blame -C -C -C lib/dax/parse.py` | 10 of 783 | 675 of 783 |
  | `git blame -C -C -C lib/hardware/sinks.py` | 0 of 306 | 214 of 306 |
  | `git blame -C -C -C lib/console.py` | 77 of 119 | 85 of 119 |
  | `git log -S '<a line of the moved code>' --all` | **yes** — lands on the move *and* the commit that first wrote the line | yes |
  | `git log -- dolby_to_easyeffects.py` | yes — every pre-move commit is still there, just not linked to the new path | yes |

  Whole-file counts flatter the flat column and punish the seeded one, because
  blanks, the new module docstring and the new import block can never trace.
  Counting only lines that provably moved — verbatim in the parent's root
  script, >20 non-space characters — the ten modules go from **791 of 1,618
  traced to 1,562: 49% → 97%**.

  `--follow` needs its caveat. It reaches past the seed because the copy is
  whole-file, so what it walks into is `dolby_to_easyeffects.py`'s entire
  267-commit log — it now answers "where did this file come from", not "who
  wrote this function". `git log -C` still stops, because `-C` annotates a diff
  and only `--follow` walks a path across a rename or copy. So the **pickaxe**
  (`git log -S` on the line itself) stays the precise route from a line in
  `lib/` to its origin; `--follow` went from misleading to merely coarse.

  What the seed actually fixes is variance, not a uniform deficit. Flat
  extraction is not always bad: `lib/hardware/speakers.py` traced 712 of 759
  lines unaided, because its commit removed 1,380 lines from the generator and
  the similarity pass found the block. `lib/dax/parse.py` and
  `lib/hardware/sinks.py`, the same shape in the same kind of commit, scored 10
  and 0 — and there is no threshold to tune, `-C1` changes nothing. Depth of
  history is a second factor (`make_fir` dates from `9eb5871`, the initial
  commit, so blame carries it through every intervening rewrite of an
  8,000-line file), which is why `fir.py` recovers 47 of 96 rather than all of
  them. Four modules gain nothing: on moved lines `lib/data/*` and
  `lib/hardware/codecs.py` were already at 99–100% and stay there, and
  `lib/hardware/amps.py` gives back one moved line (six by whole-file count,
  169 → 163) where the copy edge outbids a pairing that already worked.
  Slices 5 and 6 use the seeded shape regardless — a recipe that is
  right 97% of the time on every file beats one that is right 99% or 0%
  depending on the file, and only says which afterwards.
- **What this means for the subject line.** It is not a nicety, it is the
  primary trail — the only one that works from a browser, and the one that
  works from the command line without knowing a line of the moved code in
  advance. `git log --diff-filter=A -- <path>` lands on the seed and the
  extraction is its child, so the pair has to read as one operation without a
  diff open.
- **Verify on the real commit before pushing:** `tools/check_move_purity.py` on
  the trim commit for the motion itself, and `git blame -C -C -C lib/<new>.py`
  to see how much provenance survived. With the seed in place that is most of
  the moved lines, so a low count now means the copy edge did not take — not
  that the code is old.
- **Re-anchor `__file__`-relative paths when they move.** `lib/version.py`'s
  `Path(__file__).resolve().parent` still works (git walks up to find the
  checkout), but `ee_to_pipewire.py` builds
  `SCRIPT_DIR / "tools" / "measure_pw" / "validate_conf.py"` assuming the repo
  root. That shared anchor now exists — `lib/paths.py`'s `REPO_ROOT`
  (`63449ef`), which the converter's `validate_conf.py` lookup already resolves
  through. Route the next one through it rather than adding a `parent.parent`.
- **`lib/__init__.py` stays empty of code.** A convenience re-export there
  would drag the package in behind any single import and hand every future
  module a ready-made cycle (`tests/test_layout.py` enforces this).

The first of those rules is now mechanised. `tools/check_move_purity.py`
(`1e414de`) reads a commit's diff and asserts a subset relation: every line the
commit adds under `lib/` must appear verbatim among the lines it removed from
the root scripts. Three kinds of added line are exempt, because a freshly
extracted module cannot avoid them — its module docstring and its top-level
import block, both located with `ast` rather than guessed at from where prose
seems to end, and blank lines, which carry no provenance. Everything else (a
new comment, an `__all__`, a re-indented body) is reported with file, line and
text, and a violation that differs from a removed line by whitespace alone says
so — invisible in a diff, still fatal to `-C`. The reverse direction, source
lines that never reappear under the target, is deletion rather than motion: it
is printed and never fails the run. It checks `HEAD`, a named commit,
`--staged` (the gate to run before `git commit`) or `--worktree`, and exits **0
pure, 1 impure, 2 could not check** — where an untracked file under `lib/` is a
2 rather than a pass, since a module that appears in no diff is one the tool
never read.

Its one non-obvious choice is worth recording, because the instinct is the
opposite: it asks git for the diff with rename detection **off**
(`--no-renames`). With detection on git is helpful in exactly the wrong way —
it recognises the move and collapses it to the handful of lines that differ, so
`_doctor.py => lib/doctor.py` in `16c4723` shows up as a single changed line.
The checker would then pronounce a 115-line move pure having inspected one line
of it. Purity is a claim about the whole file, so the tool has to be shown the
whole file.

### Target shape

A module lands flat in `lib/` first; a subpackage is created when its third
member arrives. Line ranges are where the code sat when this was written.

```
lib/
├── console.py  version.py  ee_paths.py  doctor.py  paths.py       (already moved)
├── data/            machine-written tables, out of hand-edited code
│   ├── kernel_releases.py       _KERNEL_SERIES_RELEASES
│   └── speaker_pin_quirks.py    _SPEAKER_PIN_QUIRKS
├── hardware/        system probing — no DAX, no DSP
│   ├── codecs.py    HDA / SoundWire / PCI subsystem ids             (184–312)
│   ├── speakers.py  SpeakerInfo, pin config, firmware gates         (313–1390)
│   ├── amps.py      smart-amp evidence                             (1391–1600)
│   └── sinks.py     pw-dump enumeration + selection                (3334–3600)
├── dax/             everything that reads Dolby XML
│   ├── discover.py  autoprobe, driver store, find_tuning_xml       (2714–3280)
│   └── parse.py     parse_xml, ParsedTuning, resolvers             (3280–4620)
├── preset/          EasyEffects preset construction
│   ├── fir.py       interpolate_curve_db, make_fir, save_wav       (5036–5107)
│   ├── bands.py     make_band / shelf / hp / lp / peq              (5108–5356)
│   ├── plugins.py   dialog, autogain, MBC, regulator, bass, limiter (5357–6054)
│   ├── build.py     make_preset                                    (6376–6543)
│   └── autoload.py  write_autoload, EE rc, bypass preset           (3601–3790)
├── report/          everything the user reads
│   ├── findings.py  Finding + factories + print_ask                (3859–5035)
│   ├── messages.py  print_what_now, print_troubleshooting          (6055–6375)
│   └── environment.py  --doctor checks, on doctor.py's vocabulary  (1896–2713)
└── pipewire/        ee_to_pipewire internals
    ├── plugins.py   EE plugin → LV2 translation
    ├── conf.py      filter-chain conf emission
    ├── install.py   install / pin / restart
    └── checks.py    PW-side --doctor                            (conv 1286–2000)
```

Each entry point keeps its argparse builders, `main()`, and the orchestration
that reads as the program — roughly 800 lines for the generator.

Two things the table above once assigned to `console.py` stayed behind when it
moved. `_load_dsp` binds `np` and `wavfile` into the *generator's* globals,
which is where every `np.` in that file looks them up, so relocating it would
break those lookups and hand a module the converter imports at startup the
numpy dependency `test_converter_startup_pulls_in_no_dsp` exists to keep out.
And `console.py` is the one module in `lib/` allowed to import `rich`: the
optional presentation dependency is contained there rather than spread across
whatever else gets extracted, which is why it is absent from
`tests/test_layout.py`'s `STDLIB_ONLY` list while still bound by the DSP rule.

Slice 5 landed the `report/` and `preset/` halves of that tree with two
deviations from it, both forced rather than chosen.

**The `--disable`/`--enable`/experimental menus went to `report/messages.py`,
not `preset/plugins.py`.** They are read twice: by `print_troubleshooting`,
and by `add_filter_tweak_args` while argparse builds `choices` — which happens
on the completion path, where `_load_dsp` has deliberately not run. And
`plugins.py` reaches numpy (`decode_mbc_time_constant` wants `fir.SAMPLE_RATE`
for the block rate), so a generator that imported it at the top to read a
symptom string would put 0.4 s on every TAB press and fail
`tests/test_completions.py::test_completion_path_skips_the_dsp_import`. The
tables are copy anyway, in the same register as the block that prints them, so
the home the constraint picked is the one the content wanted. `plugins.py` and
`build.py` are consequently bound in `_load_dsp` beside `fir`; `bands.py` and
`autoload.py` are stdlib-only and imported at the top like everything else.

**`--doctor` split along its own comment line rather than at the section
boundary.** The banner over that block already said it: *"the pure helpers
below take plain inputs so they're unit-tested without touching the system;
the `_probe_`/`_gather_` wrappers do the I/O."* The pure half — every
`*_status` verdict and the shared message builders, which is where the copy
is — is what moved to `report/environment.py`. The I/O half stayed, because
each piece of it reads something that has not moved yet: `_probe_ee_version`
and `warn_ee_environment` want the `_USE_FLATPAK` / `DEFAULT_*` install-path
constants, `_gather_doctor_report` wants `_gather_speaker_info`,
`_print_doctor_report` wants `_print_speaker_info`, and `speaker_pin_status`
wants the pin-fix step builders — all of them generator residents that the
hardware slice left behind. Carrying them anyway would have meant re-pointing
calls inside a moved body, which is exactly what `-C` cannot follow. They come
free once `_gather_speaker_info` / `_print_speaker_info` reach `lib/` and the
EE install paths resolve through `lib/ee_paths.py`.

The dividend of that second choice is measurable: the suite's
`monkeypatch.setattr` inventory is **byte-identical before and after the
slice**. Not one patch target moved, because every patched symbol
(`_probe_ee_version`, `warn_ee_environment`, `_print_speaker_info`, and the
`subprocess`/`shutil` module objects the probes reach through) is on the I/O
side that stayed. What did change is which module the tests *import* the moved
names from, which no `test_no_test_patches_a_re_exported_name` failure can
hide behind.

Slice 6 landed `lib/pipewire/` and emptied the converter — 2,133 lines down to
483, of which the argparse builders, the completers and `main()` are all that
is left. The four modules came out as planned and stack strictly:
`plugins.py` (an EE plugin block → its LV2 node, plus the `EE_*` label tables)
← `conf.py` (`build_chain`, `emit_links`, `format_conf`, the SPA-JSON writer)
← `install.py` and `checks.py`, neither of which the other imports. Four
deviations, each forced by something:

**`DEFAULT_OUTPUT_DIR` went to `checks.py`, not `install.py`.** It is the one
constant `tests/test_pw_doctor.py` patches — three sites, to point the doctor
at a `tmp_path` — and the bodies that read it are `check_conf_directory`,
`gather_pw_doctor` and `report_pw_doctor`, all of them moved lines that may
not be re-pointed. Had it lived in `install.py`, `checks.py` would have held
its own `from … import DEFAULT_OUTPUT_DIR` binding and the patch would have
needed to name `checks` anyway — two bindings for one constant, which is the
stale-binding hazard in miniature. Four of its six readers are in `checks.py`;
the two in the root script (the `--output` help text and `main`) re-point for
free, as every root-side call site does.

**`warn_if_stacked` went to `checks.py` too**, despite firing at write time
rather than at `--doctor` time. It reads `installed_confs`, and it is
`check_stacked_chains` asked of the confs on disk instead of the live graph;
its four tests already live in `test_pw_doctor.py`. The alternative — leaving
it beside the other end-of-run messages — would have made `install.py` import
`installed_confs` from `checks.py` while `checks.py` imported
`DEFAULT_OUTPUT_DIR` back from `install.py`. That cycle is the reason, not the
tidiness: with `warn_if_stacked` moved, the four layers have no way to form
one.

**`PluginHandler` and `EE_KEY_DISPATCH` stayed with the emitters** while
`build_chain` went to `conf.py`. Both readings are defensible — the table is
"the driver" by the old section banner and "the translation index" by content
— and the tiebreak is arithmetic: `conf.py` importing one name
(`EE_KEY_DISPATCH`) beats `plugins.py` importing eight emitter names back.

**The converter imports its own `conf` module as `pw_conf`.** `main()` binds a
local named `conf` for the rendered text, three lines before it writes it out.
Second alias of the split after `hardware_sinks`, and the same comment shape
above it.

Two smaller things worth having recorded. `lib/pipewire/*` may not import
`lib/preset/*` — `plugins.py` and `build.py` there reach numpy through
`fir.py`, and `test_converter_startup_pulls_in_no_dsp` asks the question from
the converter's end, so the failure would be immediate but the reason
non-obvious. And `lib/pipewire/plugins.py` carries a comment naming a
`localresearch/` path (the LSP source tree the enum tables were read out of),
moved verbatim because a move commit cannot also fix it; it wants the tidy-up
commit after.

Unlike slice 5, this one *does* move patch targets — that dividend was
specific to `--doctor`'s pure/IO seam. Twenty-one `monkeypatch.setattr` calls
aimed at the converter across three test files reduce to six distinct names,
all retargeted: `_pw_dump`, `_wireplumber_version`, `_plugin_presence`,
`DEFAULT_OUTPUT_DIR`, `_UNSCANNED_CONF_DIR` and `report_pw_doctor` to
`lib.pipewire.checks`, `_validate_conf` and `_autodetect_speaker_sink` to
`lib.pipewire.install`, with `main` alone staying on the root module. A green
suite does not prove a retargeted patch still bites — a patch that reaches
nothing leaves a passing test that quietly ran the real code — so each was
checked by breaking the production function and confirming the test stayed
green.

### The EasyEffects install paths, and the duplicate that was real

The second wave opens with the smallest step there is: the `DEFAULT_OUTPUT_DIR`
/ `DEFAULT_IRS_DIR` / `DEFAULT_AUTOLOAD_DIR` / `DEFAULT_EASYEFFECTS_RC` block
left the generator for `lib/ee_paths.py`, where the base it derives from
already lived. It goes first because it is what the slice-5 note said the
stranded `--doctor` I/O half was waiting on, and because the readers that
*can't* move — `add_autoload_args`, `add_output_args`, `_complete_preset_names`
and `main` — re-point for free from a root script.

Two constants named `DEFAULT_*_DIR` looked like the same duplication and were
not:

- `lib/pipewire/install.py`'s `DEFAULT_IRS_DIR` was a genuine second
  definition — the same `easyeffects_base() / "irs"`, written twice — and is
  gone. The converter's `--irs-dir` default now reads the generator's
  attribute, which is what `tests/test_ee_to_pipewire.py`'s
  `test_irs_default_matches_the_generator` had existed to police.
- `lib/pipewire/checks.py`'s `DEFAULT_OUTPUT_DIR` shares a name with the
  generator's and nothing else: it is `~/.config/pipewire/pipewire.conf.d`,
  PipeWire's drop-in directory, against the EasyEffects preset directory under
  `~/.local/share`. Collapsing the two would have redirected every conf this
  repo writes. It stays exactly where slice 6 put it, patch target included.

That test survives rather than being deleted, because the layer where drift is
still possible moved up: the constants can no longer disagree, but a `--irs-dir`
given a default of its own can. It now asks both parsers, and asserts
**identity** with `lib.ee_paths.DEFAULT_IRS_DIR` rather than equality — on a
native-install machine a hardcoded native path compares equal to the right
answer while being wrong for exactly the Flatpak users the original bug hit.
Checked by re-introducing both failure shapes (hardcoded, and re-derived
through `easyeffects_base()`) and confirming it fails on each.

This slice is deliberately **not** a seed/trim pair, and it is the counterexample
worth having next to the rule above. Seventeen lines of constants sit under the
threshold where a copy edge earns its commit — and the seed could not have
recovered much of them anyway, because the move is not verbatim: the block's
private aliases (`_EASYEFFECTS_BASE`, `_USE_FLATPAK`, `_FLATPAK_APP_ID`) become
the module's own public names, so `check_move_purity.py` reports the derivations
as changed rather than moved, correctly. The provenance that mattered here was
never `git blame`'s; it was proving the resolved paths didn't move, which is a
`git worktree add --detach <scratch> HEAD` copy printing every constant either
side.

### DAX source discovery, and a list that could not take the module

583 lines — `DOLBY_FILENAME_RE` through `find_tuning_xml`, the whole answer to
"where are the tunings and which one is this laptop's" — left the generator for
`lib/dax/discover.py`, beside the `parse.py` that takes over once a path is
picked. The cleanest large extraction of the wave: `symtable` over the block
says it reaches exactly six names it does not define — `ET`, `Path`, `os`, `re`,
plus `console` and `codecs`, both already under `lib/` — so nothing had to be
dragged along and no moved body needed a call re-pointed. The four call sites
that stay (`main`'s two `find_tuning_xml`, its `autoprobe_dolby_source`, and
the `is_soundwire_xml` that decides the bus) re-point for free, as root-side
call sites always do.

The module is stdlib-only in the sense the converter's startup cares about — no
numpy, no scipy — and still **cannot** join `tests/test_layout.py`'s
`STDLIB_ONLY`. That list's `FORBIDDEN` covers `rich` as well, and every one of
these functions announces what it matched, so the module imports `lib/console.py`
and the optional dependency it owns. Checked rather than assumed: adding it
fails with `pulls in rich,rich_argparse`. It is the third module to land in that
position after `lib/pipewire/install.py` and `checks.py`, and the second in
`lib/dax/`, so the tuple now carries the reason in a comment instead of leaving
the absence to be re-discovered. The rule that *does* bind it is the one with no
list — `test_converter_startup_pulls_in_no_dsp` — and it is green.

Twelve `monkeypatch.setattr` calls in `tests/test_cli.py` moved to the new
module: `_resolve_driver_store` ×8, `_ntfs_family_mountpoints` ×2,
`_walk_for_dolby_xml_dirs` and `_detect_expected_subsys_ids` ×1 each. Each was
sabotage-checked — break the production function, confirm the test that patches
it still passes — because a retargeted patch that reaches nothing leaves a green
test that quietly ran the real code.

Four duplications were **noticed and deliberately left**, since collapsing one
inside a move commit is a behaviour change under a subject line promising there
isn't one. Three are inside `discover.py`: the `dax3_ext_*.inf_*`-wrapper scan
written twice (`_candidate_has_matching_xml` as a `… or [driver_store]`,
`find_tuning_xml` as an `if not xml_dirs:`), the PCI-subsystem byte order
`f"{device}{vendor}".upper()` derived twice, and the DAX3-eligible-file
predicate spelled four times. That last one is the reason for the rule rather
than an example of it: three sites test the suffix exclusion *and*
`DOLBY_FILENAME_RE`, while `find_tuning_xml` globs `*.[xX][mM][lL]` and applies
only the suffix half — so an XML with no `SUBSYS_` token still reaches
`scanned_files` there, and with it the security-key content fallback, while the
three probe sites never see it at all. "Collapse the duplication" would silently
narrow what the generator can match. The fourth is across the boundary:
`tests/corpus/test_corpus.py`'s `_discover_corpus` re-implements
`autoprobe_dolby_source`'s mount-then-CWD walk, which its own docstring already
admits.

### The speaker report, and where a `lib/` caller may be re-pointed

649 lines — `warn_speaker_firmware_gate` through `report_speaker_info`, plus
`speaker_pin_status` — left the generator for `lib/report/speaker.py`, taking
it from 2,769 lines to 2,116. This is the *reporting* half of a split whose
probing half became `lib/hardware/speakers.py` in wave 1: that module answers
what is wired to what, and everything here decides what to say about the
answer, across the three surfaces that must not drift — `--speaker-info`,
`--doctor`, and the findings a normal run raises.

`speaker_pin_status` came along even though it is a `--doctor` verdict and the
slice-5 note above parked it with the I/O half. It is a verdict *about the
speakers*, it reads `_pin_phrase` / `upgrade_prospect` /
`speaker_pin_fix_steps`, and moving it now is what leaves the `--doctor` I/O
that comes next with no pin edge at all.

`symtable` over the block says it reaches fourteen names it does not define:
`Path`, `date`, `textwrap`; `console`, `version`, `amps`, `codecs`, `speakers`,
`speaker_pin_quirks`, `environment`, `report_findings`; and `Finding`,
`CheckResult`, `DOCTOR_WARN`. Nothing had to be dragged along beyond
`_MODPROBE_CONF` and `_pin_phrase`, both private to the block, and no moved
body needed a call re-pointed. The last three arrive as bare names on
`lib/report/environment.py`'s precedent — a frozen dataclass and two string
constants, nothing a test would patch — and `report_findings` keeps the
generator's alias because the moved lines read through it. In the generator and
in both test files the new module is imported as `report_speaker`: one letter
from `lib.hardware.speakers`, which those files still use.

**The cycle that had to be checked, and wasn't one.** `upgrade_prospect` reads
`environment.parse_kernel_series` and `_print_speaker_info` reads
`environment._kernel_series_age`, so `speaker.py` imports `environment.py`.
Nothing goes back the other way: `environment.py` reaches
`lib/hardware/speakers.py` for a type and never the report. The edge is
one-directional and the `--doctor` I/O that assembles both sits above them —
which is the whole reason it is going into a module of its own rather than
back into `environment.py`.

**A `lib/`-side call site can only be re-pointed in three places.** The one
caller of this code outside the generator is `lib/pipewire/checks.py`, whose
PipeWire `--doctor` ends on the same hardware dump. It reached it through a
deferred, `try`-guarded `import dolby_to_easyeffects as gen` — deferred because
that import cost the generator's NumPy/SciPy on a path that does no DSP.
Retargeting it looks like a one-line edit and is not: `check_move_purity.py`
exempts a module's docstring, its top-level import block and blank lines, so in
a file already under `lib/` the *only* pure edits are a deletion, a top-level
import, and docstring prose. A rewritten line inside a function body is a
violation whether or not it is a moved line, and the tool cannot tell the two
apart. So the import moved to the top of the file as
`from lib.report import speaker as gen`, the `try` came out, and the two call
lines were left untouched — including the alias, which now reads oddly and is
kept for exactly that reason. The sentence the deleted comment carried moved
into the docstring, where it is exempt. What it buys: the PipeWire doctor no
longer pulls numpy and scipy to print a hardware dump, which was the reason the
import was deferred in the first place.

Read that the other way round, because it is the lesson with a future in it:
hoisting the import is a **behaviour change** — the import now happens on every
converter run rather than on `--doctor` alone, and a `try` that degraded to
"(hardware report unavailable)" is gone in favour of letting an `ImportError`
on a `lib` sibling be the bug it is — and it passed `check_move_purity.py` as
*pure*, because the shape it changed into is an exempt one. The exemption for a
top-level import block exists so a freshly extracted module can import what it
needs; in an existing `lib/` module it will also launder an edit that has
nothing to do with the move. So **exit 0 certifies the shape of a commit, not
its innocence**, and it certifies nothing at all about files that were already
under `lib/` before it ran. The change here is disclosed in the commit body and
measured — converter startup 0.07–0.08 s before, 0.07–0.10 s after, numpy and
scipy still absent from a converter run's 214 modules — but nothing in the
tooling required that of it, and the honest alternative was the shape slice ①
took: leave the edit where it reads best and let the checker report the
impurity.

`lib/report/speaker.py` is stdlib-only in the sense the converter's startup
cares about and still cannot join `tests/test_layout.py`'s `STDLIB_ONLY`: it
prints, so it reaches `lib/console.py` and the optional `rich` that list's
`FORBIDDEN` also covers. Checked rather than assumed — importing it leaves
`rich,rich_argparse` in `sys.modules`. No `lib/report` module can ever be
eligible, since the package exists to print, so the tuple now says so once for
all four rather than leaving each absence to be re-discovered.

The patch inventory is two calls, both in `tests/test_cli.py`
(`_gather_speaker_info`, `_print_speaker_info`) and both retargeted to the new
module. Small, but not to be trusted on a green suite alone: each was checked
in both directions — break the production function and the patching test must
still pass, then aim the same patch at a throwaway object and it must fail with
the sabotage reaching the real function. The third patch in that test,
`version.get_version`, needed no retargeting and is the payoff of `c6f90db`:
had the generator still been re-exporting that name, the moved caller would
have read the real version through `lib/version.py` while the patch rebound a
copy, and the assertion about a version-stamped header would have passed
anyway. The other 57 references — 52 in `tests/test_speaker_sinks.py`, five in
`tests/test_cli.py` — are plain calls, and every `monkeypatch.setattr` those
tests rely on already names `lib.hardware.*` rather than the generator.

Five duplications were **noticed and deliberately left**, for the reason the
rule gives: collapsing one inside a move commit is a behaviour change under a
subject line promising there isn't one.

- `_gather_speaker_pins` and `_gather_speaker_info` share their first dozen
  lines verbatim — the `/proc/asound/cards` read, the three `codecs.get_*`
  probes, the demo-injection guard around `_detect_hda_speakers`. The split is
  deliberate and documented (the cheap one must not pay for `amixer`,
  `journalctl` and a `/lib/firmware` glob), but the overlap is copy, not
  design.
- The pin sentence is written twice: `warn_hidden_speaker_pin`'s "Upstream
  Linux carries a fix for this exact model that declares {phrase} on codec
  {ssid} an internal speaker, and your kernel isn't applying it" and
  `speaker_pin_status`'s lowercase near-identical detail. Two surfaces, one
  claim, and the *procedure* under it is already shared through
  `speaker_pin_fix_steps` — so this is the half that got left behind when the
  rest was unified.
- The firmware-gate symptom ("silent, thin or crackly") is spelled out in
  `warn_speaker_firmware_gate` and again in `_amp_status_lines`, with a comment
  in the second saying it is hedged for the same reason as the first.
- `speakers.find_hidden_speaker_pin(info)` is called twice inside
  `_print_speaker_info` — once for the flagged-pin set, once for the layout
  line — and a third time by `unlisted_speaker_pin_finding`, all on the same
  `SpeakerInfo`. Pure recomputation of a pure function; cheap, but three
  answers where there is one question.
- `_pin_phrase(missing)` is computed in `warn_hidden_speaker_pin` and again in
  the `_hidden_pin_finding` it calls two lines later with the same argument.

### The `--doctor` I/O, and the patch dividend being spent

372 lines — `parse_ee_version` through `warn_ee_environment`, plus
`easyeffects_is_running` from further down the file — left the generator for
`lib/report/doctor_run.py`, taking it from 2,116 lines to 1,744. This is the
half slice 5 deliberately stranded: the `*_status` verdicts went to
`report/environment.py` then, and the `_probe_`/`_gather_`/`_print_` wrappers
that do the I/O stayed because each read something still in the generator. Both
blockers are gone — the install paths resolve through `lib/ee_paths.py`
(slice ①) and `_gather_speaker_info` / `_print_speaker_info` /
`speaker_pin_status` are in `lib/report/speaker.py` (slice ③) — so nothing here
needed a call re-pointed inside a moved body.

Membership was derived from the call graph rather than the section boundary,
which matters because the block is **not contiguous**: `list_endpoints`,
`sanitize_profile_type` and `get_profile_types` sit between
`warn_ee_environment` and `easyeffects_is_running` and are XML inspection, not
diagnostics. `easyeffects_is_running` had to travel regardless of where it sat
— `_probe_ee_version`'s `native()` calls it to tell "installed but silent" from
"absent" (issue #46), so leaving it behind would have meant re-pointing a moved
line. Its third caller, in `main()`'s autoload block, re-points for free the way
every root-side call site does. `symtable` over the result says the module
reaches seventeen names it does not define, and every one is an import: `Path`,
`dataclass`, `json`, `re`, `shutil`, `subprocess`; `console`, `ee_paths`,
`version`, `autoload`, `environment`, `report_findings`, `report_speaker`; and
`CheckResult`, `DOCTOR_PASS`, `DOCTOR_WARN`, `DOCTOR_FAIL`.

**Why a new module and not more of `environment.py`.** That would close a
loop. `speaker.py` imports `environment.py` (`upgrade_prospect` reads
`parse_kernel_series`, `_print_speaker_info` reads `_kernel_series_age`), and
the doctor assembles *both* — `_gather_doctor_report` calls
`_gather_speaker_info` and `speaker_pin_status`, `_print_doctor_report` calls
`_print_speaker_info`. So it sits strictly above them, and putting it back in
`environment.py` would make environment → speaker → environment. The direction
actually created is one-way: `doctor_run` → {`speaker`, `environment`,
`findings`, `autoload`}, and nothing under `lib/` imports `doctor_run` at all.
The converter is untouched by it, so `test_converter_startup_pulls_in_no_dsp`
never sees the new edge.

**`report_doctor` came too**, on `lib/pipewire/checks.py`'s precedent: the
converter calls `checks.report_pw_doctor()` straight from `main()`, and this is
the same seam one tool over. It makes `report_doctor` and `warn_ee_environment`
the first functions under `lib/` to take an argparse namespace. That is a real
deviation from "each entry point keeps its argparse builders and `main()`", and
the tiebreak is that both are `--doctor`'s own top level rather than the
program's: what `main()` keeps is the decision to run them.

**The report vocabulary went with its last reader.** `DOCTOR_PASS` /
`DOCTOR_WARN` / `DOCTOR_FAIL` / `DOCTOR_UNKNOWN` / `CheckResult` were bound onto
the generator as attributes of `lib/doctor.py`, and after this move not one of
the five has a reader left in that file — `DOCTOR_UNKNOWN` already had none, and
was reachable only because a test read it through the generator. So the five
lines are deleted rather than moved, the module takes the four it uses as bare
names on `environment.py`'s precedent, and the reads in `tests/test_preset.py`
and `tests/test_speaker_sinks.py` retarget to `lib.doctor`, which is where they
were defined all along. The move
also orphans `import shutil`, `import subprocess`, `dataclass` and the
`lib.doctor` module import; those come out of the generator's import block with
it.

**This is the slice that spends slice 5's dividend**, and the whole of it.
Thirteen `monkeypatch.setattr` calls retarget, in two shapes:

- `_probe_ee_version` (`tests/test_preset.py`) and `warn_ee_environment`
  (`tests/test_cli.py`) — one call each, ordinary function patches now naming
  `lib.report.doctor_run`.
- the **module objects** `subprocess` (×7) and `shutil` (×4), all in
  `tests/test_preset.py` and all spelled `monkeypatch.setattr(d.subprocess,
  "run", …)`. These read the module *through* the generator's binding, so they
  survive being pointed anywhere the same module object is reachable — but the
  generator no longer imports either, so left alone they would have raised
  `AttributeError` rather than passing quietly. They now name `doctor_run`.

A green suite proves none of that. Each was checked in both directions. For the
two function patches: `raise AssertionError("SABOTAGE")` as the first statement
of the production function, and the patching test must still pass — then the
same patch aimed at a throwaway `SimpleNamespace` must fail with the sabotage
reaching real code. Both did. The module patches need the equivalent of a
poisoned attribute, since there is no "production function" to break: replacing
the module's own `subprocess` / `shutil` bindings with shims whose `run` /
`which` raise on call leaves all seven tests green (every one patched the object
production reads through), and re-aiming the patches at a throwaway fails
exactly the tests that patch that name — all seven for `subprocess`, and for
`shutil` the four that patch it while the other three stay green.

`lib/report/doctor_run.py` cannot join `tests/test_layout.py`'s `STDLIB_ONLY`
for the reason the whole package can't: it prints, so it reaches `lib/console.py`
and the optional `rich` that list's `FORBIDDEN` also covers. The comment
covering all of `lib/report` already said so and now names it among the four.

Two things were **noticed and left**, per the rule that collapsing duplication
inside a move commit is a behaviour change under a subject line promising there
isn't one:

- `_gather_doctor_report` and `warn_ee_environment` open with the same four
  lines — probe, unpack `probe.version` / `found` / `is_flatpak` into locals,
  hand them to `environment.ee_version_status` — and then ask the
  install-location question twice more, once through
  `environment.install_status` and once as a bare `ee_is_flatpak !=
  ee_paths.USE_FLATPAK`. Two surfaces on one probe, which is the same shape
  `e3a7ee4` unified on the *message* side and did not on the probe side.
  `report_doctor`'s `custom_dirs` predicate and `warn_ee_environment`'s
  default-dirs guard are that third copy in opposite polarity.
- The unpacking in both binds a local named `version`, which shadows the
  `lib/version.py` module the file imports. It is harmless today because
  neither function calls `version.get_version()` and `_print_doctor_report`
  (which does) has no such local — but it is the exact hazard the generator
  aliases `hardware_sinks`, `report_findings` and `report_speaker` to avoid,
  and it moved verbatim because a move commit may not rename what it carries.

Unrelated to the move but found while checking what it orphaned: the generator
still imports `configparser`, `contextlib`, `math`, `Callable`, `field`,
`kernel_releases` and `bands` with no reader for any of them, most of them
stranded by earlier slices. Deadwood for the tidy-up commit, not this one.

### The voicing table, and a table that serves two packages

Nine lines — `VOICING_CURVES`, the Balanced/Detailed/Warm → `ieq_*` curve-key
map — left the generator for `lib/report/messages.py`. It goes on its own and
first because its two readers end up in *different packages*: the per-profile
report derives its "this profile's three voicings" sentence from it, and the
emit loop keys the presets it builds off it, and those two are headed for
`lib/report/profile.py` and `lib/preset/emit.py` respectively. Whichever of the
two had kept it, the other would have had to import it — and both reach numpy,
so a shared nine-line table would have been reason enough to drag scipy across
a package boundary.

That is the same argument slice 5 of wave 1 made for the `--disable`/`--enable`
menus, one step further out. It is *not* the same constraint: those tables are
read on the completion path, where `_load_dsp` has deliberately not run, so
their home had to be stdlib-only. Both of `VOICING_CURVES`'s readers are
DSP-bound, and neither is on the completion path. What picks `messages.py` here
is the content — the table is copy as much as it is data, and `print_what_now`
twenty lines below already spells two of the three voicing names in the hint it
derives from what was built.

Like the install paths in the first slice of this wave, and for the same
reason, it is deliberately **not** a seed/trim pair: nine lines sit well under
the threshold where a copy edge earns its own commit. Unlike them it *is* pure
motion — `check_move_purity.py` reports 9 moved, 0 unmoved — because the block
crosses verbatim, comment included. The only edit either side is at the two
call sites, which are still in the root script at this point and therefore
re-point for free; they arrive at `lib/` already reading
`messages.VOICING_CURVES`, so the two bodies that carry them are unchanged by
their own moves.

The patch inventory is **empty** — no test names `VOICING_CURVES` — which is
worth recording rather than passing over, since the sabotage check that follows
every other slice has nothing to bite on here.

Two duplications were **noticed and left**. `print_what_now` tests
`n.endswith("-Detailed")` and `n.endswith("-Warm")` as string literals, and
`dolby_to_pipewire.py`'s `VARIANT_STEMS` spells all three a third time to map
`--variant warm` onto a preset stem. Both are now in a position to read the
table instead — the first is in the same module — but collapsing either inside
a move commit is a behaviour change under a subject line promising there isn't
one.

### The per-profile report, and the first module `_load_dsp` binds

543 lines — the whole of `_report_parsed_profile` — left the generator for
`lib/report/profile.py`, taking it from 1,744 lines to 1,188. It was the
largest symbol left in the file and had already been told it wasn't `main()`:
its own docstring said it was "split out of `main()` so the orchestration there
stays legible".

Membership came out unusually clean. `symtable` over the block says it reaches
ten names it does not define, and every one is an import — `np`, `console`,
`parse`, `fir`, `plugins`, `report_findings`, `messages`,
`_print_finding_detail`, plus the two channel arrays a lambda closes over.
**No private helper travelled with it**, because it has none: `register` and
`_peq_spec` are nested inside the body, and everything else it calls is
already under `lib/`. One call site stays, in `main()`, and re-points for free
the way root-side call sites always do.

**This is the first extracted module the generator may not import at the top of
the file.** It reaches numpy directly (the audio-optimizer summary),
`lib/preset/fir.py` for the sample rate the compressor crossovers print
against, and `lib/preset/plugins.py` for the decoders whose answers it reports
— so it is bound inside `_load_dsp()` beside `fir` and `build`, on
`lib/preset/plugins.py`'s precedent from slice 5 of wave 1. Importing it at the
top would put numpy and scipy — ~0.4 s of a ~0.5 s startup — on every TAB
press, which
`tests/test_completions.py::test_completion_path_skips_the_dsp_import` traps.
Verified rather than assumed: `_ARGCOMPLETE=1` and an `import
dolby_to_easyeffects` still leave numpy and scipy out of a 223-module
`sys.modules`, and `--help` still costs 0.62–0.68 s against 0.63–0.67 s
before.

Two consequences for `_load_dsp` itself. `lib.preset.plugins` loses its last
reader in the generator — the report was it — so the binding comes out with
the move, and `build` alone carries the chain (`build` → `plugins` → `fir` →
numpy). And the module is aliased **`report_profile`** on
`report_findings`/`report_speaker`'s precedent: `main()` binds `profile_type`,
`profile_label` and `profile_findings` within a few lines of the one call it
serves. `lib/report/profile.py` cannot join `tests/test_layout.py`'s
`STDLIB_ONLY` for two reasons now rather than one — it prints *and* it reaches
numpy — and the comment covering the whole of `lib/report` already says the
first.

**The patch inventory is empty, and that is the finding.** Not one
`monkeypatch.setattr` in the suite moves: the full inventory across `tests/`
names 45 distinct targets and none of them is a symbol this slice touches. What
retargets is three import sites — `tests/test_preset.py`'s top-level import,
one local `import dolby_to_easyeffects as d` that existed only for this call
(deleted, the module-level name serves), and two calls in `tests/test_cli.py`,
now through a `report_profile` alias matching the generator's.

An import retarget has the same silent failure as a stale patch, so it was
checked in both directions with the equivalent instruments:

- `raise AssertionError("SABOTAGE")` as the first statement of the moved
  function fails **34 tests** across the two files — so the retargeted sites
  reach real, moved code.
- Leaving the sabotage in place and putting a *working* `_report_parsed_profile`
  back on the root script as a stale binding changes nothing: the same 34 still
  fail. Nothing reads the generator's name.
- Pointing the two retargeted imports at a `SimpleNamespace` instead fails
  **28** — the six-test difference is the tests that reach the report through
  `main()` rather than through the import, which is the expected shape and not
  a gap.

### Preset emission, and a blocker that had quietly expired

199 lines — `_emit_ieq_presets`, the `FIR_VERIFY_OK_DB` gate it grades against,
and `save_wav_stereo` — left the generator for `lib/preset/emit.py`, taking it
from 1,188 lines to 985. `symtable` says the three reach nine names between
them and every one is an import: `json`, `np`, `wavfile`, `console`, `parse`,
`fir`, `build`, `autoload`, `messages`. `save_wav_stereo` and the gate travel
because `_emit_ieq_presets` calls and reads them; nothing else does.

**`save_wav_stereo`'s recorded blocker had expired without anyone noticing.**
The comment over it said it stayed behind `lib/preset/fir.py` "because binding
`wavfile` in a module that isn't this one means writing a second deferred
import, which is new code rather than motion". That was true when `fir.py`
moved and is not true now: `fir.py` imports numpy at module scope, so a module
the generator only reaches through `_load_dsp()` may import scipy the same way
— in the **top-level import block, which `check_move_purity.py` exempts**. The
deferral it was avoiding was never needed; what was needed was a destination
that is itself deferred. Worth generalising: a blocker recorded in a comment
is a fact about the code *at that commit*, and the slice that removes it will
not be the one that wrote it down.

`_load_dsp` is now the whole point of the pattern rather than a place numpy
lands. Three of its five globals go — `wavfile`, `fir` and `build` all lose
their last generator reader with these lines — and what is left is `np` (which
`main()` still needs to convert the parsed curves to dB) plus the two modules
this wave created. Every module in `lib/` that reaches the DSP stack is now
behind one of those two, and the generator's own import block is stdlib-only
below `lib/`.

The one edit that is **not** motion is disclosed here because nothing in the
tooling would have caught it: `main()`'s call to `_emit_ieq_presets` grew five
characters, so its visually-aligned continuation lines were re-indented to
match. Root-side call sites re-point for free and `check_move_purity.py` only
reads lines added under `lib/`, so this passed as pure; it is a whitespace
change to nine lines that stay in the generator, and it changes no provenance
that `-C` would have followed.

`lib/preset/emit.py` is the **only edge from `lib/preset/` into
`lib/report/`** — `messages.VOICING_CURVES`, per the slice above — and it is
one-way: nothing under `lib/report/` imports it. It also cannot join
`STDLIB_ONLY` for the strongest reason in the list, being the one module that
imports scipy at module scope.

Five test import sites retarget, one per file: `test_preset`,
`test_golden_preset`, `test_ee_to_pipewire`, `corpus/test_corpus` and
`corpus/test_ee_to_pipewire_corpus`. No `monkeypatch.setattr` target moves —
the same finding as the slice before, and now the second slice in a row where
the patch inventory is empty and the exposure is entirely in imports. Checked
in both directions: sabotaging both moved functions fails 3,066 tests and
errors 78 more; leaving the sabotage in place and putting a *working*
`save_wav_stereo` back on the root script changes those numbers by nothing, so
no test reads the generator's name; and aiming the five imports at a throwaway
fails all five files (the fifth only under `--run-slow`, where its 3,056 tests
stop being skipped — a default-run green there is no evidence, per
`.claude/rules/testing.md`).

Two consumers keyed on paths were checked and one moved. `.claude/rules/`'s
`user-messages.md` and `cli-help.md` both already glob `lib/**/*.py`, so the
copy contract and the argparse↔README mirror followed the code without an
edit. `ee-preset-format.md` did not: its `.irs`-extension trap is about the
filename this module builds, and the module that builds it has only just
arrived at a path a rule can be scoped to. It joins the list, which is the
"what the split unlocks" argument arriving one rule early.

Two bookkeeping items landed on this trim that belonged on the one before it:
`tests/test_layout.py`'s `STDLIB_ONLY` comments, which enumerated the
`lib.preset` and `lib.report` modules and so went stale as each new one
arrived. Both now name every sibling and say which of the two disqualifying
dependencies each has, verified by importing each module in a subprocess
rather than by reading.

Nothing was collapsed. The duplication left alone here is
`FIR_VERIFY_OK_DB`'s prose: the constant's own comment and the comment inside
the loop both explain the same threshold in the same terms ("far above the
minimum-phase design's normal residual (~0.05 dB)"), now nine lines apart in
one module instead of split across two parts of an 8,000-line file. Task #26.

### The sweep-up, and the seed that would have cost more than it bought

The last slice of the wave is four small moves and one cleanup, and none of
them is the interesting part. The generator ends at 924 lines from 8,108, the
wrapper at 449 from 610, the converter untouched at 484.

- **`list_endpoints`, `sanitize_profile_type`, `get_profile_types`** (39 lines)
  → `lib/dax/parse.py`. These are the reason the `--doctor` block was not
  contiguous: they sat between `warn_ee_environment` and
  `easyeffects_is_running` and inspect the XML rather than the environment.
  `parse.py` already excluded the `off` profile *citing* `get_profile_types`,
  so the rule and its citation now share a file. One import (`re`) added, four
  root-side call sites and one test import re-pointed for free.
- **`_HelpHintParser`** (14 lines) → `lib/console.py`, beside the
  `_HelpFormatter` it is the argparse-side sibling of.
- **The activation block** (149 lines) → `lib/pipewire/install.py`.
- **`_safe_node_name`** (5 lines) → `lib/pipewire/conf.py`, beside the
  `_sanitize_name` and `DEFAULT_NODE_NAME` it predicts the output of.
- **Eight unread imports** off the generator, in a commit of their own:
  `configparser`, `contextlib`, `json`, `math`, `Callable`, `field`,
  `kernel_releases`, `bands` — the seven the `--doctor` slice recorded, plus
  `json`, whose last reader left with `_emit_ieq_presets` one slice later.
  `replace` on the `dataclasses` line still has a caller and stays;
  `from __future__ import annotations` is a compiler directive, not an import,
  and stays in all three root scripts. Confirmed by `ast` at the commit, not by
  grep: `bands` has 22 textual hits in the file and every one is prose.

**A seed can cost more than it buys, and this is the shape where it does.** The
rule above says extending an existing module needs a seed from the blob that
path held at *its own* first seed. That rule assumes one source script.
`lib/pipewire/install.py` held 180 lines extracted from `ee_to_pipewire.py`
and was about to receive 149 from `dolby_to_pipewire.py`, and **no blob
contains both**. Both shapes were built and measured on the same paths:

| shape | of the 149 moved lines | of the 191 lines above them |
|---|---|---|
| flat extraction | **149** | 180 (the 11 are this commit's docstring and `import time`) |
| seed = copy of `dolby_to_pipewire.py`, then trim | 149 | **18** |

The seed recovers nothing the flat commit did not already have, and spends 162
traced lines to do it — because the seed *replaces* the file, so install.py's
own earlier content exists nowhere in the trim's parent tree for `-C -C -C` to
find. So this one shipped flat, and the general form of the rule wants the extra
clause: **a seed pays only when a single blob holds every line the commit is
about to put at that path.** For a second helping from a different root script
there is no such blob, and the flat commit is not the fallback but the better
answer.

Why flat did so well here at all is the same variance the table further up
measured: this commit removed 149 lines from a 609-line file and added them to
a 180-line one, which is a similarity problem `-C -C` solves, unlike ~800 lines
against a shrinking 8,000-line file. The other three moves were measured the
same way and needed no seed either — 38 of 39 lines traced into `parse.py`
(the exception is `    ep = root.find(`, four tokens long) and all 14 lines of
the class into `console.py`. `_safe_node_name` traces 3 of 5, which is the two
impure lines below and not a provenance problem.

**The import direction, re-checked against the tree rather than the note.**
`checks.py` → `install.py` was avoided in slice 6 to prevent a cycle, and slice
3 has since put a module-scope `from lib.report import speaker as gen` on
`checks.py`. Neither matters here: the activation block reaches five names it
does not define — `Path`, `console`, `shutil`, `subprocess`, `time`, every one
an import — and none of them is in `checks.py`. The package still layers
`plugins ← conf ← {install, checks}` with no edge between the top two, and a
graph walk over all of `lib/` reports no cycle anywhere. `lib/pipewire/` still
imports nothing from `lib/preset/`, and this slice adds no edge in either
direction between `lib/preset/` and `lib/report/`.

**The numpy claim was wrong, and the measurement says so.** The reason given
for moving `_HelpHintParser` was that `from dolby_to_easyeffects import
_HelpHintParser` dragged the generator's numpy into the wrapper. It does not:
two lines above it, `import dolby_to_easyeffects` does — unconditionally, since
the generator calls `_load_dsp()` at module scope on any non-completion run —
and the wrapper cannot drop that import, which is how it reaches `run_cli`,
`ensure_dsp` and the shared argument builders. A wrapper import measures 666
modules with numpy and scipy present either side of the commit, and 666 again
with the `from`-import deleted and nothing else changed. What the move is
actually worth is that it was the last import crossing between two root
scripts. On the completion path all three scripts stay DSP-free: 222 / 214 /
231 modules, numpy and scipy absent from each. The generator's 223 became 222
for one reason — `lib.preset.bands` was the only *module* among the eight dead
imports; the other seven are stdlib and still arrive transitively.

**One disclosed impurity, in the smallest move of the five.**
`_safe_node_name`'s body read `conf._sanitize_name(...)` and
`conf.DEFAULT_NODE_NAME`, and inside `conf.py` those prefixes cannot stand, so
two of its five lines are rewritten and `check_move_purity.py` reports them.
There is no exempt shape to reach for — the alternative is a module importing
itself — and the rule is to leave the code where it reads best and disclose,
as slices 1 and 7 did. The other three moves are pure.

The patch inventory is **six**, all in `tests/test_dolby_to_pipewire.py` and
all of the module-object kind: `install.subprocess` ×4, `install.shutil` and
`install.time` ×1, previously reaching those modules through the wrapper's
bindings. The wrapper no longer imports any of the three — they left with the
block — so left alone they would have raised `AttributeError` rather than
passing quietly, the same shape the `--doctor` slice hit. Checked in both
directions with a shim scoped to the three calls the moved code makes
(`subprocess.run` on `pw-cli`/`systemctl`, `shutil.which("pw-cli")`,
`time.sleep`), so `_validate_conf`'s use of the same two modules is untouched:
poisoned, all 41 tests stay green; re-aimed at a throwaway with the poison
still in, exactly 6 fail on the sabotage. The re-pointed calls were checked the
same way — sabotaging the three moved print/activate functions fails 24 tests,
and putting working copies back on the wrapper under their old names changes
that by nothing, so no caller reads a root binding. Sabotaging
`_safe_node_name` fails 27, with the same stale-binding result.

Two duplications **noticed and left**. `PIPEWIRE_RESTART_CMD` collapses a
string now written out three more times — `ee_to_pipewire.py`'s "To activate:"
line, `lib/pipewire/checks.py`'s doctor remedy, and
`lib/pipewire/install.py`'s own `_print_next_steps` step 1, which after this
move sits 24 lines above the constant that would replace it. Collapsing it is a
behaviour change under a subject line promising there isn't one, and the four
sites do not phrase it identically, so it is task #26 rather than a follow-up
commit here. The second is smaller: `_print_manual_activation` and
`_print_next_steps` now spell the same "nothing usually means the LSP or Calf
LV2 plugins are missing" sentence in one file, deliberately differing only in
the tail about what happens once the line appears.

### What it does to the test suite

Less than the 12,269 lines of tests suggest. Measured before starting, because
the answer changes the order:

| Kind of change | Lines | Files |
|---|---|---|
| Nothing at all | ~630 | `test_layout`, `test_version`, `test_readme_cli_sync`, `test_changelog_section`, `test_corpus_audit`, `test_stimuli`, `test_validate_conf` |
| One path constant | ~590 | `test_kernel_releases`, `test_speaker_pin_quirks` |
| Patch targets renamed, assertions untouched | ~2,510 | `test_cli` (1,717), `test_dolby_to_pipewire`, `test_completions` |
| Import lines only | ~1,950 | `test_biquads`, `test_fir_math`, `test_decoders`, `test_golden_preset`, `tests/corpus/*` |
| Real churn | ~6,280 | `test_speaker_sinks`, `test_preset`, `test_pw_doctor`, `test_ee_to_pipewire` |

`test_cli.py` is the one to notice: 1,717 lines, the largest single block of
generator tests, and **zero** `d.<attr>` accesses — it drives the generator as a
*subprocess* through `SCRIPT = ROOT / "dolby_to_easyeffects.py"`. Its 57
`monkeypatch.setattr` calls each name the module explicitly, so a split renames
57 patch targets and touches not one assertion. That is the dividend of keeping
the entry points at root: every test that exercises the CLI the way a user does
is immune to how the code behind it is arranged.

The other end is `test_speaker_sinks.py` — 1,609 lines, 50 distinct `d.<attr>`
accesses (`find_hidden_speaker_pin` ×23, `SpeakerInfo` ×20, `_amp_status_lines`
×12), every one of which follows its symbol into `lib/hardware/`. Do that slice
after the pattern is proven elsewhere.

`tests/` stays **flat**. A test file splits only when its subject does, named
for the module it covers (`test_fir_math.py` already is). Mirroring `lib/` into
subdirectories would add a second axis of churn on top of the source moves and
buy nothing the filename doesn't already give.

### The monkeypatch hazard, and the import rule that prevents it

86 `monkeypatch.setattr` calls target the generator module — unambiguous while
there is one module. After a split it stops being: if
`lib/hardware/speakers.py` does `from lib.hardware.codecs import
get_soundwire_ids`, it holds *its own* reference, and patching
`lib.hardware.codecs.get_soundwire_ids` does not reach it. Eleven call sites
patch that exact symbol, eleven more patch `get_hda_codec_ids`, eight
`_resolve_driver_store`.

> **Across `lib/` package boundaries, import the module, not the name.**
> `from lib.hardware import codecs` … `codecs.get_soundwire_ids()`, never
> `from lib.hardware.codecs import get_soundwire_ids`.

One patch point — the defining module — however many callers. Inside a single
module, `from X import name` stays fine; the rule is about crossing package
boundaries, where the test has to guess which binding it is holding.

Two cases that look like they need the rule and don't:

- **The 30 `_CONSOLE` patches** — the largest single group — survive as-is.
  `cprint` reads the global at call time, so patching it on whichever module
  owns the console works from any caller. Only the module named in the patch
  changes. They were also the obvious candidate for one `conftest.py` fixture,
  which the prep phase below took: `silence_console`, called with the module
  whose output the test asserts on.
- **The 19 `setattr(d.subprocess, …)` / `setattr(d.shutil, …)` patches** reach
  the *stdlib module object* through the generator's namespace, so they are
  already global in effect and stay correct wherever the calling code ends up.
  They need retargeting to a module that still imports `subprocess`, nothing
  more.

The rule is enforced rather than remembered:
`tests/test_layout.py::test_no_test_patches_a_re_exported_name` (`4772ffe`)
reads the root scripts for names they only re-export out of `lib/`, reads
`tests/` for `monkeypatch.setattr` calls aimed at a root module, and fails on
the intersection — naming the module to patch instead. Static is the only way
worth doing it: the failure it guards has no symptom, because a patch that
reaches nothing leaves a green test that quietly ran the real code.

On arrival it found exactly one violation, and it is the rule in miniature.
`tests/test_cli.py` patched `dolby_to_easyeffects.get_version`, a name the
generator had lifted out of `lib/version.py`. Harmless the day it was written,
since the only caller sat in the same module as the re-export — and latent,
because `report_speaker_info()` is on its way into `lib/report/`, after which
the patch would rebind the root script's copy while the moved caller read the
real function, and the assertion about a version-stamped header would pass with
the real version in it. `c6f90db` fixed it at the source rather than at the
patch site: all three root scripts now `from lib import version` and call
`version.get_version()`, so there is one binding and one thing to patch.

### A tool keyed on a fixed path list goes quiet when code moves

This one predates the refactor and outlives it. `tools/extract_claims.py`
builds the inventory of user-facing strings that the **/copy-audit** skill
reviews, and it harvested them from a hardcoded table of the three root
filenames. When `doctor.py` moved into `lib/` in `16c4723`, its three verdict
lines went with it and dropped straight out of that inventory. Nothing failed:
no test covers the tool, no error was printed, and the next copy audit would
simply have reviewed less than it believed it was reviewing — the same shape as
the monkeypatch hazard above, a check that stops checking without stopping.
`23c9ba8` taught it to discover `lib/**/*.py` and key rows by import path
(`lib.doctor-002`), so the inventory now grows with the package rather than
with someone's memory.

The lesson generalises well past that tool. Every consumer keyed on a fixed
list of source paths — an audit's file table, a workflow's `git add`, a
`.claude/rules` `paths:` glob, a test's `CONVERTER` constant — narrows the
moment code moves out from under it, and the ones that narrow *loudly* are the
lucky half. So the per-slice checklist is not only "does it import" and "does
the golden digest hold": grep the whole repo, `.github/` and `.claude/`
included, for the name of the file being emptied, and read every hit that isn't
prose.

### What the split unlocks for CLAUDE.md

CLAUDE.md sits at 137 loaded lines against the ~120 its own house rules target,
and the usual fix — move rationale into `docs/` — is already applied. The
remaining lever is path-scoped `.claude/rules/*.md`, and granular files are what
makes scoping mean anything: a rule scoped to an 8,100-line file fires on
essentially every edit, which is indistinguishable from being global.

The principle that decides what is eligible:

> **Demote what constrains an edit. Keep what gates an action.**

A rule loads when a file matching its `paths:` glob is in play. That is exactly
right for "when you touch this, obey that", and useless for anything that must
fire *before* an action — pushing, running the measurement tooling, starting
issue triage, deciding a change needs a CHANGELOG entry, creating a new file.
Roughly half of "Repo etiquette" is action-gating and stays put however granular
the source gets.

Eligible once the split lands, ~29 lines:

| CLAUDE.md | Lines | Scoped to |
|---|---|---|
| Zero added latency / minimum-phase FIR | 6 | `lib/preset/fir.py` |
| Golden-digest + corpus-tier mechanics | 12 | `tests/**`, `pyproject.toml` |
| EE preset format quirks | 4 | `lib/preset/build.py`, `lib/preset/bands.py` |
| XML-only derivability, the evidence-bar detail | ~5 | `lib/dax/**`, `lib/preset/**` |
| `filter_coefficients` is not an audio EQ | 2 | `lib/dax/parse.py` |

The FIR entry shows why the split is the unlock rather than a nicety. Scoped to
`dolby_to_easyeffects.py` it fires on every edit to the file, so it belongs in
CLAUDE.md today. Scoped to a ~70-line `lib/preset/fir.py` it fires exactly when
someone is about to lengthen an IR — which is the only moment it matters. Each
demotion keeps a one-line trigger in CLAUDE.md and moves only the rationale into
the rule, the same division `changelog.md` and `user-messages.md` already use.

One counter-example, recorded so it isn't re-proposed: the `ee_to_pipewire.py —
companion converter` section looks eligible but isn't. "Stereo only; 4-channel
upmix isn't translated" is a *triage* fact — needed when reading an issue about
surround sound, not when editing the converter — and a rule scoped to the file
would not be loaded at the moment it is wanted.

Projected: **137 → ~108 loaded lines**, with headroom for the next real rule.
About 3 lines (the comparison-plots bullet → `tools/**/*.py`) are demotable
today without waiting for anything.

**Done after slice 6: 140 → 121 loaded lines**, across five new rule files —
`dsp-fir.md` (`lib/preset/fir.py`), `testing.md` (`tests/**`,
`pyproject.toml`), `xml-derivability.md` (`lib/dax/**`, `lib/preset/**`),
`ee-preset-format.md` (the four modules that write or read an EE preset key,
which now includes `lib/pipewire/plugins.py` — the two sides have to agree on
the enum label strings character for character) and `plots.md`
(`tools/**/*.py`). The 108 was optimistic because it costed the demotions at
zero: every one of them leaves a one-line trigger, and five triggers is five
lines that the projection had spent. 121 against a ~120 target is the honest
number, and the remaining slack is in the triggers rather than in prose.

Two of the table's rows needed correcting against the code before they could
be scoped. **`filter_coefficients` does not appear in `lib/dax/parse.py`** —
it appears in no source file at all, only in the docs, because the whole point
is that it is never parsed. Scoping a rule to the file where the mistake
*would* be made is still right, so it went into `xml-derivability.md` under
"What is not a source of parameters" rather than becoming a rule of its own
for two lines. And the **EE-format row's scope was too narrow**: `build.py`
and `bands.py` write the keys, but `lib/pipewire/plugins.py` reads them back
and `lib/report/environment.py` has the `--doctor` check for the
`kernel-path`/`kernel-name` trap, and all four have to agree.

Three more counter-examples, to go with the `ee_to_pipewire.py` one above:

- **"Co-locate definitions with use"** is an edit constraint, but its glob
  would be `lib/**/*.py` plus all three root scripts — every source file in
  the repo. A rule that fires on every edit is a rule in CLAUDE.md with an
  extra hop in front of it.
- **"Docs are layered"** looks like `docs.md`'s business, and `docs.md` even
  defers to CLAUDE.md for it. It stays because it is a map for *reading*, not
  for editing: you consult it to decide which of four docs to open, and no
  `paths:` glob fires on a read.
- **The comparison-plots rule keeps its trigger for a specific reason** — the
  scripts where hidden-curve bugs actually happen are ad-hoc ones under
  `localresearch/`, which is gitignored and may not be named in a committed
  file, so no glob can cover them. `tools/**/*.py` catches the committed half;
  the CLAUDE.md line catches the rest.

The `ee_to_pipewire.py — companion converter` section survives slice 6 intact,
which is worth noting because the split was supposed to be what unlocked it.
"It pins a WirePlumber 0.5+ smart filter … keep both defaults on" *is* an edit
constraint, and `lib/pipewire/install.py` is now a precise scope for it — but
it is one clause of a seven-line section whose other six are the triage
context the counter-example above is about. Extracting the clause costs a
sixth rule file and saves nothing.

### Order to do it in

A prep phase came first, before any code moved (`5c16e4b..7c3687c`). The
`cli-help` and `user-messages` rule globs widened to `lib/**/*.py`, so they keep
firing on the argparse and Finding copy as it moves. `lib/paths.py`'s
`REPO_ROOT` gave the first `__file__`-relative path to move one place to resolve
through. `tools/extract_claims.py` learned to walk `lib/` (above).
`tests/test_layout.py` gained the two guards a split needs and a single module
never did — the re-export trap, and `test_converter_startup_pulls_in_no_dsp`,
which asks the stdlib-only contract from the converter's end rather than off a
hand-kept list — plus `test_subpackage_init_is_import_free`, whose subject list
is discovered rather than written down, so it is in force on the commit that
creates `lib/hardware/` instead of a later one that remembers it. And
`tools/check_move_purity.py` arrived to check the move rule itself. Each is
something the moves are then measured against, which is worth more before them
than after. In the same phase the inline `_CONSOLE` patches — the generator's
30 and the converter's seven — collapsed into one `silence_console` fixture in
`tests/conftest.py` (`7c3687c`, 35 call sites), each call naming the module
whose output that test asserts on; the two tests that are *about* the console
still patch it themselves. The console slice then has one patch shape to update
rather than 35 lines to find.

One further prep commit belongs to that slice alone, and it is a *behaviour*
change rather than a guard. The converter's console targeted **stderr** where
the generator's targeted stdout — a leftover from when `ee_to_pipewire.py`
wrote the generated conf to stdout and its messages had to keep clear of it.
`2c3fb30` removed that dump, leaving no machine-readable stdout anywhere in the
repo, so the split protected nothing while costing two mechanisms that existed
only to undo it: `_report_on_stdout()`, which swapped the console (and a
`_PLAIN_TO_STDOUT` flag for the no-rich path) around the diagnostic report so
`--doctor > report.txt` wouldn't write an empty file, and an unconditional
`contextlib.redirect_stdout(sys.stderr)` in `dolby_to_pipewire.py` so the
generator's closing block would land in the stream its phase banners were on.
Putting the converter's console on stdout deletes both, and makes a run of
either PipeWire script one redirectable stream. Doing it *before* the move is
the point: `tools/check_move_purity.py` cannot tell a behaviour change from a
motion, so every remaining difference between the two consoles — the stream,
a missing `width=_wrap_width()` cap, an inlined versus named theme dict, a
`_make_console` helper with a parameter nothing passed any more — was resolved
in a commit honestly labelled as the change it is, leaving the move nothing to
carry but identical lines. Two of those resolved without touching rendering,
and both were checked rather than assumed: the width cap is inert under
`soft_wrap=True` (rich neither wraps nor pads, so a real run is byte-identical
at 60, 100 and 200 columns, ANSI included), and `rich.text.Text` — the
generator's only rich use outside `cprint` — moved down to `print_ask` rather
than into the shared console, so what gets extracted is what both scripts
actually share.

Then each slice is one commit of pure code motion, and each carries its measured
test cost. A rules demotion lands *after* the slice that creates its scope
target, one per slice, so a rule never points at a path that doesn't exist yet.

1. **`lib/console.py`** — `dolby_to_pipewire.py` already reaches into *both*
   scripts for `cprint` / `_disable_color`; one shared console ends that.
   *Test cost: the `_CONSOLE` patches now all run through `silence_console`,
   so what the slice retargets is one patch shape* rather than 35 hand-written
   lines.
2. **`lib/data/`** — the highest value per line moved, and it isn't about size:
   two weekly workflows currently rewrite tables inside a hand-edited file.
   Touches `tools/update_kernel_releases.py`,
   `tools/update_speaker_pin_quirks.py` and both workflows' `git add` /
   `git checkout --` lines. *Test cost: near zero* — both updaters already work
   on a file path through `parse_table` / `render_table` / `apply_update`, and
   their tests assert against a self-contained `FIXTURE` string, so only the
   `DEFAULT_SCRIPT` / `CONVERTER` constants move.

   It went second, behind the console, and the reason is worth keeping: what
   separates two cheap slices is how fast a mistake is *proven*. A console
   mistake fails `pytest` in seconds. Half a `lib/data/` mistake does too —
   `tests/test_kernel_releases.py` and `tests/test_speaker_pin_quirks.py` parse
   the table out of the real file, so a stale path constant fails at once — but
   the other half lives in `.github/workflows/kernel-release-table.yml` and
   `speaker-pin-quirks.yml`, which name the generator by hand (`git add
   dolby_to_easyeffects.py`, `git checkout -- dolby_to_easyeffects.py`) and
   which nothing under `tests/` reads. Move the tables without editing those
   lines and the `git add` becomes a whitelist pointing at an unmodified file:
   it stages nothing, and the run dies a step later on `git commit` refusing an
   empty commit — a week after the mistake, in a message about git rather than
   about the move, on a schedule nobody is watching. Both workflows want an
   `if git diff --cached --quiet; then … exit 1; fi` before the commit, so the
   failure names the file it expected to have changed. Cheap-to-falsify slices
   first.
3. **`lib/preset/fir.py`** and **`lib/dax/parse.py`** — the pieces
   `tools/measure_dax/analyze.py` imports today, so the measurement harness
   stops pulling in an 8,000-line module to call `make_fir`. *Test cost: import
   lines in four files.* Decide where `SAMPLE_RATE` / `FIR_LENGTH` and
   `DB_FIXED_POINT_SCALE` land — four test files import them, and "co-locate
   with use" and "one obvious home" pull in different directions here.
4. **`lib/hardware/`** — the expensive one (`test_speaker_sinks.py`); worth
   doing only once the import rule above has been exercised on slices 1–3.
5. **`lib/report/`**, the **`lib/preset/`** remainder, then **`lib/pipewire/`** —
   the converter last, once the pattern is proven on the generator.

Every step is guarded by `tests/test_golden_preset.py`: a pure move cannot
change an emitted parameter, so a moved digest means the move wasn't pure. The
stronger check, cheap enough to repeat per slice, is running the generator from
a `git worktree add --detach <scratch> HEAD~1` and `cmp`-ing the `.irs`, the
preset JSON (ignoring the `_generator` stamp) and the conf `ee_to_pipewire.py
--output` writes (ignoring its `# version:` stamp) against the same run at the
parent commit — that is what confirmed `26d5c58` was a pure move on real input
rather than only on the synthetic golden fixture. The conf used to come off
`--dry-run`'s stdout; `2c3fb30` removed that, and `--output` is the route now.

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
