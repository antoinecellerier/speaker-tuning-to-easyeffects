# Windows-side Claude session context

You are running on Windows assisting the user with **`capture_dax.py`**.
The companion Linux machine is offline; everything you need is in this
file. Don't ask the user to reboot.

## What this is, in 30 seconds

The user maintains a Linux tool that converts Dolby DAX3 tuning XMLs
into EasyEffects presets. DAX3 ships only on Windows; the Linux tool
generates a minimum-phase FIR per channel (cepstral homomorphic
processing), among other things, to approximate what DAX3 does to
audio. There's an open question — does DAX3's actual implementation
use minimum-phase, linear-phase, or something else? We can't tell
from the published XML (it's magnitude-only), so we measure DAX3's
output empirically on Windows.

**Your job, on Windows:** run `capture_dax.py` to play a swept-sine
stimulus through DAX3 and record WASAPI loopback. The captured
`loopback_*.wav` files go back to Linux for deconvolution and
comparison.

**Out of scope on Windows:**
- Changing the Linux-side conversion script (you don't have it
  anyway). Even if the captures reveal that DAX3 is linear-phase,
  the user has explicitly said we don't switch — there's a hard
  no-added-latency constraint (linear-phase = ~42 ms group delay).
- Modifying `capture_dax.py`'s post-processing math. The
  deconvolution and comparison run on Linux.
- Reverse-engineering Dolby's binaries.

## What "the user" wants from you

If `capture_dax.py` works, just run it for each profile and confirm
the captures look reasonable. Saved feedback says the user prefers
terse confirmations.

If something breaks, **fix capture_dax.py** to get the captures
recorded. Don't redesign the experiment, don't suggest a different
tool, don't ask them to reboot — debug the script.

## Files in this directory

- `capture_dax.py` — the script you're running and may need to fix.
  Reads `--stimulus <path>` plus its sidecar `<basename>.json`, plays it
  through the speaker output, records WASAPI loopback. Pre-capture
  validation: endpoint format (48 kHz, any bit depth), Dolby APO
  presence. Post-capture: level checks, similarity vs the matching
  per-kind OFF baseline. ~600 lines.
- **5 stimulus files** (each with a matching `.json` sidecar):
  - `stimulus_sweep.wav` — exponential sweep, −18 dBFS peak (10 s + 1 s tail)
  - `stimulus_sweep_quiet.wav` — same sweep at −42 dBFS peak
  - `stimulus_pink.wav` — pink noise, −18 dBFS RMS (12 s + 1 s tail)
  - `stimulus_pink_quiet.wav` — pink noise at −42 dBFS RMS
  - `stimulus_multitone.wav` — 20 band-center tones, −18 dBFS RMS
- `captures/` — output dir. Each capture writes
  `loopback_<kind>_<label>.wav` (e.g., `loopback_pink_dynamic.wav`)
  plus a matching `.json` sidecar.
- `CLAUDE_WINDOWS.md` — this file.

## Run sequence

```powershell
# One-time
pip install sounddevice numpy scipy soundfile pycaw comtypes pyaudiowpatch

# OFF baseline (Dolby Access: toggle Atmos OFF):
foreach ($s in 'sweep','sweep_quiet','pink','pink_quiet','multitone') {
    python capture_dax.py --stimulus stimulus_$s.wav --label off
}

# Then for each Dolby profile (toggle in Dolby Access UI between blocks):
foreach ($s in 'sweep','sweep_quiet','pink','pink_quiet','multitone') {
    python capture_dax.py --stimulus stimulus_$s.wav --label dynamic
}
# … repeat the inner block for movie / music / game / voice
```

5 stimuli × 6 profile labels = 30 captures, ~13 s each ≈ 6 min of pure
capture time + manual Dolby Access toggling between profile groups.

The **OFF baseline is critical** — `analyze.py` on Linux uses it both
to validate the capture chain (a true identity passthrough should
deconvolve to a clean bandlimited Dirac) and as the per-stimulus
reference for the "forgot to switch profile" similarity check. Always
capture `--label off` first for each stimulus; otherwise the post-capture
similarity check has no baseline.

The `--label off` capture should be done with **Dolby Atmos toggled
OFF** in Dolby Access (or with all enhancements disabled). The script
warns rather than aborts in that case if the Dolby APO is still
inserted on the endpoint.

## Hard requirements before capture

1. **Speaker endpoint sample rate = 48 kHz** (shared mode). Set in
   `Settings → System → Sound → All sound devices → [your speakers] →
   Output settings → Format`. Bit depth (16- or 24-bit) doesn't matter:
   WASAPI loopback taps the float32 engine mix bus regardless. Some
   Realtek drivers don't expose a 32-bit float option at all — pick
   any 48 kHz entry. The script aborts if the sample rate is wrong.
2. **Dolby Access installed** with the user's speakers as the active
   endpoint. Verify by playing music with Dolby on/off and listening
   for an obvious change.
3. **Volume reasonable** — −18 dBFS stimulus through the system
   volume should produce audible but not loud playback. If the
   capture clips, lower system volume; if it's <−60 dBFS peak,
   raise it.

## Known fragile spots in capture_dax.py

These are places where it might fail on a particular Windows
configuration. Fix in order of likelihood:

### 1. `sounddevice.WasapiSettings(loopback=True)` may not exist

The `WasapiSettings(loopback=...)` kwarg requires a recent
`sounddevice` (≥ 0.4.6) backed by a recent PortAudio. If you get
`TypeError: __init__() got an unexpected keyword argument 'loopback'`
or the input stream produces silence, fall back to **`pyaudiowpatch`**
(a maintained fork of PyAudio with explicit WASAPI loopback support):

```powershell
pip install pyaudiowpatch
```

Then in `capture_dax.py`, swap `play_and_record()` for a `pyaudiowpatch`
implementation. Pattern:

```python
import pyaudiowpatch as pyaudio
p = pyaudio.PyAudio()
# Get the loopback device that mirrors the default output
default_output = p.get_default_output_device_info()
loopback = next(
    d for d in p.get_loopback_device_info_generator()
    if d["name"].startswith(default_output["name"])
)
in_stream = p.open(
    format=pyaudio.paFloat32,
    channels=2,
    rate=48000,
    frames_per_buffer=1024,
    input=True,
    input_device_index=loopback["index"],
)
out_stream = p.open(
    format=pyaudio.paFloat32,
    channels=2,
    rate=48000,
    output=True,
)
# read in chunks, play stimulus through out_stream, collect input
```

Keep the script's pre/post validation logic and command-line interface
unchanged. Just swap the audio I/O backend.

### 2. Dolby presence check: three layered signals

`detect_dolby()` looks for Dolby via three independent sources, in
order, and considers any single hit sufficient:

1. **Per-endpoint MMDevices property store** at
   `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render\<endpoint-id>\Properties`.
   Format-id `{a45429a4-aa63-4480-b7f8-3f2552daee93}` holds the
   spatial-mode display names (e.g. "Dolby Atmos for built-in
   speakers"). The active spatial-mode CLSID is the REG_SZ value at
   `{9637b4b9-11ee-4c35-b43c-7b2452c993cc},1` and is captured into the
   metadata sidecar regardless of detection outcome.
2. **System-wide APO registry** at
   `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\AudioEngine\AudioProcessingObjects`.
   This key is **absent on Win11 24H2** and only exists on older Win10
   builds — its absence is not an error, just a no-op fallback.
3. **Dolby Access UWP package** under
   `%LOCALAPPDATA%\Packages\DolbyLaboratories.DolbyAccess_*`.

If all three miss, the script aborts (`--label off` warns instead of
aborting). If you *know* Dolby Atmos is the active spatial mode but
detection still fails, pass `--no-apo-check` to bypass — the captured
metadata sidecar still records what the detector saw, so the bypass
is auditable later.

Don't disable the check unconditionally — it's there to catch the
"wrong endpoint selected" / "Dolby Access uninstalled" mistakes.

### 3. Endpoint resolution

`resolve_endpoint()` auto-picks the default WASAPI output if no
`--device` is given. If the laptop has multiple audio devices (HDMI,
Bluetooth, dock), the user may have a non-speaker as the system
default. Symptom: capture is silent or doesn't include DAX3.
Fix: `python capture_dax.py --label off --device "Speakers"` (any
substring of the friendly name).

### 4. Dolby Access state file read

`best_effort_dolby_state()` looks for the user's currently selected
profile inside `%LOCALAPPDATA%\Packages\DolbyLaboratories.DolbyAccess_*\
LocalState\`. The format is **undocumented** — the script does
substring matching against profile names in `*.json` and `*.dat`
files. False positives and negatives are both possible. If the script
fires the "Dolby Access shows X but you passed Y" warning when both
look correct, just answer "y" and continue.

This check is a nice-to-have, not load-bearing. If it's noisy you
can comment out the call to `best_effort_dolby_state(args.label)` in
`main()`.

### 5. Resampling silently engaged

If the OFF baseline capture deconvolves (on the Linux side) to
something with sidelobes worse than −25 dB, the most common cause
is silent resampling somewhere in the audio path:
- The endpoint format check passed but Windows is downsampling
  internally (rare).
- The user has "Loudness Equalization", "Bass Boost", or other
  third-party enhancements enabled. Disable in
  `Settings → System → Sound → [speakers] → Properties → Enhancements
  → Disable all sound effects`.
- An ASIO driver is grabbing the device exclusively.

The user will see this only after running `deconvolve.py` on Linux,
so it's a "next reboot" issue rather than something to debug here.
But if levels look weird in the capture (RMS very low, peak clipping),
flag it before they leave Windows.

## Output expectations

Per capture, you should see something like:

```
============================================================
DAX3 IR capture — label: dynamic
============================================================
  Resolved endpoint: 'Speakers (Realtek(R) Audio)' (sounddevice index 4)
    default samplerate: 48000 Hz
    max output channels: 2
  Endpoint format OK (48000 Hz)
  Dolby APOs detected on system: 2
    {6C4E7DA4-30D5-44B7-A6CD-C0F08F5DEC0E} Dolby Atmos PostMix APO
    {F4250F44-5F92-4F03-A19F-0F2BB2B08C04} Dolby DAX3 EFX
  starting input loopback…
  starting playback…
  wrote captures/loopback_dynamic.wav (528000 samples)
  capture peak -18.4 dBFS, RMS -45.2 dBFS, clip samples: 0
  similarity to loopback_off.wav: 0.4221
```

Reasonable values:
- **Capture peak**: −20 to −10 dBFS. Outside that range, adjust system
  volume (lower if clipping, higher if very quiet).
- **Similarity to loopback_off.wav**: ≤ 0.95 for any DAX3-on capture.
  Higher = profile didn't change. The script warns at > 0.98.
- **Clip samples = 0**. Anything else means clipping during capture
  (DAX3's regulator may have engaged) — lower system volume and
  re-run.

## Saved user preferences (these apply to you too)

- **Don't push to git or modify shared state.** Local file edits are
  fine. Anything that creates a commit, opens a PR, or writes outside
  this directory: ask first.
- **No emojis in output unless explicitly asked.**
- **Terse confirmations preferred.** End-of-turn summary should be
  one or two sentences, no headers, no bullets.
- **For GitHub interactions** (unlikely on Windows but if it comes
  up): `gh issue/pr comment` bodies need a Claude Code attribution
  footer matching commits' `Co-Authored-By` line.

## What to do when captures are complete

Tell the user:
1. Which `loopback_<label>.wav` files were produced and their
   reasonable levels.
2. Any warnings (e.g., similarity to OFF too high → suggest re-run).
3. To copy the `captures/` directory back to Linux for the next
   stage (`deconvolve.py` + `compare.py` live there).

End your turn there. The Linux side picks it up.
