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
| [easyeffects-and-pipewire.md](research/easyeffects-and-pipewire.md) | EasyEffects, PipeWire, WirePlumber, Flatpak, paths and sample rate |
| [virtual-bass.md](research/virtual-bass.md) | DAX's virtual bass engine (VBE), the Calf bass enhancer and `--enable virtual-bass` |
| [adaptive-processing.md](research/adaptive-processing.md) | the volume leveler and autogain, the multi-band compressor (MBC), the dialog enhancer, surround and media intelligence (MI) steering |

The rest of this file is being split by class into `docs/research/`.

### Issues

| Issue | Material | Where |
|---|---|---|
| #14 | VBE on HDA devices and `--enable virtual-bass` | [virtual-bass.md#r-dax-virtual-bass](research/virtual-bass.md#r-dax-virtual-bass) |
| #18 | single pin, a 2-driver laptop per PSREF | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #22 | field follow-up: the preset loads but is inaudible | [easyeffects-and-pipewire.md#r-preset-loads-but-inaudible](research/easyeffects-and-pipewire.md#r-preset-loads-but-inaudible) |
| #25 | autogain's HDA default flip, rejected; the −50 dB silence gate | [adaptive-processing.md#r-autogain-default-flip](research/adaptive-processing.md#r-autogain-default-flip) |
| #27 | amps read as speakers; the bass-enhancer field report is at [virtual-bass.md#r-soundwire-bass-enhancer-constants](research/virtual-bass.md#r-soundwire-bass-enhancer-constants) | [hardware-and-drivers.md#r-smart-amp-families](research/hardware-and-drivers.md#r-smart-amp-families) |
| #29 | CS42L43 excluded as a jack codec; the bass-enhancer field rounds are at [virtual-bass.md#r-soundwire-bass-enhancer-constants](research/virtual-bass.md#r-soundwire-bass-enhancer-constants); the removed SoundWire dialog-enhancer arm's field evidence is at [adaptive-processing.md#r-dialog-enhancer-gain-ceiling](research/adaptive-processing.md#r-dialog-enhancer-gain-ceiling) | [hardware-and-drivers.md#r-amp-parts-rejected](research/hardware-and-drivers.md#r-amp-parts-rejected) |
| #30 | two pins, PSREF names woofers and tweeters | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #33 | kernel 6.12 → 7.0 fix, old-kernel hint | [hardware-and-drivers.md#r-kernel-misconfigured-codec](research/hardware-and-drivers.md#r-kernel-misconfigured-codec) |
| #36 | single pin, a 2-driver laptop per PSREF; the reporter's enable-autogain recommendation is at [adaptive-processing.md#r-autogain-bypassed-by-default](research/adaptive-processing.md#r-autogain-bypassed-by-default) | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #39 | crackle; rule out the kernel (TAS2781 calibration differs by kernel lineage) | [hardware-and-drivers.md#r-kernel-misconfigured-codec](research/hardware-and-drivers.md#r-kernel-misconfigured-codec) |
| #44 | single pin, a 2-driver laptop per PSREF; DAX applying no VBE is at [virtual-bass.md#r-dax-virtual-bass](research/virtual-bass.md#r-dax-virtual-bass) | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #46 | single pin, a 2-driver laptop per PSREF | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #50 | single pin, a 2-driver laptop per PSREF; its missing `38dc` quirk entry concerns its smart amp, not a bass pin | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #51 | two pins, PSREF names woofers and tweeters | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #53 | hidden woofer pin | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #63 | the chain selected as the system output: two sinks in series | [easyeffects-and-pipewire.md#r-chain-as-system-output](research/easyeffects-and-pipewire.md#r-chain-as-system-output) |
| #84 | EasyEffects plays hot above 48 kHz; the deep-threshold regulator stays in this file | [easyeffects-and-pipewire.md#r-convolver-resample-gain](research/easyeffects-and-pipewire.md#r-convolver-resample-gain) |
| #93 | presets written where EasyEffects stopped reading; the locale pin | [easyeffects-and-pipewire.md#r-flatpak-xdg-roots](research/easyeffects-and-pipewire.md#r-flatpak-xdg-roots) |
| #95 | unlisted machine, firmware mic setting; the EasyEffects crash is at [easyeffects-and-pipewire.md#r-irs-in-place-rewrite](research/easyeffects-and-pipewire.md#r-irs-in-place-rewrite) | [hardware-and-drivers.md#r-fixed-level-speaker-pin](research/hardware-and-drivers.md#r-fixed-level-speaker-pin) |

### Moved sections

| Old heading | Now at |
|---|---|
| Bad sound with a perfect preset: the kernel layer below (issue #33) | [hardware-and-drivers.md#r-kernel-misconfigured-codec](research/hardware-and-drivers.md#r-kernel-misconfigured-codec) |
| Half the speakers, silently: a woofer pin the firmware hides (issue #53) | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| The class next door: pin present, DAC source wrong | [hardware-and-drivers.md#r-speaker-dac-misrouted](research/hardware-and-drivers.md#r-speaker-dac-misrouted) |
| When no table lists the machine (issue #95) | [hardware-and-drivers.md#r-fixed-level-speaker-pin](research/hardware-and-drivers.md#r-fixed-level-speaker-pin) |
| What counts as a smart amp, and which ones we watch for | [hardware-and-drivers.md#r-smart-amp-families](research/hardware-and-drivers.md#r-smart-amp-families) |
| Swept and rejected | [hardware-and-drivers.md#r-amp-parts-rejected](research/hardware-and-drivers.md#r-amp-parts-rejected) |
| Selecting the chain as the system output (issue #63) | [easyeffects-and-pipewire.md#r-chain-as-system-output](research/easyeffects-and-pipewire.md#r-chain-as-system-output) |
| Open: does a hand-picked chain suppress Bluetooth auto-switching? | [easyeffects-and-pipewire.md#r-chain-as-system-output](research/easyeffects-and-pipewire.md#r-chain-as-system-output) |
| Rejected: `priority.session` on the v1 capture node | [easyeffects-and-pipewire.md#r-chain-as-system-output](research/easyeffects-and-pipewire.md#r-chain-as-system-output) |
| Rejected: pinning a single v1 chain's playback | [easyeffects-and-pipewire.md#r-chain-as-system-output](research/easyeffects-and-pipewire.md#r-chain-as-system-output) |
| A preset that plays hot: EasyEffects resamples the kernel and keeps the gain (issue #84) | [easyeffects-and-pipewire.md#r-convolver-resample-gain](research/easyeffects-and-pipewire.md#r-convolver-resample-gain) |
| Presets written where EasyEffects stopped reading: the Flatpak's two XDG roots (issue #93) | [easyeffects-and-pipewire.md#r-flatpak-xdg-roots](research/easyeffects-and-pipewire.md#r-flatpak-xdg-roots) |
| The same bug on a native install: XDG_DATA_HOME / XDG_CONFIG_HOME | [easyeffects-and-pipewire.md#r-flatpak-xdg-roots](research/easyeffects-and-pipewire.md#r-flatpak-xdg-roots) |
| Rejected approaches → The `easyeffects` CLI for `--doctor`'s live state | [easyeffects-and-pipewire.md#r-ee-cli-live-state](research/easyeffects-and-pipewire.md#r-ee-cli-live-state) |
| Rejected approaches → Rewriting `{preset}.irs` in place and reloading | [easyeffects-and-pipewire.md#r-irs-in-place-rewrite](research/easyeffects-and-pipewire.md#r-irs-in-place-rewrite) |
| Rejected approaches → Caching the LV2 port schemas anywhere but in memory | [code-organisation.md#caching-the-lv2-port-schemas-anywhere-but-in-memory](code-organisation.md#caching-the-lv2-port-schemas-anywhere-but-in-memory) |
| DAX runs psychoacoustic VBE; the schema can't drive a per-device mapping | [virtual-bass.md#r-dax-virtual-bass](research/virtual-bass.md#r-dax-virtual-bass) |
| Phase 2 (2026-08): decoding `virtual-bass-subgains` and scoring a chain | [virtual-bass.md#r-dax-virtual-bass](research/virtual-bass.md#r-dax-virtual-bass) |
| SoundWire Calf BassEnhancer constants | [virtual-bass.md#r-soundwire-bass-enhancer-constants](research/virtual-bass.md#r-soundwire-bass-enhancer-constants) |
| Measurement outcome: dynamics plugins on the test stimuli | [adaptive-processing.md#r-dynamics-dormancy](research/adaptive-processing.md#r-dynamics-dormancy) |
| Why autogain is bypassed by default | [adaptive-processing.md#r-autogain-bypassed-by-default](research/adaptive-processing.md#r-autogain-bypassed-by-default) |
| The 2026-07 default-flip attempt (issue #25) | [adaptive-processing.md#r-autogain-default-flip](research/adaptive-processing.md#r-autogain-default-flip) |
| Translating active autogain to LSP `autogain_stereo` (PW converter) | [adaptive-processing.md#r-autogain-pw-translation](research/adaptive-processing.md#r-autogain-pw-translation) |
| Verified math (sanity checks) → Q15 block-rate time constants | [adaptive-processing.md#r-mbc-time-constant-decode](research/adaptive-processing.md#r-mbc-time-constant-decode) |
| DAX3 LTI behaviour for our stimuli | [adaptive-processing.md#r-dax-lti-behaviour](research/adaptive-processing.md#r-dax-lti-behaviour) |
| Dialog-enhancer gain ceiling | [adaptive-processing.md#r-dialog-enhancer-gain-ceiling](research/adaptive-processing.md#r-dialog-enhancer-gain-ceiling) |
| Surround→stereo-base | [adaptive-processing.md#r-surround-boost-stereo-base](research/adaptive-processing.md#r-surround-boost-stereo-base) |
| MBC ratio and time constants | [adaptive-processing.md#r-mbc-ratio-time-constants](research/adaptive-processing.md#r-mbc-ratio-time-constants) |
| Volume-leveler→autogain window | [adaptive-processing.md#r-leveler-autogain-window](research/adaptive-processing.md#r-leveler-autogain-window) |
| Conservative-autogain offsets | [adaptive-processing.md#r-conservative-autogain-offsets](research/adaptive-processing.md#r-conservative-autogain-offsets) |
| Approximate DAX's leveler / regulator | [adaptive-processing.md#r-dax-leveler-approximation](research/adaptive-processing.md#r-dax-leveler-approximation) |
| Rejected approaches → Noise gate before the compressor | [adaptive-processing.md#r-compressor-noise-gate](research/adaptive-processing.md#r-compressor-noise-gate) |
| Rejected approaches → An *unused* EasyEffects built-in to cover a dropped DAX feature | [adaptive-processing.md#r-unused-ee-builtins](research/adaptive-processing.md#r-unused-ee-builtins) |

### Legacy numbers

Commit messages, released CHANGELOG sections and issue comments cite these
numbers, and this table is their resolver. It is frozen: no rows are added.

| Old number | Tag | Heading |
|---|---|---|
| Finding 1 | [`r-dax-lti-behaviour`](research/adaptive-processing.md#r-dax-lti-behaviour) | DAX3 LTI behaviour for our stimuli |
| Finding 2 | [`r-dax-phase-response`](#r-dax-phase-response) | DAX3's phase response |
| Finding 3 | [`r-dax-response-vs-xml`](#r-dax-response-vs-xml) | DAX3 response vs the published XML curves |
| Finding 4 | [`r-ee-response-vs-xml`](#r-ee-response-vs-xml) | EE-on-Linux response vs the XML |
| Finding 5 | [`r-hf-shaping-block-audit`](#r-hf-shaping-block-audit) | Audit for a missed HF-shaping XML block |
| Finding 6 | [`r-ao-sign-variant-matrix`](#r-ao-sign-variant-matrix) | Testing hypotheses (a) and (b) |
| Finding 7 | [`r-xml-interpretation-hypotheses`](#r-xml-interpretation-hypotheses) | Five XML-interpretation hypotheses |
| Finding 8 | [`r-dax-virtual-bass`](research/virtual-bass.md#r-dax-virtual-bass) | DAX runs psychoacoustic VBE; the schema can't drive a per-device mapping |
| Finding 9 | [`r-ieq-amount-scaling`](#r-ieq-amount-scaling) | `ieq-amount` scaling and the HF gap (issue #13) |
| Finding 10 | [`r-simplified-schema-ao-units`](#r-simplified-schema-ao-units) | simplified-schema AO units on a second device (issue #44) |
| entry 1 | [`r-dialog-enhancer-gain-ceiling`](research/adaptive-processing.md#r-dialog-enhancer-gain-ceiling) | Dialog-enhancer gain ceiling |
| entry 2 | [`r-surround-boost-stereo-base`](research/adaptive-processing.md#r-surround-boost-stereo-base) | Surround→stereo-base |
| entry 3 | [`r-convolver-headroom-restore`](research/loudness-and-limiting.md#r-convolver-headroom-restore) | Convolver SoundWire headroom restore |
| entry 4 | [`r-regulator-slope-ratio`](research/loudness-and-limiting.md#r-regulator-slope-ratio) | Regulator slope→ratio |
| entry 5 | [`r-regulator-timbre-knee`](research/loudness-and-limiting.md#r-regulator-timbre-knee) | Regulator timbre→knee |
| entry 6 | [`r-mbc-ratio-time-constants`](research/adaptive-processing.md#r-mbc-ratio-time-constants) | MBC ratio and time constants |
| entry 7 | [`r-leveler-autogain-window`](research/adaptive-processing.md#r-leveler-autogain-window) | Volume-leveler→autogain window |
| entry 8 | [`r-peq-anti-clipping-trim`](research/loudness-and-limiting.md#r-peq-anti-clipping-trim) | PEQ anti-clipping trim |
| entry 9 | [`r-soundwire-bass-enhancer-constants`](research/virtual-bass.md#r-soundwire-bass-enhancer-constants) | SoundWire Calf BassEnhancer constants |
| entry 10 | [`r-conservative-autogain-offsets`](research/adaptive-processing.md#r-conservative-autogain-offsets) | Conservative-autogain offsets |
| entry 11 | [`r-fixed-dynamics-constants`](research/loudness-and-limiting.md#r-fixed-dynamics-constants) | Fixed dynamics constants |
| Follow-ups item 1 | [`r-single-block-xml-ab`](#r-single-block-xml-ab) | Stripped-down single-block tuning XML A/B on Windows |
| Follow-ups item 2 | [`r-hybrid-phase-matching`](#r-hybrid-phase-matching) | Match DAX's hybrid phase character |
| Follow-ups item 3 | [`r-dax-leveler-approximation`](research/adaptive-processing.md#r-dax-leveler-approximation) | Approximate DAX's leveler / regulator |
| Follow-ups item 4 | [`r-fit-to-dax-capture`](#r-fit-to-dax-capture) | Empirically tune the preset to match DAX's *captured* response, not the XML's published curves |
| Follow-ups item 5 | [`r-regulator-stress-amount`](research/loudness-and-limiting.md#r-regulator-stress-amount) | `regulator-stress-amount` mapping investigated and rejected |

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
    content (the
    [surround→stereo-base factor](research/adaptive-processing.md#r-surround-boost-stereo-base)).
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
[DAX virtual-bass finding](research/virtual-bass.md#r-dax-virtual-bass)
investigates it at length, including the `--enable-vbe` experiment. Issue
[#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14)
tracks the opt-in baseline. So there is nothing simplified-schema-specific to
add here. The explicit `boost`/`cutoff`/`width` look mappable to Calf
`bass_enhancer`, but being corpus-frozen, they would be a hardcoded baseline
rather than derived tuning.

Issue #22's field follow-up is at
[easyeffects-and-pipewire.md#r-preset-loads-but-inaudible](research/easyeffects-and-pipewire.md#r-preset-loads-but-inaudible).

## Plugin chain order

`make_preset` in `lib/preset/build.py` builds the chain in this order:

```
Convolver → [Bass Enhancer] → Equalizer (PEQ)
    → Dialog Enhancer EQ → Autogain → MB Compressor → Regulator → Limiter
```

`Bass Enhancer` is emitted only for SoundWire devices (harmonic bass
restoration, the
[DAX virtual-bass finding](research/virtual-bass.md#r-dax-virtual-bass)). A
`Stereo Tools` widener, mapped from `surround-boost`, used to sit after the bass
enhancer. It was removed 2026-06-13 after a DAX capture showed Dolby applies no
stereo widening on 2-channel content (the
[surround→stereo-base factor](research/adaptive-processing.md#r-surround-boost-stereo-base)).

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
| ~~stereo_tools#0~~ | — | (not emitted) | — | **Removed 2026-06-13.** The converter emits no stereo widener; `surround-boost` is not mapped (the [surround→stereo-base factor](research/adaptive-processing.md#r-surround-boost-stereo-base)). |
| equalizer#0 | `mode` | `"IIR"` | AUDIBLE | Biquad realisation of the per-band PEQ. Alternatives: FIR / FFT / SPM. FFT mode would reproduce the band targets exactly at every FFT bin instead of analytically. Open: candidate test. |
| equalizer#0 | `q-mode` | (none) | AUDIBLE | Resolved (2026-06): the EE 8.x equalizer schema we emit has no separate q-mode key. The Q convention is a property of the per-band filter family (`mode`), covered in the row below. |
| equalizer#0 | per-band `mode` | `"RLC (BT)"` | AUDIBLE | Filter family. Verified for HP-slope behavior (commit `944a8f3`). Bell-width convention: see the note below. |
| equalizer#0 | `split-channels` | `true` | AUDIBLE | Required: the Dolby PEQ is asymmetric L/R on most devices. Linking would force-symmetrise. |
| autogain#0 | `bypass` | `true` (HDA), `false` (SDW) | AUDIBLE | Documented in [Why autogain is bypassed by default](research/adaptive-processing.md#r-autogain-bypassed-by-default): re-enabling reintroduces pumping on quiet→loud transitions. |
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
2. **Look-ahead is the latency.**
   [Translating active autogain to LSP `autogain_stereo`](research/adaptive-processing.md#r-autogain-pw-translation)
   pins `lkahead` to `0.0` because "`lkahead=0` keeps it at zero over the
   PipeWire quantum (the hard constraint)". The comment at that line in
   `emit_autogain` calls lookahead "the only latency source (port 41)". On that
   reading, non-zero look-ahead is exactly what spends latency, and the two
   sites above spend it.
3. **Not ours to answer for.** `.claude/rules/dsp-fir.md` says the limiter's
   `lk` "is whatever the EasyEffects preset already carried rather than a value
   we chose". That holds only from `ee_to_pipewire.py`'s vantage, where the
   preset is an input. This repo *writes* that preset, in `make_limiter`, so at
   the project level the value is ours. The sentence is a third position in the
   disagreement, not a resolution of it.

**What the measurements on record do and don't cover.** Nothing measured here
currently bears on the mechanism clause either way. The 2026-06-22 EE-vs-PW
[proof](research/adaptive-processing.md#r-autogain-pw-translation) reports
identical capture onsets at 0.30 s. It ran an *autogain-only* preset with every
other stage stripped from `plugins_order`. The limiter and both MBCs were not in
that chain, so it cannot speak to either site. The full-chain capture from the
same session, and the EE↔PW equivalence residuals in `docs/ee-to-pipewire.md`,
are EE-*against*-PW comparisons in which both sides carry the same look-ahead.
They would catch a relative delay between the two paths, not an absolute one
against bypass.

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

## Verified math (sanity checks)

These sanity-checks are the derivations and accuracy measurements behind the
values catalogued in [reference.md](reference.md).

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

The nine findings from
[DAX LTI behaviour](research/adaptive-processing.md#r-dax-lti-behaviour) through
the [`ieq-amount` scaling finding](#r-ieq-amount-scaling) come from a ThinkPad
X1 Yoga Gen 7 with a Realtek ALC287, subsystem 17AA:22E6, which matches the
development tuning XML, `DEV_0287` keyed `SUBSYS_17AA22E6`. The
[simplified-schema AO units finding](#r-simplified-schema-ao-units) covers a
second device, a Yoga Slim 7 14ARE05 with the simplified schema, whose battery
arrived 2026-07-30 via issue
[#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44).

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
carry time-varying gain
([DAX LTI behaviour](research/adaptive-processing.md#r-dax-lti-behaviour)),
which Farina deconvolution folds into pre- and post-peak energy, biasing the
ratio toward looking linear-phase. If this characterization ever becomes
load-bearing, the multitone capture's per-band phase is the cleaner signal. No
converter decision rests on it.

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
   playback. EasyEffects' autogain is bypassed by default, as
   [Why autogain is bypassed by default](research/adaptive-processing.md#r-autogain-bypassed-by-default)
   explains. A content-adaptive leveler equivalent
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
[DAX-leveler approximation follow-up](research/adaptive-processing.md#r-dax-leveler-approximation).
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
[Rewriting `{preset}.irs` in place](research/easyeffects-and-pipewire.md#r-irs-in-place-rewrite).

**A note on metrics.** The `summarise_variants.py` output reports two residuals,
`vsDAX` and `vsXML`. They answer different questions, and `vsXML` is the weaker
signal:

- `vsDAX` is the residual against the captured DAX response, and the only
  external check. DAX captures are imperfect: they cover one device and one
  driver, and DAX itself is non-LTI per
  [DAX LTI behaviour](research/adaptive-processing.md#r-dax-lti-behaviour). They
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
[DAX-leveler approximation follow-up](research/adaptive-processing.md#r-dax-leveler-approximation).

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
(leveler + HF dynamics), not the EQ. No converter change indicated. The
[round-3 subsection](research/loudness-and-limiting.md#r-deep-threshold-bass-loss)
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
at the 1688 Hz notch](images/finding10-measured-vs-predicted.png)

**Dolby-off is a true bypass, and loopback taps post-APO.** The off-state
stepped and multitone captures are flat to −0.05 dB with zero cross-pass
adaptive span. So an off/on pair is a clean A/B, and the capture method needs
no correction for the off leg.

**Non-LTI behaviour reproduces on device 2**, consistent with
[DAX LTI behaviour](research/adaptive-processing.md#r-dax-lti-behaviour):

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
[fixed dynamics constants](research/loudness-and-limiting.md#r-fixed-dynamics-constants)
(f)). This relates to the regulator under-engagement thread (the
[MBC ratio and time constants](research/adaptive-processing.md#r-mbc-ratio-time-constants)
and the
[fixed dynamics constants](research/loudness-and-limiting.md#r-fixed-dynamics-constants)).
No converter change is indicated for it: chasing a content-adaptive layer with a
static chain is the same trade rejected in the
[AO sign variant matrix](#r-ao-sign-variant-matrix).

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
| [Dialog-enhancer gain ceiling](research/adaptive-processing.md#r-dialog-enhancer-gain-ceiling) | `dialog-enhancer-amount` (0–16) | default audible when `dialog-enhancer-enable=1`. X1 Yoga: `dynamic`/`movie` amount=5, `voice` amount=3, off on `music`/`game` |
| [Surround→stereo-base](research/adaptive-processing.md#r-surround-boost-stereo-base) ✅ | `surround-boost` (1/16 dB) | resolved: widening dropped. It was emitted when surround was present (`surround-boost=96` on `dynamic`/`movie`) |
| [Convolver SoundWire headroom restore](research/loudness-and-limiting.md#r-convolver-headroom-restore) ✅ | (none: a post-normalisation heuristic for the IEQ-only, no-AO SoundWire curve) | resolved: restore dropped. It was default audible on SoundWire |
| [Regulator slope→ratio](research/loudness-and-limiting.md#r-regulator-slope-ratio) | `regulator-distortion-slope` | regulator only engages at high level |
| [Regulator timbre→knee](research/loudness-and-limiting.md#r-regulator-timbre-knee) | `regulator-timbre-preservation` (corpus-frozen at 0.75) | regulator, high level |
| [MBC ratio and time constants](research/adaptive-processing.md#r-mbc-ratio-time-constants) | `mb-compressor-tuning` 6-tuples | dormant: the MBC doesn't engage on the −10 dBFS test stimuli (the [DAX response vs XML](#r-dax-response-vs-xml)) |
| [Volume-leveler→autogain window](research/adaptive-processing.md#r-leveler-autogain-window) | `volume-leveler-amount` (0–10) | bypassed by default on HDA, where `--enable autogain` opts in. Active in the conservative SoundWire path |
| [PEQ anti-clipping trim](research/loudness-and-limiting.md#r-peq-anti-clipping-trim) | (none: a headroom heuristic over the XML's PEQ gains) | default audible on every XML whose PEQ has boost bells/shelves |
| [SoundWire Calf BassEnhancer constants](research/virtual-bass.md#r-soundwire-bass-enhancer-constants) | (none: the XML's `bass-enhancer-*`/VBE fields are corpus-frozen; the [DAX virtual-bass finding](research/virtual-bass.md#r-dax-virtual-bass)) | default audible on SoundWire, the most audible invented stage on those devices |
| [Conservative-autogain offsets](research/adaptive-processing.md#r-conservative-autogain-offsets) | `volume-leveler-out-target` | active on SoundWire; audible on HDA only via `--enable autogain` or manual GUI enable |
| [Fixed dynamics constants](research/loudness-and-limiting.md#r-fixed-dynamics-constants) | (none) | dormant at nominal levels (the [dynamics-dormant measurement](research/adaptive-processing.md#r-dynamics-dormancy); device-specific, see the end of the [fixed dynamics constants](research/loudness-and-limiting.md#r-fixed-dynamics-constants)); engaged on loud content |

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
     ([dialog-enhancer gain ceiling](research/adaptive-processing.md#r-dialog-enhancer-gain-ceiling)):
     done, result negative/refining.** Across the X1 Yoga pink battery, the
     profiles that differ only in DE amount do not differ in steady-state
     magnitude. `movie` (amount=5) vs `game` (amount=0) is ~0.01 dB RMS in-band,
     and `dynamic`/`movie`/`game` all sit within ~1 dB RMS despite DE 5/5/0, far
     below the modelled ~1.25 dB bell. Profiles with *identical* IEQ+AO (`music`
     vs `game`, both DE off) differ by ~4 dB RMS. So per-profile MI voicing
     ([DAX LTI behaviour](research/adaptive-processing.md#r-dax-lti-behaviour),
     non-LTI) dwarfs and is uncorrelated with DE amount. So the DE is
     content-adaptive (speech-gated): pink cannot excite it, and cross-profile
     differencing on pink is the wrong test. Validating the 6/8 dB ceiling needs
     a speech / speech-shaped stimulus. Because MI voicing differs per profile,
     it also needs a *same-profile* DE-on-vs-off capture rather than a
     cross-profile comparison.
   - **Surround
     ([surround→stereo-base factor](research/adaptive-processing.md#r-surround-boost-stereo-base)):
     screened, not testable offline.** The captured battery uses correlated pink
     (`stimulus_pink`, corr +1.0), which has no Side component for the widener
     to act on. Falsifying the `/20` mapping needs the decorrelated
     `stimulus_stereo_pink` captured in stereo (Phase 3).
   - **Regulator
     ([regulator slope→ratio factor](research/loudness-and-limiting.md#r-regulator-slope-ratio),
     [regulator timbre→knee factor](research/loudness-and-limiting.md#r-regulator-timbre-knee)):
     not testable on this device.** The X1 Yoga's slope and timbre values are
     identical on every profile, so there is no operating-point variation to fit
     `1/(1−slope)` or `−6·timbre`. These need a device with non-default values
     (Phase 4), independent of stimulus.
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
     half of the
     [surround→stereo-base factor](research/adaptive-processing.md#r-surround-boost-stereo-base):
     +4.10 dB S/M at surr=96. The new `stimulus_stepped_loud` (−2 dBFS peak)
     crosses the ≈ −6.4 dBFS MBC knee. The EE dynamics wake exactly at the
     high-chain-gain bands, −5.5 dB GR @234 Hz / −3.2 dB @2.25 kHz
     ([MBC ratio and time constants](research/adaptive-processing.md#r-mbc-ratio-time-constants),
     [fixed dynamics constants](research/loudness-and-limiting.md#r-fixed-dynamics-constants)).
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
     - **[Surround→stereo-base factor](research/adaptive-processing.md#r-surround-boost-stereo-base),
       over-application found:** DAX widening at surr=96 is zero (S/M-delta
       +0.01 dB, identical to surr=0/off); our chain adds +4.10 dB.
     - **[MBC ratio and time constants](research/adaptive-processing.md#r-mbc-ratio-time-constants)
       and
       [fixed dynamics constants](research/loudness-and-limiting.md#r-fixed-dynamics-constants),
       DAX compresses ~2× harder** at loud level: −10.6 dB GR @234 Hz vs EE
       −5.5, strong over 140–400 Hz and 1.9–4.7 kHz. This is entangled with the
       bass-level gap.
     - **[Dialog-enhancer gain ceiling](research/adaptive-processing.md#r-dialog-enhancer-gain-ceiling),
       no DE signature** on espeak speech (`movie` DE=5 ≡ `game` DE=0 to ±0.00
       dB), unresolved: the robotic voice may not trigger MI, and Dolby Access
       offered no movie DE toggle.
     - **[PEQ anti-clipping trim](research/loudness-and-limiting.md#r-peq-anti-clipping-trim),
       still confounded** by the leveler's common loudness target even at pinned
       volume.
3. *User-contributed data.* The X1 Yoga is an HDA device and is corpus-frozen
   on several fields, so some entries can only be falsified by other
   hardware/XMLs:
   - a SoundWire device for the
     [SoundWire bass-enhancer constants](research/virtual-bass.md#r-soundwire-bass-enhancer-constants)
     and the
     [conservative-autogain offsets](research/adaptive-processing.md#r-conservative-autogain-offsets),
     which need a SoundWire-device DAX capture (the #29 Zenbook S14 is the first
     candidate);
   - a device with `ieq-amount≠10` for the
     [`ieq-amount` scaling finding](#r-ieq-amount-scaling) residual;
   - a device with `regulator-timbre-preservation≠0.75` or a differing
     `regulator-distortion-slope` for the
     [regulator slope→ratio factor](research/loudness-and-limiting.md#r-regulator-slope-ratio)
     and the
     [regulator timbre→knee factor](research/loudness-and-limiting.md#r-regulator-timbre-knee).

   The
   [convolver headroom restore](research/loudness-and-limiting.md#r-convolver-headroom-restore)
   and the SoundWire dialog arm of the
   [dialog-enhancer gain ceiling](research/adaptive-processing.md#r-dialog-enhancer-gain-ceiling)
   owed no capture: they were instead *removed* 2026-07-03 as invented gains
   compensating the since-fixed #13 IEQ over-application. The
   [PEQ anti-clipping trim](research/loudness-and-limiting.md#r-peq-anti-clipping-trim),
   the
   [SoundWire bass-enhancer constants](research/virtual-bass.md#r-soundwire-bass-enhancer-constants),
   the
   [conservative-autogain offsets](research/adaptive-processing.md#r-conservative-autogain-offsets)
   and the
   [fixed dynamics constants](research/loudness-and-limiting.md#r-fixed-dynamics-constants)
   (added in the 2026-06 review) piggyback on the same campaign. The
   [PEQ anti-clipping trim](research/loudness-and-limiting.md#r-peq-anti-clipping-trim)
   needs narrow-vs-wide-Q profile captures on existing HDA hardware, and the
   [fixed dynamics constants](research/loudness-and-limiting.md#r-fixed-dynamics-constants)
   folds into the loud-content captures that wake the dynamics stages (the
   [MBC ratio and time constants](research/adaptive-processing.md#r-mbc-ratio-time-constants)).
   The converter's `_UNMODELED_FEATURES` warning already nudges users to report
   XMLs whose `regulator-overdrive`/`relaxation-amount` deviate from the corpus
   constants. Track the asks on a GitHub issue (cf. issue
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

<a id="r-hybrid-phase-matching"></a>

#### Match DAX's hybrid phase character

Status: out of scope unless a constraint changes.

It needs a partial-linear-phase FIR, which adds ~20–40 ms group delay. The
no-added-latency constraint rules it out, and relaxing that needs an explicit
decision. The `--fir-phase=linphase` flag is the upper-bound experiment for
this. The [AO sign variant matrix](#r-ao-sign-variant-matrix) shows pure
linear-phase doesn't help magnitude.

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
