---
name: audio-validate
description: >-
  Validates a change to the audio output path against measured on-device ground
  truth. Use after any change to FIR generation, gain staging, filter
  parameters, or the EasyEffects / PipeWire output chain, before adopting or
  shipping it — and whenever the user asks to "test on device", "capture the EE
  response", "compare against DAX", or confirm a change didn't regress audibly.
  This skill gates on an audio handoff and drives the live-EE capture → DAX/EE
  compare → listening route end-to-end. Do NOT run tools/measure_ee/ scripts or
  any live capture outside this gated flow.
---

# audio-validate

The pytest suite catches **structural** regressions, not **audible** ones.
Every change to the audio output path is decided on **measured on-device ground
truth** — DAX captures plus a live-EasyEffects loopback — not on offline math.
This skill runs that validation safely and reports a verdict.

Tools live in `tools/measure_ee/` (Linux EE capture) and `tools/measure_dax/`
(Windows DAX capture + shared `analyze.py`). Read `tools/measure_ee/README.md`
for the exact current invocations and flags before running — treat the commands
below as the shape of the flow, not a frozen copy. Default all output dirs to
`./localresearch/measure_ee/` (gitignored), never `~/` or `/tmp/`.

## 0. Offline pre-screen (optional, never decisive)

If comparing several variants, run `compare_ee_analytical.py` first to narrow
the set. Offline analytical scoring (FIR magnitude, the biquad chain model)
**only predicts direction** — it ignores the dynamics stages and real hardware.
Never adopt a change on offline metrics alone; it is a filter, not a verdict.

## 1. Audio-handoff gate — STOP here first

Before running ANY tooling below, **ask the user to take over audio**: "I'm
about to run the EE capture route — it mutes your speakers, reroutes sinks,
restarts EasyEffects, and plays a stimulus battery. Ready for me to take over
audio?" Wait for an explicit yes. These scripts disrupt the live session and can
leave routing broken if interrupted unannounced.

## 2. Capture-validity check

- **DAX captures are converter-independent ground truth** — they stay valid
  across `dolby_to_easyeffects.py` edits. Reuse the existing ones.
- **EE-side captures go stale** after any FIR/scaling/gain change to the
  converter. If the EE capture you'd compare against predates the change under
  test, regenerate it (steps 3–4) before comparing — otherwise the diff is
  measuring the old preset.

## 3. Live-EE capture route

1. `bash tools/measure_ee/setup_null_sink.sh` — loads `module-null-sink
   ee_capture`, points `~/.config/easyeffects/db/easyeffectsrc` at it
   (`outputDevice=ee_capture`, `useDefaultOutputDevice=false`), restarts EE.
   (EE 8.x reads `db/easyeffectsrc`, not the legacy top-level file. Mic
   indicator pops once on restart — expected.)
2. **Smoke-gate the route** with a bypass preset before trusting any capture:
   `python3 tools/measure_ee/smoke.py --target ee_capture.monitor`. Expect PASS
   — gain ~0 dB, flatness < 0.5 dB, residual < −35 dB. `smoke.py` does the
   `pw-record --target 0` + manual `pw-link` itself (WirePlumber treats
   `--target` as a hint and would otherwise reroute to the mic). If smoke fails,
   stop and diagnose the route — don't run the battery.
3. Run the battery with the real preset: `python3
   tools/measure_ee/capture_battery.py --preset <Name> --target
   ee_capture.monitor --out-dir ./localresearch/measure_ee/ee_captures …`.
4. Don't play other audio during a capture — anything hitting `easyeffects_sink`
   contaminates the measurement. Sample rate is locked at 48 kHz.

## 4. Compare

- `analyze.py` the EE captures, then `python3
  tools/measure_ee/compare_ee_vs_dax.py --ee-dir … --dax-dir …` for the
  frequency-domain overlay; `compare_ir_time_domain.py` for envelope/peak.
- Judge on the measured **EE−DAX** (and EE−XML) result across all bands, not a
  single number.

## 5. Listening pass

Tell the user what to listen for based on what the change touched
(symptom → past trap): clipping / level jumps (convolver autogain +50 dB, MBC
output-gain) · pumping on quiet→loud transitions (why autogain is bypassed) ·
ripple / muddy mids / harsh highs (parametric-bell IEQ stacking) · loudness loss
(over-conservative PEQ output-gain / headroom) · noise-floor boost in silence
(LSP MBC upward-compression). Confirm audio quality with the user — the meter
and the ear are both required to sign off.

## 6. Restore audio

`bash tools/measure_ee/teardown.sh` — restores the db rc from backup and unloads
the null sink. Always run this, including on failure, so the user's speakers
come back.

**Then verify EE actually rebuilt its pipeline, not just that it runs:**
`pw-link -l | grep ee_soe_output_level` must show links into the speaker sink.
An EE restarted amid graph churn (sinks/chains being torn down around it) can
come up with `easyeffects_sink` present but **no processing graph behind it** —
a silent black hole that swallows every app routed to it, while short event
sounds on the raw default sink still play (2026-08-21: cost the user Firefox
and Spotify audio for ~40 min post-teardown). Process alive + sink listed +
rc correct is NOT restored; the output links are. If they're missing, restart
EE once more after the graph has settled.

## Dead ends — don't retry (capture route)

Ruled out in prior sessions; `smoke.py` already encodes the working fix:

- `pw-record --target=easyeffects_sink` / `easyeffects_source`, or `-P
  node.target=` / `target.object=` — WirePlumber policy overrides them all and
  reroutes to the mic. And `easyeffects_sink:monitor` is the *pre*-processing
  port anyway. Only `--target 0` + a manual `pw-link` works.
- For repeated preset switches use `easyeffects -l <name>` (no mic-indicator
  pop); only a `--service-mode` restart pops.

(A bullet here used to rule out `lv2apply`/`module-filter-chain` as LV2 hosts —
refuted 2026-08-19: both host the LSP/Calf plugins this project uses;
`work:schedule` is optional for them. Calf Saturator aborts at teardown *after*
a complete render — validate offline renders by frame count, as
`tools/measure_ee/render_vbe_chain.py` does. Offline renders stay a pre-screen,
not a validation.)

## Report

State: what changed, the measured EE−DAX/EE−XML deltas (with the plots written
under `./localresearch/measure_ee/`), the listening result, and a clear
adopt / reject / needs-more-data verdict. If you only got to the offline
pre-screen, say so — that is not a validation.
