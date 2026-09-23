# tools/ — the scripts that keep everything else correct

Almost nothing in here ships to a user, and nothing here is part of the
conversion. Each script exists to keep something *else* right: a generated
data table in `lib/data/`, a figure in `docs/`, the release notes, the copy a
run prints, or the converter's own output. The tables below list what each one
keeps correct and who runs it. That, not the filename, is how you find the one
you need. `ab_sink.sh` is a listening aid rather than a correctness check.
`fetch_driver/` is the one thing here an end user runs, and it gets its own
section below.

[`measure_pw/validate_conf.py`](measure_pw/validate_conf.py) is a
near-exception to "nothing here is part of the conversion". It runs the same
schema check `ee_to_pipewire.py` applies to every conf it writes. It is not a
runtime dependency of the converter, though. That check's runtime core is
`lib/pipewire/validate.py`, which the converter calls in process. What is left
here is a front end you can point at a conf yourself. See "Three files that
look misplaced" below before touching it.

**The capture scripts touch your audio devices.** So do the scripts that set
up and tear down a capture sink or chain. They mute speakers, reroute sinks and
swap presets. So they run behind the audio handoff in CLAUDE.md: use the
/audio-validate skill rather than invoking them ad hoc. The comparison
scripts, and those `measure_ee/README.md` marks read-only or offline, are
file-in/file-out, with no audio and no PipeWire daemon. So is
`validate_conf.py`.

## Root scripts

| script | what it keeps correct | who runs it, and when |
|---|---|---|
| [`_wavio.py`](_wavio.py) | Every WAV read in the measurement tree. `pw-record` writes a `PEAK` chunk that `scipy.io.wavfile` doesn't recognise and warns about on each read. This strips it | Nobody directly: it has no CLI. Scripts across `measure_dax/`, `measure_ee/` and `measure_pw/` `sys.path`-insert this directory and `from _wavio import read`. `git grep "from _wavio import" -- tools` lists them. See below for why it lives here |
| [`ab_sink.sh`](ab_sink.sh) | Nothing: it is a listening aid. It flips the default sink with `wpctl` between the generated Dolby voicing sinks and the raw speaker, moving playing streams over immediately. So you can A/B `--variant all --target-sink ''` by ear | You, while choosing a voicing. It is independent of the converters and lists whatever `Audio/Sink` nodes exist |
| [`changelog_section.py`](changelog_section.py) | The GitHub Release. It slices `CHANGELOG.md` down to one version's section for the notes. With `--title` it lifts the heading's tagline into the release title | `.github/workflows/release.yml`, on a pushed `vYYYY.MM` tag. It exits non-zero on a missing section, or on a heading still carrying a date instead of a tagline. So the job fails loudly instead of publishing empty or misnamed notes. `tests/test_changelog_section.py` guards it. That test also holds every `## v` heading in the real file to the shape, because CI runs no tests on a tag push |
| [`check_move_purity.py`](check_move_purity.py) | `git blame -C -C` history across an extraction. It proves a commit is *pure code motion*: every line it adds under `lib/` was already there, byte-for-byte, in a line it removed | You, by hand, against one commit, before pushing an extraction. It is wired to nothing. The rule it enforces is in `docs/code-organisation.md`, "Splitting the single-file scripts" |
| [`corpus_audit.py`](corpus_audit.py) | Every cross-device figure in `docs/cross-device-findings.md` and `docs/design-notes.md`. Those numbers are meant to be re-derived from this, never carried forward | You, after pulling new driver packages. The /copy-audit skill also captures its output as the evidence reviewers check numbers against. It is also imported as a library, see below. `tests/test_corpus_audit.py` guards it |
| [`extract_claims.py`](extract_claims.py) | The inventory of user-visible strings the /copy-audit skill reviews. Each is tagged with whether a given git range changed it | That skill, at the start of an audit. `tests/test_extract_claims.py` guards it, because this tool has twice failed by *shrinking* rather than erroring |
| [`preview_doctor.py`](preview_doctor.py) | The `--doctor` copy under states this machine isn't in, such as an output that isn't the speakers or an install that isn't there. Each scenario stubs the probes and nothing else, so the checks, wording and verdict are the shipped ones | `tools/user_review_capture.py`, for the /user-review skill. `--list` prints the scenarios. `tests/test_preview_doctor.py` guards it |
| [`preview_output.py`](preview_output.py) | The end-of-run copy. For each finding a run can raise, it locates a corpus XML that actually raises it and prints a real run's tail. Every run is `--dry-run`, so nothing is written | You, after changing any message a user reads, and the /user-review and /copy-audit skills. `--list` says which XML matches what |
| [`render_forced_conditions.py`](render_forced_conditions.py) | The other half of that: the conditional messages *no* corpus device reaches, where `preview_output.py` can find no example. It patches one XML field off its otherwise-universal value and runs the generator on the result | The /copy-audit skill, beside `preview_output.py`. A non-zero exit means a condition stopped firing because the patch no longer matches the schema. It does not mean the copy is fine |
| [`scan_sound_tag.py`](scan_sound_tag.py) | The kernel-sound-watch comment for a sound-tree pull tag. It scans the tag's commits for `.github/kernel-watchlist.txt` terms, because a merge-window `-rc1` annotation names none of the per-device quirks behind it | `.github/workflows/kernel-sound-watch.yml`, weekly, once per new tag. `tests/test_scan_sound_tag.py` guards it |
| [`update_kernel_releases.py`](update_kernel_releases.py) | `_KERNEL_SERIES_RELEASES` in `lib/data/kernel_releases.py`, the release-month table behind the old-kernel hint | `.github/workflows/kernel-release-table.yml`, weekly, opening a PR per new series. It is append-only, and report-only without `--write`. `tests/test_kernel_releases.py` guards it |
| [`update_speaker_pin_quirks.py`](update_speaker_pin_quirks.py) | `_SPEAKER_PIN_QUIRKS` in `lib/data/speaker_pin_quirks.py`, the machines whose BIOS hides a woofer pin | `.github/workflows/speaker-quirks.yml`, weekly. It rebuilds the table wholesale each run, because entries do disappear upstream and a stale one tells a user to force a fixup their kernel no longer has. `--blame` also resolves the upstream commit that last wrote each row's line, so the warning links the fix rather than the whole driver. `--blame` needs a token in `GH_TOKEN`. The commit is carried forward like `since`, at one GitHub query per file. `tests/test_speaker_pin_quirks.py` guards it |
| [`update_speaker_route_quirks.py`](update_speaker_route_quirks.py) | `_SPEAKER_ROUTE_QUIRKS` in `lib/data/speaker_route_quirks.py`, the machines whose speaker pin is routed through a widget with no volume amp | The same workflow and run discipline as the pin table above. It shares that script's parser primitives, `since` walk and `--blame` commit resolver by import. Membership is the hand-verified `_FUNC_FIXUP_ROUTES` allowlist only. `tests/test_speaker_route_quirks.py` guards it |
| [`user_review_capture.py`](user_review_capture.py) | The four reviewer-ready captures the /user-review skill hands a cold reader. The runs are pty-wrapped so stdout and stderr interleave as a terminal shows them. ANSI is stripped, the last-screen slice is cut, and the preview blocks are redacted of the finding names that would give the answer away | That skill, once per review round |

## Measurement directories

Each has its own README describing the procedure end to end. These rows say
only which question the directory answers.

| directory | what it keeps correct | who runs it, and when |
|---|---|---|
| [`measure_dax/`](measure_dax/) | The measured ground truth: what DAX3 itself does, captured on Windows over WASAPI loopback and analysed here. The captures are converter-independent, so they stay valid across our edits | You, when a device's real response is the missing evidence. `make_stimulus.py` on Linux → `capture_dax.py` on Windows → `analyze.py` on Linux |
| [`measure_ee/`](measure_ee/) | Whether the generated preset, running live in EasyEffects, matches that ground truth. Also the variant sweeps that narrow a candidate change before it is adopted | You, through /audio-validate. EE-side captures go stale after any FIR or scaling change, so regenerate them before comparing |
| [`measure_perf/`](measure_perf/) | The README's "which should I use?" guidance: CPU cycles and memory for the same preset through EasyEffects vs the PipeWire filter-chain | You, when that cost claim needs re-measuring on a device |
| [`measure_pw/`](measure_pw/) | That the PipeWire `filter-chain` conf is equivalent to the EasyEffects chain in both frequency and time domain. Through `validate_conf.py`, also that it is schema-valid at all | The comparisons: you, through the handoff. `validate_conf.py`: you, against a conf already on disk. `ee_to_pipewire.py` runs the same check in process on every run |

## docs/ — gates for a prose rewrite

These check that a rewrite of the docs, the README or code comments loses no
data. You run them by hand before committing a rewrite, and nothing else calls
them. `tests/test_docs_tools.py` guards all four.

| script | what it keeps correct | who runs it, and when |
|---|---|---|
| [`docs/docinv.py`](docs/docinv.py) | The data in a text: figures with units, dates, hashes, issue refs, flags, paths and code spans. `diff OLD NEW` lists what a rewrite dropped, and `diff NEW OLD` what it invented. `--counts` adds tokens, hedges and quantifiers whose count moved. `--py` reads only comments and docstrings | You, on every rewrite, in both directions |
| [`docs/docstats.py`](docs/docstats.py) | Nothing: it is a metric. Sentence length, asides, parentheticals and long lines per file, with `-b REV` for before and after | You, to see what a rewrite changed |
| [`docs/docwrap.py`](docs/docwrap.py) | 80-column prose. `check` reports long, ragged or split lines. `fix` rewraps them and refuses any change beyond whitespace | You, after editing prose |
| [`docs/check_comment_only.py`](docs/check_comment_only.py) | That a change to `.py` files touched only comments and docstrings, by comparing the `ast` of both versions | You, before committing a comment or docstring rewrite |

## fetch_driver/ — a staging area, not a new category

[`fetch_driver/get_lenovo_dax_xml.py`](fetch_driver/get_lenovo_dax_xml.py) is
the script that end user runs. On a Lenovo laptop with no Windows partition, it
resolves the audio-driver package from Lenovo's update catalog, verifies and
unpacks it, and prints the extracted-XML directory to hand to a converter. It
does not run a converter. That step is meant to move inside
`dolby_to_easyeffects.py`, as a "no XML found, fetch it?" prompt. This
directory stages that capability until it lands. It is not a permanent
"user-facing tools" bucket. Full usage:
[`fetch_driver/README.md`](fetch_driver/README.md).

## Three files that look misplaced

"This is obviously in the wrong place" is the first thought every reader has
about each of these. For two of them it is wrong, and the reason is worth
writing down. For the third it is right, and the fix is half-landed.

**[`_wavio.py`](_wavio.py): a library at the root of a directory of CLIs.**
It has no CLI, no test and no documentation besides this file. It sits here
because `tools/` is the common ancestor of its three consumer directories.
Scripts under `measure_dax/`, `measure_ee/` and `measure_pw/` each insert this
directory on `sys.path` and import `read` from it. Anywhere deeper, two of the
three would need a longer path. A sub-package of its own would change every one
of those import lines to buy one file a tidier home.

**[`corpus_audit.py`](corpus_audit.py): not only a CLI.**
`preview_output.py` and `render_forced_conditions.py` both
`from corpus_audit import discover_roots, find_xmls`. So it is the
corpus-discovery library for the copy tooling as well as the statistics
command. `.claude/rules/claims.md` also names it by literal path in its
`paths:` frontmatter, so the claims rule loads whenever it is edited. That
glob silently matches nothing if the file moves.

**[`measure_pw/validate_conf.py`](measure_pw/validate_conf.py): not a
measurement tool.**
It is the command-line front end to the schema check `ee_to_pipewire.py`
applies to every conf it writes. The converter refuses to write a conf that
fails. The script needs no audio, no PipeWire daemon and no capture. It is in
`measure_pw/` for historical reasons, next to the audio battery that is the
*other* half of proving a conf correct. That is the status quo, not a
decision.

The runtime core it wraps is
[`lib/pipewire/validate.py`](../lib/pipewire/validate.py). What stayed here is
the CLI: argument parsing, the stdin form, the 0/1/2 exit codes and the prose
`--help` prints. The converter does not shell out to this path. It calls
`validate.run(conf)` in process, gets a `Report` back and renders the warnings
and errors itself. So no top-level user-runnable script depends on anything
outside `lib/` for this check, and the script is a standalone tool like
everything else here.

One site runs it: `tests/corpus/test_ee_to_pipewire_corpus.py`, once, on a
single rendered conf. It carries no `is_file()` guard on the script, and that
is the point. A wrapper that moved away has to fail that test loudly rather
than turn into "don't check". Otherwise an XML would pass green with no conf
ever validated.

`tests/test_layout.py` holds the two guards on the ways this file breaks by
itself:

- `test_the_validator_cli_still_finds_its_runtime_core` watches its `sys.path`
  bootstrap, so the CLI keeps starting from outside the checkout. The
  bootstrap is the repo root, counted from
  `Path(__file__).resolve().parents[2]`. Move either file and that import
  breaks. Meanwhile `tests/test_validate_conf.py` goes on importing
  `lib.pipewire.validate` directly and holds no path at all.
- `test_the_validator_cli_separates_setup_failure_from_a_bad_conf` watches the
  exit codes. A dependency it cannot run has to exit 2, never the 1 that means
  the conf itself is bad.

## Why this directory is flat

It looks like it wants subdirectories, such as updaters in one and copy
tooling in another. It doesn't get them. The reasons are cheap to write down
and expensive to rediscover:

- **These paths are effectively public API.** Every `tools/…` path here is
  written down somewhere no importer resolves: CLAUDE.md, `.claude/rules/`
  `paths:` frontmatter, `.claude/skills/`, the workflows, `docs/`, `lib/`
  docstrings, the suite, and the tools' own usage strings. Moving one file is
  a repo-wide edit whose misses are silent. That is exactly the failure
  `docs/code-organisation.md` records under "A tool keyed on a fixed path list
  goes quiet when code moves". `tests/test_layout.py` sweeps every one of
  those references and fails on any that stops resolving. So the cost is
  *findable*, but it is a cost.
- **`_wavio.py` pins the root anyway.** Grouping the loose scripts would leave
  it here regardless, since the root is the common ancestor of its consumers. So
  the flat directory does not disappear. It only gets emptier.
- **The grouping would be invented.** The loose scripts do unrelated jobs, which
  do not fall into categories that would still look right in six months. A wrong
  grouping is worse than none: it tells you where a script *isn't*.

This holds for the loose scripts. A self-contained kit with its own section in
this file gets a directory, as `measure_*/`, `fetch_driver/` and `docs/` do.

What the flat directory lacked was a map and a check that its paths resolve.
Both exist: this file, and the `tools/` assertions in `tests/test_layout.py`.
That is the answer, and it does not need auditing again.
