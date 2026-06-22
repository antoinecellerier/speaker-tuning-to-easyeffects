---
paths:
  - "CHANGELOG.md"
---

# Editing CHANGELOG.md

Add an entry only for a meaningful user-facing functional change — audible
output, a new/changed flag or feature, a user-facing bug fix, or code-level
new-device support (new detection/handling) — or an in-depth research
conclusion worth surfacing. Skip everything else: refactors, tests,
research-log notes, and plain docs edits — including just recording a
confirmed device in the README's supported-devices table, which is a docs
edit, not a functional change. New entries go under `## Unreleased` only, in
the matching `### Changed/Added/Fixed/Docs` section.

Word them per the conventions in the comment at the top of `CHANGELOG.md`:
concise and human-readable, most-impactful-first within each section, and
`[AUDIBLE]` honesty (only claim a listening impression if it was heard
on-device). Link out to the issue/PR and `docs/design-notes.md` for the deep
why instead of explaining it inline.

Cutting a release — moving Unreleased entries into a dated `## vYYYY.MM`
heading, then tagging and pushing — is maintainer-initiated; don't do it unless
explicitly asked (same gate as "Never push without explicit per-push
permission"). The steps are in that same top comment.
