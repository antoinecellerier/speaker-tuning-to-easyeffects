# Cross-device DAX3 findings

> Interpretive analysis of the parameter schema used by Dolby DAX3 tuning XML (distributed publicly as part of Windows audio driver packages), for the purpose of Linux interoperability. No verbatim tuning arrays are reproduced.

Cohort-level analysis of **196 DAX3 tuning files** spanning **3 Realtek codec variants**
(ALC257, ALC285, ALC287) found in the `dax3_ext_rtk` and `fusion_ext_intel` driver
packages. The README documents how the script handles one specific device; this doc
captures what's universal across the ecosystem and what varies from device to device,
so readers can judge which parts of the pipeline are portable and which are tuned.

| Codec  | Devices | MBC enabled | MBC disabled |
|--------|---------|-------------|--------------|
| ALC257 | 236     | 19 (8%)     | 150 (64%)    |
| ALC285 | 27      | 0 (0%)      | 14 (52%)     |
| ALC287 | 81      | 4 (5%)      | 63 (78%)     |

> All files live under two driver packages: `dax3_ext_rtk` (Realtek, 196 files) and
> `fusion_ext_intel` (Intel, 67 files sharing the same XMLs). All endpoint types are
> `internal_speaker` — no headphone or external tunings.

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
| `dialog-enhancer-ducking`          | 0                  | Always zero                        |
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

Of 196 devices on the `dynamic` profile:

- **153** have PEQ filters
- Most have **1–2 filters per speaker** (typically a high-pass + one bell)
- **2 devices** have **5 filters per speaker** (the most complex)
- **4 unique filter types** are used:

| Type | Description                       | Count      | Script support |
|------|-----------------------------------|------------|----------------|
| 1    | Bell/peaking EQ                   | majority   | ✅ Yes         |
| 9    | High-pass (with order)            | common     | ✅ Yes         |
| 4    | Low-shelf (with S parameter)      | 4 filters  | ✅ Yes         |
| 7    | High-pass variant (with order)    | 8 filters  | ✅ Yes         |

### Type 4 — low-shelf filter

```xml
<filter speaker="0" enabled="1" type="4" f0="600" gain="2.000000" s="1.000000"/>
```

Uses frequency, gain, and **slope (S)** parameter instead of Q. The script maps it to
EasyEffects `"type": "Lo-shelf"` with Q derived from S via the standard audio shelf
formula.

### Type 7 — high-pass (Butterworth-style)

```xml
<filter speaker="0" enabled="1" type="7" f0="100" order="4"/>
```

Same structure as type 9 (HP with order, no gain). Likely a different filter topology
(e.g., Butterworth vs Linkwitz-Riley). The script treats it identically to type 9.

Types 4 and 7 only appear across 12 filters / 196 devices (rare). The script warns on
any other unknown type.

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
