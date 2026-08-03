---
name: user-review
description: >-
  Review the scripts' user-facing terminal output by running it past subagent
  reviewers role-playing a first-time user, then report severity-ranked
  findings. Use after changing any message a user reads — end-of-run blocks,
  warnings, flag menus, asks, phase banners — and whenever the user asks to
  "review the output", "check how this reads", "run it past a user", or wants
  to know whether a message is understandable and actionable. The test suite
  traps structure; this catches copy that is confusing, contradictory, or
  impossible to act on.
---

# user-review

The suite pins *structure* — one sentence per ask, no stray URL, a clean run
collapsing to the ask alone. None of that catches a sentence that reads as
gibberish, two messages that contradict each other, or an ask nobody can act
on. Only a reader who doesn't already know the answer finds those.

Re-run this after fixing. Each round so far has found faults introduced by the
previous round's fixes.

## 1. Capture real output

Capture at `COLUMNS=80`. Never hand-write or paraphrase samples — reviewers
must see exactly what a user sees, wrapping included.

- Per-message coverage: `tools/preview_output.py --width 80` finds a real
  corpus XML for each finding and prints the resulting run. `--list` reports
  which XML matches what, and names any pattern with no corpus match.
- Whole experience: one full real run, so the reviewer meets the diagnostics,
  the scrollback and the ending in the order a user does.
- Both entry points: `dolby_to_easyeffects.py` and `dolby_to_pipewire.py`.
  Their readers differ — the wrapper's user chose it to avoid EasyEffects — so
  findings do not transfer.
- Wrap the command in `script -qec "…" /dev/null` when ordering matters.
  Piping makes stdout block-buffered and reorders it against stderr, which a
  terminal does not do.

Run `dolby_to_pipewire.py` only with `--dry-run` or `--no-activate`; otherwise
it restarts PipeWire. Point `--output-dir` at a scratch directory.

## 2. Give the reviewer the output and nothing else

This rule makes the result worth anything, and it is the easiest to break.

Do not name the sections, say which lines print mid-run versus at the end,
explain that a bracketed tag is a handle for reports, or define any term the
output uses. A user has none of that. Every hint is comprehension granted
rather than measured, and it turns a finding into a pass.

If the reviewer has to work out what a `[tag]` is, that *is* the finding.

Exception: name flags the harness passed that a user would not (`--dry-run`
forced by the preview tool), so they don't report those as faults.

## 3. Reviewer prompt template

```
ROLE-PLAY. You are NOT a developer on this project. Stay in character, and do
not read any source code.

You own a Linux laptop and wanted better speaker sound. You found
speaker-tuning-to-easyeffects on GitHub, cloned it, and ran <command>. You can
use a terminal, copy-paste commands, and file a GitHub issue. You are NOT an
audio engineer. You have never heard of Dolby DAX3, "the regulator", "volmax",
"IEQ", "audio optimizer", "PEQ", "MBC", "smart amp", or "volume leveler". You
just want your laptop to sound good.

Read: <captured output path>

Judge as the user: do you understand it, do you know what to do, would you
bother.

<output format from §5>

Be harsh but fair. No praise. Do not propose code changes. Do not invent
filler findings — say so if a severity level is empty.
```

Spell out the unknown terms explicitly. Without that list the model supplies
the expertise itself and reports that everything is clear.

## 4. Simulate the terminal

At least one reviewer experiences it as a screen, not a file: give them the
last ~26 lines first, ask what happened and what they would do next, and only
then let them scroll. Whether they would scroll at all is itself a finding.

This is how "the success line scrolled off the top and the last screen looked
like a failure" surfaces. Reading top-to-bottom hides it.

Dispatch three reviewers in parallel over different slices. Fixes come later,
one at a time.

## 5. Output format

Require one list, numbered 1..N, worst first, no grouping:

```
SEVERITY: CRITICAL | HIGH | MEDIUM | LOW
THE LINE: <quoted verbatim>
WHAT'S WRONG: one or two sentences, from your point of view as the user
THE FIX: your rewrite, one sentence
```

- CRITICAL — misleads, contradicts itself, or asks the impossible
- HIGH — can't act on it, or would act wrongly
- MEDIUM — gets there eventually at a cost, or would skip it
- LOW — wording nit

Ask for one line up front on whether they would keep using it and file the
report, and at most five bullets afterwards on what they expected to be a
problem and isn't — that names what not to undo later.

## 6. Triage before fixing

Reviewer output is evidence, not instruction.

- Verify every claim against the code and a real run. Some are misreadings;
  several have been genuine bugs the suite passed straight over.
- Drop harness artifacts — anything caused by flags or scratch paths the
  capture used, which a real user won't hit. Say which you dropped and why.
- Weight agreement: two reviewers reaching the same conclusion independently
  has been the strongest signal available.

Report the ranked, triaged list and let the user choose what to fix. The list
is normally longer than the change they want.
