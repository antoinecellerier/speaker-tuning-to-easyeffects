<!--
Maintainers — to cut a release:
  1. Move the Unreleased entries into a new "## vYYYY.MM — DATE" heading
     at the top of the version list (use the current year.month; add a
     .1/.2 suffix if there's already a release this month).
  2. Commit, then: git tag vYYYY.MM && git push origin vYYYY.MM
  3. .github/workflows/release.yml publishes the GitHub Release, pulling
     the notes from this file's matching section.
Keep entries reverse-chronological (newest at the top). Tag any entry
that changes the generated preset's sound with **[AUDIBLE]**, and for
those, lead with what a listener actually notices (dull, harsh, louder,
clearer…) grounded in real-device testing where we have it — keep the DSP
mechanism as a secondary clause. If a change alters the output but hasn't
been listened to, say so plainly rather than inventing an impression.

The pre-v2026.05 sections below were reconstructed from git history,
grouped into the releases that would have been cut at the time.
-->

# Changelog

Notable changes to the converter. Entries tagged **[AUDIBLE]** change the
sound of the generated preset — **re-run the script to regenerate your
preset** (and reload it in EasyEffects / restart PipeWire) to pick them up.
Everything else is tooling, packaging, docs, or new-device support that
doesn't alter existing devices' output.

Versions are date-based (`vYYYY.MM`). Watch this repository on GitHub
(**Watch → Custom → Releases**) to be notified when a new version ships.

## Unreleased

### Changed

- **[AUDIBLE]** Stereo image is no longer artificially widened. On
  `dynamic`/`movie` profiles the converter used to add a Calf Stereo Tools
  widener (mapped from Dolby's `surround-boost`), pushing the side channel up
  ~4 dB for a wider-but-sometimes-hollow image. A Windows DAX capture
  (2026-06-13) showed Dolby applies **no** stereo widening on 2-channel
  content — `surround-boost` is a multichannel-virtualization control that's
  dormant without a surround/object bed, not a stereo-width knob — so the
  mapping was removed. Stereo playback now matches Dolby's actual 2-channel
  output. The `--disable stereo` flag is gone (nothing to disable). Re-run the
  script to regenerate your presets. Detail: `docs/design-notes.md`
  unvalidated-scaling entry 2.

### Added

- Measurement tooling for the unvalidated-scaling capture campaign
  (design-notes catalogue entries 1/2/6/8/11; no change to generated
  presets): a speech stimulus (`stimulus_speech`, espeak-ng synthesis with a
  shaped-noise fallback) for the speech-gated dialog enhancer, an MBC-waking
  `stimulus_stepped_loud` (−2 dBFS peak), a side/mid widening readout for
  the stereo stimuli (which the analyzer previously skipped as unknown
  kinds), and an absolute-level mode (`compare_ee_vs_dax.py --absolute`,
  with un-normalized transfer curves now stored by `analyze.py`) for
  broadband-level questions like the PEQ anti-clipping trim.

- Simplified-schema DAX3 XMLs are now supported. Some Lenovo drivers
  (xml_version ~3.2.x — e.g. the ThinkPad X1 Carbon Gen 8) name the per-channel
  audio-optimizer correction `<gain_l>`/`<gain_r>` instead of `<ch_00>`/`<ch_01>`
  and ship no multi-band-compressor or speaker-PEQ blocks; the script previously
  rejected them as an unsupported schema variant. It now maps `gain_l`→left /
  `gain_r`→right (the same 20-band, 1/16-dB encoding, resolved the same way) and
  emits a convolver + regulator preset, warning that MBC and PEQ are absent in
  this variant. The regulator and all `tuning-cp` blocks (dialog, surround,
  leveler, volmax) are unchanged.
  ([#22](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/22))
- `--autoload-sink NODE_NAME` (repeatable) — bind autoload to an explicit
  PipeWire sink, bypassing speaker detection. Mirrors `ee_to_pipewire.py`'s
  `--target-sink`.

### Fixed

- Autoload now works on cards whose output *route* description differs from
  their *profile* description. EasyEffects keys entries on the route (e.g.
  `Speaker`), but the script wrote the profile, so on any such card (observed
  on a classic `analog-stereo` card reporting `Analog Stereo`) the entry never
  matched and the `Nothing` fallback won, leaving the Dolby correction silently
  unapplied. Cards where the two coincide were unaffected. The filename now
  uses the route read from `pw-dump`; sinks with an unresolvable route are
  skipped with an explanation.
  ([#18](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/18))
- Autoload now finds internal speakers that PipeWire doesn't tag
  `audio-speakers` (some laptops fall back to a generic UCM2 profile that omits
  the icon, so the built-in speaker shows up as `audio-card-analog` and the old
  strict filter silently matched nothing). Detection falls back to a relaxed
  tier of internal analog sinks, prompting when several are found and listing
  every sink it saw. `ee_to_pipewire.py`'s smart-filter target gets the same
  fallback.
  ([#18](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/18))

## v2026.05 — 2026-05-28

### Changed

- **[AUDIBLE]** Brought back the treble. The speaker voicing was being
  applied about 10× too strongly, rolling the highs off far harder than
  Dolby intends — up to ~28 dB too quiet at the very top of the band — so
  presets sounded dull and dark, missing high-frequency detail and "air."
  Reading `ieq-amount` as a percentage (its true meaning) restores the
  bright, detailed top end, now within ~1.5 dB of the Dolby reference.
  Validated by on-device measurement on a ThinkPad X1 Yoga Gen 7 and
  corroborated on a second laptop. Re-run the script to regenerate your
  preset.
  ([#13](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/13))

### Added

- `CHANGELOG.md` and a GitHub Releases workflow so notable (especially
  audible) changes are easy to follow; **Watch → Releases** to subscribe.
- `--version` flag on both scripts, and a provenance stamp on every
  generated file (a `_generator` field in the preset JSON, a `# version:`
  line in the PipeWire conf) recording the `git describe` version that
  produced it.
- `ee_to_pipewire.py` companion converter: turns a generated EasyEffects
  preset into a PipeWire `filter-chain` `.conf` (LSP + Calf backed) for
  users who'd rather not run EasyEffects. Attaches as a WirePlumber 0.5+
  smart filter, self-contained conf layout, with a `tools/measure_pw/`
  equivalence harness.
- Recognise Lenovo IdeaPad text-vendor `SUBSYS` values (e.g. `IDEA4002`)
  during device discovery.
- Mark the Lenovo Yoga Pro 9 14IRP8 (83BU, Realtek ALC287 17AA:38BE) as a
  supported device. ([#17](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/17))

### Fixed

- Fix HDA codec unpacking in the autodetection error path.

### Docs

- VBE-on-HDA investigation: corpus settles the schema, and cascading LSP
  filters + a Saturator can escape the Virtual Bass Enhancement ceiling
  (wet-only / harmonic-structure). Confirmed the construction/gain tweaks
  remain negligible after the IEQ fix.
  ([#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14),
  [#13](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/13))

## v2026.04.3 — 2026-04-29

### Changed

- **[AUDIBLE]** Corrected the steepness of high-pass / low-pass filters,
  which were being built twice as steep as the Dolby tuning specifies.
  Affects only devices whose tuning includes HP/LP filters; this is a
  numerical correctness fix — the change in roll-off was not separately
  listening-tested, so regenerate and trust your ears.
- Round multiband-compressor and regulator band parameters to 4 decimals
  (negligible numerically).

### Added

- `tools/measure_dax/` (capture DAX3's response on Windows) and
  `tools/measure_ee/` (capture the live EasyEffects response on Linux),
  plus an EE-on-Linux vs DAX-on-Windows comparison harness.
- pytest suite covering the converter with no proprietary inputs, run in
  GitHub Actions CI.
- Warn when the XML carries unmodeled DSP blocks (DSO, advanced
  virtualizer) or watching-only fields.
- Record the ThinkPad T14s Gen 6 AMD subsystem ID in the tested-devices
  table.

### Docs

- Empirical DAX3 comparison findings and hypothesis testing (rejected the
  "DAX inverts the audio-optimizer" hypothesis; the residual signature is
  structural across profiles); documented the deterministic-from-XML
  constraint in `CLAUDE.md`.
  ([#11](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/11),
  [#12](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/12))

## v2026.04.2 — 2026-04-22

### Changed

- **[AUDIBLE]** Louder, closer to Windows. Applies Dolby's loudness
  makeup (`volmax-boost`, typically +6 dB) so the preset no longer sounds
  quieter than the same laptop under Dolby on Windows. If it ends up too
  loud, or the limiter pumps on already-loud tracks, rebuild with
  `--disable volmax`.
- **[AUDIBLE]** Devices whose Dolby tuning uses 1, 3, or 4 dynamics bands
  now get the right number of compression bands instead of being forced to
  2. Most laptops ship 2-band tunings and are unaffected; this changes the
  output only on devices with a different band count (not separately
  listening-tested).
- **[AUDIBLE]** Support high-shelf (type 3) and low-pass (type 6/8) PEQ
  filters for the few devices whose tuning includes them (some Lenovo
  AIO / ALC274 SKUs) — high-shelf affecting high-frequency
  brightness/sibilance, low-pass how rolled-off the highs are.
  **Experimental:** reproduced numerically from the Dolby tuning but not
  yet audibly validated — feedback welcome.

### Added

- Coloured script and `--help` output via optional `rich` /
  `rich-argparse`, richer per-profile headers, and per-profile `--disable`
  hints.
- `--dry-run` to skip writing presets, IRs, and autoload configs.
- Autoprobe a Dolby source when both `--windows` and the XML path are
  omitted; detect extracted drivers by XML content; accept a `C:` drive
  mount in `--windows`.
- Configure EasyEffects' global fallback preset alongside `--autoload`;
  detect Flatpak EasyEffects even before its first launch.

### Fixed

- Reject simplified-schema XMLs with a clear error instead of crashing,
  filter out Fusion mic-AEC XMLs, and improve endpoint error messages.
- Sanitise the XML profile type before using it in output paths.

### Docs

- Expand the cross-device findings to a 1050-XML cohort.

## v2026.04.1 — 2026-04-17

### Added

- **[AUDIBLE]** Better presets for SoundWire laptop speakers (small
  full-range drivers on newer Intel platforms), plus auto-detection of
  those codecs. Aims to restore the brightness these speakers lose after
  the FIR is normalised, add a psychoacoustic bass enhancer so small
  drivers produce more *perceived* bass, and lift presence/clarity for
  dialog. Designed from the speaker characteristics rather than
  listening-validated on a specific device — feedback from SoundWire
  laptop owners welcome.
- `--speaker-info` to report detected audio hardware and speaker layout.
- Auto-detect Flatpak EasyEffects and write presets to the correct paths.

### Docs

- Tested-devices table, `innoextract` extraction recipe, consolidated
  research under `docs/`, `CLAUDE.md`, and a README restructure that
  front-loads user-facing docs. PR #7 review feedback (ThinkPad X1 Carbon
  Gen 13). ([PR #7](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/pull/7))

## v2026.02 — 2026-02-28

Initial release.

### Added

- Convert a Dolby DAX3 tuning XML into EasyEffects 8.x output presets:
  minimum-phase FIR convolver (IEQ target + audio-optimizer speaker
  correction), speaker PEQ (bell, shelf, and high-pass filters), dialog
  enhancer, stereo widening, autogain
  (volume leveler), multiband compressor, per-band regulator, and a
  brickwall safety limiter.
- Endpoint/profile selection, `--all-profiles`, `--autoload`, `--windows`
  auto-discovery, and positional XML path. MIT licensed; DAX3 conversion
  findings documented.

### Notable behaviour baked into this first release

- Autogain is **bypassed by default** — without Dolby's Media Intelligence
  steering it distorts on quiet→loud transitions.
- The convolver's internal autogain is disabled (it would otherwise add a
  ~+50 dB boost), the dynamics processors are downward-only, and limiter
  enum parameters are emitted as string labels (not integer indices).
