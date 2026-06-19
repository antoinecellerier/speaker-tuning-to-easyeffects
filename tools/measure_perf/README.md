# measure_perf — EasyEffects vs PipeWire-filter-chain performance

Quantifies the runtime cost of the two delivery paths for the *same* preset on
the *same* machine, so a user with a performance criterion can choose. Backs
the README's "Which should I use?" guidance. Sibling to
[`tools/measure_pw/`](../measure_pw/) (which proves the two paths are
*acoustically* equivalent); this one measures what they *cost*.

[`compare_paths.py`](compare_paths.py) runs three conditions — `bypass`
(no processing), `ee` (EasyEffects), `pw` (PipeWire filter-chain) — over
several interleaved rounds and reports CPU, memory, and real-time headroom.

## The one idea that makes it valid: measure cycles, not time

On a laptop the CPU clock is **not pinnable** — even with `performance` governor
and turbo disabled, the frequency wanders (observed 0.9–2.2 GHz *within* a
single 15 s window on the dev device). Time-based metrics (CPU%, `pw-top`
BUSY-µs) scale with frequency: the *same* DSP work reads differently as the
clock moves, so a CPU% comparison is dominated by clock noise.

The fix is to count **CPU cycles** via `perf`. The cycle count to push a fixed
DSP workload (48 kHz stereo through the convolver + EQ + MBC + limiter) is the
same regardless of clock speed — at low freq those cycles take longer wall-time,
at high freq less, but the *count* is invariant. So **`Gcyc/s` is the headline
metric**; CPU% is recorded but secondary (and visibly noisier).

Other noise controls:

- **Differential.** Every path is reported against a same-session `bypass`
  baseline measured back-to-back; the bypass-subtracted *marginal* is the cost
  of adding that path. Totals include the main pipewire daemon **and** the
  path's process(es), so the subtraction is like-for-like.
- **EE off for `bypass` and `pw`.** EasyEffects' analyzers burn CPU even idle,
  so it's stopped except in the `ee` condition.
- **Warm-up discard + interleaved rounds** (rotated order) to spread thermal
  drift across conditions; **median / p95 / IQR**, never a bare mean.

## Validity gates (a noisy window is flagged, not reported)

Each window is marked invalid (and dropped from the stats) unless: cycles were
counted, the tracked PIDs stayed alive, the processing nodes were actually
running (`pw-top` BUSY>0), and the captured output was non-silent.

**Expected-response gate.** Every condition renders into the `ee_capture` null
sink (mute-proof — audio never reaches the speaker); the harness captures each
output spectrum and asserts **`pw ≈ ee`** (both apply the same correction) and
**both differ from `bypass`**. PW reproducing the already-validated EE chain is
the proof that we're measuring two paths doing the *identical* DSP — not, say,
an EE that silently loaded no preset. (That bug actually surfaced during
bring-up: EE must be told `easyeffects -l <preset>` or it passes through.)

## Prerequisites

- **Audio handoff** — the harness stops/starts EE, swaps the default sink, and
  loads a filter-chain. Output stays on the silent `ee_capture` null sink, so
  the speaker mute is irrelevant. A `try/finally` restores EE config, the
  default sink, and the quantum even on crash.
- **`perf`** (`linux-perf`) with `perf_event_paranoid <= 1`
  (`sudo sysctl kernel.perf_event_paranoid=1`; reverts on reboot). Without it
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

Writes `localresearch/measure_perf/perf_summary.json` (medians + the marginal
deltas + the response-check verdict). `--rounds/--warmup/--window/--capture`
tune rigor vs wall-clock.

## Reference numbers (dev device)

X1 Yoga Gen 7 (Alder Lake hybrid, HDA), `Dolby-Balanced`, 48 kHz / 1024
quantum, 5 rounds, all 15 windows valid:

| Metric | bypass | EasyEffects | PW filter-chain |
|---|---|---|---|
| **CPU, marginal (Gcyc/s)** | — | **+0.37** (IQR 0.01) | **+0.33** (IQR 0.01) |
| CPU% (secondary, freq-noisy) | 0.9 | 11.6 | 10.0 |
| **Memory, Pss (MB)** | 22 | **270** | **78** |
| xruns @ 1024/48k | 0 | 0 | 0 |
| Response check | — | — | `pw≈ee 0.5 dB, both ≠ bypass 17.5 dB` |

**Reading the CPU% column — it is smaller than it looks.** These are
*one-core-equivalent* percentages (fraction of a *single* core's wall-time),
**not** a share of the whole CPU: ~10 % of one core is ~0.6 % of this 16-core
laptop's total capacity. They were also measured on an otherwise-idle machine
whose clock sat low (~1–2 GHz), so the percentage is relative to a *modest*
clock — not "10 % of the CPU at full tilt." The real, clock-independent cost is
the cycle figure: **~0.33–0.37 Gcyc/s marginal ≈ a tenth of one modern core**.
CPU% is shown only as a familiar (if noisy) cross-check.

Takeaways: the PW chain costs **~11 % fewer CPU cycles** and **~3.5× less RAM**
(the EasyEffects process alone is ~248 MB of Qt/GUI vs the chain child's
~56 MB); both run xrun-free with zero added latency. Numbers are device-specific
— ship the tool so users measure their own; treat these as the reference point,
not a universal claim.

## Caveats

- `Gcyc/s` is whole-process (main daemon + path process), bypass-subtracted; it
  is not a per-node DSP figure. `pw-top` BUSY is recorded per node but is
  frequency-sensitive, so it's a cross-check, not the headline.
- The spectral response-check is a *shape* sanity (`pw≈ee`, both≠bypass), not
  the ±0.5 dB equivalence battery — that lives in `tools/measure_pw/`.
- `setup_chain.sh` is rotted (its `--target-object` without `--target-sink ''`
  now generates a conflicting smart-filter conf); this harness uses its own
  lean loader instead.
