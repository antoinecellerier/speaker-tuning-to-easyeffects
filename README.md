# Dolby Atmos Speaker Tuning to EasyEffects Presets for Linux

Bring your laptop's Windows speaker tuning to Linux. This script converts the
Dolby Atmos DAX3 tuning XML shipped inside Windows audio drivers into
[EasyEffects](https://github.com/wwmm/easyeffects) 8.x output presets. Your
speakers get the same speaker correction, EQ, and dynamics processing as on
Windows, at zero added latency.

> **EasyEffects 8.x required.** If your distro ships EasyEffects 7, install the
> [Flatpak](https://flathub.org/apps/com.github.wwmm.easyeffects). The EE 7 and
> EE 8 preset formats aren't compatible. Debian trixie, Ubuntu 24.04+, and
> Fedora 43 and earlier ship EE 7.

Don't want to run EasyEffects? The same tuning also runs as a self-contained
PipeWire filter-chain, with no GUI and no extra daemon. See
[PipeWire filter-chain](#pipewire-filter-chain-instead-of-easyeffects) under
Advanced.

**Contents:** [Quick start](#quick-start) ·
[Staying up to date](#staying-up-to-date) ·
[Supported devices](#supported-devices) · [Install](#install) · [Usage](#usage)
· [Advanced](#advanced) · [How it works](#how-it-works) ·
[Running the tests](#running-the-tests) · [Further reading](#further-reading)

## Quick start

1. Install the dependencies listed under [Install](#install). In short: Python 3
   with NumPy and SciPy.

2. Run the script. It needs no path if your Windows partition is mounted or a
   driver package is extracted in the current directory:

   ```bash
   python3 dolby_to_easyeffects.py --autoload
   ```

   Or point it at the Windows directory or a tuning XML:

   ```bash
   python3 dolby_to_easyeffects.py --windows /mnt/windows/Windows --autoload
   python3 dolby_to_easyeffects.py path/to/DEV_0287_SUBSYS_*.xml --autoload
   ```

`--autoload` sets EasyEffects to apply the Dolby correction on your internal
speaker automatically. Skip it if you'd rather select Dolby-Balanced,
Dolby-Detailed or Dolby-Warm yourself under Presets. See [Autoload](#autoload)
for details.

![Selecting a generated Dolby preset in EasyEffects](docs/images/ee-preset-select.jpg)

## Staying up to date

[CHANGELOG.md](CHANGELOG.md) tracks notable changes. Each version is published
as a
[GitHub Release](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/releases).
To be notified when a new version ships, click **Watch → Custom → Releases** at
the top of the GitHub page.

Entries tagged **[AUDIBLE]** change the *sound* of the generated preset. When
you see one, pull the latest and **re-run the script** to regenerate and reload
your preset. The run loads it into a running EasyEffects when it can, and says
what to pick otherwise. Filter-chain users re-run `ee_to_pipewire.py` too,
since a conf keeps the impulse it was converted with. Other entries are tooling,
packaging, docs, or new-device support that doesn't alter existing devices'
output, so there's nothing to regenerate.

Each generated preset and `.conf` is stamped with the version that produced it,
so you can always tell what made a file when reporting an issue. The preset
JSON holds it in a `_generator` field, and the conf in a `# version:` line.
`--version` prints the version.

## Supported devices

The converter works on the internal speakers of laptops, handhelds and other
devices whose Windows driver ships a Dolby DAX3 tuning. Confirmed on:

| Device | Codec / Subsystem | Reported by |
|---|---|---|
| ASUS TUF Gaming A15 (FA507NV) | Realtek ALC256, 1043:19DD | [#34](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/34) |
| ASUS Zenbook 14 UX3405CA, UX3405MA | Realtek ALC294, 1043:1A63 | [#19](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/19), [#24](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/24) |
| Framework Laptop 13 Pro (Intel Core Ultra Series 3) | Realtek ALC285, F111:000F | [#73](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/73) |
| Lenovo IdeaPad 3 15ALC6 (82KU) | Realtek ALC257, 17AA:38BC | [#103](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/103) |
| Lenovo IdeaPad Pro 5 14AHP9 (83D3) | Realtek ALC287, 17AA:38D0 | [#18](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/18) |
| Lenovo IdeaPad Pro 5 14APH8 (83AM) | Realtek ALC287, 17AA:38C5 | [#33](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/33) — reporter confirms working on kernel 7.0, broken on 6.12 |
| Lenovo IdeaPad Pro 5 14IMH9 (83D2) | Realtek ALC287, 17AA:38CE | [#36](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/36) — reporter recommends enabling autogain |
| Lenovo Legion Y540-15IRH (81SX) | Realtek ALC257, 17AA:380F | [#70](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/70) |
| Lenovo Legion Y7000 2020 (82AV) | Realtek ALC257, 17AA:3872 | [#93](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/93) |
| Lenovo ThinkBook 16p G5 IRX (21N5) | Realtek ALC287, 17AA:38F9 | [#76](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/76) |
| Lenovo XiaoXinPro-13ARE 2020 (82DM) | Realtek ALC257, 17AA:387F | [#91](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/91) |
| Lenovo Yoga 7 2-in-1 16AKP10 | — | [#1](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/1) |
| Lenovo Yoga 7 16IAH7 (82UF) | Realtek ALC287, 17AA:386A | [#53](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/53) — woofers need kernel 7.2, or the `hda_model=` line the tool prints |
| Lenovo Yoga Pro 7 14APH8 (82Y8) | Realtek ALC287, 17AA:38C6 | [#30](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/30) |
| Lenovo Yoga Pro 7 14ASP9 (83HN) | Realtek ALC287, 17AA:38A7 | [#51](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/51) |
| Lenovo Yoga Pro 7 14IMH9 (83E2) | Realtek ALC287, 17AA:38CF | [#83](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/83) |
| Lenovo Yoga Pro 9i 14IRP8 (83BU) | Realtek ALC287, 17AA:38BE | [#17](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/17) |
| Lenovo Yoga Slim 7 14ARE05 (82A2) | Realtek ALC287, 17AA:380D | [#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44) — reporter finds it on par with Windows with `--volmax-slot output-gain`, which brings back bass the default placement loses |
| Lenovo Yoga Slim 7 14ILL10 (83JX) | Soundwire 17AA:3838 | [#59](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/59) |
| Lenovo Yoga Slim 7 Pro 14ACH5 (82MS, reported as Yoga 14sACH 2021) | Realtek ALC287, 17AA:384F | [#84](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/84) |
| ThinkPad E14 Gen 2 AMD (20T6) | Realtek ALC257, 17AA:507F | [#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25) — verified close-to-Windows: needs `--enable autogain`, plus to taste a raised Autogain *Target* (EE GUI) and desktop volume >100% |
| ThinkPad L14 Gen 6 AMD (21S8) | Realtek ALC257, 17AA:50FF | [#61](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/61) |
| ThinkPad T14 Gen 1 (20S1) | Realtek ALC257, 17AA:22B1 | [#86](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/86) |
| ThinkPad T14 Gen 1 AMD (20UD, 20UE) | Realtek ALC257, 17AA:5081 | [#45](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/45) |
| ThinkPad T14 Gen 2 AMD (20XL) | Realtek ALC257, 17AA:5094 | [#80](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/80) |
| ThinkPad T14 Gen 2 Intel (20W1) | Realtek ALC257, 17AA:22C9 | [#55](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/55) |
| ThinkPad T14 Gen 7 AMD (21WV, 21WW) | Realtek ALC257, 17AA:5144 | [#48](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/48) |
| ThinkPad T14 Gen 7 Intel (21WN) | Realtek ALC257, 17AA:2356 | [#42](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/42) |
| ThinkPad T14s Gen 2 AMD (20XG) | Realtek ALC257, 17AA:5096 | [#57](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/57) |
| ThinkPad T14s Gen 3 (21BS) | Realtek ALC257, 17AA:22EE | [#88](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/88) |
| ThinkPad T14s Gen 6 AMD | 17AA:50F0 | [#3](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/3) |
| ThinkPad X1 Carbon Gen 9 (20XW) | Realtek ALC287, 17AA:22D5 | [#63](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/63) |
| ThinkPad X1 Carbon Gen 10 (21CC) | Realtek ALC287, 17AA:22E7 | [#99](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/99) |
| ThinkPad X1 Carbon Gen 11 (21HN) | Realtek ALC287, 17AA:2315 | [#95](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/95) |
| ThinkPad X1 Carbon Gen 13 | Soundwire 17AA:2339 | [#7](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/pull/7/) |
| ThinkPad X1 Yoga Gen 6 (20Y0) | Realtek ALC287, 17AA:22D4 | [#78](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/78) |
| ThinkPad X1 Yoga Gen 7 (21CD) | Realtek ALC287, 17AA:22E6 | author |
| ThinkPad X13 Gen 2 Intel (20WK) | Realtek ALC257, 17AA:22CF | [#105](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/105) |
| ThinkPad X13 Gen 6 Intel (21RK) | Realtek ALC257, 17AA:2344 | [#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23) |
| ThinkPad X13 Yoga Gen 2 (20W9) | Realtek ALC285, 17AA:22D6 | [#101](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/101) |
| ThinkPad X13 Yoga Gen 4 (21F3) | Realtek ALC257, 17AA:230D | [#97](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/97) |

If you test it on other hardware, please
[open a device report](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/new?template=device-report.yml)
whether it works or not.

## Install

The script needs Python 3, [NumPy](https://numpy.org/), and
[SciPy](https://scipy.org/).

PipeWire's `pw-dump` is also required by the script's `--autoload`. The PipeWire
scripts need it whenever they auto-detect your speaker sink, which is every run
without `--target-sink`. It ships in the same package as the daemon on Debian,
Ubuntu and Arch. Fedora, openSUSE and Alpine split the command-line tools into
their own package. If it's missing, the scripts name its package for your
distribution.

[Rich](https://github.com/Textualize/rich) and
[rich-argparse](https://github.com/hamdanal/rich-argparse) are optional. With
them, the script renders its output and `--help` with semantic colors. Without
them, everything works in plain monochrome.
[argcomplete](https://github.com/kislyuk/argcomplete) is optional too, for
[shell tab-completion](#shell-tab-completion).

<details>
<summary>Install commands for your distro</summary>

- **Debian/Ubuntu/Mint/Pop!_OS:**
  `sudo apt install python3-numpy python3-scipy python3-rich python3-rich-argparse`
- **Fedora/RHEL/Rocky/Alma:**
  `sudo dnf install python3-numpy python3-scipy python3-rich python3-rich-argparse`
- **openSUSE:**
  `sudo zypper install python3-numpy python3-scipy python3-rich python3-rich-argparse`
- **Arch/Manjaro/EndeavourOS:**
  `sudo pacman -S python-numpy python-scipy python-rich python-rich-argparse`
- **Alpine:** `sudo apk add py3-numpy py3-scipy py3-rich` — Alpine has no
  rich-argparse package, so `--help` stays plain there
- **Gentoo:**
  `sudo emerge dev-python/numpy dev-python/scipy dev-python/rich dev-python/rich-argparse`
- **NixOS:**
  `nix-shell -p "python3.withPackages (ps: with ps; [ numpy scipy rich rich-argparse ])"`

</details>

A venv works too, if your distro isn't listed or you'd rather not touch system
packages:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` also includes `pytest` and `pytest-xdist`, so you can run the
test suite with `pytest tests/` from the same venv. pytest won't start without
xdist 3.2 or newer, because `pyproject.toml` passes `-n auto --dist worksteal`.

## Usage

### Command-line options

**Tuning input** — the script auto-discovers a tuning source when you pass
neither an XML path nor `--windows`. It probes the mounted Windows partitions in
`/proc/mounts` and the current directory.
- `xml_file` — optional positional path to the Dolby DAX3 tuning XML, such as
  `DEV_0287_SUBSYS_*.xml`
- `--windows DIR` — auto-discover the tuning XML from a mounted Windows
  directory, by matching the audio codec subsystem ID from `/proc/asound`
- `--best-guess` — if auto-detection finds no exact hardware match, fall back to
  the only internal-speaker tuning whose manufacturer is present. When more than
  one qualifies, it lists the candidates to pick one with the positional XML
  path. Reach for it when a SoundWire device reports "No matching DAX3 tuning
  XML found".

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
  [Troubleshooting](#troubleshooting-a-preset-that-sounds-like-nothing) below.

**Profile selection**
- `--endpoint TYPE` — endpoint type (default: `internal_speaker`)
- `--mode MODE` — endpoint operating mode (default: `normal`). Yoga-class
  convertible laptops ship distinct tunings per hinge pose. If `--list` shows
  them for your device, try `--mode tablet`, `stand`, `tent`, or `lid_close`.
- `--profile TYPE` — profile type, such as `dynamic`, `music` or `voice`
  (default: first profile)
- `--all-profiles` — generate presets for all profiles in the selected
  endpoint/mode

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
  [Disabling and enabling filters](#disabling-and-enabling-filters) below.
- `--enable NAME` — switch on an optional stage the preset leaves off
  (repeatable). Valid names: `autogain`, `level-restore`, `virtual-bass`. See
  [Disabling and enabling filters](#disabling-and-enabling-filters) below.
- `--volmax-slot {input-gain,output-gain}` — where the `volmax-boost` loudness
  gain is injected. The default `input-gain` runs it through the per-band
  regulator so loud bass doesn't distort (issue
  [#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23)).
  `output-gain` is the older placement: an opt-out for A/B, or a way to bring
  back bass and loudness an aggressive regulator takes away (issue
  [#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44)).
  See [Disabling and enabling filters](#disabling-and-enabling-filters).

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

### Troubleshooting: a preset that sounds like nothing

If you loaded a generated preset in EasyEffects and hear no difference from
bypass, check the EasyEffects setup before suspecting the preset. Each of the
following makes a correct preset inaudible. Run:

```
python3 dolby_to_easyeffects.py --doctor
```

It checks the common causes and prints a pasteable report:

- **EasyEffects 7.** Version 8 changed the preset format. On EasyEffects 7 the
  speaker-correction filter loads nothing, so the preset is effectively
  bypassed. This repo targets EasyEffects 8.x. If your distro ships 7, install
  the [Flatpak](https://flathub.org/apps/com.github.wwmm.easyeffects), as the
  note at the top says.
- **Wrong install location.** The presets were written to the Flatpak path while
  you run the native package, or vice versa. EasyEffects never sees them.
- **A missing impulse file.** The convolver references a `.irs` that isn't in
  the irs directory, so the speaker correction is silent.
- **No Dolby preset selected**, or EasyEffects' global bypass is on. Its switch
  is the highlighted top-left toggle below. If your output is a headset, HDMI or
  Bluetooth, having `Nothing` selected is the expected
  [bypass fallback](#autoload), not a fault. `--doctor` says so, and reports
  which preset your speakers autoload instead.
- **EasyEffects not running in the background.** The preset only processes audio
  while EasyEffects is active, so it can vanish after you close the window or
  reboot. In EasyEffects → Preferences → Background Service, shown below, enable
  *Enable service mode* and *Autostart on login*. `--doctor` reports whether
  both are set.

![EasyEffects' global on/off toggle, highlighted at top left. If it's off, every preset is bypassed.](docs/images/ee-global-bypass.jpg)

![EasyEffects Background Service preferences. Enable service mode and autostart on login so the preset keeps applying after you close the window or reboot.](docs/images/ee-background-service.jpg)

A normal generation run also warns at the end if it detects an EasyEffects
version that can't use the presets it just wrote. To check your version
directly, open EasyEffects' About dialog:

![Checking the EasyEffects version](docs/images/ee-version.jpg)

Everything above is about the EasyEffects setup. On the
[PipeWire filter-chain](#pipewire-filter-chain-instead-of-easyeffects) route,
run `python3 dolby_to_pipewire.py --doctor` instead. It checks that route's own
failures: chains stacked on one sink, a conf that didn't load, a missing impulse
file, and a target sink that's gone.

### Troubleshooting: correct but too quiet

If the preset sounds right but quieter than Windows, part of the gap is
expected. Dolby's dynamic volume leveler ships bypassed here, because without
Dolby's content analysis it distorts on quiet→loud transitions
([why](docs/design-notes.md#why-autogain-is-bypassed-by-default)). Try these in
order:

- **Re-run the script with `--enable autogain`.** The volume leveler ships
  bypassed by default and carries most of the loudness gap: ~+9 dB measured on
  program material. The trade-off: without Dolby's content analysis, it can
  audibly saturate when loud sound arrives over a quiet background. That is why
  it isn't the default. If you hear that, drop the flag again. For still more
  loudness, raise the Autogain *Target* a few dB in the EasyEffects GUI, at
  increased saturation risk. If you instead enable Autogain by hand on a preset
  generated before this option existed, also raise its *Silence threshold* to
  about −50 dB. Otherwise, sounds arriving after silence will crackle.
- **If it is quieter than with the preset switched off entirely**, not just
  quieter than Windows, re-run with `--enable level-restore`. The flag hands
  back the level the preset loses below bypass. **Experimental**: the restored
  level also drives the final limiter harder. On the one device that has
  listened, loud speech picked up audible artifacts. If yours does too, add
  `--disable volmax` or drop the flag. Report either way on
  [#50](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/50).
  The script's end-of-run menu offers this flag only on tunings where it would
  change the level. The `level-restore` row in
  [Disabling and enabling filters](#disabling-and-enabling-filters) explains
  why the preset can play below bypass.
- **Allow volume above 100%** in your desktop environment. GNOME:
  `gsettings set org.gnome.desktop.sound allow-volume-above-100-percent true`.
  KDE Plasma: volume applet settings → *Raise maximum volume*. Any environment:
  `wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.25`, or pavucontrol.
  Over-amplification is digital gain applied after the preset's limiter, so
  extreme values can clip.
- **Check mixer levels.** In `alsamixer`, set Master/PCM/Speaker to 100%.
- **On the PipeWire path, check which output is selected.** If your sound
  settings show the filter chain itself, `Dolby-… (speaker filter)`, instead of
  your speakers, both volumes apply. Pick your speakers, since the chain is
  inserted into them automatically. `python3 dolby_to_pipewire.py --doctor`
  reports which is selected.
- On a device whose regulator is aggressive, `--volmax-slot output-gain` can
  bring back bass and a little loudness that the per-band limiter takes away.
  This was confirmed by ear on one device
  ([#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44)).
  See [Disabling and enabling filters](#disabling-and-enabling-filters).
- **Speakers thin and quiet even with EasyEffects off.** Run `--speaker-info`.
  If it flags an amplifier-firmware error, your distro lacks this machine's
  speaker firmware, which no preset can fix
  ([background](docs/cross-device-findings.md)).
  [Issue #27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27)
  links a worked, device-specific example of extracting the firmware from the
  Windows driver.

### Disabling and enabling filters

If the generated preset has audible artifacts on your hardware, you can rebuild
it without specific filters rather than hand-editing the chain inside
EasyEffects. Such artifacts include saturation, pumping, harsh highs and
uncomfortable stereo width. Repeat `--disable NAME` as many times as needed:

| Name | What to try if you hear... |
|------|----------------------------|
| `volmax` | Loud parts distort or sound crushed. Drops the static ~+6 dB `volmax-boost` loudness gain. *The default `--volmax-slot input-gain` already handles distortion on loud **low** frequencies. If the preset instead sounds bass-light, with more bass when you switch it off, try `--volmax-slot output-gain`, confirmed by ear on one device ([#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44)).* |
| `mbc` | A compressed or "squashed" character you don't like. Drops the multi-band dynamics processor, which has 1–4 bands depending on profile. |
| `regulator` | The volume audibly wobbles or surges on its own. Drops the per-band limiter. If `volmax` is enabled, it falls back to the brickwall limiter's input-gain. |
| `coupled-bands` | The loudest moments feel clamped or lose impact. Drops the zones the tuning leaves at full scale but marks non-isolated, which the per-band limiter covers by default. **Not yet validated by ear**: the engaged path has been neither captured nor heard ([docs/reference.md](docs/reference.md)). |
| `autogain` | Loudness pumping tied to the content: quiet passages swell, then duck when things get loud. Drops the volume leveler, which runs by default only on SoundWire speakers. This mirrors `--enable autogain` below. |
| `bass-enhancer` | Bass sounds artificial or distorted on SoundWire devices. Only emitted for SoundWire speakers. |
| `dialog` | Vocals feel over-boosted or harsh in the presence region. Drops the 2.5 kHz speech-band EQ. |
| `high-shelf` | Harsh or sibilant high frequencies on devices whose tuning includes a type-3 shelf. In Lenovo AIO-RTK XMLs, it sits around 2.7 kHz at +2–5 dB. **Experimental** path: the reproduction of the Dolby tuning is numerically verified, but not yet audibly validated. Feedback welcome. |
| `lo-pass` | Highs sound rolled off or dull on devices whose tuning includes a type-6/8 low-pass. That is rare: a few ALC274 SKUs. **Experimental**, with the same caveat as `high-shelf`. |

Some filters ship in the preset but inactive. `--enable NAME` switches them on:

| Name | What to try if you hear... |
|------|----------------------------|
| `autogain` | The preset sounds right but noticeably quieter than Windows. Turns on the volume leveler. See [Troubleshooting: correct but too quiet](#troubleshooting-correct-but-too-quiet). |
| `level-restore` | The preset is quieter than with it switched off entirely, and thin with it. The impulse response is normalised so its loudest band sits at 0 dB, which drops everything else below unity. On tunings whose peak exceeds their `volmax-boost`, the result plays below bypass. This hands that level back. **Experimental**: it also feeds the peak into the final limiter. On the one device that has listened, loud speech picked up audible artifacts. If yours does too, try `--disable volmax`. Report either way on [#50](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/50). |
| `virtual-bass` | Bass feels thinner than it did on Windows, on HDA internal speakers. Windows DAX synthesizes harmonics that suggest bass small drivers can't physically produce. This records the XML's virtual-bass parameters so the [PipeWire converter](#pipewire-filter-chain-instead-of-easyeffects) can build that stage. EasyEffects itself can't express it, so the EE preset's audio is unchanged. You need `dolby_to_pipewire.py` to hear it. **Experimental**: measured close to DAX on one device. Report what you hear on [#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14). |

Convolver, PEQ, and the final brickwall limiter can't be toggled from the CLI.
They're the FIR correction, speaker PEQ, and safety net.

To work out *which* stage you're hearing, switch effects off one at a time in
the EasyEffects window instead of rebuilding. Turning off **Convolver** isolates
the speaker-correction curve from everything dynamic. Turning off the
**Multiband Compressor** that carries the per-band limiter also takes out the
loudness boost riding it. Most tunings produce only one Multiband Compressor,
which is that limiter. Where Dolby's own multi-band compressor is also present,
you get two, and the limiter is the second of them. The run's own output names
which stages it built.

## Advanced

### Autoload

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
[Troubleshooting](#troubleshooting-a-preset-that-sounds-like-nothing). Pass
`--autoload-sink NODE_NAME` to bind a sink yourself, or `--no-autoload-bypass`
to skip the bypass if you manage it yourself.

![EasyEffects autoload: Dolby-Balanced bound to the speaker output, with Nothing as the global fallback preset](docs/images/ee-autoload.jpg)

<details>
<summary>How speaker detection and the bypass fallback work</summary>

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

</details>

### PipeWire filter-chain instead of EasyEffects

The same tuning runs as a self-contained PipeWire filter-chain `.conf`. It needs
no GUI, uses less CPU, runs set-and-forget, and works whether or not EasyEffects
is installed. `dolby_to_pipewire.py` produces it in one command by driving the
preset generator and the `ee_to_pipewire.py` converter.
[`docs/ee-to-pipewire.md`](docs/ee-to-pipewire.md) has the design notes and
equivalence measurements.

#### Quick start (PipeWire)

One command goes from tuning XML to an active sink. It generates the EasyEffects
preset in a throwaway temporary directory, so nothing is installed under
`~/.local/share/easyeffects`. It converts the preset to a conf and copies the
matching `.irs` beside it. Then it restarts PipeWire and verifies the sink.

Prerequisites are NumPy and SciPy, which the generator needs, and the LSP/Calf
LV2 plugins. [Install](#install) and *Plugin dependencies and
validation* below cover them. The tuning XML is located the same way as in the
main [Quick start](#quick-start): auto-discovery, `--windows`, or
[manual extraction](#extracting-the-xml).

```bash
python3 dolby_to_pipewire.py         # add --no-activate to restart PipeWire yourself
```

The default converts the **Balanced** voicing, which is Dolby's default.
`--variant detailed` and `--variant warm` pick the others.
`--variant all --target-sink ''` creates one sink per variant, so you can A/B
them from sound settings. Smart-filter routing has to be off for that. Otherwise
PipeWire runs the three in series instead of offering a choice. Expect a subtle
difference. The three voicings sit at most about 1 dB apart, in a broad tilt
through the mids and treble, so an A/B that isn't gapless may not show it. The
voicing curves are Dolby-global, and the device-specific correction applies
under every one: [details](docs/cross-device-findings.md).

The conf lands in `~/.config/pipewire/pipewire.conf.d/`, or under
`$XDG_CONFIG_HOME` instead if you've set it. It attaches transparently to your
internal-speaker sink. Apps keep targeting the speaker, while HDMI, Bluetooth
and USB outputs bypass it automatically. The conf is stereo only. It covers the
convolver, PEQ, dialog, multiband compressor, regulator and limiter, plus
`bass_enhancer` and `stereo_tools`. An active `autogain` volume leveler is
translated too. Only 4-channel upmix isn't. See
[Limitations](docs/ee-to-pipewire.md#limitations--known-gaps).

- **Already run EasyEffects?** Before activating, quit it and stop it starting
  again: turn off its Background Service and autostart, or remove its autoload.
  Otherwise both chains process the audio at once. Restarting PipeWire stops
  EasyEffects for the session anyway, along with whatever it was applying.
- **No sound, or it doesn't sound right?**
  `python3 dolby_to_pipewire.py --doctor` reports what's installed, what
  PipeWire is doing with it, and what to do about each problem it finds.
- **To remove the filter:** delete
  `~/.config/pipewire/pipewire.conf.d/Dolby_Balanced.conf` and the `.irs` beside
  it, then restart pipewire.

<details>
<summary>Manual two-step (keep the EasyEffects preset files, full flag surface)</summary>

The wrapper is a thin orchestrator over the two converters. Run them yourself to
keep the preset JSON and `.irs` under `~/.local/share/easyeffects`, or for flags
the wrapper doesn't expose: `--node-name`, `--target-object`, `--no-copy-irs`
and autoload.

```bash
# 1. Generate the preset JSON (no EasyEffects install required)
python3 dolby_to_easyeffects.py            # omit --autoload; that only wires EE

# 2. Convert it to a filter-chain conf (the matching .irs is copied beside it)
#    Step 1 prints where it wrote the preset — on a Flatpak EasyEffects that
#    is under ~/.var/app/com.github.wwmm.easyeffects/, not the path below.
python3 ee_to_pipewire.py ~/.local/share/easyeffects/output/Dolby-Balanced.json

# 3. Activate
systemctl --user restart pipewire pipewire-pulse

# 4. Confirm the sink is loaded
pw-cli ls Node | grep Dolby_Balanced
```

`ee_to_pipewire.py` itself adds no Python dependencies. At runtime it needs only
the LSP/Calf LV2 plugins.

</details>

<details>
<summary>Plugin dependencies and validation</summary>

The chain loads LV2 plugins from your system. LSP provides the PEQ, MBC,
regulator, limiter and virtual-bass filters. Calf provides the `bass_enhancer`
and `stereo_tools` stages and the virtual-bass saturator, when the preset uses
them. On Debian-family systems EasyEffects pulls in LSP but not Calf, which is
listed as an alternative. Elsewhere, check both. If they're missing, the chain
won't load in PipeWire. The converter names the missing package for your
distribution. Install the **LV2 builds**, because the base `lsp-plugins` and
`calf` packages don't all ship the `.lv2` bundle PipeWire loads:

- **Debian/Ubuntu/Mint/Pop!_OS:**
  `sudo apt install lsp-plugins-lv2 calf-plugins`
- **Fedora/RHEL/Rocky/Alma:**
  `sudo dnf install lsp-plugins-lv2 lv2-calf-plugins`
- **openSUSE:** `sudo zypper install lv2-lsp-plugins`, plus Calf from
  [Packman](https://packman.links2linux.de/), since openSUSE's own repositories
  don't carry it
- **Arch/Manjaro/EndeavourOS:** `sudo pacman -S lsp-plugins-lv2 calf`
- **Alpine:** `sudo apk add lsp-plugins-lv2 calf-lv2`
- **Gentoo:** `sudo emerge media-libs/lsp-plugins media-plugins/calf`, with
  `USE=lv2` for `media-plugins/calf`
- **NixOS:** add `pkgs.lsp-plugins` and `pkgs.calf` to
  `environment.systemPackages`, then run `nixos-rebuild switch`. A `nix-shell`
  won't do, since PipeWire runs outside it

Add your distribution's `lv2info` if you want the converter to check the plugin
set before it writes anything:

- **Debian/Ubuntu/Mint/Pop!_OS:** `sudo apt install lilv-utils`
- **Fedora/RHEL/Rocky/Alma:** `sudo dnf install lilv`
- **openSUSE:** `sudo zypper install lilv`
- **Arch/Manjaro/EndeavourOS:** `sudo pacman -S lilv-tools`
- **Alpine:** `sudo apk add lilv`
- **Gentoo:** `sudo emerge media-libs/lilv` with `USE=tools`
- **NixOS:** `nix-shell -p lilv`. The converter runs `lv2info` itself, so this
  one needn't be visible to PipeWire

You shouldn't need these lists on a run that fails. The converter prints
whichever line matches your `/etc/os-release`, derivatives included. On a
distribution it can't place, it lists them all and points back here.

Before writing the conf, the converter runs `lv2info` to validate it against
installed plugin metadata. If `lv2info` can't load a plugin, the run **refuses
to write the conf** and names the package to install. PipeWire couldn't load
that plugin either, because both resolve plugins through the same library.
`lv2info` itself is optional. PipeWire needs the lilv *library*, not the
command, so a machine with LSP and Calf installed runs the chain without it.
Without it, nothing checks the plugin set before the conf is written. A missing
package then shows up only as a sink that never appears after the restart. The
run says so and names the package that would have caught it. Pass
`--no-validate` to skip the check entirely.

The run also uses `spa-json-dump` to read the conf back. It uses `pw-cli` and
`pw-dump` to find your speaker sink and confirm the chain loaded. These ship in
the same package as the daemon on Debian, Ubuntu and Arch. Fedora splits them
into `pipewire-utils`. openSUSE and Alpine put `pw-cli` and `pw-dump` in
`pipewire-tools`, and `spa-json-dump` in `pipewire-spa-tools`. The run names
whichever is missing.

A chain that can't load doesn't stop PipeWire from starting. The conf marks its
module `nofail`, so PipeWire skips it and your audio keeps working unprocessed
([#71](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/71)).

</details>

<details>
<summary><code>dolby_to_pipewire.py</code> command-line options</summary>

Inherited flags behave exactly as in the script that owns them. The wrapper
shares their definitions with `dolby_to_easyeffects.py` for generation and
`ee_to_pipewire.py` for conversion.

**Tuning input** — the script auto-discovers a tuning source when you pass
neither an XML path nor `--windows`. It probes the mounted Windows partitions in
`/proc/mounts` and the current directory.
- `xml_file` — optional positional path to the Dolby DAX3 tuning XML, such as
  `DEV_0287_SUBSYS_*.xml`
- `--windows DIR` — auto-discover the tuning XML from a mounted Windows
  directory, by matching the audio codec subsystem ID from `/proc/asound`
- `--best-guess` — fall back to a manufacturer-matched tuning when
  auto-detection finds no exact hardware match

**Inspection**
- `--list` — show available endpoints and profiles in the XML, then exit
- `--speaker-info` — report detected audio hardware and speaker layout, then
  exit
- `--doctor` — report the state of the installed PipeWire filter chain, plus
  your audio hardware, then exit

**Profile selection**
- `--endpoint TYPE` — endpoint type from the XML (default: `internal_speaker`)
- `--mode MODE` — endpoint operating mode (default: `normal`)
- `--profile TYPE` — profile type, such as `dynamic`, `music` or `voice`
  (default: first profile)
- `--all-profiles` — convert every profile except `off` in the selected
  endpoint/mode, each as its own sink. Needs `--target-sink ''`.

**Variant**
- `--variant {balanced,detailed,warm,all}` — which IEQ voicing to convert
  (default: `balanced`, Dolby's default voicing). `all` creates one sink per
  variant for A/B from sound settings, and needs `--target-sink ''`.

**Routing**
- `--target-sink NODE_NAME` — hardware sink the filter attaches to as a
  WirePlumber smart filter (default: auto-detect the internal-speaker sink).
  `''` disables smart-filter routing.
- `--target-object NODE_NAME` — bind the chain's playback to a specific
  downstream node instead of letting WirePlumber choose. Set automatically when
  installing more than one sink.

**Output**
- `--prefix PREFIX` — prefix for preset/sink names (default: `Dolby` →
  `Dolby_Balanced`, etc.)
- `--output-dir DIR` — directory for the generated `.conf` and `.irs` copy
  (default: `~/.config/pipewire/pipewire.conf.d`)
- `--force` — overwrite existing conf and `.irs` files

**Activation**
- `--no-activate` — don't restart PipeWire or verify the sink; print the manual
  activation steps instead

**Filter tweaks**
- `--disable NAME` — drop a filter from the generated chain (repeatable); same
  names as `dolby_to_easyeffects.py`
- `--enable NAME` — switch on an optional stage the chain leaves off
  (repeatable), such as `autogain`
- `--volmax-slot {input-gain,output-gain}` — which regulator gain slot carries
  the static volmax boost (default: `input-gain`)

**General**
- `--verbose` (alias `-v`) — print the generator's full frequency tables; same
  as `dolby_to_easyeffects.py`
- `--dry-run` — report where each conf would be written without installing it.
  Nothing is written outside the staging directory, and PipeWire is not
  restarted. To keep the confs, use `--output-dir DIR --no-activate` instead.
- `--no-validate` — skip the `lv2info` schema self-check
- `--no-color` — disable colored terminal output
- `--version` — print the version and exit

</details>

<details>
<summary><code>ee_to_pipewire.py</code> command-line options</summary>

- `preset` — positional path to the EasyEffects preset JSON that
  `dolby_to_easyeffects.py` writes, such as
  `~/.local/share/easyeffects/output/Dolby-Balanced.json`. Optional with
  `--doctor`.

**Inspection**
- `--doctor` — report the state of the installed filter chain, then exit. It
  covers stacked chains, confs that didn't load, a missing impulse response and
  a target sink that no longer exists.

**Routing**
- `--target-sink NODE_NAME` — hardware sink the filter attaches to as a
  WirePlumber smart filter (default: auto-detect the internal-speaker sink with
  the same probe as `--autoload`). Pass an empty string, `''`, to disable
  smart-filter routing and emit a v1 virtual sink that apps target directly.

**Output**
- `--output PATH` — output `.conf` path (default:
  `~/.config/pipewire/pipewire.conf.d/<node-name>.conf`)
- `--node-name NAME` / `--node-description DESC` — override the sink's node name
  and human-readable label (default: derived from the preset filename stem).
  Converting several presets then yields distinct sinks.
- `--force` — overwrite the output conf if it already exists

**Impulse response**
- `--irs-dir DIR` — directory holding the `.irs` referenced by the preset's
  convolver (default: the EasyEffects `irs` directory, Flatpak or native)
- `--no-copy-irs` — leave the conf pointing at the original EE-side `.irs`
  instead of copying it beside the conf. The conf then pins that one file.
  Re-run this converter after regenerating a preset whose sound changed, because
  its impulse gets a new name.

**General**
- `--no-validate` — skip the `lv2info` schema self-check. The check also refuses
  a conf naming a plugin `lv2info` can't load.
- `--dry-run` — report where the conf and impulse response would be written
  without writing them. To keep the conf, use `--output` instead.
- `--skip-next-steps` — replace the post-write next-steps checklist with a
  one-line activation pointer, for callers that handle activation themselves.
  `dolby_to_pipewire.py` passes it automatically.
- `--no-color` — disable colored terminal output. Output is already plain when
  `rich` isn't installed.
- `--version` — print the version and exit

</details>

#### Which should I use?

The two paths sound the same:
[measurements](docs/ee-to-pipewire.md#equivalence-to-the-ee-chain) show them
equivalent. Choose on everything else:

- **Features → EasyEffects.** A GUI to tweak and switch presets live. The
  `autogain` volume leveler isn't EE-only: the PW side
  [translates it too](docs/ee-to-pipewire.md#plugin-coverage). Autogain runs by
  default on SoundWire devices. On HDA the generator leaves it bypassed unless
  you pass `--enable autogain`, because its loudness boost can audibly
  [saturate](docs/design-notes.md#why-autogain-is-bypassed-by-default) on
  quiet-background content.
- **Lightness / headless / set-and-forget → the PW conf.** No GUI, no extra
  daemon. Running `Dolby-Balanced` at 48 kHz on the X1 Yoga development device,
  the filter-chain costs **~11 % fewer CPU cycles** and **~3.5× less RAM** than
  running EasyEffects. RAM use is ~78 MB vs ~270 MB. The EasyEffects process is
  mostly Qt/GUI. Both are light in absolute terms. The DSP takes roughly a tenth
  of one CPU core, well under 1 % of a typical multi-core laptop. So the memory
  and feature differences usually matter more than the CPU one.
- **Latency → a wash.** Both add zero latency over the PipeWire quantum. The FIR
  is minimum-phase. Both ran xrun-free at 1024/48 kHz.

Those CPU/RAM figures are device-specific. Reproduce them on your own hardware
with [`tools/measure_perf/`](tools/measure_perf/). It uses a frequency-invariant
`perf`-cycle measurement, since laptop clocks don't hold still.

### Extracting the XML

The easiest way is `--windows`, which auto-discovers the XML from a mounted
Windows partition. The script reads your audio codec's device and subsystem IDs
from `/proc/asound` and matches them against the XMLs in the DriverStore.

**No Windows partition, on a Lenovo laptop?** Run
`tools/fetch_driver/get_lenovo_dax_xml.py`. It resolves the audio-driver package
for your machine type from Lenovo's update catalog, downloads and
checksum-verifies it, and extracts the DAX3 tuning XML. It needs
[`innoextract`](https://constexpr.org/innoextract/install). It then prints the
directory to pass to either converter:

```bash
python3 tools/fetch_driver/get_lenovo_dax_xml.py --dry-run   # show what it resolved
python3 tools/fetch_driver/get_lenovo_dax_xml.py             # fetch, verify, unpack
```

From the repo root, the next step is just `python3 dolby_to_easyeffects.py`,
with no path to pass. The fetcher unpacks into the repo's `driver-cache/`, which
the converters' autoprobe already covers. It prints the exact command to run,
with `--windows` filled in on the rare occasions it's needed.

<details>
<summary>Manual extraction, or from a Lenovo driver EXE without a Windows partition</summary>

To extract the XML manually, take it from the Windows driver package at:
```
C:\Windows\System32\DriverStore\FileRepository\dax3_ext_*.inf_*\DEV_*_SUBSYS_*.xml
```
Match **both** parts of the filename to your codec:

- `DEV_` to its device id, the last four hex digits of `Vendor Id`: `0x10ec0287`
  → `DEV_0287`.
- `SUBSYS_` to its subsystem ID.

`cat /proc/asound/card*/codec* | grep -E 'Vendor|Subsystem'` shows both IDs. The
subsystem alone is not enough, because Lenovo reuses subsystem IDs across
different codecs. Picking the other codec's tuning sounds clearly wrong. See the
[details](docs/cross-device-findings.md). You don't need the `_settings.xml`
companion file, which contains UI/profile defaults.

**From a Lenovo driver EXE.** Install
[`innoextract`](https://constexpr.org/innoextract/install). Download the Lenovo
audio driver EXE, for example `n4ba126w.exe`, into this project directory. Then
run from the project root:

```bash
# 1. Extract only the Dolby tuning XMLs into ./driver-cache/
innoextract -I 'code$GetExtractPath$/Dolby/03_dax_ext' -d ./driver-cache ./n4ba126w.exe

# 2. Generate presets (autoprobe finds the extracted XMLs automatically)
python3 dolby_to_easyeffects.py --autoload
```

If the autoprobe reports ambiguity, for example with several extracted driver
trees, pass `--windows ./driver-cache` to point it at the one you want.

</details>

### Auto-detection notes

<details>
<summary>How the script finds your XML, EasyEffects install, and codec</summary>

**Windows partition or extracted DriverStore.** Omitting `--windows` and the
positional XML triggers the autoprobe. It enumerates the NTFS-family mountpoints
in `/proc/mounts`: `ntfs`, `ntfs3` and `fuseblk`. It keeps any whose DriverStore
contains `dax3_ext_*.inf_*` subdirs, whether a full system root like
`/mnt/windows/Windows` or a drive-root mount like `/mnt/c`.

If nothing mounted matches, it falls back to a bounded walk of the current
directory. It looks for any directory whose files include a Dolby-shaped XML:
`DEV_*_SUBSYS_*.xml`, `SOUNDWIRE_*_SUBSYS_*.xml` or `SDW_*_SUBSYS_*.xml`,
excluding `_settings.xml` companions. That covers hand-organised collections and
the raw `innoextract` layout,
`./driver-cache/code$GetExtractPath$/Dolby/03_dax_ext/`. Neither needs a
`dax3_ext_*.inf_*` rename. The walk skips hidden directories, doesn't follow
symlinks, and is depth-capped.

A single unambiguous match is used. When several match, the autoprobe narrows to
those containing an XML for your detected audio hardware. It uses that one if
exactly one survives. Otherwise it errors with the shortlist, so you can pick
one via `--windows DIR`.

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

**SoundWire codecs on newer Intel platforms.** Auto-detection also handles
SoundWire-based audio on Lunar Lake / Panther Lake and later, Meteor Lake, and
some Tiger/Alder Lake SKUs. That includes Qualcomm Aqstic and Cirrus cs35l56
smart-amp platforms. The script reads device IDs from
`/sys/bus/soundwire/devices/` and the PCI subsystem ID of the HD Audio
controller from `/sys/class/sound/card*/device`. It matches them against Dolby
filenames of the form
`SOUNDWIRE_MAN_<man>_FUNC_<func>_SUBSYS_<device><vendor>.xml`, for example
`SOUNDWIRE_MAN_025D_FUNC_1318_SUBSYS_233917AA.xml`.

The PCI subsystem is the per-device key. On Cirrus platforms the `FUNC` token is
a device id that needn't equal the Linux SoundWire part id, as
[cross-device-findings](docs/cross-device-findings.md) shows. So if no part
matches, the script falls back to the PCI subsystem + manufacturer.

`--windows` accepts any of these:

- a full Windows system root, such as `/mnt/windows/Windows`
- a drive-root mount, such as `/mnt/c`, where the script looks for a
  case-insensitive `Windows/` child
- an already-extracted DriverStore directory containing `dax3_ext_*.inf_*`
  subfolders directly

</details>

### Shell tab-completion

All three scripts support tab-completion through the optional
[argcomplete](https://github.com/kislyuk/argcomplete) package. They complete
their flags, the `--disable` / `--enable` / `--variant` value lists, file and
directory paths, and your live PipeWire sink names for `--autoload-sink` and
`--target-sink`. Install `python3-argcomplete` on Debian/Ubuntu/Fedora/openSUSE,
`python-argcomplete` on Arch, or `py3-argcomplete` on Alpine.

Argcomplete is off until you register it. Add this line to `~/.bashrc`, or to
`~/.zshrc` *after* its `compinit` line:

```bash
eval "$(activate-global-python-argcomplete --dest=-)"
```

Run the scripts **directly** to get completion: `./dolby_to_easyeffects.py …`.
In bash, that hook also covers the `python3 dolby_to_easyeffects.py …` form used
elsewhere in this README. In zsh it does not, because zsh's own `python`
completion takes precedence over it.

To scope completion to these three scripts rather than every argcomplete-enabled
program, run `eval "$(register-python-argcomplete dolby_to_easyeffects.py)"`
once per script instead. That form covers `./dolby_to_easyeffects.py` only, in
both shells.

## How it works

The script parses the DAX3 XML's two processing stages. It emits a minimum-phase
FIR impulse response plus a chain of EasyEffects plugins, at zero added latency.
Every parameter traces back to an XML field.

```mermaid
flowchart LR
  XML["DAX3 tuning XML<br/>(Windows driver)"] --> P["dolby_to_easyeffects.py<br/>parse CP + VLLDP"]
  P --> FIR[".irs FIR<br/>impulse response"]
  P --> PRM["plugin params<br/>EQ · MBC · regulator · limiter"]
  FIR --> EE["EasyEffects preset"]
  PRM --> EE
  EE --> SPK(["device speakers"])
```

The preset is up to eight plugins, in this order:

1. Convolver: FIR speaker correction
2. Bass Enhancer: SoundWire only
3. Equalizer: speaker PEQ
4. Dialog Enhancer
5. Autogain: bypassed by default
6. Multiband Compressor
7. Regulator: per-band limiter
8. Limiter: brickwall safety net

![A generated preset loaded in EasyEffects, convolver through limiter](docs/images/ee-chain-loaded.jpg)

For the full detail, see the docs:

- **[docs/reference.md](docs/reference.md)** — the current-state reference:
  every XML→parameter mapping, the plugin chain in detail, units, profile
  differences, which mappings are DAX-validated, and what's deliberately not
  implemented, and why.
- **[docs/design-notes.md](docs/design-notes.md)** — the research log: why the
  chain is ordered this way, the FIR cepstral construction, what was attempted
  and rejected, and the open threads worth picking up.
- **[docs/cross-device-findings.md](docs/cross-device-findings.md)** — empirical
  analysis across ~2,800 DAX3 files: which DSP blocks are universal vs.
  device-specific. [docs/corpus.md](docs/corpus.md) describes what that
  collection is made of.

## Running the tests

The `pytest` suite under `tests/` covers the converter without requiring any
proprietary Dolby tuning data as input.

```bash
pytest tests/
```

The bulk of the suite needs no setup. It covers
DSP math, output schema, `--disable`/argparse behavior, and a dedicated
regression suite for every shipped-bug "trap". It uses synthetic, hand-built
inputs only. No real DAX3 XML is shipped or checked in.

The corpus tier under `tests/corpus/` runs the full pipeline against a corpus of
real DAX3 XMLs: parse → FIR → preset → IRS. It auto-discovers them the same way
the main script does. That means NTFS-family mountpoints whose DriverStore
contains `dax3_ext_*.inf_*`, plus a bounded walk of the current working
directory for any folder containing Dolby-shaped XMLs. To override, point it at
a specific directory:

```bash
ATMOS_CORPUS_DIR=/path/to/dax3/xmls pytest tests/corpus/
```

The corpus tier skips cleanly if no corpus is reachable and `ATMOS_CORPUS_DIR`
is unset.

The heaviest tiers walk every endpoint/profile/curve combination and validate
every distinct discovered XML's generated PipeWire conf through `lv2info`. Both
are gated behind `--run-slow` or `ATMOS_RUN_SLOW=1`. Every run fans across your
cores via [`pytest-xdist`](https://pypi.org/project/pytest-xdist/), which turns
the heavy tiers from tens of minutes into a few. `pyproject.toml` sets
`-n auto --dist worksteal`. Pass `-n 0` to force a serial run, which is what you
want alongside `-x`, `-s` or `--pdb`.

The suite catches structural regressions, such as a FIR that isn't
minimum-phase, convolver autogain accidentally re-enabled, MBC compression-mode
flipped to upward, or enums emitted as integers. It does not substitute for
listening tests after any change to the output path.

## Further reading

In-tree docs and tooling with more context:

- [docs/reference.md](docs/reference.md) — current-state reference:
  XML→parameter mappings, the plugin chain, units, profile differences, and
  what's not implemented
- [docs/design-notes.md](docs/design-notes.md) — research log: why the plugin
  chain is ordered the way it is, gain-staging rationale, why autogain is
  bypassed by default, and an empirical comparison of our generated FIR against
  DAX3's actual response on Windows
- [docs/code-organisation.md](docs/code-organisation.md) — how the two entry
  points were split into `lib/`: the module shape it landed in, and the git
  discipline that keeps `git blame -C` tracing code back through an extraction
- [docs/cross-device-findings.md](docs/cross-device-findings.md) — empirical
  analysis of ~2,800 DAX3 tuning files across Realtek, Senary, Qualcomm Aqstic,
  and SoundWire smart-amp codecs, including which DSP blocks are unmodeled
- [docs/corpus.md](docs/corpus.md) — what those tuning files are: how one is
  counted, which OEM driver package each came from, what the collection is
  skewed towards, and how to compare your own against it
- [docs/alternative-pipelines.md](docs/alternative-pipelines.md) — design
  sketches for offloading parts of the pipeline to Intel SOF DSP or running
  under PipeWire filter-chain instead of EasyEffects
- [docs/ee-to-pipewire.md](docs/ee-to-pipewire.md) — current architecture of the
  `ee_to_pipewire.py` companion converter: smart-filter routing, self-contained
  conf layout, plugin coverage, and equivalence guarantees
- [tools/measure_dax/](tools/measure_dax/) — Windows-side capture + Linux-side
  analysis scripts for measuring DAX3's actual response via WASAPI loopback.
  They reproduce the empirical comparison in `design-notes.md` on any
  Lenovo/ThinkPad with DAX3 installed.
- [tools/measure_ee/](tools/measure_ee/) — Linux-side counterpart: captures the
  live EasyEffects pipeline, with our generated preset applied, into the same
  `loopback_*.{wav,json}` schema. `tools/measure_dax/analyze.py` and
  `tools/measure_ee/compare_ee_vs_dax.py` can then overlay the EE-on-Linux
  response next to the DAX-on-Windows reference.
- [tools/measure_pw/](tools/measure_pw/) — captures and validates the PipeWire
  filter-chain rendering of the same preset by the `ee_to_pipewire.py`
  companion. Its `validate_conf.py` deterministic schema check catches
  inverted bools, unknown ports and out-of-range values without any audio
  capture. `compare_ee_vs_pw.py` and `compare_ee_vs_pw_time_domain.py` overlay
  the PW captures against the EE-side captures from `tools/measure_ee/`.
- [tools/measure_perf/](tools/measure_perf/) — measures what EasyEffects and the
  PipeWire filter-chain *cost*, in frequency-invariant `perf` CPU cycles,
  memory, and xruns. `measure_pw` proves the two delivery paths *sound* the
  same. Backs the README "Which should I use?" guidance.

## References

- [wwmm/easyeffects](https://github.com/wwmm/easyeffects) — preset format
  reference
- [shuhaowu/linux-thinkpad-speaker-improvements](https://github.com/shuhaowu/linux-thinkpad-speaker-improvements)
  — alternative approach using captured impulse responses via WASAPI loopback
- [taprobane99/Lenovo-Yoga-Slim-7x-Dolby-Linux-Audio](https://github.com/taprobane99/Lenovo-Yoga-Slim-7x-Dolby-Linux-Audio)
  — downstream port of this script's output to a PipeWire filter-chain config
  with 4-speaker upmix on Snapdragon X. See `docs/alternative-pipelines.md`
  Option 3.
- [sklynic/easyeffects-tuf-gaming-a15](https://github.com/sklynic/easyeffects-tuf-gaming-a15)
  — manual DAX3 EQ extraction for ASUS laptops
- [mister2d/thinkpad-linux-audio](https://github.com/mister2d/thinkpad-linux-audio/)
  — extended Dolby pipeline for ThinkPads, built on top of this tooling. See
  [#2](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/2).
