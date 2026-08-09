# Cross-device DAX3 findings

> Interpretive analysis of the parameter schema used by Dolby DAX3 tuning XML (distributed publicly as part of Windows audio driver packages), for the purpose of Linux interoperability. No verbatim tuning arrays are reproduced.

The original cohort was **196 DAX3 tuning files** spanning **3 Realtek codec variants**
(ALC257, ALC285, ALC287) found in the `dax3_ext_rtk` and `fusion_ext_intel` driver
packages. Successive driver-package pulls (`ext_lenovo_AIO_rtk`, `ext_thinkpad_AIO_rtk`,
`ext_capg_thinkpad`, `ext_amd_thinkpad_AIO`, plus newer IdeaPad/SoundWire
packages) grew it to the current
**2795 tuning XMLs / 40732 profile rows** spanning **14 HD-Audio codec DEV IDs
(mostly Realtek ALC) plus SoundWire/SDW**. [`reference.md`](reference.md) documents how the script maps one
specific device; this doc captures what's universal across the ecosystem and what
varies from device to device, so readers can judge which parts of the pipeline are
portable and which are tuned.

What that collection is made of, and which OEM driver package each part came
from, is [`corpus.md`](corpus.md).

> **All figures below are from the 2795-XML cohort audited 2026-08-03**, except
> where a passage is explicitly marked as the original 196-XML cohort (kept for
> the historical methodology notes), or carries its own later re-derivation
> date. Regenerate any aggregate here with
> [`tools/corpus_audit.py`](../tools/corpus_audit.py) — including the §12 and
> §14 checks, which were ad-hoc queries until 2026-08-03 and are now part of
> the committed sweep.

> **The file counts here predate a filter fix (2026-08-09) and are known to be
> off.** The sweep tool used its own filename test rather than the converter's,
> so it counted `_dmic`/`_amic` microphone companions as tunings and skipped the
> `HDAUDIO_`/`INTELAUDIO_`/`PCI_`/`AUCD_` spellings of real ones. Row-based
> distributions move only by what the added devices contribute; the file, device
> and package counts need a re-derivation pass. See
> [`corpus.md`](corpus.md#reconciling-the-counts).

Original `dax3_ext_rtk` + `fusion_ext_intel` cohort (`dynamic` profile rows):

| Codec  | Devices | MBC enabled | MBC disabled |
|--------|---------|-------------|--------------|
| ALC257 | 236     | 19 (8%)     | 150 (64%)    |
| ALC285 | 27      | 0 (0%)      | 14 (52%)     |
| ALC287 | 81      | 4 (5%)      | 63 (78%)     |

> **Known inconsistency (review 2026-06):** this original-cohort table's columns
> don't reconcile — per row, enabled + disabled ≠ Devices (169/236, 14/27,
> 67/81), and the Devices column sums to 344 against the 196 files above. The
> per-codec *enabled* counts (19/0/4 → 23 total) do match the original §2 and are
> the load-bearing numbers; the Devices and disabled columns need a re-count
> against the original package set before being relied on. Current-cohort MBC
> rates are in §2 below.

Current 2795-XML cohort — XML count per codec (not dynamic-profile count):

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

> All files live under `internal_speaker` endpoints — no headphone or external
> tunings in any cohort. The newer packages introduce many non-`normal` operating
> modes (tablet/stand/tent/lid_close/etc., see §13) that the original 196-XML
> cohort did not exercise.

---

## 1. Universal constants

These parameters are **identical across every device and profile** examined (a
handful of newer-schema exceptions are footnoted):

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
| `dialog-enhancer-ducking`          | 0 (mostly)         | 98.5% of rows; 616/40732 are non-zero (8 or 6) — not universal |
| `regulator-overdrive`              | 0                  | Always zero where present          |
| IEQ curve preset                   | `ieq_balanced`     | Only curve used anywhere           |

The script skips bass enhancer, virtual bass, graphic EQ, volume modeler, and
non-zero system/pre/post gains because none of them meaningfully exist in the wild.

The three IEQ voicing curves themselves are also universal (re-derived
2026-07-31 by an ad-hoc sweep over the 1,825-XML corpus): every speaker XML —
1,793 files; the other 32 are `_dmic`/`_amic` microphone tunings with no
`ieq_*` elements — carries all three of `ieq_balanced` / `ieq_detailed` /
`ieq_warm`, and each curve's 20-value array is **byte-identical across every
device**. The variants are Dolby-global voicings, not device tunings; the
device-specific correction (audio-optimizer + PEQ) applies identically under
every one. This is the empirical basis for `dolby_to_pipewire.py --variant`'s
fixed choice list and its `balanced` default (the preset row above: every
device's profiles select `ieq_balanced`).

---

## 2. Multi-band compressor — the minority feature

Only **100 of the 1589 files** whose `dynamic` profile declares
`mb-compressor-enable` (6%) switch it on — equivalently **189 of the 4225
dynamic-profile *rows* (4%)**, since some files carry several endpoint modes;
both denominators appear in this doc, so both rates are given here once. (The
file denominator counts files that declare the field: a file omitting it is not
a device that chose to leave the compressor off. The earlier "77 of 2451" used a
denominator no committed query reproduces.) This is the most important finding:
**MBC is the exception, not the rule** — except on `music`, where it reaches 36%.

| Profile              | MBC=1 | MBC=0 |
|----------------------|-------|-------|
| dynamic              | 189   | 4036  |
| game                 | 214   | 3925  |
| movie                | 226   | 3999  |
| music                | 1518  | 2707  |
| voice                | 134   | 4091  |
| voice_onlinecourse   | 18    | 2603  |
| off                  | 1     | 4224  |

Music profiles enable MBC far more often (**36%**), confirming MBC is used for
loudness maximisation on premium speakers, not as a universal safety feature.

### Band-count distribution (MBC-enabled profiles)

Current cohort, by `group_count` (a populated `band_group` count of 1–4):

| `group_count` | Enabled profiles | Disabled (but populated) |
|---------------|------------------|--------------------------|
| 1             | 633              | 21918                    |
| 2             | 1283             |   597                    |
| 3             | 346              |   494                    |
| 4             | 459              |   484                    |

Two noteworthy wrinkles the old 2-band decoder masked:

- **633 profiles enable MBC with `group_count=1`** — single-band, full-spectrum
  dynamics. Almost entirely on the `music` profile (630 of 633), typically used
  as a loudness maximiser: band-0 ratios range from 1:1 (pure makeup) up to ~6:1
  and thresholds from 0 dB to −12 dB, with fast attack/release. Emitted from the
  `mbc-1band` experimental path since the guard was relaxed; LSP MBC accepts a
  single enabled band with no split frequency and bands 1-7 disabled.
- **978 profiles declare 3- or 4-band tunings but gate the compressor off**
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

Devices that do enable MBC show wide ratio variation. (Cluster table from the
original 196-XML cohort — the Devices column sums to 21 of the 23 dynamic-profile
MBC enables then; the cluster shape is illustrative, not a current-cohort count.)

| Ratio  | Threshold    | Makeup  | Devices |
|--------|--------------|---------|---------|
| 1.1:1  | −4.0 dB      | 2.0 dB  | 4 (gentle)     |
| 1.7:1  | −4 to −11 dB | 2–4 dB  | 8 (moderate)   |
| 2.0:1  | −5.3 dB      | 1.6 dB  | 4 (moderate)   |
| 5.0:1  | −3.0 dB      | 2.0 dB  | 1 (aggressive) |
| 10.0:1 | −4.0 dB      | 3.5 dB  | 4 (limiting)   |

The development device (ALC287 22E6) uses 1.7:1 @ −6.4 dB with 2 dB makeup — moderate.
The 10:1 devices are essentially using the compressor as a limiter.

For the ~96% of dynamic-profile rows without MBC, the regulator alone provides
dynamics control, which is a much simpler and safer signal chain.

---

## 3. Volume leveler amount — wide variation

The `vl_amount` parameter (0–10 scale) varies significantly across devices
(current-cohort row shares):

| Profile               | Distribution                                               |
|-----------------------|------------------------------------------------------------|
| **dynamic**           | 5 (49%), 3 (19%), 4 (17%), 2 (5%), 7 (4%), 1 (3%)          |
| **movie**             | 5 (50%), 3 (20%), 4 (16%), 2 (4%), 7 (3%), 1 (2%)          |
| **music**             | 2 (56%), 0 (18%), 3 (15%), 4 (5%), 1 (3%)                  |
| **game**              | 0 (95%), 4 (2%), 2 (1%)                                    |
| **voice**             | 0 (99%), 2 (<1%)                                           |
| **voice_onlinecourse**| 0 (99%), 2 (<1%)                                           |

The development device uses `vl_amount=2`, which is on the **gentler end** for the
dynamic profile. The most common value is **5** (49% of dynamic rows).

---

## 4. Volmax-boost — the loudness ceiling

`volmax-boost` (1/16 dB, in tuning-cp) defines the maximum gain the volume leveler may
add above the output target. Distribution for the `dynamic` profile (current cohort):

| Boost       | Share        |
|-------------|--------------|
| 4 dB (64)   | 5%           |
| 5 dB (80)   | 7%           |
| **6 dB (96)** | **78%**    |
| 7 dB (112)  | 2%           |
| 8 dB (128)  | 3%           |

**6 dB is the dominant value** (78% of dynamic rows). The development device also uses 6 dB.

Notable per-profile patterns:

- **voice**: polarised — 6 dB (36%), 9 dB (33%), 8 dB (22%) — highest boosts, for speech intelligibility
- **voice_onlinecourse**: 4 dB (98%) — the gentlest, avoids pumping on long-form speech
- **music**: 6 dB (77%) with some at 3–4 dB
- **off**: 0 dB (99%) — effectively disabled

The voice profile's high boost combined with a disabled compressor means the regulator
alone has to catch peaks on that profile.

The script applies `volmax-boost` as `input-gain` on the regulator
(`multiband_compressor#1`) — so per-band limiting tames it before the brickwall —
falling back to `limiter#0.input-gain` when the regulator is absent;
`--volmax-slot output-gain` restores the older placement (issue #23, see
`design-notes.md`). Can be disabled with `--disable volmax` if the boost drives the
brick-wall limiter into audible gain reduction on already-loud masters.

---

## 5. Dialog enhancer — profile-dependent behaviour

Current-cohort enable rates:

| Profile               | Enabled | Disabled |
|-----------------------|---------|----------|
| dynamic               | 58%     | 42%      |
| movie                 | 56%     | 44%      |
| music                 | 0%      | 100%     |
| game                  | 3%      | 97%      |
| voice                 | 55%     | 45%      |
| voice_onlinecourse    | —       | —        |

Dialog enhancer is a **speech enhancement feature**, consistently disabled for music
and game profiles across all devices. (Enable rates on dynamic/movie are lower than
the original cohort's 86% — the newer Lenovo-AIO packages leave it off more often.)

> `voice_onlinecourse` shows no rate because `corpus_audit.py` reports only the
> eight most common profiles and it ranks tenth; the `personalize` amount is the
> `personalize_user1`/`_user2` figure (94%), since the bare `personalize` profile
> (86 rows) is likewise below the cut. Widen the profile list in the tool before
> quoting either.

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

The `regulator-distortion-slope` (1/16 scale) controls how hard the regulator limits.
Current-cohort distribution:

| Slope        | Effective ratio       | Share (of 26214 rows declaring it) |
|--------------|-----------------------|----------------|
| 0            | (no limiting)         | <1% (12)       |
| 4 (0.25)     | 1.3:1 — gentle        | <1% (88)       |
| 6 (0.375)    | 1.6:1                 | <1% (64)       |
| 8 (0.50)     | 2:1 — moderate        | 2% (507)       |
| 9 (0.5625)   | ~2:1                  | <1% (27)       |
| 11–12        | ~3–4:1 — firm         | **1.5%** (397) |
| 13 (0.8125)  | ~6:1                  | <1% (24)       |
| **16 (1.00)**| **∞:1 — hard limiter**| **95.7%** (25095/26214) |

(The original 196-XML breakdown had slope=16 at 53%; the AIO-RTK packages that
dominate the current cohort use the hard limiter far more.)

The development device uses slope=16 (hard limiter), which is the **most common**
setting. The hard limiter mode means the regulator acts as a brickwall at its threshold.

**Implication for pipeline design:** when slope=16 the regulator is a brickwall
limiter, so on the large majority of profile rows (95.7%) the regulator *is* the
brickwall limiter. (Rows, not devices — no per-device slope share is computed.) The explicit output limiter added to the EasyEffects chain is redundant on
those devices and essential on the soft-slope minority. See `docs/design-notes.md`
for why both exist.

**Real-world evidence the regulator chain matters.** On a Snapdragon X Yoga Slim
7x running Linux without DSP-level Dolby protection,
[taprobane99](https://github.com/taprobane99/Lenovo-Yoga-Slim-7x-Dolby-Linux-Audio)
had to manually trim four ALSA UCM mixer levels (`PA Volume 12→6`, two
`Digital Volume 68→58`, one `Digital Volume 84→5`), disable the WSA884x amp's
internal compressor (`COMP Switch 1→0`), and cap WirePlumber to 7% just to
avoid blowing the speakers — about 22 dB of headroom thrown away because the
kernel-level audio stack has no Dolby-equivalent per-band limiter. The
regulator we emit is doing the work that lets the rest of the chain run
unattenuated.

---

## 7. Regulator thresholds — per-band frequency shaping

Each device has a unique 20-band regulator threshold curve. General shape of
`threshold_high`:

- **Range**: −60 dB to 0 dB across bands
- **Low bands** (sub-bass): deepest thresholds (−60 to −30 dB), protecting small
  laptop speakers from excursion damage
- **High bands**: typically 0 dB (no limiting)
- **Mid bands**: vary per device — the "speaker personality" region

There are **408 distinct `threshold_high` curves** across the 2795-XML cohort —
nearly every speaker tuning has a custom regulator curve. This is the most
device-specific parameter in the entire chain.

> Not a like-for-like successor to the 399 quoted for the 2483-XML cohort. The
> committed counter (added 2026-08-03) resolves `preset=` references to the curve
> they name, where the earlier ad-hoc query counted only literal `value=`
> strings, so the two differ by methodology as well as by cohort. No committed
> tool reproduces 399; treat the delta as unknown rather than as growth of 9.

---

## 8. Audio optimizer — voice profile often uses different curves

A **majority of devices** use a **different audio-optimizer curve for the `voice`
profile** compared to `dynamic` / `movie` / `music` / `game` (which all share the
same curve) — but it is far from universal in the current cohort.

The voice AO curve typically:

- Reduces low-frequency correction (less bass boost)
- Adjusts mid-frequency emphasis for speech clarity
- Shares the same high-frequency rolloff

> **Re-derived 2026-06-17** with a methodology-matched query (`corpus_audit`
> voice-AO divergence: per-endpoint, `internal_speaker`/`normal` only, full-schema
> only — simplified `gain_l`/`gain_r` XMLs excluded — comparing the resolved voice
> AO vector vs the dynamic AO vector with exact-integer inequality; **re-run
> 2026-08-03**): **55% per-endpoint (1076/1959)** and **62% per-device (407/659)**
> of devices diverge on at least one endpoint (718 simplified-schema endpoints
> excluded — a different quantity from the 674 content-unique XMLs below). This is well
> below the original 196-XML cohort's **97%** — the drop is real, not a methodology
> artifact: the newer Lenovo-AIO packages that dominate the current cohort
> differentiate the voice AO curve far less often (the same direction as the
> dialog-enhancer enable-rate drop in §5). So the "most devices" claim now holds
> only as a slim per-device majority, and a large minority (~37%) ship an identical
> voice AO. Regenerate with [`tools/corpus_audit.py`](../tools/corpus_audit.py).

All non-voice profiles (dynamic, movie, music, game, personalize) share identical AO
curves. The script processes each profile independently, so the voice preset
automatically picks up the voice-specific AO curve when generated from a device
that has one.

### Curves pinned at the declared gain range

Some tunings ask for the largest per-band gain the format expresses. `<setting>
<geq_maximum_range>` states that range where present — always `192` (= +12.0 dB at
1/16 dB) — and a minority of tunings put one or more bands right at it.

Measured over the 674 content-unique XMLs carrying an `internal_speaker`/`normal`
audio-optimizer block (first profile, both channels, raw value ≥ the declared range,
defaulting to 192 where the file omits it):

| population | ≥1 band at the rail | median peak-to-peak |
|---|---|---|
| simplified schema (148) | 32 (22%) | 15.2 dB |
| full schema (526) | 84 (16%) | 14.0 dB |

Within the simplified subset the older schema version is where they cluster, and it
is also the only one that declares the range at all:

| `xml_version` | XMLs | ≥1 pinned | declares `geq_maximum_range` | median p-p |
|---|---|---|---|---|
| 3.2.0 | 27 | 16 (59%) | 27/27 | 19.9 dB |
| 3.2.1 | 121 | 16 (13%) | 0/121 | 15.0 dB |

This is confounded with package vintage — the 3.2.0 files concentrate in the older
`ext_thinkpad_AIO_rtk` stores — so read it as "older tunings are more likely to sit
at the rail", not as a schema-version rule.

Why it matters for the generated preset: the FIR is peak-normalised, so a boost at
the rail becomes a deep relative cut everywhere else, and the bands it boosts are
often ones the regulator leaves unlimited. Issue #46's T495 (`17AA5125`, 3.2.0) is
the worst case seen so far — 23.7 dB peak-to-peak with three bands at the rail,
wider than 95% of simplified tunings — and a run now warns when the largest boost
lands on an unlimited band (8% of parseable tunings, against 16% for the
all-inert-regulator warning beside it). **These particular counts have no
committed query yet** — [`tools/corpus_audit.py`](../tools/corpus_audit.py) does
not compute content-hash dedup, `geq_maximum_range`, `xml_version` or the AO
peak-to-peak spread, so regenerating them means an ad-hoc sweep (or adding those
four to the tool, which is the better fix).

### Curves shipped with the optimizer switched off

A profile can carry a non-zero audio-optimizer curve and still declare
`audio-optimizer-enable=0`. The converter reads that gate and drops the curve,
keeping only the IEQ voicing (reference.md, chain row 1); before commit
`f5473c5` it applied the curve regardless.

> **Derived 2026-08-04** over the 3056-XML corpus (788 content-unique), walking
> `endpoint/profile/tuning-vlldp` and resolving `ch_00`/`ch_01` — or
> `gain_l`/`gain_r` on simplified files — through `resolve_xml_value`; a plain
> grep misses the `preset=` indirection. **Only the `tuning-vlldp` gate counts**:
> `tuning-cp` carries an `audio-optimizer-enable` of its own (46364 zero-valued
> elements against vlldp's 4702), and the converter never reads it.

| population (content-unique) | gate off | …with a non-zero curve |
|---|---|---|
| all endpoints and profiles | 1283 rows | 22 rows |
| `internal_speaker`/`normal` | 773 rows | 18 rows, 17 XMLs / 17 subsystem ids |

So the gate is common and almost always redundant — the curve it disables is
already all-zero. Where it is *not* redundant, the depth no longer applied is
**13.7 dB** at the deepest affected band on `off` (median 12.0) and **7.0 dB**
on `music`; those three figures reproduce identically on the raw corpus, which
counts the same tunings 108 times over across driver packages (31 subsystem
ids, 30 of the rows `music`). Almost every affected row is the `off` profile,
but `music` is one users select deliberately.

Regenerating one affected device moved only its three Music presets — up to
+6.4 dB in band, 3.7 dB RMS — leaving every other profile byte-identical; the
dev device, which never declares the field, regenerates byte-identical
throughout. Not heard on affected hardware: none of the devices is in reach.
**No committed query yet** — same caveat as the block above.

---

## 9. PEQ filters — mostly simple, occasionally complex

Across the 2795-XML cohort (raw filter counts, all speakers, all profiles):

| Type | Description                          | Count (2795 cohort) | Script support          |
|------|--------------------------------------|---------------------|-------------------------|
| 1    | Bell/peaking EQ                      | 39973               | ✅ Yes                  |
| 9    | High-pass (with order)               |  7080               | ✅ Yes                  |
| 3    | **High-shelf** (with S parameter)    |  3748               | 🧪 Experimental         |
| 7    | High-pass variant (with order)       |  1763               | ✅ Yes                  |
| 4    | Low-shelf (with S parameter)         |   384               | ✅ Yes                  |
| 8    | Low-pass variant (with order)        |   350               | 🧪 Experimental         |
| 6    | Low-pass (with order)                |    20               | 🧪 Experimental         |

In the original 196-XML audit only types 1/4/7/9 were observed. The expanded cohort
surfaces three previously-unseen types, all now emitted via experimental paths.

### Type 3 — high-shelf filter (experimental)

```xml
<filter speaker="0" enabled="1" type="3" f0="2700" gain="2.000000" s="1.000000"/>
```

Same parameter shape as type 4 (`f0`/`gain`/`s`) but mirrored — gains are strictly
non-negative (range 0 to +15 dB across the corpus, no cut variants seen), and the
inflection is above `f0` rather than below. Present in **87 distinct XMLs** (3730
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
type 6 appears at 8–10 kHz with order 4 (tweeter-guard rolloff, mostly ALC274 with
a few ALC287), type 8 at 8/19.5 kHz with order 4–8 (mostly ALC235/ALC256). Rare by
XML: only 9 XMLs carry type 6 (18 filters) and 23 carry type 8 (350 filters). Emitted via `make_lp_band`
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
bells). The complexity ceiling in the cohort is ~7–8 filters per speaker on a few
Lenovo AIO-RTK tunings — still comfortably below the LSP PEQ 32-band ceiling.

---

## 10. IEQ amount — nearly always maxed

Current-cohort IEQ=10 share (when `ieq_enable=1`):

| Profile     | IEQ=10 | Other     |
|-------------|--------|-----------|
| dynamic     | 97%    | 4/6 (3%)  |
| movie       | 100%   | —         |
| music       | 84%    | 3–8 (16%) |
| game        | 100%   | —         |
| voice       | 100%   | —         |

No device selects any preset other than `ieq_balanced` — but "all devices use it"
overstates the data: of the 4131 IEQ-enabled rows, **3817 name `ieq_balanced` and
314 declare no `ieq-bands-set` at all**, falling back to the same curve by default
rather than choosing it. The IEQ amount scales the intelligent EQ curve (room
correction); music profiles occasionally reduce it. The near-universal IEQ=10
means the full curve should be applied in most cases.

---

## 11. MI steering — dynamic profile only

The `mi-dv-leveler-steering-enable=1` parameter appears **almost exclusively on the
`dynamic` profile** (4205 of 4225 dynamic rows; effectively none elsewhere) across
all devices that have it. This confirms it's a deliberate choice to add
Media-Intelligence-driven gain hold only for the "adaptive" profile.

This is the key feature that the EasyEffects pipeline cannot replicate: without
content analysis the autogain has no way to know when silence is "real" silence vs a
quiet passage that will resume loud. This is the root reason the script bypasses
autogain by default — see `docs/design-notes.md` for the full rationale.

---

## 12. Defensive paths verified inert against the corpus

Dolby's XML schema permits more variation than most shipping devices exhibit.
The parser includes defensive handling for several of these cases. Re-checked
against the full 2795-XML cohort (re-checked 2026-08-03):

| Code path                                 | Defensive behaviour                                                     | Corpus check (2795-XML cohort)                                                     | Trigger condition                                              |
|-------------------------------------------|-------------------------------------------------------------------------|------------------------------------------------------------------------------------|----------------------------------------------------------------|
| Default profile (no `--profile` flag)     | `parse_xml` picks `endpoint.find("profile")` (first child)              | 2677/2677 internal_speaker/normal endpoints have `dynamic` first                   | XML where `off` or another no-op profile precedes `dynamic`    |
| Asymmetric L/R PEQ filter counts          | Missing-channel HP slot fills with 100 Hz/24 dB-oct HP, bell slot with flat 1 kHz bell | 12204 PEQ profiles → 38 with an L/R filter-count diff (13 differ in HP count)     | Per-driver tuning where one channel has filters the other lacks |
| Empty `regulator-tuning/threshold_high`   | Falls back to `[0.0]*20` (no limiting), now with a warning; volmax still routes via regulator | **Fixed (2026-06-17).** `threshold_schema` (corpus_audit) confirms exactly **9 profiles on one newer SoundWire device (`SUBSYS_37A317AA`)** stored `threshold_high`/`threshold_low` in a per-channel `<ch_00>…<ch_07>` sub-schema (real non-zero values on ch_00) that the flat `resolve_xml_value` didn't read — so that device got **no regulator limiting**. The other 36,620 reg-enabled internal_speaker profiles use the direct `value=`/`preset=` form (incl. siblings like `384B17AA` whose `threshold_high preset="array_20_zero"` is an intentional zero). `resolve_channel_or_direct` now reads `ch_00`; the `[0.0]*20` fallback only fires on a genuinely empty tuning, and warns when it does. (The doc previously mis-described this as an `isolated_band` sub-schema — `isolated_band` is an unrelated *sibling* element in the older flat schema.) | Genuinely empty / hand-edited / broken regulator tuning |
| Shelf filter with explicit `q` attribute  | Output-gain compensation now uses full shelf gain (commit `c505864`)    | 384 type-4 shelf filters → 0 with explicit `q`                                     | Driver release that adds `q` to a shelf — previously silently under-compensated |
| `is_soundwire` filename detection         | Falls back to HDA mode (no bass enhancer, no convolver headroom restore) | All matched XMLs in the corpus have `SOUNDWIRE_…` or `SDW_…` filenames intact      | User manually renames a SoundWire XML before passing it in     |
| `make_multiband_compressor` 5+ band cap   | `min(group_count, 8)` enforced                                          | Max observed `group_count` = 4 (Dolby schema only allocates `band_group_0..3`)     | Dolby schema extension                                         |

**Now reachable on the current cohort** (formerly inert — these are no longer
defensive-only paths and should be treated as implementation gaps):

| Code path                                 | Current behaviour                                                        | Current-cohort check                                                              | Status                                                          |
|-------------------------------------------|--------------------------------------------------------------------------|------------------------------------------------------------------------------------|-----------------------------------------------------------------|
| 1-band MBC (`group_count=1`)              | Emits LSP `multiband_compressor` with band 0 active (no split frequency) and bands 1-7 disabled; `mbc-1band` experimental marker added to the end-of-run callout | 633 profiles enable MBC with `group_count=1` (§2), dominated by the `music` profile using 1-2:1 ratio with fast attack/release as a loudness maximiser | Experimental — reproduced from the Dolby tuning but not yet audibly validated. `--disable mbc` turns it off. |
| Asymmetric L/R PEQ peak gain              | Output-gain compensation uses global `max(L,R)` peak                    | `corpus_audit` L/R peak-asymmetry tally: **131 rows / 10 devices** (only ALC257/287, only Lenovo convertible/AIO packages) differ; 119 are ~1 dB matched-filter gain trims (median 1.0 dB), 12 are structural 7 dB cases in convertible `stand` pose | **Resolved (keep global-max).** EE's equalizer has per-channel `left`/`right` bands but a *single* `output-gain`; applying `max(L,R)` equally to both channels preserves the Dolby-tuned L/R relationship at every frequency (incl. the 7 dB worst case). A per-channel trim would impose a broadband L-vs-R tilt and isn't representable as one `output-gain`. The only cost is extra headroom on the quieter channel, which the downstream leveler restores. |
| Non-zero `dialog-enhancer-ducking`        | Not currently read by the script (irrelevant on present pipeline)        | 616/40732 rows have ducking=6 or 8 (§1)                                            | Informational — no downstream consumer, but the "always 0" invariant claim was too strong |
| Unknown PEQ filter type                   | Warns "unknown PEQ filter type N, skipping" and drops the filter         | No observed filter outside `(1,3,4,6,7,8,9)` on the cohort                          | Inert — types 3/6/8 are now emitted (see §9); the warning remains a guard against future driver releases adding new types |

If a future driver release breaks any of the truly-inert assumptions, the script will
silently produce a degraded preset rather than crash. The corpus audit is
reproducible with [`tools/corpus_audit.py`](../tools/corpus_audit.py) for the general
distributions; the L/R-asymmetry and §14 presence checks were run as ad-hoc queries
over the same corpus.

Two "by-design" behaviours that look like bugs but aren't:

- The SoundWire convolver applies `peak_db * 0.5` as `output-gain`, intentionally
  letting peak frequencies exceed 0 dBFS so the brick-wall limiter shapes them
  back. Restores half of the headroom that pure peak-normalisation would lose
  for the IEQ-only (no-AO) curve.
- The PEQ output-gain compensation deliberately ignores high-pass and negative-gain
  filters: HP slots reduce headroom requirements (cuts only), and shelves/bells
  with negative gain don't add headroom pressure.

---

## 13. Endpoint operating modes and profile variants

The original 196-XML cohort only exposed `operating_mode="normal"` endpoints with
the six canonical profile types (`dynamic`/`movie`/`music`/`game`/`voice`/`off`).
The Lenovo-AIO-RTK and ThinkPad-AIO-RTK packages exercise both axes much further.

### Operating modes

Current-cohort row counts (top modes; ~20 rarer convertible/desktop poses make up the
tail):

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

The script only ever reads `operating_mode="normal"` (the `--mode` default). On
convertibles, Dolby ships distinct tunings per hinge pose — the "normal" fallback
is fine for the clamshell case, but users of Yoga-class devices would need
`--mode tablet|stand|tent` to pick up the pose-specific tuning. The CLI already
exposes `--mode`; the README documents it.

### Profile types

The canonical Dolby profile vocabulary expands beyond the six listed in the
original cohort (current-cohort row counts):

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

The `personalize_user{1,2,3}` slots are Dolby-provided starting tunings meant to be
reshaped via the Dolby Access Windows app. In the shipped XML they carry real
Dolby tunings, not empty slots — `--profile personalize_user2` is a legitimate
preset source. The `game_{shooter,racing,rpg,rts}` variants appear only on a
small subset of ThinkPad AIO-RTK devices; all share the outer `game` tuning
shape with per-genre tweaks to surround-boost and dialog handling.

`--list` already reports whatever profile names the XML declares, so users pick
these up naturally. `--all-profiles` iterates every one and generates
`Dolby-{ProfileName}-{IEQ-variant}` presets for each.

#### Which profile the device ships on

A few XMLs state it: `<setting><default_profile>`. Over the 791 content-unique XMLs
with an `internal_speaker` endpoint, **28 declare it** (25 `music`, 2 `dynamic`,
1 `movie`) and **26 of those name something other than the profile we build** —
`dynamic` is physically first in the endpoint on all 791, so the script's
"first profile" default silently diverges from Windows on those 26.

The script does not act on the declaration; it reports the mismatch and suggests
`--profile <name>` (issue #46). Adopting it as the selection default is gated on
hearing the difference on a device — issue #29's reporter independently preferred
`music` on a Zenbook S14, which would be the second data point.

---

## 14. Newer-pipeline DSP blocks not modeled by the script

The newer Lenovo IdeaPad / ThinkPad-X13s SoundWire packages introduced several DSP
blocks that don't appear in the original Realtek/Intel cohort. The script does not
implement any of them. Some are flagged at end of run via
`collect_unmodeled_features` in `lib/dax/parse.py`; the rest are silently
dropped.

The three bands below are the useful distinction. A stage with **parameters** in
the XML is a candidate for implementation; a stage with only an **on/off bit**
can never be derived, however common it is; and a stage that is **inert
everywhere** costs nothing to skip.

> **Re-derived 2026-08-03** against the current **2795-XML / 40732-row** corpus
> (`tools/corpus_audit.py`, "Present-but-not-modelled stages"), which reports
> files, rows and devices separately — the table below counts XMLs, and one XML
> contributes many endpoint × profile rows. The presence counts grew with the
> corpus (1234 → 1345 XMLs); every "enabled in 0" claim still holds. The one
> substantive change is the volume-leveler compressor, which is not merely
> present but **enabled almost everywhere it appears**.

### Band A — has parameters, so implementable

| Block | Element(s) | Active in corpus | Status |
|---|---|---|---|
| Sliding bass | `sliding-bass-enable`, `-xo-frequency`, `-max-gain`, `-attack-time`, `-release-time`, `-gain-curve`, `-band-boundary`, `-min-level`/`-max-level` | Enabled and non-inert on **832 rows / 156 XMLs / 63 devices** (only 13 enabled rows are fully inert). Peak boost **3.0–18.6 dB, median 12.0**. Mostly `music` (374 rows) but also 73 rows each on `dynamic`/`movie`/`game`/`personalize_*`, so it reaches the default build. Crossover 180–300 Hz; `band-boundary` always 6 and the curve always 5 points | **Parameters are all there; the semantics are not.** See "What blocks sliding bass" below — the 5-point `gain-curve` has two incompatible readings, and they imply different stages. Not implemented, and not guessable without a capture |

#### What blocks sliding bass

The fields are all present and internally consistent — `band-boundary` is 6 and
`gain-curve` has 5 points on every one of the 274 enabled profiles examined.
What is missing is what the five points are *indexed by*, and the two readings
imply different stages:

- **Level-indexed** — gain slides with input level across the
  `min-level`/`max-level` window, i.e. a dynamic EQ or upward compressor on a
  low band, which an LSP `multiband_compressor` could approximate. Against it:
  a typical curve is `0, 192, 26, 0, 0` (1/16 dB → 0, 12.0, 1.6, 0, 0), which
  rises then falls. A level→gain curve for a bass boost would normally decrease
  monotonically as level rises.
- **Band-indexed** — a per-band gain shape over the bands below
  `band-boundary`, i.e. "sliding" bass energy *up in frequency* out of the range
  the speaker cannot reproduce. The same curve reads naturally this way: nothing
  in the sub band, a large boost one band up, a taper above. If that shifting is
  genuine harmonic synthesis rather than EQ, it hits the same wall as Virtual
  Bass Enhancement — EasyEffects cannot reproduce it (§14 above, issue
  [#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14)).

`gain-curve[1]/16` equals `max-gain` exactly on the 300 Hz family (192→12.000000,
288→18.0, 297→18.5625), which fixes the 1/16-dB scale — but the identity breaks
elsewhere (`0,43,6,0,0` appears with `max-gain` 6.0 *and* 4.0 on different
crossovers), so the two fields are independent and the relationship is not a
decode. `min-level` is 0 everywhere and `max-level` is 52/80/90/98/100 in
unknown units; `attack-time` 706–712 and `release-time` 500 likewise.

So an implementation today would be choosing one of three mechanisms — dynamic
EQ, static per-band shape, or bass synthesis — on a parameter worth up to 18 dB.
That is a guess, not a mapping, which is why no `--enable` flag ships for it.

**What would settle it:** a DAX capture. All 64 devices carrying sliding bass
have an in-device A/B — it is on for `music` and off for every other profile
(e.g. `17AA3DD8`) — so capturing stepped bass tones under Windows on `music` vs
`dynamic` separates the readings directly: level-indexed changes with stimulus
level, a static band shape does not, and synthesis shows new harmonics (the
Δ3 signature the issue-#14 harness already measures).

**Not surfaced to users either, deliberately (2026-08-04).** Sliding bass is
absent from `_UNMODELED_FEATURES`, so no run mentions it — including on the
63 devices that carry it. Adding the row was considered while triaging issue
[#50](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/50)
and declined until a capture exists: the finding would name a gap with no
resolution to offer, and its `ask` would put a capture request in front of
every reader rather than the few who can run one. Revisit once the semantics
are settled.

That issue is the best capture candidate so far: a dual-booting reporter, on
the 300 Hz / `max-gain=18.0` variant above, whose complaint is the symptom the
stage would explain ("the generated preset lacks low end/bass that Windows
has").

#### `threshold_high = −960` looks like a rail used as a non-value

Swept 2026-08-05 over 3056 XMLs → 41,667 regulator-enabled profiles → 833,340
band samples. `threshold_high` spans exactly [−60, 0] dB; both ends are rails
(`0` on 66.03% of samples, no positive value ever). The **−60.00 dB** end
(raw −960, 4852 samples, 433 XML paths, 117 filenames, ~10% of profiles) does
not behave like a threshold:

- **14.06 dB gap** to the next-lowest observed value (−735 raw), against
  ~1-raw-unit granularity above it (161 distinct values in [−960, −300]);
  4852 occurrences at −960 versus 10 / 28 / 10 at its neighbours.
- Appears **only on bands 0–2** (4150 / 678 / 24), never above 234 Hz, and the
  run **always starts at band 0** (4150 of 4150 profiles).
- Step to the neighbouring band averages **−40.2 dB**, where a normal
  band0−band1 step averages −1.1 dB.
- Carriers are the tunings with the **largest AO bass boost** (median +7.0 /
  +10.0 dB at bands 1/2, against +1.1 / +3.5 for non-carriers) — a −60 dBFS
  band ceiling would fight the tuning's own intent.
- Sibling XMLs sharing a PCI_SUBSYS swap it for the *opposite* rail (`0`), and
  within one file `17AA5080` uses all-zero on 7 profiles and −960 on `music`
  alone, while `17AA3832`/`17AA3851` use it on `voice` only — uncorrelated with
  any plausible tuning intent.

`threshold_low` gives no independent signal: it is `threshold_high − 192`
(−12.00 dB) on **99.712%** of all band samples, including inactive ones, so
that delta accompanies any value and cannot argue for or against a sentinel
reading. `isolated_band` does not gate it either (99.5% of ≤−40 dB bands are
`iso=1`, but so are 84.9% of ordinary negative bands).

Our `make_regulator` renders it literally, as a ~56:1 clamp. **No DAX capture
of any carrier device exists**, so there is no measured evidence either way —
this is a structural argument only. What would settle it: an `off`/`dynamic`
pink pair at two levels on any ≥2-band carrier (24 filenames), where the
47/141 Hz `dynamic − off` figure answers it directly. Not raised with issue
#44 — that device (`17AA380D`) carries no rail at all.

### Band B — an on/off bit and nothing else, so *not* derivable

These are enabled on real devices, and no amount of corpus evidence will make
them implementable: the schema carries no threshold, ratio, attack, release or
depth for any of them — only the enable flag. Verified by diffing the full tag
set of a device that has them against one that does not. Emitting a stage anyway
would mean inventing every parameter, which is the per-device hand-tuning the
XML-only rule exists to prevent (see CLAUDE.md "Core invariants").

| Block | Element(s) | Active in corpus | Status |
|---|---|---|---|
| Volume-leveler DRC sub-component | `volume-leveler-drc-enable` | **618 XMLs, enabled on 9979 of 11150 rows** | Reported at end of run. Moot by default (the leveler is bypassed); only reachable under `--enable autogain` |
| Volume-leveler compressor sub-component | `volume-leveler-compressor-enable` | **137 XMLs, 77 devices, enabled on 2408 of 2410 rows** | Same. **Does not explain the issue [#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25) autogain overshoot** — neither that device (`17AA507F`) nor the dev device (`17AA22E6`) carries the element, so Dolby's leveler runs uncompressed there too. Two devices (`37A317AA`, `C1DC144D`) switch it off on `music` and on elsewhere, the closest thing to an in-device A/B |
| Media-Intelligence steering | `mi-virt-steering-enable`, `mi-dialog-enhancer-steering-enable` (4245 rows), `mi-surround-compressor-steering-enable` (4139 rows) | Present on all 2681 XMLs | Content-adaptive steering of stages we do model. Not warned — it is on the `dynamic` profile of essentially every device, so a note would fire on every run |
| MBC channel deviation | `mb-compressor-channel-deviation` | 1589 XMLs, non-zero on 64 rows | Not warned; near-universally zero |

### Band C — present but inert, or engine plumbing

Named once so a future schema sweep doesn't re-discover them as findings:

| Group | Element(s) | Active in corpus |
|---|---|---|
| Inert feature blocks | `bass-extraction-enable`, `bass-enhancer-boost`, `virtual-bass-slope-gain`, `virtual-bass-overall-gain`, `volume-modeler-calibration`, `noise-gate-enable`, `process-optimizer-enable`, `virtualizer-start-band` | Zero on every row where present |
| Fixed feature constants | `bass-enhancer-width`/`-cutoff-frequency`, `bass-extraction-cutoff-frequency`, `virtual-bass-src-freqs`/`-mix-freqs`/`-subgains`, `virtualizer-{front,surround,height}-speaker-angle` | Identical on all 40732 rows; nothing device-specific to carry |
| DSP-engine descriptors | `max_num_*`, `output_ports`, `nb_output_channels`, `low_latency_enable`, `processing_mode`, `mix_matrix`, `mi_process_disable` | Dolby runtime plumbing, not audio parameters |

### Previously catalogued blocks

| Block                                              | Element(s)                                                                   | Active in corpus                          | Status                                                                                                  |
|----------------------------------------------------|------------------------------------------------------------------------------|-------------------------------------------|---------------------------------------------------------------------------------------------------------|
| Dynamic Speaker Optimization (DSO)                 | `init-info/dynamic_speaker_optimization_enable`, `dynamic-speaker-optimization-amount`, `dynamic-speaker-optimization-speaker-interval` | 1 XML, 1 device (`SUBSYS_37A317AA`, IdeaPad-5x-2-in-1 SoundWire SPK1) — **enabled** on all 10 of its rows | **Warned at parse time.** Excursion-aware bass limiting tied to driver size; needs Dolby DSP data we don't have. |
| Advanced speaker virtualizer                       | `advanced-speaker-virtualizer-rendering-config`, `advanced-speaker-virtualizer-start-bin`, `speaker_virtualizer_mode` | Same 1 XML / device                       | **Warned at parse time.** Newer FFT-domain replacement for `output-mode-partial-{surround,height}-virtualizer-enable`; also unmodeled. |
| Volume-leveler compressor sub-component            | `volume-leveler-compressor-enable`                                           | 137 XMLs, 77 devices (newer Lenovo AIO / ThinkPad packages) — and **enabled on 2408 of 2410 rows**, not merely present | Not warned. Harmless by default, since the volume leveler is bypassed entirely (autogain trap, see design-notes.md) — dropping a sub-block of a stage we don't run costs nothing. It could matter under `--enable autogain` on one of these 77 devices, where our leveler would run without the compressor Dolby pairs with it. **This does not explain the issue [#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25) autogain overshoot**: neither that device (`17AA507F`) nor the dev device (`17AA22E6`) carries the element at all, so Dolby's leveler runs uncompressed there too. Unmeasured on the 77. |
| Rear / rear-height virtualizer angles              | `virtualizer-rear-speaker-angle`, `virtualizer-rear-height-speaker-angle`, `rear-height-filter-mode` | Common on 4+ speaker laptops              | Not modeled. The legacy `output-mode-partial-{surround,height}-virtualizer-enable` already isn't modeled either; see CLAUDE.md and design-notes.md. |
| Surround-decoder centre spreading                  | `surround-decoder-center-spreading-enable`                                   | Present in 1345 XMLs, **enabled in 0**    | Defensive — would silently drop if a future driver enables it.                                          |
| Woofer-only regulator                              | `woofer-regulator-enable`, `woofer-regulator-tuning`                         | Present in 1345, **enabled in 0**         | Defensive — would silently drop if a future driver enables it.                                          |
| Independent regulator mode                         | `regulator-independent-enable`                                               | 1 XML, never enabled                      | Defensive.                                                                                              |
| Bass-extraction LFE gain                           | `bass-extraction-lfe-gain`                                                   | Present in 1345, **enabled in 0**         | Defensive — bass-extraction itself is universally off.                                                  |
| Channel-gain matrix attributes                     | `gain_c`, `gain_l`, `gain_r`, `gain_ls`, `gain_rs`, `gain_lfe`, `gain_lrs`, `gain_rrs`, `gain_ltm`, `gain_rtm` | Companion to virtualizer downmix          | Tied to the unmodeled virtualizer; would only matter once advanced-virt is implemented. **NB:** inside `<audio-optimizer-bands>`, simplified-schema XMLs reuse `gain_l`/`gain_r` as the L/R speaker-correction arrays — *those* are modeled (mapped to the `ch_00`/`ch_01` slots, issue [#22](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/22)), unrelated to the downmix matrix here. |

The `_UNMODELED_FEATURES` table in `lib/dax/parse.py` carries the two
rare-but-real cases in the previous table (DSO, advanced virtualizer) plus four watch-only
fields (`peak-level`, `ieq-bands-set`, `regulator-overdrive`,
`regulator-relaxation-amount`) that warn only when an XML deviates from the
corpus constants — silent on every shipped tuning today. The
universally-present-but-never-enabled defensive elements (band C, and the MI
steering row in band B) are deliberately *not* listed — they'd fire on every run
for no gain. If a future driver release flips one of them on, the corpus sweep
will catch it before the warning needs to. Everything that *is* listed now
prints once at the end of the run rather than mid-parse, where it was buried
under the per-band tables.

### Why these aren't implemented

Band B is answered above: there is nothing to derive. Band A's sliding bass is
derivable and simply not done yet. For the two warned features, implementation
would require either real device measurements or undocumented Dolby DSP
internals:

- **DSO** maps a target excursion (driver-specific) and a per-band power
  envelope to a real-time gain. The `amount` (1–10) and `speaker-interval`
  (`720,300` mm × 100?) attributes name the dial but not the algorithm. A
  rough LSP `multiband_compressor` band-0 limiter could approximate the
  bass-protection role, but at the cost of pumping artifacts that DSO
  specifically avoids — net negative versus dropping it.
- **Advanced speaker virtualizer** is an FFT-domain HRTF-style spatializer.
  The 8-int rendering config (`103,32568,6698,5090,1,1,1,1`) is opaque, and
  no LSP/EE plugin reproduces this kind of processing. The legacy
  `output-mode-partial-*-virtualizer-enable` blocks were once approximated
  as a stereo widener via `surround-boost → stereo_tools`, but a 2026-06 DAX
  capture showed Dolby applies no stereo widening on 2-channel content, so
  that mapping was removed (design-notes entry 2); the advanced variant is the
  same unreproducible spatializer one generation later.

The warning is the honest outcome: the user knows what's being dropped, and a
future device-level investigation can wire in something better.

---

## 15. SoundWire tuning-filename matching — `FUNC` is not the Linux part id

Auto-detection matches a SoundWire tuning by parsing its filename
(`SOUNDWIRE_[SDCAFUNCTION_NN_]MAN_<man>_FUNC_<func>_SUBSYS_<device><vendor>.xml`)
against `/sys/bus/soundwire/devices` IDs and the HD-Audio controller's PCI
subsystem. Across the corpus's 29 Qualcomm Aqstic (`MAN_025D`) tunings, `FUNC`
equals the Linux SoundWire **part id** (e.g. `FUNC_1318` ↔ part `1318`), so the
original match keyed on `(manufacturer, part) + PCI subsystem`.

A Samsung Galaxy Book6 driver package (Cirrus Logic `MAN_01FA`, cs35l56
amplifiers; Panther Lake, `xml_version 3.8.0`) falsifies that as a *general*
rule. Its tuning is `SOUNDWIRE_SDCAFUNCTION_10_MAN_01FA_FUNC_3556_SUBSYS_CA0A144D.xml`,
yet sysfs reports SoundWire parts `3557` (the six cs35l56 amps) and `4245` (the
SDCA codec) — **neither equals `FUNC_3556`**. The XML's own `security-key`
(`SOUNDWIRE\SDCA_FUNCTION_10&MAN_01FA&FUNC_3556&…&SUBSYS_CA0A144D`) confirms
`FUNC` is a Dolby/Cirrus device id and `SUBSYS_<pci>` is the per-device key: the
package ships five tunings identical but for `SUBSYS` (`F020144D`, `C1DC144D`,
`C1DE144D`, `C910144D`, `CA0A144D`) — one per SKU — all `MAN_01FA_FUNC_3556`.

So `FUNC` is now treated as **preferred, not required**: match `(man, part)`
exactly first, and only when nothing matches that way fall back to PCI
subsystem + manufacturer (issue [#26](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/26)). The exact tier still matters because some
Lenovo SKUs ship *two* tunings sharing `MAN`+`SUBSYS` but differing in `FUNC`
(e.g. `SUBSYS_383917AA`: `FUNC_0721` vs `FUNC_1320`); the detected part still
disambiguates those. (Galaxy Book6 is XML-derived only — generates cleanly but
unvalidated by ear, since the maintainer has no access to the hardware.)

Two diagnosis follow-ups from the first field report (issue [#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27)). (1)
`--speaker-info` was double-counting these amps: each cs35l56 is a **mono**
amplifier (SoundWire enumerates one slave per amp chip), so six were reported as
"12 speakers". The layout estimate now counts each enumerated amp once and
probes its channel count from the sink data-port DisCo props
(`dpN_sink/max_ch`, kernel ABI `sysfs-bus-soundwire-slave`) rather than assuming
stereo. (2) The report's "quiet, smartphone-like" sound is a driver/firmware-layer
issue, not the preset — **now confirmed on-device.** Per the cs35l56 kernel
driver doc a missing per-amp `.wmfw`/`.bin` (under `/lib/firmware/cirrus/`) leaves
the amp playing a **mono mix with no voicing/protection**, and the only
authoritative signal is the kernel log — no sysfs/debugfs exposes amp
audio-state. The #27 capture bears this out: all six amps load the base DSP ROM,
then the kernel logs `FIRMWARE_MISSING` / `Calibration disabled due to missing
firmware controls` / `Can't read tuning IDs` (CS35L57 parts; the machine-specific
Cirrus tuning is absent on this Fedora 44 build), so the amps run without voicing
or protection. Root cause is the distro `linux-firmware` gap for this SKU, not
the converter. Two diagnostic lessons fed back into `--speaker-info`: (a) generic
`cirrus/cs35l*` blobs were *present* (≈1785 files) while the machine blob was
missing, so file-presence can't certify — the log is authoritative; and (b) the
first kernel-log marker set caught only boot/init timeouts and reported "no
errors" on this exact failure, so the scan now flags the firmware-missing
signature too (verified verbatim against `cs35l56-shared.c` / `cs-amp-lib.c`,
with the equivalent TI `tas2781-*` and Realtek `rt1320-sdw.c` firmware-load
failures), and the clean-log line now tells the reader to eyeball the log rather
than trust the scan.

**Addendum (2026-07-21, issue [#27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27) follow-up) — first amp-DSP-voiced device.**
The reporter closed the firmware gap himself by extracting the CS35L57 tuning
from Samsung's Windows driver
([write-up](https://github.com/JeanLuX/notebook/blob/main/samsung-galaxy-book6-ultra/AUDIO-CIRRUS-FIRMWARE-EXTRACTION.md));
result "night and day", near-Windows quality with **no** DAX-derived processing
at all. Package-layout intel from that write-up: one shared `.wmfw` under `fw/`,
per-SSID tuning dirs under `tn/` (`CA0A` for his SKU — mirroring the five
per-SKU `SUBSYS_*` XMLs above), renamed to the linux-firmware convention
`cs35l57-b2-dsp1-misc-144dca0a-lXuY.{wmfw,bin}`; calibration comes separately
from EFI variables ("Calibration applied" in the kernel log is the success
marker).

With the amps properly voiced, the converter's output was reported as a net
negative: the default preset "degrades the sound dramatically", and with
`--disable bass-enhancer --disable volmax --disable regulator` it was at best
neutral (all three were disabled together, so no single-filter attribution —
a confound to remember before blaming any one default). The likely mechanics,
all visible in the reporter's pasted stdout: this XML's host-side tuning is
near-flat (audio-optimizer all-zero on both channels, IEQ ≤ ±1.5 dB, no PEQ
filters, regulator `threshold_high` flat at 0 dB, stress all-zero) — the
voicing lives in the Cirrus amp-DSP tuning, not in DAX host processing. A flat
0 dB `threshold_high` means `make_regulator` disables every band (a threshold
≥ 0 dBFS never triggers), so the regulator emits but limits nothing and the
volmax +6 dB riding its input-gain hits the brickwall limiter untamed — the
issue-#23 "per-band compression tames the boost" rationale silently doesn't
apply (the generator now warns when this shape occurs). The shape is common,
not a Samsung quirk — ad-hoc sweep (2026-07-21, the converter's `parse_xml`
over the `tests/corpus` discovery walk; 46,336 `internal_speaker` profile rows
across 3,055 reachable files): the warning condition (regulator emitted,
volmax > 0, every `threshold_high` ≥ 0 dB) holds on **7.3% of profile rows**,
**338/979 distinct tunings** on at least one profile, and **142/978 (14.5%)
on a plain default run** (normal mode, first profile). 201 of the 338 fire
only on voice-family profiles — consistent with the volmax-in-`voice`
concentration in the design-notes #23 corpus analysis ("input-gain only does
anything when the regulator is active"); the default-run cases are
`dynamic`/`movie` at +5…+9 dB. These are corpus counts, not audibility
claims: firing means the taming rationale doesn't apply, not proven squash.
And the default-on
SoundWire `bass_enhancer` adds harmonics on top of an amp that now does real
bass management — the second field report against that default (after issue
#29; design-notes unvalidated-scaling entry 9).

Takeaway: on devices whose DAX host tuning is near-flat because the voicing
ships in amp firmware, the converter has little to offer once the firmware is
installed — the honest outcome is "install the firmware, skip the preset".

---

## 16. HDA tuning-filename matching — the codec subsystem id is not unique

HDA-style filenames (`DEV_<codec-device>_SUBSYS_<codec-subsystem>_PCI_SUBSYS_…`)
were originally matched on the codec subsystem alone, with `tuning_version` as
the only tiebreak. Issue [#33](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/33) (IdeaPad Pro 5 14APH8, ALC287, subsystem
`17AA38C5`) falsified the uniqueness assumption: its driver store ships both
`DEV_0287_SUBSYS_17AA38C5` (tuning_version 8) and `DEV_0257_SUBSYS_17AA38C5`
(tuning_version 11) — Lenovo reuses the subsystem id across an ALC287 and an
ALC257 SKU (a Yoga Slim 7 ProX driver package independently pairs `17AA38C5`
with an ALC257 in its Fortemedia/SAM `.dat` names). The version tiebreak
therefore selected the other codec's tuning, which the reporter heard as
clearly worse.

This is systematic, not a one-off: across the reachable corpus (2836 files,
2026-07 count via the `tests/corpus` discovery walk), 83 of 766 distinct
HDA-style subsystems (11%) appear with more than one `DEV` token — all Lenovo
(`17AA…`), dominated by `0257`/`0287` pairs, with a few three-way splits
(e.g. `17AA3852`: `0230`/`0257`/`0287`).

The `DEV` token is the codec device id (the low 16 bits of the HDA vendor id,
`0x10EC0287` → `0287`), so matching now keys on the `(DEV, SUBSYS)` pair, with
subsystem-only kept as a warned fallback tier — the same
preferred-not-required tiering as the SoundWire `FUNC` rule (§15).
`tuning_version` still tiebreaks within a tier (duplicate driver-store copies
are common).

---

## Interesting observations

1. **No Intel Fusion devices found** — the `fusion_ext_intel` driver package shares
   the same XML files as `dax3_ext_rtk`, suggesting Intel SST-based Dolby uses
   identical tuning to Realtek-based Dolby.

2. **Music profiles are the MBC outlier** — 38% enable MBC on music vs 4% on dynamic,
   confirming MBC is primarily a loudness tool, not a protection feature.

3. **voice_onlinecourse is the safest profile** — ~0% MBC, 0% VL amount, 4 dB volmax,
   simplest chain. Dolby's own tuning for speech entirely disables the compressor and
   uses the gentlest volume leveler. A good template for a "no artifacts" preset.

4. **Hard limiting (slope=16) is the overwhelming majority (97%)** — Dolby engineers
   prefer true brickwall limiting on the regulator for most laptop speakers.

5. **Every device has a unique regulator curve** — 408 distinct threshold curves
   (see §7 on why that is not comparable to the older 399), confirming these are
   individually tuned per speaker.

---

## Open follow-ups (from the 2026-06 re-derivation)

Surfaced by the 2483-XML re-derivation; queued, not yet actioned.

1. **Newer-SoundWire regulator gap (code).** ✅ **Resolved (2026-06-17).** The
   real schema was a per-channel `<ch_00>…<ch_07>` layout (not the `isolated_band`
   sub-schema this list first claimed — that's an unrelated older-schema sibling).
   `resolve_channel_or_direct` now reads `ch_00`, recovering real per-band limiting
   on `SUBSYS_37A317AA`'s 9 profiles; the `[0.0]*20` fallback now only fires (and
   warns) on a genuinely empty tuning. Scope re-derived via `corpus_audit`'s
   `threshold_schema` (9 profiles/1 device; 36,620 others untouched). DSO and the
   advanced virtualizer for that device remain unmodeled (§14).
2. **Asymmetric L/R PEQ peak gain (investigate).** ✅ **Resolved (2026-06-17):
   keep global-max.** Re-derived via `corpus_audit`: 131 rows / 10 devices differ
   (median 1 dB, max 7 dB; only Lenovo convertible/AIO). EE's equalizer has only a
   single `output-gain`, and applying `max(L,R)` equally to both channels preserves
   the per-speaker L/R relationship at every frequency — a per-channel trim would
   corrupt it. No code change (§12).
3. **Voice-AO divergence rate (re-derive).** ✅ **Resolved (2026-06-17).**
   Methodology-matched re-derivation (`corpus_audit`, per-endpoint, full-schema,
   internal_speaker/normal): **52% per-endpoint / 63% per-device** diverge — well
   below the original 97%. The newer AIO packages differentiate the voice AO curve
   less often; §8 updated.
4. **1-band MBC audibility (validate).** Music 1-band MBC band-0 ratios reach
   ~6:1 at thresholds to −12 dB (§2) — real compression, not just makeup.
   Audition a high-ratio example; the `mbc-1band` path is still experimental.
5. **`peak-level` disposition (minor).** ✅ **Resolved (2026-06-17): keep
   watch-only.** Re-derived via `corpus_audit` (peak-level section): 244/27508
   nonzero (−13 on 175, then −6/−8/−4/−15/−32/−5/−1), i.e. zero on 99.1% of rows.
   The 1/16-dB→limiter-threshold interpretation is unverified, and mapping it wrong
   would trade away headroom audibly on the <1% that set it — so watch-listing in
   `_UNMODELED_FEATURES` (warn on deviation, don't act) is correct. Lives under
   `tuning-vlldp`; the watch's `.//peak-level` xpath catches it regardless.
6. **Re-run on new driver pulls (process).** ✅ **Done (2026-08-03)** for the
   2795-XML cohort: every aggregate above regenerated with
   [`tools/corpus_audit.py`](../tools/corpus_audit.py). Still a standing process
   for the next pull. Four blocks remain outside the tool and were left as-is
   rather than restated: §8's rail-pinning counts (needs content-hash dedup,
   `geq_maximum_range`, `xml_version` and AO peak-to-peak), §9's per-XML and
   per-speaker filter distributions, §13's `default_profile` paragraph, and the
   `voice_onlinecourse`/`personalize` rows in §3 and §5 (the tool prints only the
   eight most common profiles). Adding those five queries is the next step.
7. **Galaxy Book6 quiet-output cause (device-gated).** ✅ **Resolved (2026-06-30).**
   The reporter's `--speaker-info` + kernel log confirmed the §15 hypothesis:
   the cs35l56/57 amps load the base DSP ROM but log `FIRMWARE_MISSING` /
   `Calibration disabled…` / `Can't read tuning IDs` — the machine-specific Cirrus
   tuning is missing on this Fedora 44 build, so they run without voicing, quiet
   and flat, regardless of the preset. Root cause is the distro `linux-firmware`
   gap for this SKU (route the reporter there); the converter is not implicated.
   `--speaker-info` now flags those markers (see §15). **Closed out (2026-07-21):**
   the reporter self-fixed the gap by extracting the CS35L57 `.wmfw`/`.bin` from
   Samsung's Windows driver ([his write-up](https://github.com/JeanLuX/notebook/blob/main/samsung-galaxy-book6-ultra/AUDIO-CIRRUS-FIRMWARE-EXTRACTION.md)),
   confirming the diagnosis end-to-end. Post-fix preset verdict was *negative* —
   the device is **not** added to the README tested table; see the §15 addendum
   for the outcome and mechanism.
8. **Zenbook S14 UX5406SA partial-success report (issue [#29](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/29), reporter-gated).**
   First cs42l43-codec device (SoundWire part 0x4243 jack codec + 4× cs35l56,
   PCI SSID `1043:1E13`) and — unlike the Galaxy Book6 above — *not* a firmware
   gap: no kernel-log failure markers, and upstream linux-firmware ships this
   SSID's per-amp Cirrus tuning (`cs35l56-b0-dsp1-misc-10431e13-amp1..4.bin`).
   Also the first device where the Dolby `FUNC` token should *equal* the amp
   part id (0x3556) — a clean §15-matching datapoint once the XML filename is
   confirmed. Reporter symptoms ("too bass boosted", chassis resonance, wonky
   dynamics; music profile better than dynamic) drove the 2026-07-03 removal of
   two vestigial SoundWire boosts (design-notes unvalidated-scaling entries
   1/3) and are the pending A/B for the bass-enhancer default (entry 9 / issue
   #14) and a candidate second-device loud capture for entries 6/11. Pending
   from the reporter: generation stdout, the tuning XML, kernel log, A/B
   results.
