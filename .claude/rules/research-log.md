---
paths:
  - "docs/design-notes.md"
  - "docs/cross-device-findings.md"
  - "docs/corpus.md"
  - "docs/alternative-pipelines.md"
---

# Research log

These files record what was measured and why the converter does what it
does. The data is append-only.

## Shape of an entry

1. The heading names the subject.
   Yes: `### Finding 6: Testing hypotheses (a) and (b)`.
   No: `### Finding 6: Hypothesis (b) is rejected; …`.
2. The first sentence is the result.
3. A conditions line follows: device, capture or corpus, date, and the
   commit or issue.
4. Figures sit in a table. Each carries n and a source a reader can open: a
   capture name, an issue, a `tools/` script, a commit.
5. At most one paragraph of interpretation, then an `Open:` list of what
   the entry leaves unmeasured.
6. A superseded entry keeps its place and number under a one-line banner
   naming what replaced it.
7. A rejected approach gets one sentence each for the hypothesis, the
   evidence and the verdict.

## Where new content goes

| New content | Home |
|---|---|
| A capture compared against DAX | the next `### Finding N` in design-notes "Empirical comparison vs DAX3 on Windows" |
| An unvalidated scaling factor | a new numbered entry in design-notes "Unvalidated converter scaling factors" |
| A next step toward DAX parity | design-notes "Follow-ups to close the gap to DAX" |
| A device investigation that changed the converter | its own design-notes H2, titled by the mechanism, with the issue number in the first line |
| An idea tried and dropped | design-notes "Rejected approaches" |
| A corpus-wide statistic | cross-device-findings.md, regenerated with `tools/corpus_audit.py` |
| How many tunings are public, and where | corpus.md |
| A sketch of another pipeline | alternative-pipelines.md |
