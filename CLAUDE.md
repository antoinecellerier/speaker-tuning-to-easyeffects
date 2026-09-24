# CLAUDE.md

<!-- House rules for THIS file. Block-level HTML comments are stripped
  before CLAUDE.md is injected into context, so this note costs nothing.
  - Budget: ≤ ~120 lines of loaded content (Claude Code's docs target < 200).
    Shorter files get better adherence. Prune one stale line before adding
    one.
  - What a line holds, where else it could go, and the audit to run before
    committing: .claude/rules/instructions.md. -->

## Core invariants

- **XML-only derivability.** Every emitted parameter must trace to a parsed DAX3
  XML field, including FIR coefficients, biquad freq/Q/gain, compressor
  thresholds and regulator gains. No per-device hand-tuned offsets: they invert
  the value prop. The mappings are *hypotheses*, and the bar to change a default
  one is high: `.claude/rules/xml-derivability.md`. Current
  validated/unvalidated status: `docs/reference.md`.
- **Zero added latency** over the PipeWire quantum is a hard constraint
  (video lip-sync, interactive use), so the FIR stays **minimum-phase** and
  nothing in the chain takes look-ahead. Why that is load-bearing, and what
  the levers are: `.claude/rules/dsp-fir.md`.
- **Every claim traces to evidence** in docs, copy, comments, replies and
  commits, as every parameter traces to the XML. Assert a number only once it is
  re-derived from data. Rewording carries numbers over exactly and keeps each
  claim's scope: validated or hypothesis, which devices, what n. Checklist:
  `.claude/rules/claims.md`.

## Testing

- `pytest tests/` is the fast tier. The trap-regression suite that locks in
  every shipped bug lives in `tests/test_preset.py`.
- **The golden digest and the corpus tier** fail quietly when a digest is
  re-recorded green or a corpus walk skips: `.claude/rules/testing.md`.
- Add or extend a unit test for any math change: FIR, coefficient decoding, gain
  staging, unit conversions, filter design. A check worth re-running belongs in
  `tests/`, not an ad-hoc script. After a preset/structure change, run the
  script against a real XML and confirm the expected files appear under
  `~/.local/share/easyeffects/`.

## Validating audio changes

- **Validate on device.** Measured ground truth decides adoption: DAX captures
  and live-EE loopback. Offline analytical scoring in `tools/measure_ee/` only
  narrows the variant set; it never decides. Plan captures by /audio-validate
  step 2: DAX captures stay valid across converter edits, and EE captures go
  stale after any FIR, scaling or gain change.
- **IMPORTANT: hand off audio first.** YOU MUST ask the user to take over audio
  before running `tools/measure_ee/` or any live capture. The measurement
  tooling mutes speakers, reroutes sinks and swaps presets. Use the
  /audio-validate skill: it gates on the handoff, runs capture, compare and
  listen, then restores audio. Listen for the symptoms in its step 5
  "Listening pass", the single symptom → past-trap checklist; don't duplicate
  or fork it here.

## Repo etiquette

- **Artifacts → `./localresearch/<area>/`**, never `~/` or `/tmp/`. New scripts
  default `--out-dir` and friends there.
- **Never reference `localresearch/` paths in committed files**: source, docs,
  commit messages, issue/PR comments. Gitignored, they rot on clone. State the
  lesson directly; cite committed paths only.
- **Check for existing CLIs before writing a parser/validator**: `lv2info`,
  `pw-cli`, `spa-json-dump`, `pactl` and the like. Wrap partial tools; only add
  custom logic for project-specific checks on top. Same for Python libraries: a
  dependency that removes a whole apparatus beats hand-rolling one. Price both,
  then soft-import with a fallback so the no-dependency path keeps working, as
  `rich` and `argcomplete` do.
- **Device-issue triage updates the kernel watchlist.** Update
  `.github/kernel-watchlist.txt` in the same commit that opens or closes a
  device investigation; its header has the format. Load the
  **/kernel-watch-triage** skill before analysing a kernel-sound-watch hit.
- **Never push without explicit per-push permission.** One "commit and push"
  authorizes that push only, so re-ask for the next. Same for `--force`, tags,
  and opening/merging PRs.
- **Keep commit messages short.** Subject ≤72 chars. Add a body only where the
  *why* isn't evident from the diff, and then just the reason, not the
  investigation behind it. A commit body is read once, so rationale and
  rejected alternatives go in `docs/`, where they stay findable.
- **Add a `CHANGELOG.md` entry** under `## Unreleased` when a meaningful
  user-facing functional change, a newly reported/tested device, or an in-depth
  research conclusion ships. Other plain docs edits get none. What counts and
  how to word it: `.claude/rules/changelog.md`.
- **Investigation flags are scaffolding.** Revert the flag once its hypothesis
  on `dolby_to_easyeffects.py` is closed, and record the finding in the
  research log. Exceptions: the `tools/` measurement harness, and a user-facing
  opt-in the finding justifies, such as `--enable autogain`.
- **Issue triage & GitHub comments:** load the /issue-replies skill when
  starting triage and before drafting or posting any reply.
- **Comparison plots:** verify every curve is actually visible, because a hidden
  curve reads as agreement. How: `.claude/rules/plots.md`.
- **Co-locate definitions with use.** A constant or helper sits by its user,
  grouped by meaning, not piled at module top. Module-wide values are exempt.
- **Comments and docstrings state the current fact and its why.** Cite where
  the evidence lives, such as its research unit's `r-` tag, instead of restating
  it. History belongs in git. A docstring's first line says what it does.
- **Docs are layered**; README "Further reading" links all. `docs/reference.md`
  is the current-state reference: mappings, plugin chain, units,
  not-implemented. `docs/design-notes.md` and the per-class `docs/research/`
  files are the research log, where the why, findings and rejected approaches
  go. `docs/cross-device-findings.md` is the corpus; README is the guide.
- **Repo root is the command surface:** only the three entry-point scripts.
  `tests/test_layout.py` enforces it; other modules go in `lib/`. Extraction
  recipe: `docs/code-organisation.md` "Splitting the single-file scripts".
- **End-of-run messages** follow the `.claude/rules/user-messages.md` copy
  contract: detail inline, one short sentence in the closing block, one link.

## Past rabbit holes worth skipping

- The base64 `filter_coefficients` blob in `tuning-vlldp` is VLLDP-internal
  analysis filters, not an audio EQ: `docs/reference.md`.
- EE preset format has three traps: enum labels, `.irs` and `kernel-name`. All
  three load silently and do nothing: `.claude/rules/ee-preset-format.md`.

## ee_to_pipewire.py — companion converter

It turns the generated EE preset into a PipeWire `filter-chain` `.conf` for
users not running EE. **Stereo only**; 4-channel upmix isn't translated. It pins
a WirePlumber 0.5+ smart filter to the internal-speaker sink and self-validates
via `lv2info`. Keep both defaults on. Routing, flags and plugin coverage,
including autogain: `docs/ee-to-pipewire.md`.
