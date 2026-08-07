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
  mid-string and it stops being clickable.
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

## Every claim is checkable

Plain language is the goal, but a sentence a first-time reader understands
perfectly can still be false, and nothing in `/user-review` is positioned to
notice. Before shipping a message, name what each claim rests on:

- **What the tool does** → the gate that decides it. If the predicate is
  broader or narrower than the sentence, the sentence is wrong: a section
  gated on `if regulator:` describes a stage `--disable regulator` removed,
  and a `<= 0` branch saying "your tuning asks for none" is wrong about
  every negative value.
- **What the audio or Dolby does** → `docs/reference.md` "Validated vs
  unvalidated mappings". Anything on the unvalidated list gets hedged, not
  asserted; anything the docs hold as a *leading hypothesis* is reported as
  what we measured, not as what Dolby intends.
- **How common something is** ("every device", "rare", "usually") → a
  re-derivation date and a figure. Derive it through `resolve_xml_value`,
  never a bare grep: the `preset=` indirection hides values a text search
  reports as absent.
- **What other software does** (PipeWire, WirePlumber, EasyEffects, a shell
  command's output) → a command that reproduces it on a real machine.

**Removing a qualifier is the usual way a true sentence becomes false.**
"Nothing limits it" for "nothing limits it band by band", "the most likely
reason" for "a plausible cause", "sized from this speaker's bass cutoff" for
a constant used on 36 of 39 files — each was a readability win that changed
the truth value. When a qualifier is load-bearing, say so in a comment beside
it, so the next round doesn't trim it back.

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
