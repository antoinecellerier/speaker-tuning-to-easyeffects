# Cross-device DAX3 findings

> Interpretive analysis of the parameter schema used by Dolby DAX3 tuning XML (distributed publicly as part of Windows audio driver packages), for the purpose of Linux interoperability. No verbatim tuning arrays are reproduced.

This doc captures what's universal across the ecosystem and what varies from
device to device, so readers can judge which parts of the pipeline are portable
and which are tuned. [`reference.md`](reference.md) documents how the script
maps one specific device.

The current cohort is **2795 tuning XMLs / 40732 profile rows**, spanning 14
HD-Audio codec DEV IDs (mostly Realtek ALC) plus SoundWire/SDW. The original
cohort was 196 DAX3 tuning files spanning 3 Realtek codec variants (ALC257,
ALC285, ALC287), found in the `dax3_ext_rtk` and `fusion_ext_intel` driver
packages. Successive driver-package pulls grew it: `ext_lenovo_AIO_rtk`,
`ext_thinkpad_AIO_rtk`, `ext_capg_thinkpad`, `ext_amd_thinkpad_AIO`, plus newer
IdeaPad/SoundWire packages. [`corpus.md`](corpus.md) describes what that
collection is made of, and which OEM driver package each part came from.

> **All figures below are from the 2795-XML cohort audited 2026-08-03**, except
> where a passage is explicitly marked as the original 196-XML cohort, or
> carries its own later re-derivation date. The original-cohort passages are
> kept for the historical methodology notes.
> [`tools/corpus_audit.py`](../tools/corpus_audit.py) regenerates any aggregate
> here. Since 2026-08-03 its committed sweep includes the §12 and §14 checks,
> which were ad-hoc queries before.

> **The file counts here predate a filter fix (2026-08-09) and are known to be
> off.** The sweep tool used its own filename test rather than the converter's.
> It counted `_dmic`/`_amic` microphone companions as tunings, and skipped the
> `HDAUDIO_`/`INTELAUDIO_`/`PCI_`/`AUCD_` spellings of real ones. The file,
> device and package counts need a re-derivation pass. Row-based distributions
> move only by what the added devices contribute. See
> [`corpus.md`](corpus.md#reconciling-the-counts).

Original `dax3_ext_rtk` + `fusion_ext_intel` cohort (`dynamic` profile rows):

| Codec  | Devices | MBC enabled | MBC disabled |
|--------|---------|-------------|--------------|
| ALC257 | 236     | 19 (8%)     | 150 (64%)    |
| ALC285 | 27      | 0 (0%)      | 14 (52%)     |
| ALC287 | 81      | 4 (5%)      | 63 (78%)     |

> **Known inconsistency (review 2026-06):** this original-cohort table's columns
> don't reconcile. Per row, enabled + disabled ≠ Devices (169/236, 14/27,
> 67/81). The Devices column sums to 344 against the 196 files above. The
> per-codec *enabled* counts (19/0/4 → 23 total) match the original §2 and are
> the load-bearing numbers. The Devices and disabled columns need a re-count
> against the original package set before being relied on. §2 below gives the
> current-cohort MBC rates.

Current 2795-XML cohort, XML count per codec (not dynamic-profile count):

| Codec family          | XMLs | Notes                                              |
|-----------------------|------|----------------------------------------------------|
| ALC257 (DEV_0257)     | 1576 | Dominant, mostly Lenovo AIO-RTK packages           |
| ALC287 (DEV_0287)     |  540 | Primary ThinkPad codec; dev-device family          |
| ALC235 (DEV_0235)     |  236 |                                                    |
| ALC285 (DEV_0285)     |   98 | Includes the original cohort                       |
| ALC230 (DEV_0230)     |   81 |                                                    |
| ALC256 (DEV_0256)     |   79 |                                                    |
| ALC274 (DEV_0274)     |   72 | Carries the rare PEQ type-6 low-pass filters       |
| SoundWire (`MAN_025D`)|   51 | Plus three `SDW_` prefix variants                  |
| ALC298/0887/0892/0897 |   32 | Desktop-style AIO codecs                           |
| DEV_1F86 / DEV_1F87   |   26 | Newer codecs absent from earlier cohorts           |
| DEV_0294              |    1 | New in the 2026-08 cohort (ASUS ROG Xbox Ally X)   |

> All files live under `internal_speaker` endpoints. No cohort holds a
> headphone or external tuning. The newer packages introduce many non-`normal`
> operating modes that the original 196-XML cohort did not exercise:
> tablet/stand/tent/lid_close/etc. (§13).

---

## 1. Universal constants

These parameters hold **one value across every device and profile** examined,
bar the exception rows the Notes column counts. Every exception sits on
`xml_version` 3.5.5 or later (ad-hoc query 2026-09-24).

| Parameter                          | Value              | Notes                              |
|------------------------------------|--------------------|------------------------------------|
| `volume-leveler-in-target`         | −320 (−20 dBFS)    | Script reads this correctly        |
| `volume-leveler-out-target`        | −320 (−20 dBFS)    | Script reads this correctly        |
| `regulator-relaxation-amount`      | 96                 | 96 wherever present (newer schema); script reads it correctly |
| `mb-compressor-agc-enable`         | 0 (off)            | 0 on all but 3 rows in the corpus  |
| `mb-compressor-slow-gain-enable`   | 0 (off)            | No device uses slow-gain mode      |
| `bass-enhancer-enable`             | 0                  | Never enabled on any device        |
| `virtual-bass-mode`                | 0                  | Never enabled on any device        |
| `graphic-equalizer-enable`         | 0                  | Never enabled on any device        |
| `volume-modeler-enable`            | 0                  | Never enabled on any device        |
| `pregain`                          | 0                  | Always zero                        |
| `postgain` (CP & VLLDP)            | 0                  | Zero on all but 66 rows (value 6)  |
| `system-gain`                      | 0                  | Always zero                        |
| `calibration-boost`                | 0                  | Always zero                        |
| `dialog-enhancer-ducking`          | 0 (mostly)         | 98.5% of rows; 616/40732 are non-zero (8 or 6), so not universal |
| `regulator-overdrive`              | 0                  | Always zero where present          |
| IEQ curve preset                   | `ieq_balanced`     | Only curve used anywhere           |

The script skips bass enhancer, virtual bass, graphic EQ, volume modeler, and
non-zero system/pre/post gains because none of them meaningfully exist in the
wild.

The three IEQ voicing curves are also universal. Every speaker XML carries all
three of `ieq_balanced` / `ieq_detailed` / `ieq_warm`, and each curve's 20-value
array is byte-identical across every device. An ad-hoc sweep over the 1,825-XML
corpus re-derived this on 2026-07-31. Of those 1,825 XMLs, 1,793 are speaker
XMLs; the other 32 are `_dmic`/`_amic` microphone tunings with no `ieq_*`
elements. A re-run on 2026-09-24 over 3422 tuning XMLs, 898 of them
content-unique, finds the same: every one carries all three curves, and each
curve has one value across all of them.

The variants are Dolby-global voicings, not device tunings. The device-specific
correction (audio-optimizer + PEQ) applies identically under every one. This is
the empirical basis for `dolby_to_pipewire.py --variant`'s fixed choice list and
its `balanced` default. Per the IEQ curve preset row above, every device's
profiles select `ieq_balanced`.

---

## 2. Multi-band compressor

**MBC is the exception, not the rule**, except on `music`, where it reaches 36%.
This is the most important finding. Only 100 of the 1589 files whose `dynamic`
profile declares `mb-compressor-enable` (6%) switch it on. That is 189 of the
4225 dynamic-profile *rows* (4%), since some files carry several endpoint modes.
This doc uses both denominators, so this section gives both rates once. The file
denominator counts files that declare the field, because a file omitting it is
not a device that chose to leave the compressor off. The earlier "77 of 2451"
used a denominator no committed query reproduces.

| Profile              | MBC=1 | MBC=0 |
|----------------------|-------|-------|
| dynamic              | 189   | 4036  |
| game                 | 214   | 3925  |
| movie                | 226   | 3999  |
| music                | 1518  | 2707  |
| voice                | 134   | 4091  |
| voice_onlinecourse   | 18    | 2603  |
| off                  | 1     | 4224  |

Music profiles enable MBC far more often (36%). That suggests MBC serves
loudness maximisation on music rather than as a universal safety feature.

### Band-count distribution (MBC-enabled profiles)

Current cohort, by `group_count` (a populated `band_group` count of 1–4):

| `group_count` | Enabled profiles | Disabled (but populated) |
|---------------|------------------|--------------------------|
| 1             | 633              | 21918                    |
| 2             | 1283             |   597                    |
| 3             | 346              |   494                    |
| 4             | 459              |   484                    |

The old 2-band decoder masked two wrinkles:

- **633 profiles enable MBC with `group_count=1`**: single-band, full-spectrum
  dynamics. 630 of 633 are on the `music` profile. The single band typically
  serves as a loudness maximiser. Band-0 ratios range from 1:1 (pure makeup) up
  to ~6:1, and thresholds from 0 dB to −12 dB, with fast attack/release. The
  converter emits them from the `mbc-1band` experimental path, since
  `d29b802` relaxed the `group_count < 2` guard. LSP MBC accepts a single
  enabled band with no split frequency and bands 1-7 disabled.
- **978 profiles declare 3- or 4-band tunings but gate the compressor off**
  (`mbc_enable=0`). Dolby ships the coefficients anyway, so a future driver
  update that flips the enable bit would suddenly activate them. The N-band
  decoder handles this transparently. Before commit `07612e9` it would have
  silently dropped bands above index 1.

Concrete examples reached by the N-band path:

- **voice profile, 3-band**: bands 1 (1313–7125 Hz) and 2 (7125+ Hz) both at
  2:1 above −12/−18 dBFS with +6/+9 dB makeup. The 2-band-capped decoder dropped
  this speech-band compression.
- **music profile, 4-band**: all bands at 1:1 with per-band makeup ranging +1.2
  to +2.9 dB. It serves as a 4-band makeup stage, not a compressor.

The decoder emits `group_count` bands, capped at LSP MBC's 8-band ceiling. It
was 2-band-only until commit `07612e9`.

### Compressor ratio diversity

Devices that enable MBC show wide ratio variation. The cluster table below is
from the original 196-XML cohort. Its Devices column sums to 21 of the 23
dynamic-profile MBC enables in that cohort. The cluster shape is illustrative,
not a current-cohort count.

| Ratio  | Threshold    | Makeup  | Devices |
|--------|--------------|---------|---------|
| 1.1:1  | −4.0 dB      | 2.0 dB  | 4 (gentle)     |
| 1.7:1  | −4 to −11 dB | 2–4 dB  | 8 (moderate)   |
| 2.0:1  | −5.3 dB      | 1.6 dB  | 4 (moderate)   |
| 5.0:1  | −3.0 dB      | 2.0 dB  | 1 (aggressive) |
| 10.0:1 | −4.0 dB      | 3.5 dB  | 4 (limiting)   |

The development device (ALC287 22E6) uses a moderate 1.7:1 @ −6.4 dB with 2 dB
makeup. The 10:1 devices essentially use the compressor as a limiter.

On the ~96% of dynamic-profile rows without MBC, the regulator alone provides
dynamics control. That is a much simpler and safer signal chain.

---

## 3. Volume leveler amount

The `vl_amount` parameter (0–10 scale) varies significantly across devices.
Current-cohort row shares:

| Profile               | Distribution                                               |
|-----------------------|------------------------------------------------------------|
| dynamic               | 5 (49%), 3 (19%), 4 (17%), 2 (5%), 7 (4%), 1 (3%)          |
| movie                 | 5 (50%), 3 (20%), 4 (16%), 2 (4%), 7 (3%), 1 (2%)          |
| music                 | 2 (56%), 0 (18%), 3 (15%), 4 (5%), 1 (3%)                  |
| game                  | 0 (95%), 4 (2%), 2 (1%)                                    |
| voice                 | 0 (99%), 2 (<1%)                                           |
| voice_onlinecourse    | 0 (99%), 2 (<1%)                                           |

The most common dynamic value is **5** (49% of dynamic rows). The development
device uses `vl_amount=2`, which is on the gentler end for the dynamic profile.

---

## 4. Volmax-boost — the loudness ceiling

`volmax-boost` (1/16 dB, in tuning-cp) defines the maximum gain the volume
leveler may add above the output target. **6 dB is the dominant value** on the
`dynamic` profile (78% of current-cohort dynamic rows), and the development
device also uses 6 dB. Distribution for the `dynamic` profile (current cohort):

| Boost       | Share        |
|-------------|--------------|
| 4 dB (64)   | 5%           |
| 5 dB (80)   | 7%           |
| 6 dB (96)   | 78%          |
| 7 dB (112)  | 2%           |
| 8 dB (128)  | 3%           |

Notable per-profile patterns:

- **voice**: polarised: 6 dB (36%), 9 dB (33%), 8 dB (22%). These are the
  highest boosts.
- **voice_onlinecourse**: 4 dB (98%). The gentlest.
- **music**: 6 dB (77%), with some at 3–4 dB.
- **off**: 0 dB (99%), effectively disabled.

The voice profile combines a high boost with a disabled compressor, so the
regulator alone has to catch its peaks.

The script applies `volmax-boost` as `input-gain` on the regulator
(`multiband_compressor#1`), so per-band limiting tames it before the brickwall.
When the regulator is absent it falls back to `limiter#0.input-gain`.
`--volmax-slot output-gain` restores the older placement (issue #23, see
[`volmax-boost` slot](research/loudness-and-limiting.md#r-volmax-boost-slot)).
`--disable volmax` disables it, for when the boost drives the brick-wall limiter
into audible gain reduction on already-loud masters.

---

## 5. Dialog enhancer by profile

Current-cohort enable rates:

| Profile               | Enabled | Disabled |
|-----------------------|---------|----------|
| dynamic               | 58%     | 42%      |
| movie                 | 56%     | 44%      |
| music                 | 0%      | 100%     |
| game                  | 3%      | 97%      |
| voice                 | 55%     | 45%      |
| voice_onlinecourse    | —       | —        |

Dialog enhancer is a **speech enhancement feature**, consistently disabled for
music and game profiles across all devices. Enable rates on dynamic/movie are
lower than the original cohort's 86%. The newer Lenovo-AIO packages leave it off
more often.

> `voice_onlinecourse` shows no rate because `corpus_audit.py` reports only the
> eight most common profiles and it ranks tenth. The `personalize` amount is
> the `personalize_user1`/`_user2` figure (94%), since the bare `personalize`
> profile (86 rows) is likewise below the cut. Widen the profile list in the
> tool before quoting either.

### Dialog enhancer amount

| Profile    | Most common (when enabled) |
|------------|----------------------------|
| dynamic    | 5 (93%)                    |
| movie      | 5 (92%)                    |
| game       | 6 (77%) or 7 (22%)         |
| voice      | 3 (47%) or 8 (33%)         |
| personalize| 10 (94%)                   |

---

## 6. Regulator distortion slope — limiting severity

Slope 16, a hard limiter, holds **95.7%** of the current-cohort rows that
declare `regulator-distortion-slope`. The parameter (1/16 scale) controls how
hard the regulator limits.

| Slope        | Effective ratio       | Share (of 26214 rows declaring it) |
|--------------|-----------------------|----------------|
| 0            | (no limiting)         | <1% (12)       |
| 4 (0.25)     | 1.3:1 — gentle        | <1% (88)       |
| 6 (0.375)    | 1.6:1                 | <1% (64)       |
| 8 (0.50)     | 2:1 — moderate        | 2% (507)       |
| 9 (0.5625)   | ~2.3:1                | <1% (27)       |
| 11–12        | ~3–4:1 — firm         | 1.5% (397)     |
| 13 (0.8125)  | ~5.3:1                | <1% (24)       |
| 16 (1.00)    | ∞:1 — hard limiter    | 95.7% (25095/26214) |

The original 196-XML breakdown had slope=16 at 53%. The AIO-RTK packages that
dominate the current cohort use the hard limiter far more.

The development device uses slope=16, the most common setting. In hard-limiter
mode the regulator acts as a brickwall at its threshold.

For pipeline design, the regulator *is* the brickwall limiter on the large
majority of profile rows (95.7%). The share counts rows, not devices; no
per-device slope share is computed. The explicit output limiter added to the
EasyEffects chain is redundant on those rows and essential on the soft-slope
minority. See `docs/design-notes.md` for why both exist.

Real-world evidence shows the regulator chain matters. On a Snapdragon X Yoga
Slim 7x running Linux without DSP-level Dolby protection,
[taprobane99](https://github.com/taprobane99/Lenovo-Yoga-Slim-7x-Dolby-Linux-Audio)
had to take these steps just to avoid blowing the speakers:

- manually trim four ALSA UCM mixer levels (`PA Volume 12→6`, two
  `Digital Volume 68→58`, one `Digital Volume 84→5`)
- disable the WSA884x amp's internal compressor (`COMP Switch 1→0`)
- cap WirePlumber to 7%

That is about 22 dB of headroom thrown away, because the kernel-level audio
stack has no Dolby-equivalent per-band limiter. The regulator we emit is doing
the work that lets the rest of the chain run unattenuated.

---

## 7. Regulator thresholds — per-band frequency shaping

The 20-band regulator threshold curve varies across tunings, though most devices
share theirs with another device. General shape of `threshold_high`:

- **Range**: −60 dB to 0 dB across bands
- **Low bands** (sub-bass): deepest thresholds (−60 to −30 dB), protecting
  small laptop speakers from excursion damage
- **High bands**: typically 0 dB (no limiting)
- **Mid bands**: vary per device; the "speaker personality" region

The 2795-XML cohort holds **408 distinct `threshold_high` curves**. On the
`dynamic` profile in `normal` mode, 91 of 859 devices use no curve that another
device uses, and 259 of 513 distinct DSP configurations carry one no other
configuration has (ad-hoc query 2026-09-24).

> 408 is not a like-for-like successor to the 399 quoted for the 2483-XML
> cohort. The committed counter (added 2026-08-03) resolves `preset=`
> references to the curve they name. The earlier ad-hoc query counted only
> literal `value=` strings. So the two differ by methodology as well as by
> cohort. No committed tool reproduces 399; treat the delta as unknown rather
> than as growth of 9.

---

## 8. Audio optimizer

A majority of devices use a different audio-optimizer curve for the `voice`
profile than for `dynamic` / `movie` / `music` / `game`, but it is far from
universal in the current cohort.

The voice AO curve typically:

- Reduces low-frequency correction (less bass boost)
- Adjusts mid-frequency emphasis for speech clarity
- Shares the same high-frequency rolloff

> **Re-derived 2026-06-17, re-run 2026-08-03:** 55% per-endpoint (1076/1959)
> diverge, and 62% per-device (407/659) of devices diverge on at least one
> endpoint. The methodology-matched query (`corpus_audit` voice-AO divergence)
> is per-endpoint, `internal_speaker`/`normal` only, full-schema only.
> Simplified `gain_l`/`gain_r` XMLs are excluded. It compares the resolved
> voice AO vector with the dynamic AO vector by exact-integer inequality. The
> 718 excluded simplified-schema endpoints are a different quantity from the
> 674 content-unique XMLs below. The per-device rate is well below the original
> 196-XML cohort's 97%. The drop is real, not a methodology artifact: the
> newer Lenovo-AIO packages that dominate the current cohort differentiate the
> voice AO curve far less often. That is the same direction as the
> dialog-enhancer enable-rate drop in §5. So the opening "majority of devices"
> claim holds only as a slim per-device majority, and a large minority (~38%)
> ship an identical voice AO. Regenerate with
> [`tools/corpus_audit.py`](../tools/corpus_audit.py).

All non-voice profiles (dynamic, movie, music, game, personalize) share
identical AO curves. The script processes each profile independently, so the
voice preset picks up the voice-specific AO curve on a device that has one.

### Curves pinned at the declared gain range

A minority of tunings put one or more bands right at the largest per-band gain
the format expresses. `<setting> <geq_maximum_range>` states that range where
present. It is always `192`, which is +12.0 dB at 1/16 dB.

Measured over the 674 content-unique XMLs carrying an
`internal_speaker`/`normal` audio-optimizer block: first profile, both channels,
raw value ≥ the declared range, defaulting to 192 where the file omits it.

| population | ≥1 band at the rail | median peak-to-peak |
|---|---|---|
| simplified schema (148) | 32 (22%) | 15.2 dB |
| full schema (526) | 84 (16%) | 14.0 dB |

Within the simplified subset they cluster in the older schema version, which is
also the only one that declares the range at all:

| `xml_version` | XMLs | ≥1 pinned | declares `geq_maximum_range` | median p-p |
|---|---|---|---|---|
| 3.2.0 | 27 | 16 (59%) | 27/27 | 19.9 dB |
| 3.2.1 | 121 | 16 (13%) | 0/121 | 15.0 dB |

This is confounded with package vintage. The 3.2.0 files concentrate in the
older `ext_thinkpad_AIO_rtk` stores. So read it as "older tunings are more
likely to sit at the rail", not as a schema-version rule.

It matters for the generated preset for two reasons. The FIR is peak-normalised,
so a boost at the rail becomes a deep relative cut everywhere else. And the
bands it boosts are often ones the regulator leaves unlimited. Issue #46's T495
(`17AA5125`, 3.2.0) is the worst case seen so far: 23.7 dB peak-to-peak with
three bands at the rail, wider than 95% of simplified tunings. A run warns when
the largest boost lands on an unlimited band. That was 8% of parseable tunings
in an ad-hoc sweep on 2026-08-03 (`216a822`), against 16% for the
all-inert-regulator warning beside it.

These particular counts have **no committed query yet**.
[`tools/corpus_audit.py`](../tools/corpus_audit.py) counts content-unique files
but runs no query over them, and does not compute `geq_maximum_range`,
`xml_version` or the AO peak-to-peak spread. Regenerating them means an ad-hoc
sweep, or adding those queries to the tool, which is the better fix.

### Curves shipped with the optimizer switched off

A profile can carry a non-zero audio-optimizer curve and still declare
`audio-optimizer-enable=0`. The gate is common and almost always redundant:
the curve it disables is already all-zero. The converter reads that gate and
drops the curve, keeping only the IEQ voicing (reference.md, chain row 1).
Before commit `f5473c5` it applied the curve regardless.

> **Derived 2026-08-04** over the 3056-XML corpus (788 content-unique). The
> walk covers `endpoint/profile/tuning-vlldp` and resolves `ch_00`/`ch_01`, or
> `gain_l`/`gain_r` on simplified files, through `resolve_xml_value`. A plain
> grep misses the `preset=` indirection. **Only the `tuning-vlldp` gate
> counts.** `tuning-cp` carries an `audio-optimizer-enable` of its own (46364
> zero-valued elements against vlldp's 4702), and the converter never reads
> it.

| population (content-unique) | gate off | …with a non-zero curve |
|---|---|---|
| all endpoints and profiles | 1283 rows | 22 rows |
| `internal_speaker`/`normal` | 773 rows | 18 rows, 17 XMLs / 17 subsystem ids |

Where the gate is *not* redundant, the depth no longer applied is 13.7 dB at
the deepest affected band on `off` (median 12.0) and 7.0 dB on `music`. Those
three figures reproduce identically on the raw corpus, which counts the same
tunings 108 times over across driver packages: 31 subsystem ids, 30 of the
rows `music`. Almost every affected row is the `off` profile, but `music` is
one users select deliberately.

Regenerating one affected device moved only its three Music presets, by up to
+6.4 dB in band, 3.7 dB RMS. Every other profile stayed byte-identical. The dev
device never declares the field and regenerates byte-identical throughout. Not
heard on affected hardware: none of the devices is in reach. No committed query
yet: same caveat as the block above.

### Where the correction starts and stops

The norm is a curve that corrects nearly the whole grid: 501 XMLs (63.7%)
start at band 0 or 1, and 545 (69.3%) run to 19.7 kHz. An AO curve need not
span the band grid. Counting the leading and trailing all-zero bands of the
resolved curve says where a tuning declines to correct at all. That differs
from the enable gate above, which switches the curve off wholesale.

> **Derived 2026-08-21** over the 788 content-unique XMLs, first
> `internal_speaker`/`normal` profile, resolving `ch_00` / `gain_l` through
> `resolve_xml_value`. 787 parse. The one miss (`17AA508B`) offers only
> `internal_speaker`/`laptop` and `/tablet`, so it has no `normal` mode to
> read; the corpus tier skips it by design. The 20-band frequency grid is
> identical across all 787: 47 Hz, 141, 234, 328, 469 … 13875, 19688. So band
> index and frequency are interchangeable here.

| all-zero bands | at the bottom | at the top |
|---|---|---|
| 0 | 250 (31.8%) | 545 (69.3%) |
| 1 | 251 (31.9%) | 85 (10.8%) |
| 2 | 61 (7.8%) | 13 (1.7%) |
| 3 | 89 (11.3%) | 15 (1.9%) |
| 4 | 13 (1.7%) | 12 (1.5%) |
| 5–19 | 10 (1.3%) | 4 (0.5%) |
| 20 (curve all zero) | 113 (14.4%) | 113 (14.4%) |

Trimming ≥4 bands off the bottom without being all-zero is rare: 23 XMLs,
**2.9%**.

Trimming ≥4 bands off both ends is rarer still: 7 tunings across 6 device ids.
Five of those share one exact span.

| span | tunings | device ids |
|---|---|---|
| 469 Hz – 7125 Hz (bands 4–15) | 5 | `17AA3912`, `17AA3914`, `17AA393A` (two distinct tunings), `17AA3941` |
| 469 Hz – 844 Hz (bands 4–6) | 2 | `17AA38D2`, `17AA38D7` |

`17AA3941` is the subsystem id of issue
[#67](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/67)'s
machine, a Lenovo IdeaPad Pro 5 14AGP11 (machine type 83SG). It is the only
member of the cluster with a published speaker spec. PSREF gives "Stereo
speakers, 2W x2, optimized with Dolby Atmos®" with no amplifier line. The
reporter's `--speaker-info`, ACPI and i2c listings agree that no smart amp is
present. That single spec is the only hardware anchor either row has; the other
five ids are unidentified. The four sharing #67's span sit in one contiguous
`17AA:39xx` run, which suggests a single platform generation. The two 469–844 Hz
outliers sit apart from it in `17AA:38Dx`.

Why it matters for issue
[#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14):
the five tunings sharing the 469–7125 Hz span are the corpus's clearest a-priori
case for `--enable virtual-bass`, which is why it was offered on #67. The
`virtual-bass-*` family is present in all 787 content-unique tunings and carries
one parameter set across every one of them: source band 35–160 Hz, mix band
94–469 Hz, three sub-band gains, zero overall and slope gain. §1 records the
`virtual-bass-mode = 0` half of this. On the five-tuning cluster above, the
audio optimizer's first corrected band is 469 Hz, exactly the top of that mix
band. So everything below the virtual-bass crossover is left entirely to the one
stage the default chain does not reproduce.

Read the zeros as *"Dolby applies no correction here"*, not *"the driver cannot
reach here"*. A response already flat enough needs no correction either. The tie
to driver size rests on one device's published spec and is a hypothesis, not a
measurement. No committed query yet: same caveat as the two blocks above, since
`corpus_audit` runs no query over its content-unique set and computes no AO
span.

---

## 9. PEQ filters — mostly simple, occasionally complex

Raw filter counts across the 2795-XML cohort, all speakers and all profiles:

| Type | Description                          | Count (2795 cohort) | Script support          |
|------|--------------------------------------|---------------------|-------------------------|
| 1    | Bell/peaking EQ                      | 39973               | ✅ Yes                  |
| 9    | High-pass (with order)               |  7080               | ✅ Yes                  |
| 3    | High-shelf (with S parameter)        |  3748               | 🧪 Experimental         |
| 7    | High-pass variant (with order)       |  1763               | ✅ Yes                  |
| 4    | Low-shelf (with S parameter)         |   384               | ✅ Yes                  |
| 8    | Low-pass variant (with order)        |   350               | 🧪 Experimental         |
| 6    | Low-pass (with order)                |    20               | 🧪 Experimental         |

The original 196-XML audit observed only types 1/4/7/9. The expanded cohort
surfaces three previously-unseen types, all emitted via experimental paths.

### Type 3 — high-shelf filter (experimental)

```xml
<filter speaker="0" enabled="1" type="3" f0="2700" gain="2.000000" s="1.000000"/>
```

Type 3 has the same parameter shape as type 4 (`f0`/`gain`/`s`), mirrored. Its
gains are strictly non-negative: 0 to +15 dB across the corpus, with no cut
variants seen. The inflection is above `f0` rather than below. In the 2483-XML
cohort (2026-06-16) it was present in **87 distinct XMLs** (3730 filters),
centred around 2.7 kHz with a +2 to +5 dB presence lift. `make_hishelf_band`
emits it in LSP `"Hi-shelf"` mode, with the same Q-from-S formula as Lo-shelf.
The formula is symmetric in shelf direction. A throwaway FFT script verified it
numerically against the RBJ high-shelf cookbook formula. Affected users can turn
it off with `--disable high-shelf` and are invited to report audibility.

### Types 6 and 8 — low-pass variants (experimental)

```xml
<filter speaker="0" enabled="1" type="6" f0="8000" order="4"/>
<filter speaker="0" enabled="0" type="8" f0="19500" order="8"/>
```

They have the same shape as types 7/9 (`f0`/`order`, no gain), with the
direction flipped. Type 6 appears at 8–10 kHz with order 2 or 4, mostly on
ALC274 with a few ALC287. Type 8 appears almost only at 19.5 kHz with order 8
(342 of 350 filters), mostly on ALC235/ALC256, and all 342 ship `enabled="0"`.
The orders, frequencies and enable states are from an ad-hoc query on
2026-09-24. Both are rare by XML: in the 2483-XML cohort only 9 XMLs carried
type 6 (18 filters) and 23 carried type 8 (350 filters). The parser skips
disabled filters, so the LP filters that reach `make_lp_band` are the enabled
order 2–4 ones at 8–10 kHz. It emits them in LSP `"Lo-pass"` mode, structurally
a mirror of the already-verified HP path. Turn them off with
`--disable lo-pass`.

### Types 1, 4, 7, 9 — supported

Types 1 and 9 are the dominant filters in the corpus. Types 4 and 7 are a
minority but fully handled:

```xml
<filter speaker="0" enabled="1" type="4" f0="600"  gain="2.000000" s="1.000000"/>
<filter speaker="0" enabled="1" type="7" f0="100" order="4"/>
```

Type 4 maps to EasyEffects `"type": "Lo-shelf"`, with Q derived from S via the
standard audio shelf formula. Type 7 is treated identically to type 9. Both are
HP with order, likely different filter topologies: Butterworth vs
Linkwitz-Riley.

### Filters-per-speaker distribution

Most devices have 1–3 filters per speaker, typically a high-pass plus one or two
bells. The complexity ceiling in the cohort is ~7–8 filters per speaker on a few
Lenovo AIO-RTK tunings. That is still comfortably below the LSP PEQ 32-band
ceiling.

---

## 10. IEQ amount

Current-cohort IEQ=10 share, when `ieq_enable=1`:

| Profile     | IEQ=10 | Other     |
|-------------|--------|-----------|
| dynamic     | 97%    | 4/6 (3%)  |
| movie       | 100%   | —         |
| music       | 84%    | 3–8 (16%) |
| game        | 100%   | —         |
| voice       | 100%   | —         |

All but one device selects `ieq_balanced`. The two issue-#21 Apple Boot Camp
XMLs name `ieq_detailed` on the `music` profile, re-derived 2026-08-24 through
the parser's own lookup. Their `PCI_`-spelled filenames are among the spellings
the pre-2026-08-09 sweep skipped, so the row counts below don't include them.
Even so, "all devices use it" overstates the data. Of the 4131 IEQ-enabled rows,
**3817 name `ieq_balanced` and 314 declare no `ieq-bands-set` at all**, falling
back to the same curve by default rather than choosing it. The IEQ amount scales
the intelligent EQ curve, a Dolby-global voicing identical on every device (§1).
Music profiles occasionally reduce it. The near-universal IEQ=10 means the full
curve should be applied in most cases.

Correction: the converter applies the curve at `ieq-amount/100`, so IEQ=10 is a
10 % weight, per the
[`ieq-amount` scaling finding](research/eq-and-frequency-response.md#r-ieq-amount-scaling).

---

## 11. MI steering

`mi-dv-leveler-steering-enable=1` appears **almost exclusively on the `dynamic`
profile**, across all devices that have it: 4205 of 4225 dynamic rows,
effectively none elsewhere. This confirms it's a deliberate choice to add
Media-Intelligence-driven gain hold only for the "adaptive" profile.

This is the key feature that the EasyEffects pipeline cannot replicate. Without
content analysis, the autogain has no way to know when silence is "real" silence
vs a quiet passage that will resume loud. This is the root reason the script
bypasses autogain by default on HDA.
[Why autogain is bypassed by default](research/adaptive-processing.md#r-autogain-bypassed-by-default)
has the full rationale.

---

## 12. Defensive paths checked against the corpus

Dolby's XML schema permits more variation than most shipping devices exhibit.
The parser handles several of these cases defensively. Each was re-checked
against the full 2795-XML cohort on 2026-08-03:

| Code path                                 | Defensive behaviour                                                     | Corpus check (2795-XML cohort)                                                     | Trigger condition                                              |
|-------------------------------------------|-------------------------------------------------------------------------|------------------------------------------------------------------------------------|----------------------------------------------------------------|
| Default profile (no `--profile` flag)     | `parse_xml` picks `endpoint.find("profile")` (first child)              | 2677/2677 internal_speaker/normal endpoints have `dynamic` first                   | XML where `off` or another no-op profile precedes `dynamic`    |
| Asymmetric L/R PEQ filter counts          | Missing-channel HP slot fills with 100 Hz/24 dB-oct HP, bell slot with flat 1 kHz bell | 12204 PEQ profiles → 38 with an L/R filter-count diff (13 differ in HP count)     | Per-driver tuning where one channel has filters the other lacks |
| Empty `regulator-tuning/threshold_high`   | Falls back to `[0.0]*20` (no limiting) and warns. Volmax still routes via regulator | **Fixed (2026-06-17).** See the note below. | Genuinely empty / hand-edited / broken regulator tuning |
| Shelf filter with explicit `q` attribute  | Output-gain compensation uses full shelf gain (commit `c505864`)        | 384 type-4 shelf filters → 0 with explicit `q`                                     | Driver release that adds `q` to a shelf, previously silently under-compensated |
| `is_soundwire` filename detection         | Falls back to HDA mode (no bass enhancer, autogain bypassed by default)  | All matched XMLs in the corpus have `SOUNDWIRE_…` or `SDW_…` filenames intact      | User manually renames a SoundWire XML before passing it in     |
| `make_multiband_compressor` 5+ band cap   | `min(group_count, 8)` enforced                                          | Max observed `group_count` = 4 (Dolby schema only allocates `band_group_0..3`)     | Dolby schema extension                                         |

- **Empty `regulator-tuning/threshold_high`.**
  - *Cause*: `threshold_schema` (corpus_audit) confirms exactly 9 profiles on
    one newer SoundWire device (`SUBSYS_37A317AA`) stored
    `threshold_high`/`threshold_low` in a per-channel `<ch_00>…<ch_07>`
    sub-schema, with real non-zero values on ch_00. The flat
    `resolve_xml_value` didn't read that sub-schema, so that device got no
    regulator limiting.
  - *Other profiles*: the other 36,620 reg-enabled internal_speaker profiles use
    the direct `value=`/`preset=` form. That includes siblings like `384B17AA`,
    whose `threshold_high preset="array_20_zero"` is an intentional zero.
  - *Fix*: `resolve_channel_or_direct` reads `ch_00`. The `[0.0]*20` fallback
    only fires on a genuinely empty tuning, and warns when it does.
  - *Earlier description*: the doc previously mis-described this as an
    `isolated_band` sub-schema. `isolated_band` is an unrelated *sibling*
    element in the older flat schema.

**Now reachable on the current cohort.** These formerly inert paths are no
longer defensive-only and should be treated as implementation gaps:

| Code path                                 | Current behaviour                                                        | Current-cohort check                                                              | Status                                                          |
|-------------------------------------------|--------------------------------------------------------------------------|------------------------------------------------------------------------------------|-----------------------------------------------------------------|
| 1-band MBC (`group_count=1`)              | Emits LSP `multiband_compressor` with band 0 active (no split frequency) and bands 1-7 disabled. Adds the `mbc-1band` experimental marker to the end-of-run callout | 633 profiles enable MBC with `group_count=1` (§2). The `music` profile dominates them, using a 1-2:1 ratio with fast attack/release as a loudness maximiser | Experimental: reproduced from the Dolby tuning but not yet audibly validated. `--disable mbc` turns it off. |
| Asymmetric L/R PEQ peak gain              | Output-gain compensation uses global `max(L,R)` peak                    | `corpus_audit` L/R peak-asymmetry tally (2026-06-17): 131 rows / 10 devices differ, only ALC257/287 and only Lenovo convertible/AIO packages | **Resolved (keep global-max).** EE's equalizer has per-channel `left`/`right` bands but a *single* `output-gain`. |
| Non-zero `dialog-enhancer-ducking`        | Not read by the script, and irrelevant on the present pipeline           | 616/40732 rows have ducking=6 or 8 (§1)                                            | Informational: no downstream consumer, but the "always 0" invariant claim was too strong |
| Unknown PEQ filter type                   | Warns "unknown PEQ filter type N, skipping" and drops the filter         | No observed filter outside `(1,3,4,6,7,8,9)` on the cohort                          | Inert: types 3/6/8 are emitted (see §9). The warning remains a guard against future driver releases adding new types |

- **Asymmetric L/R PEQ peak gain.**
  - *Tally split*: of the 131 differing rows, 119 are ~1 dB matched-filter gain
    trims (median 1.0 dB). 12 are structural 7 dB cases in convertible `stand`
    pose.
  - *Why global-max*: applying `max(L,R)` equally to both channels preserves the
    Dolby-tuned L/R relationship at every frequency, including the 7 dB worst
    case. A per-channel trim would impose a broadband L-vs-R tilt and isn't
    representable as one `output-gain`. The only cost is extra headroom on the
    quieter channel. The downstream leveler restores it only when it runs, which
    on HDA takes `--enable autogain`.

If a future driver release breaks any of the truly-inert assumptions, the script
will silently produce a degraded preset rather than crash.
[`tools/corpus_audit.py`](../tools/corpus_audit.py) reproduces the corpus audit,
including the L/R-asymmetry and §14 presence checks.

Two "by-design" behaviours, one since removed, that look like bugs but aren't:

- *Removed 2026-07-03; the convolver emits 0 dB gain on every device family
  ([convolver headroom restore](research/loudness-and-limiting.md#r-convolver-headroom-restore)).*
  The SoundWire convolver applies `peak_db * 0.5` as `output-gain`. This
  intentionally lets peak frequencies exceed 0 dBFS so the brick-wall limiter
  shapes them back. It restores half of the headroom that pure
  peak-normalisation would lose for the IEQ-only (no-AO) curve.
- The PEQ output-gain compensation deliberately ignores high-pass and
  negative-gain filters. HP slots reduce headroom requirements, since they only
  cut. Shelves/bells with negative gain don't add headroom pressure.

---

## 13. Endpoint operating modes and profile variants

The Lenovo-AIO-RTK and ThinkPad-AIO-RTK packages exercise both axes, operating
mode and profile type, much further than the original 196-XML cohort. That
cohort only exposed
`operating_mode="normal"` endpoints with the six canonical profile types
(`dynamic`/`movie`/`music`/`game`/`voice`/`off`).

### Operating modes

Current-cohort row counts for the top modes. ~20 rarer convertible/desktop poses
make up the tail.

| `operating_mode`        | Rows  | Typical hardware                                       |
|-------------------------|-------|--------------------------------------------------------|
| `normal`                | 25768 | All laptops — the mode selected by default             |
| `laptop`                | 3522  | Convertible in clamshell pose                          |
| `stand`                 | 3502  | Convertible in stand/present pose                      |
| `tablet`                | 3450  | Convertible folded flat                                |
| `tent`                  | 3430  | Convertible in tent pose                               |
| `lid_close`             |  486  | Lid-closed external-monitor use                        |
| `detachable_speaker`    |  220  | Detachable tablet-with-dock SKUs                       |
| `Laptop_flipped` / `Table_Portrait` / `Table_Portrait_flipped` | 36 each | Newer convertible poses |

By default the script reads `operating_mode="normal"`, the `--mode` default. On
convertibles, Dolby ships distinct tunings per hinge pose. The "normal" fallback
is fine for the clamshell case. Users of Yoga-class devices would need
`--mode tablet|stand|tent` to pick up the pose-specific tuning. The CLI exposes
`--mode`, and the README documents it.

### Profile types

The canonical Dolby profile vocabulary expands beyond the six listed in the
original cohort. Current-cohort row counts:

| Profile             | Rows | Notes                                                 |
|---------------------|------|-------------------------------------------------------|
| `dynamic`           | 4225 | Primary listening profile                             |
| `movie`             | 4225 |                                                       |
| `music`             | 4225 |                                                       |
| `voice`             | 4225 |                                                       |
| `off`               | 4225 | No-op pass-through                                    |
| `game`              | 4139 |                                                       |
| `personalize_user1` | 4139 | User-customisable slot 1 (not the `personalize` alias) |
| `personalize_user2` | 4139 | Slot 2                                                |
| `personalize_user3` | 4139 | Slot 3                                                |
| `voice_onlinecourse`| 2621 | Ultra-gentle leveler profile (§4)                     |
| `game_shooter`      |   86 | Genre-specific game profile                           |
| `game_racing`       |   86 |                                                       |
| `game_rpg`          |   86 |                                                       |
| `game_rts`          |   86 |                                                       |
| `personalize`       |   86 | Legacy single-slot personalize (pre-user1/2/3 schema) |

The `personalize_user{1,2,3}` slots carry real Dolby tunings in the shipped XML,
not empty slots. `--profile personalize_user2` is a legitimate preset source.
The slots are Dolby-provided starting tunings meant to be reshaped via the Dolby
Access Windows app. The `game_{shooter,racing,rpg,rts}` variants appear only on
a small subset of ThinkPad AIO-RTK devices. All share the outer `game` tuning
shape, with per-genre tweaks to surround-boost and dialog handling.

`--list` reports whatever profile names the XML declares, so users pick these up
naturally. `--all-profiles` iterates every one and generates
`Dolby-{ProfileName}-{IEQ-variant}` presets for each.

#### Which profile the device ships on

A few XMLs state it in `<setting><default_profile>`. Over the 791 content-unique
XMLs with an `internal_speaker` endpoint, 28 declare it: 25 `music`, 2
`dynamic`, 1 `movie`. **26 of those** name something other than the profile we
build. `dynamic` is physically first in the endpoint on all 791, so the script's
"first profile" default diverges from Windows on those 26.

The script does not act on the declaration. It reports the mismatch and suggests
`--profile <name>` (issue #46). Adopting it as the selection default is gated on
hearing the difference on a device. Issue #29's reporter independently preferred
`music` on a Zenbook S14, which would be the second data point.

---

## 14. Newer-pipeline DSP blocks not modeled by the script

The script implements none of the DSP blocks below. The newer Lenovo IdeaPad /
ThinkPad-X13s SoundWire packages introduced some of them. Others appear outside
them: the dev X1 Yoga's XML (`SUBSYS_17AA22E6`) already carries MI steering, MBC
channel deviation, the rear virtualizer angles, and the surround-decoder
centre-spreading, woofer-regulator and bass-extraction LFE fields.
`collect_unmodeled_features` in `lib/dax/parse.py` flags some at end of run. The
rest are silently dropped.

The three bands below sort them by what the XML carries:

- **Parameters in the XML:** a candidate for implementation.
- **Only an on/off bit:** never derivable, however common.
- **Inert everywhere:** costs nothing to skip.

> **Re-derived 2026-08-03** against the current 2795-XML / 40732-row corpus,
> with `tools/corpus_audit.py` ("Present-but-not-modelled stages"). It reports
> files, rows and devices separately. The table below counts XMLs, and one XML
> contributes many endpoint × profile rows. The presence counts grew with the
> corpus (1234 → 1345 XMLs), and every "enabled in 0" claim still holds. The one
> substantive change is the volume-leveler compressor: it is not merely present
> but enabled almost everywhere it appears.

### Band A — has parameters, so implementable

| Block | Element(s) | Active in corpus | Status |
|---|---|---|---|
| Sliding bass | `sliding-bass-enable`, `-xo-frequency`, `-max-gain`, `-attack-time`, `-release-time`, `-gain-curve`, `-band-boundary`, `-min-level`/`-max-level` | Enabled and non-inert on 832 rows / 156 XMLs / 63 devices. Only 13 enabled rows are fully inert. See the note below. | The parameters are all there; the semantics are not. See "What blocks sliding bass" below. Not implemented, and not guessable without a capture |

- **Sliding bass.**
  - *Corpus spread*, over the rows the table counts: peak boost 3.0–18.6 dB,
    median 12.0. Mostly `music` (374 rows), but also 73 rows each on
    `dynamic`/`movie`/`game`/`personalize_*`, so it reaches the default build.
    Crossover 180–300 Hz. `band-boundary` is always 6 and the curve always 5
    points.
  - *Semantics*: the 5-point `gain-curve` has two incompatible readings, and
    they imply different stages.

#### What blocks sliding bass

What is missing is what the five `gain-curve` points are *indexed by*. The
fields are all present and internally consistent: `band-boundary` is 6 and
`gain-curve` has 5 points on every one of the 274 enabled profiles examined. The
two readings imply different stages:

- **Level-indexed:** gain slides with input level across the
  `min-level`/`max-level` window. That is a dynamic EQ or upward compressor on a
  low band, which an LSP `multiband_compressor` could approximate. Against it: a
  typical curve is `0, 192, 26, 0, 0` (1/16 dB → 0, 12.0, 1.6, 0, 0), which
  rises then falls. A level→gain curve for a bass boost would normally decrease
  monotonically as level rises.
- **Band-indexed:** a per-band gain shape over the bands below `band-boundary`.
  That is "sliding" bass energy *up in frequency*, out of the range the speaker
  cannot reproduce. The same curve reads naturally this way: nothing in the sub
  band, a large boost one band up, a taper above. If that shifting is genuine
  harmonic synthesis rather than EQ, it hits the same wall as Virtual Bass
  Enhancement: EasyEffects cannot reproduce it. See the
  [DAX virtual-bass finding](research/virtual-bass.md#r-dax-virtual-bass) and
  [issue #14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14).

`gain-curve[1]/16` equals `max-gain` exactly on the 300 Hz family:
192→12.000000, 288→18.0, 297→18.5625. That fixes the 1/16-dB scale. The identity
breaks elsewhere: `0,43,6,0,0` appears with `max-gain` 6.0 *and* 4.0 on
different crossovers. So the two fields are independent, and the relationship is
not a decode. `min-level` is 0 everywhere. `max-level` 52/80/90/98/100,
`attack-time` 706–712 and `release-time` 500 are all in unknown units.

An implementation today would pick one of three mechanisms on a parameter worth
up to 18 dB: dynamic EQ, a static per-band shape, or bass synthesis. That is a
guess, not a mapping, so no `--enable` flag ships for it.

**What would settle it:** a DAX capture. 55 of the 64 devices that enable
sliding bass have an in-device A/B: it is on for `music` and off for every other
profile, for example `17AA3DD8`. The other nine also enable it on `dynamic`,
`movie`, `game` and `personalize_*` (ad-hoc query 2026-09-24). So stepped bass
tones captured under Windows on `music` vs `dynamic` separate the readings
directly. Level-indexed changes with stimulus level, and a static band shape
does not. Synthesis shows new harmonics, the Δ3 signature the issue-#14 harness
already measures.

The XML enable is not the whole gate (2026-08-21). The Intel streaming-extension
INFs ship paired `EnableSlidingBassAddReg` / `DisableSlidingBassAddReg` sections
writing `HKR,Streaming_Speaker,DolbySlidingBass`, selected per hardware ID.
Lenovo's Cirrus OEM INF has an `EnableDolbySlidingBass` variant. The dev X1
Yoga's live driver key carries `DolbySlidingBass = 0`. This is the only
per-device bass feature gate found anywhere in the corpus INFs' registry
surface. It makes sliding bass Dolby's demonstrated pattern for gating bass
behaviour outside the tuning XML. An implementation would face the same
does-Windows-actually-run-it question as Virtual Bass Enhancement (the
[DAX virtual-bass finding](research/virtual-bass.md#r-dax-virtual-bass) deep
audit).

Sliding bass is deliberately not surfaced to users either (2026-08-04). It is
absent from `_UNMODELED_FEATURES`, so no run mentions it, including on the 63
devices that carry it. Adding the row was considered while triaging issue
[#50](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/50)
and declined until a capture exists, because the finding would name a gap with
no resolution to offer. Its `ask` would put a capture request in front of every
reader rather than the few who can run one. Revisit once the semantics are
settled.

That issue is the best capture candidate so far. Its reporter dual-boots, is on
the 300 Hz / `max-gain=18.0` variant above, and reports the symptom the stage
would explain: "the generated preset lacks low end/bass that Windows has".

#### What `threshold_high = −960` encodes

The −60.00 dB end of `threshold_high` (raw −960) does not behave like a
threshold. Swept 2026-08-05 over 3056 XMLs → 41,667 regulator-enabled profiles →
833,340 band samples, `threshold_high` spans exactly [−60, 0] dB, and both ends
are rails. `0` holds on 66.03% of samples, and no positive value ever appears.
The −960 end covers 4852 samples, 433 XML paths, 117 filenames and ~10% of
profiles:

- A 14.06 dB gap separates it from the next-lowest observed value (−735 raw).
  Above it the granularity is ~1 raw unit: 161 distinct values in [−960, −300].
  −960 occurs 4852 times, versus 10 / 28 / 10 at its neighbours.
- It appears only on bands 0–2 (4150 / 678 / 24), never above 234 Hz. The run
  always starts at band 0 (4150 of 4150 profiles).
- The step to the neighbouring band averages −40.2 dB, where a normal
  band0−band1 step averages −1.1 dB.
- Carriers are the tunings with the largest AO bass boost: at bands 1/2, median
  +7.0 / +10.0 dB, against +1.1 / +3.5 for non-carriers. A −60 dBFS band ceiling
  would fight the tuning's own intent.
- Its use is uncorrelated with any plausible tuning intent. Sibling XMLs sharing
  a PCI_SUBSYS swap it for the *opposite* rail (`0`). Within one file,
  `17AA5080` uses all-zero on 7 profiles and −960 on `music` alone.
  `17AA3832`/`17AA3851` use it on `voice` only.

`threshold_low` gives no independent signal. It is `threshold_high − 192`
(−12.00 dB) on 99.712% of all band samples, including inactive ones. So that
delta accompanies any value and cannot argue for or against a sentinel reading.
`isolated_band` does not gate it either: 99.5% of ≤−40 dB bands are `iso=1`, but
so are 84.9% of ordinary negative bands.

Our `make_regulator` renders it literally, as a ~56:1 clamp. **No DAX capture of
any carrier device exists**, so there is no measured evidence either way. This
is a structural argument only. What would settle it: an `off`/`dynamic` pink
pair at two levels on any ≥2-band carrier (24 filenames). The 47/141 Hz
`dynamic − off` figure answers it directly. Not raised with issue #44: that
device (`17AA380D`) carries no rail at all.

### Band B — an on/off bit and nothing else

These are enabled on real devices, but no amount of corpus evidence will make
them implementable. The schema carries only the enable flag for each: no
threshold, ratio, attack, release or depth. Verified by diffing the full tag set
of a device that has them against one that does not. Emitting a stage anyway
would mean inventing every parameter. That is the per-device hand-tuning the
XML-only rule exists to prevent (see CLAUDE.md "Core invariants").

| Block | Element(s) | Active in corpus | Status |
|---|---|---|---|
| Volume-leveler DRC sub-component | `volume-leveler-drc-enable` | 618 XMLs, enabled on 9979 of 11150 rows | Reported at end of run: detail only where the leveler is bypassed (the HDA default), an ask where it runs (the SoundWire default, or `--enable autogain`). See the note below |
| Volume-leveler compressor sub-component | `volume-leveler-compressor-enable` | 137 XMLs, 77 devices, enabled on 2408 of 2410 rows | Same. See the note below |
| Media-Intelligence steering | `mi-virt-steering-enable`, `mi-dialog-enhancer-steering-enable` (4245 rows), `mi-surround-compressor-steering-enable` (4139 rows) | Present on all 2681 XMLs | Content-adaptive steering of stages we do model. Not warned: it is on the `dynamic` profile of essentially every device, so a note would fire on every run |
| MBC channel deviation | `mb-compressor-channel-deviation` | 1589 XMLs, non-zero on 64 rows | Not warned; near-universally zero |

- **Volume-leveler DRC sub-component.** First non-Lenovo carrier: Framework's
  `F111:010F`
  ([#73](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/73)
  package, 2026-08-25). SoundWire carriers, where the leveler runs by default:
  43 of the 668 files that enable it in the 3641-file corpus (ad-hoc query,
  2026-09-25).
- **Volume-leveler compressor sub-component.**
  - *Issue #25*: the compressor sub-component does not explain the issue
    [#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25)
    autogain overshoot: neither that device (`17AA507F`) nor the dev device
    (`17AA22E6`) carries the element, so Dolby's leveler runs uncompressed there
    too.
  - *In-device A/B*: two devices (`37A317AA`, `C1DC144D`) switch it off on
    `music` and on elsewhere. That is the closest thing to an in-device A/B.
  - *First non-Lenovo carrier*: Framework Laptop 13 Pro `F111:000F`
    ([#73](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/73),
    2026-08-25), on in all ten of its profiles. Its `F111:010F` package sibling
    carries the DRC sub-component instead.

### Band C — present but inert, or engine plumbing

Named once so a future schema sweep doesn't re-discover them as findings:

| Group | Element(s) | Active in corpus |
|---|---|---|
| Inert feature blocks | `bass-extraction-enable`, `bass-enhancer-boost`, `virtual-bass-slope-gain`, `virtual-bass-overall-gain`, `volume-modeler-calibration`, `noise-gate-enable`, `process-optimizer-enable`, `virtualizer-start-band` | Zero on every row where present |
| Fixed feature constants | `bass-enhancer-width`/`-cutoff-frequency`, `bass-extraction-cutoff-frequency`, `virtual-bass-src-freqs`/`-mix-freqs`/`-subgains`, `virtualizer-{front,surround,height}-speaker-angle` | Identical on all 40732 rows; nothing device-specific to carry |
| DSP-engine descriptors | `max_num_*`, `output_ports`, `nb_output_channels`, `low_latency_enable`, `processing_mode`, `mix_matrix`, `mi_process_disable` | Dolby runtime plumbing, not audio parameters |

### Previously catalogued blocks

| Block | Element(s) | Active in corpus | Status |
|---|---|---|---|
| Dynamic Speaker Optimization (DSO) | `init-info/dynamic_speaker_optimization_enable`, `dynamic-speaker-optimization-amount`, `dynamic-speaker-optimization-speaker-interval` | 1 XML, 1 device (`SUBSYS_37A317AA`, IdeaPad-5x-2-in-1 SoundWire SPK1), enabled on all 10 of its rows | Warned: the detail at parse time, the ask at the end of the run. Excursion-aware bass limiting tied to driver size. It needs Dolby DSP data we don't have. |
| Advanced speaker virtualizer | `advanced-speaker-virtualizer-rendering-config`, `advanced-speaker-virtualizer-start-bin`, `speaker_virtualizer_mode` | Same 1 XML / device | Warned: the detail at parse time, the ask at the end of the run. Newer FFT-domain replacement for `output-mode-partial-{surround,height}-virtualizer-enable`, and also unmodeled. |
| Volume-leveler compressor sub-component | `volume-leveler-compressor-enable` | 137 XMLs, 77 devices, and enabled on 2408 of 2410 rows, not merely present | Reported at end of run, as in Band B: detail only where the leveler is bypassed (the HDA default, see [adaptive-processing.md](research/adaptive-processing.md#r-autogain-bypassed-by-default)), an ask where it runs (the SoundWire default, or `--enable autogain`). See the note below. |
| Rear / rear-height virtualizer angles | `virtualizer-rear-speaker-angle`, `virtualizer-rear-height-speaker-angle`, `rear-height-filter-mode` | Common on 4+ speaker laptops | Not modeled. The legacy `output-mode-partial-{surround,height}-virtualizer-enable` isn't modeled either; see [reference.md](reference.md) and the [surround→stereo-base factor](research/adaptive-processing.md#r-surround-boost-stereo-base). |
| Surround-decoder centre spreading | `surround-decoder-center-spreading-enable` | Present in 1345 XMLs, enabled in 0 | Defensive: it would silently drop if a future driver enables it. |
| Woofer-only regulator | `woofer-regulator-enable`, `woofer-regulator-tuning` | Present in 1345, enabled in 0 | Defensive: it would silently drop if a future driver enables it. |
| Independent regulator mode | `regulator-independent-enable` | 1 XML, never enabled | Defensive. |
| Bass-extraction LFE gain | `bass-extraction-lfe-gain` | Present in 1345, enabled in 0 | Defensive: bass-extraction itself is universally off. |
| Channel-gain matrix attributes | `gain_c`, `gain_l`, `gain_r`, `gain_ls`, `gain_rs`, `gain_lfe`, `gain_lrs`, `gain_rrs`, `gain_ltm`, `gain_rtm` | Companion to virtualizer downmix | Tied to the unmodeled virtualizer, so it would only matter once advanced-virt is implemented. See the note below. |

- **Volume-leveler compressor sub-component.**
  - *Carriers*: newer Lenovo AIO / ThinkPad packages, plus Framework's
    `F111:000F` since
    [#73](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/73)
    (2026-08-25), the first non-Lenovo carrier.
  - *Autogain*: where the leveler is bypassed (the HDA default), dropping a
    sub-block of a stage we don't run costs nothing. Where it runs (the
    SoundWire default, or `--enable autogain`), our leveler runs without the
    compressor Dolby pairs with it, which could matter on these 77 devices.
    SoundWire carriers, where that is the default: 36 of the 139 files that
    enable it in the 3641-file corpus (ad-hoc query, 2026-09-25). Unmeasured on
    the 77.
  - *Issue #25*: see the Band B note above.
- **Channel-gain matrix attributes.** Separately, simplified-schema XMLs reuse
  `gain_l`/`gain_r` inside `<audio-optimizer-bands>` as the L/R
  speaker-correction arrays. *Those* are modeled, mapped to the `ch_00`/`ch_01`
  slots (issue
  [#22](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/22)).
  They are unrelated to the downmix matrix in the table above.

The `_UNMODELED_FEATURES` table in `lib/dax/parse.py` carries:

- the two rare-but-real cases in the previous table, DSO and the advanced
  virtualizer;
- four watch-only fields: `peak-level`, `ieq-bands-set`, `regulator-overdrive`
  and `regulator-relaxation-amount`. They warn only when an XML deviates from
  the corpus constants, so they are silent on every shipped tuning today.

It deliberately does *not* list band C's inert elements, or band B's MI
steering, which is on for `dynamic` almost everywhere. They'd fire on every run
for no gain. If a future driver release flips one of them on, the corpus sweep
will catch it before the warning needs to. Everything that *is* listed prints
its detail where the parser meets it, and its ask once at the end of the run,
where the per-band tables can't bury it.

### Why these aren't implemented

Band B has nothing to derive. Band A's sliding bass carries its parameters but
not their semantics, so it waits on a capture (see "What blocks sliding bass").
The two warned features would need either real device measurements or
undocumented Dolby DSP internals:

- **DSO** maps a driver-specific target excursion and a per-band power envelope
  to a real-time gain. The `amount` (1–10) and `speaker-interval` (`720,300` mm
  × 100?) attributes name the dial but not the algorithm. A rough LSP
  `multiband_compressor` band-0 limiter could approximate the bass-protection
  role. The cost is pumping artifacts that DSO specifically avoids, a net
  negative versus dropping it.
- **Advanced speaker virtualizer** is an FFT-domain HRTF-style spatializer. The
  8-int rendering config (`103,32568,6698,5090,1,1,1,1`) is opaque. No LSP/EE
  plugin reproduces this kind of processing. The legacy
  `output-mode-partial-*-virtualizer-enable` blocks were once approximated as a
  stereo widener via `surround-boost → stereo_tools`. A 2026-06 DAX capture
  showed Dolby applies no stereo widening on 2-channel content, so that mapping
  was removed
  ([surround→stereo-base factor](research/adaptive-processing.md#r-surround-boost-stereo-base)).
  The advanced variant is the same unreproducible spatializer one generation
  later.

The warning is the honest outcome. The user knows what's being dropped, and a
future device-level investigation can wire in something better.

---

## 15. SoundWire tuning-filename matching

Auto-detection treats `FUNC` as **preferred, not required**. It first matches
`(man, part)` exactly. Only when nothing matches that way does it fall back to
PCI subsystem + manufacturer (issue
[#26](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/26)).

The SoundWire filename
(`SOUNDWIRE_[SDCAFUNCTION_NN_]MAN_<man>_FUNC_<func>_SUBSYS_<device><vendor>.xml`)
is matched against `/sys/bus/soundwire/devices` IDs and the HD-Audio
controller's PCI subsystem. Across the corpus's 29 Qualcomm Aqstic (`MAN_025D`)
tunings, `FUNC` equals the Linux SoundWire part id (e.g. `FUNC_1318` ↔ part
`1318`). So the original match keyed on `(manufacturer, part) + PCI subsystem`.

A Samsung Galaxy Book6 driver package falsifies that as a *general* rule. The
package is for Cirrus Logic `MAN_01FA` with cs35l56 amplifiers, on Panther Lake,
at `xml_version 3.8.0`. Its tuning is
`SOUNDWIRE_SDCAFUNCTION_10_MAN_01FA_FUNC_3556_SUBSYS_CA0A144D.xml`. Yet sysfs
reports SoundWire parts `3557` (the six cs35l56 amps) and `4245` (the SDCA
codec), and neither equals `FUNC_3556`. The XML's own `security-key`
(`SOUNDWIRE\SDCA_FUNCTION_10&MAN_01FA&FUNC_3556&…&SUBSYS_CA0A144D`) confirms
that `FUNC` is a Dolby/Cirrus device id and `SUBSYS_<pci>` is the per-device
key. The package ships five tunings, one per SKU, identical but for `SUBSYS`
(`F020144D`, `C1DC144D`, `C1DE144D`, `C910144D`, `CA0A144D`). All five are
`MAN_01FA_FUNC_3556`.

The exact tier still matters, because some Lenovo SKUs ship *two* tunings that
share `MAN`+`SUBSYS` but differ in `FUNC` (e.g. `SUBSYS_383917AA`: `FUNC_0721`
vs `FUNC_1320`). The detected part still disambiguates those. Galaxy Book6 is
XML-derived only: it generates cleanly but is unvalidated by ear, since the
maintainer has no access to the hardware.

The first field report (issue
[#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27))
produced two diagnosis follow-ups:

1. `--speaker-info` was double-counting these amps, and reported six as "12
   speakers". Each cs35l56 is a mono amplifier, and SoundWire enumerates one
   slave per amp chip. The layout estimate counts each enumerated amp once. It
   probes the amp's channel count from the sink data-port DisCo props
   (`dpN_sink/max_ch`, kernel ABI `sysfs-bus-soundwire-slave`) rather than
   assuming stereo.
2. The report's "quiet, smartphone-like" sound is a driver/firmware-layer issue,
   not the preset, and is confirmed on-device. Per the cs35l56 kernel driver
   doc, a missing per-amp `.wmfw`/`.bin` (under `/lib/firmware/cirrus/`) leaves
   the amp playing a mono mix with no voicing/protection. The only authoritative
   signal is the kernel log, since no sysfs/debugfs exposes amp audio-state. In
   the #27 capture, all six amps load the base DSP ROM, then the kernel logs
   `FIRMWARE_MISSING` / `Calibration disabled due to missing firmware controls`
   / `Can't read tuning IDs`. These are CS35L57 parts, and the machine-specific
   Cirrus tuning is absent on this Fedora 44 build. So the amps run without
   voicing or protection. Root cause is the distro `linux-firmware` gap for this
   SKU, not the converter. Two diagnostic lessons fed back into
   `--speaker-info`:
   - (a) File presence can't certify; the log is authoritative. Generic
     `cirrus/cs35l*` blobs were *present* (≈1785 files) while the machine blob
     was missing.
   - (b) The first kernel-log marker set caught only boot/init timeouts, and
     reported "no errors" on this exact failure. The scan flags the
     firmware-missing signature too, plus the equivalent TI `tas2781-*` and
     Realtek `rt1320-sdw.c` firmware-load failures. Each marker was verified
     verbatim against its driver source (`cs35l56-shared.c` / `cs-amp-lib.c` for
     Cirrus). The clean-log line tells the reader to eyeball the log rather than
     trust the scan.

**Addendum (2026-07-21, issue
[#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27)
follow-up) — first amp-DSP-voiced device.**

On devices whose DAX host tuning is near-flat because the voicing ships in amp
firmware, the converter has little to offer once the firmware is installed. The
honest outcome is "install the firmware, skip the preset".

The reporter closed the firmware gap himself by extracting the CS35L57 tuning
from Samsung's Windows driver
([write-up](https://github.com/JeanLuX/notebook/blob/main/samsung-galaxy-book6-ultra/AUDIO-CIRRUS-FIRMWARE-EXTRACTION.md)).
The result was "night and day": near-Windows quality with no DAX-derived
processing at all. Per that write-up, the Windows driver package holds one
shared `.wmfw` under `fw/` and per-SSID tuning dirs under `tn/`. His SKU's dir
is `CA0A`, mirroring the five per-SKU `SUBSYS_*` XMLs above. The files are
renamed to the linux-firmware convention
`cs35l57-b2-dsp1-misc-144dca0a-lXuY.{wmfw,bin}`. Calibration comes separately
from EFI variables. "Calibration applied" in the kernel log is the success
marker.

With the amps properly voiced, the reporter found the converter's output a net
negative: the default preset "degrades the sound dramatically", and with
`--disable bass-enhancer --disable volmax --disable regulator` it was at best
neutral. All three were disabled together, so the run allows no single-filter
attribution. Remember that confound before blaming any one default.

The likely mechanics follow from the reporter's pasted stdout and the
converter's own logic. This XML's host-side tuning is near-flat: audio-optimizer
all-zero on both channels, IEQ ≤ ±1.5 dB, no PEQ filters, regulator
`threshold_high` flat at 0 dB, stress all-zero. The voicing lives in the Cirrus
amp-DSP tuning, not in DAX host processing. A flat 0 dB `threshold_high` means
`make_regulator` disables every band, because a threshold ≥ 0 dBFS never
triggers. So the regulator emits but limits nothing, and the volmax +6 dB riding
its input-gain hits the brickwall limiter untamed. The issue-#23 "per-band
compression tames the boost" rationale silently doesn't apply. The generator
warns when this shape occurs.

Correction, 2026-09-25: these mechanics and the counts below predate the
coupled-bands default of 2026-08-11. Under that default such a tuning gets a
full-band limiter, and `lib/report/profile.py` gates the warning off. It
survives for `--disable coupled-bands` and for a tuning with no qualifying
zone. The
[inert-regulator caveat](research/loudness-and-limiting.md#r-volmax-boost-slot)
has the post-flip count.

The shape is common, not a Samsung quirk. The warning condition is: regulator
emitted, volmax > 0, and every `threshold_high` ≥ 0 dB.

| Scope | Condition holds on |
|---|---|
| Profile rows | 7.3% |
| Distinct tunings, on at least one profile | 338/979 |
| A plain default run (normal mode, first profile) | 142/978 (14.5%) |

Source: an ad-hoc sweep on 2026-07-21, running the converter's `parse_xml` over
the `tests/corpus` discovery walk. It covered 46,336 `internal_speaker` profile
rows across 3,055 reachable files.

201 of the 338 fire only on voice-family profiles. That is consistent with the
volmax-in-`voice` concentration in the
[#23 corpus analysis](research/loudness-and-limiting.md#r-volmax-boost-slot)
("input-gain only does anything when the regulator is active"). The default-run
cases are `dynamic`/`movie` at +5…+9 dB. These are corpus counts, not audibility
claims. Firing means the taming rationale doesn't apply, not proven squash.

The default-on SoundWire `bass_enhancer` also adds harmonics on top of an amp
that now does real bass management. This is the second field report against that
default, after issue #29 (the unvalidated
[SoundWire bass-enhancer constants](research/virtual-bass.md#r-soundwire-bass-enhancer-constants)).

---

## 16. HDA tuning-filename matching

Matching keys on the `(DEV, SUBSYS)` pair, with subsystem-only kept as a warned
fallback tier. This is the same preferred-not-required tiering as the SoundWire
`FUNC` rule (§15). The `DEV` token is the codec device id: the low 16 bits of
the HDA vendor id (`0x10EC0287` → `0287`).

HDA-style filenames (`DEV_<codec-device>_SUBSYS_<codec-subsystem>_PCI_SUBSYS_…`)
were originally matched on the codec subsystem alone, with `tuning_version` as
the only tiebreak. Issue
[#33](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/33)
falsified the uniqueness assumption on an IdeaPad Pro 5 14APH8 (ALC287,
subsystem `17AA38C5`). Its driver store ships both `DEV_0287_SUBSYS_17AA38C5`
(tuning_version 8) and `DEV_0257_SUBSYS_17AA38C5` (tuning_version 11). Lenovo
reuses the subsystem id across an ALC287 and an ALC257 SKU. A Yoga Slim 7 ProX
driver package independently pairs `17AA38C5` with an ALC257 in its
Fortemedia/SAM `.dat` names. The version tiebreak therefore selected the other
codec's tuning, which the reporter heard as clearly worse.

This is systematic, not a one-off. Across the reachable corpus (2836 files, a
2026-07 count via the `tests/corpus` discovery walk), 83 of 766 distinct
HDA-style subsystems (11%) appear with more than one `DEV` token. All 83 are
Lenovo (`17AA…`), dominated by `0257`/`0287` pairs, with a few three-way splits
(e.g. `17AA3852`: `0230`/`0257`/`0287`).

`tuning_version` still tiebreaks within a tier, since duplicate driver-store
copies are common.

---

## 17. Bus-prefixed filenames

> Measured 2026-08-22 over the full corpus (3638 files), not the frozen
> 2795-XML cohort the sections above use.

The same tuning ships under several filename spellings: `DEV_…`,
`HDAUDIO_DEV_…`, `INTELAUDIO_DEV_…`. These are the duplicate copies that §16's
`tuning_version` tiebreak picks between.

The prefix is the Windows hardware-ID namespace the `.inf` binds the device
under. A Dolby extension `.inf` routinely binds both HD-Audio and Intel-SST
enumerations of one codec:

```
%Device.ExtensionDesc% = DeviceExtension_Install,INTELAUDIO\FUNC_01&VEN_10EC&DEV_0257&SUBSYS_17AA3810
%Device.ExtensionDesc% = DeviceExtension_Install,HDAUDIO\FUNC_01&VEN_10EC&DEV_0257&SUBSYS_17AA3810
```

So the package ships one XML per hardware ID. The prefixed files are not
produced by installing the package, a belief this doc and `corpus.md` both
previously recorded. Two measurements refute it:

- The `ext_realtek_lenovo_ideapad` package
  ([corpus.md](corpus.md#publicly-downloadable-driver-packages)) was unpacked
  with `innoextract` and never installed, yet 58 of its 60 tunings are prefixed.
- The development machine's *installed* DriverStore is mostly bare: 199 of 219.
  All 20 prefixed files in that installed store exist verbatim and
  byte-identical inside an extracted package.

The pairs are the same audio. Across all 9 device ids that carry both spellings,
the two files are byte-identical except for `tuning_version`, `tuning_date` and
`security-key`. The last is bus-specific, which is why a file-digest dedup
cannot merge them. The package's 24 content-unique tunings are 18 distinct DSP
configurations.

The tiebreak is therefore cosmetic, and its direction is not systematic, so
"prefer `INTELAUDIO`" would be the wrong rule:

| Model | `HDAUDIO` | `INTELAUDIO` |
|---|---|---|
| Y540-15ICH (issue [#70](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/70)) | 4 | **5** |
| Y540-17ICH | **16** | 15 |
| Y545-15ICH | **11** | 10 |

Ranking by `tuning_version` is right, and either pick yields the same preset.

Where this can arise is bounded. `HDAUDIO_`/`INTELAUDIO_` filenames stop after
`xml_version` 3.2.1:

| `xml_version` | Prefixed files |
|---|---|
| 3.2.0 | 250 of 287 (87%) |
| 3.2.1 | 34 of 947 (4%) |
| 3.4.2 and later | 0 of 2404 |

No file at `xml_version` 3.4.2 or later carries one, and the corpus holds no
3.3.x–3.4.1 files. Qualcomm Aqstic's `AUCD_` prefix is a separate namespace and
does persist into current tunings. It is not a duplicate spelling of a bare
name.

It is still reachable today. Current omnibus packages carry legacy
3.2.0/3.2.1 tunings for old SKUs: 35 prefixed files in
`ext_lenovo_AIO_rtk_22h2_24h2_25h2_v10.1029.1430.37` alone. A *colliding* pair
needs a second condition: a per-model package that ships both enumerations of
one codec. All 9 collisions sit in the per-model subfolders of
`ext_realtek_lenovo_ideapad`. The omnibus packages carry both prefixes but never
for the same device.

When auto-detection reports two XMLs matching one device and picks the higher
`tuning_version`, that is a legacy-package artifact with no audible stake. A
device-report reply need not chase which one was chosen.

---

## Interesting observations

1. **No Intel Fusion devices found.** The `fusion_ext_intel` driver package
   shares the same XML files as `dax3_ext_rtk`. That suggests Intel SST-based
   Dolby uses identical tuning to Realtek-based Dolby.

2. **Music profiles are the MBC outlier.** 36% enable MBC on music vs 4% on
   dynamic (§2). That suggests MBC is primarily a loudness tool, not a
   protection feature.

3. **voice_onlinecourse is the safest profile.** It has ~0% MBC, 0% VL amount, 4
   dB volmax and the simplest chain. Dolby's own tuning for speech entirely
   disables the compressor and uses the gentlest volume leveler. A good template
   for a "no artifacts" preset.

4. **Hard limiting (slope=16) is the overwhelming majority (95.7% of rows,
   §6).** Dolby engineers prefer true brickwall limiting on the regulator for
   most laptop speakers.

5. **Regulator curves are many but shared.** There are 408 distinct threshold
   curves; §7 explains why that is not comparable to the older 399, and how
   often a tuning's curve is its own.

---

## Open follow-ups (from the 2026-06 re-derivation)

Surfaced by the 2483-XML re-derivation. Each item carries its own status.

1. **Newer-SoundWire regulator gap (code).** ✅ **Resolved (2026-06-17).** The
   real schema was a per-channel `<ch_00>…<ch_07>` layout. It was not the
   `isolated_band` sub-schema this list first claimed, which is an unrelated
   older-schema sibling. `resolve_channel_or_direct` reads `ch_00`, recovering
   real per-band limiting on `SUBSYS_37A317AA`'s 9 profiles. The `[0.0]*20`
   fallback only fires, and warns, on a genuinely empty tuning. Scope was
   re-derived via `corpus_audit`'s `threshold_schema`: 9 profiles/1 device, with
   36,620 others untouched on the 2795-XML cohort (2026-08-03). DSO and the
   advanced virtualizer for that device remain unmodeled (§14).
2. **Asymmetric L/R PEQ peak gain (investigate).** ✅ **Resolved (2026-06-17):
   keep global-max.** Re-derived via `corpus_audit`: 131 rows / 10 devices
   differ, with a median of 1 dB and a max of 7 dB, only on Lenovo
   convertible/AIO. EE's equalizer has only a single `output-gain`. Applying
   `max(L,R)` equally to both channels preserves the per-speaker L/R
   relationship at every frequency, and a per-channel trim would corrupt it. No
   code change (§12).
3. **Voice-AO divergence rate (re-derive).** ✅ **Resolved (2026-06-17).** A
   methodology-matched re-derivation (`corpus_audit`, per-endpoint, full-schema,
   internal_speaker/normal) found 52% per-endpoint / 63% per-device diverge on
   the 2483-XML cohort (§8 has the 2026-08-03 re-run), well below the original
   97%. The newer AIO packages differentiate the voice AO curve less often. §8
   updated.
4. **1-band MBC audibility (validate).** Music 1-band MBC band-0 ratios reach
   ~6:1 at thresholds to −12 dB (§2). That is real compression, not just makeup.
   Audition a high-ratio example. The `mbc-1band` path is still experimental.
5. **`peak-level` disposition (minor).** ✅ **Resolved (2026-06-17): keep
   watch-only.** Re-derived via `corpus_audit` (peak-level section): 244/27508
   nonzero (−13 on 175, then −6/−8/−4/−15/−32/−5/−1), i.e. zero on 99.1% of
   rows. The 1/16-dB→limiter-threshold interpretation is unverified. Mapping it
   wrong would trade away headroom audibly on the <1% that set it. So
   watch-listing in `_UNMODELED_FEATURES` (warn on deviation, don't act) is
   correct. It lives under `tuning-vlldp`, and the watch's `.//peak-level` xpath
   catches it regardless.
6. **Re-run on new driver pulls (process).** ✅ **Done (2026-08-03)** for the
   2795-XML cohort. Every aggregate above was regenerated with
   [`tools/corpus_audit.py`](../tools/corpus_audit.py). It is still a standing
   process for the next pull. Four blocks remain outside the tool and were left
   as-is rather than restated: §8's rail-pinning counts, §9's per-XML and
   per-speaker filter distributions, §13's `default_profile` paragraph, and the
   `voice_onlinecourse`/`personalize` rows in §3 and §5. §8's counts need
   queries over the content-unique set, `geq_maximum_range`, `xml_version` and
   AO peak-to-peak. For the §3 and §5 rows, the tool prints only the eight most
   common profiles. Adding the missing queries to the tool is the next step.
7. **Galaxy Book6 quiet-output cause (device-gated).** ✅ **Resolved
   (2026-06-30).** The reporter's `--speaker-info` + kernel log confirmed the
   §15 hypothesis. The cs35l56/57 amps load the base DSP ROM but log
   `FIRMWARE_MISSING` / `Calibration disabled…` / `Can't read tuning IDs`. The
   machine-specific Cirrus tuning is missing on this Fedora 44 build, so they
   run without voicing, quiet and flat, regardless of the preset. Root cause is
   the distro `linux-firmware` gap for this SKU, so route the reporter there.
   The converter is not implicated. `--speaker-info` flags those markers (see
   §15). **Closed out (2026-07-21):** the reporter self-fixed the gap by
   extracting the CS35L57 `.wmfw`/`.bin` from Samsung's Windows driver
   ([his write-up](https://github.com/JeanLuX/notebook/blob/main/samsung-galaxy-book6-ultra/AUDIO-CIRRUS-FIRMWARE-EXTRACTION.md)).
   That confirmed the diagnosis end-to-end. The post-fix preset verdict was
   *negative*, and the device is *not* added to the README tested table. The §15
   addendum has the outcome and mechanism.
8. **Zenbook S14 UX5406SA partial-success report (issue
   [#29](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/29),
   reporter-gated).** First cs42l43-codec device: SoundWire part 0x4243 jack
   codec + 4× cs35l56, PCI SSID `1043:1E13`. Unlike the Galaxy Book6 above, it
   is *not* a firmware gap. It shows no kernel-log failure markers, and upstream
   linux-firmware ships this SSID's per-amp Cirrus tuning
   (`cs35l56-b0-dsp1-misc-10431e13-amp1..4.bin`). It is also the first device
   where the Dolby `FUNC` token should *equal* the amp part id (0x3556). That
   makes it a clean §15-matching datapoint once the XML filename is confirmed.
   The reporter's symptoms were "too bass boosted", chassis resonance and wonky
   dynamics, with the music profile better than dynamic. They drove the
   2026-07-03 removal of two vestigial SoundWire boosts
   ([dialog-enhancer gain ceiling](research/adaptive-processing.md#r-dialog-enhancer-gain-ceiling),
   [convolver headroom restore](research/loudness-and-limiting.md#r-convolver-headroom-restore)).
   The reporter's A/B is also pending for the bass-enhancer default
   ([SoundWire bass-enhancer constants](research/virtual-bass.md#r-soundwire-bass-enhancer-constants)
   / issue #14), and the device is a candidate second-device loud capture for
   [the MBC ratio and time constants](research/adaptive-processing.md#r-mbc-ratio-time-constants)
   and
   [fixed dynamics constants](research/loudness-and-limiting.md#r-fixed-dynamics-constants).
   **XML received 2026-08-27.** It was pasted inline on the issue, with security
   key
   `SOUNDWIRE\SDCA_06&MAN_01FA&FUNC_3556&…&SUBSYS_1E131043&AGGREGATEDSPEAKER=…`,
   `xml_version 3.7.1`, tuned 2024-12-10. It confirms the §15 datapoint:
   `FUNC_3556` *equals* the cs35l56 part id here, unlike the Galaxy Book6
   (`FUNC_3556` vs part `3557`). So `FUNC` matches the part on some Cirrus
   packages and not others, and the preferred-not-required tier stays right. The
   endpoint is 2-channel (`total_count=2`) behind four amps, so the
   woofer/tweeter split lives in the per-amp Cirrus firmware, not the XML. The
   XML changes the open A/B. The profile the reporter calls bad, `dynamic`, is
   the one where the tuning enables the volume leveler (amount 5, DRC on) and
   every `mi-*-steering` switch. Our SoundWire path runs that leveler by default
   with no steering. That is the failure mode that made the HDA leveler opt-in
   (issue #25). `--disable autogain` (008b4d6, v2026.08) did not exist when the
   reporter tested. So the `dynamic` A/B with it off is the pending ask. The DAX
   capture still decides the bass-enhancer default
   ([SoundWire bass-enhancer constants](research/virtual-bass.md#r-soundwire-bass-enhancer-constants)).
   Still pending from the reporter: that A/B, the capture and the generation
   stdout.
