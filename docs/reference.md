# Reference — what the converter does today

The settled current state: how DAX3 XML fields map to the generated
EasyEffects preset, the plugin chain, units, and what's deliberately left
out. This is the **what**. For the **why** (the research log, superseded
hypotheses, what was attempted) see [design-notes.md](design-notes.md); for
the empirical picture across ~2,500 DAX3 files see
[cross-device-findings.md](cross-device-findings.md).

> **Core invariant:** every parameter emitted traces to a parsed DAX3 XML
> field — no per-device hand-tuned offsets. The XML→param mappings below are
> hypotheses about the schema; DAX captures are the only signal that can
> falsify them. See [design-notes.md](design-notes.md) for the evidence
> behind each.

## Units & conventions

- **IEQ and audio-optimizer values** — stored in **1/16 dB**; divide by 16
  for dB. Confirmed by `geq_maximum_range=192` = 12 dB (standard graphic-EQ
  range).
- **Speaker PEQ gains** — already in dB (float attributes, e.g.
  `gain="-4.000000"`).
- **`ieq-amount`** — a **percentage** weight on the IEQ voicing (`scale =
  amount/100`); `10` applies the IEQ curve at 10 % on top of the
  audio-optimizer correction, not at full depth. (See design-notes
  "Finding 9" — this `/100` reading is DAX-capture-validated.)

## Input: Dolby DAX3 XML

The XML (`DEV_0287_SUBSYS_*.xml` and SoundWire variants) carries two
processing stages:

- **`tuning-cp`** (Content Processing) — software DSP: IEQ, graphic EQ,
  dialog enhancer, surround decoder, volume leveler.
- **`tuning-vlldp`** (Very Low Latency Driver Path) — hardware-level DSP:
  audio-optimizer (speaker correction), speaker PEQ, multi-band compressor,
  regulator.

For the full annotated XML tree, see design-notes.md ("XML structure") and
cross-device-findings.md.

## Output: the plugin chain

Up to eight plugins, chained in this order (seven on HDA devices — the bass
enhancer is SoundWire-only):

| # | Plugin | Source XML | What it does |
|---|--------|-----------|--------------|
| 1 | **Convolver** | IEQ target + `audio-optimizer-bands` | Min-phase FIR implementing the combined IEQ + speaker-correction curve |
| 2 | **Bass Enhancer** (Calf) | IEQ-only SoundWire curve | Harmonic bass restoration on SoundWire speakers; **SoundWire-only** |
| 3 | **Equalizer** | `speaker-peq-filters` | 4th-order high-pass at 100 Hz (speaker protection) + per-channel PEQ bells/shelves/HP-LP |
| 4 | **Dialog Enhancer** | `dialog-enhancer-amount` | Speech-band EQ boost at 2.5 kHz (2nd equalizer instance); on most profiles except music |
| 5 | **Autogain** | `volume-leveler-*` | Volume leveler; **bypassed by default on HDA** (`--enable autogain` opts in; active on SoundWire — see below). Placed before the compressor to match Dolby's CP→VLLDP flow |
| 6 | **Multiband Compressor** | `mb-compressor-tuning` | 1–4 bands of dynamics processing per `group_count` |
| 7 | **Regulator** | `regulator-tuning` (+ `volmax-boost`) | Per-band limiter (2nd MBC instance); default slot for `volmax-boost` is `input-gain` (≈+6 dB, pre-band-limiting). `--volmax-slot output-gain` moves the boost post-band-limiting (opt-out, see below) |
| 8 | **Limiter** | — (+ `volmax-boost` fallback) | Brickwall at -1 dBFS safety net; fallback slot for `volmax-boost` when the regulator isn't emitted |

**`surround-boost` is not mapped to stereo widening** — a 2026-06 DAX
capture showed no stereo-width change on 2-channel content (it's a
multichannel-virtualization control, dormant without a surround/object bed).
Earlier versions added a Calf Stereo Tools widener here; see design-notes
"unvalidated-scaling entry 2".

Output files:
- `~/.local/share/easyeffects/irs/Dolby-{Balanced,Detailed,Warm}.irs` —
  stereo FIR impulse responses.
- `~/.local/share/easyeffects/output/Dolby-{Balanced,Detailed,Warm}.json` —
  EasyEffects presets.

## XML → parameter mapping

**IEQ curve → FIR.** The 20-value IEQ arrays (e.g. `ieq_balanced`) are the
desired **composite** frequency response, not individual filter gains —
applied directly as bell gains they stack to +20–30 dB at the mids. The
script instead builds a minimum-phase FIR (cepstral construction; zero added
latency) from the combined IEQ + audio-optimizer target. Why FIR over a
biquad fit: design-notes "Rejected approaches → Parametric-EQ approximation"
(with the peak/RMS error table). The `.irs` files are RIFF/WAVE (IEEE
float32, stereo, 48 kHz, 4096 samples).

**Volume leveler → Autogain.** Maps to EE's EBU R 128 autogain. On HDA it
ships **bypassed** (`--enable autogain` opts in): without Dolby's MI (Media
Intelligence) content steering it boosts legitimate quiet content and loud
onsets saturate (measured; design-notes "The 2026-07 default-flip
attempt"). On SoundWire it ships active with gentler settings. Settings are
always preserved so users can toggle it in the GUI.
- `volume-leveler-out-target` -320 (1/16 dB = -20 dBFS) → -20 LUFS target;
  the SoundWire path subtracts a further 6 dB of safety headroom (invented,
  design-notes entry 10).
- `volume-leveler-amount` (0–10) → `maximum-history` window. HDA:
  `max(30 − 5·amount, 10)` s; SoundWire: `max(40 − 4·amount, 15)` s (both
  formulas invented, design-notes entry 7).
- `silence-threshold` = -50 dB on both paths — invented but field-confirmed
  (issue #25) and capture-measured: it stops the leveler winding up its gain
  over near-silence, which crackled short notification sounds at EE's -70 dB
  plugin default.
- autogain `reference` = Geometric Mean (MSI) — combines momentary,
  short-term, and integrated loudness for balanced behaviour.

**MB compressor → Multiband Compressor.** Each band is a 6-value tuple:

| Index | Field | Units | → |
|-------|-------|-------|---|
| 0 | Crossover band index | index into the 20-freq table | e.g. 3 → 328 Hz |
| 1 | Threshold | 1/16 dB | -103 → -6.4 dB |
| 2 | Gain coefficient | Q15 | `ratio = 1/(coeff/32768)`; 32767 ≈ bypass |
| 3 | Attack | Q15 block-rate | `tau = -1/(blocks·ln(coeff/32768))` |
| 4 | Release | Q15 block-rate | same decode |
| 5 | Makeup gain | 1/16 dB | 32 → +2 dB |

Only the first `group_count` of the four `band_group_N` slots are decoded
(capped at LSP's 8-band ceiling). Block rate assumes 256 samples @ 48 kHz.

**`volmax-boost` → loudness makeup.** `volmax-boost` (e.g. 96 = +6 dB) is
the ceiling of Dolby's MI-steered VolMax. With no MI leveler to apply it
dynamically, the script applies it statically as the regulator's `input-gain`,
falling back to the brickwall limiter's `input-gain` when the regulator isn't
emitted. Disable with `--disable volmax`.

Neither placement is Dolby-derived — `volmax-boost` is itself a CP-stage
leveler ceiling, so applying it at the VLLDP-stage regulator is a pragmatic
approximation. The default `input-gain` (issue #23) feeds the boost into the
regulator's per-band downward compression, which tames the boosted low end
before the brickwall, eliminating a measured low-end distortion. `--volmax-slot
output-gain` (opt-out) moves the boost after the regulator (the pre-#23
placement) — the full loudness makeup straight into the brickwall; use it for
A/B, or to recover loudness if a device's regulator over-tames the bass. The
default flipped to `input-gain` after a second, aggressive-regulator device
(ThinkPad X13 Gen 6, issue #23) confirmed it stays clean and loud; see
design-notes.

**Regulator → per-band limiter.** A second MBC instance configured as a
limiter (Peak sidechain, 1 ms attack) from `regulator-tuning` `threshold_high`
(1/16 dB per band). The 20 Dolby bands are grouped into ≤8 zones of identical
threshold; tighter low-frequency limiting protects laptop speakers from
sub-bass they can't reproduce. `regulator-distortion-slope` → limiting ratio
(`1/(1-slope)`); `regulator-timbre-preservation` → knee (`-6 × timbre dB`).
`threshold_low` and `stress-amount` are not used. `threshold_high` is read from a
direct `value=`/`preset=` (most devices) or, on the newer SoundWire per-channel
schema, from its `<ch_00>` sub-element (the `ch_00`/`ch_01` form the audio
optimizer also uses); an empty tuning falls back to no limiting with a warning.

## Profile differences

| Profile | IEQ | IEQ curve | Volume leveler | vlldp AO/PEQ | MB compressor |
|---|---|---|---|---|---|
| dynamic | yes | ieq_balanced | on (amount 2) | shared | enabled |
| movie | no | — | off | shared | enabled |
| music | yes | ieq_balanced | on (amount 2) | shared | enabled |
| game | no | — | on (amount 2) | shared | enabled |
| voice | no | — | off | **different** | disabled |

All non-voice profiles share the same audio-optimizer and speaker PEQ; voice
has different AO tuning and simplified PEQ. The MB-compressor threshold varies
slightly per profile. (Wider corpus distribution: cross-device-findings.md.)

## EasyEffects 8.x specifics

- Presets live in `~/.local/share/easyeffects/output/` (not `~/.config/`).
- IR files live in `~/.local/share/easyeffects/irs/` with the `.irs`
  extension (not `.wav`).
- The convolver uses `"kernel-name"` (filename stem), not the deprecated
  `"kernel-path"`.
- The equalizer has no graphic-EQ mode — parametric only (LSP plugin).

EE 7 uses an incompatible preset format; on EE 7 the speaker-correction
filter loads nothing. Use the Flatpak if your distro still ships EE 7.

## Validated vs unvalidated mappings

- **Validated against DAX captures:** the `ieq-amount` `/100` reading
  (design-notes Finding 9, issue #13); the min-phase FIR realises the
  composite target to <0.1 dB RMS (synthetic LTI check).
- **Unvalidated (the "`ieq-amount` class"):** the dialog-enhancer dB ceiling,
  the surround `/20`, the regulator slope/knee mappings, the MBC Q15
  decode, and the autogain window formulas and offsets (design-notes
  entries 7/10; the −50 dB silence gate within them *is* field-confirmed
  and capture-measured — issue #25) all ship by default but are **not yet
  confirmed against a DAX capture**. Each, with the measurement that would
  validate it, is catalogued in design-notes "Unvalidated converter scaling
  factors".

## Not implemented (and why)

- **`filter_coefficients`** — base64 biquad blob in `tuning-vlldp`;
  investigated but it's VLLDP-internal analysis filters, not audio-path EQ.
  The audio-optimizer + PEQ already capture the same correction. (design-notes
  "Rejected approaches".)
- **`regulator-stress-amount` / `threshold_low`** — secondary regulator
  parameters; only `threshold_high` drives the per-band limiter.
- **Always-inert / out-of-scope XML fields** — deliberately ignored because
  they're always zero/disabled on the modelled endpoints, are DSP internals
  with no EasyEffects equivalent, or concern multichannel/subwoofer routing
  irrelevant to stereo laptop output: `pregain`/`postgain`/`calibration-boost`/
  `system-gain` (all 0 dB), `bass-extraction-*`, `virtual-bass-*`,
  `volume-modeler-*`, `graphic-equalizer-*`, `surround-*` /
  virtualizer-geometry, `mi-*-steering-enable`,
  `output-mode`/`mix_matrix`/`processing_mode`, `init-info` sizing, CP-level
  `audio-optimizer-bands` / `regulator-tuning` (always zero — vlldp has the
  real data), `mb-compressor-agc-enable` / `mb-compressor-slow-gain-enable`,
  `woofer-regulator-*`, `band_20_freq` @ 44.1 kHz (script is 48 kHz only),
  and `ieq-bands-set` (the script generates all three IEQ variants).

## Open threads — where to pick up work

- **Close the gap to DAX** and validate the scaling factors above:
  design-notes "Unvalidated converter scaling factors" and "Follow-ups to
  close the gap to DAX".
- **Second-device confirmation** of any default mapping (the bar to change a
  default is ≥1 second-device capture): cross-device-findings.md.
- **Corpus / cross-device follow-ups** (newer-SoundWire regulator gap,
  asymmetric-L/R-peak path, voice-AO re-derivation, 1-band-MBC audibility):
  cross-device-findings.md "Open follow-ups".
- **Measurement tooling** to produce the captures: `tools/measure_dax/`
  (Windows DAX), `tools/measure_ee/` (live EE on Linux), `tools/measure_pw/`
  (PipeWire `filter-chain`).
