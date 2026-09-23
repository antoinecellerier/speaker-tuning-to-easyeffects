---
paths:
  - "tests/**/*.py"
  - "tests/golden_preset_baseline.json"
  - "pyproject.toml"
  - "lib/tool_env.py"
  - "lib/host.py"
---

# Tests that can pass while checking nothing

The fast tier is `pytest tests/`, and it is mostly ordinary unit testing.
Three things in it are not: the golden digest, the corpus tier and the
machine itself. Each has a failure mode that looks like success.

## The golden digest

`tests/test_golden_preset.py` pins every emitted parameter of the whole
preset by digest, on synthetic input. A moved digest means **the output
changed**. Nothing else in the fast tier notices a 0.5 dB shift in one band
of one profile.

- If you meant the change, re-record with `ATMOS_UPDATE_GOLDEN=1`. Commit
  the baseline diff in the same commit as its cause, so review reads the
  parameter change next to the code that made it. A baseline diff that
  arrives alone, or a commit later, is unreviewable.
- If you did not mean it, the digest is telling you a refactor was not the
  pure move you thought it was. Re-recording makes it green and throws away
  the only signal that said so.
- Never set `ATMOS_UPDATE_GOLDEN=1` speculatively, and never on a run whose
  purpose was to check something else.

## The corpus tier

`tests/corpus/` runs the full pipeline against every distinct real DAX3 XML
it auto-discovers. It searches NTFS mounts and the CWD. `ATMOS_CORPUS_DIR`
overrides the search. Byte-identical copies are walked once, so its item
count is not the corpus file count.

Check for `s` in the summary before believing a green run. The tier **skips
cleanly** when no corpus is reachable. That makes it safe to leave in the
default run. It also makes a green suite on a machine without a corpus no
evidence about real devices at all.

- The heavy walks are marked `slow` and skipped unless you pass `--run-slow`
  or set `ATMOS_RUN_SLOW=1`. They are the walk over every
  endpoint×profile×curve and `ee_to_pipewire`'s `lv2info` conf validation.
  The default walk visits one combination per XML, so it proves "no XML
  crashes it", not "every profile is right".
- `tests/test_ee_to_pipewire.py` is not a corpus test. Its structural
  invariants run in the fast tier and stay there.
- Pass `-n 0` to force serial when a failure's output is interleaved, or
  when a test needs a stable ordering to reproduce. `pyproject.toml` makes
  every run `-n auto`.

## Git worktrees

A refactor verified only in a git worktree is **not** verified against the
corpus. When you verify in a worktree:

- pass `ATMOS_CORPUS_DIR=<main-checkout>/localresearch`;
- take digests only from a run whose stderr you saw;
- baseline test counts in the same checkout you compare them against.

`localresearch/` is gitignored, so `git worktree add` gives a checkout
without it. Every corpus-fed check then degrades silently:

- `preview_output.py` exits 1. The obvious render diff pipes it as
  `2>/dev/null | md5sum`, which prints the empty string's digest,
  `d41d8cd98f00…`. It reads as a real result either way.
- `tests/corpus/` skips, and the fast tier drops with it.
  `pytest tests/ --collect-only` shows it: a worktree collects well under half
  the tests the main checkout does.

## The machine

`tests/conftest.py` switches the machine off for every test. It uses two
environment variables, which child processes inherit:

- `ATMOS_NO_LIVE_TOOLS` makes `lib/tool_env.py` report every tool
  uninstalled. That is CI's state. The module is the only way `lib/` and the
  entry scripts reach a tool, and `tests/test_layout.py` enforces that.
- `ATMOS_HOST_ROOT` makes `lib/host.py` re-root every `/proc`, `/sys`,
  `/etc` and `/lib/firmware` read under an empty directory. A test then sees
  no sound hardware, no DMI and no distro. Besides removing the machine
  dependence, this keeps out real HDA codec reads. Those serialise in the
  kernel, and once cost the fast tier three quarters of its run. A test that
  opens a real host location in-process fails, naming the path.

### Faking the machine

- Tools: patch `tool_env.run` or `tool_env.which`, or a narrower seam. For a
  child process, put an executable of the tool's name in
  `ATMOS_FAKE_TOOLS_DIR`. A PATH shim is never reached.
- Host files: pass the function its own tree, since most functions take a
  root parameter. You can also patch the module's path constant, or use the
  `fake_host` fixture to place a file at a host path. Place `/etc/os-release`
  this way for a distro-dependent message, which otherwise has no distro to
  name.
- Either way, make the case assert the branch the fake exists for. With the
  machine off, a run whose fake isn't reached **still passes**, down the
  "not installed" / "no hardware" branch.
- A test that exists to exercise the real machine takes
  `@pytest.mark.live_machine`, and skips where it can't run.

### Live checks

A new tool or host location in `lib/` needs a check in
`tests/test_live_machine.py` or a reasoned exemption. Until then its guard
fails: `test_every_tool_lib_runs_has_a_live_check` or
`test_every_host_location_lib_reads_has_a_live_check`. The checks are there
because **fakes drift silently**. The file reads each tool and each host
location `lib/` parses for real, through the function that parses it. It
asserts the parse rather than the values.

On a dev machine, read that file's skips. Each is something with no drift
check on this machine.
