---
paths:
  - "dolby_to_easyeffects.py"
  - "ee_to_pipewire.py"
  - "dolby_to_pipewire.py"
  - "lib/**/*.py"
---

# End-of-run messages: two halves, one ask

Assume the reader runs this tool **once**, on a single machine, and never
again. Whatever we want from them we get on that run or not at all. So the
closing block is unconditional, and nothing may be deferred to "next time".

A `Finding` prints in two halves, and the split is the whole point:

- `detail` is the technical why. It prints inline, at the detection site,
  next to the table or value it explains, because only there does it have
  context. The slug leads the line. A left edge is what makes it findable
  when scrolling back through a couple of hundred lines of tables.
- `ask` is ONE short sentence in the user's terms: the fix to try, or the
  question we want answered. It prints in the closing block with the slug
  trailing, so the sentence reads first.

Rules that hold for anything added here:

- **No "nothing to do" entries.** A finding the user cannot act on carries
  no `ask` at all. Its detail still prints inline, so it still reaches us in
  a pasted report. Telling someone nothing is required of them, in a block
  whose purpose is to prompt action, only teaches them to skip it.
- **One link, and it is last.** The closing block prints `_REPORT_FORM_URL`
  once. No message body may contain a URL, because wrapped prose folds it
  mid-string and it stops being clickable. The one carve-out is a
  verification link: the upstream commit a speaker-quirk warning rests on,
  from `upstream_change_lines`. It prints as its own line, verbatim, never
  inside wrapped prose and never in a `Finding`. The closing block's link
  stays the last thing on screen.
- **Declare `kind` where the condition is raised**, not in a central table.
  `"hint"` fixes the user's own audio. `"ask"` is something the project
  needs.
- **Slugs are unique and stable.** They are the handle a user can quote
  back at us, and the de-duplication key across profiles. `--all-profiles`
  visits up to nine. Never key de-duplication on rendered text: several
  findings embed a per-profile value, so text keys silently miss repeats.
- **Name a slug after the symptom, unless it names an XML field.** A finding
  about behaviour gets a name its reader can parse: `loudness-untamed`, not
  `volmax-inert`. A finding about a field keeps the field's own name, such
  as `peak-level` or `regulator-overdrive`. That is the handle triage greps
  for, and those asks request the XML anyway.
- **A finding that didn't apply everywhere says so.** `Finding.scope`
  carries a short label, rendered inside the tag. Empty means "applies
  throughout", so a single-profile run shows nothing, and the default run is
  single-profile.
- **Specific before generic.** Findings this run raised come before the
  `--disable`/`--enable` menus. Once a finding has named a flag, the menus
  shrink to one line per filter.

Prose long enough to need folding must ask for it, via `_cprint_wrapped` or
`_print_flag_hint`. `cprint` hands text to the console verbatim, so URLs
survive.

## `--doctor`: inventory leads, diagnosis trails

Both doctors, `lib/pipewire/checks.py` and `lib/report/doctor_run.py`, print
version → hardware → audio server → the tool's own setup → checks → summary
→ verdict → fix → link. The audio server section is `=== PipeWire ===`:
output sink, clock, dropouts. The setup section is
`=== EasyEffects setup ===` or `=== PipeWire filter-chain setup ===`.
Sections are named by what they list, never "Environment".

**Edit that order, and the text around it, in `lib/report/doctor_layout.py`**,
not in either doctor. The doctors supply only their own facts lines, checks and
remedy. The diagnosis comes last for the same reason the link does: the report
is longer than a terminal, and its reader is there because something is already
wrong. Ending on the inventory scrolled the verdict and the fix command off a
26-line window, and left a PCI listing as the last thing on screen.

- **Inventory is context, so it goes first.** It runs widest to narrowest.
  Hardware comes first, as `--speaker-info` prints it. The tool's own setup,
  with its confs, sinks and presets, comes last and sits directly above the
  checks, because the check details name those confs and sinks.
- **Each section's header sits with what it labels.** The doctor header
  names the checks, not the report.
- **Nothing is appended after the link.** The closing "Paste everything
  above" makes the whole report pasteable, so no section repeats that
  instruction in its own heading.
- `tests/test_pw_doctor.py` and `tests/test_preset.py` each carry a
  `test_doctor_ends_on_the_diagnosis_not_the_inventory` trap.

## Every claim is checkable

A message must hold for **every** device that can reach it, not the one
whose bug report prompted it. `--variant`, `--all-profiles`, each `--disable`
name, SoundWire vs HDA and the simplified schema are all separate readers.

`tests/test_cli.py` ("Closing-block copy contract") traps the one-sentence
budget, the no-URL and no-empty-action rules, slug uniqueness, and that a
clean run collapses to just the ask. When you add a raiser that isn't
table-driven, extend `_every_finding()`, because the traps only cover what
that walks.

After changing copy here, run the **/user-review** skill. The traps are
structural and can't tell you a message is confusing, contradictory, or
impossible to act on.

Neither the traps nor the /user-review reviewers can tell you a message is
*false*. Reviewers grade comprehension, and a wrong sentence can read
beautifully. The **/copy-audit** skill sweeps a git range for that, checking
each claim against the evidence its type demands.
