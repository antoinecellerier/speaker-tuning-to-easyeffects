# measure_pw — capture & validate the PipeWire filter-chain output of `ee_to_pipewire.py`

Companion to [`tools/measure_ee/`](../measure_ee/) (which captures the
live EasyEffects pipeline). This directory hosts the tooling that
proves the PipeWire `filter-chain` `.conf` produced by
[`ee_to_pipewire.py`](../../ee_to_pipewire.py) is equivalent — both in
frequency and time domain — to the EasyEffects chain that
[`dolby_to_easyeffects.py`](../../dolby_to_easyeffects.py) emits from
the same Dolby DAX3 XML.

The PW chain reuses the same `ee_capture` null sink + `pw-record`
pipeline as `measure_ee`, so captures from both sides are
schema-identical and feed the same analysis tools.

## Why a deterministic schema check first

Comparative audio testing has a long warm-up (set up null sink,
restart EE, run capture battery, deconvolve sweeps, plot diffs) and
the failure modes are noisy — a misrouted PW link or a sub-sample
group-delay change can mask the real bug. So the conf gets validated
*before* anyone spends five minutes on a battery: `ee_to_pipewire.py`
runs this check on every conf it writes and refuses to write one that
fails — keep that default on, don't pass `--no-validate`.
Run the checker by hand only against a conf already on disk: one
edited by hand, or generated earlier with `--no-validate`.

```sh
python3 tools/measure_pw/validate_conf.py <some.conf>
```

> **`validate_conf.py` is not a measurement tool.** It is the command-line
> front end to the schema check `ee_to_pipewire.py` runs on every conf it
> writes. It needs no audio, no capture and no PipeWire daemon, and it sits in
> a `measure_*` directory for historical reasons. The runtime core is
> [`lib/pipewire/validate.py`](../../lib/pipewire/validate.py) and what is left
> here is a thin CLI that imports it — **the converter does not shell out to
> this path**, it calls `validate.run(conf)` in process and renders the
> resulting `Report` itself. The old layering violation, a top-level
> user-runnable script depending on something outside `lib/`, is gone.
>
> One site still runs this script: `tests/corpus/test_ee_to_pipewire_corpus.py`,
> once, on a single rendered conf — with no `is_file()` guard on it, so a
> wrapper that moved away fails loudly instead of turning into "don't check".
> `tests/test_layout.py` guards the rest: that this file's `sys.path` bootstrap
> still resolves `lib` so the CLI keeps working standalone, and that a
> dependency it cannot run exits 2 (setup error) rather than 1 (bad conf).
> `tests/test_validate_conf.py` imports the library directly. Wider context:
> [`tools/README.md`](../README.md).

It shells out to `lv2info` for every LV2 URI in the conf, parses
each port's `Symbol`/`Min`/`Max`/`Default`/`Properties`, and
validates the conf's `control = { ... }` block against it. Catches:

  - Unknown port symbols (typos / schema drift in the converter).
  - Out-of-range values.
  - The xm/MUTE inversion trap: any non-Off filter type paired with
    `xm=1` is flagged because the band would be silently muted.

Audio testing is still the final gate — schema correctness is
necessary but not sufficient. Some bugs (e.g. `inputs`/`outputs`
arrays missing from `filter.graph`, comb-filter from auto-route
leaks, runtime-only LV2 plugin behaviour, asymmetric stereo
processing that mono-symmetric stimuli cannot expose) only show up
at runtime.

## Files

| file | role |
|---|---|
| [`setup_chain.sh`](setup_chain.sh) | Generates the conf, drops it in `~/.config/pipewire/filter-chain.conf.d/`, starts a child `pipewire -c filter-chain.conf` process, links the chain output to `ee_capture`, sets the chain as default sink, and stops EasyEffects (so its `easyeffects_sink` doesn't compete with the chain for WirePlumber's auto-link policy — see "WirePlumber traps" below). |
| [`teardown_chain.sh`](teardown_chain.sh) | Reverses the above: kills the child `pipewire`, removes the conf drop-in, restores the default sink, and restarts EE in service mode if setup stopped it. |
| [`capture_battery.py`](capture_battery.py) | Plays the 5-stimulus battery into the chain and captures from `ee_capture.monitor` to `loopback_<stim>_<label>.{wav,json}`, schema-identical to `tools/measure_ee/capture_battery.py`. Re-uses `tools/measure_ee/smoke.py`'s `play_and_capture` primitive for timing parity. |
| [`compare_ee_vs_pw.py`](compare_ee_vs_pw.py) | Frequency-domain magnitude diff: Farina deconvolution for sweeps, Welch-averaged spectrum for steady-state stimuli. Multitone-aware (only the actual tone bins are compared; inter-tone bins are noise vs noise). PASS when |dB diff| ≤ tolerance (default 0.5 dB) across 50 Hz–18 kHz on every stimulus. |
| [`compare_ee_vs_pw_time_domain.py`](compare_ee_vs_pw_time_domain.py) | Sample-aligned subtraction: integer-sample lag from cross-correlation, fractional refinement via FFT phase rotation, residual = ee − pw_aligned. Reports signal-to-residual ratio (S/R) in dB. PASS when S/R ≥ 30 dB on every stimulus. |
| [`validate_conf.py`](validate_conf.py) | The CLI front end for the deterministic schema check described above — argument parsing and exit codes over `lib/pipewire/validate.py`, which holds the parsing and validation itself. No PipeWire daemon needed; sub-second. Not a measurement tool — it is the CLI over the check `ee_to_pipewire.py` runs in process on every run; see the note above before moving it. |
| [`autogain_proof.py`](autogain_proof.py) | Leveler-only EE-vs-PW comparison: strips every other stage from `plugins_order` so no convolver/MBC/limiter confounds the reading, plays a loud→quiet→loud→silence pink stimulus through both sides, and compares short-term-LUFS trajectories (target, ride depth, 90% rise/fall time). This is what established that the PW `autogain_stereo` translation tracks EE's leveler. |
| [`volume_stage_probe.py`](volume_stage_probe.py) | Answers where a filter-chain sink's own volume control sits relative to the graph — before the dynamics, or outside them (issue [#63](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/63)). Captures the *speaker sink's monitor*, which taps the mix after the chain and before the hardware mixer, over four legs: chain at unity, chain turned down, speaker turned down (control: proves the hardware volume cannot reach the tap), and the stimulus pre-scaled inside the file (control: proves the stimulus actually exercises the dynamics). **Read the second control before believing the result** — a chain whose compressor never engages produces a pure gain either way, and reports as "volume is applied after the DSP" if you skip it. `--analyze-only` re-reads existing captures without playing anything. |
| [`autogain_fullchain.py`](autogain_fullchain.py) | The same comparison over the *full* HDA chain with autogain forced active — a long quiet segment lets the leveler wind gain up, then a hard loud onset arrives while it is still high. Answers whether PW reproduces or worsens EE's overshoot into the brickwall. Reuses the signal helpers from `autogain_proof.py`. |

Both autogain harnesses take `--out-dir` (default: the untracked
`localresearch/measure_pw/` tree) and follow the same
`build` → `capture --side {ee,pw}` → `analyze` sequence as the battery above.
They reroute sinks and play audio, so run them through the audio handoff, not
ad hoc.

## Workflow

```sh
# 0. one-time: stimuli (shared with tools/measure_ee/)
mkdir -p localresearch/measure_ee/stimuli && cd localresearch/measure_ee/stimuli
python3 ../../../tools/measure_dax/make_stimulus.py
cd -

# 1. the cheap deterministic check needs no step of its own: step 3
#    generates the conf with ee_to_pipewire.py, which validates its own
#    output and refuses to write a conf that fails. To re-check a conf
#    already on disk (hand-edited, or written with --no-validate):
#      python3 tools/measure_pw/validate_conf.py <some.conf>

# 2. set up the EE-side route (loads ee_capture null sink, redirects
#    EE output to it, restarts EE — this mutes your speakers temporarily)
bash tools/measure_ee/setup_null_sink.sh

# 3. set up the PW chain (stops EE, loads the chain, wires it into
#    ee_capture, sets the chain as default sink)
bash tools/measure_pw/setup_chain.sh \
    ~/.local/share/easyeffects/output/Dolby-Balanced.json \
    Dolby_PW_Test

# 4. capture EE side first (need EE running — do this BEFORE step 3
#    if you also want EE captures; or restart EE between batteries)
#    For a fresh comparison run:
#      bash tools/measure_pw/teardown_chain.sh   # restarts EE
python3 tools/measure_ee/capture_battery.py \
    --stimulus-dir localresearch/measure_ee/stimuli \
    --preset Dolby-Balanced \
    --label ee_dolby_balanced \
    --target ee_capture.monitor \
    --out-dir localresearch/measure_ee/captures_ee

# 5. capture PW side (re-do step 3 to put chain back, then:)
python3 tools/measure_pw/capture_battery.py \
    --node-name Dolby_PW_Test \
    --label pw_dolby_balanced

# 6. frequency-domain comparison
python3 tools/measure_pw/compare_ee_vs_pw.py
# → localresearch/measure_pw/ee_vs_pw/{summary.json,diff_*.png}

# 7. time-domain comparison
python3 tools/measure_pw/compare_ee_vs_pw_time_domain.py
# → localresearch/measure_pw/ee_vs_pw/{td_summary.json,td_*.png}

# 8. tear down (restores speakers, restarts EE)
bash tools/measure_pw/teardown_chain.sh
bash tools/measure_ee/teardown.sh
```

## Equivalence thresholds

These are the targets the comparison scripts check against by
default. Real-world results on the development device come in
comfortably under all of them.

| metric | target | rationale |
|---|---|---|
| Frequency-domain max |Δ| (50 Hz–18 kHz) | ≤ 0.5 dB | Below the audible threshold for tonal-balance changes; well within EE's own preset-to-preset variance. |
| Time-domain S/R (signal-to-residual) | ≥ 30 dB | Safety margin, not a ceiling. Mono-symmetric stimuli on the dev device with the full LSP+Calf chain measure +70..+73 dB; a sub-30 result is a real regression. Asymmetric stereo stimuli (`stimulus_stereo_pink`) exercise the M/S split and pass at the same +70 dB+ band. |

## WirePlumber traps that bit during development

Three runtime gotchas that aren't obvious from the static schema —
worth knowing before you debug a "chain doesn't process audio"
mystery:

1. **`pw-cat --target` is a hint, not a directive.** WirePlumber
   policy routes the playback stream to whatever the system default
   sink is. The fix is `pactl set-default-sink effect_input.<NAME>`
   (which `setup_chain.sh` does, restoring on teardown).

2. **WirePlumber auto-links the chain output to every Audio/Sink it
   sees**, even with `target.object = ee_capture` baked into the
   conf. If `easyeffects_sink` is around, the chain output goes to
   *both* `ee_capture` *and* `easyeffects_sink` → the latter loops
   back through EE's processing → ends up on `ee_capture` again, a
   few ms delayed → **comb-filter pattern on the captured spectrum**.
   The fix is to stop EE for the duration (which `setup_chain.sh`
   does, then `teardown_chain.sh` restarts it).

3. **The `filter.graph` block needs explicit `inputs = [...]` and
   `outputs = [...]` arrays.** Without them, audio enters the chain's
   input sink but never reaches any node — the graph has no
   externally-routed endpoints. Symptom: the chain loads, registers
   nodes, accepts audio at the input sink monitor, but its output
   ports produce silence. This was missing from the design doc.
   `ee_to_pipewire.py` emits these now and the round-trip test
   covers it.

## Why audio testing is still the final gate

`validate_conf.py` checks the static schema. It can't tell you:

- Whether the chain *runs* (some plugins fail to instantiate at the
  filter-graph layer even when their static metadata is fine).
- Whether the actual audio reaches the chain's input (WirePlumber
  routing traps).
- Whether the convolver block size, partition strategy, or FFT
  precision diverges from EE in ways that affect transients.

The compare scripts answer all of those, at the cost of ~1 min for
a full battery. Run `validate_conf.py` on every commit; run the
audio battery before tagging a release or merging a converter
change that could affect the output path.

## Captures and outputs land in `localresearch/measure_pw/`

`capture_battery.py` defaults to `localresearch/measure_pw/captures/`
and the comparison scripts default to
`localresearch/measure_pw/ee_vs_pw/`. EE-side captures stay in
`localresearch/measure_ee/captures_ee/` and stimuli in
`localresearch/measure_ee/stimuli/` (shared, since
`tools/measure_dax/make_stimulus.py` writes them once and both
sides feed from the same source). `localresearch/` is gitignored.
