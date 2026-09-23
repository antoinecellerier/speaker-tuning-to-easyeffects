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

- **README** serves the end user: discover, install, use, troubleshoot, stay
  current. Keep it to that journey. Link to the docs for deep DSP/XML internals
  instead of putting them here. Keep safety-critical or searchable text outside
  `<details>`, because in-page find doesn't match collapsed text and anchors
  into it are unreliable.
  - **Flags list and troubleshooting rows are one line each**, objectively. A
    flag entry is *what it does* plus *when you'd reach for it*, plus the
    default if there is one. A troubleshooting row maps a *symptom* to *which
    flag*. No DSP mechanism, no measurement numbers, no device IDs: link to
    reference/design-notes for the why. A second sentence of mechanism belongs
    in the docs, not the README.
- **docs/reference.md** serves a technically-inclined user asking "what does the
  converter do *now*". It holds settled facts only.
- **docs/design-notes.md** serves a contributor asking "*why*, and what was
  tried".
- **tools/measure_*/** holds on-device measurement workflows.

When unsure, "what it does now" goes to reference, "why / evidence / rejected
approaches" to design-notes, and user-facing how-to to README.

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
   across rows, and prose for an argument.
7. Bold marks at most one headline figure or warning per section, never a
   whole sentence. A bold label opening a list item, such as a
   troubleshooting symptom, is a label, not emphasis, so it is allowed.
8. A heading names what the section holds: no verdict, no URL. Citation
   numbers never change (`Finding N:`, `## N.`, numbered entries and
   items), because code and other docs cite them.
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
