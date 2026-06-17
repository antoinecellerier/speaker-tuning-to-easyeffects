---
paths:
  - "CHANGELOG.md"
---

# Editing CHANGELOG.md

Add an entry for any user-visible change — audible output, a new/changed flag
or feature, a user-facing bug fix, or new-device support. Skip internal-only
churn (refactors, tests, research-log notes). New entries go under
`## Unreleased` only, in the matching `### Changed/Added/Fixed/Docs` section.

Word them per the conventions in the comment at the top of `CHANGELOG.md`:
concise and human-readable, most-impactful-first within each section, and
`[AUDIBLE]` honesty (only claim a listening impression if it was heard
on-device). Link out to the issue/PR and `docs/design-notes.md` for the deep
why instead of explaining it inline.

Cutting a release — moving Unreleased entries into a dated `## vYYYY.MM`
heading, then tagging and pushing — is maintainer-initiated; don't do it unless
explicitly asked (same gate as "Never push without explicit per-push
permission"). The steps are in that same top comment.
