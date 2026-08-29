---
paths:
  - "CHANGELOG.md"
---

# Editing CHANGELOG.md

Add an entry only for a meaningful user-facing functional change — audible
output, a new/changed flag or feature, a user-facing bug fix, or new-device
support (new detection/handling code, **or** a newly reported/confirmed
device added to the tested list — a short `### Added` rollup, e.g. `Mark
additional tested devices: …`) — or an in-depth research conclusion worth
surfacing. Skip everything else: refactors, tests, research-log notes, and
plain docs edits (including other supported-devices-table wording tweaks).
New entries — and any edits, including wording cleanup/retrofits — go under
`## Unreleased` only, in the matching `### Changed/Added/Fixed/Docs` section.
The released `## vYYYY.MM` sections are frozen release history: never reword
them (their text shipped in a GitHub Release).

Word them per the spec in the comment at the top of `CHANGELOG.md` — it is
objective; follow it literally, don't approximate. The shape: WHAT changed
(user-facing) → optional one-clause mechanism → optional flag/"re-run to
regenerate" → optional link; `<= 3` sentences / ~50 words; most-impactful-first
within each section; `[AUDIBLE]` honesty (claim an impression only if heard
on-device). The link is for a change a report prompted, or one whose why the
ceiling pushed out of the entry — an obvious, unreported change needs none.
Measurement numbers, device IDs, corpus stats, plugin internals, and provenance
do **not** go inline — they live in `docs/design-notes.md`/`reference.md` behind
the link. The comment carries a worked too-long-vs-tight example; match the
tight one. If an entry needs more than three sentences, that's the signal the
surplus belongs in design-notes, not here.

Cutting a release — moving Unreleased entries into a `## vYYYY.MM` heading,
then tagging and pushing — is maintainer-initiated; don't do it unless
explicitly asked (same gate as "Never push without explicit per-push
permission"). The steps are in that same top comment.

At a cut, draft the tagline and summary per the top comment's "Release
tagline & summary" block — the one edit that sits outside `## Unreleased` —
and stop for the maintainer to validate both before anything is tagged: they
ship as the release title and its opening line, and /copy-audit never sees
them.
