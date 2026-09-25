# `dolby_to_easyeffects.py`

## Command-line options

**Tuning input** — the script auto-discovers a tuning source when you pass
neither an XML path nor `--windows`. It probes the mounted Windows partitions in
`/proc/mounts` and the current directory.
- `xml_file` — optional positional path to the Dolby DAX3 tuning XML, such as
  `DEV_0287_SUBSYS_*.xml`
- `--windows DIR` — auto-discover the tuning XML from a mounted Windows
  directory, by matching the audio codec subsystem ID from `/proc/asound`
- `--best-guess` — on a SoundWire machine, if auto-detection finds no exact
  hardware match, fall back to the only internal-speaker tuning whose
  manufacturer is present. When more than one qualifies, it lists the candidates
  to pick one with the positional XML path. Reach for it when a SoundWire device
  reports "No matching DAX3 tuning XML found".

**Inspection**
- `--list` — show available endpoints and profiles in the XML, then exit
- `--speaker-info` — report detected audio hardware and speaker layout, then
  exit
- `--doctor` (alias `--diagnose`) — run environment self-diagnostics and exit.
  It checks hardware, install location, EasyEffects version and compatibility,
  preset and impulse-file integrity, the selected preset, background service
  mode and autostart, kernel age, and the PipeWire graph sample rate. The checks
  and what to do about them print last. If a generated preset seems inaudible,
  run this first and paste the output into an issue. See
  [Troubleshooting](troubleshooting.md#troubleshooting-a-preset-that-sounds-like-nothing).

**Profile selection**
- `--endpoint TYPE` — endpoint type (default: `internal_speaker`)
- `--mode MODE` — endpoint operating mode (default: `normal`). Yoga-class
  convertible laptops ship distinct tunings per hinge pose. If `--list` shows
  them for your device, try `--mode tablet`, `stand`, `tent`, or `lid_close`.
- `--profile TYPE` — profile type, such as `dynamic`, `music` or `voice`
  (default: first profile)
- `--all-profiles` — generate presets for every profile except `off` in the
  selected endpoint/mode

**Output**
- `--prefix NAME` — change preset name prefix (default: `Dolby` →
  `Dolby-Balanced`, etc.)
- `--output-dir DIR` — EasyEffects preset directory (default:
  `~/.local/share/easyeffects/output/`)
- `--irs-dir DIR` — impulse response directory (default:
  `~/.local/share/easyeffects/irs/`)

**Autoload**
- `--autoload [PRESET]` — write EasyEffects autoload config for speaker outputs.
  It defaults to the first Balanced preset generated.
- `--autoload-dir DIR` — autoload config directory (default:
  `~/.local/share/easyeffects/autoload/output/`)
- `--autoload-sink NODE_NAME` — bind autoload to a named PipeWire sink,
  bypassing speaker detection. Repeatable. Use it if detection picks the wrong
  output or finds none, for example on a device whose speaker isn't tagged
  `audio-speakers`. Find the name with `pw-dump | grep node.name`. See
  [Autoload](#autoload) below.
- `--no-autoload-bypass` — with `--autoload`, don't write a `Nothing` bypass
  preset or enable EasyEffects' global Fallback Preset. See
  [Autoload](#autoload) below.

**Filter tweaks**
- `--disable NAME` — drop a filter from the generated preset (repeatable). Valid
  names: `volmax`, `mbc`, `regulator`, `coupled-bands`, `autogain`,
  `bass-enhancer`, `dialog`, `high-shelf`, `lo-pass`. See
  [Disabling and enabling filters](filters.md).
- `--enable NAME` — switch on an optional stage the preset leaves off
  (repeatable). Valid names: `autogain`, `level-restore`, `virtual-bass`. See
  [Disabling and enabling filters](filters.md).
- `--volmax-slot {input-gain,output-gain}` — where the `volmax-boost` loudness
  gain is injected. The default `input-gain` runs it through the per-band
  regulator so loud bass doesn't distort (issue
  [#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23)).
  `output-gain` is the older placement: an opt-out for A/B, or a way to bring
  back bass and loudness an aggressive regulator takes away (issue
  [#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44)).
  See [Disabling and enabling filters](filters.md).

**General**
- `--verbose` (alias `-v`) — print the full frequency tables, hidden by default.
  Include a `-v` log when reporting a sound problem.
- `--dry-run` — run without writing any files to disk: no presets, IRs or
  autoload config. Useful for debugging script execution and output.
- `--no-reload` — don't ask a running EasyEffects to load the preset when the
  run finishes. Presets are still written. No effect under `--dry-run`, or when
  `--output-dir`/`--irs-dir` point outside EasyEffects' own folders.
- `--skip-ee-check` — skip the end-of-run EasyEffects environment check, for
  workflows that don't target an EasyEffects install. `dolby_to_pipewire.py`
  passes it automatically.
- `--skip-closing` — skip the end-of-run closing blocks, for wrappers that
  install elsewhere and present their own. The blocks are what was written and
  how to use it, plus the report-back block.
- `--no-color` — disable colored terminal output
- `--version` — print the version and exit

When you pass `--mode`, `--profile` or `--all-profiles`, the preset names
include the mode or profile, as in `Dolby-Music-Balanced` or
`Dolby-Tablet-Voice-Warm`.

## Autoload

`--autoload` configures EasyEffects to apply a preset automatically whenever the
internal speaker output becomes active. To generate all presets and autoload one
on the speaker:

```bash
python3 dolby_to_easyeffects.py --windows /mnt/windows/Windows \
    --all-profiles --autoload Dolby-Dynamic-Balanced
```

It writes a `{node.name}:{route}.json` autoload file to
`~/.local/share/easyeffects/autoload/output/`, after detecting the speaker sink
with `pw-dump`. It also installs an empty `Nothing` bypass preset, so HDMI,
Bluetooth, USB and other non-speaker outputs don't keep processing the speaker
tuning. Run it from a desktop session with PipeWire running.

The run loads the preset into a running native EasyEffects 8.0.9+ right away. It
can't reach other installs, such as a Flatpak EasyEffects, which keeps the
socket inside its sandbox. It leaves EasyEffects alone on a non-speaker output,
or with the `Nothing` bypass preset already active, and says so. In those cases,
restarting EasyEffects lets the autoload pick the preset up. If EasyEffects is
running, restart it anyway for the fallback setting to take effect.

For the autoload to take effect on every login, also enable Background Service
and Autostart on login in EasyEffects' preferences, so EasyEffects is running
when the speaker becomes active. See
[Troubleshooting](troubleshooting.md#troubleshooting-a-preset-that-sounds-like-nothing).
Pass `--autoload-sink NODE_NAME` to bind a sink yourself, or
`--no-autoload-bypass` to skip the bypass if you manage it yourself.

![EasyEffects autoload: Dolby-Balanced bound to the speaker output, with Nothing as the global fallback preset](images/ee-autoload.jpg)

### How speaker detection and the bypass fallback work

The autoload file name follows EasyEffects' convention,
`{node.name}:{route}.json`. `{route}` is the sink's active output route
description, such as `Speaker`. EasyEffects matches on that route, not on the
card profile.

The script detects speaker sinks from PipeWire via `pw-dump`. It first takes the
sinks tagged with the `audio-speakers` device icon, excluding
HDMI/DisplayPort/Bluetooth. Some devices lack a device-specific UCM2 profile and
fall back to a generic one that doesn't set the speaker icon. If none are
tagged, the script falls back to a relaxed tier of internal analog outputs,
still excluding HDMI/Bluetooth/headsets. In that tier, it applies a single match
automatically, asks you to choose when it finds several, and lists every sink it
saw with its icon, so you can see why. If the active output route can't be
read from PipeWire, the script skips that sink and says why. It doesn't write a
guessed name, which EasyEffects wouldn't match.

EasyEffects applies the last-loaded preset to whatever sink is active. If you
switch to HDMI, a USB headset or Bluetooth while a Dolby preset is loaded, the
Dolby correction keeps running on hardware it was never tuned for. `--autoload`
mitigates this by also writing the `Nothing` bypass preset and turning on
EasyEffects' global Fallback Preset, pointed at `Nothing`. Any sink without its
own autoload entry then falls back to a no-op chain. An existing `Nothing.json`
preset is preserved. An already-enabled fallback is left untouched, whatever
preset it points at.

## Flatpak EasyEffects

**Flatpak EasyEffects.** The script picks the Flatpak paths when a Flatpak has
left files under `~/.var/app/com.github.wwmm.easyeffects/`, or is installed but
never launched. Otherwise it uses the native `~/.local/share/easyeffects/`. On
the Flatpak, presets, impulse responses and autoload entries go to
`.../data/easyeffects/`. EasyEffects 8 keeps user files there, and only its
settings database under `.../config/easyeffects/db/`. The native paths above are
the XDG defaults. Set `XDG_DATA_HOME` or `XDG_CONFIG_HOME` and the script
follows them, as EasyEffects does. The Flatpak paths don't move, because
`flatpak run` overrides those variables inside its own sandbox. Override any of
it with `--output-dir`, `--irs-dir`, and `--autoload-dir`.
