# Vendor APO layers: Dolby DAX2 registry presets

[reference.md](../../reference.md) covers what the converter emits, and
[vendor-apo.md](../vendor-apo.md) where the vendor APO layers stand.

<a id="r-dolby-registry-preset"></a>

## Dolby DAX2 registry presets: the tuning a driver .inf writes

Some drivers write the device's Dolby tuning into the registry, in the older
DAX2 format, instead of shipping a DAX3 XML: 47 numbered endpoint properties,
seven of them arrays, four of which carry their own frequency grid. The arrays
match DAX3's IEQ, audio-optimizer, graphic-EQ, regulator and virtual-bass
fields by shape. A DAX3 preset written the same way names five of the single
values, each matching one field of a DAX3 XML, and the volume leveler's two
targets as a pair. Conditions: Surface Laptop 3 13.5" Intel (issue #148),
`SurfaceLaptop3_Intel_Win11_22000_24.080.2664.0.msi` (SHA-256 `15910385…`),
`HDXSSTM2.inf` DriverVer 6.0.9249.2, `DAP_CPDP_VERSION` 2.0.1, codec ALC274
`10EC11B2`. The key between the formats: Surface Go 3
`SurfaceGo3_Win11_22631_26.080.320.0.msi` (SHA-256 `41983621…`), `HDXHAPM.inf`
6.0.9157.2 `[DAX3Tuning_11CE.AddReg]` against
`Headphone_Default_Generic_Default_DolbyAudioPremium.xml` from the same MSI.
Read 2026-10-25.

**The binding.** The codec's hardware ID, such as
`HDAUDIO\FUNC_01&VEN_10EC&DEV_0274&SUBSYS_10EC11B2`, names an install section.
Its AddReg sections set `OEMSettingsOverride` on each endpoint topology to a
preset key, and write the preset under `InterfaceSetting\<preset>\EP\0` as
`{f112024a-fe30-42a8-80ab-8dd825a06f78},N`. The section and file names are the
OEM's (`DAX2Tuning_11B2.AddReg`, but most codecs bind presets written elsewhere
in the file). One `.inf` holds the presets for every codec it binds, and codecs
share preset keys. The speaker and headphone presets hold different values, and
only the name says which endpoint a preset is for (`…_SPK`, `…_HP`). Each
Surface package whose file list was checked ships Dolby's `DolbyDAX2APO*.dll` in
the folder of the `.inf` carrying the property set
([fleet](#r-dolby-registry-fleet)).

| Container | Layout (fits every preset read) |
|---|---|
| value | REG_BINARY: an 8-byte PROPVARIANT header, u16 type first, then 32-bit little-endian integers |
| type 3 | one int32 |
| type 0x41 | int32 blob: `n`, then arrays of `n` values, each prefixed by its count `n`; or, for the virtual-bass frequency pairs, two values |
| frequency blob | the first array is the grid in Hz, rising |

The same arrays appear a second time under `SSTPPCfg\{E0E018A8-…}\SPK\MFX`,
for Intel's SST DSP, with the per-array counts dropped. On `11B2` the IEQ,
graphic-EQ, audio-optimizer and regulator copies match the endpoint values.

**What each index reads as.** Evidence: *shape* is a blob's grid and layout;
*match* is the DAX3 registry-form preset below, whose value at that index
matches exactly one field of the XML; *assumed* is a switch read as the field
beside the values it gates.

| Index | Layout | DAX3 field | Evidence |
|---|---|---|---|
| 2 | int | `volmax-boost` | match |
| 6, 7 | int | `volume-leveler-in-target`, `-out-target` | match (both −320 in every preset read, so their order is untested) |
| 8 | int | `volume-leveler-amount` | assumed |
| 9 | int | `volume-leveler-enable` | assumed |
| 10 | int | `ieq-enable` | assumed |
| 12 | int | `ieq-amount` | match |
| 13 | 20-band grid 65…17326 Hz + 1 array | the IEQ curve | shape |
| 18 | 20-band grid 47…19688 Hz + 8 arrays | `audio-optimizer-bands` `ch_00`…`ch_07` | shape |
| 21 | 20-band grid + 1 array | `graphic-equalizer-bands` | shape |
| 22 | 20-band grid + 3 arrays | `regulator-tuning` `threshold_low`, `threshold_high`, `isolated_band` | shape |
| 24 | int | `regulator-speaker-dist-enable` | assumed |
| 25 | int | `regulator-relaxation-amount` | match |
| 26 | int | `regulator-timbre-preservation` | match |
| 35 | int | `dialog-enhancer-enable` | assumed |
| 38 | int | `dialog-enhancer-amount` | match |
| 42, 45, 46 | pair, 3-array, pair | `virtual-bass-src-freqs`, `-subgains`, `-mix-freqs` | match |

The regulator's two threshold arrays sit 192 (12 dB) apart in every band on
`11B2`. The Surface Laptop 4's DAX3 XML, `10EC123E`, has the same spacing
between its `threshold_low` and `threshold_high`. The IEQ grid is the one older
DAX3 XMLs, such as Lenovo's 530S package v220 (`xml_version` 3.2.0), list as
`band_20_freq fs_44100`.

**The key between the formats.** The DAX3 registry-form preset uses the same
property set, indices 0…59. Its distinctive values each match one field of the
headphone XML's `music` profile, which scores the most matches of its nine
profiles (21):

| Index | Value | XML field |
|---|---|---|
| 2 | 64 | `volmax-boost` |
| 12 | 8 | `ieq-amount` |
| 15, 16, 17 | 144, 100, 32 | `bass-enhancer-boost`, `-cutoff-frequency`, `-width` |
| 25 | 96 | `regulator-relaxation-amount` |
| 26 | 12 | `regulator-timbre-preservation` |
| 38 | 7 | `dialog-enhancer-amount` |
| 51 | 200 | `bass-extraction-cutoff-frequency` |

Its indices 9, 10 and 23 hold 1, and the `music` profile's `tuning-cp` has
three enables at 1: IEQ, regulator and volume leveler. Which index is which
does not follow from one preset, so the switches are read by adjacency.

**Counter-examples sought.** The layouts diverge in two places. DAX3's
registry form has no index 18 and adds a 232-value blob at 48; and it has no
28–30, where DAX2 holds 0, 10 and 200; the DAX3 form holds 200 at 51 and 10
at 56–58. The reader names index 18 by its own grid, not by the key, and maps
none of 28–30. Every single value read across the
13 DAX2 presets is one DAX3 XMLs also use ([fleet](#r-dolby-registry-fleet)).
So nothing contradicts reading DAX2's indices as the DAX3 registry form's.

The reading keeps the speaker's own correction and regulator, which every
preset holds as its own (13 distinct of 13). The IEQ curve is the same in all
13, so it isn't the device's own. It matches none of the voicing curves in the
corpus's DAX3 XMLs, and the run builds it as the Balanced voicing.

Rejected: finding the presets by the `DAX2Tuning_` section prefix. It reached
3 of the 18 codecs, since most codecs bind presets written under other section
names. The reader follows the binding instead.

Open:

- No Windows capture of a DAX2 device exists, so every mapping above is
  unvalidated, and the *assumed* switches most of all. On all 13 presets
  index 9 is 0, so the leveler reads off on every one.
- Index 47 (1 or 0) and the other unmapped indices are unread.
- `10EC120A` binds a DAX2 preset in Realtek 6.0.8828.1 and has a DAX3 XML in
  the Surface Laptop 4 Intel MSI. The two are not a pair to compare: the XML
  is flat (no correction, no regulator), and the DAX2 binding is a preset
  four other codecs share.
- `10EC11CE` binds a DAX3 registry-form *speaker* preset in
  `HDXHAPMRTKWOV.inf` and has no XML anywhere in the corpus. The reader
  refuses that form; reading it would be a separate format.

<a id="r-dolby-registry-fleet"></a>

## Which drivers ship a DAX2 registry preset

18 HD-Audio codec IDs bind a DAX2 speaker preset, all in Microsoft Surface
packages. They bind 13 distinct presets, through 14 distinct `.inf` files of
Realtek versions 6.0.1.7734 to 6.0.9249.2. Conditions: the newest Windows 10 or
11 MSI of 16 Surface models from Microsoft's download pages, plus the Surface
Laptop 3 Intel's Windows 10 MSI (issue #148), plus the 25-model
[Surface fleet](surface.md#r-surface-apo-fleet). Each `.inf` carrying the
property set was found by content, and every hardware ID in it was bound with
`lib/apo/dax2.py`. 2026-10-25. The corpus tier re-reads them all:
`tests/corpus/test_dax2_corpus.py`.

| Codec | Subsystems | Presets | Single values across them |
|---|---|---|---|
| ALC298 (`10EC0298`) | `1072 108A 108C 10AE 10C4 10CA 10CC 1104 1106 1146 1160 1170 118A` | 10 | volmax 144 or 160; timbre 8, 11 or 12; dialog enhancer off or on, amount 5 |
| ALC274 (`10EC0274`) | `11B2 11D2 11E6 11E8 120A` | 4 | volmax 96, 144 or 160; timbre 8 or 12; dialog enhancer off or on, amount 5 or 7 |

One preset is shared by both codecs (`2165fb65` in the survey, bound by
`1146 1160 118A 11D2 120A`), so the rows sum to 14. Four codecs changed preset
between driver versions, so the newest DriverVer decides. Read with the
[index names](#r-dolby-registry-preset), all 13 presets set `ieq-amount` 10,
relaxation 96, IEQ and regulator on, and leveler off.

Each single value is one DAX3 XMLs also carry, volmax 160 and timbre 11
included (text search over the working tree's XMLs, 2026-10-25).

The `.inf` files are `HDXSSTM.inf` and `HDXSSTM2.inf`, under `SurfaceUpdate/`
or, in the oldest packages, `Program Files/SurfacePlatformInstaller/…`. Of the
18 MSIs whose file list was kept, 16 show a `.inf` carrying the property set,
and in each of them Dolby's `DolbyDAX2APOv201.dll` sits in that `.inf`'s
folder.

Not found outside Surface. No `.inf` in the corpus's other driver packages
carries the property set. The download fleet's 37 Inno packages list no
`DolbyDAX2APO*` binary, and no `.inf` in its nine ASUS and Framework bundles
carries the property set, nested archives unpacked. Those are DAX3-era
drivers, so this says nothing about older ones from other OEMs.

Open:

- Which Surface model each subsystem ID is, beyond `10EC11B2`, the Surface
  Laptop 3 13.5" Intel (#148).
- Other OEMs' 2015–2019 drivers are unsurveyed.
