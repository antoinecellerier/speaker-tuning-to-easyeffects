# Cross-device DAX3 findings

> Interpretive analysis of the parameter schema used by Dolby DAX3 tuning XML (distributed publicly as part of Windows audio driver packages), for the purpose of Linux interoperability. No verbatim tuning arrays are reproduced.

The original cohort was **196 DAX3 tuning files** spanning **3 Realtek codec variants**
(ALC257, ALC285, ALC287) found in the `dax3_ext_rtk` and `fusion_ext_intel` driver
packages. A 2026-04-22 expansion pulled in four more Lenovo audio-driver packages
(`ext_lenovo_AIO_rtk`, `ext_thinkpad_AIO_rtk`, `ext_capg_thinkpad`,
`ext_amd_thinkpad_AIO`) for a total of **1050 tuning XMLs / 15551 profile rows**
spanning **9 Realtek codec variants plus SoundWire**. The README documents how the
script handles one specific device; this doc captures what's universal across the
ecosystem and what varies from device to device, so readers can judge which parts
of the pipeline are portable and which are tuned.

Original `dax3_ext_rtk` + `fusion_ext_intel` cohort (`dynamic` profile rows):

| Codec  | Devices | MBC enabled | MBC disabled |
|--------|---------|-------------|--------------|
| ALC257 | 236     | 19 (8%)     | 150 (64%)    |
| ALC285 | 27      | 0 (0%)      | 14 (52%)     |
| ALC287 | 81      | 4 (5%)      | 63 (78%)     |

Expanded 1050-XML cohort — XML count per codec (not dynamic-profile count):

| Codec family          | XMLs | Notes                                           |
|-----------------------|------|-------------------------------------------------|
| ALC257 (DEV_0257)     | 605  | Dominant, mostly Lenovo AIO-RTK package         |
| ALC287 (DEV_0287)     | 221  | Primary ThinkPad codec; dev-device family       |
| ALC235 (DEV_0235)     |  92  | New vs original cohort                          |
| ALC256 (DEV_0256)     |  37  | New                                             |
| ALC274 (DEV_0274)     |  26  | New — source of the rare PEQ type-6/8 filters   |
| ALC285 (DEV_0285)     |  26  | Same as original                                |
| ALC230 (DEV_0230)     |  24  | New                                             |
| SoundWire (`MAN_025D`)|  10  | New — includes one `SDW_` prefix variant        |
| ALC298/0887/0892/0897 |   9  | New, low count — desktop-style AIO codecs       |

> All files live under `internal_speaker` endpoints — no headphone or external
> tunings in either cohort. The new packages introduce non-`normal` operating modes
> (tablet/stand/tent/lid_close/etc., see §13) that the original 196-XML cohort did
> not exercise.

---

## 1. Universal constants

These parameters are **identical across every device and profile** examined:

| Parameter                          | Value              | Notes                              |
|------------------------------------|--------------------|------------------------------------|
| `volume-leveler-in-target`         | −320 (−20 dBFS)    | Script reads this correctly        |
| `volume-leveler-out-target`        | −320 (−20 dBFS)    | Script reads this correctly        |
| `regulator-relaxation-amount`      | 96                 | Script reads this correctly        |
| `mb-compressor-agc-enable`         | 0 (off)            | No device uses AGC mode            |
| `mb-compressor-slow-gain-enable`   | 0 (off)            | No device uses slow-gain mode      |
| `bass-enhancer-enable`             | 0                  | Never enabled on any device        |
| `virtual-bass-mode`                | 0                  | Never enabled on any device        |
| `graphic-equalizer-enable`         | 0                  | Never enabled on any device        |
| `volume-modeler-enable`            | 0                  | Never enabled on any device        |
| `pregain`                          | 0                  | Always zero                        |
| `postgain` (CP & VLLDP)            | 0                  | Always zero                        |
| `system-gain`                      | 0                  | Always zero                        |
| `calibration-boost`                | 0                  | Always zero                        |
| `dialog-enhancer-ducking`          | 0 (mostly)         | 98% of rows; 246/15551 are non-zero (8 or 6) in the expanded cohort — not universal |
| `regulator-overdrive`              | 0                  | Always zero                        |
| `peak-level`                       | 0                  | Zero on all but 1 device (−0.2 dB) |
| IEQ curve preset                   | `ieq_balanced`     | Only curve used anywhere           |

The script skips bass enhancer, virtual bass, graphic EQ, volume modeler, and
non-zero system/pre/post gains because none of them exist in the wild.

---

## 2. Multi-band compressor — the minority feature

Only **23 of 196 devices** (12%) enable the MB compressor on the `dynamic` profile.
This is the most important finding: **MBC is the exception, not the rule.**

| Profile              | MBC=1 | MBC=0 |
|----------------------|-------|-------|
| dynamic              | 23    | 227   |
| game                 | 23    | 227   |
| movie                | 20    | 230   |
| music                | 48    | 202   |
| voice                | 3     | 247   |
| voice_onlinecourse   | 0     | 250   |
| off                  | 0     | 250   |

Music profiles enable MBC more often (19%), suggesting MBC is used for loudness
maximisation on premium speakers, not as a universal safety feature.

### Band-count distribution (MBC-enabled profiles)

Expanded 1050-XML cohort, across all profiles where `mb-compressor-enable=1`:

| `group_count` | Enabled profiles | Disabled profiles   |
|---------------|------------------|---------------------|
| 1             |  294             | 9550                |
| 2             |  561             |  272                |
| 3             |  175             |  212                |
| 4             |  121             |  186                |

Two noteworthy wrinkles the old 2-band decoder masked:

- **294 profiles enable MBC with `group_count=1`** — single-band, full-spectrum
  dynamics. Almost entirely on the `music` profile, typically used as a loudness
  maximiser: ratios 1:1 to 2:1, threshold 0 dB to −6 dB, sub-millisecond attack
  and release (coeffs 10/20 in Q15 block-rate → 0.66/0.72 ms). Emitted from the
  `mbc-1band` experimental path since the guard was relaxed; LSP MBC accepts a
  single enabled band with no split frequency and bands 1-7 disabled.
- **398 profiles declare 3- or 4-band tunings but gate the compressor off**
  (`mbc_enable=0`). Dolby ships the coefficients anyway, so a future driver update
  that flips the enable bit would suddenly activate them. The N-band decoder
  handles this transparently; prior to commit `07612e9` it would have silently
  dropped bands above index 1.

Concrete examples reached by the N-band path:

- **voice profile, 3-band**: bands 1 (1313–7125 Hz) and 2 (7125+ Hz) both at 2:1
  above −12/−18 dBFS with +6/+9 dB makeup — speech-band compression the 2-band-capped
  decoder previously dropped.
- **music profile, 4-band**: all bands at 1:1 with per-band makeup ranging +1.2 to
  +2.9 dB — used as a 4-band makeup stage, not as a compressor.

The decoder was 2-band-only until commit `07612e9`. It now emits `group_count` bands
(capped at LSP MBC's 8-band ceiling).

### Compressor ratio diversity

Devices that do enable MBC show wide ratio variation:

| Ratio  | Threshold    | Makeup  | Devices |
|--------|--------------|---------|---------|
| 1.1:1  | −4.0 dB      | 2.0 dB  | 4 (gentle)     |
| 1.7:1  | −4 to −11 dB | 2–4 dB  | 8 (moderate)   |
| 2.0:1  | −5.3 dB      | 1.6 dB  | 4 (moderate)   |
| 5.0:1  | −3.0 dB      | 2.0 dB  | 1 (aggressive) |
| 10.0:1 | −4.0 dB      | 3.5 dB  | 4 (limiting)   |

The development device (ALC287 22E6) uses 1.7:1 @ −6.4 dB with 2 dB makeup — moderate.
The 10:1 devices are essentially using the compressor as a limiter.

For devices without MBC (88%), the regulator alone provides dynamics control, which
is a much simpler and safer signal chain.

---

## 3. Volume leveler amount — wide variation

The `vl_amount` parameter (0–10 scale) varies significantly across devices:

| Profile               | Distribution                                               |
|-----------------------|------------------------------------------------------------|
| **dynamic**           | 5 (51%), 4 (18%), 7 (14%), 3 (6%), 2 (9%), 1 (2%)          |
| **movie**             | 5 (51%), 4 (19%), 7 (14%), 3 (7%), 2 (6%), 1 (2%), 6 (0.3%)|
| **music**             | 2 (45%), 3 (18%), 1 (6%), 0 (24%), 4 (6%)                  |
| **game**              | 0 (94%), 2 (6%)                                            |
| **voice**             | 0 (99%), 2 (1%)                                            |
| **voice_onlinecourse**| 0 (99%), 2 (1%)                                            |

The development device uses `vl_amount=2`, which is on the **gentler end** for the
dynamic profile. The most common value across devices is **5** (51% of dynamic profiles).

---

## 4. Volmax-boost — the loudness ceiling

`volmax-boost` (1/16 dB, in tuning-cp) defines the maximum gain the volume leveler may
add above the output target. Distribution for the `dynamic` profile:

| Boost       | Devices      |
|-------------|--------------|
| 4 dB (64)   | 9            |
| 5 dB (80)   | 11           |
| **6 dB (96)** | **151 (77%)** |
| 7 dB (112)  | 2            |
| 8 dB (128)  | 22           |
| 9 dB (144)  | 1            |

**6 dB is the dominant value** (77% of devices). The development device also uses 6 dB.

Notable per-profile patterns:

- **voice**: 8 dB (43%) and 9 dB (25%) — highest boosts for speech intelligibility
- **voice_onlinecourse**: 4 dB (92%) — the gentlest, avoids pumping on long-form speech
- **music**: 6 dB (80%) with some at 3–4 dB
- **off**: 0 dB (99.5%) — effectively disabled

The voice profile's high boost combined with a disabled compressor means the regulator
alone has to catch peaks on that profile.

The script applies `volmax-boost` as `output-gain` on the regulator
(`multiband_compressor#1`), falling back to `limiter#0.input-gain` when the regulator
is absent. Can be disabled with `--disable volmax` if the boost drives the brick-wall
limiter into audible gain reduction on already-loud masters.

---

## 5. Dialog enhancer — profile-dependent behaviour

| Profile               | Enabled | Disabled |
|-----------------------|---------|----------|
| dynamic               | 86%     | 14%      |
| movie                 | 86%     | 14%      |
| music                 | 0%      | 100%     |
| game                  | 1%      | 99%      |
| voice                 | 72%     | 28%      |
| voice_onlinecourse    | 84%     | 16%      |

Dialog enhancer is a **speech enhancement feature**, consistently disabled for music
and game profiles across all devices.

### Dialog enhancer amount

| Profile    | Most common            |
|------------|------------------------|
| dynamic    | 5 (97%)                |
| movie      | 5 (97%)                |
| game       | 7 (99%) when enabled   |
| voice      | 3 (68%) or 8 (20%)     |
| personalize| 10 (94%)               |

---

## 6. Regulator distortion slope — limiting severity

The `regulator-distortion-slope` (1/16 scale) controls how hard the regulator limits:

| Slope        | Effective ratio       | Devices        |
|--------------|-----------------------|----------------|
| 4 (0.25)     | 1.3:1 — gentle        | 6              |
| 6 (0.375)    | 1.6:1                 | 4              |
| 8 (0.50)     | 2:1 — moderate        | 27             |
| 12 (0.75)    | 4:1 — firm            | 6              |
| **16 (1.00)**| **∞:1 — hard limiter**| **103 (53%)**  |

The development device uses slope=16 (hard limiter), which is the **most common**
setting. The hard limiter mode means the regulator acts as a brickwall at its threshold.

**Implication for pipeline design:** when slope=16 the regulator is a brickwall
limiter, so for 53% of devices the regulator *is* the brickwall limiter. The explicit
output limiter added to the EasyEffects chain is redundant on those devices and
essential on the remaining 47%. See `docs/design-notes.md` for why both exist.

---

## 7. Regulator thresholds — per-band frequency shaping

Each device has a unique 20-band regulator threshold curve. General shape of
`threshold_high`:

- **Range**: −60 dB to 0 dB across bands
- **Low bands** (sub-bass): deepest thresholds (−60 to −30 dB), protecting small
  laptop speakers from excursion damage
- **High bands**: typically 0 dB (no limiting)
- **Mid bands**: vary per device — the "speaker personality" region

There are **~80 distinct threshold patterns** across 196 devices — nearly every device
has a custom regulator curve tuned to its specific speaker characteristics. This is
the most device-specific parameter in the entire chain.

---

## 8. Audio optimizer — voice profile uses different curves

**97% of devices** use a **different audio-optimizer curve for the `voice` profile**
compared to `dynamic` / `movie` / `music` / `game` (which all share the same curve).

The voice AO curve typically:

- Reduces low-frequency correction (less bass boost)
- Adjusts mid-frequency emphasis for speech clarity
- Shares the same high-frequency rolloff

All other profiles (dynamic, movie, music, game, personalize) share identical AO
curves. The script processes each profile independently, so the voice preset
automatically picks up the voice-specific AO curve when generated from a device
that has one.

---

## 9. PEQ filters — mostly simple, occasionally complex

Across the expanded 1050-XML cohort (raw filter counts, all speakers, all profiles —
not the de-duplicated-per-device view of the original audit):

| Type | Description                          | Count (1050 cohort) | Script support          |
|------|--------------------------------------|---------------------|-------------------------|
| 1    | Bell/peaking EQ                      | 15432               | ✅ Yes                  |
| 9    | High-pass (with order)               |  3088               | ✅ Yes                  |
| 3    | **High-shelf** (with S parameter)    |  1754               | 🧪 Experimental         |
| 7    | High-pass variant (with order)       |  1016               | ✅ Yes                  |
| 4    | Low-shelf (with S parameter)         |   192               | ✅ Yes                  |
| 8    | Low-pass variant (with order)        |    22               | 🧪 Experimental         |
| 6    | Low-pass (with order)                |    10               | 🧪 Experimental         |

In the original 196-XML audit only types 1/4/7/9 were observed. The expanded cohort
surfaces three previously-unseen types, all now emitted via experimental paths:

### Type 3 — high-shelf filter (experimental)

```xml
<filter speaker="0" enabled="1" type="3" f0="2700" gain="2.000000" s="1.000000"/>
```

Same parameter shape as type 4 (`f0`/`gain`/`s`) but mirrored — gains are strictly
non-negative (range 0 to +15 dB across the corpus, no cut variants seen), and the
inflection is above `f0` rather than below. Present in **32 distinct XMLs** (1754
filters), centred around 2.7 kHz with +2 to +5 dB presence lift. Emitted via
`make_hishelf_band` (LSP `"Hi-shelf"` mode) with the same Q-from-S formula as
Lo-shelf (the formula is symmetric in shelf direction). Verified numerically by a
throwaway FFT script against the RBJ high-shelf cookbook formula; affected users
can turn it off with `--disable high-shelf` and are invited to report audibility.

### Types 6 and 8 — low-pass variants (experimental)

```xml
<filter speaker="0" enabled="1" type="6" f0="8000" order="4"/>
<filter speaker="0" enabled="1" type="8" f0="19500" order="8"/>
```

Same shape as types 7/9 (`f0`/`order`, no gain) but with the direction flipped —
type 6 appears at 8–10 kHz with order 4 (tweeter-guard rolloff on some ALC274
Lenovo laptops), type 8 at 8/19.5 kHz with order 4–8. Rare: only 3 XMLs carry type
6 (10 filters) and 2 XMLs carry type 8 (22 filters). Emitted via `make_lp_band`
(LSP `"Lo-pass"` mode), structurally a mirror of the already-verified HP path.
Turn off with `--disable lo-pass`.

### Types 1, 4, 7, 9 — supported

Type 1 and type 9 are the dominant filters in the corpus; types 4 and 7 are minority
but fully handled:

```xml
<filter speaker="0" enabled="1" type="4" f0="600"  gain="2.000000" s="1.000000"/>
<filter speaker="0" enabled="1" type="7" f0="100" order="4"/>
```

Type 4 maps to EasyEffects `"type": "Lo-shelf"` with Q derived from S via the standard
audio shelf formula. Type 7 is treated identically to type 9 (both are HP with order,
likely different filter topologies — Butterworth vs Linkwitz-Riley).

### Filters-per-speaker distribution

Most devices have 1–3 filters per speaker (typically a high-pass plus one or two
bells). The complexity ceiling in the expanded cohort is ~7–8 filters per speaker on
a few Lenovo AIO-RTK tunings — still comfortably below the LSP PEQ 32-band ceiling.

---

## 10. IEQ amount — nearly always maxed

| Profile     | IEQ=10 | Other     |
|-------------|--------|-----------|
| dynamic     | 96%    | 4/6 (4%)  |
| movie       | 100%   | —         |
| music       | 87%    | 3–8 (13%) |
| game        | 100%   | —         |
| voice       | 100%   | —         |
| off         | 100%   | —         |

All devices use `ieq_balanced` as the preset. The IEQ amount scales the intelligent
EQ curve (room correction); music profiles occasionally reduce it. The near-universal
IEQ=10 means the full curve should be applied in most cases.

---

## 11. MI steering — dynamic profile only

The `mi-dv-leveler-steering-enable=1` parameter appears **exclusively on the `dynamic`
profile** across all devices that have it. This confirms it's a deliberate choice to
add Media-Intelligence-driven gain hold only for the "adaptive" profile.

This is the key feature that the EasyEffects pipeline cannot replicate: without
content analysis the autogain has no way to know when silence is "real" silence vs a
quiet passage that will resume loud. This is the root reason the script bypasses
autogain by default — see `docs/design-notes.md` for the full rationale.

---

## 12. Defensive paths verified inert against the corpus

Dolby's XML schema permits more variation than any shipping device exhibits.
The parser includes defensive handling for several of these cases, but none of
them currently fire on the observed corpus (audited 2026-04-22, 372 XMLs across
`dax3_ext_rtk` plus the IdeaPad-3-17ABA7 sub-corpus). Listed here so future
readers know the defensive code is intentional — and what would have to change
in the wild for each path to become reachable.

| Code path                                 | Defensive behaviour                                                     | Corpus check                                                                       | Trigger condition                                              |
|-------------------------------------------|-------------------------------------------------------------------------|------------------------------------------------------------------------------------|----------------------------------------------------------------|
| Default profile (no `--profile` flag)     | `parse_xml` picks `endpoint.find("profile")` (first child)              | 217/217 internal_speaker/normal endpoints have `dynamic` first                     | XML where `off` or another no-op profile precedes `dynamic`    |
| Asymmetric L/R PEQ filter counts          | Missing-channel HP slot fills with 100 Hz/24 dB-oct HP, bell slot with flat 1 kHz bell | 1440 PEQ profiles → 0 with asymmetric HP counts; 1 with any L/R filter-count diff | Per-driver tuning where one channel has filters the other lacks |
| Asymmetric L/R PEQ peak gain              | Output-gain compensation uses global `max(L,R)` peak                    | 509 profiles with positive PEQ gain → 0 with L≠R peak                              | Per-channel boost values diverging                             |
| Empty `regulator-tuning/threshold_high`   | Falls back to `[0.0]*20` (no limiting), volmax still routes via regulator | 0 regulator-enabled profiles with missing/empty inline threshold and no `preset=` ref | Hand-edited XML or future driver release with broken regulator tuning |
| Shelf filter with explicit `q` attribute  | Output-gain compensation now uses full shelf gain (commit `c505864`)    | 192 type-4 shelf filters → 0 with explicit `q`                                     | Driver release that adds `q` to a shelf — previously silently under-compensated |
| `is_soundwire` filename detection         | Falls back to HDA mode (no bass enhancer, no convolver headroom restore) | All matched XMLs in the corpus have `SOUNDWIRE_…` or `SDW_…` filenames intact      | User manually renames a SoundWire XML before passing it in     |
| `make_multiband_compressor` 5+ band cap   | `min(group_count, 8)` enforced                                          | Max observed `group_count` = 4 (Dolby schema only allocates `band_group_0..3`)     | Dolby schema extension                                         |

**Now reachable on the expanded 1050-XML cohort** (formerly inert in the original
196-XML and 372-XML audits — listed here for symmetry, but these are no longer
defensive-only paths and should be treated as implementation gaps):

| Code path                                 | Current behaviour                                                        | Expanded-cohort check                                                              | Status                                                          |
|-------------------------------------------|--------------------------------------------------------------------------|------------------------------------------------------------------------------------|-----------------------------------------------------------------|
| Unknown PEQ filter type (`ftype not in (1,3,4,6,7,8,9)`) | Warns "unknown PEQ filter type N, skipping" and drops the filter | No observed filter outside the supported set on the 1050-XML cohort            | Inert again — types 3/6/8 are now emitted (see §9); the warning remains as a guard against future driver releases adding new types |
| 1-band MBC (`group_count=1`)              | Emits LSP `multiband_compressor` with band 0 active (no split frequency) and bands 1-7 disabled; `mbc-1band` experimental marker added to the end-of-run callout | 294 profiles enable MBC with `group_count=1` (§2), dominated by the `music` profile using 1-2:1 ratio with fast attack/release as a loudness maximiser | Experimental — reproduced from the Dolby tuning but not yet audibly validated. `--disable mbc` turns it off. |
| Non-zero `dialog-enhancer-ducking`        | Not currently read by the script (irrelevant on present pipeline)        | 246/15551 rows have ducking=6 or 8 (§1)                                            | Informational — no downstream consumer, but the "always 0" invariant claim was too strong |

If a future driver release breaks any of the truly-inert assumptions, the script will
silently produce a degraded preset rather than crash. The corpus audit is
reproducible with the python snippets in the message body of commit `07612e9`
(MBC band counts) and via the H2/M2/L1 snippets in this file's git history.

Two "by-design" behaviours that look like bugs but aren't:

- The SoundWire convolver applies `peak_db * 0.5` as `output-gain`, intentionally
  letting peak frequencies exceed 0 dBFS so the brick-wall limiter shapes them
  back. Restores half of the headroom that pure peak-normalisation would lose
  for the IEQ-only (no-AO) curve.
- The PEQ output-gain compensation deliberately ignores high-pass and negative-gain
  filters: HP slots reduce headroom requirements (cuts only), and shelves/bells
  with negative gain don't add headroom pressure.

---

## 13. Endpoint operating modes and profile variants (expanded cohort)

The original 196-XML cohort only exposed `operating_mode="normal"` endpoints with
the six canonical profile types (`dynamic`/`movie`/`music`/`game`/`voice`/`off`).
The Lenovo-AIO-RTK and ThinkPad-AIO-RTK packages added in the 2026-04-22 expansion
exercise both axes significantly further.

### Operating modes

| `operating_mode`        | Rows  | Typical hardware                                       |
|-------------------------|-------|--------------------------------------------------------|
| `normal`                | 9868  | All laptops — the mode selected by default             |
| `laptop`                | 1306  | Convertible in clamshell pose                          |
| `stand`                 | 1296  | Convertible in stand/present pose                      |
| `tablet`                | 1288  | Convertible folded flat                                |
| `tent`                  | 1278  | Convertible in tent pose                               |
| `lid_close`             |  219  | Lid-closed external-monitor use                        |
| `detachable_speaker`    |  140  | Detachable tablet-with-dock SKUs                       |
| `hybridaudio_detached`  |   20  | Same family, detached-speaker path                     |
| `book`                  |   20  | Book-pose convertibles                                 |
| `flat`                  |   20  | Flat-on-desk orientation                               |

The script only ever reads `operating_mode="normal"` (the `--mode` default). On
convertibles, Dolby ships distinct tunings per hinge pose — the "normal" fallback
is fine for the clamshell case, but users of Yoga-class devices would need
`--mode tablet|stand|tent` to pick up the pose-specific tuning. The CLI already
exposes `--mode`; no script change needed, but the README could call this out.

### Profile types

The canonical Dolby profile vocabulary expands beyond the six listed in the
original cohort:

| Profile             | Rows | Notes                                                 |
|---------------------|------|-------------------------------------------------------|
| `dynamic`           | 1599 | Primary listening profile                             |
| `movie`             | 1599 |                                                       |
| `music`             | 1599 |                                                       |
| `voice`             | 1599 |                                                       |
| `off`               | 1599 | No-op pass-through                                    |
| `game`              | 1576 |                                                       |
| `personalize_user1` | 1576 | User-customisable slot 1 (not the `personalize` alias) |
| `personalize_user2` | 1576 | Slot 2                                                |
| `personalize_user3` | 1576 | Slot 3                                                |
| `voice_onlinecourse`| 1137 | Ultra-gentle leveler profile (§4)                     |
| `game_shooter`      |   23 | Genre-specific game profile                           |
| `game_racing`       |   23 |                                                       |
| `game_rpg`          |   23 |                                                       |
| `game_rts`          |   23 |                                                       |
| `personalize`       |   23 | Legacy single-slot personalize (pre-user1/2/3 schema) |

The `personalize_user{1,2,3}` slots are Dolby-provided starting tunings meant to be
reshaped via the Dolby Access Windows app. In the shipped XML they carry real
Dolby tunings, not empty slots — `--profile personalize_user2` is a legitimate
preset source. The `game_{shooter,racing,rpg,rts}` variants appear only on a
small subset of ThinkPad AIO-RTK devices; all share the outer `game` tuning
shape with per-genre tweaks to surround-boost and dialog handling.

`--list` already reports whatever profile names the XML declares, so users pick
these up naturally. `--all-profiles` iterates every one and generates
`Dolby-{ProfileName}-{IEQ-variant}` presets for each.

### Corpus-shift caveats on prior distributions

A few other distributions shifted noticeably under the expanded cohort — record
the direction without redoing every table:

- **Music-profile MBC enable** jumped from 19% (original) to **44%** (expanded).
  Lenovo's AIO-RTK tunings enable multi-band loudness maximisation more
  aggressively than the ThinkPad-only original cohort.
- **Regulator distortion slope** shifted from 53% hard-limiter (slope=16) to
  **95%** in the expanded cohort. The per-XML table in §6 still reflects the
  original 196-file breakdown; the design implication — that the output brickwall
  limiter is redundant on slope-16 devices but essential on softer slopes —
  stands, but the "soft-slope minority" is smaller than the original 47%.
- **Voice-profile volmax-boost** is more polarised than §4 reported: 45% at 6 dB,
  24% at 9 dB, 23% at 8 dB (in the expanded cohort), rather than the original
  "8 dB (43%), 9 dB (25%)" picture.

---

## Interesting observations

1. **No Intel Fusion devices found** — the `fusion_ext_intel` driver package shares
   the same XML files as `dax3_ext_rtk`, suggesting Intel SST-based Dolby uses
   identical tuning to Realtek-based Dolby.

2. **Music profiles are the MBC outlier** — 19% enable MBC on music vs 9% on dynamic,
   suggesting MBC is primarily a loudness tool, not a protection feature.

3. **voice_onlinecourse is the safest profile** — 0% MBC, 0% VL amount, 4 dB volmax,
   simplest chain. Dolby's own tuning for speech entirely disables the compressor and
   uses the gentlest volume leveler. A good template for a "no artifacts" preset.

4. **Hard limiting (slope=16) is the majority** — Dolby engineers prefer true brickwall
   limiting on the regulator for most laptop speakers.

5. **Every device has a unique regulator curve** — there's no "one size fits all"
   threshold pattern, confirming these are individually tuned per speaker.
