---
paths:
  - "README.md"
  - "CHANGELOG.md"
  - "docs/**/*.md"
  - "tools/**/*.md"
  - ".github/ISSUE_TEMPLATE/*"
---

# Writing docs in this repo

## Audience & placement

Put new content where its reader looks, and keep the others' content out.
CLAUDE.md "Docs are layered" maps what each file holds. This section covers each
file's *audience* and the placement discipline.

- **README** is the end user's landing page: what the tool is, whether it
  works on their device, quick start, install, what to do when something is
  wrong, and where to read on. Anything a reader needs after the first run
  goes to a user guide, and README links it.
- **User guides** serve the same end user: use, tune, troubleshoot. They are
  the pages under `docs/README.md` "Using it", each opening with the
  `[README](../README.md) · [All docs](README.md)` line. Link to reference
  or the research log for DSP/XML internals instead of putting them here.
- **Flags lists and symptom rows are one line each**, objectively, in the
  guides and in README "Something not right?". A flag entry is *what it does*
  plus *when you'd reach for it*, plus the default if there is one. A symptom
  row maps a *symptom* to *which flag or page*. No DSP mechanism, no
  measurement numbers, no device IDs: link to reference or the research log
  for the why. A second sentence of mechanism belongs there.
- **No collapsible sections** (`<details>`) in any doc. Use a heading, because
  in-page find and search engines miss collapsed text and anchors into it are
  unreliable.
- **docs/reference.md** serves a technically-inclined user asking "what does the
  converter do *now*". It holds settled facts only.
- **docs/design-notes.md** and the per-class **docs/research/** files serve a
  contributor asking "*why*, and what was tried".
- **tools/measure_*/** holds on-device measurement workflows.

When unsure, "what it does now" goes to reference, "why / evidence / rejected
approaches" to the research log (placement: research-log.md "Where new content
goes"), and user-facing how-to to a user guide.

## Sentences

Write for the reader the file serves, in the fewest sentences that answer
their question, then stop. A short section is never the defect.

1. One fact per sentence. Give an aside its own sentence, because a reader
   loses the main clause across a dash or a parenthesis.
2. Lead with the result or the action. Conditions and evidence follow.
3. Use the literal phrase. When short and clear pull apart, choose clear.
4. Describe the current state and what to do. "Now", "no longer" and
   "still" are changelog voice. What is no longer needed is not worth
   saying, since nobody can act on it.
5. State what was tested, seen and concluded. The path that led there
   belongs in git and the issue thread.

## Structure

6. Use a list for parallel items or steps, a table for values compared
   across rows, and prose for an argument. A table cell holds a value or
   a short phrase, plus a second short clause where needed. A longer cell
   usually means the row wants a bold-labelled note under the table, or
   its own subsection if it is an essay (`docstats.py` counts long
   cells).
7. Bold marks at most one headline figure or warning per section, never a
   whole sentence. A bold label opening a list item, such as a
   troubleshooting symptom, is a label, not emphasis, so it is allowed.
8. A heading names what the section holds: no verdict, no URL.
9. Each fact has one home, and other places link to it. A link replaces a
   restatement only when its target holds the fact.
10. Judge length against the neighbours: a section longer than neighbours
    that matter as much needs cutting. In a reference, stop at the first
    sentence that explains instead of describes.
11. This file is the spec. A neighbouring entry's format may predate it, so
    don't copy it.
12. Wrap markdown prose at 80 columns and never split a link or a code
    span (`tools/docs/docwrap.py check`, `fix`). Released CHANGELOG
    sections and the issue forms keep their layout.

After changing README or a user guide, run the **/docs-review** skill. The doc
tests trap links and drift, not a reader who gets lost.
