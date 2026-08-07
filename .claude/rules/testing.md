---
paths:
  - "tests/**/*.py"
  - "tests/golden_preset_baseline.json"
  - "pyproject.toml"
---

# The two guarded tiers: the golden digest, and the corpus

The fast tier (`pytest tests/`) is ordinary unit testing and needs no rule.
These two do, because both have a failure mode that looks like success.

## `tests/test_golden_preset.py` — every emitted parameter, by digest

It pins the whole preset on synthetic input, so **a moved digest means the
output changed**. Nothing else in the fast tier notices a 0.5 dB shift in one
band of one profile.

- If you meant it: re-record with `ATMOS_UPDATE_GOLDEN=1` and commit the
  baseline diff **in the same commit as its cause**, so review reads the
  parameter change next to the code that made it. A baseline diff arriving
  alone, or a commit later, is unreviewable.
- If you did not mean it: the digest is telling you a refactor was not the
  pure move you thought it was. Re-recording makes it green and throws away
  the only signal that said so.
- So never set `ATMOS_UPDATE_GOLDEN=1` speculatively, and never on a run whose
  purpose was to check something else.

## `tests/corpus/` — the real DAX3 XMLs

Runs the full pipeline against every XML it auto-discovers (NTFS mounts plus
the CWD); `ATMOS_CORPUS_DIR` overrides the search. It **skips cleanly** when
no corpus is reachable — which is what makes it safe to leave in the default
run, and also what makes a green suite on a machine without one no evidence
about real devices at all. Check for `s` in the summary before believing it.

- The heavy walks — every endpoint×profile×curve, and `ee_to_pipewire`'s
  `lv2info` conf validation — are marked `slow` and **skipped unless
  `--run-slow`** (or `ATMOS_RUN_SLOW=1`). The default walk visits one
  combination per XML, so it proves "no XML crashes it", not "every profile
  is right".
- `tests/test_ee_to_pipewire.py` is not a corpus test: its structural
  invariants run in the fast tier and stay there.
- Every run is `-n auto` via `pyproject.toml`. Pass `-n 0` to force serial
  when a failure's output is interleaved, or when a test needs a stable
  ordering to reproduce.
