---
paths:
  - "dolby_to_easyeffects.py"
  - "dolby_to_pipewire.py"
  - "ee_to_pipewire.py"
  - "lib/**/*.py"
  - "README.md"
---

# CLI flags: argparse and the README list are mirrors

A script has exactly two full flag listings. Argparse is the source of
truth for names, defaults, choices, groups and order. The README options list
mirrors its group labels and order, one bullet per flag. Touch either side
and update the other in the same commit: new flag, rename, regrouping,
default/choices change, or help wording that changes a documented claim.

- `tests/test_readme_cli_sync.py` traps name/order/group-label drift and
  runs in the fast tier. It cannot see stale *claims*, so after edits diff
  `<script> --no-color --help` against the README bullets by hand.
- Extend the trap's `*_README_OMITS` sets only for a deliberate README
  omission, like a measurement-only flag such as `--target-object`, never to
  quiet a failure.
- Group titles are the user journey: tuning input → inspection → …;
  routing → output → …. Put a new flag in the group it serves, where it
  fits that journey. Appending it at the end re-starts the drift.
- Other docs, like reference.md and ee-to-pipewire.md, mention flags in
  prose. Those aren't mirrored listings and carry no sync guarantee.
