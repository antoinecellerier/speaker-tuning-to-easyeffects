# Vendor APO layers: HP's Sonitude presets

[reference.md](../../reference.md) covers what the converter emits, and
[vendor-apo.md](../vendor-apo.md) where the vendor APO layers stand.

<a id="r-sonitude-apo-presets"></a>

## HP's Sonitude presets: the Bang & Olufsen voicing with no Dolby file

On the HP ZBook 15 G6 (issue #140) the Windows B&O voicing is a Sonitude
(formerly Sound Research) APO preset. Its 265-point curve, applied as cuts only,
is within 1.00 dB rms of what the B&O switch toggles from 200 Hz to 12.5 kHz. On
a second device, an HP OMEN, the same reading is within 1.61 dB rms of its
published Windows response, where the curve as stored misses by 7.15. Below the
preset's high-pass corner, Windows cuts as a 4th-order Butterworth high-pass at
that corner. A second capture set confirmed a 4th-order Butterworth under
pre-registration, with its corner taken from the curve's own step (127 Hz in
Voice, where the field says 125). Taking the corner from the field came after
that test, and has not been scored on its own from 50 to 160 Hz. On the ZBook
Windows adds a layer that stays on with enhancements off, which no file read
holds. One listener has heard the Music build on the ZBook: "considerably
better… almost like Windows", with bass that wants to be "more dense and full".
By ear, Windows plays about 6 dB louder. Conditions: HP softpaq `sp161474.exe`
(Realtek HD Audio Driver, SHA-256 `a47d4827…`), Sonitude APO 2.1.1.99, codec
ALC285 on SOF, subsystem `103C860F`; the reporter's Windows 11 loopback
captures, `off` (Audio enhancements: Off) against `music` (B&O Audio Control
"Music"), volume 50 %, 2026-10-17. OMEN: `103C8A3F`, preset 70 of the same
package's ALC245 archive, scored against the impulse response
[hp-omen-bo-linux-audio](https://github.com/iphysicc/hp-omen-bo-linux-audio)
measured on Windows with Sound Research APO 2.0.11
(`linux/ir/hp-bo-speaker-ir-48k.wav`, SHA-256 `29103776…`).

**The package.** `SNAPO64Ext.inf` binds the device and copies 33
`SEAPODAT.<bus>.FUNC_01-VEN_<v>-DEV_<d>.zip` archives. No DAX3 XML ships.
Realtek's `RTAIODAT.DAT` has no record for `103C860F`
([database](../hardware-and-drivers.md#r-realtek-codec-database)).

| Container | Layout (fits every archive in the package) |
|---|---|
| `SSIDMAP.bin` | `SRSM`, u32 version, u32 count, u32 record size (20 or 32), then count × (u32 preset number, `SUBSYS_…` name) |
| `<N>.<codename>.txt` | plain-text manifest: device ID, then each `Preset.bin` and the `.srproject` it came from |
| `<N>/<endpoint>/EFX/<output>/<content>/Preset.bin` | header, a section table whose third entry points at the body, then stage blocks to the end of the file: i32 stage, i32 first and last key, then per key a u32 length and its bytes |
| speaker curve | two blocks of u16 265 + 265 float32 per preset (L, R; identical in 313 of the 318 `AnyContent` speaker presets) |

The stage-block walk ends exactly at the end of all 1,829 endpoint-0
`Preset.bin` files, every output and mode. Stage 5 keys 19 and 20 hold a flag
byte and a float32 corner in Hz per channel: on the ZBook, 80 Hz in Music and
Movie, 125 in Voice. It is on in 299 of the 318 `AnyContent` speaker presets,
at 40 to 232 Hz. Where the curve steps up by more than
6 dB, the step sits within 5 % of this corner in 243 of the 245 presets
that have both, so it reads as the speaker's high-pass.

`103C860F` maps to preset 177, `X_Venus`; its manifest names endpoint 0's
`AnyContent` file "Venus Music". That file holds a 10-band equaliser at 31 Hz to
16 kHz with every band at 0 dB. It reads as the B&O app's user equaliser, a
reading of its values that nothing confirms. The archives hold 1,348 distinct
subsystem IDs. 1,345 are HP's, six of them stored as `xxxx103C`. The local
corpus's XML filenames hold no `103C` ID at all, so it says nothing about HP's
Dolby use. From 100 Hz up, the endpoint-0 `AnyContent` headphone curves span
0.0 dB (median, n = 210) against 21.0 dB for the speaker ones (n = 318), as
"the curve is the EQ" predicts.

**The captures** are level-independent and identical across channels, so one
static curve describes them:

| Check | Result |
|---|---|
| L − R, 141 Hz–13.9 kHz | ≤ 0.002 dB, every stimulus |
| pink at −42, −18 and −14 dBFS RMS | music − off within 0.02 dB per 1/3 octave |
| sweep vs pink, music − off shape | within 0.21 dB |
| music − off at 1 kHz | −6.25 dB |
| `off` against the stimulus, relative to 1 kHz, pink at −18 dBFS | −36 dB at 31.5 Hz, −8.7 at 80, −4.9 at 500, +1.3 to +1.9 over 1.6–6.3 kHz, −7.8 at 16 kHz |
| `music` delay after `off` | 20.0, 22.4 and 21.3 ms at the three pink levels |

**The readings.** One broadband offset is fitted, then 1/3-octave bands are
scored. The first four rows were pre-registered on the ZBook alone (bands
160 Hz–12.5 kHz). `AnyContent` fits better than `Movie` and `Voice` under
every model:

| Model | ZBook vs music − off: rms / max (dB) | ZBook vs the full chain | OMEN IR, 200 Hz–12.5 kHz |
|---|---|---|---|
| flat | 3.96 / 7.9 | 5.40 / 13.3 | — |
| the curve on 10·2^(k/24) Hz | 1.22 / 3.2 | 2.25 / 5.0 | 7.15 / 18.3 |
| the 21 (Hz, Q, dB) triples as RBJ peaking | 1.16 / 3.4 | 2.17 / 5.0 | — |
| curve + triples | 5.33 / 11.3 | 4.68 / 11.8 | — |
| the curve, cuts only | 0.98 / 3.07 | 2.08 / 5.0 | 1.61 / 4.6 |

The cuts-only reading was proposed after the ZBook scores were seen. Its OMEN
score was pre-registered, with adoption at ≤ 2 dB rms and ≥ 2 dB better than the
curve; it passed. On the ZBook it changes little above 200 Hz (1.00 against 1.02
dB rms). It accounts for the bass: at 80, 100 and 125 Hz the curve leaves
Windows 8.8, 8.8 and 5.6 dB lower, and the cuts-only curve −0.4, +0.3 and +0.7
dB. The OMEN static rebuild that failed by 16 dB at 330 Hz is the curve's boost
there, +29.6 dB above 1 kHz against +12.3 measured, not a sum of the curve and
its triples. The triples read as a fit of the curve, and summing the two double
counts. Across the fleet the curve peaks above +6 dB in 247 of the 318
`AnyContent` speaker presets (median +9.8 dB), so reading the boosts would give
most of them a bass hump neither device's Windows plays. Below about 60 Hz
Windows cuts more than the curve: −12.5 dB at 31.5 Hz on the ZBook toggle, −64
dB on the OMEN response. The `off` layer is host-side processing, since WASAPI
loopback taps the audio engine's output; Sonitude's `.inf` registers its MFX and
EFX in no RAW mode for this device, and the sidecars show a spatial-audio CLSID,
though spatial audio was off.

**The build.** The minimum-phase FIR (4096 taps) is within 0.05 dB of the
target from 403 Hz up. Below that, the curve's 1/24-octave points sit closer
than the FIR's 11.7 Hz bins, and the build is within 1.9 dB of it. In the
loopback it sits 1.0 dB from Windows' level against the B&O switch, and 1.3 dB
against the full chain. That is not a level match at the speaker: B&O's
loopback carries the endpoint volume and `off`'s doesn't (the third capture
set, below). From 31.5 to 125 Hz it
leaves Windows −2.7, −2.2, −1.0, −0.5, −1.5, +0.4 and +0.7 dB from it against
the B&O switch; the cuts-only build before the high-pass reading left −12.9,
−8.2 and −4.4 dB in the bottom three bands. A live EasyEffects capture of the
build (null-sink route, 2026-10-20) matches its FIR within 0.01 dB per
1/3 octave at all three pink levels, and scores against Windows as the FIR
does.

**The second capture set** (the same laptop, 2026-10-19, volume 50 %, every
stimulus hash matching) recorded Voice, Movie and a level battery. Each test
was pre-registered before the recordings existed:

| Test | Pre-registered | Result |
|---|---|---|
| Voice − Music, 50–160 Hz | high-pass at each curve's break (Music 80 Hz, Voice 127 Hz): −16.0 dB at 50 Hz rising to 0 at 160; cuts only: +12.3 to 0 | −16.4 at 50 Hz to +1.3 at 160. High-pass within 0.97 dB rms (pink) and 0.80 (sweep), cuts only 17.5: adopted |
| Voice − Music, 200 Hz–12.5 kHz | cuts only supported at ≤ 1.0 dB rms and ≤ half the difference's own rms (2.23) | 1.67: inconclusive. The curve as stored: 1.44 |
| Movie − Music, 200 Hz–12.5 kHz | the same bar; the difference's rms is 1.01 | 0.68: inconclusive. The curve as stored: 0.55. Movie plays 2.3 dB louder |
| a level-dependent B&O stage | present if music − off moves > 1 dB with level | pink at −60 to −14 dBFS RMS: within 0.01 dB. Tones at −2 dBFS peak: up to 5.2 dB at 469 Hz, so present |

On the loud tones `off` loses 2 to 5 dB from 141 Hz to 11 kHz. Music loses
4.5 to 5.4 dB at 141–234 Hz and 1.5 to 1.9 dB at 2.2–2.6 kHz, and nothing
where B&O cuts its own output deep. A stage downstream of B&O that compresses
near full scale, present in both states, would produce both; that reading
came after the data, and the third set tests it. B&O Music also adds
harmonics to bass bursts, which `off` doesn't. At 50 Hz the third harmonic
matches the fundamental at −5 dBFS peak and sits 11 dB under it at −25 dBFS.
At 80 Hz it sits 18 and 29 dB under, and at 120 and 180 Hz there is none.
The preset's record group at 0x5c2–0x5e3 fits a psychoacoustic bass stage: a
mix gain (−10 dB in Music, −36 in Voice), an enable flag, a source frequency
(88 Hz in Music, 137.5 in Voice) and a band from twice that to 300 Hz.
Spatial audio was off for every capture of both sets.

**The third capture set** (the same laptop, 2026-10-20, every stimulus hash
matching, no clipped sample) recorded B&O Music at 30 % and 100 % volume, a
tone-level ladder and level steps in both states at 50 %, and Voice bass
bursts. Each test was pre-registered before the recordings existed:

| Test | Pre-registered | Result |
|---|---|---|
| Volume-dependent EQ, Music at 30 % and 100 % against 50 % | present if any band 63 Hz–12.5 kHz moves > 1 dB | absent: the shape moves within 0.01 dB (pink; the 100 % sweep shows ±0.7 dB ripple the pink doesn't) |
| One level-dependent stage common to both states, tones at 150 Hz–10 kHz, −34 to −2 dBFS peak | Music's loss is `off`'s at Music's own level, within 1.5 dB rms and 3 dB max | passes on n = 2 points: rms 0.36, max 0.49 |
| Music − off steady from −34 to −14 dBFS at 470 Hz, 1, 2.4 and 5 kHz | within 0.5 dB | fails as written: 1.1 to 1.2 dB at 470 Hz, 1 and 5 kHz |
| Level-step timing, 150 Hz and 2.4 kHz | Music and `off` settle alike; no slow tail in Music alone | mixed: 150 Hz settles alike (40 ms up, 20 ms down); at 1 kHz Music drifts −0.58 dB over about 2 s after the drop to −30 dBFS, and `off` doesn't |
| Voice's harmonics, from a model with two choices fitted on Music's bursts, given Voice's own fields, no refit | third harmonic within 6 dB at 50, 80 and 120 Hz, both levels; at most −85 dB at 180 Hz; odd harmonics only | fails as written: 120 Hz within 1 dB, 80 Hz within 3.7 dB; 50 Hz 10.5 to 12.8 dB too strong; 180 Hz −79.1 dB at the loud level. Odd only, as predicted |

The volume sits inside B&O's loopback. Against 50 %, Music's loopback moves
−7.50 dB at 30 % and +10.98 dB at 100 %, where the endpoint moves −7.49 and
+10.33 dB. `off`'s loopback (first set, 50 %) sits at the stimulus level, so
there the volume is applied after the tap. By ear on the ZBook, the preset needs
85 % (−4.2 dB) on Linux to match B&O at Windows' 50 % (−10.3 dB): about 6 dB. If
Windows leaves the hardware at full in the B&O state, B&O plays about 11.3 dB
over the cuts-only preset at the same volume: the fitted 0.99 dB plus the 10.33
dB of volume its captures carry. That condition is unmeasured: it needs `off`
captured at 30 % and 100 %. Every shape score fits one broadband offset, so none
moves. Nothing else on the Linux path processes: the reporter's speaker sink at
0.70 sets ALSA's Speaker control to −9.00 dB, Master and every SOF gain stage
sit at 0 dB, and no other filter chain runs.

On dynamics, B&O adds no level loss of its own above 0.5 dB up to −2 dBFS
at 50 % volume: Music's loss is what `off`'s would be at Music's own level,
within 0.49 dB. That rests on n = 2 tone-and-level points, the only ones
where either state loses more than 0.5 dB and Music's own level stays on the
ladder. At −2 dBFS `off` loses 1.8 to 5.3 dB at every tone, and Music loses
only where its own output is high. At the quiet end there is a small effect
of B&O's own, up to about 0.6 dB: on the two quietest rungs (−34 and
−30 dBFS) both states lose 0.5 to 1.2 dB and Music about 0.5 dB more, which
is what fails the third row. That row's 0.5 dB bar, like M_B below, rests on
a stage arrangement neither the file nor the captures establish.

The harmonic model's 120 Hz hit is the distinctive one: Music has no
harmonic there (−102 dB), and the model predicted Voice's from Voice's own
fields with no refit. Its shape is wrong elsewhere, and no harmonic stage is
built. Harmonic maps from the first two sets' sweeps and bursts (exploratory,
not pre-registered; nothing reaches the converter) show Music's third
harmonic growing about 3.4, then 1.7, then 1.5 dB per dB of input across the
steps −42, −25, −18 and −5 dBFS at 50 Hz, and within 0.2 dB of those at 80
Hz. Music's and Movie's third harmonic sits at −60 to −32 dB re input from
40 to 90 Hz, peaking at 70 Hz, and falls to −80 dB by 100 Hz. Voice's rises
from −123 dB at 40 Hz to −56 at 100 Hz and falls to −90 by 150 Hz. In Music
the fifth harmonic exceeds the third below about 45 Hz. `off` has none
(≤ −99 dB).

Movie and Voice ship as profiles, marked experimental, built as Music is:
each from its own curve and high-pass field. Movie plays 2.3 dB louder than
Music on Windows, and no field reading tested explains that step. Every
preset is peak-normalised, so the level between modes doesn't follow
Windows.

Rejected:

- Shipping a measured impulse response, as hp-omen-bo-linux-audio does. A
  capture is not a tuning field, and its look-ahead limiter adds latency.
- Applying the curve's boosts. Neither device's Windows output has them: the
  OMEN response is 7.15 dB rms from the curve and 1.61 from its cuts.
- M_B, a reading of further filter and gain fields in the preset,
  pre-registered partly blind on the second set. It rests on a stage
  arrangement neither the file nor the captures establish. It misses its bar:
  Movie − Music at 1.85 dB rms with no offset fitted, against 1.0.

Open:

- By-ear confirmation of Movie and Voice, which nobody has heard. One
  listener on the ZBook has heard Music twice. The cuts-only build was close
  to Windows, with less punch in the bass and quieter at the same slider
  position. The build with the high-pass was "considerably better… almost
  like Windows", with bass that wants to be "more dense and full". Windows'
  50 % is −10.3 dB at the endpoint, where PipeWire's cubic 50 % is −18.1 dB,
  so the slider positions don't compare.
- The psychoacoustic bass stage, the likely missing punch. Its fields are
  read above but not built: the converter's virtual-bass branch plays only
  through PipeWire. The second set's stepped tones at 47 and 81 Hz are held
  out to test a model of it.
- Windows' own decode of the preset. A registry search found no
  `SoundResearch` key under any account, only browser native-messaging hosts
  for `com.soundresearch.contentclassifier`.
- What the `off` layer is. It isn't Windows spatial audio, which was off
  throughout; HP processing outside the APO's modes remains.
- Whether Windows leaves the hardware at full volume in the B&O state,
  which sets how much louder B&O plays: it needs `off` captured at 30 % and
  100 %.
- The low end of a preset whose high-pass is on but whose curve has no step
  (54 of the 318 `AnyContent` speaker presets), where the converter applies
  the high-pass; of one whose curve steps but whose high-pass is off (11),
  where it keeps the stored curve; and of the 2 whose step sits well above a
  70 Hz corner, where it keeps the stored curve above the corner. No capture
  covers any of them.
- A third device, ideally one whose curve boosts the treble, for the
  cuts-only reading.
- The dynamics stages are unread. B&O's small effect at the quiet end and
  Movie's 2.3 dB level step over Music are unexplained.
- Which archive Windows reads when both an `INTELAUDIO` and an `HDAUDIO` one
  hold a subsystem ID with different curves. In `sp161474.exe`, 45 codec and
  subsystem-ID pairs (44 IDs) sit in both. Their curves differ bit for bit in
  31 pairs, but in 28 of those by at most 2.4e-7 dB, float rounding; 3 pairs,
  for 2 IDs, differ by 4.0 dB or more. `103C860F` is in the `INTELAUDIO` one
  only. The converter takes the bus from whether Linux runs SOF, a heuristic,
  and says so.
- A download that carries Dolby XMLs for some models and a Sonitude preset
  for this one takes the Dolby route, and never tries the preset.
