# Windows-side Claude session context

You are running on Windows, assisting the user with `capture_dax.py`. The
companion Linux machine is offline, so everything you need is in this file.
Don't ask the user to reboot.

## What this is, in 30 seconds

The user maintains a Linux tool that converts Dolby DAX3 tuning XMLs into
EasyEffects presets. DAX3 ships only on Windows. Among other things, the
Linux tool generates a minimum-phase FIR per channel by cepstral homomorphic
processing, to approximate what DAX3 does to audio. An open question is
whether DAX3's actual implementation uses minimum-phase, linear-phase or
something else. The published XML is magnitude-only and cannot tell us, so
we measure DAX3's output empirically on Windows.

Your job on Windows is to run `capture_dax.py` to play a swept-sine stimulus
through DAX3 and record WASAPI loopback. The captured `loopback_*.wav` files
go back to Linux for deconvolution and comparison.

**Out of scope on Windows:**

- Changing the Linux-side conversion script. You don't have it anyway. Even
  if the captures reveal that DAX3 is linear-phase, the user has explicitly
  said we don't switch. Linear-phase means ~43 ms group delay, and there is
  a hard no-added-latency constraint.
- Modifying `capture_dax.py`'s post-processing math. The deconvolution and
  comparison run on Linux.
- Reverse-engineering Dolby's binaries.

## What "the user" wants from you

If `capture_dax.py` works, just run it for each profile and confirm the
captures look reasonable. The user prefers terse confirmations.

If something breaks, **fix capture_dax.py** to get the captures recorded.
Debug the script. Don't redesign the experiment, and don't suggest a
different tool.

## Files in this directory

- `capture_dax.py`: the script you're running and may need to fix. It reads
  `--stimulus <path>` plus its sidecar `<basename>.json`, plays it through
  the speaker output and records WASAPI loopback. Before capture it
  validates Dolby APO presence and the endpoint format: 48 kHz, any bit
  depth. After capture it checks levels and the similarity vs the matching
  per-kind OFF baseline.
- 5 stimulus files, each with a matching `.json` sidecar:
  - `stimulus_sweep.wav`: exponential sweep, −18 dBFS peak, 10 s + 1 s tail
  - `stimulus_sweep_quiet.wav`: same sweep at −42 dBFS peak
  - `stimulus_pink.wav`: pink noise, −18 dBFS RMS, 12 s + 1 s tail
  - `stimulus_pink_quiet.wav`: pink noise at −42 dBFS RMS
  - `stimulus_multitone.wav`: 20 band-center tones, −18 dBFS RMS
- The stimuli the later runs use, if the user copied them, each with its
  `.json` sidecar: the ladder rungs `stimulus_pink{60,48,30,24,14}.wav`,
  `stimulus_stepped_loud.wav` and `stimulus_bass_burst[_quiet].wav`.
- `captures/`: the output dir. Each capture writes
  `loopback_<tag>_<label>.wav` plus a matching `.json` sidecar. `<tag>` is
  the stimulus file name minus `stimulus_`, so `stimulus_pink.wav` captured
  as `dynamic` writes `loopback_pink_dynamic.wav`.
- `CLAUDE_WINDOWS.md`: this file.

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
capture time, plus manual Dolby Access toggling between profile groups.

Always capture `--label off` first for each stimulus. Otherwise the
post-capture similarity check has no baseline. The **OFF baseline is
critical**: `analyze.py` on Linux uses it both to validate the capture chain
and as the per-stimulus reference for the "forgot to switch profile"
similarity check. For the chain check, a true identity passthrough should
deconvolve to a clean bandlimited Dirac.

Do the `--label off` capture with Dolby Atmos toggled OFF in Dolby Access,
or with all enhancements disabled. If the Dolby APO is still inserted on
the endpoint in that case, the script warns rather than aborts.

## Priority run: the volume-leveler level ladder

If time is short, do this run first. It is currently the largest unmeasured
gap between DAX and the Linux chain.

DAX applies a gain that depends on how loud the input is. Only two points on
that curve have ever been measured, both on the dev device. As DAX-on minus
DAX-off, they are +7.1 dB at −17.8 dBFS and +16.4 dB at −41.8 dBFS. The Linux
side approximates that curve with chosen constants rather than measured ones.
Seven rungs turn two points into a curve.

```powershell
# Ladder rungs, quietest first. pink/pink_quiet are the two you already
# have; the rest are new files from make_stimulus.py.
$ladder = 'pink60','pink48','pink_quiet','pink30','pink24','pink','pink14'

# OFF first (Dolby Access: Atmos OFF) — every rung needs its own baseline.
foreach ($s in $ladder) { python capture_dax.py --stimulus stimulus_$s.wav --label off }

# Then Atmos ON, profile = Dynamic.
foreach ($s in $ladder) { python capture_dax.py --stimulus stimulus_$s.wav --label dynamic }
```

14 captures, ~13 s each. Two rungs, `pink` and `pink_quiet`, are already
done on both labels. Re-capturing them is harmless, and it confirms the
session is comparable to the archived one.

Capture `off` at every rung, not just once. It is the control for this
measurement, not just a validity check. The whole result is on-minus-off *at
the same input level*, so a missing rung loses that rung entirely. It is
also the only way to confirm the off path is level-linear. If it is, that
is worth knowing. Assume it instead, and the ladder measures nothing.

**Loud end.** Do not add pink rungs above −14 dBFS RMS. Pink noise has
~13 dB of crest factor, so −12 dBFS RMS already peaks past full scale and
`make_stimulus.py` will refuse it. For the loud end, use the stepped-sine
stimulus built for it:

```powershell
foreach ($lbl in 'off','dynamic') {
    python capture_dax.py --stimulus stimulus_stepped_loud.wav --label $lbl
}
```

## Already in the archive

The stepped battery is **complete at all three levels, with its OFF
pairs**, checked 2026-08-05 against the dev-device archives:

| stimulus | `dynamic` | `off` |
|---|---|---|
| `stepped` (−18 dBFS) | yes | yes |
| `stepped_quiet` (−42 dBFS) | yes | yes |
| `stepped_loud` (−2 dBFS) | yes | yes |

Stepped is the regime the regulator actually engages in. On the dev device,
`stepped_loud` drives DAX over its threshold on 3 of 4 active bands. The open
regulator questions in design-notes entries 6/11 are therefore an analysis gap,
not a capture gap. Don't re-capture the stepped battery: it buys nothing.

The pink ladder measures the volume leveler. It will not incidentally
measure the regulator: pink tops out at −14 dBFS RMS before it clips, which
on the dev device still leaves DAX several dB short of its lowest regulator
threshold. Stepped is the stimulus for the regulator.

Whatever you capture, the `off` baseline is the session's own reference: a
cheap per-session check, and worth it. Endpoint volume lands inside the
loopback on some machines and not others. The dev device reads −0.02 dB with
its endpoint at −18.8 dB. Another reporter's machine read −10.01 dB with its
endpoint at −10.5 dB. The `_dax_off_reference` docstring in
`tools/measure_ee/compare_ee_vs_dax.py` records both readings. Sessions are
only comparable through their own OFF capture, so never skip it and never
assume last session's holds.

## Also worth capturing: bass burst, OFF

`stimulus_bass_burst.wav` and `stimulus_bass_burst_quiet.wav` were captured
on `dynamic` only. The on-minus-off delta is the bass measurement the Linux
side wants, and it cannot be computed from the archive. Two captures fix
that:

```powershell
foreach ($s in 'bass_burst','bass_burst_quiet') {
    python capture_dax.py --stimulus stimulus_$s.wav --label off
}
```

## Hard requirements before capture

1. Speaker endpoint sample rate = 48 kHz, in shared mode. Set it in
   `Settings → System → Sound → All sound devices → [your speakers] → Output settings → Format`.
   Bit depth doesn't matter, 16- or 24-bit alike: WASAPI loopback taps the
   float32 engine mix bus regardless. Some Realtek drivers don't expose a 32-bit
   float option at all, so pick any 48 kHz entry. The script aborts if the
   sample rate is wrong.
2. Dolby Access installed, with the user's speakers as the active endpoint.
   Verify by playing music with Dolby on/off and listening for an obvious
   change.
3. Volume reasonable: −18 dBFS stimulus through the system volume should
   produce audible but not loud playback. If the capture clips, lower system
   volume. If it's <−60 dBFS peak, raise it.

## Known fragile spots in capture_dax.py

These are places where the script might fail on a particular Windows
configuration. Fix in order of likelihood:

### 1. `sounddevice.WasapiSettings(loopback=True)` may not exist

The `WasapiSettings(loopback=...)` kwarg requires `sounddevice` ≥ 0.4.6,
backed by a recent PortAudio. If you get
`TypeError: __init__() got an unexpected keyword argument 'loopback'`
or the input stream produces silence, fall back to `pyaudiowpatch`, a
maintained fork of PyAudio with explicit WASAPI loopback support:

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

`detect_dolby()` looks for Dolby in three independent sources, in order,
and considers any single hit sufficient:

1. The per-endpoint MMDevices property store at
   `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render\<endpoint-id>\Properties`.
   Format-id `{a45429a4-aa63-4480-b7f8-3f2552daee93}` holds the
   spatial-mode display names, e.g. "Dolby Atmos for built-in speakers".
   The active spatial-mode CLSID is the REG_SZ value at
   `{9637b4b9-11ee-4c35-b43c-7b2452c993cc},1`. The script captures it into
   the metadata sidecar regardless of detection outcome.
2. The system-wide APO registry at
   `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\AudioEngine\AudioProcessingObjects`.
   This key is absent on Win11 24H2 and only exists on older Win10 builds.
   Its absence is not an error, just a no-op fallback.
3. The Dolby Access UWP package under
   `%LOCALAPPDATA%\Packages\DolbyLaboratories.DolbyAccess_*`.

If all three miss, the script aborts, except that `--label off` warns
instead of aborting. If you *know* Dolby Atmos is the active spatial mode
but detection still fails, pass `--no-apo-check` to bypass it. The captured
metadata sidecar still records what the detector saw, so the bypass is
auditable later.

Don't disable the check unconditionally. It is there to catch the "wrong
endpoint selected" and "Dolby Access uninstalled" mistakes.

### 3. Endpoint resolution

`resolve_endpoint()` auto-picks the default WASAPI output if no `--device`
is given. If the laptop has multiple audio devices, such as HDMI, Bluetooth
or a dock, the user may have a non-speaker as the system default. Symptom:
capture is silent or doesn't include DAX3. Fix:
`python capture_dax.py --label off --device "Speakers"`. Any substring of
the friendly name works.

### 4. Dolby Access state file read

`best_effort_dolby_state()` looks for the user's currently selected profile
inside `%LOCALAPPDATA%\Packages\DolbyLaboratories.DolbyAccess_*\LocalState\`.
The format is undocumented, so the script does substring matching against
profile names in `*.json` and `*.dat` files. False positives and negatives
are both possible. If the script fires the "Dolby Access shows X but you
passed Y" warning when both look correct, just answer "y" and continue.

This check is a nice-to-have, not load-bearing. If it's noisy, you can
comment out the call to `best_effort_dolby_state(args.label)` in `main()`.

### 5. Resampling silently engaged

If the OFF baseline capture deconvolves on the Linux side to something with
sidelobes worse than −25 dB, the most common cause is silent resampling
somewhere in the audio path:

- The endpoint format check passed but Windows is downsampling internally.
  This is rare.
- The user has "Loudness Equalization", "Bass Boost", or other third-party
  enhancements enabled. Disable them in
  `Settings → System → Sound → [speakers] → Properties → Enhancements → Disable all sound effects`.
- An ASIO driver is grabbing the device exclusively.

The user will see this only after running `analyze.py` on Linux, so it's
a "next reboot" issue rather than something to debug here. But if levels
look weird in the capture, such as very low RMS or peak clipping, flag it
before they leave Windows.

## Output expectations

Per capture, you should see something like:

```
============================================================
DAX3 capture — tag: pink (kind: pink), label: dynamic
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
  wrote captures/loopback_pink_dynamic.wav (528000 samples)
  capture peak -18.4 dBFS, RMS -45.2 dBFS, clip samples: 0
  similarity to loopback_off.wav: 0.4221
```

Reasonable values:

| value | reasonable | otherwise |
|---|---|---|
| Capture peak | −20 to −10 dBFS | Adjust system volume: lower if clipping, higher if very quiet. |
| Similarity to loopback_off.wav | ≤ 0.95 for any DAX3-on capture | Higher = profile didn't change. The script warns at > 0.98. |
| Clip samples | 0 | Anything else means clipping during capture. DAX3's regulator may have engaged. Lower system volume and re-run. |

## Saved user preferences (these apply to you too)

- Don't push to git or modify shared state. Local file edits are fine.
  Anything that creates a commit, opens a PR, or writes outside this
  directory: **ask first**.
- No emojis in output unless explicitly asked.
- Terse confirmations are preferred. The end-of-turn summary should be one
  or two sentences, with no headers and no bullets.
- GitHub interactions are unlikely on Windows. If one comes up,
  `gh issue/pr comment` bodies need a Claude Code attribution footer
  matching commits' `Co-Authored-By` line.

## What to do when captures are complete

Tell the user:

1. Which `loopback_<tag>_<label>.wav` files were produced, and their reasonable
   levels.
2. Any warnings, e.g. similarity to OFF too high → suggest a re-run.
3. To copy the `captures/` directory back to Linux, where `analyze.py` runs
   the next stage.

End your turn there. The Linux side picks it up.
