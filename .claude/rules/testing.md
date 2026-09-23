---
paths:
  - "tests/**/*.py"
  - "tests/golden_preset_baseline.json"
  - "pyproject.toml"
  - "lib/tool_env.py"
  - "lib/host.py"
---

# Tests that can pass while checking nothing

The fast tier (`pytest tests/`) is mostly ordinary unit testing. Three things
in it are not, because each has a failure mode that looks like success: the
golden digest, the corpus tier, and the machine itself.

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

Runs the full pipeline against every distinct XML it auto-discovers (NTFS
mounts plus the CWD) — byte-identical copies are walked once, so its item
count is not the corpus file count; `ATMOS_CORPUS_DIR` overrides the search.
It **skips cleanly** when
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

## A git worktree has no corpus — and says so quietly

`localresearch/` is gitignored, so `git worktree add` gives a checkout without
it, and every corpus-fed check degrades silently. `preview_output.py` exits 1;
piped as `2>/dev/null | md5sum` — the obvious render diff — that is the empty
string's digest, `d41d8cd98f00…`, which reads as a real result either way.
`tests/corpus/` skips, and the fast tier drops with it (1260/441 in a worktree
against 4093/6119 in the main checkout, same commit).

So a refactor verified only in a worktree is **not** verified against the
corpus. Pass `ATMOS_CORPUS_DIR=<main-checkout>/localresearch`, take digests
only from a run whose stderr you saw, and baseline test counts in the same
checkout you compare them against.

## The machine: off in every test, one live check each

`tests/conftest.py` switches the machine off for every test, through two
environment variables that child processes inherit:

- `ATMOS_NO_LIVE_TOOLS` — `lib/tool_env.py`, the only way `lib/` and the entry
  scripts reach a tool (`tests/test_layout.py` enforces it), reports every
  tool uninstalled. That is CI's state.
- `ATMOS_HOST_ROOT` — `lib/host.py` re-roots every `/proc`, `/sys`, `/etc`,
  `/lib/firmware` read under an empty directory: no sound hardware, no DMI,
  no distro. Besides the machine dependence, real HDA codec reads serialise
  in the kernel and once cost the fast tier three quarters of its run. A test
  that opens a real host location in-process fails, naming the path.

Faking the machine:

- **Tools:** patch `tool_env.run` / `tool_env.which` (or a narrower seam). For
  a child process, put an executable of the tool's name in
  `ATMOS_FAKE_TOOLS_DIR`; a PATH shim is never reached.
- **Host files:** pass the function its own tree (most take a root
  parameter), patch the module's path constant, or use the `fake_host`
  fixture to place a file at a host path — `/etc/os-release` included, since
  a distro-dependent message otherwise has no distro to name.
- Either way, **make the case assert the branch the fake exists for**: with
  the machine off, a run whose fake isn't reached still passes, down the
  "not installed" / "no hardware" branch.
- **A test that exists to exercise the real machine** takes
  `@pytest.mark.live_machine` and skips where it can't run.

**Fakes drift silently.** `tests/test_live_machine.py` reads each tool and
each host location `lib/` parses for real, through the function that parses
it, asserting the parse rather than the values. A new tool or location in
`lib/` fails its guard (`test_every_tool_lib_runs_has_a_live_check`,
`test_every_host_location_lib_reads_has_a_live_check`) until it has a check
there or a reasoned exemption. On a dev machine, read that file's skips: each
is something with no drift check on this machine.
