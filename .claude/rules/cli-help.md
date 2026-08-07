---
paths:
  - "dolby_to_easyeffects.py"
  - "dolby_to_pipewire.py"
  - "ee_to_pipewire.py"
  - "lib/**/*.py"
  - "README.md"
---

# CLI flags: argparse and the README list are mirrors

A script's full flag listing lives in exactly two places: its argparse
declarations (source of truth for names, defaults, choices, groups, order)
and the README options list (same group labels, same order, one bullet per
flag). Touch either side — new flag, rename, regrouping, default/choices
change, or help wording that changes a documented claim — and update the
other in the same commit.

- `tests/test_readme_cli_sync.py` traps name/order/group-label drift and
  runs in the fast tier. It cannot see stale *claims* — after edits, diff
  `<script> --no-color --help` against the README bullets by hand.
- Deliberate README omissions (measurement-only flags like
  `--target-object`) live in the trap's `*_README_OMITS` sets. Extend them
  for a conscious omission only, never to quiet a failure.
- Group titles are the user journey (tuning input → inspection → …;
  routing → output → …). A new flag joins the group it serves, positioned
  where it fits that journey — appending at the end re-starts the drift.
- Other docs (reference.md, ee-to-pipewire.md) mention flags in prose;
  those aren't mirrored listings and carry no sync guarantee.
