<!--
Maintainers — to cut a release:
  1. Move the Unreleased entries into a new "## vYYYY.MM — DATE" heading
     at the top of the version list (use the current year.month; add a
     .1/.2 suffix if there's already a release this month).
  2. Commit, then: git tag vYYYY.MM && git push origin vYYYY.MM
  3. .github/workflows/release.yml publishes the GitHub Release, pulling
     the notes from this file's matching section.
Keep entries reverse-chronological (newest at the top). Within each ### section,
order most-impactful first — [AUDIBLE] and user-facing changes above minor or
internal ones. Section order follows the same idea: lead with the section
carrying the most user-facing change (as v2026.05 leads with Changed).

Each entry has a FIXED SHAPE, in this order, and stops there:
  1. WHAT changed, in user-facing terms. For [AUDIBLE]: the effect a listener
     notices (dull, harsh, louder, clearer). Otherwise: the flag, feature, or
     fixed symptom.
  2. (optional) ONE clause of mechanism — the "how", never the "why".
  3. (optional) the user knob — flag name / how to opt out / "re-run to regenerate".
  4. a link — issue/PR number, plus docs/design-notes.md for the full why.
Hard ceiling: <= 3 sentences (~50 words). If it won't fit, the overflow IS the
"deep why" — move it to docs/design-notes.md and link; do not inline it.

Keep these OUT of the entry (they live in design-notes / reference, behind the
link): measurement numbers (THD, LUFS, dB), specific device IDs / PCI-subsystem
codes, corpus statistics, plugin/library internals (LSP/Calf node names,
lkahead, libebur128…), and DSP derivation. Provenance ("confirmed on a ThinkPad
X13") is already in the linked issue/commit — don't restate it.

[AUDIBLE] honesty: claim a listening impression ONLY if it was actually heard
on-device; if the output changed but wasn't listened to, say so plainly.

Worked example — too long (deep why inline, fails the ceiling):
  - **[AUDIBLE]** Cleaner low end on loud bass. volmax-boost now rides the
    regulator input so per-band compression tames it before the brickwall; on
    the dev device this cut a 234 Hz tone from 11.6% to 0.06% THD with broadband
    loudness unchanged, and a corpus audit of threshold_high (median -18 dB)…
Same change, tight (overflow moved behind the link):
  - **[AUDIBLE]** Cleaner low end on loud, bass-heavy content with volmax. The
    boost now runs through the per-band regulator, which tames it before the
    final limiter instead of distorting. --volmax-slot output-gain restores the
    old placement. ([#23]; measurements and why in docs/design-notes.md)

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

### Added

- New `dolby_to_pipewire.py` turns the tuning XML into an active PipeWire
  filter-chain sink in one command — the EasyEffects preset is staged in a
  throwaway directory (no EasyEffects files installed), converted, and
  PipeWire restarted with the sink verified. `--variant` picks the
  Balanced (default) / Detailed / Warm voicing or `all`; `--no-activate`
  skips the restart; new `--skip-ee-check` / `--skip-next-steps` flags on
  the two underlying scripts support the flow (docs/ee-to-pipewire.md).
- **[AUDIBLE]** (opt-in) New experimental `--enable coupled-bands` flag
  engages the speaker-protection limiter on bands the tuning marks
  non-isolated but leaves at a 0 dB threshold, so loud content that
  crosses full scale there is tamed before the final limiter. Not yet
  heard on device; default presets are unchanged
  ([#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44),
  [#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27);
  hypothesis and measurements in `docs/design-notes.md`).
- New `--enable autogain` flag activates the volume leveler for
  Windows-level loudness on HDA devices, where it ships bypassed — an
  on-device attempt to flip the default found its gain ride audibly
  saturates quiet-background content, so it stays opt-in. Enabling the
  leveler (by flag or in the EasyEffects GUI) now also gets a raised
  silence gate that fixes the crackle on short sounds arriving after
  silence; re-run the script to regenerate
  ([#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25);
  measurements in `docs/design-notes.md`).
- All three scripts now tab-complete in bash and zsh — flag names, the
  `--disable` / `--enable` / `--variant` value lists, paths, and your live
  PipeWire sink names — via the optional `argcomplete` package. Add one
  `eval` line to your shell rc to enable it (README "Shell tab-completion");
  without the package the scripts are unchanged.
- Mark additional tested devices: ThinkPad T14 Gen 7
  ([#42](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/42)),
  ThinkPad T14 Gen 1 AMD
  ([#45](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/45)),
  ThinkPad T14 Gen 7 AMD
  ([#48](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/48)).
- A run now says when the tuning names a different profile than the one
  built: a few XMLs declare the profile the device ships on under Windows,
  and the script always builds the endpoint's first. Rebuild with
  `--profile <name>` to match Windows; presets are otherwise unchanged
  ([#46](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/46)).
- A run now warns when the tuning's largest correction boost lands on a
  band the speaker-protection limiter leaves unlimited, so that boost and
  the volmax gain reach the final limiter unprotected. Suggests
  `--disable volmax` / `--enable coupled-bands` if bass or loud content
  distorts; presets are unchanged
  ([#46](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/46)).

### Fixed

- **[AUDIBLE]** The speaker-correction curve is no longer applied on profiles
  whose tuning switches the audio optimizer off. `audio-optimizer-enable` was
  never read, so a profile that ships a curve but declares the optimizer
  disabled still got it; re-run to regenerate. Affects the `off` profile and a
  few devices' `music` profile — up to 6 dB in-band there, unchanged everywhere
  else, and not yet heard on affected hardware
  ([docs/cross-device-findings.md](docs/cross-device-findings.md) §14).



- Running the script from a session with no display (ssh, tmux) no longer
  claims EasyEffects isn't installed. `easyeffects --version` needs a
  display to answer, and any failure to answer was read as "not found";
  the script now says the version couldn't be checked and why
  ([#46](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/46)).
- The PipeWire converter now writes the multiband compressor's
  compression-mode and boost settings explicitly instead of inheriting
  plugin defaults (same output today, but no longer tied to
  installed-plugin defaults staying put), and warns on preset content it
  can't translate instead of dropping it silently. Re-run
  `ee_to_pipewire.py` to regenerate (`docs/ee-to-pipewire.md`).
- The smart-amp firmware-gate fix command printed at the end of a run now
  works on current kernels: it spells out `iface=CARD`, which modern tas2781
  drivers require (the old form failed with "Cannot find the given element").
  The firmware self-check also finds compressed `.bin.zst` blobs now, and the
  hint says what it means if toggling the gate changes nothing
  ([#39](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/39)).

### Docs

- A second device's Dolby-on/off measurements confirm the converter's
  speaker-correction curve on the simplified tuning-file variant — the
  measured Windows response matches the generated filter, so remaining
  Windows-vs-Linux differences there come from Dolby's adaptive loudness
  processing (the opt-in `--enable autogain` covers the largest part)
  ([#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44);
  measurements in `docs/design-notes.md`).
- Documented two independent crackle sources on the PipeWire filter-chain
  path: session quanta smaller than the validated 1024 are an untested regime
  (with a `pw-metadata` isolation step), and the ROG Xbox Ally X has
  kernel-side playback-dropout history whose amp-calibration handling differs
  by kernel lineage (vanilla 6.18-stable skips the unit's calibration; SteamOS
  and mainline apply it with TI's fix) — rule the kernel out first
  ([#39](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/39);
  details in `docs/ee-to-pipewire.md`).

## v2026.07 — 2026-07-21

### Fixed

- **[AUDIBLE]** Auto-detection no longer picks another codec's tuning when two
  tunings share one codec-subsystem id (Lenovo reuses them across Realtek
  codecs), which made affected devices sound clearly wrong; the codec device id
  now disambiguates. Re-run the script to regenerate if yours matched wrongly
  ([#33](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/33)).
- `--speaker-info` (and XML matching on PCI-keyed filenames) now reports the
  analog audio controller's PCI subsystem instead of the GPU HDMI function's,
  which hid the machine SKU id on dual-controller AMD laptops
  ([#33](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/33)).
- `--speaker-info` no longer doubles the SoundWire amplifier count in the
  speaker-layout estimate; each enumerated amp is counted once, with its
  channel count probed from the hardware
  ([#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27)).
- Auto-detection now finds the Dolby tuning on Cirrus cs35l56 SoundWire laptops
  (e.g. Samsung Galaxy Book6), which the old part-id match missed. No change for
  already-matched devices; re-run with `--autoload`
  ([#26](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/26);
  why in `docs/cross-device-findings.md`).

### Changed

- **[AUDIBLE]** SoundWire presets are quieter and less mid-forward: two legacy
  hardcoded boosts are removed, as both compensated an IEQ over-application
  bug fixed in v2026.05. Re-run the script to regenerate; not yet heard
  on-device (SoundWire hardware pending,
  [#29](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/29);
  full why in docs/design-notes.md, unvalidated-scaling entries 1/3).

### Added

- The generation report now warns when the tuning's regulator never engages
  yet carries the volmax loudness boost, since the boost then hits the final
  limiter untamed and can squash loud content; the warning points at
  `--disable volmax`
  ([#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27)).
- Old-kernel hint: when the running kernel series is more than 18 months old,
  the end of a run, `--doctor`, and `--speaker-info` now say so and suggest a
  newer kernel (distro backports / HWE) — speaker-amp fixes land kernel-side,
  and one report's bad sound was fixed entirely by a kernel upgrade
  ([#33](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/33)).
- `--speaker-info` now reports an amplifier-status section — per-amp driver
  bind, a probed channel count, and firmware / kernel-log evidence for both
  HDA and SoundWire smart amps — to help diagnose silent or degraded speakers.
  Known firmware-missing log signatures are flagged; otherwise it points you
  at the log to read yourself
  ([#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27)).
- `--best-guess`: when auto-detection finds no exact hardware match, fall back
  to the only internal-speaker tuning whose manufacturer is present (or list the
  candidates to pass one as the XML path), so a laptop on an unmapped filename
  convention can still generate a preset instead of erroring
  ([#26](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/26)).
- Mark additional tested devices: ThinkPad E14 Gen 2 AMD
  ([#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25)),
  Lenovo Yoga Pro 7 14APH8
  ([#30](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/30)),
  ASUS TUF Gaming A15
  ([#34](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/34)),
  Lenovo IdeaPad Pro 5 14IMH9
  ([#36](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/36)),
  Lenovo IdeaPad Pro 5 14APH8
  ([#33](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/33)).

### Docs

- Field outcome: on the Samsung Galaxy Book6 Ultra, installing the machine's
  Cirrus speaker firmware (extracted from the Windows driver by the reporter)
  fixed the speakers outright, and the generated preset then added no audible
  benefit — that device's voicing lives in the amplifier firmware, not in the
  Dolby host tuning. README now routes the thin-and-quiet-speakers symptom to
  `--speaker-info` and the reporter's extraction write-up
  ([#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27);
  full analysis in `docs/cross-device-findings.md`).
- Research: the loud-bass dynamics gap vs Dolby (generated presets compressing
  loud bass less than DAX) is re-attributed to gain staging, not regulator
  timing. The v2026.06 `--volmax-slot input-gain` default narrows but does not
  close the gap on re-measurement, leaving the band-limiter realization as the
  open lever
  ([#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23);
  evidence in `docs/design-notes.md`).

## v2026.06 — 2026-06-22

### Changed

- **[AUDIBLE]** Cleaner low end on loud, bass-heavy content with `volmax`. The
  `volmax-boost` loudness gain now runs *through* the per-band regulator by
  default, so it tames the boosted bass before the final limiter instead of
  distorting it. `--volmax-slot output-gain` restores the old placement. Re-run
  to regenerate
  ([#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23);
  measurements and why in `docs/design-notes.md`).
- **[AUDIBLE]** Stereo image is no longer artificially widened. On
  `dynamic`/`movie` profiles the converter used to push the sides wider (a
  widener mapped from Dolby's `surround-boost`), sometimes hollowing the image;
  a Windows DAX capture showed Dolby does no widening on 2-channel content, so
  the mapping and the defunct `--disable stereo` flag were removed. Re-run to
  regenerate (detail in `docs/design-notes.md`).
- A failed hardware auto-detection now prints a plain `Error: …` pointing at
  `--help`, instead of dumping argparse's full usage banner — which made an
  environment problem (no tuning found) look like a mistyped command. Genuine
  flag mistakes still show usage.

### Added

- **Volume leveler now reproduced in the PipeWire `filter-chain`.** A
  non-bypassed SoundWire `autogain`, previously dropped with a "no LV2
  equivalent" warning, is now emitted as an LSP loudness AGC, so
  `ee_to_pipewire.py` covers every stage of the EE chain. Zero added latency,
  validated EE-vs-PW on-device (details in `docs/design-notes.md`).
- `--doctor` (alias `--diagnose`) — self-diagnostic for "it loads but sounds
  like nothing" reports: checks the EasyEffects version, install, impulse-file
  integrity, the selected preset, and background-service setup, then prints a
  pasteable PASS/WARN/FAIL report. Normal runs also warn when the detected EE
  install can't use the presets (e.g. EE 7 bypasses the v8 convolver).
  ([#22](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/22))
- Simplified-schema DAX3 XMLs are now supported (some Lenovo drivers, e.g. the
  ThinkPad X1 Carbon Gen 8, were previously rejected). The converter maps their
  single speaker correction to the left/right channels and emits a convolver +
  regulator preset, warning that MBC and PEQ are absent in this variant.
  ([#22](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/22))
- Warns when a smart-amp firmware gate is muting the woofers. Some laptops
  (e.g. the Lenovo Yoga Pro 9i) keep the woofers muted until the ALSA control
  `Speaker Force Firmware Load` is enabled, so `--speaker-info` and the
  end-of-run summary now detect the gate and print copy-paste fixes.
  ([#17](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/17))
- Auto-discovery now matches Apple Boot Camp tuning XMLs (Intel-Mac DAX3
  tunings key on a device-first PCI subsystem, the opposite byte order from an
  HDA codec). Tentative — unverified on real T2-Mac Linux hardware.
  ([#21](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/21))
- `--autoload-sink NODE_NAME` (repeatable) — bind autoload to an explicit
  PipeWire sink, bypassing speaker detection. Mirrors `ee_to_pipewire.py`'s
  `--target-sink`.
- Mark additional tested devices: ASUS Zenbook 14 UX3405CA
  ([#19](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/19)),
  Lenovo IdeaPad Pro 5 14AHP9
  ([#18](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/18)),
  and ThinkPad X13 Gen 6 Intel
  ([#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23)).
- Measurement tooling for the unvalidated-scaling capture campaign (no change to
  generated presets): new loud/speech stimuli, a stereo widening readout, and an
  absolute-level comparison mode (`compare_ee_vs_dax.py --absolute`).
- A "Which should I use?" guide in the README weighing EasyEffects against the
  PipeWire `filter-chain` (richer live control vs lower CPU/RAM, both
  zero-added-latency), plus `tools/measure_perf/` to reproduce the figures on
  your own hardware. Numbers + method: `tools/measure_perf/README.md`.
- Colored terminal output for `ee_to_pipewire.py`, matching the main script
  (`--no-color` to disable); `--dry-run` now also reports where the conf and
  IRS would be written.

### Fixed

- Autoload now works on cards whose output *route* differs from their *profile*.
  EasyEffects keys entries on the route but the script wrote the profile, so on
  such cards the Dolby correction was silently never applied; the filename now
  uses the route, and sinks with an unresolvable route are skipped with an
  explanation.
  ([#18](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/18))
- Autoload now finds internal speakers PipeWire doesn't tag `audio-speakers`
  (some laptops use a generic UCM2 profile, so the old strict filter matched
  nothing). It falls back to a relaxed tier of internal analog sinks, prompting
  when several match; `ee_to_pipewire.py`'s smart-filter target gets the same
  fallback.
  ([#18](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/18))
- A newer SoundWire device (Lenovo IdeaPad 5x 2-in-1) now gets its per-band
  regulator limiting; its thresholds lived in a sub-schema the parser didn't
  read, so the small speakers silently ran without their excursion guard. No
  other device's output changes; XML-derived but not yet verified on the
  hardware — if you have this device, re-run the script. (Detail in
  `docs/cross-device-findings.md`.)
- Hardened the SoundWire convolver's pre-chain gain: now capped at +12 dB with a
  warning if a correction curve would exceed it. No real device reaches the cap
  (presets are unchanged) — the failure mode is now a loud warning instead of
  silent clipping.

### Docs

- README restructured into a user-first guide (quick-start-led, collapsible
  reference sections, a pipeline diagram, EasyEffects screenshots). The deep
  DSP/XML internals moved to a new [`docs/reference.md`](docs/reference.md), and
  `docs/design-notes.md` is now explicitly the research log. No change to
  generated output.

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
