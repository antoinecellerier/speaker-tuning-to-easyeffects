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
  invert the value prop. The mappings are *hypotheses*, and the bar to
  change a default one is high: `.claude/rules/xml-derivability.md`.
  Current validated/unvalidated status: `docs/reference.md`.
- **Zero added latency** over the PipeWire quantum is a hard constraint
  (video lip-sync, interactive use), so the FIR stays **minimum-phase** and
  nothing in the chain takes look-ahead. Why that is load-bearing, and what
  the levers are: `.claude/rules/dsp-fir.md`.

## Testing

- `pytest tests/` — fast (DSP math, output schema, the trap-regression
  suite that locks in every shipped bug, `--disable`/argparse). The traps
  live in `tests/test_preset.py`.
- **The golden digest and the corpus tier** both fail quietly — a digest
  re-recorded green, a corpus walk that skipped: `.claude/rules/testing.md`.
- Changing math (FIR, coefficient decoding, gain staging, unit
  conversions, filter design)? Add or extend a unit test — a check worth
  re-running belongs in `tests/`, not an ad-hoc script. For preset/structure
  changes, run the script against a real XML and confirm the expected files
  appear under `~/.local/share/easyeffects/`.

## Validating audio changes

The suite catches **structural** regressions, not **audible** ones — past
sessions shipped bugs that only showed on real content.

- **Validate on device.** Measured ground truth (DAX captures + live-EE
  loopback) decides adoption. Offline analytical scoring (`tools/measure_ee/`)
  is a *pre-screen* to narrow the variant set — never the deciding signal.
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
  logic for project-specific checks on top. Same for Python libraries — a
  dependency that removes a whole apparatus beats hand-rolling one. Price
  both, then **soft-import with a fallback** (`rich` / `argcomplete`) so the
  no-dependency path keeps working.
- **Device-issue triage updates the kernel watchlist** — opening or closing
  a device investigation → update `.github/kernel-watchlist.txt` in the same
  commit (`# watch: #NN` headers; `standing` = outlives the issue). The
  kernel-sound-watch workflow greps new sound-tree pull tags with it, one
  hit comment per tag. **Investigated a hit? Append the verdict to that
  comment** (`### Triage (YYYY-MM-DD)`: resolved commit(s), impact, action,
  Claude footer) — bookkeeping, not a reply; no draft-review cycle needed.
- **Never push without explicit per-push permission.** One "commit and
  push" authorizes that push only — re-ask for the next. Same for
  `--force`, tags, and opening/merging PRs.
- **Keep commit messages short.** Subject ≤72 chars; a body only where the
  *why* isn't evident from the diff, and then the reason — not the
  investigation behind it. A commit body is read once; rationale and rejected
  alternatives go in `docs/`, where they stay findable.
- **A meaningful user-facing functional change, a newly reported/tested
  device, or an in-depth research conclusion ships → add a `CHANGELOG.md`
  entry** under `## Unreleased`; other plain docs edits don't. What counts and
  how to word it: `.claude/rules/changelog.md`.
- **Investigation flags are scaffolding.** Once a hypothesis on
  `dolby_to_easyeffects.py` is closed, revert the flag and record the
  finding in design-notes. Exceptions: a user-facing opt-in the finding
  justifies (`--enable autogain`), and the `tools/` measurement harness.
- **Issue triage & GitHub comments:** load the **/issue-replies** skill when
  starting issue triage and before drafting any reply — the draft-for-review
  flow, assertion/citation rules, and triage asks live there; don't post
  without it.
- **Comparison plots:** verify every curve is actually visible — a hidden
  curve reads as agreement. How: `.claude/rules/plots.md`.
- **Co-locate definitions with use** — a constant or helper sits by its user,
  grouped by meaning, not piled at module top (module-wide values excepted).
- **Repo root = command surface** — only the three entry-point scripts; every
  other module goes in `lib/`. Splitting the two behemoths is in progress —
  target shape and the move-commit rules are in `docs/design-notes.md`
  "Splitting the single-file scripts".
- **End-of-run messages** follow a copy contract — detail inline, one short
  sentence in the closing block, one link: `.claude/rules/user-messages.md`.

## Past rabbit holes worth skipping

- `filter_coefficients` (`tuning-vlldp` base64 blob) is VLLDP-internal
  analysis filters, NOT an audio EQ — see `docs/reference.md`.
- EE preset format traps (enum labels, `.irs`, `kernel-name`) — all three
  load silently and do nothing: `.claude/rules/ee-preset-format.md`.

## ee_to_pipewire.py — companion converter

Turns the generated EE preset into a PipeWire `filter-chain` `.conf` for
users not running EE. **Stereo only**; 4-channel upmix isn't translated.
It pins a WirePlumber 0.5+ smart filter to the internal-speaker sink and
self-validates via `lv2info` — keep both defaults on. Plugin coverage
(incl. autogain), routing, and flags: `docs/ee-to-pipewire.md`.

Docs are layered (README "Further reading" links all): `docs/reference.md`
= current-state reference (mappings, plugin chain, units, not-implemented);
`docs/design-notes.md` = research log (the why; new findings + rejected
approaches go here); `docs/cross-device-findings.md` = corpus; README = guide.
