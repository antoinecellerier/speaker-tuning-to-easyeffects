---
paths:
  - "dolby_to_easyeffects.py"
  - "ee_to_pipewire.py"
  - "dolby_to_pipewire.py"
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
- **Specific before generic.** Findings this run actually raised come before
  the `--disable`/`--enable` menus, which shrink to one line per filter once
  a finding has already named a flag.

`cprint` hands text to the console verbatim so URLs survive, so **prose long
enough to need folding must ask** via `_cprint_wrapped` / `_print_flag_hint`.

`tests/test_cli.py` ("Closing-block copy contract") traps the one-sentence
budget, the no-URL and no-empty-action rules, slug uniqueness, and that a
clean run collapses to just the ask. Extend `_every_finding()` when you add a
raiser that isn't table-driven — the traps only cover what that walks.
