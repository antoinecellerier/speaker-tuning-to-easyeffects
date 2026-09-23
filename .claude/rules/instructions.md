---
paths:
  - "CLAUDE.md"
  - ".claude/rules/*.md"
  - ".claude/skills/**/*.md"
  - "tools/measure_dax/CLAUDE_WINDOWS.md"
---

# Instruction files

CLAUDE.md, the rules and the skills are read by Claude and by the
maintainer. This file is the standard they are written and audited against.
Its sources are Anthropic's guidance:
<https://code.claude.com/docs/en/memory>,
<https://code.claude.com/docs/en/best-practices>,
<https://code.claude.com/docs/en/skills>, and for skill descriptions
<https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices>.
Their sentences follow docs.md "Sentences"; read it before a rewrite, since
it does not load for CLAUDE.md or `.claude/`.

## What each line holds

1. Keep a line only if removing it would cause a mistake. Cut what Claude
   can read from the code.
2. Write the standing instruction, a one-clause reason, and a pointer to the
   single source of any detail. Past events and one-off exceptions go to git
   history or memory.
3. Say what to do. A prohibition names the alternative.
4. Emphasis (IMPORTANT, YOU MUST) goes on at most one line per file, and
   only on a line Claude has been seen to skip.

## Where a line goes

5. CLAUDE.md holds rules that apply in every session. Rationale goes to
   `docs/`, and a multi-step procedure to a skill. A rule that constrains
   an edit to some files moves to a path-scoped rule. A rule that gates an
   action, or is needed while reading, stays: no `paths:` glob fires before
   a push, a capture, a reply or a read. Counter-examples already rejected:
   docs/code-organisation.md "Which CLAUDE.md rules a granular `lib/` can
   absorb".
6. A rule file scopes itself with `paths:`, the only frontmatter field a
   rule reads. It loads when Claude reads a matching file and is gone after
   compaction until the next such read.
7. A skill description is written in the third person, puts the main use
   first, and stays under 1,024 characters. SKILL.md holds standing
   instructions and stays under 500 lines.

## Before committing

8. Run /claude-md-audit before committing a change to CLAUDE.md, a rule or
   a skill.
