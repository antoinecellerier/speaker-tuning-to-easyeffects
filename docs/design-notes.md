# Design notes

> Interpretive analysis of the parameter schema used by Dolby DAX3 tuning XML (distributed publicly as part of Windows audio driver packages), for the purpose of Linux interoperability. No verbatim tuning arrays are reproduced.

This doc covers the architectural *why* behind the generated EasyEffects preset,
so future readers don't have to reverse-engineer it from commit history.
[reference.md](reference.md) covers *what* the script emits: mappings, plugin
chain, units and what's not implemented.

> **This file and docs/research/ are the research log.** Findings appear in
> roughly the order they were established. Superseded hypotheses stay for the
> audit trail. See the banners in
> [eq-and-frequency-response.md](research/eq-and-frequency-response.md) that mark
> text superseded by the
> [`ieq-amount` scaling finding](research/eq-and-frequency-response.md#r-ieq-amount-scaling).
> For the settled current-state summary, start with [reference.md](reference.md).
> For the open threads worth picking up, see "Unvalidated converter scaling
> factors" and "Follow-ups to close the gap to DAX" further down.

## Where the research lives

| Class file | What it holds |
|---|---|
| [hardware-and-drivers.md](research/hardware-and-drivers.md) | kernel, codec pins and routing, smart amps, firmware |
| [easyeffects-and-pipewire.md](research/easyeffects-and-pipewire.md) | EasyEffects, PipeWire, WirePlumber, Flatpak, paths and sample rate |
| [virtual-bass.md](research/virtual-bass.md) | DAX's virtual bass engine (VBE), the Calf bass enhancer and `--enable virtual-bass` |
| [adaptive-processing.md](research/adaptive-processing.md) | the volume leveler and autogain, the multi-band compressor (MBC), the dialog enhancer, surround and media intelligence (MI) steering |
| [loudness-and-limiting.md](research/loudness-and-limiting.md) | `volmax-boost` and its slot, peak normalisation and `--enable level-restore`, the per-band regulator, the brickwall limiter and the PEQ anti-clipping trim |
| [eq-and-frequency-response.md](research/eq-and-frequency-response.md) | the IEQ and its `ieq-amount` weight, the audio optimizer and its XML units, the PEQ curve, the FIR and its phase |

The rest of this file is being split by class into `docs/research/`.

### Issues

| Issue | Material | Where |
|---|---|---|
| #4 | an IdeaPad XML giving a comparable IEQ shape in the parametric-EQ rejection | [eq-and-frequency-response.md#r-parametric-eq-approximation](research/eq-and-frequency-response.md#r-parametric-eq-approximation) |
| #11 | DAX's tier-2 adaptive sub-models; `regulator-stress-amount` tested as a threshold offset; its long-IR-tail note, behind the rejected IR trim, is at [eq-and-frequency-response.md#r-convolver-ir-trim](research/eq-and-frequency-response.md#r-convolver-ir-trim) | [loudness-and-limiting.md#r-regulator-stress-amount](research/loudness-and-limiting.md#r-regulator-stress-amount) |
| #13 | `ieq-amount` read as a percentage; FIR construction tweaks and mixed phase rejected | [eq-and-frequency-response.md#r-ieq-amount-scaling](research/eq-and-frequency-response.md#r-ieq-amount-scaling) |
| #14 | VBE on HDA devices and `--enable virtual-bass`; the corpus-frozen `bass-enhancer-*`/`virtual-bass-*` fields are at [eq-and-frequency-response.md#r-simplified-schema-gain-arrays](research/eq-and-frequency-response.md#r-simplified-schema-gain-arrays) | [virtual-bass.md#r-dax-virtual-bass](research/virtual-bass.md#r-dax-virtual-bass) |
| #15 | the `/16`-dB convention, generalised to the simplified schema | [eq-and-frequency-response.md#r-simplified-schema-ao-units](research/eq-and-frequency-response.md#r-simplified-schema-ao-units) |
| #18 | single pin, a 2-driver laptop per PSREF | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #22 | field follow-up: the preset loads but is inaudible; the simplified schema's `gain_l`/`gain_r` audio optimizer and the elements we don't read are at [eq-and-frequency-response.md#r-simplified-schema-gain-arrays](research/eq-and-frequency-response.md#r-simplified-schema-gain-arrays) | [easyeffects-and-pipewire.md#r-preset-loads-but-inaudible](research/easyeffects-and-pipewire.md#r-preset-loads-but-inaudible) |
| #23 | `volmax-boost` slot: `input-gain` default, `output-gain` opt-out; volmax landing within ~1 dB of DAX's loud-level makeup on #44's device is at [eq-and-frequency-response.md#r-simplified-schema-ao-units](research/eq-and-frequency-response.md#r-simplified-schema-ao-units) | [loudness-and-limiting.md#r-volmax-boost-slot](research/loudness-and-limiting.md#r-volmax-boost-slot) |
| #25 | autogain's HDA default flip, rejected; the −50 dB silence gate; the leveler gap quantified on #44's device is at [eq-and-frequency-response.md#r-simplified-schema-ao-units](research/eq-and-frequency-response.md#r-simplified-schema-ao-units) | [adaptive-processing.md#r-autogain-default-flip](research/adaptive-processing.md#r-autogain-default-flip) |
| #27 | amps read as speakers; the bass-enhancer field report is at [virtual-bass.md#r-soundwire-bass-enhancer-constants](research/virtual-bass.md#r-soundwire-bass-enhancer-constants); the inert-regulator volmax caveat is at [loudness-and-limiting.md#r-volmax-boost-slot](research/loudness-and-limiting.md#r-volmax-boost-slot), and the all-zero-threshold coupled-bands A/B at [loudness-and-limiting.md#r-fixed-dynamics-constants](research/loudness-and-limiting.md#r-fixed-dynamics-constants) | [hardware-and-drivers.md#r-smart-amp-families](research/hardware-and-drivers.md#r-smart-amp-families) |
| #29 | CS42L43 excluded as a jack codec; the bass-enhancer field rounds are at [virtual-bass.md#r-soundwire-bass-enhancer-constants](research/virtual-bass.md#r-soundwire-bass-enhancer-constants); the removed SoundWire dialog-enhancer arm's field evidence is at [adaptive-processing.md#r-dialog-enhancer-gain-ceiling](research/adaptive-processing.md#r-dialog-enhancer-gain-ceiling); the removed convolver headroom restore's field evidence is at [loudness-and-limiting.md#r-convolver-headroom-restore](research/loudness-and-limiting.md#r-convolver-headroom-restore) | [hardware-and-drivers.md#r-amp-parts-rejected](research/hardware-and-drivers.md#r-amp-parts-rejected) |
| #30 | two pins, PSREF names woofers and tweeters | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #33 | kernel 6.12 → 7.0 fix, old-kernel hint | [hardware-and-drivers.md#r-kernel-misconfigured-codec](research/hardware-and-drivers.md#r-kernel-misconfigured-codec) |
| #36 | single pin, a 2-driver laptop per PSREF; the reporter's enable-autogain recommendation is at [adaptive-processing.md#r-autogain-bypassed-by-default](research/adaptive-processing.md#r-autogain-bypassed-by-default) | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #39 | crackle; rule out the kernel (TAS2781 calibration differs by kernel lineage) | [hardware-and-drivers.md#r-kernel-misconfigured-codec](research/hardware-and-drivers.md#r-kernel-misconfigured-codec) |
| #44 | single pin, a 2-driver laptop per PSREF; DAX applying no VBE is at [virtual-bass.md#r-dax-virtual-bass](research/virtual-bass.md#r-dax-virtual-bass); the deep-threshold regulator and `--volmax-slot output-gain` are at [loudness-and-limiting.md#r-deep-threshold-bass-loss](research/loudness-and-limiting.md#r-deep-threshold-bass-loss); the DAX capture confirming the simplified schema's AO units is at [eq-and-frequency-response.md#r-simplified-schema-ao-units](research/eq-and-frequency-response.md#r-simplified-schema-ao-units) | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #45 | the T495's successor, the T14 Gen 1 AMD, reported working from a driver package we don't hold | [loudness-and-limiting.md#r-gain-rail-tuning](research/loudness-and-limiting.md#r-gain-rail-tuning) |
| #46 | the tuning pinned at the gain rail; single pin, a 2-driver laptop per PSREF, is at [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) | [loudness-and-limiting.md#r-gain-rail-tuning](research/loudness-and-limiting.md#r-gain-rail-tuning) |
| #50 | `--enable level-restore`; single pin, a 2-driver laptop per PSREF, and its missing `38dc` quirk entry (its smart amp, not a bass pin) are at [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) | [loudness-and-limiting.md#r-level-restore](research/loudness-and-limiting.md#r-level-restore) |
| #51 | two pins, PSREF names woofers and tweeters | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #53 | hidden woofer pin | [hardware-and-drivers.md#r-woofer-pin-hidden](research/hardware-and-drivers.md#r-woofer-pin-hidden) |
| #63 | the chain selected as the system output: two sinks in series | [easyeffects-and-pipewire.md#r-chain-as-system-output](research/easyeffects-and-pipewire.md#r-chain-as-system-output) |
| #73 | the generated voicings sit at most ~1 dB apart under the `/100` weight; DAX's own Detailed−Warm delta is unmeasured | [eq-and-frequency-response.md#r-ieq-amount-scaling](research/eq-and-frequency-response.md#r-ieq-amount-scaling) |
| #84 | EasyEffects plays hot above 48 kHz; the deep-threshold regulator is at [loudness-and-limiting.md#r-deep-threshold-distortion](research/loudness-and-limiting.md#r-deep-threshold-distortion) | [easyeffects-and-pipewire.md#r-convolver-resample-gain](research/easyeffects-and-pipewire.md#r-convolver-resample-gain) |
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
| Per-channel regulator thresholds: newer SoundWire schema (`SUBSYS_37A317AA`) | [loudness-and-limiting.md#r-per-channel-regulator-thresholds](research/loudness-and-limiting.md#r-per-channel-regulator-thresholds) |
| Gain-staging budget | [loudness-and-limiting.md#r-gain-staging-budget](research/loudness-and-limiting.md#r-gain-staging-budget) |
| `volmax-boost` slot: `input-gain` vs `output-gain` (issue #23) | [loudness-and-limiting.md#r-volmax-boost-slot](research/loudness-and-limiting.md#r-volmax-boost-slot) |
| Why the PEQ `output-gain` stays a single global `max(L,R)` (not per-channel) | [loudness-and-limiting.md#r-gain-staging-budget](research/loudness-and-limiting.md#r-gain-staging-budget) |
| Why bypass has more bass than the preset (issue #44, round 3, 2026-08-22) | [loudness-and-limiting.md#r-deep-threshold-bass-loss](research/loudness-and-limiting.md#r-deep-threshold-bass-loss) |
| Second deep-threshold tuning: issue #84's Yoga Slim 7 Pro 14ACH5 (2026-08-30) | [loudness-and-limiting.md#r-deep-threshold-distortion](research/loudness-and-limiting.md#r-deep-threshold-distortion) |
| Convolver SoundWire headroom restore | [loudness-and-limiting.md#r-convolver-headroom-restore](research/loudness-and-limiting.md#r-convolver-headroom-restore) |
| Regulator slope→ratio | [loudness-and-limiting.md#r-regulator-slope-ratio](research/loudness-and-limiting.md#r-regulator-slope-ratio) |
| Regulator timbre→knee | [loudness-and-limiting.md#r-regulator-timbre-knee](research/loudness-and-limiting.md#r-regulator-timbre-knee) |
| PEQ anti-clipping trim | [loudness-and-limiting.md#r-peq-anti-clipping-trim](research/loudness-and-limiting.md#r-peq-anti-clipping-trim) |
| Fixed dynamics constants | [loudness-and-limiting.md#r-fixed-dynamics-constants](research/loudness-and-limiting.md#r-fixed-dynamics-constants) |
| `regulator-stress-amount` mapping investigated and rejected | [loudness-and-limiting.md#r-regulator-stress-amount](research/loudness-and-limiting.md#r-regulator-stress-amount) |
| A tuning pinned at the gain rail: the T495 (issue #46) | [loudness-and-limiting.md#r-gain-rail-tuning](research/loudness-and-limiting.md#r-gain-rail-tuning) |
| Giving back what normalisation removed: `--enable level-restore` (issue #50) | [loudness-and-limiting.md#r-level-restore](research/loudness-and-limiting.md#r-level-restore) |
| Measured on the dev device, 2026-08-04 | [loudness-and-limiting.md#r-level-restore](research/loudness-and-limiting.md#r-level-restore) |
| Measured on a second device, 2026-08-04/05 | [loudness-and-limiting.md#r-level-restore](research/loudness-and-limiting.md#r-level-restore) |
| Heard on the dev device, 2026-08-18 | [loudness-and-limiting.md#r-level-restore](research/loudness-and-limiting.md#r-level-restore) |
| Simplified-schema XMLs: `gain_l`/`gain_r` audio-optimizer (issue #22) | [eq-and-frequency-response.md#r-simplified-schema-gain-arrays](research/eq-and-frequency-response.md#r-simplified-schema-gain-arrays) |
| Verified math (sanity checks) | [eq-and-frequency-response.md#r-fir-accuracy](research/eq-and-frequency-response.md#r-fir-accuracy) |
| DAX3's phase response | [eq-and-frequency-response.md#r-dax-phase-response](research/eq-and-frequency-response.md#r-dax-phase-response) |
| DAX3 response vs the published XML curves | [eq-and-frequency-response.md#r-dax-response-vs-xml](research/eq-and-frequency-response.md#r-dax-response-vs-xml) |
| EE-on-Linux response vs the XML | [eq-and-frequency-response.md#r-ee-response-vs-xml](research/eq-and-frequency-response.md#r-ee-response-vs-xml) |
| Audit for a missed HF-shaping XML block | [eq-and-frequency-response.md#r-hf-shaping-block-audit](research/eq-and-frequency-response.md#r-hf-shaping-block-audit) |
| Testing hypotheses (a) and (b) | [eq-and-frequency-response.md#r-ao-sign-variant-matrix](research/eq-and-frequency-response.md#r-ao-sign-variant-matrix) |
| Implications for the converter | [eq-and-frequency-response.md#r-dax-gap-implications](research/eq-and-frequency-response.md#r-dax-gap-implications) |
| Five XML-interpretation hypotheses | [eq-and-frequency-response.md#r-xml-interpretation-hypotheses](research/eq-and-frequency-response.md#r-xml-interpretation-hypotheses) |
| `ieq-amount` scaling and the HF gap (issue #13) | [eq-and-frequency-response.md#r-ieq-amount-scaling](research/eq-and-frequency-response.md#r-ieq-amount-scaling) |
| simplified-schema AO units on a second device (issue #44) | [eq-and-frequency-response.md#r-simplified-schema-ao-units](research/eq-and-frequency-response.md#r-simplified-schema-ao-units) |
| Match DAX's hybrid phase character | [eq-and-frequency-response.md#r-hybrid-phase-matching](research/eq-and-frequency-response.md#r-hybrid-phase-matching) |
| Empirically tune the preset to match DAX's *captured* response, not the XML's published curves | [eq-and-frequency-response.md#r-fit-to-dax-capture](research/eq-and-frequency-response.md#r-fit-to-dax-capture) |
| Rejected approaches → `filter_coefficients` as an audio EQ source | [eq-and-frequency-response.md#r-filter-coefficients-blob](research/eq-and-frequency-response.md#r-filter-coefficients-blob) |
| Rejected approaches → Parametric-EQ approximation of the IEQ curve | [eq-and-frequency-response.md#r-parametric-eq-approximation](research/eq-and-frequency-response.md#r-parametric-eq-approximation) |
| Rejected approaches → Auto-trimming the convolver IR to its audible length | [eq-and-frequency-response.md#r-convolver-ir-trim](research/eq-and-frequency-response.md#r-convolver-ir-trim) |
| Closed follow-ups | [eq-and-frequency-response.md#r-variant-sweep](research/eq-and-frequency-response.md#r-variant-sweep) |
| The variant sweep | [eq-and-frequency-response.md#r-variant-sweep](research/eq-and-frequency-response.md#r-variant-sweep) |

### Legacy numbers

Commit messages, released CHANGELOG sections and issue comments cite these
numbers, and this table is their resolver. It is frozen: no rows are added.

| Old number | Tag | Heading |
|---|---|---|
| Finding 1 | [`r-dax-lti-behaviour`](research/adaptive-processing.md#r-dax-lti-behaviour) | DAX3 LTI behaviour for our stimuli |
| Finding 2 | [`r-dax-phase-response`](research/eq-and-frequency-response.md#r-dax-phase-response) | DAX3's phase response |
| Finding 3 | [`r-dax-response-vs-xml`](research/eq-and-frequency-response.md#r-dax-response-vs-xml) | DAX3 response vs the published XML curves |
| Finding 4 | [`r-ee-response-vs-xml`](research/eq-and-frequency-response.md#r-ee-response-vs-xml) | EE-on-Linux response vs the XML |
| Finding 5 | [`r-hf-shaping-block-audit`](research/eq-and-frequency-response.md#r-hf-shaping-block-audit) | Audit for a missed HF-shaping XML block |
| Finding 6 | [`r-ao-sign-variant-matrix`](research/eq-and-frequency-response.md#r-ao-sign-variant-matrix) | Testing hypotheses (a) and (b) |
| Finding 7 | [`r-xml-interpretation-hypotheses`](research/eq-and-frequency-response.md#r-xml-interpretation-hypotheses) | Five XML-interpretation hypotheses |
| Finding 8 | [`r-dax-virtual-bass`](research/virtual-bass.md#r-dax-virtual-bass) | DAX runs psychoacoustic VBE; the schema can't drive a per-device mapping |
| Finding 9 | [`r-ieq-amount-scaling`](research/eq-and-frequency-response.md#r-ieq-amount-scaling) | `ieq-amount` scaling and the HF gap (issue #13) |
| Finding 10 | [`r-simplified-schema-ao-units`](research/eq-and-frequency-response.md#r-simplified-schema-ao-units) | simplified-schema AO units on a second device (issue #44) |
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
| Follow-ups item 2 | [`r-hybrid-phase-matching`](research/eq-and-frequency-response.md#r-hybrid-phase-matching) | Match DAX's hybrid phase character |
| Follow-ups item 3 | [`r-dax-leveler-approximation`](research/adaptive-processing.md#r-dax-leveler-approximation) | Approximate DAX's leveler / regulator |
| Follow-ups item 4 | [`r-fit-to-dax-capture`](research/eq-and-frequency-response.md#r-fit-to-dax-capture) | Empirically tune the preset to match DAX's *captured* response, not the XML's published curves |
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
the
[`ieq-amount` scaling finding](research/eq-and-frequency-response.md#r-ieq-amount-scaling)
come from a ThinkPad X1 Yoga Gen 7 with a Realtek ALC287, subsystem 17AA:22E6,
which matches the development tuning XML, `DEV_0287` keyed `SUBSYS_17AA22E6`.
The
[simplified-schema AO units finding](research/eq-and-frequency-response.md#r-simplified-schema-ao-units)
covers a second device, a Yoga Slim 7 14ARE05 with the simplified schema, whose
battery arrived 2026-07-30 via issue
[#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44).

### Unvalidated converter scaling factors (the `ieq-amount` class)

The converter carries a cluster of scaling factors that map an XML field onto a
filter parameter through a constant we *invented* rather than confirmed. The
[`ieq-amount` scaling finding](research/eq-and-frequency-response.md#r-ieq-amount-scaling)
showed the risk: it corrected a scaling *interpretation*, not an arithmetic
slip. `ieq-amount` was read as `amount/10` when the field is a percentage,
`amount/100`. The `/16`-dB convention is the one such constant we have actually
verified. In issue
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
| [MBC ratio and time constants](research/adaptive-processing.md#r-mbc-ratio-time-constants) | `mb-compressor-tuning` 6-tuples | dormant: the MBC doesn't engage on the −10 dBFS test stimuli (the [DAX response vs XML](research/eq-and-frequency-response.md#r-dax-response-vs-xml)) |
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
     [`ieq-amount` scaling finding](research/eq-and-frequency-response.md#r-ieq-amount-scaling)
     for all five profiles. Pink residuals reproduce the
     [`ieq-amount` scaling finding](research/eq-and-frequency-response.md#r-ieq-amount-scaling)'s
     table (`dynamic` 1.03 dB RMS), and `music`'s ~3.5 dB MI-voicing outlier is
     unchanged. On the new speech stimulus, EE treats speech and pink
     identically, confirming our DE is static where DAX's is gated. The new
     decorrelated stereo gives the EE half of the
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
     - **[`ieq-amount` scaling finding](research/eq-and-frequency-response.md#r-ieq-amount-scaling)
       confirmed on a second DAX session.** Pink EE−DAX RMS is 0.97–1.49 dB
       across profiles, and the 19.7 kHz Δ is −1.5…−3.2 dB (vs −28 dB before the
       [`ieq-amount` scaling finding](research/eq-and-frequency-response.md#r-ieq-amount-scaling)).
       The `/100` HF fix holds. `music`, the prior 3.5 dB outlier, is now 1.09
       dB.
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
     [`ieq-amount` scaling finding](research/eq-and-frequency-response.md#r-ieq-amount-scaling)
     residual;
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
[`ieq-amount` scaling finding](research/eq-and-frequency-response.md#r-ieq-amount-scaling)
met it via convergent second-device evidence, two independent methods on a Yoga
Slim 7x, though not yet a second DAX *capture*. Its "residual open question"
tracks what a capture would add.

### Follow-ups to close the gap to DAX

After the
[`ieq-amount` scaling finding](research/eq-and-frequency-response.md#r-ieq-amount-scaling),
two gaps remain: the ~1 dB HF residual and the LF/leveler gap. Closing them
needs either data outside the XML or a relaxation of the determinism / latency
constraints. When this list was assembled, the cheap, deterministic, XML-only
experiments looked exhausted: hypothesis (b) rejected (the
[AO sign variant matrix](research/eq-and-frequency-response.md#r-ao-sign-variant-matrix)),
no missed XML block (the
[HF-shaping block audit](research/eq-and-frequency-response.md#r-hf-shaping-block-audit)),
5-profile coverage in (the
[AO sign variant matrix](research/eq-and-frequency-response.md#r-ao-sign-variant-matrix)).
The
[`ieq-amount` scaling finding](research/eq-and-frequency-response.md#r-ieq-amount-scaling)
then closed most of the HF gap with exactly such an experiment, a re-reading of
a field we already parsed, so "exhausted" was wrong. Items are cited by their
`r-` tags. Items are grouped by status.

**Closed by the variant sweep (the
[AO sign variant matrix](research/eq-and-frequency-response.md#r-ao-sign-variant-matrix)
and the
[XML-interpretation hypotheses](research/eq-and-frequency-response.md#r-xml-interpretation-hypotheses)):**
listed under
[The variant sweep](research/eq-and-frequency-response.md#r-variant-sweep).

<a id="r-single-block-xml-ab"></a>

#### Stripped-down single-block tuning XML A/B on Windows

Status: still actionable, no constraint change.

It remains the sharpest tool for what is left: pinpointing which DAX stage
carries the ~1 dB HF residual and the LF/leveler behavior. Disable everything
except IEQ in a tuning XML and capture DAX, then add AO, then add per-band PEQ,
and so on. The HF/mid gap from before the
[`ieq-amount` scaling finding](research/eq-and-frequency-response.md#r-ieq-amount-scaling)
that motivated it is now mostly closed. The risk is unchanged, so weigh it
against that much smaller payoff. The A/B needs driver-level XML replacement,
and could brick DAX on the test machine until restoration. Scope it before
attempting.

## Rejected approaches

Ideas investigated and explicitly declined, recorded so they don't get
re-proposed:

- **GPU compute for FIR convolution.** The FIR convolver uses <0.1% of a single
  CPU core, so there's no CPU pressure. CPU→GPU round-trip latency is
  unacceptable for realtime audio. See `docs/alternative-pipelines.md` Option 5.
- **Custom SOF DSP topology with FIR + DRC modules.** It has the highest offload
  potential, but requires rebuilding signed firmware and custom topology files.
  That is too much maintenance burden for a workstation tool. See
  `docs/alternative-pipelines.md` Option 2.

[Filter.cpp]: https://github.com/lsp-plugins/lsp-dsp-units/blob/master/src/main/filters/Filter.cpp
