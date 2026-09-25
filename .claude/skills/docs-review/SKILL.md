---
name: docs-review
description: >-
  Reviews the user-facing docs — README.md and the user guides under
  docs/README.md "Using it" — by running them past subagent reviewers
  role-playing fixed personas: a first-time visitor, a user troubleshooting a
  symptom, and a PipeWire-only user. Reports severity-ranked findings after
  triage. Use after changing README or a user guide, before a release, and
  whenever the user asks for a "persona review", to "check the docs read well",
  or whether a newcomer can get from zero to working sound. Complements
  /user-review, which covers the scripts' terminal output. The doc tests trap
  broken links and drift, and this catches a reader who gets lost, stuck or
  misled.
---

# docs-review

`tests/test_doc_refs.py` proves every link lands and the sync tests prove the
option lists match argparse. Neither proves a reader can find the fix for
their symptom, or would not give up at step 3. Only a reader who doesn't
already know the answer finds those.

The personas and scenarios below are fixed on purpose. Re-running the same
ones after each docs change is what shows drift. Change them only when the
product changes: a new entry point, a new common symptom, a dropped route.

Re-run after fixing. A fix to a landing page can strand a reader somewhere
else.

## 1. Give the reviewer the files and nothing else

Each prompt names the entry file and the directory it may follow links into.
Do not summarise the docs, define a term, or say which section answers which
symptom. A user has none of that, and every hint turns a finding into a
pass. The one piece of context is how to read the files: as rendered GitHub
markdown, where `<!-- -->` comments and `<a id>` tags are invisible.

Reviewers are read-only: they read files and run nothing.

## 2. Dispatch the reviewers

Dispatch all three in parallel with the prompts below verbatim, filling in
the absolute repo path for `<REPO>`. Run them at `model: sonnet`: a less
capable reader is a more faithful proxy for a newcomer, and cheaper.

### Shared block (append to every prompt)

```
Treat the files as rendered GitHub markdown: `<!-- -->` comments and `<a id>`
tags are invisible. Follow only links you would actually click, within
<REPO>/README.md and <REPO>/docs/. READ-ONLY: only read files; do not edit or
run anything.

REPORT FORMAT, under 450 words. First, one line: would you keep going with
this tool? Then ONE list, worst first. For each finding: SEVERITY, the quoted
sentence with file:line, what's wrong from your point of view, and your fix in
one sentence.

- CRITICAL — no route forward, a wrong or unsafe instruction, a contradiction,
  or a link whose target doesn't deliver what its text promises
- HIGH — you'd act wrongly, or couldn't act without guessing
- MEDIUM — you'd get there at a cost: a detour, jargon, a buried step
- LOW — polish, including style that differs between pages

Say explicitly if a severity level is empty. No praise, no filler findings.
```

### Reviewer A: first-time visitor

```
Role-play a Linux laptop user who just found this project via a Reddit thread
titled "Finally, my ThinkPad speakers sound like on Windows". You're
comfortable with a terminal, have never used git, and have never heard of
EasyEffects presets, DAX3 or PipeWire filter-chains. Your laptop: a Lenovo
Yoga, Linux only (you wiped Windows), Ubuntu.

Read <REPO>/README.md first, then follow the links you'd click. Cover: the
first screen (what it is, whether it's for you); the exact steps from zero to
working sound, flagging every stuck point; anything that scares you off; and
dangling "below/above" references or naming that differs between pages.
```

### Reviewer B: troubleshooter

```
Role-play a user who installed this tool a week ago with a Flatpak
EasyEffects and now has a problem. Run four scenarios, each from where you'd
arrive:
A. You googled "easyeffects dolby preset no difference" and landed directly on
   <REPO>/docs/troubleshooting.md.
B. From <REPO>/README.md: music sounds "crushed and pumpy on loud parts".
C. From <REPO>/README.md: after switching to Bluetooth headphones they sound
   over-processed.
D. From <REPO>/README.md: you updated to a new release and re-ran the script,
   but hear no change.
For each: the path you took (file → section), hops to an actionable fix, and
whether the fix really addresses the symptom.
```

### Reviewer C: PipeWire-only user

```
Role-play a Fedora user on a Lenovo laptop with its Windows partition mounted,
who runs a minimal desktop and won't install EasyEffects or any GUI audio app.
You want the tuning applied at boot and to forget about it.

Read <REPO>/README.md first, then follow the links you'd click. Cover: whether
you can tell early that a no-EasyEffects route exists; the exact steps to
working sound; how you'd confirm it's active and how you'd remove it; and
anything that tells you to install or run EasyEffects after all.
```

## 3. Triage before reporting

Reviewer output is evidence, not instruction.

- Verify each finding against the docs and the code. Some are misreadings. A
  link complaint is checked by opening the target section.
- Weight agreement: two reviewers reaching the same finding independently is
  the strongest signal.
- Drop findings that ask for content the docs deliberately leave to another
  page, when the link to it is there and says so.
- Before adopting a fix, name the doc section, code path or measurement that
  makes the new sentence true, because no reviewer here checks truth:
  `.claude/rules/claims.md` "What each claim rests on", and for a rewrite its
  "Rewriting existing text" gates.

Report the triaged list, ranked, each finding keeping a severity label (the
reviewer's, or yours where triage moved it), and let the user choose what to
fix. Name what you dropped and why.
