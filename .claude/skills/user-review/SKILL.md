---
name: user-review
description: >-
  Reviews the scripts' user-facing terminal output by running it past subagent
  reviewers role-playing a first-time user, then reports severity-ranked
  findings. Use after changing any message a user reads — end-of-run blocks,
  warnings, flag menus, asks, phase banners — and whenever the user asks to
  "review the output", "check how this reads", "run it past a user", or wants
  to know whether a message is understandable and actionable. The test suite
  traps structure; this catches copy that is confusing, contradictory, or
  impossible to act on.
---

# user-review

The suite pins *structure*: one sentence per ask, no stray URL, a clean run
collapsing to the ask alone. None of that catches a sentence that reads as
gibberish, two messages that contradict each other, or an ask nobody can act
on. Only a reader who doesn't already know the answer finds those.

Re-run this after fixing. Each round so far has found faults introduced by the
previous round's fixes.

## 1. Capture real output

One command produces every reviewer file:

    tools/user_review_capture.py <corpus-xml>

`tools/preview_output.py --list` prints candidate XMLs. The helper captures
at 80 columns under a pty with `script -qec`, and redacts the preview harness
framing. When the kernel allows it, the helper runs inside a fake-home
namespace. The repo is then mounted at the persona's clone location and the
XML staged in their home, so paths in the captures read `/home/user/…`.
Reviewers then see an authentic user's world with no path disclosures.

The files, in its `--out-dir`:

- `cap_ee_full.txt`: one full **real** run, whose sandboxed writes vanish
  with the namespace. Reviewers see the real closing, which is what most
  actual users read, so no flag disclosure is needed.
- `cap_pw_full.txt`: the wrapper with `--no-activate`, since a real run
  restarts PipeWire. It writes real confs into the fake home and prints
  genuine to-finish steps. The one remaining disclosure is the skipped
  activation. Its reader chose it to avoid EasyEffects, so findings do not
  transfer between entry points.
- If the helper prints a sandbox-unavailable note, or you pass
  `--no-sandbox`, it fell back to `--dry-run` against real paths. Then
  disclose both the flag and that paths show the harness machine, under the
  §2 exception.
- `slice_ee_tail26.txt`: the EE run's last terminal screen, for reviewer A.
- `slice_preview_blocks.txt`: per-message coverage. `preview_output.py`
  finds a corpus XML for each finding pattern and prints the resulting
  closing block. It drives `dolby_to_easyeffects.py` only, so nothing but
  the wrapper's full run covers the wrapper.
- `slice_doctor_blocks.txt`: the same idea for `--doctor`, from
  `tools/preview_doctor.py`, with one whole report per scenario. What a
  diagnostic says is decided by the machine it runs on, and the states worth
  reviewing are the ones this laptop can't be in. Scenarios stub the probes
  only. The checks, wording, summary and verdict are the shipped ones, so a
  new doctor check earns a scenario there rather than a hand-made sample.
  `tools/preview_doctor.py --list` prints the scenarios. These blocks are
  captured **outside** the sandbox on purpose: the fake home has no
  EasyEffects install, so a sandboxed report would review "nothing is set up
  here" instead of the checks. `doctor.tilde` and `doctor.no_bt_address`
  redact paths and Bluetooth identifiers on the way out. The blocks still
  carry the capture machine's own hardware inventory: DMI product, kernel,
  sound cards, attached USB devices. That inventory is the maintainer's, and
  it is not what a reviewer is being asked about. It stays in gitignored
  `localresearch/`, so don't paste these blocks anywhere else.
- `<name>.color.txt`: each capture with the terminal's colors kept as
  `⟦color⟧…⟦/⟧` markers, such as `⟦yellow⟧`, `⟦faint⟧` or `⟦bold-cyan⟧`.
  The markers name what the screen shows, never what we mean by it. The
  meaning would be comprehension granted (§2), and the color choices
  themselves are something reviewers can fault. Reviewers A and C read
  these, and reviewer B reads plain as the color-blind control. Plain files
  are the verbatim-quoting source.
- `meta.txt`: which pattern each block came from, and the patterns with no
  corpus match. It is orchestrator-only (§2).

Never hand-write or paraphrase samples. Reviewers must see exactly what a
user sees, wrapping included. To capture something the helper doesn't cover:

- set `COLUMNS=80`;
- wrap the command in `script -qec "…" /dev/null`, because piping reorders
  stdout against stderr and a terminal does not;
- run `dolby_to_pipewire.py` only with `--dry-run` or `--no-activate`;
- point `--output-dir` at scratch only with `--no-activate`. Under
  `--dry-run` keep the defaults, so the printed paths are the ones a user
  sees.

Do not read the captures into your own context. The helper's summary is the
validity check: exit code, line count and last line per file. If in doubt,
tail at most ~30 lines. Read capture lines during triage only, and only the
lines a claim is about.

## 2. Give the reviewer the output and nothing else

This rule makes the result worth anything, and it is the easiest to break.

Do not name the sections, say which lines print mid-run versus at the end,
explain that a bracketed tag is a handle for reports, or define any term the
output uses. A user has none of that. Every hint is comprehension granted
rather than measured, and it turns a finding into a pass.

If the reviewer has to work out what a `[tag]` is, that *is* the finding.
`meta.txt` exists so *you* never have to guess which block is which. It
never reaches a reviewer.

Exception: name flags the harness passed that a user would not, such as the
`--dry-run` forced by the capture, so reviewers don't report those as faults.

## 3. Dispatch the reviewers

Dispatch three reviewers in parallel, one slice each, with the prompts below
verbatim. Fill in absolute paths and `<N>`, the block count from the
helper's summary or meta.txt. A fourth, reviewer D, joins them for a round
that touched `--doctor`. Run them at `model: sonnet`: a less capable reader
is a more faithful proxy for a first-time user, and far cheaper. Revert to
the session model only if finding quality drops. Fixes come later, one at a
time.

Reviewer A gets the last screen before the scrollback. That order is how
"the success line scrolled off the top and the last screen looked like a
failure" surfaces, which reading top-to-bottom hides. Whether they would
scroll at all is itself a finding.

Keep the prompts' list of unknown terms in sync with the jargon the output
actually uses. Without that list the model supplies the expertise itself and
reports that everything is clear.

### Shared blocks

Each prompt below starts with PERSONA and ends with FORMAT, verbatim:

PERSONA (EE variant, for reviewers A and C):

```
ROLE-PLAY. You are NOT a developer on this project. Stay in character, and do
not read any source code. Do not open any file other than the capture file(s)
named below. Do not run any commands other than reading those files.

You own a Linux laptop and wanted better speaker sound. You found
speaker-tuning-to-easyeffects on GitHub, cloned it, and ran
`python3 dolby_to_easyeffects.py` with the path to a tuning file the README
helped you find on your Windows partition (you copied it into your home
folder first). You can use a terminal, copy-paste commands, and file a
GitHub issue. You are NOT an audio engineer. You have never heard of Dolby
DAX3, "the regulator", "volmax", "IEQ", "audio optimizer", "PEQ", "MBC",
"smart amp", or "volume leveler". Any other signal-processing jargon (FIR,
Nyquist, crossover, biquad, high-shelf) is equally unknown to you. You just
want your laptop to sound good.
```

Sandboxed captures need no dry-run note for reviewer A, because the run is
real. On a fallback capture, re-add the old disclosure: "--dry-run was
forced by our capture tooling; judge the wording, not the flag." Reviewer C
always gets the preview-blocks disclosure in its own body below.

COLOR NOTE. Append it to PERSONA for reviewers A and C, whose files are the
`.color.txt` variants:

```
Color note: your terminal shows colors, and the capture preserves them as
markers — text between ⟦yellow⟧ and ⟦/⟧ is yellow on screen, ⟦faint⟧ text
is dimmed, ⟦bold-cyan⟧ / ⟦bold-magenta⟧ / ⟦bold-red⟧ are bold in that
color, ⟦green⟧ is green, unmarked text is the normal color. Read the screen
the way your eyes would — including whether the colors themselves help you
or steer you wrong; that is fair game for findings. The ⟦…⟧ markers are our
capture notation, not program output: never report them as faults, and
strip them when quoting THE LINE.
```

PERSONA (wrapper variant, for reviewer B): the same, with the second
paragraph replaced by:

```
You own a Linux laptop and wanted better speaker sound. You found
speaker-tuning-to-easyeffects on GitHub, cloned it, and ran
`python3 dolby_to_pipewire.py` with the path to a tuning file the README
helped you find on your Windows partition (you copied it into your home
folder first). You deliberately chose this script
instead of the EasyEffects one because you do NOT want to install EasyEffects
— you just use plain PipeWire like every modern Linux distro ships. You can
use a terminal, copy-paste commands, and file a GitHub issue. You are NOT an
audio engineer. You have never heard of Dolby DAX3, "the regulator",
"volmax", "IEQ", "audio optimizer", "PEQ", "MBC", "smart amp", or "volume
leveler". Any other signal-processing jargon (FIR, Nyquist, crossover,
biquad, high-shelf, filter-chain internals) is equally unknown to you. You
just want your laptop to sound good.

Harness note (do not report this as a fault): the capture passed
--no-activate, so the final PipeWire restart was skipped — judge whether
the skipped-activation wording is clear, not that it was skipped. (On a
fallback capture the flag is --dry-run instead; disclose that.)
```

FORMAT, for all reviewers:

```
REPORT FORMAT:
First, one line: would you keep using this tool, and would you file the
report it asks for?
Then ONE list of findings, numbered 1..N, worst first, no grouping. For each:

SEVERITY: CRITICAL | HIGH | MEDIUM | LOW
THE LINE: <quoted verbatim>
WHAT'S WRONG: one or two sentences, from your point of view as the user
THE FIX: your rewrite, one sentence

- CRITICAL — misleads, contradicts itself, or asks the impossible
- HIGH — can't act on it, or would act wrongly
- MEDIUM — gets there eventually at a cost, or would skip it
- LOW — wording nit

After the list: at most five bullets on what you expected to be a problem
and isn't.

Be harsh but fair. No praise. Do not propose code changes. Do not invent
filler findings — say so if a severity level is empty.
```

### Reviewer A: terminal simulation, EasyEffects run

PERSONA (EE), then:

```
THIS IS A TWO-STEP EXERCISE. Follow the order strictly.

STEP 1 — the screen. Your terminal window shows 26 lines. After the run
finished, this is what is on your screen — everything earlier has scrolled
off the top. Read ONLY this file first:
<abs path>/slice_ee_tail26.color.txt

Answer, in character, before reading anything else:
a) What just happened — did it work?
b) What would you do next, concretely?
c) Would you bother scrolling up? Why or why not?

STEP 2 — the scrollback. Now you scroll up and read the whole run:
<abs path>/cap_ee_full.color.txt

Judge as the user: do you understand it, do you know what to do, would you
bother.
```

then FORMAT, with this line inserted after its first line: "Then your STEP 1
answers (a/b/c) verbatim."

### Reviewer B: PipeWire wrapper run, top to bottom

PERSONA (wrapper), then:

```
Read the whole run top to bottom:
<abs path>/cap_pw_full.txt

Judge as the user: do you understand it, do you know what to do, would you
bother. Pay attention to whether the output ever talks to you as if you were
an EasyEffects user — you are not, you picked this script to avoid that.
```

then FORMAT.

### Reviewer D: the `--doctor` scenario reports

Dispatch it only when the round changed `--doctor` copy. PERSONA (EE) plus
the COLOR NOTE, then:

```
Some time after your first run, you ran a second command the README mentions
for when something seems wrong. The file below contains <N> of its reports,
separated by "===== DIAGNOSTIC REPORT #N =====" lines our tooling inserted
(don't report those separator lines as faults). What this command prints
depends on the laptop and how it is set up, and these were captured on one
laptop set up <N> different ways. For each report in turn, imagine YOUR
laptop is that one and this is what you are looking at.

Harness note (do not report these as faults): because it is one machine, the
hardware sections are identical in every report — only the parts that describe
how it is set up differ, and those are what to judge.

Read:
<abs path>/slice_doctor_blocks.color.txt

Judge as the user, for each report: do you understand what it is telling you,
do you know whether anything is wrong, do you know what to do about it, would
you bother. Also compare across the reports — if two reports say nearly the
same thing in different words, or contradict each other about the same thing,
that's a finding.
```

then FORMAT, with "no grouping" extended to "no grouping (note which
DIAGNOSTIC REPORT # each came from)".

Name no check, no status tag and no scenario. Which state each report is in
is exactly what the reviewer is measuring, and `meta.txt` holds the map.

### Reviewer C: the per-pattern closing blocks

PERSONA (EE), then:

```
The file below contains <N> run endings, separated by
"===== RUN ENDING #N =====" lines our tooling inserted (don't report those
separator lines as faults). The tool prints a different ending depending on
the laptop model, and these were captured on <N> different laptops. For each
ending in turn, imagine YOUR laptop is that one and this is the end of YOUR
run.

Harness note (do not report this as a fault): these endings were captured
with `--dry-run` forced by our tooling, so they show the dry-run closing —
a real first run would not pass that flag. Judge whether the dry-run
wording itself is clear, but not the fact that it was a dry run.

Read:
<abs path>/slice_preview_blocks.color.txt

Judge as the user, for each ending: do you understand it, do you know what
to do, would you bother. Also compare across endings — if two endings say
nearly the same thing in different words, or contradict each other about the
same feature, that's a finding.
```

then FORMAT, with "no grouping" extended to "no grouping (note which RUN
ENDING # each came from)".

## 4. Triage before fixing

Reviewer output is evidence, not instruction.

- Verify every claim against the code and a real run. Some are misreadings,
  and several have been genuine bugs the suite passed straight over.
- Drop harness artifacts: anything caused by flags or scratch paths the
  capture used, which a real user won't hit. Say which you dropped and why.
- Weight agreement: two reviewers reaching the same conclusion independently
  has been the strongest signal available.
- Check salience claims, such as "buried" or "I'd miss this", against the
  ⟦color⟧ markers before accepting them. They are only trustworthy from the
  color-aware reviewers. The plain-control reviewer is effectively color-blind,
  and round 1's all-plain captures overstated burying for exactly that reason.
- **Say what makes the replacement sentence true, before adopting it.** A fix is
  not done until you can name the code path, doc section or measurement it rests
  on, because a false sentence can satisfy a reviewer's "I understood it"
  perfectly. Plain language is the goal. Only rewording past the evidence is
  the regression. Dropping a qualifier is the usual way this goes wrong:
  `.claude/rules/claims.md` "What each claim rests on". A whole-range sweep is
  the **/copy-audit** skill.

Report the ranked, triaged list and let the user choose what to fix. The
list is normally longer than the change they want. Every finding keeps a
severity label: the reviewer's, or yours where triage moved it. Rank order
alone hides how bad the top is and how ignorable the tail is. The report
also names the patterns meta.txt lists as having no corpus match. Those
messages went unreviewed this round, and silence would read as coverage.
