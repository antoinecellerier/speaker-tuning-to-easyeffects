---
paths:
  - "README.md"
  - "CHANGELOG.md"
  - "docs/**/*.md"
  - "tools/**/*.md"
  - ".github/ISSUE_TEMPLATE/*"
  - "dolby_to_easyeffects.py"
  - "ee_to_pipewire.py"
  - "dolby_to_pipewire.py"
  - "lib/**/*.py"
  - "tools/corpus_audit.py"
---

# Claims

Every sentence that states a fact rests on evidence: docs, terminal copy,
comments, issue replies, commit messages. CLAUDE.md "Core invariants" states
the principle; this file is the checklist.

## What each claim rests on

A sentence a first-time reader understands perfectly can still be false.
Before one ships, name what each claim rests on:

- **What the tool does** → the gate that decides it. If the predicate is
  broader or narrower than the sentence, the sentence is wrong: a section
  gated on `if regulator:` describes a stage `--disable regulator` removed,
  and a `<= 0` branch saying "your tuning asks for none" is wrong about
  every negative value.
- **What the audio or Dolby does** → `docs/reference.md` "Validated vs
  unvalidated mappings". Anything on the unvalidated list gets hedged, not
  asserted; anything the docs hold as a *leading hypothesis* is reported as
  what we measured, not as what Dolby intends.
- **How common something is** ("every device", "rare", "usually") → a
  re-derivation date and a figure. Derive it through `resolve_xml_value`,
  never a bare grep: the `preset=` indirection hides values a text search
  reports as absent.
- **What other software does** (PipeWire, WirePlumber, EasyEffects, a shell
  command's output) → a command that reproduces it on a real machine.

**Removing a qualifier is the usual way a true sentence becomes false.**
"Nothing limits it" for "nothing limits it band by band", "the most likely
reason" for "a plausible cause", "sized from this speaker's bass cutoff" for
a constant used on 36 of 39 files — each was a readability win that changed
the truth value. When a qualifier is load-bearing, say so in a comment beside
it, so the next round doesn't trim it back.

## Empirical claims must be reproduced from data

Every empirical number — corpus stats, error/dB figures, decoded coefficient
values, prevalence counts — must be re-derived from data before you assert or
update it. Don't carry a prior figure (or an agent-computed one) forward
unchecked, and don't expand an abbreviation/name you can't verify.

- Regenerate corpus statistics with `tools/corpus_audit.py`; cite it as the
  source. Run one-off cross-cuts as ad-hoc queries over the same corpus.
- When a count changes, re-check the *claim* it supports — a number shift can
  flip a qualitative conclusion.

Carried-over and agent-computed figures, and one fabricated package-name
expansion, are where wrong numbers have shipped.

## Rewording keeps the scope

1. Keep every qualifier, as above. Validated stays validated, a hypothesis
   stays a hypothesis, a one-device result stays scoped to that device, and
   n stays attached. A frequency word ("most", "usually") needs the count
   behind it.
2. Carry every number over exactly. A rewrite asserts no new figure, so it
   re-derives none: a figure that looks stale goes to the maintainer.

## Rewriting existing text

The wording may change; the data and the claims may not. Gate each rewrite:

- `tools/docs/docinv.py diff --counts OLD NEW`: restore each missing or
  rarer token, or list it with a reason for the maintainer to veto. A
  dropped hedge or quantifier shows here too. Add `--py` for comments and
  docstrings.
- `tools/docs/docinv.py diff --counts NEW OLD`: each new token, and each
  quantifier that became more common, traces to an old sentence, or goes.
- Replacing a restatement with a link: run the first check on the removed
  text against the link's target.
- A comment-only change to Python passes
  `tools/docs/check_comment_only.py`, and `tests/test_golden_preset.py` does
  not move.
- Moves and rewording go in separate commits, so each diff can be read.
- A second reader compares old with new for altered claims and lost
  qualifiers before the commit.
