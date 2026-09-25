# measure_perf — EasyEffects vs PipeWire-filter-chain performance

This harness quantifies the runtime cost of the two delivery paths for the
*same* preset on the *same* machine, so a user with a performance criterion can
choose. It backs the "Which should I use?" guidance in
[docs/dolby-to-pipewire.md](../../docs/dolby-to-pipewire.md#which-should-i-use).
Its sibling
[`tools/measure_pw/`](../measure_pw/) proves the two paths are *acoustically*
equivalent. This one measures what they *cost*.

[`compare_paths.py`](compare_paths.py) runs three conditions over several
interleaved rounds: `bypass` with no processing, `ee` through EasyEffects, and
`pw` through the PipeWire filter-chain. It reports CPU, memory, and real-time
headroom.

## The one idea that makes it valid: measure cycles, not time

On a laptop you can't pin the CPU clock. The frequency wanders even with the
`performance` governor and turbo disabled. On the dev device it was observed at
0.9–2.2 GHz *within* a single 15 s window. Time-based metrics such as CPU% and
`pw-top` BUSY-µs scale with frequency. The *same* DSP work reads differently as
the clock moves, so clock noise dominates a CPU% comparison.

The harness counts CPU cycles via `perf` instead. The DSP workload is fixed:
48 kHz stereo through the convolver + EQ + MBC + limiter. Pushing it takes the
same cycle count regardless of clock speed. A slow clock spreads those cycles
over more wall-time, but the *count* is invariant. So **`Gcyc/s` is the
headline metric**. CPU% is recorded but secondary, and visibly noisier.

Other noise controls:

- **Differential.** Every path is reported against a `bypass` baseline measured
  back-to-back in the same session. The bypass-subtracted *marginal* is the cost
  of adding that path. Totals include the main pipewire daemon and the path's
  process, so the subtraction is like-for-like.
- **EE off for `bypass` and `pw`.** EasyEffects' analyzers burn CPU even idle,
  so it's stopped except in the `ee` condition.
- **Warm-up discard + interleaved rounds.** Rounds run in rotated order to
  spread thermal drift across conditions. The stats are median / p95 / IQR,
  never a bare mean.

## Validity gates (a noisy window is flagged, not reported)

The harness marks a window invalid and drops it from the stats unless all four
hold:

- cycles were counted
- the tracked PIDs stayed alive
- the processing nodes were actually running, with `pw-top` BUSY>0 in at least
  half the window's snapshots. `bypass` skips this check, since it runs no
  processing node.
- the captured output was non-silent

**Expected-response gate.** Every condition renders into the `ee_capture` null
sink, which is mute-proof because audio never reaches the speaker. The harness
captures each output spectrum. It asserts `pw ≈ ee`, since both apply the same
correction, and that both differ from `bypass`. PW reproducing the
already-validated EE chain proves the two paths run the *identical* DSP. It
rules out, say, an EE that silently loaded no preset. That bug surfaced during
bring-up: EE passes audio through unless told `easyeffects -l <preset>`.

## Prerequisites

- **Audio handoff.** The harness stops/starts EE, swaps the default sink, and
  loads a filter-chain. Output stays on the silent `ee_capture` null sink, so
  the speaker mute is irrelevant. A `try/finally` restores the EE config, the
  default sink and the quantum, even on a crash.
- **`perf`** from `linux-perf`, with `perf_event_paranoid <= 1`.
  `sudo sysctl kernel.perf_event_paranoid=1` sets it until reboot. Without it
  every window flags `no-cycles`.
- `pw-top`, `pw-record`, `pw-play`, `pw-link`, `pactl`, `pw-metadata`.

## Usage

```sh
# preview environment + plan, touches no audio:
python3 tools/measure_perf/compare_paths.py <preset.json> --check

# the real run (~6–8 min; standard 5 rounds x 5 s warm-up + 15 s window):
python3 tools/measure_perf/compare_paths.py \
    ~/.local/share/easyeffects/output/Dolby-Balanced.json
```

The run writes `localresearch/measure_perf/perf_summary.json`: medians, the
marginal deltas and the response-check verdict.
`--rounds/--warmup/--window/--capture` trade rigor against wall-clock time.

## Reference numbers (dev device)

X1 Yoga Gen 7, an Alder Lake hybrid on HDA, with `Dolby-Balanced`, 48 kHz /
1024 quantum and 5 rounds. All 15 windows were valid:

| Metric | bypass | EasyEffects | PW filter-chain |
|---|---|---|---|
| CPU, marginal (Gcyc/s) | — | +0.37 (IQR 0.01) | +0.33 (IQR 0.01) |
| CPU% (secondary, freq-noisy) | 0.9 | 11.6 | 10.0 |
| Memory, Pss (MB) | 22 | 270 | 78 |
| xruns @ 1024/48k | 0 | 0 | 0 |
| Response check | — | — | `pw≈ee 0.5 dB, both ≠ bypass 17.5 dB` |

The CPU% column is smaller than it looks. Each figure is a
*one-core-equivalent* percentage: a fraction of a *single* core's wall-time,
not a share of the whole CPU. ~10 % of one core is ~0.6 % of this machine's 16
logical CPUs. The machine was otherwise idle, and its clock sat low at
~1–2 GHz. So the percentage is relative to a *modest* clock, not "10 % of the
CPU at full tilt." The real cost is the clock-independent cycle figure:
**~0.33–0.37 Gcyc/s marginal ≈ a tenth of one modern core**. CPU% is shown only
as a familiar cross-check, and it is noisy.

### Graph sample rate (issue #84)

Same harness and `Dolby-Balanced`, with `--rate`/`--quantum`, capturing each
path's output level as well as its cycles:

| Graph rate | EE Gcyc/s | PW Gcyc/s | EE out | PW out |
|---|---|---|---|---|
| 48 kHz / 1024 | +0.33 | +0.29 | −35.9 dBFS | −35.9 |
| 96 kHz / 1024 | — | — | −30.0 | −35.8 |
| 192 kHz / 1024 | +1.49 | +0.86 | −24.1 | −35.9 |
| 192 kHz / 512 | +1.08 | +0.95 | −24.1 | −35.9 |

Two findings:

- Cost at 192 kHz is ~4.5× for EasyEffects and ~3× for the PipeWire chain, not
  the 16× a rate-squared estimate predicts. Every run in the table ran with
  turbo on, and the 96 kHz row is from n=2 rounds, so the second digit is soft.
- The EasyEffects path **plays hot by the rate ratio in dB** above 48 kHz, and
  the PipeWire path does not. This correctness bug is isolated to the convolver.
  [A preset that plays hot](../../docs/research/easyeffects-and-pipewire.md#r-convolver-resample-gain)
  has the write-up.

Two gotchas for anyone re-running this:

- `--rate` really does take effect. The 48 kHz `ee_capture` null sink does not
  pin the graph.
- Forcing the *rate* alone makes PipeWire scale the quantum with it, 1024 →
  4096, which preserves the cycle's real-time duration. Force both to hold the
  blocksize constant. `pw_force()` does that.

Takeaways: the PW chain costs ~11 % fewer CPU cycles and ~3.5× less RAM.
EasyEffects' marginal footprint is ~248 MB of Qt/GUI, against ~56 MB for the
chain child. Both run xrun-free with zero added latency. The numbers are
device-specific, so the tool ships for users to measure their own. Treat these
as the reference point, not a universal claim.

## Caveats

- `Gcyc/s` covers the main daemon plus the path process, bypass-subtracted. It
  is not a per-node DSP figure. `pw-top` BUSY is recorded per node, but it is
  frequency-sensitive, so it's a cross-check, not the headline.
- The spectral response-check is a *shape* sanity check: `pw≈ee`, both≠bypass.
  It is not the ±0.5 dB equivalence battery, which lives in
  `tools/measure_pw/`.
- This harness loads the chain with its own lean loader, not
  `tools/measure_pw/setup_chain.sh`. It brings the chain up and down around
  each `pw` window and hands the child's PID to `perf`.
