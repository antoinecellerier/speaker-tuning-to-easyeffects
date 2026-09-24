# Alternative pipelines

Design sketches for replacing or offloading parts of the Dolby effects pipeline
beyond the default EasyEffects path, to reduce CPU/memory overhead, enable
headless use, or move processing onto dedicated hardware.

Development system for the notes below: ThinkPad X1 Yoga Gen 7, Intel Alder
Lake-P, Realtek ALC287, SOF firmware (`sof-hda-dsp`), PipeWire 1.4.10.

## Current pipeline (all software, via EasyEffects)

```
Audio → Convolver → [Bass Enh.] → Equalizer → Dialog EQ
          IEQ+AO    SoundWire     HP + PEQ    speech bell

      → Autogain → MB Compressor → Regulator → Limiter → Speaker
        leveler      dynamics      per-band    brickwall
        (bypassed)                 limiter     −1 dBFS
```

A `Stereo Tools` M/S widener mapped from `surround-boost` sat after the bass
enhancer until its removal in 2026-06. DAX applies no stereo widening on 2-ch
content. See the
[surround→stereo-base factor](research/adaptive-processing.md#r-surround-boost-stereo-base).

`docs/design-notes.md` "Plugin chain order" has the rationale for the stage
order. The bass enhancer is emitted only for SoundWire devices, and autogain
ships bypassed. The stages run inside the EasyEffects process, which sits in the
PipeWire graph as a filter node. All are LV2 plugins except autogain, which is
EE-native (libebur128). `ee_to_pipewire.py` translates a non-bypassed autogain
to LSP `autogain_stereo`, the LV2 loudness-AGC equivalent, so it can reproduce
every stage of the EE chain.

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

The speaker PEQ from the vlldp section maps directly to IIR biquad sections. It
is a 4th-order highpass at 100 Hz plus 3 bell filters per channel:

| Filter                   | Biquad sections           |
|--------------------------|---------------------------|
| 4th-order HP @ 100 Hz    | 2 (cascaded 2nd-order)    |
| 3 bells × 2 channels     | 6                         |
| **Total**                | **8 biquads**             |

At ~24 bytes per biquad section plus header overhead, this fits comfortably in
the 1024-byte blob.

### What cannot run on it

- **FIR convolver** (IEQ + audio-optimizer): the loaded topology has no FIR EQ
  component. The IEQ target curve requires FIR for accurate reproduction. The
  best biquad fits measured ~11–16 dB peak and ~1.6–2 dB RMS error against the
  20-band composite target. The comparison table is in
  [`design-notes.md`](design-notes.md), "Rejected approaches → Parametric-EQ
  approximation".
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

- **Zero CPU cost**: runs on the DSP's Xtensa cores.
- **Lowest possible latency**: processes audio before it leaves the DSP
  pipeline, with no PipeWire graph hop.
- **Always active**: it applies at the ALSA/SOF level, so it works even without
  EasyEffects or PipeWire running.
- **Headphone-safe**: the PEQ is speaker-specific. On the DSP it applies only to
  the HDA analog output, not to Bluetooth or USB audio.

### Integration approach

The script could gain a `--sof-peq` option that:

1. Computes biquad coefficients for the HP + bell filters, which preset
   generation already calculates.
2. Packs them into the SOF IIR blob format.
3. Writes via `amixer cset numid=39`.
4. Removes the corresponding EQ stage from the EasyEffects preset, so it is not
   applied twice.

The remaining EasyEffects preset would drop only the offloaded speaker-PEQ
equalizer. It would keep every other stage: convolver, stereo tools, dialog EQ,
autogain, MBC, regulator and limiter, plus the bass enhancer on SoundWire.

## Option 2: Custom SOF topology with FIR EQ and DRC

**Status: advanced, requires building a custom topology**

The SOF firmware for Alder Lake (`sof-adl.ri`) includes `eq_iir`. The current
signed firmware binary does not appear to include the `eq_fir`, `drc`,
`crossover` or `multiband_drc` modules: zero references were found in the
binary. The community (unsigned) firmware build may include them, or they could
be compiled in from source.

If a firmware with these modules were available, a custom ALSA topology
(`sof-hda-generic.tplg`) could chain:

```
Host PCM → EQ FIR → EQ IIR → DRC → DAI (codec)
           IEQ+AO    PEQ  compressor
```

This would offload the convolver, PEQ and compressor to the DSP, and leave only
the regulator and autogain in software. The drawbacks:

- Building custom topologies requires `alsatplg` or SOF's topology2 tools.
- The signed firmware may refuse to load custom topologies without matching
  signatures.
- Firmware-level bugs would be much harder to debug than EasyEffects plugin
  issues.
- Each SOF/kernel update could require topology rebuilds.

This option is the most complete offload, but also the highest-effort and
highest-risk.

## Option 3: PipeWire filter-chain (lightweight EasyEffects replacement)

> **Shipped as [`ee_to_pipewire.py`](ee-to-pipewire.md), which departs from
> this sketch in three ways.** It writes to `pipewire.conf.d/`, not
> `filter-chain.conf.d/`. It routes as a WirePlumber smart filter on the
> speaker sink, and keeps the skeleton's plain `Audio/Sink` virtual sink only
> as its `--target-sink ''` fallback. It is a separate tool, not a
> `--pipewire-filter-chain` flag: see "Companion converter" below. The
> skeleton still fits the standalone `pipewire -c filter-chain.conf` route,
> which filter-chain.service runs.

**Status: fully feasible with existing packages**

PipeWire's `filter-chain` module can replicate the entire effects pipeline
without the EasyEffects GTK process. It uses the same underlying PipeWire graph
nodes, with a lighter footprint and no GUI.

### Available processing blocks

Already installed on the development system:

| Pipeline stage       | filter-chain implementation                               |
|----------------------|-----------------------------------------------------------|
| FIR convolver        | `builtin` type, `convolver` label — reads the same `.irs` WAV files |
| HP + PEQ bells       | `builtin` types: `bq_highpass`, `bq_peaking`              |
| Multiband compressor | LADSPA: `ZaMultiCompX2-ladspa.so` (Zam plugins)           |
| Limiter / maximizer  | LADSPA: `ZaMaximX2-ladspa.so`                             |
| Autogain / loudness  | SPA plugin: `libspa-filter-graph-plugin-ebur128.so`       |

LV2 plugins from the LSP suite are also available, such as
`mb_compressor`, `sc_mb_limiter_stereo` and `autogain_stereo` for loudness.
LADSPA is simpler for filter-chain configs. `ee_to_pipewire.py` takes the LV2
route: it uses LSP `autogain_stereo` rather than the `ebur128` SPA plugin for
the volume leveler.

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
                    # FIR convolver (IEQ + audio-optimizer). The generator names
                    # each impulse after its contents (Dolby-Balanced-<8 hex>.irs)
                    # and removes the previous one — point at the file it wrote,
                    # and re-point after a regeneration that changed the sound.
                    {
                        type   = builtin
                        name   = convL
                        label  = convolver
                        config = { filename = "~/.local/share/easyeffects/irs/Dolby-Balanced-<hash>.irs" channel = 0 }
                    }
                    {
                        type   = builtin
                        name   = convR
                        label  = convolver
                        config = { filename = "~/.local/share/easyeffects/irs/Dolby-Balanced-<hash>.irs" channel = 1 }
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

- **No GUI process**: runs as a PipeWire module, no GTK/GLib overhead.
- **Headless operation**: works on servers, in containers, or over SSH.
- **Startup via systemd**: run `pipewire -c filter-chain.conf` as a user
  service, or drop the config into `~/.config/pipewire/filter-chain.conf.d/`.
- **Lower memory**: no LV2 host, no UI toolkit.
- **Same audio quality**: uses the same SPA DSP primitives that EasyEffects
  uses.

### Limitations

- No GUI for real-time parameter tweaking. The parameters are static anyway.
- LADSPA/LV2 multiband compressor plugins may not map 1:1 to the EasyEffects
  LSP multiband compressor. Their parameter semantics differ.
- The filter-chain sink needs manual wiring as default for the speaker output,
  via WirePlumber rules or `pw-metadata`.

### Integration approach

The script could gain a `--pipewire-filter-chain` option that generates the
complete `.conf` file instead of, or in addition to, EasyEffects presets.

### Working example

[taprobane99/Lenovo-Yoga-Slim-7x-Dolby-Linux-Audio](https://github.com/taprobane99/Lenovo-Yoga-Slim-7x-Dolby-Linux-Audio)
hand-converted this script's `Dolby-Music-Balanced.json` output into a working
PipeWire `filter-chain` config, `99-dolby-music.conf`. It uses LSP LV2 plugins
(`mb_compressor_stereo`, `limiter_stereo`) and the same `.irs` files. Two
patterns are worth borrowing if anyone implements `--pipewire-filter-chain`:

- **4-speaker upmix from a stereo source.** The Yoga Slim 7x has four speakers
  the EasyEffects path can't drive. Their output node declares
  `audio.channels = 4` with `audio.position = [ FL FR RL RR ]`. The post-MBC
  stereo signal feeds a `limiter_f` (front pair) and a `limiter_r` (rear pair)
  in parallel. The final output exposes all four `limiter_{f,r}:out_{l,r}`
  ports. This duplicates the stereo image to both pairs, while front and rear
  limiters run with independent gain reduction. It is not Dolby-faithful: it
  has no surround/height virtualization (`cross-device-findings.md` §14). It is
  still materially better than mono-summing or relying on the kernel mixer.
- **Bankstown LV2 bass exciter**, inserted before the convolver. It lives at
  `https://chadmed.au/bankstown`, and
  [AsahiLinux/asahi-audio](https://github.com/AsahiLinux/asahi-audio) also uses
  it. It is a reasonable LV2-land substitute for Dolby's `bass-extraction`
  block. That block is universally `enable=0` in the corpus
  (cross-device-findings.md §14) but matters for laptops with small drivers.

The MBC band parameters they encode in the `.conf` round-trip exactly with this
script's JSON output as of the 4-decimal precision fix in commit `6e72dd0`.
Examples are `cr_0=3.938, sf_1=469.0, al_0=0.56234`, which read as ratio,
split-frequency and linear-threshold `10^(dB/20)`.

### Companion converter

[`docs/ee-to-pipewire.md`](ee-to-pipewire.md) holds the current architecture,
plugin coverage, equivalence guarantees and per-control translation tables for
`ee_to_pipewire.py`. The rationale for a separate tool stays here, with this
file's broader "alternative pipelines" trade-off discussion.

**Why a separate tool, not a `--pipewire-filter-chain` flag in the main
script:** doubling the emit surface inside `dolby_to_easyeffects.py` means
every future precision/feature fix has to land twice, with a silent-divergence
risk. A separate tool keeps that cost at zero for users on the EasyEffects
path, who are the majority.

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

- The speaker PEQ runs on the DSP via `EQIIR2.0`: always active, zero CPU.
- The convolver + dynamics run in a PipeWire filter-chain: lightweight, no GUI.
- EasyEffects is not needed at all.

This splits the pipeline at the natural boundary. The PEQ is
hardware/speaker-specific correction, analogous to Dolby's VLLDP path. The rest
is content-dependent processing, analogous to the CP path.

## Option 5: GPU compute (Intel Iris Xe)

**Status: not practical for audio**

This system has an Intel Iris Xe GPU with Vulkan (mesa 26.0) and OpenCL support.
GPU-based FFT convolution is theoretically possible, and the 4096-tap FIR is a
natural fit for parallel compute. The drawbacks:

- **Latency**: the CPU→GPU→CPU round-trip adds 1–5 ms, unacceptable for
  real-time audio at low buffer sizes.
- **No framework**: no existing Linux audio pipeline supports GPU offload.
- **Overkill**: the FIR convolver at 4096 taps × 48 kHz uses <0.1% of a single
  CPU core. The entire EasyEffects pipeline uses ~1–2% CPU. There is no CPU
  pressure to solve.
- **Power**: waking the GPU for audio processing would use more power than the
  CPU path.

Not worth pursuing.

## Summary

| Option                    | Offloads                           | CPU savings                    | Effort    | Risk |
|---------------------------|------------------------------------|--------------------------------|-----------|------|
| 1. SOF IIR EQ             | PEQ only                           | ~5% of pipeline                | Low       | Low  |
| 2. Custom SOF topology    | PEQ + FIR + compressor             | ~80% of pipeline               | Very high | High |
| 3. PipeWire filter-chain  | Replaces EasyEffects               | Same CPU, less overhead        | Medium    | Low  |
| 4. Hybrid (1 + 3)         | PEQ on DSP, rest in filter-chain   | ~5% DSP + less overhead        | Medium    | Low  |
| 5. GPU compute            | FIR convolver                      | Negligible                     | High      | High |

**Recommended path**: Option 4 (hybrid) gives the best tradeoff. The PEQ runs on
dedicated hardware where it belongs. The rest runs in a lightweight filter-chain
without the EasyEffects GUI. The whole thing can be generated by the script.
