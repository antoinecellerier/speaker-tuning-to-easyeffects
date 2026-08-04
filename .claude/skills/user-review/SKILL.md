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

One command produces every reviewer file — captured at 80 columns under a
pty (`script -qec`), preview harness framing redacted, and run inside a
fake-home namespace when the kernel allows it: paths in the captures then
read `/home/user/…` (the repo mounted at the persona's clone location, the
XML staged in their home), so reviewers see an authentic user's world with
no path disclosures:

    tools/user_review_capture.py <corpus-xml>

(`tools/preview_output.py --list` prints candidate XMLs.) The files, in its
`--out-dir`:

- `cap_ee_full.txt` — one full **real** run (sandboxed writes vanish with
  the namespace): reviewers see the real closing, which is what most actual
  users read. No flag disclosure needed.
- `cap_pw_full.txt` — the wrapper with `--no-activate` (a real run restarts
  PipeWire): real confs into the fake home, genuine to-finish steps; the
  one remaining disclosure is the skipped activation. Its reader chose it
  to avoid EasyEffects — findings do not transfer between entry points.
- If the helper prints a sandbox-unavailable note (or you pass
  `--no-sandbox`), it fell back to `--dry-run` against real paths — then
  disclose both the flag and that paths show the harness machine (§2
  exception), as earlier rounds did.
- `slice_ee_tail26.txt` — the EE run's last terminal screen, for reviewer A.
- `slice_preview_blocks.txt` — per-message coverage: `preview_output.py`
  finds a corpus XML for each finding pattern and prints the resulting
  closing block. It drives `dolby_to_easyeffects.py` only; nothing but its
  full run covers the wrapper.
- `<name>.color.txt` — each capture with the terminal's colors kept as
  `⟦color⟧…⟦/⟧` markers naming what the screen shows (`⟦yellow⟧`, `⟦faint⟧`,
  `⟦bold-cyan⟧`…), never what we mean by it — the meaning would be
  comprehension granted (§2), and the color choices themselves are something
  reviewers can fault. Reviewers A and C read these; reviewer B reads plain
  as the color-blind control. Plain files are the verbatim-quoting source.
- `meta.txt` — orchestrator-only (§2): which pattern each block came from,
  and the patterns with no corpus match.

Never hand-write or paraphrase samples — reviewers must see exactly what a
user sees, wrapping included. If you capture something the helper doesn't
cover: `COLUMNS=80`, wrap in `script -qec "…" /dev/null` (piping reorders
stdout against stderr, which a terminal does not), run `dolby_to_pipewire.py`
only with `--dry-run` or `--no-activate`, and point `--output-dir` at scratch
only with `--no-activate` — under `--dry-run` keep the defaults, so the
printed paths are the ones a user sees.

Do not read the captures into your own context. The helper's summary (exit
code, line count and last line per file) is the validity check — at most tail
~30 lines if in doubt. Read capture lines during triage only, and only the
lines a claim is about.

## 2. Give the reviewer the output and nothing else

This rule makes the result worth anything, and it is the easiest to break.

Do not name the sections, say which lines print mid-run versus at the end,
explain that a bracketed tag is a handle for reports, or define any term the
output uses. A user has none of that. Every hint is comprehension granted
rather than measured, and it turns a finding into a pass.

If the reviewer has to work out what a `[tag]` is, that *is* the finding.
`meta.txt` exists so *you* never have to guess which block is which; it never
reaches a reviewer.

Exception: name flags the harness passed that a user would not (`--dry-run`
forced by the capture), so they don't report those as faults.

## 3. Dispatch three reviewers

Three reviewers in parallel, one slice each, prompts below verbatim — fill in
absolute paths and `<N>` (the block count, from the helper's summary or
meta.txt). Run them at `model: sonnet`: a less capable reader is a more
faithful proxy for a first-time user, and far cheaper; revert to the session
model only if finding quality drops. Fixes come later, one at a time.

Reviewer A gets the last screen before the scrollback. That order is how "the
success line scrolled off the top and the last screen looked like a failure"
surfaces — reading top-to-bottom hides it, and whether they would scroll at
all is itself a finding.

The prompts spell out the unknown terms; without that list the model supplies
the expertise itself and reports that everything is clear. Keep that list in
sync with the jargon the output actually uses.

### Shared blocks

Each prompt below starts with PERSONA and ends with FORMAT, verbatim:

PERSONA (EE variant — reviewers A and C):

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

(Sandboxed captures need no dry-run note for reviewer A — the run is real.
On a fallback capture, re-add the old disclosure: "--dry-run was forced by
our capture tooling; judge the wording, not the flag." Reviewer C always
gets the preview-blocks disclosure in its own body below.)

COLOR NOTE (append to PERSONA for reviewers A and C, whose files are the
`.color.txt` variants):

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

PERSONA (wrapper variant — reviewer B): same, with the second paragraph
replaced by:

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

FORMAT (all reviewers):

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

### Reviewer A — terminal simulation, EasyEffects run

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

### Reviewer B — PipeWire wrapper run, top to bottom

PERSONA (wrapper), then:

```
Read the whole run top to bottom:
<abs path>/cap_pw_full.txt

Judge as the user: do you understand it, do you know what to do, would you
bother. Pay attention to whether the output ever talks to you as if you were
an EasyEffects user — you are not, you picked this script to avoid that.
```

then FORMAT.

### Reviewer C — the per-pattern closing blocks

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

- Verify every claim against the code and a real run. Some are misreadings;
  several have been genuine bugs the suite passed straight over.
- Drop harness artifacts — anything caused by flags or scratch paths the
  capture used, which a real user won't hit. Say which you dropped and why.
- Weight agreement: two reviewers reaching the same conclusion independently
  has been the strongest signal available.
- Salience claims ("buried", "I'd miss this") are only trustworthy from the
  color-aware reviewers — check them against the ⟦color⟧ markers before
  accepting; the plain-control reviewer is effectively color-blind, and
  round 1 (all-plain captures) overstated burying for exactly that reason.
- **Say what makes the replacement sentence true, before adopting it.** The
  bullets above verify the reviewer's *claim*; this verifies your *fix*. A
  reviewer optimises for "I understood it", which a false sentence can
  satisfy perfectly — so a fix is not done until you can name the code path,
  doc section or measurement it rests on. Dropping a qualifier is the usual
  way this goes wrong: round 8 removed the limiter noun from the
  untamed-boost warnings for good reasons and left "nothing limits it on its
  way out", which `limiter#0` falsifies on every preset. Rewording *toward*
  plain language is the design goal; rewording *past* the evidence is a
  regression that no reviewer in this loop is positioned to catch. The
  claim-type checklist is in `.claude/rules/user-messages.md`; a whole-range
  sweep is the **/copy-audit** skill.

Report the ranked, triaged list and let the user choose what to fix. Every
finding keeps a severity label (the reviewer's, or yours where triage moved
it) — rank order alone hides how bad the top is and how ignorable the tail
is, and round 1's report dropped them. The list is normally longer than the
change they want. The report also names the patterns meta.txt lists as
having no corpus match — those messages went unreviewed this round, and
silence would read as coverage.
