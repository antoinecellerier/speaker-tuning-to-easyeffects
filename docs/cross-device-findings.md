# Cross-device DAX3 findings

> Interpretive analysis of the parameter schema used by Dolby DAX3 tuning XML (distributed publicly as part of Windows audio driver packages), for the purpose of Linux interoperability. No verbatim tuning arrays are reproduced.

The original cohort was **196 DAX3 tuning files** spanning **3 Realtek codec variants**
(ALC257, ALC285, ALC287) found in the `dax3_ext_rtk` and `fusion_ext_intel` driver
packages. Successive driver-package pulls (`ext_lenovo_AIO_rtk`, `ext_thinkpad_AIO_rtk`,
`ext_capg_thinkpad`, `ext_amd_thinkpad_AIO`, plus newer IdeaPad/SoundWire
packages) grew it to the current
**2483 tuning XMLs / 36827 profile rows** spanning **13 HD-Audio codec DEV IDs
(mostly Realtek ALC) plus SoundWire/SDW**. [`reference.md`](reference.md) documents how the script maps one
specific device; this doc captures what's universal across the ecosystem and what
varies from device to device, so readers can judge which parts of the pipeline are
portable and which are tuned.

> **All figures below are from the 2483-XML cohort audited 2026-06-16**, except
> where a passage is explicitly marked as the original 196-XML cohort (kept for
> the historical methodology notes). Regenerate any aggregate here with
> [`tools/corpus_audit.py`](../tools/corpus_audit.py) (the one-off §12/§14
> checks were run as ad-hoc queries over the same corpus).

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

Current 2483-XML cohort — XML count per codec (not dynamic-profile count):

| Codec family          | XMLs | Notes                                              |
|-----------------------|------|----------------------------------------------------|
| ALC257 (DEV_0257)     | 1366 | Dominant, mostly Lenovo AIO-RTK packages           |
| ALC287 (DEV_0287)     |  481 | Primary ThinkPad codec; dev-device family          |
| ALC235 (DEV_0235)     |  236 |                                                    |
| ALC285 (DEV_0285)     |   84 | Includes the original cohort                       |
| ALC230 (DEV_0230)     |   81 |                                                    |
| ALC256 (DEV_0256)     |   79 |                                                    |
| ALC274 (DEV_0274)     |   72 | Carries the rare PEQ type-6 low-pass filters       |
| SoundWire (`MAN_025D`)|   29 | Plus one `SDW_` prefix variant                     |
| ALC298/0887/0892/0897 |   32 | Desktop-style AIO codecs                           |
| DEV_1F86 / DEV_1F87   |   22 | Newer codecs absent from earlier cohorts           |

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
| `dialog-enhancer-ducking`          | 0 (mostly)         | 98.3% of rows; 616/36827 are non-zero (8 or 6) — not universal |
| `regulator-overdrive`              | 0                  | Always zero where present          |
| IEQ curve preset                   | `ieq_balanced`     | Only curve used anywhere           |

The script skips bass enhancer, virtual bass, graphic EQ, volume modeler, and
non-zero system/pre/post gains because none of them meaningfully exist in the wild.

---

## 2. Multi-band compressor — the minority feature

Only **77 of 2451 files** (3%) enable the MB compressor on the `dynamic` profile
— equivalently 166 of the 3825 dynamic-profile *rows* (4%), since some files carry
several endpoint modes; both denominators appear in this doc, so both rates are
given here once. This is the most important finding: **MBC is the exception,
not the rule** — except on `music`, where it jumps to 38%.

| Profile              | MBC=1 | MBC=0 |
|----------------------|-------|-------|
| dynamic              | 166   | 3659  |
| game                 | 191   | 3548  |
| movie                | 206   | 3619  |
| music                | 1459  | 2366  |
| voice                | 131   | 3694  |
| voice_onlinecourse   | 18    | 2298  |
| off                  | 1     | 3824  |

Music profiles enable MBC far more often (**38%**), confirming MBC is used for
loudness maximisation on premium speakers, not as a universal safety feature.

### Band-count distribution (MBC-enabled profiles)

Current cohort, by `group_count` (a populated `band_group` count of 1–4):

| `group_count` | Enabled profiles | Disabled (but populated) |
|---------------|------------------|--------------------------|
| 1             | 633              | 19339                    |
| 2             | 1061             |   400                    |
| 3             | 340              |   470                    |
| 4             | 455              |   466                    |

Two noteworthy wrinkles the old 2-band decoder masked:

- **633 profiles enable MBC with `group_count=1`** — single-band, full-spectrum
  dynamics. Almost entirely on the `music` profile (630 of 633), typically used
  as a loudness maximiser: band-0 ratios range from 1:1 (pure makeup) up to ~6:1
  and thresholds from 0 dB to −12 dB, with fast attack/release. Emitted from the
  `mbc-1band` experimental path since the guard was relaxed; LSP MBC accepts a
  single enabled band with no split frequency and bands 1-7 disabled.
- **936 profiles declare 3- or 4-band tunings but gate the compressor off**
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
| **dynamic**           | 5 (48%), 3 (21%), 4 (17%), 2 (5%), 7 (3%), 1 (3%)          |
| **movie**             | 5 (50%), 3 (21%), 4 (16%), 2 (4%), 1 (2%), 7 (2%)          |
| **music**             | 2 (56%), 0 (18%), 3 (15%), 4 (5%), 1 (3%)                  |
| **game**              | 0 (95%), 4 (3%), 2 (1%)                                    |
| **voice**             | 0 (99%), 2 (<1%)                                           |
| **voice_onlinecourse**| 0 (99%), 2 (<1%)                                           |

The development device uses `vl_amount=2`, which is on the **gentler end** for the
dynamic profile. The most common value is **5** (48% of dynamic rows).

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

The script applies `volmax-boost` as `output-gain` on the regulator
(`multiband_compressor#1`), falling back to `limiter#0.input-gain` when the regulator
is absent. Can be disabled with `--disable volmax` if the boost drives the brick-wall
limiter into audible gain reduction on already-loud masters.

---

## 5. Dialog enhancer — profile-dependent behaviour

Current-cohort enable rates:

| Profile               | Enabled | Disabled |
|-----------------------|---------|----------|
| dynamic               | 55%     | 45%      |
| movie                 | 53%     | 47%      |
| music                 | 0%      | 100%     |
| game                  | 3%      | 97%      |
| voice                 | 52%     | 48%      |
| voice_onlinecourse    | 52%     | 48%      |

Dialog enhancer is a **speech enhancement feature**, consistently disabled for music
and game profiles across all devices. (Enable rates on dynamic/movie are lower than
the original cohort's 86% — the newer Lenovo-AIO packages leave it off more often.)

### Dialog enhancer amount

| Profile    | Most common (when enabled) |
|------------|----------------------------|
| dynamic    | 5 (92%)                    |
| movie      | 5 (92%)                    |
| game       | 6 (78%) or 7 (21%)         |
| voice      | 3 (44%) or 8 (36%)         |
| personalize| 10 (100%)                  |

---

## 6. Regulator distortion slope — limiting severity

The `regulator-distortion-slope` (1/16 scale) controls how hard the regulator limits.
Current-cohort distribution:

| Slope        | Effective ratio       | Share          |
|--------------|-----------------------|----------------|
| 4 (0.25)     | 1.3:1 — gentle        | <1%            |
| 6 (0.375)    | 1.6:1                 | <1%            |
| 8 (0.50)     | 2:1 — moderate        | 1%             |
| 11–12        | ~3–4:1 — firm         | <1%            |
| **16 (1.00)**| **∞:1 — hard limiter**| **97%** (22571/23164) |

(The original 196-XML breakdown had slope=16 at 53%; the AIO-RTK packages that
dominate the current cohort use the hard limiter far more.)

The development device uses slope=16 (hard limiter), which is the **most common**
setting. The hard limiter mode means the regulator acts as a brickwall at its threshold.

**Implication for pipeline design:** when slope=16 the regulator is a brickwall
limiter, so for the large majority of devices (~97%) the regulator *is* the brickwall
limiter. The explicit output limiter added to the EasyEffects chain is redundant on
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

There are **399 distinct `threshold_high` patterns** across the 2483-XML cohort —
nearly every speaker tuning has a custom regulator curve. This is the most
device-specific parameter in the entire chain.

---

## 8. Audio optimizer — voice profile uses different curves

**Most devices** use a **different audio-optimizer curve for the `voice` profile**
compared to `dynamic` / `movie` / `music` / `game` (which all share the same curve).

The voice AO curve typically:

- Reduces low-frequency correction (less bass boost)
- Adjusts mid-frequency emphasis for speech clarity
- Shares the same high-frequency rolloff

> The original 196-XML cohort put this at 97% of devices. A naive whole-block
> comparison over the current cohort (voice vs dynamic `audio-optimizer-bands`
> serialisation per file) lands nearer 60%, but that mixes the simplified-schema
> `gain_l`/`gain_r` XMLs (issue #22) and multi-mode files into one bucket, so the
> exact current-cohort rate needs a methodology-matched re-derivation. The
> qualitative claim — voice gets its own AO curve on most devices — holds.

All non-voice profiles (dynamic, movie, music, game, personalize) share identical AO
curves. The script processes each profile independently, so the voice preset
automatically picks up the voice-specific AO curve when generated from a device
that has one.

---

## 9. PEQ filters — mostly simple, occasionally complex

Across the 2483-XML cohort (raw filter counts, all speakers, all profiles):

| Type | Description                          | Count (2483 cohort) | Script support          |
|------|--------------------------------------|---------------------|-------------------------|
| 1    | Bell/peaking EQ                      | 36530               | ✅ Yes                  |
| 9    | High-pass (with order)               |  4784               | ✅ Yes                  |
| 3    | **High-shelf** (with S parameter)    |  3730               | 🧪 Experimental         |
| 7    | High-pass variant (with order)       |  1424               | ✅ Yes                  |
| 8    | Low-pass variant (with order)        |   350               | 🧪 Experimental         |
| 4    | Low-shelf (with S parameter)         |   192               | ✅ Yes                  |
| 6    | Low-pass (with order)                |    18               | 🧪 Experimental         |

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

All devices use `ieq_balanced` as the preset. The IEQ amount scales the intelligent
EQ curve (room correction); music profiles occasionally reduce it. The near-universal
IEQ=10 means the full curve should be applied in most cases.

---

## 11. MI steering — dynamic profile only

The `mi-dv-leveler-steering-enable=1` parameter appears **almost exclusively on the
`dynamic` profile** (3805 of 3825 dynamic rows; effectively none elsewhere) across
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
against the full 2483-XML cohort (2026-06-16):

| Code path                                 | Defensive behaviour                                                     | Corpus check (2483-XML cohort)                                                     | Trigger condition                                              |
|-------------------------------------------|-------------------------------------------------------------------------|------------------------------------------------------------------------------------|----------------------------------------------------------------|
| Default profile (no `--profile` flag)     | `parse_xml` picks `endpoint.find("profile")` (first child)              | 2448/2448 internal_speaker/normal endpoints have `dynamic` first                   | XML where `off` or another no-op profile precedes `dynamic`    |
| Asymmetric L/R PEQ filter counts          | Missing-channel HP slot fills with 100 Hz/24 dB-oct HP, bell slot with flat 1 kHz bell | 10689 PEQ profiles → 33 with an L/R filter-count diff (12 differ in HP count)     | Per-driver tuning where one channel has filters the other lacks |
| Empty `regulator-tuning/threshold_high`   | Falls back to `[0.0]*20` (no limiting), volmax still routes via regulator | **9 profiles, all on one newer SoundWire device (`SUBSYS_37A317AA`)** now hit the fallback (was 0 originally): its `threshold_high` carries the values in an `isolated_band` sub-schema with no `value` attribute, which the parser doesn't read — so that device currently gets **no regulator limiting**. Implementation gap, not just defensive. | Newer SoundWire regulator schema, or hand-edited / broken regulator tuning |
| Shelf filter with explicit `q` attribute  | Output-gain compensation now uses full shelf gain (commit `c505864`)    | 192 type-4 shelf filters → 0 with explicit `q`                                     | Driver release that adds `q` to a shelf — previously silently under-compensated |
| `is_soundwire` filename detection         | Falls back to HDA mode (no bass enhancer, no convolver headroom restore) | All matched XMLs in the corpus have `SOUNDWIRE_…` or `SDW_…` filenames intact      | User manually renames a SoundWire XML before passing it in     |
| `make_multiband_compressor` 5+ band cap   | `min(group_count, 8)` enforced                                          | Max observed `group_count` = 4 (Dolby schema only allocates `band_group_0..3`)     | Dolby schema extension                                         |

**Now reachable on the current cohort** (formerly inert — these are no longer
defensive-only paths and should be treated as implementation gaps):

| Code path                                 | Current behaviour                                                        | Current-cohort check                                                              | Status                                                          |
|-------------------------------------------|--------------------------------------------------------------------------|------------------------------------------------------------------------------------|-----------------------------------------------------------------|
| 1-band MBC (`group_count=1`)              | Emits LSP `multiband_compressor` with band 0 active (no split frequency) and bands 1-7 disabled; `mbc-1band` experimental marker added to the end-of-run callout | 633 profiles enable MBC with `group_count=1` (§2), dominated by the `music` profile using 1-2:1 ratio with fast attack/release as a loudness maximiser | Experimental — reproduced from the Dolby tuning but not yet audibly validated. `--disable mbc` turns it off. |
| Asymmetric L/R PEQ peak gain              | Output-gain compensation uses global `max(L,R)` peak                    | 8353 profiles have positive PEQ gain; ~110 now show a differing L/R peak positive gain (was 0 on the original cohort) | The `max(L,R)` compensation still handles them, but per-channel peak divergence is now real, not hypothetical — worth a closer look. |
| Non-zero `dialog-enhancer-ducking`        | Not currently read by the script (irrelevant on present pipeline)        | 616/36827 rows have ducking=6 or 8 (§1)                                            | Informational — no downstream consumer, but the "always 0" invariant claim was too strong |
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
| `normal`                | 23528 | All laptops — the mode selected by default             |
| `laptop`                | 3173  | Convertible in clamshell pose                          |
| `stand`                 | 3163  | Convertible in stand/present pose                      |
| `tablet`                | 3119  | Convertible folded flat                                |
| `tent`                  | 3109  | Convertible in tent pose                               |
| `lid_close`             |  257  | Lid-closed external-monitor use                        |
| `detachable_speaker`    |  220  | Detachable tablet-with-dock SKUs                       |
| `book` / `flat` / `hybridaudio_detached` | 30 each | Book / flat-on-desk / detached-speaker paths |

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
| `dynamic`           | 3825 | Primary listening profile                             |
| `movie`             | 3825 |                                                       |
| `music`             | 3825 |                                                       |
| `voice`             | 3825 |                                                       |
| `off`               | 3825 | No-op pass-through                                    |
| `game`              | 3739 |                                                       |
| `personalize_user1` | 3739 | User-customisable slot 1 (not the `personalize` alias) |
| `personalize_user2` | 3739 | Slot 2                                                |
| `personalize_user3` | 3739 | Slot 3                                                |
| `voice_onlinecourse`| 2316 | Ultra-gentle leveler profile (§4)                     |
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

---

## 14. Newer-pipeline DSP blocks not modeled by the script

The newer Lenovo IdeaPad / ThinkPad-X13s SoundWire packages introduced several DSP
blocks that don't appear in the original Realtek/Intel cohort. The script does not
implement any of them. Two are flagged at parse time via `warn_unmodeled_features`
in `dolby_to_easyeffects.py`; the rest are inactive across the entire 2483-XML
corpus and silently dropped.

| Block                                              | Element(s)                                                                   | Active in corpus                          | Status                                                                                                  |
|----------------------------------------------------|------------------------------------------------------------------------------|-------------------------------------------|---------------------------------------------------------------------------------------------------------|
| Dynamic Speaker Optimization (DSO)                 | `init-info/dynamic_speaker_optimization_enable`, `dynamic-speaker-optimization-amount`, `dynamic-speaker-optimization-speaker-interval` | 1 XML — IdeaPad-5x-2-in-1 SoundWire SPK1 (`SUBSYS_37A317AA`) | **Warned at parse time.** Excursion-aware bass limiting tied to driver size; needs Dolby DSP data we don't have. |
| Advanced speaker virtualizer                       | `advanced-speaker-virtualizer-rendering-config`, `advanced-speaker-virtualizer-start-bin`, `speaker_virtualizer_mode` | Same 1 XML                                | **Warned at parse time.** Newer FFT-domain replacement for `output-mode-partial-{surround,height}-virtualizer-enable`; also unmodeled. |
| Volume-leveler compressor sub-component            | `volume-leveler-compressor-enable`                                           | 110 XMLs (Yoga-7-2-in-1 BT, IdeaPad-5x-2-in-1 internal) | Not warned. VL is bypassed entirely (autogain trap, see design-notes.md), so dropping the sub-block has no audible effect. |
| Rear / rear-height virtualizer angles              | `virtualizer-rear-speaker-angle`, `virtualizer-rear-height-speaker-angle`, `rear-height-filter-mode` | Common on 4+ speaker laptops              | Not modeled. The legacy `output-mode-partial-{surround,height}-virtualizer-enable` already isn't modeled either; see CLAUDE.md and design-notes.md. |
| Surround-decoder centre spreading                  | `surround-decoder-center-spreading-enable`                                   | Present in 1234 XMLs, **enabled in 0**    | Defensive — would silently drop if a future driver enables it.                                          |
| Woofer-only regulator                              | `woofer-regulator-enable`, `woofer-regulator-tuning`                         | Present in 1234, **enabled in 0**         | Defensive — would silently drop if a future driver enables it.                                          |
| Independent regulator mode                         | `regulator-independent-enable`                                               | 1 XML, never enabled                      | Defensive.                                                                                              |
| Bass-extraction LFE gain                           | `bass-extraction-lfe-gain`                                                   | Present in 1234, **enabled in 0**         | Defensive — bass-extraction itself is universally off.                                                  |
| Channel-gain matrix attributes                     | `gain_c`, `gain_l`, `gain_r`, `gain_ls`, `gain_rs`, `gain_lfe`, `gain_lrs`, `gain_rrs`, `gain_ltm`, `gain_rtm` | Companion to virtualizer downmix          | Tied to the unmodeled virtualizer; would only matter once advanced-virt is implemented. **NB:** inside `<audio-optimizer-bands>`, simplified-schema XMLs reuse `gain_l`/`gain_r` as the L/R speaker-correction arrays — *those* are modeled (mapped to the `ch_00`/`ch_01` slots, issue #22), unrelated to the downmix matrix here. |

The `_UNMODELED_FEATURES` table in `dolby_to_easyeffects.py` carries the two
rare-but-real cases above (DSO, advanced virtualizer) plus four watch-only
fields (`peak-level`, `ieq-bands-set`, `regulator-overdrive`,
`regulator-relaxation-amount`) that warn only when an XML deviates from the
corpus constants — silent on every shipped tuning today. The
universally-present-but-never-enabled defensive elements in the table above
are deliberately *not* listed — they'd fire on every run for no gain. If a
future driver release flips one of them on, the corpus sweep will catch it
before the warning needs to.

### Why these aren't implemented

For each of the two warned features, implementation would require either real
device measurements or undocumented Dolby DSP internals:

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

5. **Every device has a unique regulator curve** — 399 distinct threshold patterns,
   confirming these are individually tuned per speaker.

---

## Open follow-ups (from the 2026-06 re-derivation)

Surfaced by the 2483-XML re-derivation; queued, not yet actioned.

1. **Newer-SoundWire regulator gap (code).** `SUBSYS_37A317AA` encodes
   `regulator-tuning/threshold_high` in an `isolated_band` sub-schema with no
   `value` attribute, so the parser falls back to `[0.0]*20` and that device
   gets **no regulator limiting** (9 profiles, §12). The same device already
   needs DSO and advanced-virtualizer handling (§14). Parse the sub-schema or
   warn.
2. **Asymmetric L/R PEQ peak gain (investigate).** ~110 profiles now show a
   differing L/R peak positive gain (was 0 on the original cohort, §12).
   Re-check against the script's actual `max(L,R)` output-gain compensation —
   is global-max adequate, or is per-channel handling needed?
3. **Voice-AO divergence rate (re-derive).** §8's "97% of devices" is the
   original-cohort figure; a naïve current-cohort recompute lands ~60% but
   mixes simplified-schema `gain_l`/`gain_r` XMLs. Re-derive per-endpoint for a
   clean rate.
4. **1-band MBC audibility (validate).** Music 1-band MBC band-0 ratios reach
   ~6:1 at thresholds to −12 dB (§2) — real compression, not just makeup.
   Audition a high-ratio example; the `mbc-1band` path is still experimental.
5. **`peak-level` disposition (minor).** Not universal — 244 nonzero rows
   (−13 dB on 175). It's watch-listed in `_UNMODELED_FEATURES`; confirm that's
   the right call rather than reading it.
6. **Re-run on new driver pulls (process).** Regenerate every figure here with
   [`tools/corpus_audit.py`](../tools/corpus_audit.py) after pulling new
   packages — the corpus has roughly doubled since the prior derivation.
