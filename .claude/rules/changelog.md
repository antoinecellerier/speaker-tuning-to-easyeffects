---
paths:
  - "CHANGELOG.md"
---

# Editing CHANGELOG.md

Add an entry only for a meaningful user-facing functional change or an
in-depth research conclusion worth surfacing. User-facing changes are:

- audible output;
- a new/changed flag or feature;
- a user-facing bug fix;
- new-device support: new detection/handling code, or a newly
  reported/confirmed device added to the tested list, which gets a short
  `### Added` rollup, e.g. `Mark additional tested devices: …`.

Skip everything else: refactors, tests, research-log notes and plain docs
edits, including other supported-devices-table wording tweaks.

New entries and any edits, wording cleanups and retrofits included, go under
`## Unreleased` only, in the matching `### Changed/Added/Fixed/Docs` section.
Never reword the released `## vYYYY.MM` sections. They are frozen release
history, because their text shipped in a GitHub Release.

Word entries per the spec in the comment at the top of `CHANGELOG.md`. It is
objective: follow it literally, don't approximate. In short:

- Shape: WHAT changed for the user, then, each optional: one clause of
  mechanism, a flag or "re-run to regenerate", a link.
- `<= 3` sentences, ~50 words. More than three sentences means the surplus
  belongs in the research log.
- Most-impactful-first within each section.
- `[AUDIBLE]` honesty: claim an impression only if heard on-device.
- Link a change a report prompted, or one whose why the ceiling pushed out
  of the entry. An obvious, unreported change needs none.
- Measurement numbers, device IDs, corpus stats, plugin internals and
  provenance go in the research log or `reference.md` behind the link,
  **not** inline.
- Match the tight version of the comment's worked too-long-vs-tight example.

Don't cut a release unless explicitly asked. It is maintainer-initiated,
the same gate as "Never push without explicit per-push permission". A cut
moves the Unreleased entries into a `## vYYYY.MM` heading, then tags and
pushes, by the steps in the same top comment.

At a cut, draft the tagline and summary per the top comment's "Release
tagline & summary" block, the one edit outside `## Unreleased`. Then stop
for the maintainer to validate both before anything is tagged. They ship as
the release title and its opening line, and /copy-audit never sees them.
