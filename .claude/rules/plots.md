---
paths:
  - "tools/**/*.py"
---

# Comparison plots: verify every curve is actually visible

A curve hidden under another is the most expensive kind of plot bug, because
the figure looks finished. "The two responses match perfectly" and "one of
them was never drawn" produce the same picture. So the wrong reading survives
review, and the cost lands as repeated render-look-fix cycles nobody counts.

Before calling a comparison plot done:

- Set z-order explicitly. Draw order is whatever the code happened to do,
  not a plan.
- Plot the reference curve last, dashed, in a distinct colour. Last puts it
  on top. Dashed lets it read through whatever is under it. A distinct
  colour keeps the distinction for a colourblind reader and a greyscale
  print.
- Check both axis extremes. A curve that leaves the axes at one end is
  indistinguishable from one that is merely flat there. Log-frequency plots
  hide the bottom decade especially well.
- Confirm the legend entry count matches the curve count you intended. It is
  the cheapest automatic check available, and it catches a series that was
  never plotted at all.
