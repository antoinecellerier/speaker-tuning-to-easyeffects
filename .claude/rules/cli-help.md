---
paths:
  - "dolby_to_easyeffects.py"
  - "dolby_to_pipewire.py"
  - "ee_to_pipewire.py"
  - "lib/**/*.py"
  - "docs/dolby-to-easyeffects.md"
  - "docs/dolby-to-pipewire.md"
  - "docs/filters.md"
---

# CLI flags: argparse and the docs list are mirrors

A script has exactly two full flag listings. Argparse is the source of truth for
names, defaults, choices, groups and order. The options list in the script's
docs page mirrors its group labels and order, one bullet per flag:
`docs/dolby-to-easyeffects.md`, and `docs/dolby-to-pipewire.md` for both
PipeWire scripts. Touch either side and update the other in the same commit: new
flag, rename, regrouping, default/choices change, or help wording that changes a
documented claim.

- `tests/test_docs_cli_sync.py` traps name/order/group-label drift and
  runs in the fast tier. It cannot see stale *claims*, so after edits diff
  `<script> --no-color --help` against the docs bullets by hand.
- Extend the trap's `*_DOC_OMITS` sets only for a deliberate docs
  omission, like a measurement-only flag such as `--target-object`, never to
  quiet a failure.
- Group titles are the user journey: tuning input → inspection → …;
  routing → output → …. Put a new flag in the group it serves, where it
  fits that journey. Appending it at the end re-starts the drift.
- The `--disable` / `--enable` names are mirrored too: one `docs/filters.md`
  table row each, in argparse order, and the "Valid names:" clause of their
  `docs/dolby-to-easyeffects.md` bullets. `tests/test_docs_cli_sync.py` traps
  both, so a new name needs its symptom row.
- Other docs, like reference.md and ee-to-pipewire.md, mention flags in
  prose. Those aren't mirrored listings and carry no sync guarantee.
