---
paths:
  - "tools/**/*.py"
---

# Comparison plots: verify every curve is actually visible

A curve hidden under another is the most expensive kind of plot bug, because
the figure looks finished. The reading is then wrong in a way that survives
review — "the two responses match perfectly" and "one of them was never drawn"
produce the same picture — and the cost lands as repeated render-look-fix
cycles nobody counts.

Before calling a comparison plot done:

- **Set z-order explicitly.** Draw order is not a plan; it is whatever the
  code happened to do.
- **Plot the reference curve last, dashed, in a distinct colour.** Last so it
  is on top, dashed so it reads through whatever is under it, distinct so a
  colourblind reader and a greyscale print both keep the distinction.
- **Check both axis extremes.** A curve that leaves the axes at one end is
  indistinguishable from one that is merely flat there, and log-frequency
  plots hide the bottom decade especially well.
- **Confirm the legend entry count matches the curve count you intended.**
  That is the cheapest automatic check available and it catches the case where
  a series was never plotted at all.

Ad-hoc analysis scripts written outside `tools/` are where this bites most and
are outside any `paths:` glob — which is why the trigger for this stays in
CLAUDE.md rather than living only here.
