# Dolby Atmos Speaker Tuning to EasyEffects Presets for Linux

Bring your laptop's Windows speaker tuning to Linux. This script converts the
Dolby Atmos, Surface or Sonitude speaker tuning shipped inside Windows audio
drivers into [EasyEffects](https://github.com/wwmm/easyeffects) 8.x output
presets. Your speakers get the same speaker correction, EQ, and dynamics
processing as on Windows, at zero added latency.

Confirmed on over 50 laptops from ASUS, Framework, HP, Lenovo and Microsoft: see
[Supported devices](#supported-devices). Tested yours?
[Report it](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/new?template=device-report.yml),
whether it works or not.

Don't want to run EasyEffects? The same tuning also runs as a self-contained
PipeWire filter-chain, with no GUI and no extra daemon. See
[PipeWire filter-chain](docs/dolby-to-pipewire.md).

**Contents:** [Quick start](#quick-start) · [Install](#install) ·
[Something not right?](#something-not-right) ·
[Documentation](#documentation) ·
[Staying up to date](#staying-up-to-date) ·
[Supported devices](#supported-devices) · [How it works](#how-it-works)

## Quick start

1. Download the code. Take the *Source code (zip)* from the
   [latest release](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/releases/latest),
   unzip it, and open a terminal in the unzipped folder.

   If you use git, clone the repository instead:

   ```bash
   git clone https://github.com/antoinecellerier/speaker-tuning-to-easyeffects.git
   cd speaker-tuning-to-easyeffects
   ```

2. Install Python 3 with NumPy and SciPy. [Install](#install) has the command
   for your distribution.

3. Follow [With EasyEffects](#with-easyeffects), or
   [Without EasyEffects](#without-easyeffects) to run the tuning as a PipeWire
   filter instead.
   [Which should I use?](docs/dolby-to-pipewire.md#which-should-i-use)
   compares them.

### With EasyEffects

1. Install EasyEffects 8.x. Prefer your distribution's `easyeffects` package
   when it ships version 8, since it then updates with the rest of your
   system. Version 7 presets aren't compatible, so if it ships an older
   version, use the Flatpak instead:

   ```bash
   flatpak install flathub com.github.wwmm.easyeffects
   ```

   No `flatpak` command, or no `flathub` remote? See
   [flathub.org/setup](https://flathub.org/setup).

2. With a Windows partition mounted, run:

   ```bash
   python3 dolby_to_easyeffects.py --autoload
   ```

   With no Windows, download your laptop's audio driver from its maker's
   support site, then pass that file:

   ```bash
   python3 dolby_to_easyeffects.py ~/Downloads/your-audio-driver.exe --autoload
   ```

   `--autoload` sets EasyEffects to apply the Dolby correction on your internal
   speaker automatically. See [Autoload](docs/dolby-to-easyeffects.md#autoload)
   for details.

3. Open EasyEffects: Dolby-Balanced, or Sonitude-Music, should be selected under
   Presets, as below. If not, as with the Flatpak, pick it there. To keep the
   correction after a reboot, turn on *Enable service mode* and *Autostart on
   login* under Preferences → Background Service.

![Selecting a generated Dolby preset in EasyEffects](docs/images/ee-preset-select.jpg)

### Without EasyEffects

1. This route also needs the LSP and Calf LV2 plugins and `lv2info`:
   [install them](docs/dolby-to-pipewire.md#plugin-dependencies-and-validation)
   first.

2. The run restarts PipeWire, so sound cuts out for a moment. With a Windows
   partition mounted, run:

   ```bash
   python3 dolby_to_pipewire.py
   ```

   With no Windows, download your laptop's audio driver from its maker's
   support site, then pass that file:

   ```bash
   python3 dolby_to_pipewire.py ~/Downloads/your-audio-driver.exe
   ```

   The filter then applies each time PipeWire starts. The run names any plugin
   it needs that's missing, and prints how to remove what it installed. Check
   it later with `python3 dolby_to_pipewire.py --doctor`.

### If the script can't find your tuning

[Getting your tuning file](docs/getting-the-tuning-file.md) says where else to
find it.

To skip the automatic search, point either script at a Windows directory or a
tuning XML yourself:

```bash
python3 dolby_to_easyeffects.py --windows /mnt/windows/Windows --autoload
python3 dolby_to_easyeffects.py path/to/DEV_0287_SUBSYS_*.xml --autoload
python3 dolby_to_pipewire.py --windows /mnt/windows/Windows
python3 dolby_to_pipewire.py path/to/DEV_0287_SUBSYS_*.xml
```

### Safe to try

The scripts need no root: they refuse to run under `sudo`. By default they keep
what they write under your home directory, apart from temporary files they
delete. To remove what they installed, run the same script with
`--uninstall`. It lists what it will remove and asks first. See
[Undo](docs/dolby-to-easyeffects.md#undo) for EasyEffects, or
[PipeWire filter-chain](docs/dolby-to-pipewire.md#remove-the-filter) for the
filter.

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
[shell tab-completion](docs/shell-completion.md).

Install commands for your distro:

- **Debian/Ubuntu/Mint/Pop!_OS:**
  ```bash
  sudo apt install python3-numpy python3-scipy python3-rich python3-rich-argparse
  ```
  On older releases that don't package `python3-rich-argparse`, it's safe to
  leave it out.
- **Fedora/RHEL/Rocky/Alma:**
  ```bash
  sudo dnf install python3-numpy python3-scipy python3-rich python3-rich-argparse
  ```
  On RHEL, Rocky and Alma, `python3-rich` and `python3-rich-argparse` come from
  EPEL, so leave out whichever dnf can't find.
- **openSUSE:**
  ```bash
  sudo zypper install python3-numpy python3-scipy python3-rich python3-rich-argparse
  ```
  Leap 16.0 has no SciPy package, so use the virtual environment below there.
- **Arch/Manjaro/EndeavourOS:**
  ```bash
  sudo pacman -S python-numpy python-scipy python-rich python-rich-argparse
  ```
- **Alpine:**
  ```bash
  sudo apk add py3-numpy py3-scipy py3-rich
  ```
  Alpine has no rich-argparse package, so `--help` stays plain there.
- **Gentoo:**
  ```bash
  sudo emerge dev-python/numpy dev-python/scipy dev-python/rich dev-python/rich-argparse
  ```
- **NixOS:**
  ```bash
  nix-shell -p "python3.withPackages (ps: with ps; [ numpy scipy rich rich-argparse ])"
  ```

If your distro isn't listed or you'd rather not touch system
packages, a Python virtual environment works too:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

<a id="troubleshooting-a-preset-that-sounds-like-nothing"></a>
<a id="troubleshooting-correct-but-too-quiet"></a>
<a id="disabling-and-enabling-filters"></a>

## Something not right?

Start with `--doctor`: run `python3 dolby_to_easyeffects.py --doctor`, or
`python3 dolby_to_pipewire.py --doctor` if you use the PipeWire filter-chain.
Each reports what it finds and what to do about it.

- **No audible difference.**
  [Check the EasyEffects setup](docs/troubleshooting.md#troubleshooting-a-preset-that-sounds-like-nothing)
  before suspecting the preset.
- **Right, but quieter than Windows.**
  [Try the loudness options](docs/troubleshooting.md#troubleshooting-correct-but-too-quiet),
  such as `--enable autogain`.
- **Loud parts crushed, pumping or harsh highs.**
  [Rebuild the preset without the filter responsible](docs/filters.md).
- **"No matching DAX3 tuning XML found".**
  [Get your tuning file](docs/getting-the-tuning-file.md) another way.
- **The effect stops after you close EasyEffects or reboot.**
  [Enable its Background Service](docs/troubleshooting.md#troubleshooting-a-preset-that-sounds-like-nothing).
- **The Dolby tuning plays on HDMI, Bluetooth or a headset.** `--doctor` says
  what to change.
  [Stop it playing there](docs/dolby-to-easyeffects.md#if-the-dolby-tuning-still-plays-on-other-outputs).
- **Re-ran the script but hear no change.** The run can't reload a Flatpak
  EasyEffects, so pick the preset again in its Presets menu. Only releases
  whose changelog entry is tagged **[AUDIBLE]** change the sound:
  [Staying up to date](#staying-up-to-date).

The [troubleshooting guide](docs/troubleshooting.md) covers more symptoms.

<a id="usage"></a>
<a id="command-line-options"></a>
<a id="advanced"></a>
<a id="autoload"></a>
<a id="pipewire-filter-chain-instead-of-easyeffects"></a>
<a id="quick-start-pipewire"></a>
<a id="which-should-i-use"></a>
<a id="extracting-the-xml"></a>
<a id="auto-detection-notes"></a>
<a id="shell-tab-completion"></a>
<a id="running-the-tests"></a>
<a id="further-reading"></a>

## Documentation

- [Getting your tuning file](docs/getting-the-tuning-file.md) — when you have
  no Windows partition, or the script can't find your tuning
- [EasyEffects presets](docs/dolby-to-easyeffects.md) — every
  `dolby_to_easyeffects.py` option, autoload, and how to undo a run
- [PipeWire filter-chain](docs/dolby-to-pipewire.md) — the same tuning without
  EasyEffects, through `dolby_to_pipewire.py`
- [Disabling and enabling filters](docs/filters.md) — to remove an artifact, or
  try an optional stage
- [Troubleshooting](docs/troubleshooting.md) — no difference, too quiet, and
  other symptoms
- [Shell tab-completion](docs/shell-completion.md) — flag and value completion
  for all three scripts

The [docs index](docs/README.md) also lists the technical reference, the
research log and how to run the tests.

## Staying up to date

[CHANGELOG.md](CHANGELOG.md) tracks notable changes. To be notified when a new
version ships, click **Watch → Custom → Releases** at the top of the GitHub
page.

Entries tagged **[AUDIBLE]** change the *sound* of the generated preset. When
you see one, get the
[latest release](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/releases/latest)
or `git pull`, and **re-run the script** with the same flags as before to
regenerate and reload your preset, or the `dolby_to_pipewire.py` wrapper if you
use it. The run loads it into a
running EasyEffects when it can, and says what to pick otherwise. Other entries
are tooling,
packaging, docs, or new-device support that doesn't alter existing devices'
output, so there's nothing to regenerate.

## Supported devices

The converter works on the internal speakers of laptops, handhelds and other
devices whose Windows driver ships a Dolby DAX3 tuning, or, experimentally, a
[Sonitude](docs/sonitude.md) one. Confirmed on:

| Device | Reported by |
|---|---|
| ASUS TUF Gaming A15 (FA507NV) <!-- Realtek ALC256, 1043:14B3 --> | [#34](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/34) |
| ASUS TUF Gaming A16 (FA607PV) <!-- Realtek ALC256, 1043:1C93 --> | [#137](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/137) — reporter recommends `--volmax-slot output-gain` |
| ASUS Zenbook 14 (UM3406HA) <!-- Realtek ALC294, 1043:1C03 --> | [#134](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/134) — reporter recommends enabling autogain |
| ASUS Zenbook 14 UX3405CA, UX3405MA <!-- Realtek ALC294, 1043:1A63 --> | [#19](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/19), [#24](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/24) |
| ASUS Zenbook Pro 16X OLED UX7602BZ <!-- Realtek ALC285, 1043:16F3 --> | [#117](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/117) |
| Framework Laptop 13 Pro (Intel Core Ultra Series 3) <!-- Realtek ALC285, F111:000F --> | [#73](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/73) |
| HP ZBook 15 G6 <!-- Realtek ALC285, 103C:860F --> | [#140](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/140) — experimental, from its [Sonitude tuning](docs/sonitude.md); reporter finds Movie matching Windows, and Music and Voice close with less bass |
| Lenovo IdeaPad 3 15ALC6 (82KU) <!-- Realtek ALC257, 17AA:38BC --> | [#103](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/103) — reporter recommends `--enable autogain --enable level-restore` |
| Lenovo IdeaPad Pro 5 14AHP9 (83D3) <!-- Realtek ALC287, 17AA:38D0 --> | [#18](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/18) |
| Lenovo IdeaPad Pro 5 14APH8 (83AM) <!-- Realtek ALC287, 17AA:38C5 --> | [#33](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/33) — reporter confirms working on kernel 7.0, broken on 6.12 |
| Lenovo IdeaPad Pro 5 14IMH9 (83D2) <!-- Realtek ALC287, 17AA:38CE --> | [#36](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/36) — reporter recommends enabling autogain |
| Lenovo IdeaPad Slim 3 16ARP10 (83K8) <!-- Realtek ALC257, 17AA:3F88 --> | [#124](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/124) |
| Lenovo IdeaPad Slim 5 14ARP10 (83HT) <!-- Conexant SN6140, 17AA:3811 --> | [#111](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/111) |
| Lenovo Legion Y540-15IRH (81SX) <!-- Realtek ALC257, 17AA:3810 --> | [#70](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/70) |
| Lenovo Legion Y7000 2020 (82AV) <!-- Realtek ALC257, 17AA:3872 --> | [#93](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/93) |
| Lenovo ThinkBook 16p G5 IRX (21N5) <!-- Realtek ALC287, 17AA:38F9 --> | [#76](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/76) |
| Lenovo XiaoXinPro-13ARE 2020 (82DM) <!-- Realtek ALC257, 17AA:387F --> | [#91](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/91) |
| Lenovo Yoga 7 2-in-1 16AKP10 <!-- — --> | [#1](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/1) |
| Lenovo Yoga 7 16IAH7 (82UF) <!-- Realtek ALC287, 17AA:386A --> | [#53](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/53) — woofers need kernel 7.2, or the `hda_model=` line the tool prints |
| Lenovo Yoga Pro 7 14APH8 (82Y8) <!-- Realtek ALC287, 17AA:38C6 --> | [#30](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/30) |
| Lenovo Yoga Pro 7 14ASP9 (83HN) <!-- Realtek ALC287, 17AA:3903 --> | [#51](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/51) |
| Lenovo Yoga Pro 7 14IMH9 (83E2) <!-- Realtek ALC287, 17AA:38CF --> | [#83](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/83) |
| Lenovo Yoga Pro 9i 14IRP8 (83BU) <!-- Realtek ALC287, 17AA:38BE --> | [#17](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/17) |
| Lenovo Yoga Slim 7 14ARE05 (82A2) <!-- Realtek ALC287, 17AA:380D --> | [#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44) — reporter finds it on par with Windows with `--volmax-slot output-gain`, which brings back bass the default placement loses |
| Lenovo Yoga Slim 7 14ILL10 (83JX) <!-- Soundwire 17AA:3838 --> | [#59](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/59) |
| Lenovo Yoga Slim 7 Pro 14ACH5 (82MS, reported as Yoga 14sACH 2021) <!-- Realtek ALC287, 17AA:384F --> | [#84](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/84) |
| Microsoft Surface Laptop 3 <!-- Realtek ALC274, 10EC:11B2 --> | [#148](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/148) — experimental, from its [Dolby DAX2 tuning](docs/reference.md#dolby-dax2-input); reporter finds it about on par with Windows from memory |
| Microsoft Surface Laptop 4 <!-- Realtek ALC274, 10EC:123E --> | [#118](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/118) |
| Microsoft Surface Laptop 5 <!-- Realtek ALC274, 10EC:1290 --> | [#128](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/128) |
| Microsoft Surface Pro 9 <!-- Realtek ALC274, 10EC:1284 --> | [#113](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/113) |
| Motorola Moto Book 60 14IRH10R (83J7) <!-- Conexant SN6140, 17AA:3830 --> | [#121](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/121) — its tuning XML comes from the Lenovo IdeaPad Slim 5 14ARP10 (83HT) driver package |
| ThinkPad E14 Gen 2 AMD (20T6) <!-- Realtek ALC257, 17AA:507F --> | [#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25) — verified close-to-Windows: needs `--enable autogain`, plus to taste a raised Autogain *Target* (EE GUI) and desktop volume >100% |
| ThinkPad E16 Gen 2 (21MA) <!-- Conexant CX11880, 17AA:50E2 --> | [#144](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/144) |
| ThinkPad L14 Gen 6 AMD (21S8) <!-- Realtek ALC257, 17AA:50FF --> | [#61](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/61) |
| ThinkPad P53 (20QQ) <!-- Conexant CX11880, 17AA:2297 --> | [#131](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/131) |
| ThinkPad T14 Gen 1 (20S1) <!-- Realtek ALC257, 17AA:22B1 --> | [#86](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/86) |
| ThinkPad T14 Gen 1 AMD (20UD, 20UE) <!-- Realtek ALC257, 17AA:5081 --> | [#45](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/45) |
| ThinkPad T14 Gen 2 AMD (20XL) <!-- Realtek ALC257, 17AA:5094 --> | [#80](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/80) |
| ThinkPad T14 Gen 2 Intel (20W1) <!-- Realtek ALC257, 17AA:22C9 --> | [#55](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/55) |
| ThinkPad T14 Gen 7 AMD (21WV, 21WW) <!-- Realtek ALC257, 17AA:5144 --> | [#48](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/48) |
| ThinkPad T14 Gen 7 Intel (21WN) <!-- Realtek ALC257, 17AA:2356 --> | [#42](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/42) |
| ThinkPad T14s Gen 2 AMD (20XG) <!-- Realtek ALC257, 17AA:5096 --> | [#57](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/57) |
| ThinkPad T14s Gen 3 (21BS) <!-- Realtek ALC257, 17AA:22EE --> | [#88](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/88) |
| ThinkPad T14s Gen 6 AMD <!-- 17AA:50F0 --> | [#3](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/3) |
| ThinkPad T490s (20NX) <!-- Realtek ALC257, 17AA:2287 --> | [#133](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/133) |
| ThinkPad X1 Carbon Gen 9 (20XW) <!-- Realtek ALC287, 17AA:22D5 --> | [#63](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/63) |
| ThinkPad X1 Carbon Gen 10 (21CC) <!-- Realtek ALC287, 17AA:22E7 --> | [#99](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/99), [#139](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/139) |
| ThinkPad X1 Carbon Gen 11 (21HN) <!-- Realtek ALC287, 17AA:2315 --> | [#95](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/95) |
| ThinkPad X1 Carbon Gen 13 <!-- Soundwire 17AA:2339 --> | [#7](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/pull/7/) |
| ThinkPad X1 Yoga Gen 6 (20Y0) <!-- Realtek ALC287, 17AA:22D4 --> | [#78](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/78) |
| ThinkPad X1 Yoga Gen 7 (21CD) <!-- Realtek ALC287, 17AA:22E6 --> | author |
| ThinkPad X13 Gen 2 Intel (20WK) <!-- Realtek ALC257, 17AA:22CF --> | [#105](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/105) |
| ThinkPad X13 Gen 3 (21BQ) <!-- Realtek ALC257, 17AA:22EF --> | [#127](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/127) |
| ThinkPad X13 Gen 6 Intel (21RK) <!-- Realtek ALC257, 17AA:2344 --> | [#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23) |
| ThinkPad X13 Yoga Gen 1 (20SX) <!-- Realtek ALC257, 17AA:22AD --> | [#107](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/107) |
| ThinkPad X13 Yoga Gen 2 (20W9) <!-- Realtek ALC285, 17AA:22D6 --> | [#101](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/101) |
| ThinkPad X13 Yoga Gen 4 (21F3) <!-- Realtek ALC257, 17AA:230D --> | [#97](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/97) |
| ThinkPad Z13 Gen 1 (21D2) <!-- Realtek ALC287, 17AA:22F1 --> | [#115](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/115) |

If you test it on other hardware, please
[open a device report](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/new?template=device-report.yml)
whether it works or not.

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
4. Dialog Enhancer: bypassed by default
5. Autogain: bypassed by default on HDA
6. Multiband Compressor
7. Regulator: per-band limiter
8. Limiter: brickwall safety net

A [Sonitude tuning](docs/sonitude.md) builds a shorter preset: its EQ in the
convolver, then the limiter.

![A generated preset loaded in EasyEffects, convolver through limiter](docs/images/ee-chain-loaded.jpg)

For the full detail, see the [docs index](docs/README.md).

## References

- [wwmm/easyeffects](https://github.com/wwmm/easyeffects) — preset format
  reference
- [shuhaowu/linux-thinkpad-speaker-improvements](https://github.com/shuhaowu/linux-thinkpad-speaker-improvements)
  — alternative approach using captured impulse responses via WASAPI loopback
- [taprobane99/Lenovo-Yoga-Slim-7x-Dolby-Linux-Audio](https://github.com/taprobane99/Lenovo-Yoga-Slim-7x-Dolby-Linux-Audio)
  — downstream port of this script's output to a PipeWire filter-chain config
  with 4-speaker upmix on Snapdragon X. See
  [docs/alternative-pipelines.md](docs/alternative-pipelines.md) Option 3.
- [sklynic/easyeffects-tuf-gaming-a15](https://github.com/sklynic/easyeffects-tuf-gaming-a15)
  — manual DAX3 EQ extraction for ASUS laptops
- [mister2d/thinkpad-linux-audio](https://github.com/mister2d/thinkpad-linux-audio/)
  — extended Dolby pipeline for ThinkPads, built on top of this tooling. See
  [#2](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/2).
