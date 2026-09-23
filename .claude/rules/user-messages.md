---
paths:
  - "dolby_to_easyeffects.py"
  - "ee_to_pipewire.py"
  - "dolby_to_pipewire.py"
  - "lib/**/*.py"
---

# End-of-run messages: two halves, one ask

**Assume the reader runs this tool once.** One machine, once, never again.
Whatever we want from them we get on that run or not at all — which is why
the closing block is unconditional and why nothing may be deferred to "next
time".

A `Finding` is printed in two halves, and the split is the whole point:

- **`detail`** — the technical why. Prints *inline, at the detection site*,
  next to the table or value it explains, because that is the only place it
  has context. The slug **leads** here: a left edge is what makes it findable
  when scrolling back through a couple of hundred lines of tables.
- **`ask`** — ONE short sentence in the user's terms, either the fix to try
  or the question we want answered. Prints in the closing block, slug
  **trailing**, so the sentence reads first.

Rules that hold for anything added here:

- **No "nothing to do" entries.** A finding the user cannot act on carries no
  `ask` at all — its detail still prints inline, so it still reaches us in a
  pasted report. Telling someone nothing is required of them, in a block whose
  purpose is to prompt action, only teaches them to skip the block.
- **One link, and it is last.** `_REPORT_FORM_URL` is printed once, by the
  closing block. No message body may contain a URL: wrapped prose folds it
  mid-string and it stops being clickable. The one carve-out is a
  *verification* link — the upstream commit a speaker-quirk warning rests on
  (`upstream_change_lines`) — printed as its own line, verbatim, never inside
  wrapped prose and never in a `Finding`; the closing block's link stays the
  last thing on screen.
- **Declare `kind` where the condition is raised**, not in a central table —
  `"hint"` fixes the user's own audio, `"ask"` is something the project needs.
- **Slugs are unique and stable.** They are the de-duplication key across
  profiles (`--all-profiles` visits up to nine) and the handle a user can
  quote back at us. Never key de-duplication on rendered text: several
  findings embed a per-profile value, so text keys silently miss repeats.
- **Name a slug after the symptom, unless it names an XML field.** A finding
  about behaviour gets a name its reader can parse (`loudness-untamed`, not
  `volmax-inert`). A finding *about* a field keeps the field's own name
  (`peak-level`, `regulator-overdrive`) — that is the handle triage greps for,
  and those asks request the XML anyway.
- **A finding that didn't apply everywhere says so.** `Finding.scope` carries
  a short label, rendered inside the tag; empty means "applies throughout", so
  a single-profile run — the default — shows nothing.
- **Specific before generic.** Findings this run actually raised come before
  the `--disable`/`--enable` menus, which shrink to one line per filter once
  a finding has already named a flag.

`cprint` hands text to the console verbatim so URLs survive, so **prose long
enough to need folding must ask** via `_cprint_wrapped` / `_print_flag_hint`.

## `--doctor`: inventory leads, diagnosis trails

Both doctors (`lib/pipewire/checks.py`, `lib/report/doctor_run.py`) print
version → hardware → the audio server (`=== PipeWire ===`: output sink,
clock, dropouts) → the tool's own setup (`=== EasyEffects setup ===` /
`=== PipeWire filter-chain setup ===`) → checks → summary → verdict → fix
→ link. Sections are named by what they list, never "Environment". **That
order and the text around it live in
`lib/report/doctor_layout.py`** — edit the frame there, not in either doctor,
which supply only their own facts lines, checks and remedy. Same reason as
above, sharpened: the report is longer than a terminal and the reader is there
*because* something is already wrong, so ending on the inventory scrolled the
verdict and the fix command off a 26-line window and left a PCI listing as the
last thing on screen.

- **Inventory is context, so it goes first** — widest (hardware, what
  `--speaker-info` prints) then narrowest (this tool's confs, sinks, presets).
  Environment sits directly above the checks because the check details name
  those confs and sinks.
- **Each section's header sits with what it labels.** The doctor header names
  the checks, not the report.
- **Nothing is appended after the link**, and the closing
  "Paste everything above" is what makes the whole report pasteable — no
  section repeats the instruction in its own heading.
- `tests/test_pw_doctor.py` and `tests/test_preset.py` each carry a
  `test_doctor_ends_on_the_diagnosis_not_the_inventory` trap.

## Every claim is checkable

What each claim in a message rests on, and why a dropped qualifier is the
usual failure: `.claude/rules/claims.md`.

A message must also hold for **every** device that can reach it, not the one
whose bug report prompted it. `--variant`, `--all-profiles`, each `--disable`
name, SoundWire vs HDA and the simplified schema are all separate readers.

`tests/test_cli.py` ("Closing-block copy contract") traps the one-sentence
budget, the no-URL and no-empty-action rules, slug uniqueness, and that a
clean run collapses to just the ask. Extend `_every_finding()` when you add a
raiser that isn't table-driven — the traps only cover what that walks.

Those traps are structural and can't tell you a message is confusing,
contradictory, or impossible to act on. After changing copy here, run the
**/user-review** skill: it puts the real output past reviewers role-playing a
first-time user and reports severity-ranked findings.

Nor can they tell you a message is *false* — reviewers grade comprehension,
and a wrong sentence can read beautifully. The **/copy-audit** skill sweeps a
git range for that, checking each claim against the evidence its type
demands.
