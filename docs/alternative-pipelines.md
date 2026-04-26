# Alternative pipelines

Design sketches for replacing or offloading parts of the Dolby effects pipeline
beyond the default EasyEffects path, to reduce CPU/memory overhead, enable headless
use, or move processing onto dedicated hardware.

Development system used for the notes below: ThinkPad X1 Yoga Gen 7, Intel Alder
Lake-P, Realtek ALC287, SOF firmware (`sof-hda-dsp`), PipeWire 1.4.10.

## Current pipeline (all software, via EasyEffects)

```
Audio → Convolver (FIR) → Equalizer (IIR) → MB Compressor → Regulator → Autogain → Speaker
            IEQ + AO       HP + PEQ bells      dynamics      limiter    vol leveler
```

All five stages run as LV2 plugins inside the EasyEffects process, which sits in the
PipeWire graph as a filter node.

## Option 1: Intel SOF DSP — IIR EQ on the playback path

**Status: ready to use now**

The `sof-hda-generic` topology already loads an IIR EQ component on the analog
playback pipeline. It's exposed as an ALSA byte control:

```
numid=39, EQIIR2.0 eqiir_bytes_2   — 1024-byte IIR EQ, speaker/headphone playback
numid=45, EQIIR10.0 eqiir_coef_10  — 1024-byte IIR EQ, DMIC capture path 0
numid=47, EQIIR11.0 eqiir_coef_11  — 1024-byte IIR EQ, DMIC capture path 1
```

### What can run on it

The **speaker PEQ** from the vlldp section — a 4th-order highpass at 100 Hz plus
3 bell filters per channel — maps directly to IIR biquad sections:

| Filter                   | Biquad sections           |
|--------------------------|---------------------------|
| 4th-order HP @ 100 Hz    | 2 (cascaded 2nd-order)    |
| 3 bells × 2 channels     | 6                         |
| **Total**                | **8 biquads**             |

At ~24 bytes per biquad section plus header overhead, this fits comfortably in the
1024-byte blob.

### What cannot run on it

- **FIR convolver** (IEQ + audio-optimizer): the loaded topology has no FIR EQ
  component. The IEQ target curve requires FIR for accurate reproduction (IIR
  approximation of the 20-band curve produces ±4–5 dB ripple).
- **Multiband compressor / regulator / autogain**: the generic HDA topology
  doesn't load DRC modules.

### How to program it

The SOF IIR EQ accepts a binary blob of biquad coefficients in Q2.30 fixed-point
format, written via ALSA:

```bash
amixer -c0 cset numid=39 < blob.bin
```

The blob format (SOF `struct sof_eq_iir_config`):

```
Header:
  uint32_t size              — total blob size in bytes
  uint32_t channels_in_config — number of channels (2 for stereo)
  uint32_t number_of_responses — number of distinct filter responses
  int32_t  reserved[4]
  uint16_t assign_response[channels] — which response index each channel uses
  (padding to 32-bit alignment)

Per response:
  uint32_t num_biquads
  Per biquad (struct sof_eq_iir_biquad):
    int32_t b0   — Q2.30 fixed-point
    int32_t b1
    int32_t b2
    int32_t a1   — negated (SOF convention: y[n] = b0*x[n] + ... - a1*y[n-1] - a2*y[n-2])
    int32_t a2
    int16_t shift — output right-shift for gain normalization
    int16_t reserved
```

Coefficient design tools:
[thesofproject/sof — tools/tune/eq/](https://github.com/thesofproject/sof/tree/main/tools/tune/eq)

### Benefits

- **Zero CPU cost** — runs on the DSP's Xtensa cores
- **Lowest possible latency** — processes audio before it leaves the DSP
  pipeline, no PipeWire graph hop
- **Always active** — works even without EasyEffects or PipeWire running
  (applies at the ALSA/SOF level)
- **Headphone-safe** — the PEQ is speaker-specific, so moving it to the DSP
  means it only applies to the HDA analog output, not to Bluetooth or USB audio

### Integration approach

The script could gain a `--sof-peq` option that:

1. Computes biquad coefficients for the HP + bell filters (already calculated
   during preset generation)
2. Packs them into the SOF IIR blob format
3. Writes via `amixer cset numid=39`
4. Removes the corresponding EQ stage from the EasyEffects preset (so it's not
   applied twice)

The remaining EasyEffects preset would be: Convolver → MB Compressor →
Regulator → Autogain (4 plugins instead of 5).

## Option 2: Custom SOF topology with FIR EQ and DRC

**Status: advanced, requires building a custom topology**

The SOF firmware for Alder Lake (`sof-adl.ri`) includes `eq_iir` but the
current signed firmware binary does **not** appear to include `eq_fir`, `drc`,
`crossover`, or `multiband_drc` modules (zero references found in the binary).
The community (unsigned) firmware build may include them, or they could be
compiled in from source.

If a firmware with these modules were available, a custom ALSA topology
(`sof-hda-generic.tplg`) could chain:

```
Host PCM → EQ FIR → EQ IIR → DRC → DAI (codec)
           IEQ+AO    PEQ  compressor
```

This would offload the convolver, PEQ, and compressor to the DSP, leaving only
the regulator and autogain in software. However:

- Building custom topologies requires `alsatplg` or SOF's topology2 tools
- The signed firmware may refuse to load custom topologies without matching
  signatures
- Firmware-level bugs would be much harder to debug than EasyEffects plugin
  issues
- Each SOF/kernel update could require topology rebuilds

This is the most complete offload option but also the highest-effort and
highest-risk.

## Option 3: PipeWire filter-chain (lightweight EasyEffects replacement)

**Status: fully feasible with existing packages**

PipeWire's `filter-chain` module can replicate the entire effects pipeline
without the EasyEffects GTK process. It uses the same underlying infrastructure
(PipeWire graph nodes) but with a lighter footprint and no GUI.

### Available processing blocks

Already installed on the development system:

| Pipeline stage       | filter-chain implementation                               |
|----------------------|-----------------------------------------------------------|
| FIR convolver        | `builtin` type, `convolver` label — reads the same `.irs` WAV files |
| HP + PEQ bells       | `builtin` types: `bq_highpass`, `bq_peaking`              |
| Multiband compressor | LADSPA: `ZaMultiCompX2-ladspa.so` (Zam plugins)           |
| Limiter / maximizer  | LADSPA: `ZaMaximX2-ladspa.so`                             |
| Autogain / loudness  | SPA plugin: `libspa-filter-graph-plugin-ebur128.so`       |

LV2 plugins are also available (LSP plugin suite: `mb_compressor`,
`sc_mb_limiter_stereo`, etc.) but LADSPA is simpler for filter-chain configs.

### Example config skeleton

```conf
# ~/.config/pipewire/filter-chain.conf.d/dolby-speaker.conf
context.modules = [
    { name = libpipewire-module-filter-chain
        args = {
            node.description = "Dolby Speaker Processing"
            media.name       = "Dolby Speaker Processing"
            filter.graph = {
                nodes = [
                    # FIR convolver (IEQ + audio-optimizer)
                    {
                        type   = builtin
                        name   = convL
                        label  = convolver
                        config = { filename = "~/.local/share/easyeffects/irs/Dolby-Balanced.irs" channel = 0 }
                    }
                    {
                        type   = builtin
                        name   = convR
                        label  = convolver
                        config = { filename = "~/.local/share/easyeffects/irs/Dolby-Balanced.irs" channel = 1 }
                    }
                    # High-pass filter (speaker protection)
                    { type = builtin name = hpL label = bq_highpass control = { "Freq" = 100.0 "Q" = 0.707 } }
                    { type = builtin name = hpR label = bq_highpass control = { "Freq" = 100.0 "Q" = 0.707 } }
                    # Speaker PEQ bells (per-channel from vlldp)
                    { type = builtin name = peq1L label = bq_peaking control = { "Freq" = 516.0 "Q" = 1.5 "Gain" = -4.0 } }
                    { type = builtin name = peq2L label = bq_peaking control = { "Freq" = 280.0 "Q" = 2.0 "Gain" = 3.0 } }
                    { type = builtin name = peq3L label = bq_peaking control = { "Freq" = 400.0 "Q" = 4.6 "Gain" = 4.0 } }
                    # ... (R channel PEQ bells similarly)
                    # Multiband compressor (via LADSPA ZaMultiCompX2)
                    # {
                    #     type  = ladspa
                    #     name  = mbcomp
                    #     plugin = "ZaMultiCompX2-ladspa"
                    #     label  = ZaMultiCompX2
                    #     control = { ... }
                    # }
                ]
                links = [
                    { output = "convL:Out"  input = "hpL:In" }
                    { output = "convR:Out"  input = "hpR:In" }
                    { output = "hpL:Out"    input = "peq1L:In" }
                    { output = "peq1L:Out"  input = "peq2L:In" }
                    { output = "peq2L:Out"  input = "peq3L:In" }
                    # ... chain continues through compressor, limiter, autogain
                ]
            }
            audio.channels = 2
            audio.position = [ FL FR ]
            capture.props = {
                node.name   = "effect_input.dolby"
                media.class = Audio/Sink
            }
            playback.props = {
                node.name   = "effect_output.dolby"
                node.passive = true
            }
        }
    }
]
```

### Benefits over EasyEffects

- **No GUI process** — runs as a PipeWire module, no GTK/GLib overhead
- **Headless operation** — works on servers, in containers, or over SSH
- **Startup via systemd** — `pipewire -c filter-chain.conf` as a user service,
  or drop config into `~/.config/pipewire/filter-chain.conf.d/`
- **Lower memory** — no LV2 host, no UI toolkit
- **Same audio quality** — uses the same SPA DSP primitives that EasyEffects uses

### Limitations

- No GUI for real-time parameter tweaking (but parameters are static anyway)
- LADSPA/LV2 multiband compressor plugins may not map 1:1 to the EasyEffects
  LSP multiband compressor (different parameter semantics)
- Need to manually wire the filter-chain sink as default for the speaker output
  (WirePlumber rules or `pw-metadata`)

### Integration approach

The script could gain a `--pipewire-filter-chain` option that generates the
complete `.conf` file instead of / in addition to EasyEffects presets.

### Working example

[taprobane99/Lenovo-Yoga-Slim-7x-Dolby-Linux-Audio](https://github.com/taprobane99/Lenovo-Yoga-Slim-7x-Dolby-Linux-Audio)
hand-converted this script's `Dolby-Music-Balanced.json` output into a working
PipeWire `filter-chain` config (`99-dolby-music.conf`) using LSP LV2 plugins
(`mb_compressor_stereo`, `limiter_stereo`) and the same `.irs` files. Two patterns
worth borrowing if anyone implements `--pipewire-filter-chain`:

- **4-speaker upmix from a stereo source** (the Yoga Slim 7x has four speakers
  the EasyEffects path can't drive). Their output node declares
  `audio.channels = 4` with `audio.position = [ FL FR RL RR ]`, the post-MBC
  stereo signal feeds a `limiter_f` (front pair) and a `limiter_r` (rear pair)
  in parallel, and the final output exposes all four `limiter_{f,r}:out_{l,r}`
  ports. This duplicates the stereo image to both pairs while letting front
  and rear limiters run with independent gain reduction. Not Dolby-faithful
  (no surround/height virtualization — see `cross-device-findings.md` §14)
  but materially better than mono-summing or relying on the kernel mixer.
- **Bankstown LV2 bass exciter** (`https://chadmed.au/bankstown`, also used by
  [AsahiLinux/asahi-audio](https://github.com/AsahiLinux/asahi-audio)) inserted
  before the convolver. A reasonable LV2-land substitute for Dolby's
  `bass-extraction` block, which is universally `enable=0` in the corpus
  (cross-device-findings.md §14) but matters for laptops with small drivers.

The MBC band parameters they encode in the `.conf` (`cr_0=3.938, sf_1=469.0,
al_0=0.56234`, etc., reading as ratio / split-frequency / linear-threshold
`10^(dB/20)`) round-trip exactly with this script's JSON output as of the
4-decimal precision fix in commit `6e72dd0`.

### Companion converter — design sketch

Not built. Captured here so the analysis doesn't have to be redone if anyone
later wants the conversion automated. Recommendation: **a sibling
`ee_to_pipewire.py` at the repo root** (matches the existing single-file
convention; no new `tools/` directory needed) that reads an existing
EasyEffects `.json` preset plus its matching `.irs`, and emits a PipeWire
`.conf`. Main script untouched, so future precision/feature fixes stay
single-target.

**Why a separate tool, not a `--pipewire-filter-chain` flag in the main
script:** doubling the emit surface inside `dolby_to_easyeffects.py` means
every future precision/feature fix has to land twice, with a silent-divergence
risk. A separate tool keeps that cost at zero for users on the EasyEffects
path (the majority).

**Per-param translation** (verified against taprobane99's `99-dolby-music.conf`
— values round-trip with this script's 4-decimal-precision JSON since
commit `6e72dd0`):

| EE param                       | PW LSP-LV2 param | Conversion             |
|--------------------------------|------------------|------------------------|
| `attack-threshold` (dB)        | `al_N`           | `10**(dB/20)` (linear) |
| `release-threshold` (dB)       | `rrl_N`          | `10**(dB/20)`          |
| `makeup` (dB)                  | `mk_N`           | `10**(dB/20)`          |
| `knee` (dB, ≤0)                | `kn_N`           | `10**(dB/20)` — note `0 dB → 1.0`, not 0 |
| `ratio`                        | `cr_N`           | identity               |
| `attack-time` (ms)             | `at_N`           | identity               |
| `release-time` (ms)            | `rt_N`           | identity               |
| `split-frequency` (Hz)         | `sf_N`           | identity               |
| `enable-band` (bool)           | `cbe_N`          | 1 / 0                  |
| `compressor-enable` (bool)     | `ce_N`           | 1 / 0                  |
| `sidechain-mode` `RMS`/`Peak`  | `scm_N`          | 1 / 0                  |
| `sidechain-lookahead` (ms)     | `sla_N`          | identity               |

**Per-stage plugin URI mapping** — one PW node per EE plugin, except as
noted:

| EE plugin key             | PW plugin                                                                     |
|---------------------------|-------------------------------------------------------------------------------|
| `convolver#0`             | `type=builtin label=convolver` × 2 (mono — one per channel)                  |
| `bass_enhancer#0`         | Calf `BassEnhancer` or Bankstown (taprobane99 uses Bankstown — pick one)    |
| `stereo_tools#0`          | Calf `http://calf.sourceforge.net/plugins/StereoTools`                        |
| `equalizer#0` (PEQ)       | LSP `http://lsp-plug.in/plugins/lv2/para_equalizer_x16_stereo`                |
| `equalizer#1` (dialog)    | Same plugin as PEQ — disambiguate by **position in `plugins_order`**          |
| `autogain#0`              | LSP `autogain_stereo` — **skip entirely if `bypass: true`** (HDA default)     |
| `multiband_compressor#0`  | LSP `mb_compressor_stereo`                                                    |
| `multiband_compressor#1`  | Same plugin, regulator-tuned params                                           |
| `limiter#0`               | LSP `limiter_stereo`                                                          |

**Six gotchas to flag before writing any code:**

- **Bypassed plugins**: skip them entirely (don't emit as a node-and-link
  pair with `bypass=true`). The autogain stage in particular is
  `bypass: true` by HDA-default design — see `make_autogain` docstring at
  `dolby_to_easyeffects.py:1694`.
- **Mono convolver**: PW's builtin `convolver` is one channel per node, so
  `convolver#0` in EE expands to two PW nodes (`conv_l`, `conv_r`) wired in
  parallel. The only place the link generator needs special-casing.
- **`volmax_boost` lives in two possible places**: `make_preset` lines
  2246–2256 puts it on the regulator's `output-gain` if the regulator is
  present, otherwise on the limiter's `input-gain`. Reader must check both.
- **Dialog-vs-PEQ disambiguation is positional only** — both are `equalizer#N`
  with the same dict shape. Assert `equalizer#1` follows `equalizer#0` in
  `plugins_order` to fail loud if a future change reorders them.
- **PEQ filter-type enum mismatch**: EE writes string `type` values
  (`"Bell"`, `"Hi-shelf"`, `"Lo-pass"`, `"Hi-pass"`); LSP
  `para_equalizer_x16` `t_N` uses integer enums. Need a small lookup
  table — easy to mis-map. Reference: [LSP para_equalizer manual](https://lsp-plug.in/?page=manuals&section=para_equalizer_x16_stereo).
- **Absolute IRS path**: PW builtin convolver `config.filename` needs an
  absolute path. Resolve `<kernel-name>.irs` against `--irs-dir` (default
  `~/.local/share/easyeffects/irs/`) and bake the absolute path into the
  conf. The conf becomes stale if the IRS moves — worth a comment in the
  conf header.

**Out of scope (intentionally):**
- WirePlumber routing rules (auto-set the new sink as default).
- 4-channel upmix (the Yoga Slim 7x case — would need speaker-layout info
  not present in the EE JSON).
- Disabling EE autoload to avoid double-processing — print a "next steps"
  block at the end of the converter run instead.

**Rough size**: ~350–500 LOC (9 stage emitters × ~25 LOC + SPA-JSON
formatter ~30 + JSON parser ~80 + link generator ~60 + CLI ~50). Well under
a full `--pipewire-filter-chain` integration in the main script, which would
have to mirror every `make_*` function permanently.

**Verification when implemented**: load conf into PW, A/B against the
EasyEffects-processed sink with reference content, and round-trip the LSP
MBC `al_N`/`mk_N` linear values back to dB to confirm they match the source
JSON's `attack-threshold`/`makeup` to within 4 decimals.

## Option 4: Hybrid — SOF DSP for PEQ + filter-chain for the rest

**Status: best practical tradeoff**

Combine options 1 and 3:

```
                     ┌──── Intel DSP (zero CPU) ────┐
Audio → filter-chain │                              │ → Speaker
  Convolver (FIR)    │  HP filter + PEQ bells       │
  MB Compressor      │  (EQIIR2.0 byte control)     │
  Regulator          │                              │
  Autogain           └──────────────────────────────┘
```

- The speaker PEQ runs on the DSP via `EQIIR2.0` — always active, zero CPU
- The convolver + dynamics run in a PipeWire filter-chain — lightweight, no GUI
- EasyEffects is not needed at all

This splits the pipeline at the natural boundary: the PEQ is
hardware/speaker-specific correction (analogous to Dolby's VLLDP path), while
the rest is content-dependent processing (analogous to the CP path).

## Option 5: GPU compute (Intel Iris Xe)

**Status: not practical for audio**

This system has an Intel Iris Xe GPU with Vulkan (mesa 26.0) and OpenCL support.
GPU-based FFT convolution is theoretically possible — the 4096-tap FIR is a
natural fit for parallel compute. However:

- **Latency**: CPU→GPU→CPU round-trip adds 1–5 ms, unacceptable for real-time
  audio at low buffer sizes
- **No framework**: no existing Linux audio pipeline supports GPU offload
- **Overkill**: the FIR convolver at 4096 taps × 48 kHz uses <0.1% of a single
  CPU core. The entire EasyEffects pipeline uses ~1–2% CPU. There is no CPU
  pressure to solve.
- **Power**: waking the GPU for audio processing would use more power than the
  CPU path

Not worth pursuing.

## Summary

| Option                        | Offloads                           | CPU savings                    | Effort    | Risk |
|-------------------------------|------------------------------------|--------------------------------|-----------|------|
| **1. SOF IIR EQ**             | PEQ only                           | ~5% of pipeline                | Low       | Low  |
| **2. Custom SOF topology**    | PEQ + FIR + compressor             | ~80% of pipeline               | Very high | High |
| **3. PipeWire filter-chain**  | Replaces EasyEffects               | Same CPU, less overhead        | Medium    | Low  |
| **4. Hybrid (1 + 3)**         | PEQ on DSP, rest in filter-chain   | ~5% DSP + less overhead        | Medium    | Low  |
| **5. GPU compute**            | FIR convolver                      | Negligible                     | High      | High |

**Recommended path**: Option 4 (hybrid) gives the best tradeoff — the PEQ runs
on dedicated hardware where it belongs, the rest runs in a lightweight
filter-chain without the EasyEffects GUI, and the whole thing can be generated
by the script.
