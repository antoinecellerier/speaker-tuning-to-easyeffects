# measure_dax — capture and compare Dolby DAX3's actual response

These tools measure DAX3's per-channel response on Windows via WASAPI loopback.
They then deconvolve / spectrum-analyze it on Linux and compare it against the
FIR our converter generates from the same XML. This reproduces the empirical
comparison in
[docs/design-notes.md](../../docs/design-notes.md#empirical-comparison-vs-dax3-on-windows)
("Empirical comparison vs DAX3").

[`tools/measure_ee/`](../measure_ee/) is the Linux-side counterpart. It runs
the same stimuli through a live EasyEffects instance, so you can overlay EE's
actual response on top of DAX's.

## Quick start

```sh
# Pick a working directory for stimuli + captures.
mkdir -p ~/dax-measure && cd ~/dax-measure

# Generate the 5 stimuli + matching inverse filter for the sweeps.
python /path/to/repo/tools/measure_dax/make_stimulus.py
```

`make_stimulus.py` writes `stimulus_*.{wav,json}` and `inverse_sweep.npy` into
the current directory. Copy the `stimulus_*` files plus `capture_dax.py` to a
Windows machine, and run the captures with the procedure below. Copy the
`loopback_*.{wav,json}` back, then run:

```sh
python /path/to/repo/tools/measure_dax/analyze.py captures/loopback_*.wav \
    --xml /path/to/DEV_xxxx_SUBSYS_xxxxxxxx.xml \
    --profile dynamic --curve balanced
```

`analyze.py` finds the inverse filter and stimulus files without flags, as
long as you keep the artifacts together. It searches cwd, then the capture's
directory, then the script directory.

## Stimulus suite

| stimulus | level | what it probes |
|---|---|---|
| `stimulus_sweep.wav` | −18 dBFS peak | exponential 20 Hz–22 kHz sweep. Recovers an LTI IR if the system is LTI |
| `stimulus_sweep_quiet.wav` | −42 dBFS peak | same sweep at lower input. Does the leveler engage less? In practice, no |
| `stimulus_pink.wav` | −18 dBFS RMS | steady-state magnitude after the leveler settles |
| `stimulus_pink_quiet.wav` | −42 dBFS RMS | pink noise at low input level |
| `stimulus_multitone.wav` | −18 dBFS RMS | 20 pure tones at Dolby band centers. Per-band amplitude + phase via single-bin DFT |
| `stimulus_stepped.wav` | −18 dBFS peak | one held tone per probe frequency, 39 frequencies: the 20 band centers + the midpoint between each pair. The whole grid is replayed ascending / descending / shuffled. Per-frequency steady-state amplitude via single-bin DFT. The cross-pass mean is the static EQ. The cross-pass span is the order-dependent adaptive dynamics. |
| `stimulus_stepped_quiet.wav` | −42 dBFS peak | same, low input. Brackets the level-dependent treble gain |
| `stimulus_pink{60,48,30,24,14}.wav` | −60 … −14 dBFS RMS | the leveler ladder. With `pink` and `pink_quiet` these are seven rungs of one curve, DAX-on minus DAX-off at each input level. −14 is the loud end pink can reach without clipping, given its ~13 dB crest factor. Use `stimulus_stepped_loud.wav` above that |

DAX3 is non-LTI. The first round of captures, a sweep at −18 dBFS only, showed
the leveler / regulator engaging during the sweep and contaminating the
deconvolved IR. The pink and multitone stimuli are designed to give the leveler
something stationary to settle on. That isolates the steady-state EQ from the
time-varying dynamics.

The stepped-sine goes one step further and separates the two in a single
capture. The same grid is replayed in different orders. The part of each
tone's response that is invariant across passes is the static EQ. The part
that shifts with what preceded it is the adaptive processing. It also samples
*between* the band centers, which band-center-only pink/multitone can't. That
matters for issue #13, because linear-vs-PCHIP interpolation is only
distinguishable between bands.

## End-to-end flow

```
[Linux]                          [Windows]                    [Linux]
make_stimulus.py   ─copy──▶   capture_dax.py    ─copy──▶   analyze.py
  ↓                              ↓                              ↓
stimulus_*.wav (5)            loopback_<kind>_<label>.wav    ir_*.wav (sweep)
inverse_sweep.npy             + .json sidecar                spectrum_*.npz (pink)
stimulus_*.json (5)                                          tones_*.npz (multitone)
                                                             compare_*.png
                                                             analysis_*.txt
```

## 0. One-time Linux setup

```sh
mkdir -p ~/dax-measure && cd ~/dax-measure
python /path/to/repo/tools/measure_dax/make_stimulus.py
```

This produces all 5 stimuli + per-stimulus meta JSON + the shared
`inverse_sweep.npy` in the current directory. The output is deterministic, so
re-run it only if you change parameters at the top of the script.

## 1. Copy to Windows

The Windows side needs:

- `capture_dax.py`
- each stimulus you intend to play, plus **its `.json` sidecar**. The analyzer
  reads the sidecar for the stimulus geometry, so a `.wav` on its own is not
  analysable.
- [`CLAUDE_WINDOWS.md`](CLAUDE_WINDOWS.md), only if Claude Code will be
  helping on Windows.

`make_stimulus.py` writes the whole suite, ~250 MB in total. Copy the subset
your question needs rather than all of it:

| question | stimuli to copy | size |
|---|---|---|
| Steady-state EQ, the usual starting point | `stimulus_sweep[_quiet]`, `stimulus_pink[_quiet]`, `stimulus_multitone` | ~22 MB |
| Anything level-dependent: does a stage's gain change with input level? | `stimulus_stepped`, `stimulus_stepped_quiet`, `stimulus_stepped_loud`: the same 39-frequency grid at −18 / −42 / −2 dBFS | ~180 MB |
| Bass-specific behaviour | `stimulus_bass_burst[_quiet]`: sustained 50/80/120/180 Hz bursts | ~10 MB |
| Stereo width | `stimulus_stereo_pink`, `stimulus_stereo_correlated` | ~10 MB |
| Leveler behaviour on real content | `stimulus_speech` | ~5 MB |

`analyze.py` handles `sweep`, `pink` and its two `stereo_*` variants,
`speech`, `multitone` and `stepped`. `bass_burst` has no analyzer branch yet.
Capture it if asked, but its analysis is ad-hoc.

## 2. One-time Windows setup

In an admin PowerShell or cmd:

```powershell
pip install sounddevice numpy scipy soundfile pycaw comtypes pyaudiowpatch
```

`pyaudiowpatch` is the active backend on Win11. `sounddevice`'s WasapiSettings
doesn't expose a `loopback` kwarg in any released version, so `capture_dax.py`
falls back automatically.

Lock the speaker endpoint format to **48 kHz**. Any bit depth works, 16 or
24-bit, because the loopback taps the float32 mix bus pre-quantize. Set it here:

> Settings → System → Sound → All sound devices → [your speakers] →
> Output settings → Format → any 48000 Hz entry

The script verifies the rate and aborts if wrong.

## 3. Capture procedure (Windows)

Run all five stimuli per profile in one go. Toggle the profile in the Dolby
Access GUI between profile groups:

```powershell
# off baseline (Dolby Atmos toggled OFF in Dolby Access)
foreach ($s in 'sweep','sweep_quiet','pink','pink_quiet','multitone','stepped','stepped_quiet') {
    python capture_dax.py --stimulus stimulus_$s.wav --label off
}

# Then for each Dolby profile (toggle in Dolby Access UI between blocks):
foreach ($s in 'sweep','sweep_quiet','pink','pink_quiet','multitone','stepped','stepped_quiet') {
    python capture_dax.py --stimulus stimulus_$s.wav --label dynamic
}
# … repeat for movie, music, game, voice
```

7 stimuli × 6 profile labels = 42 captures. Each records its stimulus plus
1.5 s. That is ~6.7 min per profile and ~40 min of pure capture time, plus
manual profile-toggle time. Plan more than 40 min on Windows.

Each capture produces `captures/loopback_<kind>_<label>.wav` plus a matching
`.json` sidecar in schema v1. The sidecar records everything that was active
at capture time: endpoint, format, Dolby spatial mode, package version map,
capture levels, similarity-to-OFF score, and the full stimulus meta.

## 4. Copy back to Linux

```sh
# from the Windows machine
scp captures/loopback_*.* user@linux:~/dax-measure/captures/
```

## 5. Linux-side analysis

```sh
cd ~/dax-measure
python /path/to/repo/tools/measure_dax/analyze.py captures/loopback_*.wav \
    --xml /path/to/DEV_xxxx_SUBSYS_xxxxxxxx.xml \
    --profile dynamic --curve balanced
```

`analyze.py` reads each capture's sidecar to determine the stimulus kind, then
dispatches:

- **kind=sweep** → Farina deconvolution → `ir_sweep_<label>_{L,R}.wav`. The
  output is an 8192-sample IR, centered on its peak with 2048 samples of
  pre-peak context, peak-normalized 32-bit float.
- **kind=pink** → Welch-style averaged PSD over the analysis window, divided
  by the same window of the stimulus to recover the steady-state EQ. The
  window defaults to 6–11 s into the capture. Result in
  `spectrum_<kind>_<label>_<channel>.npz`.
- **kind=speech** → the same pink analysis. Pauses and modulation cancel in
  the ratio, because the stimulus and the capture carry the same envelope.
- **kind=stereo_pink** and **kind=stereo_correlated_pink** → the same pink
  analysis, plus the side/mid widening transfer: the capture's S/M minus the
  stimulus's S/M. The `.npz` carries it as `sm_delta_db`, and the summary
  prints its median over 200 Hz–18 kHz.
- **kind=multitone** → single-bin DFT at each of the 20 band-center
  frequencies. It recovers per-band amplitude *and phase*, subtracting the
  known stimulus phase. Result in `tones_<kind>_<label>_<channel>.npz`.
- **kind=stepped** → single-bin DFT amplitude of each held tone, read after a
  settle skip and grouped by pass. Per probe frequency, the cross-pass mean is
  the static EQ and the cross-pass span is the adaptive dynamics. Result in
  `stepped_<kind>_<label>_<channel>.npz`.

With `--xml`, it also writes a
`compare_<basename>_<profile>_<curve>_<channel>.png` and a textual residual
table.

## What the results mean

After a run on the OFF + Dynamic captures, these are the most informative
views.

**Phase character**, sweep only, in `analysis_*.txt`:
- "minimum-phase (post-peak energy dominates)" → DAX3 IR concentrates energy
  after the peak, like our generated FIR. Phase choice matches.
- "linear-phase or symmetric (post ≈ pre)" → DAX3 uses linear-phase. Our
  converter trades phase accuracy for ~43 ms latency reduction. This finding
  doesn't motivate a code change, because the no-added-latency constraint
  applies regardless. It does answer the open question.
- "hybrid (post > pre)" → mixed-phase or non-LTI artifact.

**Multitone phase column**, in `compare_loopback_multitone_<label>_..._L.png`:
- For an LTI system, the per-band phases should form a smooth curve vs
  frequency. For a minimum-phase FIR, accumulated phase grows with frequency
  in a feature-driven way. For linear-phase, it's `−omega*N/2`.
- Wildly noisy phases per band → non-LTI processing, such as the volume
  leveler or regulator, modulates phase content-adaptively.

**Pink-noise EQ recovery**, in `compare_loopback_pink_<label>_..._L.png`:
- The cleanest steady-state magnitude readout. Once the leveler has settled,
  after ~6 s, the captured-vs-stimulus dB ratio per bin recovers the active EQ
  shape.
- Compare the recovered EQ curve to the FIR target. If they match within
  ~1 dB, our converter's curve is what DAX3 applies. If not, DAX3 is doing
  something we're not modeling.

**Sweep level swap**, with `stimulus_sweep_quiet.wav`:
- If the −42 dBFS sweep has a similar non-LTI signature to the −18 dBFS one, the
  leveler is independent of input level: it targets a fixed loudness regardless
  of input. If the quiet version is more LTI-like, the leveler engages more
  aggressively at moderate levels.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `endpoint default samplerate is 44100 Hz` | Output format not locked. Pick any 48 kHz entry in Sound settings. |
| `No Dolby installation detected` | Wrong endpoint, perhaps HDMI or a dock, or DAX3 not installed. Pass `--no-apo-check` if you can verify manually that Atmos is the active spatial mode. |
| `Dolby Access shows X but you passed Y` | Toggle the right profile in Dolby Access GUI. The check substring-matches an undocumented format, so false positives are possible. Say "y" if you're sure. |
| `capture is essentially identical to the OFF baseline` | Forgot to switch DAX3 on, or forgot to switch profile. The script flags > 0.98 cross-correlation. |
| OFF-baseline check fails with sidelobes > −25 dB on sweep | Some other APO is active, such as Realtek "Audio Effects" or "Loudness Equalization". Disable it in Sound → Properties → Enhancements. |
| `endpoint is muted — capture will be silent` | Unmute in Volume Mixer and re-run. |

## Limitations

- **No automated profile toggle.** Dolby Access has no public API. We switch
  profiles in its GUI between captures.
- **L=R sweep only.** It recovers DAX3's diagonal IR. Cross-channel
  processing, such as the surround virtualizer or dialog steering, shows up
  only as the side/mid magnitude change the `stereo_*` probes measure. No
  cross-channel IR is measured. If needed, do separate L-only and R-only sweeps
  as a follow-up.
- **Speaker-endpoint dependent.** The captured IR is what DAX3 does to the
  digital signal. It doesn't include the physical speaker's response, which
  loopback can't measure.

## Prior art

[`shuhaowu/linux-thinkpad-speaker-improvements`](https://github.com/shuhaowu/linux-thinkpad-speaker-improvements)
uses the same basic technique: WASAPI loopback of a stimulus through DAX3. It
uses a single dirac impulse and manual Audacity capture. We use a swept sine
plus stationary stimuli for ~40 dB better SNR. We also script the entire
Windows-side flow, and correlate per-stimulus baselines so the "I forgot to
switch profile" mistake is caught automatically.
