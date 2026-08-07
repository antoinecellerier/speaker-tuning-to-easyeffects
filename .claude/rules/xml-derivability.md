---
paths:
  - "lib/dax/**/*.py"
  - "lib/preset/**/*.py"
---

# Every emitted parameter traces to an XML field

This is where that invariant is actually kept: `lib/dax/` reads the DAX3 XML,
`lib/preset/` turns what it read into the numbers a plugin gets. Nothing
between the two may introduce a value that came from somewhere else.

**The value prop is that the tuning is the device's own.** A per-device
hand-tuned offset inverts it: the output stops being derived and starts being
curated, and the next device — which nobody has — gets nothing. So no
constant here is allowed to exist because it sounded better on one laptop.

## The mappings are hypotheses, not revealed truth

Every XML→parameter mapping in these two packages is a guess about what
Dolby's schema means. Several have been wrong. The consequence for editing
them:

- **DAX captures are the only signal that can falsify a mapping.** Listening,
  analytical scoring and "this looks more like what the field name suggests"
  are not. `docs/design-notes.md` records which readings a capture has already
  overturned.
- **The bar to change a *default* mapping is high: ≥1 second-device capture**
  confirming the new reading generalises across all bands. One device's
  capture explains that device; it does not establish a schema.
- Below that bar, ship the finding as an **opt-in** (`--enable …`) so the
  XML-only path stays the default and the hypothesis is testable by whoever
  has the second device.
- Unit conversions count as mappings. 1/16 dB, percent-vs-fraction and Q15
  fixed point have each been read wrong at least once, and each one is a
  silent factor error rather than a crash.

Current per-parameter status — which mappings are capture-validated and which
are still unvalidated — is `docs/reference.md` "Validated vs unvalidated
mappings". The evidence behind each, plus the empirical-shortcut and
unvalidated-scaling lists, is `docs/design-notes.md`.

## What is *not* a source of parameters

`filter_coefficients` — the base64 biquad blob in `tuning-vlldp` — is
VLLDP-internal analysis filtering, **not** an audio-path equaliser. It has
been decoded, and the coefficients do not produce sensible audio curves. The
speaker correction it looks like it might carry is already captured by the
audio-optimizer and speaker-PEQ parameters, so parsing it here would add a
second, wrong source for values that are already right. Recorded in
`docs/reference.md` "Not implemented" and `docs/design-notes.md` "Rejected
approaches" so it stops being re-proposed.

Two things this rule does **not** cover, because they have no XML provenance
to trace and never did: PipeWire node/sink selection (`lib/hardware/sinks.py`)
and hardware probing generally. Those are heuristics over the running system,
always user-overridable by a flag.
