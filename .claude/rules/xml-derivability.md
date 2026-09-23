---
paths:
  - "lib/dax/**/*.py"
  - "lib/preset/**/*.py"
---

# Every emitted parameter traces to an XML field

This is where that invariant is kept. `lib/dax/` reads the DAX3 XML, and
`lib/preset/` turns what it read into the numbers a plugin gets. Nothing between
the two may introduce a value that came from somewhere else.

No constant here may exist because it sounded better on one laptop. The value
prop is that **the tuning is the device's own**. A per-device hand-tuned offset
inverts it. The output stops being derived and starts being curated, and the
next device gets nothing, since nobody has it.

## The mappings are hypotheses

Every XML-to-parameter mapping in these two packages is a guess about what
Dolby's schema means. Several have been wrong. When you edit one:

- Only a DAX capture can falsify a mapping. Listening, analytical scoring and
  "this looks more like what the field name suggests" cannot.
  `docs/design-notes.md` records which readings a capture has already
  overturned.
- The bar to change a *default* mapping is high: **≥1 second-device capture**
  confirming the new reading generalises across all bands. One device's capture
  explains that device, not the schema.
- Below that bar, ship the finding as an `--enable …` opt-in. The XML-only path
  then stays the default, and whoever has the second device can test the
  hypothesis.
- Unit conversions count as mappings. 1/16 dB, percent-vs-fraction and Q15 fixed
  point have each been read wrong at least once. Each misreading is a silent
  factor error, not a crash.

`docs/reference.md` "Validated vs unvalidated mappings" gives each parameter's
status: capture-validated or unvalidated. `docs/design-notes.md` holds the
evidence behind each, and the empirical-shortcut and unvalidated-scaling lists.

## What is *not* a source of parameters

Parse no parameter from the base64 biquad blob `filter_coefficients` in
`tuning-vlldp`. It is VLLDP-internal analysis filtering, **not** an audio-path
equaliser. The audio-optimizer and speaker-PEQ parameters already capture the
speaker correction it looks like it might carry. `docs/reference.md` "Not
implemented" and `docs/design-notes.md` "Rejected approaches" record the
evidence.
