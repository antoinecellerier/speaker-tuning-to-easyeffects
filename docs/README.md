# Further reading

In-tree docs and tooling with more context:

- [docs/reference.md](reference.md) — current-state reference:
  XML→parameter mappings, the plugin chain, units, profile differences, and
  what's not implemented
- [docs/design-notes.md](design-notes.md) — research log index: why the
  plugin chain is ordered the way it is, the unvalidated scaling factors, and
  which research file holds what
- [docs/research/](research) — the research log, one file per problem
  class: gain-staging rationale, why autogain is bypassed by default, an
  empirical comparison of our generated FIR against DAX3's actual response on
  Windows, hardware and drivers, and more
- [docs/code-organisation.md](code-organisation.md) — how the two entry
  points are split into `lib/`: the module layout, and the git
  discipline that keeps `git blame -C` tracing code back through an extraction
- [docs/cross-device-findings.md](cross-device-findings.md) — empirical
  analysis of ~2,800 DAX3 tuning files across Realtek, Senary, Qualcomm Aqstic,
  and SoundWire smart-amp codecs, including which DSP blocks are unmodeled
- [docs/corpus.md](corpus.md) — what those tuning files are: how one is
  counted, which OEM driver package each came from, what the collection is
  skewed towards, and how to compare your own against it
- [docs/alternative-pipelines.md](alternative-pipelines.md) — design
  sketches for offloading parts of the pipeline to Intel SOF DSP or running
  under PipeWire filter-chain instead of EasyEffects
- [docs/ee-to-pipewire.md](ee-to-pipewire.md) — current architecture of the
  `ee_to_pipewire.py` companion converter: smart-filter routing, self-contained
  conf layout, plugin coverage, and equivalence guarantees
- [tools/measure_dax/](../tools/measure_dax) — Windows-side capture + Linux-side
  analysis scripts for measuring DAX3's actual response via WASAPI loopback.
  They reproduce the empirical comparison in
  `docs/research/measuring-against-windows.md` on any Lenovo/ThinkPad with DAX3
  installed.
- [tools/measure_ee/](../tools/measure_ee) — Linux-side counterpart: captures
  the live EasyEffects pipeline, with our generated preset applied, into the
  same `loopback_*.{wav,json}` schema. `tools/measure_dax/analyze.py` and
  `tools/measure_ee/compare_ee_vs_dax.py` can then overlay the EE-on-Linux
  response next to the DAX-on-Windows reference.
- [tools/measure_pw/](../tools/measure_pw) — captures and validates the PipeWire
  filter-chain rendering of the same preset by the `ee_to_pipewire.py`
  companion. Its `validate_conf.py` deterministic schema check catches
  inverted bools, unknown ports and out-of-range values without any audio
  capture. `compare_ee_vs_pw.py` and `compare_ee_vs_pw_time_domain.py` overlay
  the PW captures against the EE-side captures from `tools/measure_ee/`.
- [tools/measure_perf/](../tools/measure_perf) — measures what EasyEffects and
  the PipeWire filter-chain *cost*, in frequency-invariant `perf` CPU cycles,
  memory, and xruns. `measure_pw` proves the two delivery paths *sound* the
  same. Backs the "Which should I use?" guidance in
  [dolby-to-pipewire.md](dolby-to-pipewire.md#which-should-i-use).
