---
name: claude-md-audit
context: fork
agent: general-purpose
model: opus
description: >-
  Audits the instruction files (CLAUDE.md, .claude/rules, .claude/skills) for
  accuracy and bloat against .claude/rules/instructions.md, and proposes a
  concrete edit list. Runs before committing a change to any of them, and
  periodically as a maintenance pass. It verifies every reference still
  resolves, flags references to gitignored file paths, checks line and
  description budgets, finds duplication across CLAUDE.md / rules / skills /
  docs / memory, and scans recent session transcripts and memory for durable
  rules worth promoting or rules now stale. Use when the user asks to "audit /
  clean up / prune CLAUDE.md" or the rules or skills, or before committing one.
---

# claude-md-audit

CLAUDE.md loads every session, so it bloats over time: each session *adds* a rule
or a trap, nothing *removes*. Rules and skills drift the same way. This audit is
the forcing function. It produces a punch list of concrete add / remove /
relocate edits — **present them to the user; do not auto-apply.**

Scope: `CLAUDE.md` (and its house-rules header comment, which states the line
budget), `.claude/rules/*.md`, `.claude/skills/*/SKILL.md` and
`tools/measure_dax/CLAUDE_WINDOWS.md`. With an argument naming files, audit
only those. The standard every line is judged against is
`.claude/rules/instructions.md`; read it first. Report findings grouped by
check, each with the file, the specific line(s) and a proposed fix.

## 1. Reference accuracy

Every file path, test name, script, `docs/` link, commit hash, function name,
env var, and CLI flag named in an audited file must still resolve.
`tests/test_doc_refs.py` already checks doc links, anchors, Finding/§/entry
numbers and quoted headings, and `tests/test_layout.py` checks `tools/` paths;
run both and verify the rest by hand:
- paths/files/dirs exist (`ls`, `test -e`);
- function/symbol names exist in the scripts or `lib/` (`rg`);
- commit hashes resolve (`git cat-file -t <hash>`);
- CLI flags appear in the argparse setup.

Flag each stale reference with what it should point to now.

## 2. No gitignored-path references

An audited file must not point at specific files under gitignored trees (they
rot on clone). Check `.gitignore`, then grep the audited files for paths under
each ignored tree (`localresearch/`, `driver-cache/`, `*.xml`, …).

Distinguish two cases — only the first is a violation:
- **Violation:** a path to a *specific* gitignored file, e.g.
  `localresearch/measure_ee/RESULTS.txt`. Propose stating the lesson directly or
  relocating the content to a tracked doc.
- **Allowed:** stating the *convention* itself, e.g. "Artifacts →
  `./localresearch/<area>/`" or "Never reference `localresearch/` paths". Those
  are the rule text and must stay.

## 3. Bloat

Report CLAUDE.md's **loaded** line count vs the budget in the house-rules header
(currently ~120). Count only what enters context: exclude the leading
block-level HTML comment (`<!-- … -->`), which Claude Code strips before
injection. If over, flag the longest prose passages and propose moving rationale
to `docs/` or a multi-step procedure to a skill — CLAUDE.md should hold the rule,
not the explanation. Check each skill against the size and description limits
in instructions.md, and apply its per-line test to every rule and skill line.

## 4. Duplication

Find content repeated across CLAUDE.md, `.claude/rules/`, `docs/`, the
auto-memory files, and `.claude/skills/`. Each fact should have one home, as
instructions.md rule 5 assigns them: the *rule* in CLAUDE.md or a path-scoped
rule, the *rationale/evidence* in `docs/`, the *procedure* in a skill.
Recommend collapsing duplicates to a pointer. A pointer between two rules whose
`paths:` always load together is itself redundant.

## 5. Friction scan — what to promote

Skim recent session transcripts under
`~/.claude/projects/-home-antoine-stuff-atmos/` and the memory files in
`…/memory/` for durable rules the user has stated more than once that are NOT yet
in CLAUDE.md or a rule. Look for correction markers ("no", "don't", "again", "I told you",
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
all edits are applied. Hand it to the user to approve before editing any
audited file.
