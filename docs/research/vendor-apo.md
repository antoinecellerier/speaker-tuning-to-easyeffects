# Vendor APO layers: speaker voicing that ships outside the DAX3 XML

## Where this stands

[reference.md](../reference.md) covers what the converter emits.

- **Surface Pro 9 (Intel) voices its speaker in Microsoft's Surface APO**, not
  in Dolby. Its DAX3 XML switches off its audio-optimizer, IEQ and graphic EQ
  in every profile ([Surface APO](#r-surface-apo-efx)).
- **The vendor-apo layer** translates that config's `R/EFX` chain. It is on by
  default where a bound config has a speaker chain, and unvalidated: one
  listener has heard it and no Windows capture exists for any Surface
  ([Surface APO](#r-surface-apo-efx)).
- **Eleven of the 25 surveyed Surface models ship a Surface APO endpoint
  chain**, on HD-Audio and SoundWire speakers. The parser reads all 16 of their
  configs; on 3 it leaves `Crystal` out. Every Dolby XML one of them binds has
  its voicing off ([Surface fleet](#r-surface-apo-fleet)).

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

<a id="r-surface-apo-efx"></a>

## Microsoft's Surface APO: the endpoint chain after Dolby

The Surface Pro 9 (Intel) speaker voicing lives in `SurfaceAPO_1284.json`,
which Windows runs as the endpoint effect after Dolby. Conditions: package
`SurfacePro9_Win11_22631_26.091.15297.0.msi` (SHA-256 `7913b90e…`), Realtek
ALC274, `SUBSYS_10EC1284`; read 2026-10-06; issue #113.

**The DAX3 XML switches its voicing off.** In all 10 profiles of
`DEV_0274_SUBSYS_10EC1284_PCI_SUBSYS_72708086.xml`, `tuning-cp` has
`audio-optimizer-enable` 0 with zero bands, `ieq-enable` 0 and
`graphic-equalizer-enable` 0. The converter still applies the IEQ at its 10%
floor. The regulator thresholds are all 0 dB high and −12 dB low
(`array_20_n192`). The converter emits no regulator in 6 of the 9 profiles
other than `off`. In `personalize_user1`–`3` it emits a live limiter at
0 dBFS. The volume leveler is enabled in those 9 profiles, and an HDA preset
ships it off unless `--enable autogain`. 18 of the package's 25 XMLs have the
audio-optimizer, IEQ and graphic EQ off in every profile, counted with
`xml.etree` over `tuning-cp`.

**The binding.** `SurfaceAPOExtension.inf` binds
`HDAUDIO\FUNC_01&VEN_10EC&DEV_0274&SUBSYS_10EC1284` (and its `INTELAUDIO\`
form) to an install section. That section's AddReg sets the property
`{c1f75c4c-3243-11ea-850d-2e728ce88125},0` (`PKEY_SurfaceApoConfigFilename`)
to `%13%\SurfaceAPO_1284.json`. The `lib/apo/surface.py` finder follows that
path through the `.inf`, not the filename.

**The order.** The speaker's Realtek driver, `EHDXSSTMD3A4.inf`, gives the
speaker topology three APO presets. `SysFx` puts Dolby's APO in the stream
slot (SFX), and `SAFApo_SPK3` puts the Surface APO in the mode and endpoint
slots (MFX, EFX). Windows runs stream, then mode, then endpoint effects, so the
Surface chain processes Dolby's output.

**The R/EFX blocks, in file order** (48 kHz; L and R identical in this
file):

| Block | Content | Translation |
|---|---|---|
| `VolumeControl` | per-channel gain, `UseDependentGain` | none |
| `MainEQ` | 16 biquads (b0, b1, b2, a1, a2), L/R interleaved; 5 active per channel | folded into the FIR |
| `VolumeDepLS`, `VolumeDepHS` | one shelf per channel for each of 10 volume states | none |
| `VolumeDepMBDRC4` | crossovers 100/600/2750 Hz; 10 identical states | `multiband_compressor#2` |
| `Crystal` | 5 resonances: F0 160–844 Hz, bandwidth 120–300 Hz, Limit −2.7 to −18.5 dB | `multiband_compressor#3` |
| `OutputLimiter` | threshold 0 dB, ratio 1, pregain 0, 10 ms look-ahead | none; `limiter#0` stays last |

The `R/MFX/*` per-mode EQs are identity in the DEFAULT, RAW, COMMS, MOVIE and
MEDIA modes, at both 48 and 44.1 kHz. Only NOTIFICATION is not.

**MainEQ's response**, computed from the coefficients with
`scipy.signal.sosfreqz`:

| Hz | 30 | 50 | 80 | 300 | 1000 | 2000 | 3000 | 4000 | 6000 | 8000 | 12000 | 20000 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| dB | −18.8 | −6.0 | −1.2 | 0.0 | −0.5 | −3.3 | −7.4 | −9.7 | −2.7 | +1.4 | +3.4 | +3.0 |

All five active sections have their poles inside the unit circle and their
zeros inside or on it, to coefficient precision. Rounding splits the double
zero at DC in one high-pass section into a reciprocal pair at |z| = 1.00014
and 0.99986. The cascade is therefore minimum-phase above about 1 Hz, so a
minimum-phase FIR of the same magnitude reproduces its phase as well.

**The volume-dependent shelves** were evaluated the same way, at 50 Hz and
10 kHz:

| State | 0 | 1 | 3 | 5 | 7 | 9 |
|---|---|---|---|---|---|---|
| LS @ 50 Hz | 0.0 | +1.5 | +4.5 | +7.5 | +12.4 | +17.1 |
| HS @ 10 kHz | +0.9 | +1.8 | +3.6 | +5.3 | +7.0 | +8.8 |

`ValueTable` holds 9 steps from −2.75 to −24.75 dB, which would select among
the 10 states.

**Folding MainEQ into the FIR** needed a floor. The FIR is 4096 taps; the
errors below were graded on a 32768-point grid from 20 Hz to 20 kHz, with
numpy and scipy, 2026-10-06. "Own target" grades each design against the
floored curve it was asked for; "raw MainEQ" grades it against the unfloored
cascade:

| Target | Worst vs own target | Worst vs raw MainEQ | Within 0.5 dB of raw from |
|---|---|---|---|
| raw MainEQ | 14.8 dB at 53 Hz | 14.8 dB at 53 Hz | — |
| hard clamp 40 dB under the peak | 1.00 dB at 21 Hz | 1.00 dB at 21 Hz | 22 Hz |
| smooth floor, 40 dB under | 0.32 dB at 21 Hz | 1.42 dB at 21 Hz | 23 Hz |
| smooth floor, 50 dB under | 1.32 dB at 21 Hz | 1.19 dB at 21 Hz | 66 Hz |
| smooth floor, 60 dB under | 1.55 dB at 21 Hz | 1.54 dB at 21 Hz | 54 Hz |
| smooth floor, 40 dB under, 8192 taps | 0.10 dB at 21 Hz | not measured | not measured |

The high-pass's null at DC is what the cepstral design aliases. The converter
ships the smooth 40 dB floor at 4096 taps (`FIR_TARGET_RANGE_DB`).

**The dynamics mappings are hypotheses.** The DRC's per-band `PreGainDb`
becomes the same gain on the detector and the makeup, which is algebraically
pre-gain-then-compress. Neighbouring bands with ratio 1 and no pregain are
merged. Each
Crystal resonance becomes a limiter band with its detector narrowed to
F0 ± Bandwidth/2, split at the geometric midpoints between F0s. "Limit"
could be a level ceiling or a maximum cut, and the file holds no separate
threshold. Ratio 100, knee 0 dB, RMS detection for the DRC and Peak for
Crystal are converter choices; the config states none of them.

**Heard on one device.** The #113 reporter ran the layer on this Surface Pro 9
(Intel) and reported, 2026-10-10, that it "creates much better tuning". The run
they pasted is `dolby_to_pipewire.py`, so it was heard on the PipeWire chain;
the EasyEffects preset is unheard. They have no Windows install. The report
names no reference, most likely the preset they ran before without the layer. On
that report the layer became the default wherever a bound Surface config has a
speaker chain; the mappings stay unvalidated.

Open:

- `VolumeControl` sits first in the chain, so on Windows the dynamics likely
  see the signal after the system volume. EasyEffects and the PipeWire chain
  run before the speaker's volume, so their dynamics behave like Windows at
  full volume, read as state 0. Even there Windows' high shelf adds +0.9 dB at
  10 kHz, which the preset leaves out.
- The volume-dependent shelves are not reproduced. Neither routing lets a
  filter see the speaker volume (ee-to-pipewire.md "Smart-filter routing").
  If listeners report "thin at low volume", the candidate is a PipeWire-only
  follower that sets the shelf state from the speaker volume.
- Crystal's semantics, the DRC's level reference, crossover type,
  `OutputLimit` and the 50–100 ms hold times are unknown.
- `surfacedspextension/saflibadl.bin` (the Intel DSP side of the EFX, whose
  proxy the APO names), `RenderHeadroom=3` in that package's `.inf`, and
  Realtek's `RTAIODAT.DAT` are opaque. Whether they add processing is unknown.
- The fold puts MainEQ before Dolby's dynamics; Windows runs it after them.
  That is exact while the preset carries no Dolby dynamics: the default HDA
  run in the 6 profiles without a regulator. It is not exact with
  `--enable autogain`, or in `personalize_user1`–`3`.
- `make_fir` normalises the folded FIR to its peak, so the level reaching the
  vendor DRC's thresholds is not the level Windows feeds it.
  `--enable level-restore` gives the peak back; which matches Windows is
  unmeasured.
- On the PipeWire chain, `--enable virtual-bass` sums its wet branch after the
  whole chain, so Dolby's virtual-bass harmonics skip the vendor EQ and
  dynamics. On Windows the Surface EFX processes all of Dolby's output.
- The channel order of the interleave is unread: L and R are identical here.

<a id="r-surface-apo-fleet"></a>

## The Surface fleet: which models ship a Surface APO chain, and its shapes

Conditions: the newest Windows 11 driver MSI of 24 x86 Surface models on
Microsoft's download pages (the Surface Go 2 lists none), plus the Surface Pro
9 package from [Surface APO](#r-surface-apo-efx); extracted with `msiextract`
and read 2026-10-06. Each config was bound with `lib/apo/surface.py`'s finder
against the Dolby XMLs in the same MSI. Not surveyed, though their pages list
a Windows 11 MSI: the Book 2, Laptop Go, Pro (5th gen), Pro 6, Studio 2,
Studio 2+ and Hub 3/Hub 2S.

**12 of the 25 models ship a `SurfaceAPO_*.json`.** The other 13 (Book 3,
Go 3 and 4, Laptop 3 and 4 in their AMD and Intel editions, Laptop 5, Laptop
Go 2 and 3, Laptop Studio, Pro 7 and 7+) ship none. Every one of the 12 binds
through `SurfaceAPOExtension.inf`, in a folder named `surfaceapoextension/`,
`apoextension/`, `APOExtension/` or `surfacerender_ext/` depending on the MSI.

| Model | Configs | Bus | Shape, beside the Pro 9's | Translated |
|---|---|---|---|---|
| Pro 8 | 122E, 1248 | HD-Audio | 8-band `VolumeDepMBDRC` with five crossovers at 22.6–23 kHz; `Crystal` lists 10 entries, each resonance twice in a row, F0s unsorted | DRC as its 3 bands below 20 kHz; `Crystal` as 5 resonances |
| Pro 9 (Intel) | 1284 | HD-Audio | (the reference) | all of [Surface APO](#r-surface-apo-efx)'s mappings |
| Pro 10 for Business | 12D2 | HD-Audio | same block set | same |
| Pro 10 with 5G | 1332 | HD-Audio | same block set | same |
| Pro 11 (Intel) | 3070 | SoundWire | `VolumeDepPN`; the config is set through the `.inf`'s AddInterface sections only | same |
| Pro 12 (Intel) | 3086, 308c | SoundWire | `MainEQ1` and `MainEQ2`; `VolumeDepPN`; AddInterface only | both EQs, cascaded in file order |
| Laptop 6 for Business | 1286, 1288 | HD-Audio | `Crystal1` (5 active) and `Crystal2` (1 active, `ActiveLimiterCount`) | `Crystal` as 6 resonances |
| Laptop 7 (Intel) | 3072, 3074 | SoundWire | `VolumeDepPN`; `Crystal1` (5 active) and `Crystal2` (2 active) | `Crystal` left out: 7 resonances |
| Laptop 7 5G for Business (Intel) | 307A | SoundWire | as the Laptop 7 | as the Laptop 7 |
| Laptop 8 (Intel) | 3088, 308A | SoundWire | `VolumeDepPN`; 308A: per-band DRC output limits, `Crystal` F0s unsorted with resonances at 9 and 12 kHz | same; 308A's output limits are left out |
| Laptop for Business 13" 1st Ed. (Intel) | 3090 | SoundWire | `VolumeDepPN` | same |
| Laptop Studio 2 | 1282 | HD-Audio | no `R/EFX` chain; its `R/MFX` EQs are identity except NOTIFICATION | no layer |

"Same" means the Pro 9's translation; `VolumeDepPN` is a volume-dependent EQ,
left out with the shelves. One unvalidated reading weighs more here than on
the Pro 9: its DRC has 10 identical volume states, but 12 of the 16 configs
have states that differ, so reading state 0 as full volume decides their
compressor settings. A SoundWire XML is named after the speaker
function's manufacturer, function and subsystem IDs, and its hardware ID
carries all three. Each config binds exactly one XML in its own MSI, and the
corpus tier's vendor test folds, builds and converts all 16 pairs.

**Every XML bound to an endpoint chain has its voicing off:** 16 of 16
(`audio-optimizer-enable`, `ieq-enable` and `graphic-equalizer-enable` 0 in
every profile). The Laptop Studio 2, whose config has no endpoint chain, ships
an XML with IEQ on in its `dynamic` profile. No fleet XML has all three on. That
reads as Microsoft moving each speaker's voicing out of Dolby into its own APO,
an inference: the converse doesn't hold, since 30 of the 41 content-distinct
XMLs in the 24 surveyed MSIs have their voicing off, including ones no surveyed
config binds.

Open:

- `Crystal` with more than 6 resonances (the Laptop 7 configs) fits no single
  8-band stage. A limiter stage per `Crystal` block would carry it, which
  needs a third vendor stage slot beside `multiband_compressor#2` and `#3`.
- `VolumeDepPN`'s filter type is unread; it is volume-dependent, so it is out
  of reach either way.
- L and R differ in 10 of the 16 configs' EQs, so the interleave's channel
  order matters there, and it is still unread.
