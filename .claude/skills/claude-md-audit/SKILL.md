---
name: claude-md-audit
description: >-
  Audits CLAUDE.md for accuracy and bloat, and proposes a concrete edit list.
  Run this BEFORE committing any change to CLAUDE.md (the file's own house
  rules require it), and periodically as a maintenance pass. It verifies every
  reference still resolves, flags references to gitignored file paths, checks the
  file against its line budget, finds duplication across CLAUDE.md / docs / memory
  / skills, and scans recent session transcripts and memory for durable rules
  worth promoting or rules now stale. Use when the user asks to "audit / clean up
  / prune CLAUDE.md" or before a CLAUDE.md commit.
---

# claude-md-audit

CLAUDE.md loads every session, so it bloats over time: each session *adds* a rule
or a trap, nothing *removes*. This audit is the forcing function. It produces a
punch list of concrete add / remove / relocate edits — **present them to the
user; do not auto-apply.**

Run the checks below against `CLAUDE.md` (and its house-rules header comment,
which states the current line budget). Report findings grouped by check, each
with the specific line(s) and a proposed fix.

## 1. Reference accuracy

Every file path, test name, script, `docs/` link, commit hash, function name,
env var, and CLI flag named in CLAUDE.md must still resolve. Extract them and
verify:
- paths/files/dirs exist (`ls`, `test -e`);
- `docs/*.md` links exist and the referenced sections still exist (`rg '^#'`);
- function/symbol names exist in the scripts (`rg`);
- commit hashes resolve (`git cat-file -t <hash>`);
- CLI flags appear in the argparse setup.

Flag each stale reference with what it should point to now.

## 2. No gitignored-path references

CLAUDE.md must not point at specific files under gitignored trees (they rot on
clone). Check `.gitignore`, then grep CLAUDE.md for paths under each ignored tree
(`localresearch/`, `driver-cache/`, `*.xml`, …).

Distinguish two cases — only the first is a violation:
- **Violation:** a path to a *specific* gitignored file, e.g.
  `localresearch/measure_ee/RESULTS.txt`. Propose stating the lesson directly or
  relocating the content to a tracked doc.
- **Allowed:** stating the *convention* itself, e.g. "Artifacts →
  `./localresearch/<area>/`" or "Never reference `localresearch/` paths". Those
  are the rule text and must stay.

## 3. Bloat

Report the **loaded** line count vs the budget in the house-rules header
(currently ~120). Count only what enters context: exclude the leading
block-level HTML comment (`<!-- … -->`), which Claude Code strips before
injection. If over, flag the longest prose passages and propose moving rationale
to `docs/` or a multi-step procedure to a skill — CLAUDE.md should hold the rule,
not the explanation.

## 4. Duplication

Find content repeated across CLAUDE.md, `docs/`, the auto-memory files, and
`.claude/skills/`. Each fact should have one home: the *rule* in CLAUDE.md, the
*rationale/evidence* in `docs/`, the *procedure* in a skill. Recommend collapsing
duplicates to a pointer.

## 5. Friction scan — what to promote

Skim recent session transcripts under
`~/.claude/projects/-home-antoine-stuff-atmos/` and the memory files in
`…/memory/` for durable rules the user has stated more than once that are NOT yet
in CLAUDE.md. Look for correction markers ("no", "don't", "again", "I told you",
"instead of") and repeated constraints. Propose promotions, with the evidence.
(A rule belongs in CLAUDE.md only if it's a durable project rule not derivable
from the code — not session-specific context.)

## 6. Stale rules

Flag rules that no longer apply: an investigation flag that was reverted, a
feature/file that was removed, a trap that's now structurally impossible. Propose
removal.

## Output

A single punch list: for each finding, the line, the issue, and the exact
proposed edit (add / remove / relocate). End with the projected new line count if
all edits are applied. Hand it to the user to approve before editing CLAUDE.md.
