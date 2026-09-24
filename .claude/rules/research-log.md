---
paths:
  - "docs/design-notes.md"
  - "docs/research/*.md"
  - "docs/cross-device-findings.md"
  - "docs/corpus.md"
  - "docs/alternative-pipelines.md"
---

# Research log

These files record what was measured and why the converter does what it
does. The data is append-only.

## Shape of an entry

1. The heading names the subject.
   Yes: `<a id="r-smart-amp-families"></a>`, a blank line, then
   `## What counts as a smart amp, and which ones we watch for`.
   No: `### Hypothesis (b) is rejected; …`, a verdict, where the subject is
   "Testing hypotheses (a) and (b)".
2. The first sentence is the result.
3. A conditions line follows: device, capture or corpus, date, and the
   commit or issue.
4. Figures sit in a table. Each carries n and a source a reader can open: a
   capture name, an issue, a `tools/` script, a commit.
5. At most one paragraph of interpretation, then an `Open:` list of what
   the entry leaves unmeasured.
6. A superseded entry keeps its place and its `r-` tag under a one-line banner
   naming what replaced it.
7. A rejected approach gets one sentence each for the hypothesis, the
   evidence and the verdict.

## Where new content goes

Research goes in the class file under docs/research/ that owns the **lever**:
the stage, parameter or host setting that the result would change or that
the verdict keeps. With no lever (a pure observation of DAX), use the class
of the stage it characterises. If two classes both hold a lever, use the one
the result sentence names. Never split one measurement across files; the
other class gets a one-line Elsewhere link. A new unit gets an `r-` tag,
never a number. Add or extend the issue's row in design-notes "Issues".
Class ownership: loudness-and-limiting = volmax, normalisation, regulator,
brickwall, PEQ trim. eq-and-frequency-response = IEQ, AO, PEQ curve, FIR, XML
units. adaptive-processing = leveler, MBC, dialog, surround, MI.
virtual-bass = VBE and bass enhancer. hardware-and-drivers = kernel, codec,
pins, amps, firmware. easyeffects-and-pipewire = EasyEffects, PipeWire,
WirePlumber, Flatpak, paths, rate. measuring-against-windows = capture method,
metrics, validation bar.

A device investigation's heading names the mechanism, and its first line gives
the issue number.

A class with no file yet keeps its new content in design-notes, in that
class's current section.

A unit's tag is a line `<a id="r-<slug>"></a>` above its heading, then a blank
line. The slug names the mechanism in ≤5 words, holds no number, is unique
across docs/ and never changes. Link it as `<file>#r-<slug>`; cite it from
code, tests, skills or commits as the bare token `` `r-<slug>` ``.

Material that is not a research unit goes by type:

| New content | Home |
|---|---|
| A corpus-wide statistic | cross-device-findings.md, regenerated with `tools/corpus_audit.py` |
| How many tunings are public, and where | corpus.md |
| A sketch of another pipeline | alternative-pipelines.md |
