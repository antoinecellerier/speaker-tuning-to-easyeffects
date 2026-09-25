# PipeWire filter-chain instead of EasyEffects

The same tuning runs as a self-contained PipeWire filter-chain `.conf`. It needs
no GUI, uses less CPU, runs set-and-forget, and works whether or not EasyEffects
is installed. `dolby_to_pipewire.py` produces it in one command by driving the
preset generator and the `ee_to_pipewire.py` converter.
[`docs/ee-to-pipewire.md`](ee-to-pipewire.md) has the design notes and
equivalence measurements.

## Quick start (PipeWire)

One command goes from tuning XML to an active sink. It generates the EasyEffects
preset in a throwaway temporary directory, so nothing is installed under
`~/.local/share/easyeffects`. It converts the preset to a conf and copies the
matching `.irs` beside it. Then it restarts PipeWire and verifies the sink.

Prerequisites are NumPy and SciPy, which the generator needs, and the LSP/Calf
LV2 plugins. [Install](../README.md#install) and *Plugin dependencies and
validation* below cover them. The tuning XML is located the same way as in the
main [Quick start](../README.md#quick-start): auto-discovery, `--windows`, or
[manual extraction](getting-the-xml.md).

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
under every one: [details](cross-device-findings.md).

The conf lands in `~/.config/pipewire/pipewire.conf.d/`, or under
`$XDG_CONFIG_HOME` instead if you've set it. It attaches transparently to your
internal-speaker sink. Apps keep targeting the speaker, while HDMI, Bluetooth
and USB outputs bypass it automatically. The conf is stereo only. It covers the
convolver, PEQ, dialog, multiband compressor, regulator and limiter, plus
`bass_enhancer`, and `stereo_tools` from a hand-edited or legacy preset. An
active `autogain` volume leveler is translated too. Only 4-channel upmix isn't.
See [Limitations](ee-to-pipewire.md#limitations--known-gaps).

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

## Which should I use?

The two paths sound the same:
[measurements](ee-to-pipewire.md#equivalence-to-the-ee-chain) show them
equivalent. Choose on everything else:

- **Features → EasyEffects.** A GUI to tweak and switch presets live. The
  `autogain` volume leveler isn't EE-only: the PW side
  [translates it too](ee-to-pipewire.md#plugin-coverage). Autogain runs by
  default on SoundWire devices. On HDA the generator leaves it bypassed unless
  you pass `--enable autogain`, because its loudness boost can audibly
  [saturate](research/adaptive-processing.md#why-autogain-is-bypassed-by-default)
  on quiet-background content.
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
with [`tools/measure_perf/`](../tools/measure_perf). It uses a
frequency-invariant `perf`-cycle measurement, since laptop clocks don't hold
still.

## Manual two-step (keep the EasyEffects preset files, full flag surface)

The wrapper is a thin orchestrator over the two converters. Run them yourself to
keep the preset JSON and `.irs` under `~/.local/share/easyeffects`, or for flags
the wrapper doesn't expose: `--node-name`, `--no-copy-irs` and autoload.

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

## Plugin dependencies and validation

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

## `dolby_to_pipewire.py` command-line options

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
- `--best-guess` — on a SoundWire machine, fall back to a manufacturer-matched
  tuning when auto-detection finds no exact hardware match

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

## `ee_to_pipewire.py` command-line options

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
- `--force` — overwrite the output conf and the copied `.irs` if they already
  exist

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
