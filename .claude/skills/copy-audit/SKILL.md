---
name: copy-audit
context: fork
agent: general-purpose
model: opus
description: >-
  Audits the user-facing terminal copy changed over a git range for factual
  truth rather than readability, by fanning out reviewers partitioned by
  evidence source and triaging what survives. Use before a release, after a
  batch of messaging work, or when the user asks to "check the messages are
  accurate", "verify what we're telling people", "audit the output for
  correctness", or doubts a claim a run prints. Complements /user-review,
  which asks whether a first-time reader can act on a message; this asks
  whether the message is true, whether it holds for every device that can
  reach it, and whether its numbers still match the corpus.
---

# copy-audit

## Running as a subagent

This skill runs with `context: fork`, in a fresh subagent. Nothing from the
conversation reaches it, so everything it needs is stated here or in its
arguments. It returns one thing: the triaged report of step 4. It does
**not** fix anything: step 5 is the maintainer's choice, made in the main
conversation from that report.

- **Range:** the argument, if one names a revision or `a..b`. Otherwise
  `origin/master..HEAD`, the unpushed work. Step 2's `--since` is the
  range's base.
- **Evidence dir:** `localresearch/copy_audit/<YYYY-MM-DD>/`, which is
  gitignored. If the argument names a directory that already holds the
  step-1 files, that directory is the evidence dir: reuse its files and
  regenerate only what is missing.
- Use absolute paths in every shell command: a `cd` that fails leaves the
  shell elsewhere for every later call.
- Run reviewers as subagents at `model: opus`, with one slice and one
  evidence source each, as §3 says.
- Return the step-4 report verbatim as your final message: ranked, every
  finding with severity, the true statement and its evidence, the
  discarded findings with why, the known limits, and the patterns that
  went unrendered.

`/user-review` grades comprehension, and a false sentence can score
perfectly there. That loop's own fixes were the biggest single source of
untrue statements, because simplifying a hedged sentence is how a
hypothesis becomes an assertion.

This audit walks a git range and checks each claim against the evidence its
**type** demands. The claim-type checklist it applies, with examples of a
dropped qualifier, is `.claude/rules/claims.md` ("What each claim rests on").

**Non-expert phrasing is the design goal and is never a finding.** A reviewer
reporting that copy is informal, imprecise or jargon-free has misunderstood
the task. Only FALSE, UNSUPPORTED, or TRUE-ONLY-FOR-SOME-DEVICES counts.

Copy this checklist and track progress:

```
Copy audit:
- [ ] 1. Prepare the evidence (corpus sweep, renders, system probe)
- [ ] 2. Build the claim inventory
- [ ] 3. Fan out reviewers, one slice and one evidence source each
- [ ] 4. Re-check every finding yourself; discard what doesn't survive
- [ ] 5. Fix, grouped by topic, then verify
```

## 1. Prepare the evidence, once

Every reviewer reads files, and none re-runs a tool. That is the whole cost
control: the audit is affordable because the expensive work happens once.

```
python3 tools/corpus_audit.py > <out>/corpus_audit.txt
python3 tools/preview_output.py --full --examples 3 --width 80 > <out>/renders/findings_all.txt
python3 tools/render_forced_conditions.py --out-dir <out>/renders
```

`--examples 3` is load-bearing: the same message rendered for three different
devices is what exposes a sentence true only for the one it was written from.

`render_forced_conditions.py` covers what `preview_output.py` structurally
cannot: messages whose trigger value never occurs in the corpus, so no real
device can show them. Those are exactly the messages nobody has ever read.

Then render the conditions that need flags rather than XML values: a
`SOUNDWIRE_*` file against an HDA one, a simplified-schema file,
`--all-profiles`, `-v`, each `--disable` name, and a
`dolby_to_pipewire.py --no-activate --dry-run`. Probe the live system too,
because several claims are about other people's software:
`easyeffects --version`, `pw-cli ls Node | grep alsa_output`, `pw-link -l`,
`command -v lv2info`.

## 2. Build the claim inventory

```
python3 tools/extract_claims.py --since <rev> --out-dir <out>
```

It writes `claims.md` and the per-reviewer slices. Only `CHANGED` rows are
targets. Unchanged strings stay in the file so a claim can be read against
the run it prints in.

It reports two totals, so compare the distinct count, which counts sentences
rather than sites. A refactor that collapses a string written at two sites
legitimately shrinks the row count, while the run prints the same words.

## 3. Fan out, partitioned by evidence source

Give each reviewer one slice and one evidence source. Partitioning by
evidence rather than by file keeps each context small. The reviewer checking
corpus figures never loads the wrapper's source, and the one checking the
wrapper never loads the docs.

| Slice | Evidence | Hunts |
|---|---|---|
| `slice_numbers.md` | `corpus_audit.txt` + `cross-device-findings.md`, plus targeted corpus greps | stale figures, universals the corpus contradicts, "rare"/"typical" with nothing behind it |
| renders/ | the rendered runs + `corpus_audit.txt` | sentences true only for the device they were written from; hardcoded Hz/band counts/profile names; contradictions inside one run |
| `slice_generator.md` | the generator's source | copy vs what the code does: flag effects, what was written, `-v` gating, gates broader or narrower than the sentence |
| `slice_generator.md` | `reference.md` "Validated vs unvalidated mappings" + `design-notes.md` and `docs/research/` | unvalidated mappings asserted as fact; a *leading hypothesis* stated as Dolby's intent |
| `slice_wrapper_docs.md` | wrapper/converter source + the live system | restart and undo instructions, command output shapes, WirePlumber/EasyEffects behaviour, package names |
| `slice_changelog.md` | the code each entry describes | `## Unreleased` entries that misstate what ships |
| `slice_all_changed.md` | the sources themselves | CHANGELOG vs what ships, README vs the menus, one fact worded two incompatible ways |

Each returns a fixed schema, worst first, and no restated copy:

```
ID | SEVERITY | <=10-word quote | what is actually true | evidence file:line | confidence
```

CRITICAL false and it changes what the user does · HIGH false but low
consequence, or an unvalidated hypothesis stated as fact · MEDIUM true only
for some triggering devices · LOW stale figure, no user consequence.

Tell every reviewer: a finding must **name the true statement**. "This seems
wrong" without a replacement is not a finding, and will be discarded.

Also tell them the settled decisions from `/user-review`, so they don't
relitigate copy that survived eleven rounds. Those reopen only on proof that
one is *false*, not awkward.

## 4. Triage

Do not forward reviewer output. Re-check every finding yourself against the
code, a real run, or the system. Reviewers misread: this audit's first run
produced two claims that didn't survive, one of them a plain misreading of a
distributive sentence.

Two bars before anything ships: it names what is actually true, and it cites
evidence. Then rank, and let the user choose.

Ask what population a statistic is drawn from before believing it. The
reviewer failure mode that mirrors the writers' is a **selection effect read
as a refutation**. One reviewer reported that 23 of 23 XMLs declaring a
default profile contradict the tool's assumption. But Dolby only writes that
field when they *don't* want the default, so those 23 are exactly where a
difference is expected.

## 5. Fix, grouped by topic

Group the fixes into one commit per topic, and put a code change and its
CHANGELOG or README line in the same commit. A merged group lands at its last
member's position, because a later commit may introduce what an earlier member
patches.

Re-derive corpus figures as `.claude/rules/claims.md` "What each claim rests
on" says.

Verify after: `pytest tests/`, and check that `tests/test_golden_preset.py`
did **not** move. A copy-only fix that shifts the golden digest touched
behaviour, so investigate before re-recording. Re-run `preview_output.py` and
diff against the step-1 renders: only the lines a finding named may have
changed.

## Known limits

State these in the report.

- Claims about the Windows Dolby app aren't testable here. The available
  verdicts are "matches what our docs record" or "unsupported". Propose
  hedging, not deletion.
- Hardware-probe copy for smart amps can't be exercised without the hardware.
  Label those as source-verified only.
- Anything about the live audio graph is static analysis until it is heard.
  Say so, and route it through **/audio-validate**.
