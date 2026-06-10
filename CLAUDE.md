# CLAUDE.md

<!-- House rules for THIS file. Block-level HTML comments are stripped
  before CLAUDE.md is injected into context, so this note is free — it
  costs no session tokens and is visible only when the file is opened.
  - Keep it lean. Claude Code docs cap CLAUDE.md at < 200 lines; we target
    ≤ ~120 lines of *loaded* content — this stripped comment is free and
    doesn't count. Shorter files get better adherence (bloat → ignored
    rules). Prune one stale line before adding one.
  - Each bullet is a rule NOT derivable from the code and worth re-stating.
    Long rationale → docs/; multi-step procedures → a skill; guidance that
    only matters for some files → a path-scoped .claude/rules/ file.
  - Run /claude-md-audit before committing any change to this file. -->

A few things about this repo that aren't obvious from the code.

## Core invariants — don't break these

- **XML-only derivability.** Every parameter emitted (FIR coefficients,
  biquad freq/Q/gain, compressor thresholds, regulator gains…) must trace
  to a parsed DAX3 XML field — no per-device hand-tuned offsets, which
  invert the value prop. The XML→param mappings are *hypotheses about the
  schema*, not revealed truth: DAX captures are the only signal that can
  falsify them, but the bar to change a default mapping is high (≥1
  second-device capture confirming it generalises across all bands).
  Empirical tuning, if ever wanted, ships opt-in so the XML-only path stays
  default. Full evidence + the empirical-shortcut and unvalidated-scaling
  lists: `docs/design-notes.md`.
- **Zero added latency** over the PipeWire quantum is a hard constraint
  (video lip-sync, interactive use). FIR stays **minimum-phase** — the
  cepstral processing in `make_fir` is load-bearing; a naive inverse-FFT
  on a target magnitude gives a linear-phase filter with audible
  pre-ringing *and* ~42 ms group delay. The latency lever is peak
  position, not IR length. Surface the trade-off before proposing longer
  FIRs / look-ahead / phase-flat reconstruction.

## Testing

- `pytest tests/` — fast (DSP math, output schema, the trap-regression
  suite that locks in every shipped bug, `--disable`/argparse). The traps
  live in `tests/test_preset.py`.
- Corpus tier (`tests/corpus/`) runs the full pipeline against real DAX3
  XMLs it auto-discovers (NTFS mounts + CWD); `ATMOS_CORPUS_DIR` overrides;
  it skips cleanly when no corpus is reachable. The ee_to_pipewire corpus
  tier validates each conf via `lv2info` — minutes of wall-clock, marked
  `slow`, **skipped unless `--run-slow`** (or `ATMOS_RUN_SLOW=1`). Its fast
  structural invariants still run by default (`tests/test_ee_to_pipewire.py`).
- Changing math (FIR, coefficient decoding, gain staging, unit
  conversions, filter design)? Add or extend a unit test — a check worth
  re-running belongs in `tests/`, not an ad-hoc script. For preset/structure
  changes, run the script against a real XML and confirm the expected files
  appear under `~/.local/share/easyeffects/`.

## Validating audio changes

The suite catches **structural** regressions, not **audible** ones — past
sessions shipped bugs that only showed on real content.

- **Validate on device.** Measured ground truth (DAX captures + live-EE
  loopback) decides adoption. Offline analytical scoring (FIR magnitude,
  `compare_ee_analytical.py`) is a *pre-screen* to narrow the variant set —
  never the deciding signal.
- **Hand off audio first — IMPORTANT.** The measurement tooling mutes
  speakers, reroutes sinks, and swaps presets. YOU MUST ask the user to take
  over audio before running `tools/measure_ee/` or any live capture. Use the
  **/audio-validate** skill: it gates on the handoff and runs the
  capture → compare → listen route end-to-end, then restores audio.
- **Capture validity.** DAX captures are converter-independent and stay
  valid across edits. EE-side captures go stale after any FIR/scaling
  change — regenerate before an EE↔DAX compare.
- **Listen for** the symptom → past-trap checklist in the **/audio-validate**
  skill (step 5, "Listening pass") — that list is the single source; don't
  duplicate or fork it here.

## Repo etiquette

- **Artifacts → `./localresearch/<area>/`** (gitignored), never `~/` or
  `/tmp/`. New scripts default `--out-dir` and friends there.
- **Never reference `localresearch/` paths in committed files** (source,
  docs, commit messages, issue/PR comments) — they rot on clone. State the
  lesson directly; cite committed paths only.
- **Check for existing CLIs before writing a parser/validator** (`lv2info`,
  `pw-cli`, `spa-json-dump`, `pactl`…). Wrap partial tools; only add custom
  logic for project-specific checks on top.
- **Never push without explicit per-push permission.** One "commit and
  push" authorizes that push only — re-ask for the next. Same for
  `--force`, tags, and opening/merging PRs.
- **Investigation flags are scaffolding.** Once a hypothesis on
  `dolby_to_easyeffects.py` is closed, revert the flag and record the
  finding in design-notes — a permanent finding beats a permanent CLI
  surface. (Does not apply to the `tools/` measurement harness — keep that.)
- **GitHub issue/PR comments:** draft for review first (sourced,
  to-the-point, actionable), then post with the footer
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)` — same
  as commits get `Co-Authored-By`. Cite commits by full unquoted SHA —
  backticks suppress GitHub's auto-link (full over short for unambiguity).
- **Comparison plots:** verify every curve is actually visible — set
  z-order, plot reference curves last with dashes / a distinct colour, and
  check both axis extremes. Hidden-curve bugs cause repeated re-render cycles.
- **Co-locate definitions with use** — a constant or helper sits by its user,
  grouped by meaning, not piled at module top (module-wide values excepted).

## Past rabbit holes worth skipping

- `filter_coefficients` (the base64 blob in `tuning-vlldp`) is NOT an audio
  EQ — it's VLLDP-internal analysis filters. Speaker correction already
  lives in the audio-optimizer + PEQ parameters.
- EE preset format quirks: enum parameters are string labels, not integer
  indices (commit `91423b8`); impulse-response files need the `.irs`
  extension; EE 8.x convolver wants `"kernel-name"` (filename stem), not the
  deprecated `"kernel-path"`.

## ee_to_pipewire.py — companion converter

Turns the generated EE preset into a PipeWire `filter-chain` `.conf` for
users not running EE. **Stereo only.** Covers convolver / PEQ / dialog /
MBC / regulator / limiter (LSP-backed) plus `bass_enhancer` / `stereo_tools`
(Calf-backed). Not translated: non-bypassed `autogain` (EE's native
libebur128 has no LV2 equivalent — warns and skips) and 4-channel upmix.
By default it copies the `.irs` next to the conf and emits a WirePlumber
0.5+ smart filter pinned to the auto-detected internal-speaker sink
(`--target-sink` overrides; `''` reverts to a v1 virtual sink). It
auto-runs `validate_conf.py` (lv2info) unless `--no-validate`; full
equivalence is checked by `tools/measure_pw/`. Full detail:
`docs/ee-to-pipewire.md`.

Everything else — plugin-chain rationale, gain-staging, unit conversions,
cross-device findings — lives in `docs/` (README "Further reading").
