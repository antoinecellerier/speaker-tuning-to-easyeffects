# Dolby DAX3 to EasyEffects Preset Converter

Converts Dolby Atmos DAX3 tuning XML from Windows drivers into [EasyEffects](https://github.com/wwmm/easyeffects) 8.x output presets. If your distro still ships EasyEffects 7 (Debian trixie, Ubuntu 24.04+, Fedora 43 and earlier), install the [Flatpak](https://flathub.org/apps/com.github.wwmm.easyeffects) — the preset formats aren't compatible.

## Tested devices

| Device | Codec / Subsystem | Reported by |
|---|---|---|
| ThinkPad X1 Yoga Gen 7 | Realtek ALC287, 17AA:22E6 | author (primary development target) |
| Lenovo Yoga 7 2-in-1 16AKP10 | — | [#1](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/1) |
| ThinkPad T14s Gen 6 AMD | 17AA:50F0 | [#3](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/3) |
| ThinkPad X1 Carbon Gen 13 | Soundwire 17AA:2339 | [PR7](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/pull/7/) |

If you test it on other hardware, please open an issue with your device model and audio codec subsystem ID (`cat /proc/asound/card*/codec* | grep Subsystem`).

## Quick start

1. Install dependencies (see [Dependencies](#dependencies) below for your distro). TL;DR: you need Python 3 with NumPy and SciPy.

2. Run the script. If your Windows partition is mounted or a driver package is extracted in the current directory, no arguments are needed:

   ```bash
   python3 dolby_to_easyeffects.py --autoload
   ```

   Or point it at the Windows directory or a tuning XML explicitly:

   ```bash
   python3 dolby_to_easyeffects.py --windows /mnt/windows/Windows --autoload
   python3 dolby_to_easyeffects.py path/to/DEV_0287_SUBSYS_*.xml --autoload
   ```

The `--autoload` option wires EasyEffects to apply the Dolby correction on your internal speaker automatically. Skip it if you'd rather select a preset yourself (Presets → Dolby-Balanced / Dolby-Detailed / Dolby-Warm); see [Autoload](#autoload) for details.

### Options

- `--windows DIR` — auto-discover tuning XML from a mounted Windows directory. Omit both this flag and a positional XML path to let the script probe `/proc/mounts` and the current directory automatically
- `--list` — show available endpoints and profiles in the XML, then exit
- `--speaker-info` — report detected audio hardware and speaker layout, then exit
- `--endpoint TYPE` — endpoint type (default: `internal_speaker`)
- `--mode MODE` — endpoint operating mode (default: `normal`). Convertible laptops (Yoga-class) ship distinct tunings per hinge pose — try `--mode tablet`, `stand`, `tent`, or `lid_close` if `--list` shows them for your device.
- `--profile TYPE` — profile type, e.g. `dynamic`, `music`, `voice` (default: first profile)
- `--all-profiles` — generate presets for all profiles in the selected endpoint/mode (9 profiles × 3 IEQ curves = 27 presets)
- `--autoload [PRESET]` — write EasyEffects autoload config for speaker outputs; defaults to the first Balanced preset generated
- `--autoload-dir DIR` — autoload config directory (default: `~/.local/share/easyeffects/autoload/output/`)
- `--no-autoload-bypass` — with `--autoload`, don't write a `Nothing` bypass preset or enable EasyEffects' global Fallback Preset. See [Autoload](#autoload) below.
- `--prefix NAME` — change preset name prefix (default: `Dolby` → `Dolby-Balanced`, etc.)
- `--output-dir DIR` — EasyEffects preset directory (default: `~/.local/share/easyeffects/output/`)
- `--irs-dir DIR` — impulse response directory (default: `~/.local/share/easyeffects/irs/`)
- `--disable NAME` — drop a filter from the generated preset (repeatable). Valid names: `volmax`, `mbc`, `regulator`, `bass-enhancer`, `dialog`, `stereo`, `high-shelf`, `lo-pass`. See [Disabling filters](#disabling-filters) below.
- `--dry-run` — run without writing any files to disk (presets, IRs, autoload); useful for debugging script execution and output
- `--no-color` — disable colored terminal output

When `--mode` or `--profile` is specified (or `--all-profiles` is used), the preset names include them (e.g. `Dolby-Music-Balanced`, `Dolby-Tablet-Voice-Warm`).

### Disabling filters

If the generated preset has audible artifacts on your hardware (saturation, pumping, harsh highs, uncomfortable stereo width), you can rebuild it without specific filters rather than hand-editing the chain inside EasyEffects. Repeat `--disable NAME` as many times as needed:

| Name | What to try if you hear... |
|------|----------------------------|
| `volmax` | Output is too loud / the final limiter is pumping on loud masters. Drops the static loudness boost derived from Dolby's `volmax-boost` (typically +6 dB). |
| `mbc` | A compressed or "squashed" character you don't like. Drops the multi-band dynamics processor (1–4 bands depending on profile). |
| `regulator` | Unusual spectral pumping or narrow-band breathing. Drops the per-band limiter; `volmax` (if enabled) falls back to the brickwall limiter's input-gain. |
| `bass-enhancer` | Bass sounds artificial or distorted on SoundWire devices. Only emitted for SoundWire speakers. |
| `dialog` | Vocals feel over-boosted or harsh in the presence region. Drops the 2.5 kHz speech-band EQ. |
| `stereo` | Phasey or hollow stereo image. Drops the surround widener. |
| `high-shelf` | Harsh or sibilant high frequencies on devices whose tuning includes a type-3 shelf (Lenovo AIO-RTK XMLs around 2.7 kHz, +2–5 dB). **Experimental** path — reproduction of the Dolby tuning is numerically verified, but has not yet been audibly validated. Feedback welcome. |
| `lo-pass` | Highs sound rolled off or dull on devices whose tuning includes a type-6/8 low-pass (rare; a few ALC274 SKUs). **Experimental**, same caveat as `high-shelf`. |

Convolver, PEQ, autogain, and the final brickwall limiter can't be disabled from the CLI — they're the FIR correction, speaker PEQ, volume-leveler placeholder, and safety net respectively.

### Autoload

The `--autoload` option configures EasyEffects to automatically apply a preset whenever the internal speaker output becomes active:

Generate all presets and autoload Dolby-Dynamic-Balanced on the speaker:
```bash
python3 dolby_to_easyeffects.py --windows /mnt/windows/Windows \
    --all-profiles --autoload Dolby-Dynamic-Balanced
```

This writes a JSON file to `~/.local/share/easyeffects/autoload/output/` matching EasyEffects' autoload convention (`{node.name}:{device.profile.description}.json`). Speaker sinks are detected from PipeWire via `pw-dump`, filtering on the `audio-speakers` device icon to exclude HDMI/DisplayPort outputs. The script must be run from a desktop session with PipeWire running.

EasyEffects applies the last-loaded preset to whatever sink is currently active, so switching to HDMI, a USB headset, or Bluetooth while a Dolby preset is loaded keeps processing the Dolby correction on hardware it was never tuned for. `--autoload` mitigates this by also writing an empty `Nothing` bypass preset and turning on EasyEffects' global Fallback Preset (pointing it at `Nothing`) — any sink without its own autoload entry then falls back to a no-op chain. If EasyEffects is running when the script writes, you'll need to restart it for the setting to take effect. An existing `Nothing.json` preset is preserved, and an already-enabled fallback (pointing at any preset) is left untouched. Pass `--no-autoload-bypass` to skip both steps if you manage this yourself.

### Dependencies

The script needs Python 3, [NumPy](https://numpy.org/), and [SciPy](https://scipy.org/). PipeWire's `pw-dump` is also required if you use `--autoload`, but it's already installed on any distro running EasyEffects. [Rich](https://github.com/Textualize/rich) and [rich-argparse](https://github.com/hamdanal/rich-argparse) are optional — if installed, the script renders its output and `--help` with semantic colors; without them, output is plain monochrome and everything else still works.

Install on your distro:

- **Debian / Ubuntu / Mint / Pop!_OS:** `sudo apt install python3-numpy python3-scipy python3-rich python3-rich-argparse`
- **Fedora / RHEL / Rocky / Alma:** `sudo dnf install python3-numpy python3-scipy python3-rich python3-rich-argparse`
- **openSUSE (Leap / Tumbleweed):** `sudo zypper install python3-numpy python3-scipy python3-rich python3-rich-argparse`
- **Arch / Manjaro / EndeavourOS:** `sudo pacman -S python-numpy python-scipy python-rich python-rich-argparse`
- **Alpine:** `sudo apk add py3-numpy py3-scipy py3-rich py3-rich-argparse`
- **Gentoo:** `sudo emerge dev-python/numpy dev-python/scipy dev-python/rich dev-python/rich-argparse`
- **NixOS (shell):** `nix-shell -p "python3.withPackages (ps: with ps; [ numpy scipy rich rich-argparse ])"`

If your distro isn't listed or you'd rather not touch system packages, a venv works too:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install numpy scipy rich rich-argparse
```

## Extracting the XML

The easiest way is to use `--windows` to auto-discover the XML from a mounted Windows partition. The script reads your audio codec's subsystem ID from `/proc/asound` and matches it against the XMLs in the DriverStore.

If you prefer to extract the XML manually, it can be found in the Windows driver package at:
```
C:\Windows\System32\DriverStore\FileRepository\dax3_ext_*.inf_*\DEV_*_SUBSYS_*.xml
```
Match the `SUBSYS_` portion of the filename to your audio codec's subsystem ID (visible via `cat /proc/asound/card*/codec* | grep Subsystem`). The `_settings.xml` companion file contains UI/profile defaults and is not needed.

### From a Lenovo driver EXE (no Windows partition required)

Download the Lenovo audio driver EXE (e.g. `n4ba126w.exe`) into this project directory. You need [`innoextract`](https://constexpr.org/innoextract/install) installed.

From the project root, run these two commands:

```bash
# 1. Extract only the Dolby tuning XMLs into ./driver-cache/
innoextract -I 'code$GetExtractPath$/Dolby/03_dax_ext' -d ./driver-cache ./n4ba126w.exe

# 2. Generate presets (autoprobe finds the extracted XMLs automatically)
python3 dolby_to_easyeffects.py --autoload
```

If the autoprobe reports ambiguity (e.g. you have several extracted driver trees), pass `--windows ./driver-cache` to point it at the one you want.

## Auto-detection notes

### Windows partition or extracted DriverStore

Omitting `--windows` and the positional XML triggers the autoprobe. It enumerates NTFS-family mountpoints (`ntfs`, `ntfs3`, `fuseblk`) from `/proc/mounts` and keeps any whose DriverStore contains `dax3_ext_*.inf_*` subdirs — both full system roots like `/mnt/windows/Windows` and drive-root mounts like `/mnt/c` are accepted. If nothing mounted matches, it falls back to a bounded walk of the current directory for any directory whose files include a Dolby-shaped XML (`DEV_*_SUBSYS_*.xml` / `SOUNDWIRE_*_SUBSYS_*.xml` / `SDW_*_SUBSYS_*.xml`, excluding `_settings.xml` companions). That covers the raw `innoextract` layout (`./driver-cache/code$GetExtractPath$/Dolby/03_dax_ext/`) as well as hand-organised collections — no `dax3_ext_*.inf_*` rename required. The walk skips hidden directories, doesn't follow symlinks, and is depth-capped.

A single unambiguous match is used. When multiple candidates match, the autoprobe narrows to those containing an XML for your detected audio hardware and uses it if exactly one survives; otherwise it errors with the shortlist so you can pick one via `--windows DIR`.

### Flatpak EasyEffects

The script auto-detects whether EasyEffects is installed via Flatpak or as a native package. If `~/.var/app/com.github.wwmm.easyeffects/config/easyeffects/` exists, it writes presets there; otherwise it falls back to the native `~/.local/share/easyeffects/` path. You can still override with `--output-dir`, `--irs-dir`, and `--autoload-dir`.

### SoundWire codecs (newer Intel platforms)

Auto-detection also handles SoundWire-based audio (Lunar Lake and later, Meteor Lake, some Tiger/Alder Lake SKUs). The script reads device IDs from `/sys/bus/soundwire/devices/` and the PCI subsystem ID of the HD Audio controller from `/sys/class/sound/card*/device`, and matches them against Dolby filenames of the form `SOUNDWIRE_MAN_<man>_FUNC_<func>_SUBSYS_<device><vendor>.xml` (e.g. `SOUNDWIRE_MAN_025D_FUNC_1318_SUBSYS_233917AA.xml`). `--windows` accepts either a full Windows system root (e.g. `/mnt/windows/Windows`), a drive-root mount (e.g. `/mnt/c` — the script looks for a case-insensitive `Windows/` child), *or* an already-extracted DriverStore directory containing `dax3_ext_*.inf_*` subfolders directly.

## What the script does

### Input: Dolby DAX3 XML

The XML (`DEV_0287_SUBSYS_*.xml`) contains two processing stages:

- **tuning-cp** (Content Processing): software DSP — IEQ, graphic EQ, dialog enhancer, surround decoder, volume leveler
- **tuning-vlldp** (Very Low Latency Driver Path): hardware-level DSP — audio-optimizer (speaker correction), speaker PEQ, multi-band compressor, regulator

### Output: EasyEffects presets

Each preset contains up to eight plugins chained in order:

1. **Convolver** — FIR impulse response implementing the combined IEQ target curve + audio-optimizer speaker correction
2. **Stereo Tools** — stereo widening via Mid/Side balance (Calf Stereo Tools), mapped from Dolby's surround-boost; enabled on dynamic/movie profiles
3. **Equalizer** — 4th-order high-pass at 100 Hz (speaker protection) + speaker PEQ filters (bells, shelves, and HP/LP) per channel from the vlldp section
4. **Dialog Enhancer** — broad speech-band EQ boost at 2.5 kHz (second equalizer instance), gain scaled by the Dolby dialog-enhancer-amount; enabled on most profiles except music
5. **Autogain** — volume leveler mapped from Dolby's volume-leveler settings; **bypassed by default** because without Dolby's MI (Media Intelligence) steering the autogain causes audible distortion on quiet→loud transitions. Settings are preserved so users can enable it manually. Placed before the compressor to match Dolby's CP→VLLDP signal flow
6. **Multiband Compressor** — multi-band dynamics processing mapped from Dolby's `mb-compressor-tuning` coefficients; emits 1 to 4 bands based on the XML's `group_count` (dominated by 2-band tunings in the wild, but 3- and 4-band tunings — including voice-profile speech compression and music-profile per-band makeup — are also supported)
7. **Regulator** — per-band limiter (second multiband compressor instance) mapped from Dolby's regulator-tuning thresholds, protecting speakers from distortion at specific frequency ranges; also the primary slot where Dolby's `volmax-boost` is applied as `output-gain` (typically +6 dB of loudness makeup)
8. **Limiter** — brickwall output limiter at -1 dBFS as a safety net to catch any remaining inter-sample peaks; fallback slot for `volmax-boost` when the regulator isn't emitted

Output files:
- `~/.local/share/easyeffects/irs/Dolby-{Balanced,Detailed,Warm}.irs` — stereo FIR impulse responses
- `~/.local/share/easyeffects/output/Dolby-{Balanced,Detailed,Warm}.json` — EasyEffects presets

### EasyEffects 8.x specifics

- Presets: `~/.local/share/easyeffects/output/` (not `~/.config/`)
- IR files: `~/.local/share/easyeffects/irs/` with `.irs` extension (not `.wav`)
- Convolver uses `"kernel-name"` (filename stem), not the deprecated `"kernel-path"`
- Equalizer has no graphic EQ mode — only parametric (LSP plugin)

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
        <volume-leveler-amount value="2"/>  ← 0-10 (aggressiveness)
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
          <filter speaker="0" type="9" f0="100" order="4"/>        ← HP filter
          <filter speaker="0" type="1" f0="516" gain="-4.0" q="1.5"/>  ← Bell
          <filter speaker="0" type="1" f0="280" gain="3.0" q="2.0"/>
          <filter speaker="0" type="1" f0="400" gain="4.0" q="4.6"/>
          ...
        </speaker-peq-filters>
        <mb-compressor-enable value="1"/>
        <mb-compressor-tuning>
          <group_count value="2"/>                              ← 1–4 active bands (dev device: 2)
          <band_group_0 value="3,-103,19639,24080,32123,32"/>   ← low band (6-tuple)
          <band_group_1 value="20,-103,19654,22641,30810,32"/>  ← high band
          <band_group_2 value="20,0,32767,22641,27238,0"/>      ← unused on this device (schema slot)
          <band_group_3 value="20,0,32767,22641,27238,0"/>      ← unused on this device (schema slot)
        </mb-compressor-tuning>
        <mb-compressor-target-power-level value="-80"/>         ← 1/16 dB = -5 dBFS
        <regulator-speaker-dist-enable value="1"/>
        <regulator-tuning>
          <threshold_high value="-160,-144,-128,-80,0,..."/>    ← 1/16 dB per band
          <threshold_low value="-352,-336,-320,-272,-192,..."/> ← 1/16 dB per band
        </regulator-tuning>
        <regulator-stress-amount value="144,144,0,0,0,0,0,0"/> ← 1/16 dB
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

The Dolby MB compressor uses `group_count` active bands (1 to 4, capped at LSP MBC's 8-band ceiling) with parameters stored as 6-value tuples of raw DSP coefficients per band. Bands beyond `group_count` are ignored — the XML always allocates 4 `band_group_N` slots, but only the first `group_count` are decoded. Each band's decoded format:

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

**volmax-boost** (`<volmax-boost value="96"/>` in tuning-cp): 96/16 = 6 dB. This defines the maximum gain the Dolby volume leveler may add above its output target, i.e. the ceiling of Dolby's VolMax loudness maximiser. EasyEffects has no MI-steered leveler to apply it dynamically, so the script applies it as a static `output-gain` on the regulator (`multiband_compressor#1`), with a fallback to `input-gain` on the brickwall limiter when the regulator isn't emitted. Can be turned off with `--disable volmax` (see below).

The decoded bands for the development device (2-band tuning):
- **Band 0** (low, below 328 Hz): threshold -6.4 dB, ratio 1.67:1, attack 17 ms, release 268 ms, makeup +2 dB
- **Band 1** (high, above 328 Hz): threshold -6.4 dB, ratio 1.67:1, attack 14 ms, release 87 ms, makeup +2 dB

Other devices in the corpus ship 3- or 4-band tunings — e.g. a voice profile with 2:1 speech-band compression above 1.3 kHz and 7 kHz, or a music profile using four bands as a per-band makeup stage (1:1 ratios with +1–3 dB per band). See the cross-device findings for distribution.

### Volume leveler → Autogain mapping

The Dolby volume leveler dynamically adjusts gain to maintain a target loudness level. This maps to EasyEffects' autogain plugin, which uses EBU R 128 loudness measurement. **Bypassed by default** — without Dolby's MI (Media Intelligence) content analysis, the autogain causes audible distortion on quiet→loud transitions because it can't anticipate dynamic changes. The Dolby-derived settings are preserved so users can enable it manually:

- **volume-leveler-in/out-target**: -320 in 1/16 dB = -20 dBFS → autogain target of -20 LUFS (matching the Dolby config directly)
- **volume-leveler-amount** (0–10): controls aggressiveness → mapped to `maximum-history` window (amount 0 → 30s gentle, amount 4+ → 10s aggressive)
- **Reference**: Geometric Mean (MSI) — combines momentary, short-term, and integrated loudness for balanced behavior

### Regulator → Per-band limiter

The Dolby regulator is a 20-band limiter that prevents speaker distortion by clamping per-band levels to `threshold_high` values (in 1/16 dB). This is mapped to a second EasyEffects multiband compressor instance (`multiband_compressor#1`) configured as a limiter with Peak sidechain and 1 ms attack.

Additional regulator parameters:
- **`regulator-distortion-slope`** (1/16 scale): controls limiting aggressiveness. Slope 1.0 = hard limiter (ratio 100:1), lower values → softer compression (ratio = 1/(1-slope))
- **`regulator-timbre-preservation`** (1/16 scale): controls knee softness to preserve tonal balance. Mapped to compressor knee: knee = -6 × timbre dB (0.75 → -4.5 dB knee)

The 20 Dolby bands are grouped into zones with identical thresholds to fit within EasyEffects' 8-band limit. For this device, this produces 5 zones:

| Zone | Frequency range | Threshold |
|------|----------------|-----------|
| 0 | below 81 Hz | -10 dB |
| 1 | 81–182 Hz | -9 dB |
| 2 | 182–277 Hz | -8 dB |
| 3 | 277–392 Hz | -5 dB |
| 4 | above 392 Hz | 0 dB (no limiting) |

The tighter limiting at low frequencies protects laptop speakers from sub-bass distortion they can't reproduce cleanly. The `threshold_low` values (more aggressive thresholds) and `stress-amount` are not currently used — only `threshold_high` is mapped.

## What's not implemented

- **`filter_coefficients`** — base64-encoded biquad blob in `tuning-vlldp`. Investigated but the format doesn't produce sensible audio EQ curves; likely VLLDP-internal analysis filters rather than audio-path EQ. The audio-optimizer + PEQ parameters already capture the same speaker correction.
- **`regulator-stress-amount`** / **`threshold_low`** — secondary regulator parameters not mapped; only `threshold_high` is used for the per-band limiter.

### Unused XML data (not worth implementing)

The following XML fields are present but deliberately ignored — they are always zero/disabled on this device, are DSP pipeline internals with no EasyEffects equivalent, or relate to multi-channel/subwoofer routing irrelevant for stereo laptop output:

- `pregain`, `postgain`, `calibration-boost`, `system-gain` — all 0 dB gain trims
- `bass-enhancer-*`, `bass-extraction-*` — always disabled
- `virtual-bass-*` — always disabled
- `volume-modeler-*` — always disabled
- `graphic-equalizer-*` — always disabled (user-facing 20-band GEQ)
- `surround-decoder-center-spreading-enable` — surround upmix sub-parameter
- `virtualizer-*-speaker-angle`, `height-filter-mode` — virtualizer geometry (stereo-base mapping is simpler)
- `mi-*-steering-enable` — Media Intelligence auto-steering flags
- `output-mode` / `mix_matrix` / `processing_mode` — speaker routing
- `init-info` blocks — DSP buffer/capacity sizing
- CP-level `audio-optimizer-bands` (ch_00–ch_07) — always zero; vlldp has the real data
- CP-level `regulator-tuning` — always zero presets; vlldp has the real data
- `mb-compressor-agc-enable`, `mb-compressor-slow-gain-enable` — always off
- `woofer-regulator-*` — no subwoofer in this endpoint
- `band_20_freq` at 44.1 kHz — script is 48 kHz only
- `ieq-bands-set` — indicates default IEQ variant; script generates all three

## Further reading

In-tree docs with more context on specific aspects:

- [docs/design-notes.md](docs/design-notes.md) — why the plugin chain is ordered the way it is, gain-staging rationale, and why autogain is bypassed by default
- [docs/cross-device-findings.md](docs/cross-device-findings.md) — empirical analysis of ~1850 DAX3 tuning files across Realtek, Senary, Qualcomm Aqstic, and SoundWire smart-amp codecs, including which DSP blocks are unmodeled
- [docs/alternative-pipelines.md](docs/alternative-pipelines.md) — design sketches for offloading parts of the pipeline to Intel SOF DSP or running under PipeWire filter-chain instead of EasyEffects

## References

- [wwmm/easyeffects](https://github.com/wwmm/easyeffects) — preset format reference
- [shuhaowu/linux-thinkpad-speaker-improvements](https://github.com/shuhaowu/linux-thinkpad-speaker-improvements) — alternative approach using captured impulse responses via WASAPI loopback
- [taprobane99/Lenovo-Yoga-Slim-7x-Dolby-Linux-Audio](https://github.com/taprobane99/Lenovo-Yoga-Slim-7x-Dolby-Linux-Audio) — downstream port of this script's output to a PipeWire `filter-chain` config with 4-speaker upmix on Snapdragon X (see `docs/alternative-pipelines.md` Option 3)
- [sklynic/easyeffects-tuf-gaming-a15](https://github.com/sklynic/easyeffects-tuf-gaming-a15) — manual DAX3 EQ extraction for ASUS laptops
- [mister2d/thinkpad-linux-audio](https://github.com/mister2d/thinkpad-linux-audio/) — extended Dolby pipeline for ThinkPads, built on top of this tooling ([#2](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/2))
