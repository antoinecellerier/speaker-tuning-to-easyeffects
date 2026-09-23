# measure_ee — capture the live EasyEffects pipeline response

This is the Linux-side counterpart to [`tools/measure_dax/`](../measure_dax/).
It plays the same stimulus battery into a running EasyEffects instance and
captures the post-processing output to a wav. You can then stack the captured
response next to the DAX3 capture and identify mismatches.

## Why it's tricky (and how this avoids the dead ends)

EasyEffects 8.x routes its plugin chain over internal `pw-filter`-style pipes
that aren't standard linkable PipeWire ports. The ports we *can* see are
`easyeffects_sink:monitor`, `ee_soe_output_level:output_*` and
`easyeffects_source`. They carry pre-processing input, a level meter copy, or
the mic chain, not the post-processing mix that hits the hw sink. Capturing
from those ports failed even on the "Nothing" bypass preset.

Two more traps:

1. **`easyeffectsrc` location.** EE 8.x reads
   `~/.config/easyeffects/db/easyeffectsrc`, *not* the legacy
   `~/.config/easyeffects/easyeffectsrc`.
   `easyeffects_db_streamoutputs.kcfg` sets this with
   `<kcfgfile name="easyeffects/db/easyeffectsrc"/>`. EE silently ignores an
   edit to the wrong one. A stale legacy top-level file can linger from old
   runs, so don't trust or edit it.

2. **WirePlumber overrides `pw-record --target`.** It treats the flag as a hint
   and routes your "capture from monitor" stream to the system default capture
   source, the mic, regardless. The fix is `pw-record --target 0`, which
   disables auto-link, plus a manual
   `pw-link <source-monitor> <recorder-input>`. `smoke.py` does this for you.

## Verifying EE 8.x config (don't get misled)

When auditing whether EE is applying the current preset:

- **Autoload entries** live in the db or in memory, NOT in
  `~/.config/easyeffects/autoload/{output,input}/*.json`. That flat-file layout
  is EE 6/7. Grepping it on EE 8.x wrongly concludes "no autoload configured."
  The GUI's Autoloading page is authoritative. An entry shown there may not be
  flushed to `db/easyeffectsrc` until a clean EE quit, because KConfig writes
  lazily. On disk you'll typically see only the fallback keys,
  `outputAutoloadingFallbackPreset` and `outputAutoloadingUsesFallback`.
- **`[StreamOutputs] plugins=…`** in `db/easyeffectsrc` is the *live* applied
  chain. It is the most reliable on-disk check that a regenerated preset took
  effect. For example, confirm there that a removed stage is gone.
- **EE doesn't watch preset files.** After re-running the converter, the
  running service keeps the *old* in-memory chain until you reload with
  `easyeffects -l <PresetName>`. That reload doesn't pop the mic indicator.
  Since 2026-08 the impulse's kernel name carries a content hash, so the reload
  also picks up a regenerated FIR. Before that, EE kept the cached kernel under
  an unchanged name, and only a restart or a new preset prefix replaced it. The
  `_generator` stamp inside each preset JSON tells you which converter version
  wrote it.

## Files

- `setup_null_sink.sh`: loads `module-null-sink ee_capture`, sets
  `outputDevice=ee_capture` + `useDefaultOutputDevice=false` in the db rc, and
  restarts EE.
- `teardown.sh`: reverses the above. It restores the db rc from backup and
  unloads the null sink.
- `smoke.py`: the bypass-preset smoke harness that gates every route attempt
  before the full battery.
- `capture_battery.py`: runs the 5-stimulus battery through EE and writes
  analyzer-compatible `loopback_*.{wav,json}`.
- `sweep_variants.sh`: drives a variant matrix over the same route. It runs one
  build → capture cycle per row of a tab-separated spec: label, build command,
  EE preset. The null-sink setup and a teardown trap wrap the whole run, and the
  smoke gate runs on the first variant only. It is a harness, not an experiment:
  what is being compared lives in the session's spec file, not in the script.
- `summarise_variants.py`: read-only, no capture. It is the sweep's other half.
  It runs `analyze.py` over each variant's captures, then tabulates the
  pink-noise residual per variant × profile × channel, normalized at 1 kHz over
  200 Hz–18 kHz. The residual is always taken against the XML target: is the
  chain faithful to the XML? When `--dax-dir` is given, it is also taken
  against a DAX capture: do we match Windows?
- `autogain_dynamics.py`: the non-LTI protocol for autogain, the volume
  leveler. It runs a silence wind-up / notification-burst check and a
  speech-over-background overshoot sweep, with self-generated preset variants.
  It is the issue-#25 adoption gate: rerun it before changing any autogain
  default.
- `compare_ee_vs_dax.py`: overlays EE captures against DAX3 captures in the
  frequency domain, from analyze.py outputs on both sides.
- `compare_ir_time_domain.py`: overlays the converter FIR and the EE-captured
  and DAX-captured IRs in the time domain: envelope decay, cumulative-energy
  times, peak position. It answers "is my FIR's tail real signal or just below
  the noise floor?"
- `compare_ee_analytical.py`: an optional self-check of a capture against the
  analytical FIR + biquads model. It is useful for converter validation, *not*
  required for EE↔DAX comparison.
- `dynamics_gap.py`: read-only, no capture. It shows why EE's per-band gain
  reduction falls short of DAX's on the same stepped battery. It separates a
  wrong MBC decode from the upstream level gap and from the regulator
  under-engaging. It is the analysis behind the open design-notes entries 6/11.
  Re-run it when a new device's stepped captures land.
- `leveler_curve.py`: read-only, no capture. It measures DAX's volume-leveler
  gain versus input level, as DAX-on minus DAX-off at each rung of the pink
  ladder. The autogain constants of design-notes entries 7/10 approximate this
  measurement. It works on a two-rung archive and says so.
  `tools/measure_dax/CLAUDE_WINDOWS.md` has the capture procedure for the full
  ladder.
- `scaling_report.py`: read-only, no capture. It reproduces the named
  scaling-campaign results from one session's EE + DAX captures: Finding 9,
  entries 2/6/11 and q-mode. Point it at a new session with
  `--ee-dir`/`--dax-dir`/`--dax-archive` to get the same table for another
  device.
- `render_vbe_chain.py`: offline, no capture, no audio. It renders the issue-#14
  virtual-bass chain: LSP brick-wall band-pass → Calf Saturator → brick-wall
  post-band. It runs one `lv2apply` subprocess per stage and writes every
  intermediate plus a provenance sidecar. Its defaults reproduce the 2026-05-06
  PoC bit-for-bit, as recorded in design-notes Finding 8.
- `analyze_vbe_chain.py`: read-only, no capture. It builds harmonic tables for
  bass-burst WAVs on any labeled set of renders/captures: per-tone harmonic
  magnitudes, Δ3/Δ5/Δ7, odd/even ratio, crest factor. The metric definitions
  match the Finding 8 investigation, so numbers stay comparable.
- `score_vbe_chain.py`: read-only, no capture. It scores bass-burst captures
  against a device's own DAX capture with the Finding 8 phase-2 protocol. That
  protocol is the S metric plus the capture-runnable guard subset. The S metric
  uses a floor clamp, ×2 overshoot ≥200 Hz and macro averaging. `--dax-capture`
  is the reference, and `--capture LABEL=PATH` names the chains to judge.
  `--dry-capture` anchors what doing nothing scores.
  `tests/test_score_vbe_chain.py` locks the math.

## Usage

```sh
# 0. one-time: generate stimuli (see tools/measure_dax/README.md)
mkdir -p ~/dax-measure && cd ~/dax-measure
python /path/to/repo/tools/measure_dax/make_stimulus.py

# 1. set up the null-sink route (mutes your speakers temporarily)
bash tools/measure_ee/setup_null_sink.sh

# 2. validate the route (Nothing preset -> bit-identical pass-through)
python3 tools/measure_ee/smoke.py --target ee_capture.monitor --label v3
# expect PASS — gain ~0 dB, flatness <0.5 dB, residual <-35 dB

# 3. run the full battery with the real preset
python3 tools/measure_ee/capture_battery.py \
    --stimulus-dir ~/dax-measure \
    --preset Dolby-Balanced \
    --label ee_dolby_balanced \
    --target ee_capture.monitor \
    --out-dir ~/dax-measure/ee_captures

# 4. analyze (same analyzer DAX captures use)
cd ~/dax-measure/ee_captures
python3 /path/to/repo/tools/measure_dax/analyze.py loopback_*.wav \
    --xml /path/to/DEV_xxxx.xml \
    --profile dynamic --curve balanced

# 5. overlay EE vs DAX captures (DAX captures from Windows side)
python3 tools/measure_ee/compare_ee_vs_dax.py \
    --ee-dir ~/dax-measure/ee_captures \
    --dax-dir ~/dax-measure/captures \
    --out-dir ~/dax-measure/three_way \
    --xml /path/to/DEV_xxxx.xml --profile dynamic --curve balanced

# 6. restore your speakers
bash tools/measure_ee/teardown.sh
```

## Notes

- **Don't play other audio during a capture.** Anything writing to
  `easyeffects_sink` mixes into the EE output and contaminates the
  measurement. The null-sink route silences your speakers for the duration, so
  you'll notice if a media app is making noise.
- **EE restart is one-time per setup.** GNOME's mic indicator pops briefly
  because EE re-attaches its input pipeline. Later preset switches use
  `easyeffects -l <preset>` and don't re-pop.
- **Sample rate is locked at 48 kHz.** The null sink and all stimuli are
  48 kHz f32. If your default rate differs, set it in your audio panel or pass
  `--rate` to the helper scripts.
