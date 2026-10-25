# Vendor APO layers: speaker voicing that ships outside the DAX3 XML

## Where this stands

[reference.md](../reference.md) covers what the converter emits.

- **Surface Pro 9 (Intel) voices its speaker in Microsoft's Surface APO**, not
  in Dolby. Its DAX3 XML switches off its audio-optimizer, IEQ and graphic EQ
  in every profile ([Surface APO](vendor-apo/surface.md#r-surface-apo-efx)).
- **The vendor-apo layer** translates that config's `R/EFX` chain. It is on by
  default where a bound config has a speaker chain, and unvalidated: one
  listener has heard it and no Windows capture exists for any Surface
  ([Surface APO](vendor-apo/surface.md#r-surface-apo-efx)).
- **Eleven of the 25 surveyed Surface models ship a Surface APO endpoint
  chain**, on HD-Audio and SoundWire speakers. The parser reads all 16 of their
  configs; on 3 it leaves `Crystal` out. Every Dolby XML one of them binds has
  its voicing off ([Surface fleet](vendor-apo/surface.md#r-surface-apo-fleet)).
- **HP's Bang & Olufsen laptops can ship no Dolby file at all**: the ZBook 15
  G6 voices its speaker in a Sonitude APO preset. Applied as cuts only, its
  curve is within 1.00 dB rms of what the B&O switch changes on the ZBook, and
  1.61 dB rms of an OMEN's published Windows response, from 200 Hz to 12.5 kHz
  ([Sonitude presets](vendor-apo/sonitude.md#r-sonitude-apo-presets)).
- **Some Dolby drivers write the tuning into the registry** from the driver's
  `.inf`, as numbered values in the older DAX2 format. Their arrays match DAX3's
  IEQ, audio-optimizer, regulator and virtual-bass fields by shape, and a DAX3
  preset in the same form names five single values and a pair. The on/off
  switches are read by adjacency, and nothing has been heard or captured
  ([DAX2 presets](vendor-apo/dolby-dax2.md#r-dolby-registry-preset)).
- **18 Surface codec IDs bind a DAX2 preset**, 13 distinct ones, in the
  Realtek drivers of Microsoft's Surface packages. None turned up outside
  Surface, where only DAX3-era drivers were searched
  ([DAX2 fleet](vendor-apo/dolby-dax2.md#r-dolby-registry-fleet)).

Open:

- The layer is one `--disable` name for its EQ, DRC and `Crystal` stages
  together. Per-stage names, the way Dolby's stages each have one, would let
  a listener drop the weakest mapping, `Crystal`, and keep the EQ. One
  by-ear report doesn't yet call for that split.
- A second, opt-in format would meet two gaps the Surface default hides:
  `is_active` ignores `--disable` on an opt-in layer, so that flag is then a
  silent no-op, and the `--enable` help says `vendor-apo` "does nothing
  today".
- Fortemedia's render APO ships a per-device speaker file,
  `SAMSfpaspk_<SUBSYS>.dat`, in an opaque binary format. One Yoga Slim 7 ProX
  14ARH7 package's `OemXAudioExtFM_L.inf` names 96 of them. It is
  undecoded.

## Units by vendor

- [vendor-apo/surface.md](vendor-apo/surface.md):
  [Microsoft's Surface APO: the endpoint chain after Dolby](vendor-apo/surface.md#r-surface-apo-efx)
  and
  [The Surface fleet: which models ship a Surface APO chain, and its shapes](vendor-apo/surface.md#r-surface-apo-fleet)
- [vendor-apo/sonitude.md](vendor-apo/sonitude.md):
  [HP's Sonitude presets: the Bang & Olufsen voicing with no Dolby file](vendor-apo/sonitude.md#r-sonitude-apo-presets)
- [vendor-apo/dolby-dax2.md](vendor-apo/dolby-dax2.md):
  [Dolby DAX2 registry presets: the tuning a driver .inf writes](vendor-apo/dolby-dax2.md#r-dolby-registry-preset)
  and
  [Which drivers ship a DAX2 registry preset](vendor-apo/dolby-dax2.md#r-dolby-registry-fleet)
