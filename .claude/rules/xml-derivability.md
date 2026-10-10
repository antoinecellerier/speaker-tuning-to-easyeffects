---
paths:
  - "lib/dax/**/*.py"
  - "lib/apo/**/*.py"
  - "lib/preset/**/*.py"
---

# Every emitted parameter traces to a tuning-file field

This is where that invariant is kept. A tuning file is one an OEM ships for one
device. `lib/dax/` reads the DAX3 XML. `lib/apo/` reads the vendor APO configs
in the same driver package, such as Surface's `SurfaceAPO_*.json`. `lib/preset/`
turns what they read into the numbers a plugin gets. Nothing between them may
introduce a value that came from somewhere else.

No constant here may exist because it sounded better on one laptop. The value
prop is that **the tuning is the device's own**. A per-device hand-tuned offset
inverts it. The output stops being derived and starts being curated, and the
next device gets nothing, since nobody has it.

## The mappings are hypotheses

Every field-to-parameter mapping in these packages is a guess about what the
vendor's schema means. Several Dolby ones have been wrong. When you edit one:

- Only a capture of the vendor's own processing on Windows, such as a DAX
  capture, can falsify a mapping. Listening, analytical scoring and
  "this looks more like what the field name suggests" cannot.
  The research log (`docs/design-notes.md` and `docs/research/`) records
  which readings a capture has already overturned.
- The bar to change a *default* mapping is high: **≥1 second-device capture**
  confirming the new reading generalises across all bands. One device's capture
  explains that device, not the schema.
- Below that bar, ship the finding as an `--enable …` opt-in. The established
  path then stays the default, and whoever has the second device can test the
  hypothesis.
- A new vendor APO layer adds a source rather than changing a default
  mapping, so a capture does not gate it. It ships as an opt-in until a
  listener on a device it binds confirms the layer by ear. It then defaults
  on where it binds, and its mappings stay unvalidated until a Windows
  capture (research `r-surface-apo-efx`).
- Unit conversions count as mappings. 1/16 dB, percent-vs-fraction and Q15 fixed
  point have each been read wrong at least once. Each misreading is a silent
  factor error, not a crash.

`docs/reference.md` "Validated vs unvalidated mappings" gives each parameter's
status: capture-validated or unvalidated. The research log
(`docs/design-notes.md` and `docs/research/`) holds the evidence behind each,
and design-notes "Unvalidated converter scaling factors" lists the invented
constants.

## What is *not* a source of parameters

Parse no parameter from the base64 biquad blob `filter_coefficients` in
`tuning-vlldp`. It is VLLDP-internal analysis filtering, **not** an audio-path
equaliser. The audio-optimizer and speaker-PEQ parameters already capture the
speaker correction it looks like it might carry, so parsing it would add a
second, wrong source for values that are already right. `docs/reference.md`
"Not implemented" and research `r-filter-coefficients-blob` record the
evidence.

This rule does **not** cover PipeWire node/sink selection
(`lib/hardware/sinks.py`) or hardware probing generally, because they have no
tuning-file provenance to trace. They are heuristics over the running system,
and a flag can always override them.
