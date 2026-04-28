# measure_dax — capture and compare Dolby DAX3's actual response

Measure DAX3's per-channel response on Windows via WASAPI loopback, then
deconvolve / spectrum-analyze on Linux and compare against the FIR our
converter generates from the same XML. Reproduces the empirical
comparison in `docs/design-notes.md` ("Empirical comparison vs DAX3").

## Quick start

```sh
# Pick a working directory for stimuli + captures.
mkdir -p ~/dax-measure && cd ~/dax-measure

# Generate the 5 stimuli + matching inverse filter for the sweeps.
python /path/to/repo/tools/measure_dax/make_stimulus.py
```

`make_stimulus.py` writes `stimulus_*.{wav,json}` and
`inverse_sweep.npy` into the current directory. Copy the `stimulus_*`
files plus `capture_dax.py` to a Windows machine, run the captures
(procedure below), copy the `loopback_*.{wav,json}` back, then:

```sh
python /path/to/repo/tools/measure_dax/analyze.py captures/loopback_*.wav \
    --xml /path/to/DEV_xxxx_SUBSYS_xxxxxxxx.xml \
    --profile dynamic --curve balanced
```

`analyze.py` searches cwd / the capture's directory / the script
directory for the inverse filter and stimulus files (in that order),
so it works without flags as long as you keep the artifacts together.

## Stimulus suite

| stimulus | level | what it probes |
|---|---|---|
| `stimulus_sweep.wav` | −18 dBFS peak | exponential 20 Hz–22 kHz sweep; recovers an LTI IR if the system is LTI |
| `stimulus_sweep_quiet.wav` | −42 dBFS peak | same sweep at lower input — does the leveler engage less? (no, in practice) |
| `stimulus_pink.wav` | −18 dBFS RMS | steady-state magnitude after the leveler settles |
| `stimulus_pink_quiet.wav` | −42 dBFS RMS | pink noise at low input level |
| `stimulus_multitone.wav` | −18 dBFS RMS | 20 pure tones at Dolby band centers; per-band amplitude + phase via single-bin DFT |

The first round of captures (sweep at −18 dBFS only) showed that DAX3
is non-LTI: the leveler / regulator engage during the sweep, contaminating
the deconvolved IR. The pink and multitone stimuli are designed to give
the leveler something stationary to settle on, isolating the steady-state
EQ from the time-varying dynamics.

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

Produces all 5 stimuli + per-stimulus meta JSON + the shared
`inverse_sweep.npy` in the current directory. Deterministic — re-run
only if you change parameters at the top of the script.

## 1. Copy to Windows

The Windows side needs:

- `stimulus_sweep.wav` + `stimulus_sweep.json`
- `stimulus_sweep_quiet.wav` + `stimulus_sweep_quiet.json`
- `stimulus_pink.wav` + `stimulus_pink.json`
- `stimulus_pink_quiet.wav` + `stimulus_pink_quiet.json`
- `stimulus_multitone.wav` + `stimulus_multitone.json`
- `capture_dax.py`
- `CLAUDE_WINDOWS.md` (only if Claude Code will be helping on Windows)

Total ~12 MB of stimuli + a small script. Copy via USB / OneDrive.

## 2. One-time Windows setup

In an admin PowerShell or cmd:

```powershell
pip install sounddevice numpy scipy soundfile pycaw comtypes pyaudiowpatch
```

`pyaudiowpatch` is the active backend on Win11 (`sounddevice`'s
WasapiSettings doesn't expose a `loopback` kwarg in any released version
— `capture_dax.py` falls back automatically).

Lock the speaker endpoint format to **48 kHz** (any bit depth — 16 or
24-bit are both fine, the loopback taps the float32 mix bus pre-quantize):

> Settings → System → Sound → All sound devices → **[your speakers]** →
> Output settings → Format → any **48000 Hz** entry

The script verifies the rate and aborts if wrong.

## 3. Capture procedure (Windows)

For each profile (toggle in Dolby Access GUI between profile groups, run
all five stimuli per profile in one go):

```powershell
# off baseline (Dolby Atmos toggled OFF in Dolby Access)
foreach ($s in 'sweep','sweep_quiet','pink','pink_quiet','multitone') {
    python capture_dax.py --stimulus stimulus_$s.wav --label off
}

# Then for each Dolby profile (toggle in Dolby Access UI between blocks):
foreach ($s in 'sweep','sweep_quiet','pink','pink_quiet','multitone') {
    python capture_dax.py --stimulus stimulus_$s.wav --label dynamic
}
# … repeat for movie, music, game, voice
```

5 stimuli × 6 profile labels = 30 captures, ~13 s each = ~6 min of pure
capture time + manual profile-toggle time. Plan ~30 min on Windows.

Each capture produces `captures/loopback_<kind>_<label>.wav` plus a
matching `.json` sidecar (schema v1) recording everything that was
active at capture time: endpoint, format, Dolby spatial mode, package
version map, capture levels, similarity-to-OFF score, and the full
stimulus meta.

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

`analyze.py` reads each capture's sidecar to determine the stimulus
kind, then dispatches:

- **kind=sweep** → Farina deconvolution → `ir_sweep_<label>_{L,R}.wav`
  (8192-sample IR centered on its peak with 2048 samples of pre-peak
  context, peak-normalized 32-bit float).
- **kind=pink** → Welch-style averaged PSD over the analysis window
  (default 6–11 s into the capture), divided by the same window of the
  stimulus to recover the steady-state EQ. Result in
  `spectrum_<kind>_<label>_<channel>.npz`.
- **kind=multitone** → single-bin DFT at each of the 20 band-center
  frequencies. Recovers per-band amplitude *and phase* (subtracting the
  known stimulus phase). Result in `tones_<kind>_<label>_<channel>.npz`.

When `--xml` is given, also writes a `compare_<basename>_<profile>_<curve>_<channel>.png`
and a textual residual table.

## What the results mean

After running on the OFF + Dynamic captures, the most informative views:

**Phase character** (sweep only, in `analysis_*.txt`):
- "minimum-phase (post-peak energy dominates)" → DAX3 IR concentrates
  energy after the peak, like our generated FIR. Phase choice matches.
- "linear-phase or symmetric (post ≈ pre)" → DAX3 uses linear-phase.
  Our converter trades phase accuracy for ~42 ms latency reduction.
  This finding doesn't motivate a code change (saved no-added-latency
  feedback applies regardless), but it answers the open question.
- "hybrid (post > pre)" → mixed-phase or non-LTI artifact.

**Multitone phase column** (`compare_loopback_multitone_<label>_..._L.png`):
- For an LTI system, the per-band phases should form a smooth curve
  vs frequency. For a minimum-phase FIR, accumulated phase grows with
  frequency in a feature-driven way. For linear-phase, it's `−omega*N/2`.
- Wildly noisy phases per band → non-LTI processing (volume leveler,
  regulator) modulates phase content-adaptively.

**Pink-noise EQ recovery** (`compare_loopback_pink_<label>_..._L.png`):
- The cleanest steady-state magnitude readout. After the leveler has
  settled (~6 s), the captured-vs-stimulus dB ratio per bin recovers
  the active EQ shape.
- Compare the recovered EQ curve to the FIR target. If they match
  within ~1 dB, our converter's curve is what DAX3 applies. If not,
  DAX3 is doing something we're not modeling.

**Sweep level swap** (`stimulus_sweep_quiet.wav`):
- If the −42 dBFS sweep has a similar non-LTI signature to the −18
  dBFS one, the leveler is independent of input level (it targets a
  fixed loudness regardless of input). If the quiet version is more
  LTI-like, the leveler engages more aggressively at moderate levels.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `endpoint default samplerate is 44100 Hz` | Output format not locked. Fix in Sound settings (any 48 kHz entry). |
| `No Dolby installation detected` | Wrong endpoint (HDMI / dock?), or DAX3 not installed. Pass `--no-apo-check` if you can verify manually that Atmos is the active spatial mode. |
| `Dolby Access shows X but you passed Y` | Toggle the right profile in Dolby Access GUI (this check is undocumented-format substring matching, false positives possible — say "y" if you're sure). |
| `capture is essentially identical to the OFF baseline` | Forgot to switch DAX3 on, or forgot to switch profile. The script flags > 0.98 cross-correlation. |
| OFF-baseline check fails (sidelobes > −25 dB on sweep) | Some other APO active (Realtek "Audio Effects", "Loudness Equalization"); disable in Sound → Properties → Enhancements. |
| `endpoint is muted — capture will be silent` | Unmute in Volume Mixer and re-run. |

## Limitations

- **No automated profile toggle.** Dolby Access has no public API; we
  switch profiles in its GUI between captures.
- **L=R stereo stimulus only.** Recovers DAX3's diagonal IR. Cross-channel
  processing (surround virtualizer, dialog steering) isn't measured.
  Do separate L-only and R-only sweeps as a follow-up if needed.
- **Speaker-endpoint dependent.** The captured IR is what DAX3 does to
  the digital signal; the physical speaker's response isn't included
  (loopback can't measure that).

## Prior art

`shuhaowu/linux-thinkpad-speaker-improvements` (linked from the main
README under "Further reading") uses the same basic technique — WASAPI
loopback of a stimulus through DAX3 — with a single dirac impulse and
manual Audacity capture. We use a swept sine plus stationary stimuli
for ~40 dB better SNR, script the entire Windows-side flow, and
correlate per-stimulus baselines so the "I forgot to switch profile"
mistake is caught automatically.
