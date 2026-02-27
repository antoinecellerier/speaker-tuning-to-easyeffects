# Dolby DAX3 to EasyEffects Preset Converter

Converts Dolby Atmos DAX3 tuning XML extracted from Windows drivers into EasyEffects output presets. Developed for the ThinkPad X1 Yoga Gen 7 (Realtek ALC287, subsystem 17AA:22E6) running Debian with PipeWire 1.4.10 and EasyEffects 8.1.2.

## Quick start

1. Place the Dolby tuning XML in this directory (see [Extracting the XML](#extracting-the-xml))
2. Run `python3 dolby_to_easyeffects.py`
3. Load a preset in EasyEffects: Presets → Dolby-Balanced / Dolby-Detailed / Dolby-Warm

### Dependencies

- Python 3, NumPy, SciPy

## What the script does

### Input: Dolby DAX3 XML

The XML (`DEV_0287_SUBSYS_*.xml`) contains two processing stages:

- **tuning-cp** (Content Processing): software DSP — IEQ, graphic EQ, dialog enhancer, surround decoder, volume leveler
- **tuning-vlldp** (Very Low Latency Driver Path): hardware-level DSP — audio-optimizer (speaker correction), speaker PEQ, multi-band compressor, regulator

### Output: EasyEffects presets

Each preset contains two plugins chained in order:

1. **Convolver** — FIR impulse response implementing the combined IEQ target curve + audio-optimizer speaker correction
2. **Equalizer** — the 3 explicit speaker PEQ bell filters per channel from the vlldp section

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
        ...
      </tuning-vlldp>
    </profile>
  </endpoint>
```

### Profile differences

| Profile | IEQ enabled | IEQ curve | vlldp AO/PEQ | MB compressor |
|---|---|---|---|---|
| dynamic | yes | ieq_balanced | shared | enabled |
| movie | no | — | shared | enabled |
| music | yes | ieq_balanced | shared | enabled |
| game | no | — | shared | enabled |
| voice | no | — | **different** | disabled |

All non-voice profiles share the same audio-optimizer and speaker PEQ values. The voice profile has different AO tuning and simplified PEQ. The multi-band compressor threshold varies slightly per profile.

### EasyEffects 8.x specifics

- Presets: `~/.local/share/easyeffects/output/` (not `~/.config/`)
- IR files: `~/.local/share/easyeffects/irs/` with `.irs` extension (not `.wav`)
- Convolver uses `"kernel-name"` (filename stem), not the deprecated `"kernel-path"`
- Equalizer has no graphic EQ mode — only parametric (LSP plugin)

## What's not implemented

- **Multi-band compressor** — the Dolby vlldp has a 2-band compressor that maximizes loudness. This is the main reason Windows sounds "more powerful". EasyEffects has a multiband compressor plugin but mapping the Dolby parameters (encoded as raw coefficients) is non-trivial.
- **Volume leveler** — dynamic range compression that brings up quiet passages.
- **Dialog enhancer** — center-channel extraction and boost.
- **High-pass filter** — the speaker PEQ includes a 4th-order HP at 100 Hz to protect the laptop speakers. Skipped since EasyEffects' parametric EQ doesn't have a matching filter type and the speakers physically can't reproduce below ~100 Hz anyway.
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
