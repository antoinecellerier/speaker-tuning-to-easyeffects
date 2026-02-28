# Dolby DAX3 to EasyEffects Preset Converter

Converts Dolby Atmos DAX3 tuning XML extracted from Windows drivers into EasyEffects output presets. Developed for the ThinkPad X1 Yoga Gen 7 (Realtek ALC287, subsystem 17AA:22E6) running Debian with PipeWire 1.4.10 and EasyEffects 8.1.2.

## Quick start

1. Extract the Dolby tuning XML from your Windows driver (see [Extracting the XML](#extracting-the-xml))
2. Run `python3 dolby_to_easyeffects.py path/to/DEV_*.xml`
3. Load a preset in EasyEffects: Presets → Dolby-Balanced / Dolby-Detailed / Dolby-Warm

Options:
- `--list` — show available endpoints and profiles in the XML, then exit
- `--endpoint TYPE` — endpoint type (default: `internal_speaker`)
- `--mode MODE` — endpoint operating mode (default: `normal`)
- `--profile TYPE` — profile type, e.g. `dynamic`, `music`, `voice` (default: first profile)
- `--prefix NAME` — change preset name prefix (default: `Dolby` → `Dolby-Balanced`, etc.)
- `--output-dir DIR` — EasyEffects preset directory (default: `~/.local/share/easyeffects/output/`)
- `--irs-dir DIR` — impulse response directory (default: `~/.local/share/easyeffects/irs/`)

When `--mode` or `--profile` is specified, the preset names include them (e.g. `Dolby-Music-Balanced`, `Dolby-Tablet-Voice-Warm`).

### Dependencies

- Python 3, NumPy, SciPy

## What the script does

### Input: Dolby DAX3 XML

The XML (`DEV_0287_SUBSYS_*.xml`) contains two processing stages:

- **tuning-cp** (Content Processing): software DSP — IEQ, graphic EQ, dialog enhancer, surround decoder, volume leveler
- **tuning-vlldp** (Very Low Latency Driver Path): hardware-level DSP — audio-optimizer (speaker correction), speaker PEQ, multi-band compressor, regulator

### Output: EasyEffects presets

Each preset contains four plugins chained in order:

1. **Convolver** — FIR impulse response implementing the combined IEQ target curve + audio-optimizer speaker correction
2. **Equalizer** — 4th-order high-pass at 100 Hz (speaker protection) + 3 speaker PEQ bell filters per channel from the vlldp section
3. **Multiband Compressor** — 2-band dynamics processing mapped from Dolby's mb-compressor-tuning coefficients, with volmax-boost as output gain
4. **Autogain** — volume leveler that dynamically adjusts output to a target loudness (maps from Dolby's volume-leveler settings)

Output files:
- `~/.local/share/easyeffects/irs/Dolby-{Balanced,Detailed,Warm}.irs` — stereo FIR impulse responses
- `~/.local/share/easyeffects/output/Dolby-{Balanced,Detailed,Warm}.json` — EasyEffects presets

## Key findings

### Unit conversions

- **IEQ and audio-optimizer values**: stored in **1/16 dB** units. Divide by 16 to get dB. Confirmed by `geq_maximum_range=192` = 12 dB (standard graphic EQ range).
- **Speaker PEQ gains**: already in dB (float attributes in XML like `gain="-4.000000"`).
- **ieq-amount**: 0–10 scale where 10 = full strength. Not 0–16 despite other values using /16 convention.

### IEQ target curves are composite targets, not filter gains

The 20-value IEQ arrays (e.g. `ieq_balanced`) represent the **desired composite frequency response**, not individual filter gains. Applying them directly as parametric bell filter gains causes massive overlap stacking (+20–30 dB at mid frequencies).

Approaches tried and their problems:

| Approach | Issue |
|---|---|
| Raw values as bell gains (Q=1.5) | +33 dB cumulative boost at mid frequencies from overlapping filters |
| Iterative solver (center-freq only) | Correct at 20 center points but ±5 dB ripple between bands |
| Least-squares solver (dense grid) | Oscillating gains, still ±4 dB ripple |
| **FIR convolution (current)** | **Perfect frequency response, ≤0.06 dB error everywhere** |

### FIR generation

The script generates minimum-phase FIR filters via cepstral processing:

1. Interpolate the combined IEQ + audio-optimizer target curve to FFT frequency bins
2. Compute the real cepstrum (IFFT of log-magnitude)
3. Apply causal windowing to get minimum-phase cepstrum
4. Reconstruct via FFT → exp → IFFT
5. Normalize so peak frequency response = 0 dB

The `.irs` files are standard RIFF/WAVE (IEEE float32, stereo, 48 kHz, 4096 samples) with the `.irs` extension that EasyEffects 8.x requires.

### XML structure

```
<device_data>
  <constant>
    <band_20_freq fs_48000="47,141,234,...,19688"/>   20 center frequencies at 48kHz
    <ieq_balanced target="157,167,218,...,-283"/>       IEQ curve (1/16 dB)
    <ieq_detailed target="..."/>
    <ieq_warm target="..."/>
  </constant>
  <endpoint type="internal_speaker" operating_mode="normal" fs="48000">
    <profile type="dynamic">              ← also: movie, music, game, voice
      <tuning-cp>
        <ieq-enable value="1"/>           ← enabled for dynamic, music
        <ieq-amount value="10"/>          ← 0-10 scale
        <ieq-bands-set preset="ieq_balanced"/>
        <volume-leveler-enable value="1"/>
        <volume-leveler-amount value="2"/>  ← 0-2 (aggressiveness)
        <volume-leveler-in-target value="-320"/>  ← 1/16 dB = -20 dBFS
        <volume-leveler-out-target value="-320"/>
        <bass-enhancer-enable value="0"/>
        <regulator-enable value="1"/>
        ...
      </tuning-cp>
      <tuning-vlldp>
        <audio-optimizer-enable value="1"/>
        <audio-optimizer-bands>
          <ch_00 value="-240,0,160,..."/>  ← per-channel, 1/16 dB
          <ch_01 value="-240,0,160,..."/>
        </audio-optimizer-bands>
        <speaker-peq-filters>
          <filter speaker="0" type="9" f0="100" order="4"/>        ← HP (skipped)
          <filter speaker="0" type="1" f0="516" gain="-4.0" q="1.5"/>  ← Bell
          <filter speaker="0" type="1" f0="280" gain="3.0" q="2.0"/>
          <filter speaker="0" type="1" f0="400" gain="4.0" q="4.6"/>
          ...
        </speaker-peq-filters>
        <mb-compressor-enable value="1"/>
        <mb-compressor-tuning>
          <group_count value="2"/>                              ← 2 active bands
          <band_group_0 value="3,-103,19639,24080,32123,32"/>   ← low band (6-tuple)
          <band_group_1 value="20,-103,19654,22641,30810,32"/>  ← high band
          <band_group_2 value="20,0,32767,22641,27238,0"/>      ← bypass
          <band_group_3 value="20,0,32767,22641,27238,0"/>      ← bypass
        </mb-compressor-tuning>
        <mb-compressor-target-power-level value="-80"/>         ← 1/16 dB = -5 dBFS
        ...
      </tuning-vlldp>
    </profile>
  </endpoint>
```

### Profile differences

| Profile | IEQ enabled | IEQ curve | Volume leveler | vlldp AO/PEQ | MB compressor |
|---|---|---|---|---|---|
| dynamic | yes | ieq_balanced | on (amount 2) | shared | enabled |
| movie | no | — | off | shared | enabled |
| music | yes | ieq_balanced | on (amount 2) | shared | enabled |
| game | no | — | on (amount 2) | shared | enabled |
| voice | no | — | off | **different** | disabled |

All non-voice profiles share the same audio-optimizer and speaker PEQ values. The voice profile has different AO tuning and simplified PEQ. The multi-band compressor threshold varies slightly per profile.

### Multi-band compressor coefficient decoding

The Dolby MB compressor stores parameters as 6-value tuples of raw DSP coefficients per band. The decoded format:

| Index | Field | Units | Example (band 0) | Decoded |
|-------|-------|-------|-------------------|---------|
| 0 | Crossover band index | index into 20-freq table | 3 | 328 Hz |
| 1 | Threshold | 1/16 dB | -103 | -6.4 dB |
| 2 | Gain coefficient | Q15 fixed-point | 19639 | ratio ≈ 1.67:1 |
| 3 | Attack coefficient | Q15 block-rate | 24080 | ~17 ms |
| 4 | Release coefficient | Q15 block-rate | 32123 | ~268 ms |
| 5 | Makeup gain | 1/16 dB | 32 | +2 dB |

**Gain coefficient → ratio**: `ratio = 1 / (coeff / 32768)`. A value of 32767 (≈1.0) means bypass (1:1 ratio).

**Time constants**: Stored as exponential smoothing coefficients in Q15 format, operating per block (assumed 256 samples at 48 kHz = 187.5 blocks/sec). Decoded via `tau = -1 / (blocks_per_sec * ln(coeff / 32768))`.

**volmax-boost** (`<volmax-boost value="96"/>` in tuning-cp): 96/16 = 6 dB overall output gain boost, applied as `output-gain` on the EasyEffects multiband compressor. This is the main loudness maximizer.

The decoded bands for this device:
- **Band 0** (low, below 328 Hz): threshold -6.4 dB, ratio 1.67:1, attack 17 ms, release 268 ms, makeup +2 dB
- **Band 1** (high, above 328 Hz): threshold -6.4 dB, ratio 1.67:1, attack 14 ms, release 87 ms, makeup +2 dB

### Volume leveler → Autogain mapping

The Dolby volume leveler dynamically adjusts gain to maintain a target loudness level. This maps to EasyEffects' autogain plugin, which uses EBU R 128 loudness measurement:

- **volume-leveler-in/out-target**: -320 in 1/16 dB = -20 dBFS → autogain target of -20 LUFS (approximate equivalent)
- **volume-leveler-amount** (0–2): controls aggressiveness → mapped to `maximum-history` window (amount 0 → 30s gentle, amount 2 → 10s aggressive)
- **Reference**: Geometric Mean (MSI) — combines momentary, short-term, and integrated loudness for balanced behavior

### EasyEffects 8.x specifics

- Presets: `~/.local/share/easyeffects/output/` (not `~/.config/`)
- IR files: `~/.local/share/easyeffects/irs/` with `.irs` extension (not `.wav`)
- Convolver uses `"kernel-name"` (filename stem), not the deprecated `"kernel-path"`
- Equalizer has no graphic EQ mode — only parametric (LSP plugin)

## What's not implemented

- ~~**Multi-band compressor**~~ — now implemented. The Dolby 6-tuple coefficients have been decoded (see [coefficient decoding](#multi-band-compressor-coefficient-decoding)) and mapped to EasyEffects' multiband compressor plugin with 2 bands split at 328 Hz. The `volmax-boost` (6 dB) is applied as compressor output gain. Note: the block size for time constant decoding is assumed to be 256 samples — the exact Dolby block size is unconfirmed, so attack/release times are approximate.
- ~~**Volume leveler**~~ — now implemented via EasyEffects autogain (EBU R 128 loudness targeting). Dolby's volume-leveler-amount (0–2) maps to autogain history window length (30s gentle → 10s aggressive). Target level (-320 = -20 dBFS) maps to -20 LUFS.
- **Dialog enhancer** — center-channel extraction and boost.
- ~~**High-pass filter**~~ — now implemented as a `Hi-pass` band (slope `x4` = 24 dB/oct) in the parametric EQ. Protects laptop speakers from sub-bass energy they can't reproduce.
- **Surround decoder/virtualizer** — spatial audio processing.

## Extracting the XML

The Dolby tuning XML can be found in the Windows driver package, typically at:
```
C:\Windows\System32\DolbyAPO\DAX3\
```
Look for files named `DEV_*_SUBSYS_*.xml`. The `_settings.xml` companion file contains UI/profile defaults.

## References

- [shuhaowu/linux-thinkpad-speaker-improvements](https://github.com/shuhaowu/linux-thinkpad-speaker-improvements) — alternative approach using captured impulse responses via WASAPI loopback
- [sklynic/easyeffects-tuf-gaming-a15](https://github.com/sklynic/easyeffects-tuf-gaming-a15) — manual DAX3 EQ extraction for ASUS laptops
- [EasyEffects source (wwmm/easyeffects)](https://github.com/wwmm/easyeffects) — preset format reference
