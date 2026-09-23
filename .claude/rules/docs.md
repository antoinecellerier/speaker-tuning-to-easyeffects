---
paths:
  - "docs/**/*.md"
  - "README.md"
---

# Writing docs in this repo

## Audience & placement — match the file to the reader

Put new content where its reader looks, and keep the others' content out. (The
per-file *content* map is in CLAUDE.md "Docs are layered"; this is the *audience*
and the placement discipline.)

- **README** = the end user: discover → install → use → troubleshoot → stay
  current. Keep it to that journey — deep DSP/XML internals do **not** go here;
  link to the docs instead. Keep safety-critical or searchable text outside
  `<details>` (collapsed text isn't matched by in-page find and anchors into it
  are unreliable).
  - **Flags list / troubleshooting rows are one line each**, objectively: a flag
    entry = *what it does* + *when you'd reach for it* (+ default, if any);
    a troubleshooting row = *symptom* → *which flag*. No DSP mechanism, no
    measurement numbers, no device IDs — link to reference/design-notes for the
    why. If you're writing a second sentence of mechanism, it belongs in the
    docs, not the README.
- **docs/reference.md** = a technically-inclined user asking "what does the
  converter do *now*" — settled facts only (mappings, chain, units,
  not-implemented).
- **docs/design-notes.md** = a contributor asking "*why*, and what was tried" —
  the research log; new findings and rejected approaches land here.
- **docs/cross-device-findings.md** = corpus-wide empirics across devices.
- **tools/measure_*/** = on-device measurement workflows.

Rule of thumb when unsure: user-facing how-to → README; "what it does now" →
reference; "why / evidence / history" → design-notes.
