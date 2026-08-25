# tools/ — the scripts that keep everything else correct

Nothing in here ships to a user, and nothing here is part of the conversion.
Each script exists to keep something *else* right: a generated data table in
`lib/data/`, a figure in `docs/`, the release notes, the copy a run prints, or
the converter's own output. They are listed below with what they keep correct
and who runs them, because that — not the filename — is how you find the one
you need.

One near-exception to "nothing here is part of the conversion":
[`measure_pw/validate_conf.py`](measure_pw/validate_conf.py) runs the same
schema check `ee_to_pipewire.py` applies to every conf it writes. It is no
longer a runtime dependency of the converter, though — that check's runtime
core is `lib/pipewire/validate.py`, which the converter calls in process — so
what is left here is a front end you can point at a conf yourself. See "Three
files that look misplaced" below before touching it.

**The capture and comparison scripts touch your audio devices.** They mute
speakers, reroute sinks and swap presets, so they run behind the audio handoff
in CLAUDE.md — use the **/audio-validate** skill rather than invoking them ad
hoc. (`validate_conf.py`, `measure_ee/render_vbe_chain.py` and
`measure_ee/analyze_vbe_chain.py` are the exceptions: file-in/file-out, no
audio and no PipeWire daemon.)

## Root scripts

| script | what it keeps correct | who runs it, and when |
|---|---|---|
| [`_wavio.py`](_wavio.py) | Every WAV read in the measurement tree. `pw-record` writes a `PEAK` chunk that `scipy.io.wavfile` doesn't recognise and warns about on each read; this strips it | Nobody directly — it has no CLI. Nine scripts across `measure_dax/`, `measure_ee/` and `measure_pw/` `sys.path`-insert this directory and `from _wavio import read`. See below for why it lives here |
| [`changelog_section.py`](changelog_section.py) | The GitHub Release — slices `CHANGELOG.md` down to one version's section for the notes, and (`--title`) lifts the heading's tagline into the release title | `.github/workflows/release.yml`, on a pushed `vYYYY.MM` tag. Exits non-zero on a missing section, or on a heading still carrying a date instead of a tagline, so the job fails loudly instead of publishing empty or misnamed notes. Guarded by `tests/test_changelog_section.py`, which also holds every `## v` heading in the real file to the shape — CI runs no tests on a tag push |
| [`check_move_purity.py`](check_move_purity.py) | `git blame -C -C` history across an extraction: it proves a commit is *pure code motion* — every line it adds under `lib/` was already there, byte-for-byte, in a line it removed | You, by hand, against one commit, before pushing an extraction. Wired to nothing; the rule it enforces is in `docs/code-organisation.md`, "Splitting the single-file scripts" |
| [`corpus_audit.py`](corpus_audit.py) | Every cross-device figure in `docs/cross-device-findings.md` and `docs/design-notes.md`. Those numbers are meant to be re-derived from this, never carried forward | You, after pulling new driver packages; and the **/copy-audit** skill, which captures its output as the evidence reviewers check numbers against. Also imported as a library — see below. Guarded by `tests/test_corpus_audit.py` |
| [`extract_claims.py`](extract_claims.py) | The inventory of user-visible strings the **/copy-audit** skill reviews, each tagged with whether a given git range changed it | That skill, at the start of an audit. Guarded by `tests/test_extract_claims.py`, which exists because this tool has twice failed by *shrinking* rather than erroring |
| [`preview_output.py`](preview_output.py) | The end-of-run copy. For each finding a run can raise it locates a corpus XML that actually raises it and prints a real run's tail — every run `--dry-run`, nothing written | You, after changing any message a user reads; and the **/user-review** and **/copy-audit** skills. `--list` says which XML matches what |
| [`render_forced_conditions.py`](render_forced_conditions.py) | The other half of that: the conditional messages *no* corpus device reaches, so `preview_output.py` can find no example. It patches one XML field off its otherwise-universal value and runs the generator on the result | The **/copy-audit** skill, beside `preview_output.py`. A non-zero exit means a condition stopped firing — the patch no longer matches the schema, not that the copy is fine |
| [`update_kernel_releases.py`](update_kernel_releases.py) | `_KERNEL_SERIES_RELEASES` in `lib/data/kernel_releases.py` — the release-month table behind the old-kernel hint | `.github/workflows/kernel-release-table.yml`, weekly, opening a PR per new series. Append-only, and report-only without `--write`. Guarded by `tests/test_kernel_releases.py` |
| [`update_speaker_pin_quirks.py`](update_speaker_pin_quirks.py) | `_SPEAKER_PIN_QUIRKS` in `lib/data/speaker_pin_quirks.py` — the machines whose BIOS hides a woofer pin | `.github/workflows/speaker-pin-quirks.yml`, weekly. Rebuilt wholesale each run: entries do disappear upstream, and a stale one tells a user to force a fixup their kernel no longer has. Guarded by `tests/test_speaker_pin_quirks.py` |
| [`user_review_capture.py`](user_review_capture.py) | The four reviewer-ready captures the **/user-review** skill hands a cold reader — pty-wrapped runs so stdout and stderr interleave as a terminal shows them, ANSI stripped, the last-screen slice cut, and the preview blocks redacted of the finding names that would give the answer away | That skill, once per review round |

## Measurement directories

Each has its own README describing the procedure end to end; these rows say
only which question the directory answers.

| directory | what it keeps correct | who runs it, and when |
|---|---|---|
| [`measure_dax/`](measure_dax/) | The measured ground truth — what DAX3 itself does, captured on Windows over WASAPI loopback and analysed here. Converter-independent, so these captures stay valid across our edits | You, when a device's real response is the missing evidence. `make_stimulus.py` (Linux) → `capture_dax.py` (Windows) → `analyze.py` (Linux) |
| [`measure_ee/`](measure_ee/) | Whether the generated preset, running live in EasyEffects, matches that ground truth — plus the variant sweeps that narrow a candidate change before it is adopted | You, through **/audio-validate**. EE-side captures go stale after any FIR or scaling change; regenerate before comparing |
| [`measure_perf/`](measure_perf/) | The README's "which should I use?" guidance: CPU cycles and memory for the same preset through EasyEffects vs the PipeWire filter-chain | You, when that cost claim needs re-measuring on a device |
| [`measure_pw/`](measure_pw/) | That the PipeWire `filter-chain` conf is equivalent to the EasyEffects chain in both frequency and time domain — and, through `validate_conf.py`, that it is schema-valid at all | The comparisons: you, through the handoff. `validate_conf.py`: you, against a conf already on disk — `ee_to_pipewire.py` runs the same check in process on every run |

## Three files that look misplaced

"This is obviously in the wrong place" is the first thought every reader has
about each of these. For two of them it is wrong, and the reason is worth
writing down; for the third it is right, and the fix is half-landed.

**[`_wavio.py`](_wavio.py) — a library at the root of a directory of CLIs.**
It has no CLI, no test and, until this file, no documentation. It sits here
because `tools/` is the common ancestor of its three consumer directories:
nine scripts under `measure_dax/`, `measure_ee/` and `measure_pw/` each insert
this directory on `sys.path` and import `read` from it. Anywhere deeper and
two of the three would need a longer path; a sub-package of its own would
change all nine import lines to buy one file a tidier home.

**[`corpus_audit.py`](corpus_audit.py) — not only a CLI.**
`preview_output.py` and `render_forced_conditions.py` both `from corpus_audit
import discover_roots, find_xmls`, so it is the corpus-discovery library for
the copy tooling as well as the statistics command. `.claude/rules/docs.md`
also names it by literal path in its `paths:` frontmatter, so the docs rule
loads whenever it is edited — a glob that silently matches nothing if the file
moves.

**[`measure_pw/validate_conf.py`](measure_pw/validate_conf.py) — not a
measurement tool.**
It is the command-line front end to the schema check `ee_to_pipewire.py`
applies to every conf it writes, refusing to write one that fails. It needs no
audio, no PipeWire daemon and no capture. It is in `measure_pw/` for
historical reasons — next to the audio battery that is the *other* half of
proving a conf correct — and that is the status quo, not a decision.

The runtime core it wraps is
[`lib/pipewire/validate.py`](../lib/pipewire/validate.py); what stayed here is
the CLI — argument parsing, the stdin form, the 0/1/2 exit codes and the prose
`--help` prints. **The converter no longer shells out to this path**: it calls
`validate.run(conf)` in process, gets a `Report` back and renders the warnings
and errors itself. So the layering violation this file used to carry — the one
place where a top-level user-runnable script depended on something outside
`lib/` — is gone rather than narrowed, and the script is now a standalone tool
like everything else here.

One site still runs it: `tests/corpus/test_ee_to_pipewire_corpus.py`, once, on
a single rendered conf. It carries no `is_file()` guard on the script, and that
is the point — a wrapper that moved away has to fail that test loudly rather
than turn into "don't check", which is how an XML would pass green with no conf
ever validated.

`tests/test_layout.py` holds the two guards on the ways this file breaks by
itself. `test_the_validator_cli_still_finds_its_runtime_core` watches its
`sys.path` bootstrap — the repo root, counted from
`Path(__file__).resolve().parents[2]` — so the CLI keeps starting from outside
the checkout. Move either file and that import breaks, while
`tests/test_validate_conf.py` goes on importing `lib.pipewire.validate`
directly and holds no path at all.
`test_the_validator_cli_separates_setup_failure_from_a_bad_conf` watches the
exit codes: a dependency it cannot run has to exit 2, never the 1 that means
the conf itself is bad.

## Why this directory is flat

It looks like it wants subdirectories — updaters in one, copy tooling in
another. It doesn't get them, and the reasons are cheap to write down and
expensive to rediscover:

- **These paths are effectively public API.** Every `tools/…` path here is
  written down somewhere no importer resolves: CLAUDE.md, `.claude/rules/`
  `paths:` frontmatter, `.claude/skills/`, the workflows, `docs/`, `lib/`
  docstrings, the suite, and the tools' own usage strings. Moving one file is
  a repo-wide edit whose misses are silent — which is exactly the failure
  `docs/code-organisation.md` records under "A tool keyed on a fixed path list goes
  quiet when code moves". `tests/test_layout.py` now sweeps every one of those
  references and fails on any that stops resolving, so the cost is *findable*;
  it is still a cost.
- **`_wavio.py` pins the root anyway.** Grouping the ten scripts would leave
  it here regardless, since it is the common ancestor of its consumers — so
  the flat directory does not disappear, it only gets emptier.
- **The grouping would be invented.** Ten scripts doing ten unrelated jobs do
  not fall into categories that would still look right in six months, and a
  wrong grouping is worse than none: it tells you where a script *isn't*.

What the flat directory actually lacked was a map and a check that its paths
resolve. Both now exist — this file, and the `tools/` assertions in
`tests/test_layout.py`. That is the answer; it does not need auditing again.
