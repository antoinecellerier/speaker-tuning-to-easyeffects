# measure_ee — capture the live EasyEffects pipeline response

Linux-side counterpart to [`tools/measure_dax/`](../measure_dax/). Plays
the same stimulus battery into a running EasyEffects instance and
captures the post-processing output to a wav, so you can stack the
captured response next to the DAX3 capture and identify mismatches.

## Why it's tricky (and how this avoids the dead ends)

EasyEffects 8.x routes its plugin chain over internal `pw-filter`-style
pipes that aren't standard linkable PipeWire ports. The ports we *can*
see (`easyeffects_sink:monitor`, `ee_soe_output_level:output_*`,
`easyeffects_source`) are either pre-processing input, a level meter
copy, or the mic chain — not the post-processing mix that hits the hw
sink. Earlier attempts to capture from those ports failed even on the
"Nothing" bypass preset.

Two extra traps caught us during the retry:

1. **`easyeffectsrc` location.** EE 8.x reads from
   `~/.config/easyeffects/db/easyeffectsrc` (per
   `easyeffects_db_streamoutputs.kcfg`'s `<kcfgfile name="easyeffects/db/easyeffectsrc"/>`),
   *not* the legacy `~/.config/easyeffects/easyeffectsrc`. Editing the
   wrong one is silently ignored.

2. **WirePlumber overrides `pw-record --target`.** It treats the flag as
   a hint and routes your "capture from monitor" stream to the system
   default capture source (the mic) regardless. The fix is
   `pw-record --target 0` (no auto-link) plus a manual `pw-link
   <source-monitor> <recorder-input>`. `smoke.py` does this for you.

## Files

| file | role |
|---|---|
| `setup_null_sink.sh` | Loads `module-null-sink ee_capture`, edits db rc to `outputDevice=ee_capture` + `useDefaultOutputDevice=false`, restarts EE |
| `teardown.sh` | Reverses the above; restores db rc from backup and unloads the null sink |
| `smoke.py` | Bypass-preset smoke harness — gates every route attempt before running the full battery |
| `capture_battery.py` | Runs the 5-stimulus battery through EE, writes `loopback_*.{wav,json}` analyzer-compatible |
| `compare_ee_vs_dax.py` | Overlays EE captures vs DAX3 captures (analyze.py outputs from both sides) — frequency domain |
| `compare_ir_time_domain.py` | Overlays converter FIR / EE-captured / DAX-captured IRs in the time domain — envelope decay, cumulative-energy times, peak position. Answers "is my FIR's tail real signal or just below the noise floor?" |
| `compare_ee_analytical.py` | Optional self-check: capture vs analytical (FIR + biquads) model. Useful for converter validation, *not* required for EE↔DAX comparison. |

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
  measurement. The null-sink route silences your speakers for the
  duration so you'll notice if a media app is making noise.
- **EE restart is one-time per setup.** GNOME's mic indicator pops
  briefly (EE re-attaches its input pipeline). Subsequent preset
  switches use `easyeffects -l <preset>` and don't re-pop.
- **Sample rate is locked at 48 kHz.** The null sink and all stimuli
  are 48 kHz f32; if your default rate differs, set it in your audio
  panel or pass `--rate` to the helper scripts.
