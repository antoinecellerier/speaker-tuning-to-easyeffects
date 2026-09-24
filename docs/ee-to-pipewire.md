# `ee_to_pipewire.py` — design notes

This companion converter turns the EasyEffects `.json` preset that
[`dolby_to_easyeffects.py`](../dolby_to_easyeffects.py) emits into a PipeWire
`filter-chain` `.conf`. It serves users who'd rather not run EasyEffects: lower
CPU, no GUI, set-and-forget. It walks `plugins_order` and dispatches each EE
plugin key to a stage emitter. It copies the FIR impulse response next to the
generated conf and renders the result as PipeWire SPA-JSON. 

This file is the current architecture the script ships and the load-bearing
decisions behind it. The pre-implementation design exploration is in
[`alternative-pipelines.md` § 3 "Companion converter"](alternative-pipelines.md#companion-converter).
It includes the rationale for shipping a separate converter rather than a
`--pipewire` flag on the main script.

**Where the code lives.** `ee_to_pipewire.py` itself is the CLI: the argparse
builders it shares with the wrapper, the completers, and the `main()` that
orders the run. Everything under it is in `lib/pipewire/`, in four layers that
each import only the one below:

- `plugins.py` turns one EE plugin block into its LV2 node and holds the `EE_*`
  enum tables.
- `conf.py` holds `build_chain`, `emit_links`, `format_conf` and the SPA-JSON
  writer.
- `install.py` decides where the conf and `.irs` go, picks the smart-filter
  target sink and prints the closing messages.
- `checks.py` holds `--doctor` and the stacked-chain warning that shares its
  machinery.

The `lv2info` self-check is `validate.py`, which `main()` calls before writing
the conf. The split rules are in [code-organisation.md](code-organisation.md)
"Splitting the single-file scripts".

## One-command wrapper (`dolby_to_pipewire.py`)

[`dolby_to_pipewire.py`](../dolby_to_pipewire.py) chains generation and
conversion for users who never touch EasyEffects. It runs
`dolby_to_easyeffects.py` with `--output-dir`/`--irs-dir` pointed into a
`TemporaryDirectory`, so no preset or `.irs` lands under the EasyEffects tree.
It converts the selected variant(s) in-process via this script's `main()`. It
then restarts PipeWire and polls `pw-cli` until the sink appears, unless
`--no-activate` opts out. The wrapper adds **no conversion logic of its own**.
Everything in this document applies unchanged to wrapper-produced confs: output
layout, smart-filter routing, plugin coverage, validation and limitations. The
staging tempdir is disposable *because* of the default IRS-copy behavior
described under Output layout. The wrapper therefore never passes
`--no-copy-irs`. Its CLI is composed from `add_*_args`, the argparse group
builders the two converters share, so inherited flags can't drift from the
scripts that own them.

## Output layout

| File | Path |
|---|---|
| Filter-chain conf | `~/.config/pipewire/pipewire.conf.d/<node-name>.conf` |
| Copied IRS | `~/.config/pipewire/pipewire.conf.d/<node-name>.irs` |

That directory is `$XDG_CONFIG_HOME/pipewire/pipewire.conf.d`. PipeWire reads
the variable, per `man pipewire`, and so does this converter, so both follow a
moved config root.

`<node-name>` defaults to the preset filename stem, sanitised to `[A-Za-z0-9_]`.
For example, `Dolby-Balanced.json` → `Dolby_Balanced` and
`Dolby-Music-Warm.json` → `Dolby_Music_Warm`. Converting multiple presets in
succession therefore produces distinct sinks rather than clobbering a single
fixed name. `--node-name <name>` overrides the name. `--node-description <desc>`
overrides the human-readable label, which defaults to the preset stem
unmodified, e.g. `Dolby-Balanced`.

The conf goes in `pipewire.conf.d/`, not `filter-chain.conf.d/`, because the
daemon's stock `pipewire.conf` auto-includes that directory.
`filter-chain.conf.d/` is the overlay set for the standalone
`pipewire -c filter-chain.conf` invocation pattern. The measurement rig under
[`tools/measure_pw/`](../tools/measure_pw/) uses that pattern, but no end user
ever will. PipeWire silently doesn't load a conf dropped in the wrong directory.

The IRS is copied in by default, not symlinked or referenced in-place, so the
converted chain has no runtime dependency on the EasyEffects directory layout.
Uninstalling EasyEffects, moving its `irs/` tree or regenerating presets won't
break the PW chain. Nor does the chain follow a regeneration: the conf carries
the impulse as of its conversion. Re-run this converter after regenerating a
preset whose sound changed. `--no-copy-irs` instead keeps a cross-tree absolute
reference to one impulse file by name. A regenerated preset whose FIR changed
gets a new name, because the generator hashes the samples into it, so the same
re-run applies.

## Package names per distribution

Package names differ per distribution, and the LV2 build is not always the base
package. On Fedora and Arch, `lsp-plugins` does not ship the `.lv2` bundle
PipeWire loads; `lsp-plugins-lv2` does. `lib/packages.py` holds that table along
with the `/etc/os-release` family detection. Every message that names a package
prints the one row that matches the reader's machine, and falls back to listing
them all when it cannot place them. The table covers seven families and serves
every dependency either script can detect as missing: the Python stack,
`lv2info`, the PipeWire command-line tools, `amixer` and EasyEffects itself.

Two shapes of gap are recorded rather than papered over. `UNPACKAGED` carries
what to say where a family has no package at all. Calf reaches openSUSE only
through Packman. NixOS installs LV2 plugins declaratively, because a `nix-shell`
never reaches the daemon. `CAVEATS` carries what to add where the name resolves
but the default build does not deliver: Gentoo's `media-plugins/calf` needs
`USE=lv2`. Dropping either silently would turn "install these two" into a
command that installs one and reports success. `pw-cli`/`pw-dump` and
`spa-json-dump` are separate keys for the same reason: openSUSE and Alpine ship
them in different packages.

Names are per-repository facts, verified against each distribution's own binary
index. Repology tracks Debian, Fedora and openSUSE at *source* granularity, so
its listing says `lsp-plugins` where the installable package is
`lsp-plugins-lv2`. EasyEffects is the one entry not answered from the table.
Which release ships version 8 changes every few months. So the run asks the
machine's own package manager what it would install, and offers the distro
package only when that answer is 8 or newer.

The README's "Plugin dependencies and validation" and Install sections list the
same rows for someone reading before they run anything.
`tests/test_readme_packages_sync.py` fails if the two disagree.

## Smart-filter routing (the load-bearing UX choice)

The naive PipeWire filter-chain pattern sets `media.class = "Audio/Sink"`, which
creates a *virtual sink* apps target. That gives every user two problems:

1. **Volume stacking.** The chain sink and the hardware sink are two sinks in
   series. Each applies its own volume to its own inputs, so the two levels
   multiply. `pactl list sink-inputs` shows the app as an input on the chain and
   the chain's own output as an input on the speaker. Plain series gain is the
   whole mechanism. This is *not* PulseAudio's flat-volume mode, which nothing
   here runs. No sink reports the `FLAT_VOLUME` flag in `pactl list sinks`, and
   pipewire-pulse offers no such option. The loss is also larger than the
   percentages suggest, because a desktop's 0–100 % is cube-mapped on the way to
   `channelVolumes`. Measured, `wpctl set-volume … 0.5` lands 0.125. So a
   hardware sink left at 56 % is –15 dB, and a virtual-sink chain at 100 % on
   top of it keeps every one of them.
2. **No automatic bypass on output switch.** Plugging in HDMI or pairing
   Bluetooth headphones leaves the chain as the default sink. It then processes
   audio destined for hardware it was never tuned for, until the user manually
   changes the default.

EasyEffects-autoload sidesteps both by attaching its filter graph *directly* to
the hardware sink. Apps still target Speaker__sink, there is a single volume
layer, and HDMI gets no chain. PipeWire's `module-filter-chain` doesn't expose
that mode natively. There's no `media.class` value that means "wrap an existing
sink as a processing filter".

WirePlumber 0.5+ provides the missing piece with its smart-filter linking
pattern, in `/usr/share/wireplumber/scripts/lib/filter-utils.lua` +
`linking/find-filter-target.lua` + `linking/get-filter-from-target.lua`. A node
that declares these properties is treated as a filter rather than a destination:

```
node.link-group       = "<group>"
filter.smart          = true
filter.smart.target   = { node.name = "<hardware-sink-node-name>" }
priority.session      = -1
```

WirePlumber's link resolver intercepts streams targeting the matching hardware
sink through the `node.name` rule. It inserts the filter into the path
automatically. `filter.smart.targetable` defaults to false, so apps can't pick
the chain's capture sink directly. They target the hardware speaker as usual.

`priority.session = -1` nudges WP's "best default node" tiebreaker away from the
chain. It is belt-and-braces rather than load-bearing. A `module-filter-chain`
node declares neither `priority.session` nor `priority.driver`, so
`lib/node-utils.lua` scores it 0 anyway. An ALSA speaker sink scores 1000
(`monitors/alsa.lua`), Bluetooth 1010 (`monitors/bluez.lua`) and HDMI the 600s.
The property has no bearing at all on a sink the user picked by hand. See "The
selected output is remembered" below.

Net result:

- The speaker sink stays the system default.
- Apps targeting the speaker get audio routed through the chain transparently.
  In practice there is one volume slider, the speaker's. The chain keeps a
  control of its own, but nothing puts a user on it; see "A chain can still be
  turned down".
- HDMI / Bluetooth / USB outputs aren't matched by `filter.smart.target`, so the
  chain bypasses on its own when audio routes elsewhere.

The hardware speaker sink's `node.name` is auto-detected at conversion time. It
uses the same `pw-dump` probe as `dolby_to_easyeffects.py --autoload`:
Audio/Sink nodes tagged `device.icon_name == audio-speakers`. That tag excludes
HDMI / BT / USB. Some laptops fall back to a generic UCM2 profile that omits the
speaker icon, as in issue
[#18](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/18).
If nothing is tagged, the probe falls back to a relaxed tier of internal analog
sinks. A single relaxed candidate is used as the target with a warning. An
ambiguous one leaves the target unset, so pass `--target-sink`.

`--target-sink <node.name>` overrides the detection. `--target-sink ''` falls
back to a v1 virtual-sink emission for users on WirePlumber < 0.5 or with a
non-standard policy. Separately, `--target-object <node.name>` binds the chain's
*playback* to an explicit downstream node instead of letting WirePlumber choose.
That is a measurement-route override, for example onto a null sink; end users
want `--target-sink`.

### The selected output is remembered

The chain's sink stays visible in sound settings, as Limitations describes, so
it can be picked as the system output. Doing so is not fatal. Measured on
WirePlumber 0.5, the graph is *identical* either way,
`app → effect_input → chain → effect_output → speaker`, and nothing is processed
twice. What changes is that the chain and the speaker are then two sinks in
series, each with its own volume control, so the levels multiply.
`pactl list sink-inputs` shows the app on the chain and the chain's own output
on the speaker. A speaker left at 40 % takes −23.8 dB off a chain that reads 100
%.

The chain's control is also on the *wrong side of the tuning*. Measured with
`tools/measure_pw/volume_stage_probe.py` on the speaker sink's monitor, turning
the chain's volume down is indistinguishable from scaling the source content
itself. The S/R is 730 dB, the float64 noise floor. So the attenuation reaches
the graph as a quieter input, and the MBC, regulator and limiter engage
differently. The speaker's own control is the opposite. It is a hardware mixer
element, invisible in that capture, applied after everything PipeWire does. This
only bites on loud material. On pink noise at −13.9 dBFS RMS the dynamics stay
dormant and both controls are a plain gain. At −6.5 dBFS RMS they do not.

The part that outlives the mistake is WirePlumber's memory of it.
`default-nodes/find-selected-default-node.lua` scores the *current*
`default.configured.audio.sink` at `30000 + priority.session`. No
`priority.session` a conf could declare competes with that.
`state-default-nodes.lua` persists the pick to
`~/.local/state/wireplumber/default-nodes`. Consequences, all measured:

- A chain picked by hand stays the default across PipeWire restarts.
- Picking the speaker again clears it, and *that* sticks. It replaces rather
  than erases: the chain drops to `…audio.sink.0` in the state file.
- The entry survives the sink it names. Delete the chain and the head entry
  still says `effect_input.<name>`. Install a chain under that name again and it
  takes the default output straight back. The conf is then correct, but a
  year-old click routes it wrongly.
- Only the **head** entry does this. A name demoted down the stack does not come
  back. A stored node scores `priority.session + 20001 − i`, so a chain at
  position *i* only beats the speaker at *j* when `j − i > 1000`.

`--doctor`'s "Default output" check reports all three states. The environment
block's `Remembered:` line names a remembered pick the graph lacks. Nothing else
can show it.

### A chain can still be turned down

Smart-filter routing removes the *reason* to touch the chain's volume, not the
control. The chain sink keeps one, and it still applies. Measured with the
speaker selected as the output and the chain set to 0.125, the captured output
came back 7.9× down. It carried the same pre-graph signature as before, so the
tuning's dynamics see the attenuated signal too.

Once the speaker is selected, nothing in a desktop's sound settings puts a user
on that slider. That is what makes it worth a check. The way in is the
sequence issue #63 describes: select the chain, turn it down, switch back to the
speaker. The level stays, survives reboots, and has no visible cause.
`--doctor`'s "Chain volume" check reports it.

Deleting the conf does not clear it either. WirePlumber persists a sink's volume
by `media.name` in `~/.local/state/wireplumber/stream-properties`. It restores
that volume onto any later node with that name. So a chain reinstalled under the
same description comes back at the level it was left. Observed: a freshly
written conf read 50 % before anything had touched it. As with the selected
output above, reinstalling is not a reset.

### What a v1 install can and cannot get back

`--target-sink ''` cannot have what smart-filter routing gives: one control,
applied after the graph. There the chain is the selected sink, so the desktop's
volume keys act on it, and its control is upstream of the tuning. Hiding or
proxying the chain was explored twice and rejected; see Limitations.
`priority.session` cannot help either. Any value derived from the sinks in play,
0–2010, loses to the remembered pick's `30000 + priority`.

The loss is narrower than it looks. The speaker correction is linear, so where
the attenuation lands does not change it at all. At −13.9 dBFS RMS the whole
chain measured as exactly linear. Only the dynamics move, and only on loud
material. So the practical advice is to leave the speaker at 100 % and use the
chain's control, because that is the one the volume keys reach. That costs
compressor behaviour on loud content, not the tuning.

The half a reader cannot see is the level *underneath*. Once the chain is the
selected output, the sink it feeds is invisible from the slider they are moving.
That level survives reboots and is subtracted from everything. So both a v1-mode
run and `--doctor` read that sink's volume and print it, rather than only
advising that it be raised: "your speakers are at 40 % (−23.8 dB) right now".

### One smart filter per target sink

Smart filters that share a `filter.smart.target` are **chained, not offered as
alternatives**. In `filter-utils.lua`, `get_filter_from_target` returns the
*first* filter matching a target, and `get_filter_target` returns "the next
filter with matching target". So two installed confs put both chains in the path
in series. Measured on WirePlumber 0.5.15 with three voicings installed, the
path is `app → Balanced → Detailed → Warm → speaker sink`. It runs every stage
three times over: convolver, dialog enhancer, MBC, regulator, limiter. Setting
`filter.smart.targetable = true` does *not* turn them into choices. Picking one
in sound settings resolves it back to its target, and the link resolver
re-enters at the first filter anyway.

So more than one sink means smart-filter routing has to be off.
`dolby_to_pipewire.py` refuses `--variant all` and `--all-profiles` unless
`--target-sink ''` is passed. In that mode it pins each chain's playback with
`--target-object`. The pin is not optional. A v1 virtual sink whose playback
stream has no target follows the *default* sink. Choosing one of several in
sound settings then makes the others follow it and chain into it. Measured, that
gave `Balanced → Warm` and `Detailed → Warm`. Pinning each to the hardware sink
keeps them independent, which is the whole point of installing more than one.

A conversion also warns on the spot when the conf it just wrote joins another
aimed at the same sink. A user reaches that state by trying a second voicing or
profile. `--force` guards a single output path, so a differently-named conf
lands beside the first with no collision. Neither the run nor the audio would
otherwise say so.

### Diagnosing an installed chain (`--doctor`)

`ee_to_pipewire.py --doctor` reports the state of what is installed rather than
converting anything. `dolby_to_pipewire.py --doctor` reaches it too. It reports:

- chains stacked on one target sink
- a conf on disk with no node in the graph, as when a missing LSP/Calf plugin
  makes `module-filter-chain` drop the whole file
- an `.irs` a conf names but that isn't there
- a `filter.smart.target` naming a sink that no longer exists
- which output is selected: the chain itself, a virtual-sink chain nothing is
  playing through, or a remembered pick naming a chain that is gone. See "The
  selected output is remembered".
- confs under `filter-chain.conf.d/`
- WirePlumber older than 0.5
- EasyEffects processing the same audio
- confs written by another version of the tool

It ends with an environment block to paste into an issue.

It deliberately reports the *EasyEffects* side as a conflict only. On this path
EasyEffects is an intermediate format staged in a tempdir. The generator's
preset/autoload checks would describe directories this path never writes to.

Probing and judging are separate. Every check is a pure function over gathered
data, so `tests/test_pw_doctor.py` unit-tests states this developer machine
can't reach.

## Plugin coverage

| EE plugin key | Translated as | Notes |
|---|---|---|
| `convolver#0` | `type=builtin label=convolver` × 2 | PW's builtin convolver is mono, so EE's stereo convolver expands to one node per channel. `gain` config field carries `output-gain`. |
| `equalizer#0` (PEQ) | LSP `para_equalizer_x16_lr` | `xm` is **MUTE**, not enable: its default 0 = active. See `emit_peq` in `lib/pipewire/plugins.py`. |
| `equalizer#1` (dialog) | Same plugin as PEQ | Disambiguated by position in `plugins_order`, not by shape. `_assert_positional` fails loud if reordered. |
| `multiband_compressor#0` (MBC) | LSP `mb_compressor_stereo` | Per-band linear values round-trip to source dB to 1e-4. The per-control mapping is in the table below. |
| `multiband_compressor#1` (regulator) | Same plugin | Carries `volmax_boost`, typically +6 dB, on `input-gain` when present. |
| `limiter#0` | LSP `limiter_stereo` | `slink` is U_PERCENT (0–100), not 0–1. |
| `bass_enhancer#0` | Calf `BassEnhancer` | EE wraps Calf BassEnhancer (`src/bass_enhancer.cpp:67-74`). Triggers on SoundWire devices with small drivers. |
| `stereo_tools#0` | Calf `StereoTools` | EE wraps Calf StereoTools (`src/stereo_tools.cpp:65-80`). The generator emits no `stereo_tools#0` block; see below. |
| `autogain#0` (bypassed) | *(silent skip)* | HDA default is bypass=true, unless the preset was generated with `--enable autogain`. Emitting a bypassed node would just clutter. |
| `_vbe` (top-level metadata, `--enable virtual-bass` only) | LSP `filter_stereo` ×7 + Calf `Saturator` ×2 + builtin `copy`/`mixer` | Not an EE plugin key. EasyEffects cannot express the parallel branch, so the generator records the XML's virtual-bass values top-level, under the `_generator` contract. |
| `autogain#0` (active) | LSP `autogain_stereo` | EE's autogain is native libebur128 (`src/autogain.cpp`). `autogain_stereo` is the LV2 equivalent, a K-weighted (LUFS) loudness AGC. The mapping was validated at a 20 s history. |

- **`equalizer#0` (PEQ).** EE writes filter type / mode / slope as enum
  **strings**, such as `"Bell"`, `"Hi-pass"`, `"RLC (BT)"` and `"x1"`. LSP
  expects integers, so `EE_FTYPE_TO_LSP` / `EE_FMODE_TO_LSP` /
  `EE_FSLOPE_TO_LSP` translate them. The same pattern recurs for MBC global
  mode (`EE_MBC_GLOBAL_MODE`), MBC envelope boost (`EE_MBC_ENVB`), MBC
  sidechain mode (`EE_MBC_SCMODE`) and limiter mode (`EE_LIMITER_MODE`).
- **`multiband_compressor#1` (regulator).** `input-gain` is the default slot
  since issue
  [#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23),
  and `--volmax-slot output-gain` moves the boost. If the regulator stage is
  absent, `make_preset` puts the boost on `limiter#0`'s `input-gain` instead,
  so readers walking the gain stages must check both.
- **`bass_enhancer#0`.** `amount` is dB in the EE preset and linear in Calf, so
  `db_to_linear` converts it, as the `BIND_LV2_PORT_DB` macro does.
  `harmonics`→`drive`, `scope`→`freq`, `floor`/`blend` direct.
- **`stereo_tools#0`.** Mode strings → ints via `EE_ST_MODE` (7 labels, 0..6).
  `slev`/`mlev` are dB→linear; `sbal`/`mpan`/`stereo_base` direct linear;
  `sc_level` (1..100), `stereo_phase` (0..360°), `delay` (-20..+20 ms) direct.
  The *generator* (`dolby_to_easyeffects.py`) emits no `stereo_tools#0` block,
  because a DAX capture falsified the `surround-boost → stereo_tools` widening
  ([surround→stereo-base factor](design-notes.md#r-surround-boost-stereo-base)).
  The `stereo_tools#0` translator applies to any hand-edited or legacy preset
  that carries a `stereo_tools` block.
- **`_vbe` (top-level metadata, `--enable virtual-bass` only).**
  `lib/pipewire/vbe.py` sandwiches the whole translated chain between a copy
  fan-out and a dry+wet mixer. See "No VBE by default" under limitations.
- **`autogain#0` (active).** Active by default on SoundWire; on HDA only for
  presets generated with `--enable autogain`.
  - *Ports*: `target`→`level` and `silence-threshold`→`silence` are dB-domain
    ports passed **directly**, without `db_to_lin`, clamped to the port ranges
    (−60..0 / −84..−36). `weight=5` selects K-weighting = EBU R 128.
    `lkahead=0` keeps added latency at zero.
  - *Gain ride*: EE's `maximum-history` (s) drives the gain-ride time-constants
    asymmetrically. `tfall_l` (gain down, 200 ms/s) is faster than `tgrow_l`
    (gain up, 500 ms/s, anti-pumping), matching EE's measured behaviour. The
    on-device EE-vs-PW proof is in design-notes.
  - *Short windows*: windows shorter than 20 s surface a warning, on the
    console and in the conf-header warning block, that the PW ride may be
    faster than EE's. That covers HDA `--enable autogain` always, and SoundWire
    when `volume-leveler-amount` > 5.
  - *Gains*: EE `input-gain`/`output-gain` are always 0.0 and have no main-path
    port, so they are not written.

Anything the converter cannot express warns on the console instead of dropping
silently:

- unknown plugin keys, so a non-Dolby preset fed in surfaces as warnings, not a
  traceback
- bypassed plugins, except autogain on HDA, where bypass is the expected default
- unknown enum labels, translated to the fallback integer with a pointer at the
  `EE_*` table to extend
- presets declaring more than 16 EQ bands, with the excess dropped
- a nonzero convolver `input-gain`, which has no builtin port
- plugin objects missing from `plugins_order`, which the chain builder never
  visits

The XML→preset mapping invariant applies here too; see CLAUDE.md "XML-only
derivability". This script translates what EE's preset already encoded. It does
not introduce new parameters or hand-tuned offsets.

### MBC per-control translation

This reference table covers the `mb_compressor_stereo` controls, the
load-bearing mapping for both `multiband_compressor#0` and `#1`. dB → linear is
`10**(dB/20)`, so `0 dB → 1.0`, not 0. Identity passes the EE value through
unchanged. Values round-trip to four decimals, locked in by
`tests/test_ee_to_pipewire.py::test_mbc_round_trip_4_decimals`.

| EE param                      | LSP control | Conversion |
|-------------------------------|-------------|------------|
| `attack-threshold` (dB)       | `al_N`      | `10**(dB/20)` |
| `release-threshold` (dB)      | `rrl_N`     | `10**(dB/20)` |
| `makeup` (dB)                 | `mk_N`      | `10**(dB/20)` |
| `knee` (dB, ≤0)               | `kn_N`      | `10**(dB/20)` |
| `ratio`                       | `cr_N`      | identity |
| `attack-time` (ms)            | `at_N`      | identity |
| `release-time` (ms)           | `rt_N`      | identity |
| `split-frequency` (Hz)        | `sf_N`      | identity |
| `enable-band` (bool)          | `cbe_N`     | 1 / 0 |
| `compressor-enable` (bool)    | `ce_N`      | 1 / 0 |
| `sidechain-mode` `RMS`/`Peak` | `scm_N`     | enum (`RMS`=1, `Peak`=0; full table in `EE_MBC_SCMODE`) |
| `sidechain-lookahead` (ms)    | `sla_N`     | identity |
| `sidechain-preamp` (dB)       | `scp_N`     | `10**(dB/20)` |
| `compression-mode`            | `cm_N`      | enum (`Downward`=0; full table in `EE_MBC_CM`). Written explicitly, because LSP's boost path below is live in the other modes |
| `boost-threshold` (dB)        | `bth_N`     | `10**(dB/20)`. LV2 default is −72 dB; the generator pins −60 dB |
| `boost-amount` (dB)           | `bsa_N`     | `10**(dB/20)`. LV2 default is +6 dB; the generator pins 0 dB |
| `sidechain-custom-lowcut-filter` / `-highcut-filter` (bool) | `sclc_N` / `schc_N` | 1 / 0 |
| `sidechain-lowcut-frequency` / `-highcut-frequency` (Hz) | `sclf_N` / `schf_N` | identity, inert while the custom-filter toggles above stay off |
| `stereo-split` (bool)         | `ssplit`    | 1, written only when true. Global, not per band. The port doesn't exist on lsp-plugins < 1.2.3, and the generator always emits false == the port default |

### Known approximations & untranslated parameters

A few EE preset params are deliberately not written into the conf. Each drop is
faithful only because the generator-pinned value equals the LV2 port default the
conf silently inherits. Tests lock both sides. `_INTENTIONALLY_UNTRANSLATED` in
`tests/test_ee_to_pipewire.py` pins the generator value and the LV2 default. A
slow-tier test cross-checks the pinned defaults against the installed plugins
via `lv2info`. If either side moves, the build goes red and the param must be
translated instead.

| EE param | Pinned value | LV2 port (default) |
|---|---|---|
| MBC/regulator `mute` / `solo` | `False` | `bm_N` / `bs_N` (0) |
| MBC/regulator `sidechain-type` | `"Internal"` | `sce_N` (0). External sidechain is unwired in a filter-chain graph |
| MBC/regulator `sidechain-source` | `"Middle"` | `scs_N` (0) |
| MBC/regulator `stereo-split-source` | `"Left/Right"` | `sscs_N` (0); only read when `ssplit` is on |
| MBC/regulator `sidechain-reactivity` | `10.0` ms | `scr_N` (10) |
| limiter `oversampling` / `dithering` | `"None"` | `ovs` / `dith` (0) |
| limiter `sidechain-type` | `"Internal"` | `extsc` (0) |
| limiter `sidechain-preamp` | `0.0` dB | `scp` (1.0 linear) |
| convolver `ir-width` / `autogain` | `100` / `False` | *(no port: EE-internal IR preprocessing)* |
| PEQ/dialog `split-channels` | `True` / `False` | *(no port: the `_lr` plugin always takes explicit L/R bands)* |

The autogain translation is a known **approximation**, not a
default-equivalence. LSP `autogain_stereo` is a different implementation from
EE's native libebur128. Only the long-window gain ride is derived from the
preset: `maximum-history` → `tgrow_l`/`tfall_l`. The short-window ride
(`tgrow_s`/`tfall_s`), loudness periods, drift limit and amplification cap stay
at LSP defaults. EE's `reference` loudness-statistic selector,
`"Geometric Mean (MSI)"`, has no equivalent port. The mapping is validated on
device at a 20 s history; design-notes has the measurement. Re-deriving more of
it is gated on device measurement; see CLAUDE.md "Validating audio changes".

## Equivalence to the EE chain

The graph sample rate is a **measured non-equivalence, in this path's favour**.
It is not the only one: the autogain translation above is a known approximation,
and 4-channel upmix isn't translated at all. EasyEffects resamples the convolver
kernel to the server rate without compensating its gain. So an EasyEffects chain
on a graph above 48 kHz plays hot by the rate ratio in dB, +11.8 dB measured at
192 kHz. This path does not. `module-filter-chain` resamples the IR itself, and
documents a `resample_quality` "in case the IR does not match the graph
samplerate". Its output measured unchanged at 48, 96 and 192 kHz. The conf this
converter writes never sets that key, so what holds the level is the module's
default handling, not anything we emit. Full measurement and the isolating test:
[A preset that plays hot](research/easyeffects-and-pipewire.md#r-convolver-resample-gain).
So on a machine whose graph is deliberately above 48 kHz, for example for an
external DAC, this path holds the level the tuning intends where EasyEffects
does not. The statement is about output level, measured on one device and preset
at three rates, not a claim that the whole chain is more faithful.

The MBC/regulator/limiter linear values round-trip to the source preset's dB
values to 4 decimals. The full chain measures equivalent to the live EasyEffects
pipeline on the development device, an X1 Yoga Gen 7 (HDA) running
`Dolby-Balanced`:

- Frequency-domain: max |Δ| ≤ 0.5 dB across 50 Hz–18 kHz on every stimulus in
  the battery. The battery is sweep, sweep_quiet, pink, pink_quiet and
  multitone, plus the asymmetric `stereo_pink` for stereo-aspect validation.
  Real measurements on the dev device land in the 0.00–0.03 dB range.
- Time-domain: the PASS threshold is signal-to-residual ≥ 30 dB on every
  stimulus. Real measurements with the full LSP+Calf chain run at +70..+73 dB on
  mono-symmetric stimuli. The asymmetric `stereo_pink`, compared per channel,
  lands in the same band. So a sub-30 result is a real regression rather than a
  metrology ceiling.

The measurement workflow and thresholds are in
[`tools/measure_pw/README.md`](../tools/measure_pw/README.md).

A deterministic schema check, `lib/pipewire/validate.py`, runs in process
automatically after conversion; `--no-validate` skips it. It shells out to
`lv2info` for every URI in the conf and validates the `control = { ... }` block
against each port's `Symbol`/`Min`/`Max`/`Default`/`Properties`. It catches
unknown port symbols, out-of-range values and the `xm`-MUTE-inversion trap. The
same check has a command-line front end at `tools/measure_pw/validate_conf.py`,
for a conf already on disk. It gives the same verdicts. It exits 1 on a conf
naming a plugin `lv2info` will not resolve, and 2 when the check could not run
at all.

The same pass decides whether the conf is written at all. `lv2info` and the
filter-chain both resolve plugins through lilv. So a URI `lv2info` exits
non-zero for is one the daemon will not load either, because the plugin is
missing or its TTL won't parse. Those URIs come back in `Report.unloadable`, and
the status is `ERRORS`. The run then names the package instead of writing a conf
that cannot work. An exec that never *answered*, such as a timeout or a fork
that failed, is the opposite case and stays a warning. It says nothing about the
plugin, so that plugin's ports simply go unchecked.

`lilv-utils` is not required. PipeWire needs the lilv library, not the command,
so demanding it would block a machine whose LSP and Calf are correctly
installed. Without it the check cannot run at all, and the status is
`NO_TOOLING`. The conf is then written unchecked. The run says what that costs
and names the package that buys the check back. The name comes from the table
under "Package names per distribution" above.

Independently of any of that, `conf.format_conf` gives the emitted module
`flags = [ ifexists nofail ]`. The conf is a `pipewire.conf.d/` drop-in, so it
loads in the daemon's own context. Without the flag, one unresolvable plugin
aborts context creation and `pipewire.service` will not start at all. That is
[#71](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/71).
With the flag, the chain is skipped and playback continues unprocessed. The
activation step's sink check and `--doctor`'s "Chains loaded" report that.

## Validation in pytest

- `tests/test_ee_to_pipewire.py` covers DSP math, schema invariants, the
  load-bearing 4-decimal MBC round-trip, IRS-copy semantics, smart-filter
  property emission and a validator-via-`main()` smoke test. It also holds the
  systematic coverage guard. `make_preset` is swept across every
  emission-relevant flag combination. Every plugin key / leaf param / enum label
  the generator can emit must be translated by the converter or carried in
  `_INTENTIONALLY_UNTRANSLATED` with a default-equivalence proof. See "Known
  approximations & untranslated parameters" above. A new generator feature that
  is neither fails the fast tier.
- `tests/corpus/test_ee_to_pipewire_corpus.py` runs the full XML→preset→PW-conf
  pipeline against every distinct discovered DAX3 XML. It auto-discovers them
  from NTFS mounts and CWD, and `ATMOS_CORPUS_DIR` overrides that. It asserts
  that every link endpoint resolves and that the converter emits **zero
  warnings** on generated presets, which re-runs the coverage guard's drift
  check against real XMLs. When `lv2info` and `spa-json-dump` are installed, it
  schema-checks each rendered conf through `lib/pipewire/validate.py` in
  process, with every URI's `lv2info` output memoized for the session. One
  further test runs the `validate_conf.py` CLI over a single rendered conf,
  since nothing else exercises the wrapper end to end. The tier catches
  "converter crashes on a non-X1-Yoga XML shape" before it reaches a tester.

## Limitations / known gaps

- **WirePlumber 0.5+ required** for smart-filter routing. Debian trixie / Fedora
  41+ / Arch all ship 0.5+. Older systems, such as Debian bookworm and Ubuntu
  24.04 native, need `--target-sink ''` to fall back to the v1 virtual-sink
  emission. That mode carries the volume-stacking and HDMI-bypass caveats noted
  above.
- **The chain sink stays visible** in pavucontrol / GNOME's sound output picker,
  as a separate entry alongside the hardware speaker. Only a `HARDWARE` flag
  distinguishes it, and no picker shows that flag. Picking it works, because the
  chain auto-routes to the speaker. But the volume slider is then on the chain,
  not the speaker, which reintroduces the v1 stacking. See "The selected output
  is remembered". Two mitigations ship instead of a fix. In smart-filter mode
  the description carries a ` (speaker filter)` suffix, so the entry reads as
  what it is. `--doctor` reports it when it is selected. The desired UX is one
  sink per hardware output, with the chain transparently inserted. That requires
  hiding the chain from PA enumeration. We explored two paths, and neither is
  viable on this class of hardware:
  - `media.class = "Audio/Sink/Internal"` does suppress the chain from PA's sink
    list, but it also breaks pipewire-pulse bridging. Apps targeting the speaker
    block on writes to a sink-input PA can't represent:
    `pa_sink_input.sink == PA_INVALID_INDEX`.
  - WirePlumber's Software DSP policy inverts the visibility. It hides the
    *speaker* and exposes the chain in its place via `hide-parent: true`. The
    policy is `/usr/share/wireplumber/scripts/node/software-dsp.lua`
    ([docs](https://pipewire.pages.freedesktop.org/wireplumber/policies/software_dsp.html)).
    This inversion works for embedded devices where the speaker is the only port
    on its card. On laptops, one multi-port HDA card shares an `alsa_card.*`
    between the Speaker port and HDMI / BT / Headphones. On those cards, GNOME's
    gvc-mixer-control enumerates outputs from the active profile's port list,
    not just from PA's sink list. So a phantom "Speaker - <hw-description>"
    entry stays in the picker even with the sink hidden. Selecting the phantom
    fails, because `pactl set-default-sink` on the hidden sink returns "No such
    entity". The user-visible result is worse than just having two working
    entries. No card profile on the dev hardware excludes the Speaker port
    without also unplugging headphones, so we can't profile-swap our way out
    either.
- **No 4-channel upmix** for Snapdragon-class laptops such as the Yoga Slim 7x
  and X13s Gen 1. Every XML in the corpus reports `total_count=2`, including the
  X13s sibling. The upmix is device wisdom encoded outside the XML. See
  cross-device-findings.md §14.
- **No VBE (virtual bass enhancement) by default.** DAX synthesises
  missing-fundamental bass harmonics on HDA laptops, per issue
  [#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14).
  Neither the EE preset nor the default conf reproduces them. The converter is a
  faithful 1:1 translation, and the stage is a parallel graph EasyEffects cannot
  express. Building it by default would make the PW conf deliberately diverge
  from EE.

  The experimental opt-in is `--enable virtual-bass`, given to the preset
  generator or to `dolby_to_pipewire.py`. The generator then embeds a top-level
  `_vbe` metadata block. EasyEffects ignores unknown top-level keys, the same
  contract as `_generator`. This converter turns the block into the measured wet
  branch (`lib/pipewire/vbe.py`). The translated chain is sandwiched between a
  `copy` fan-out and a dry+wet `mixer`, with two LSP brick-wall band-passed arms
  into Calf Saturators between them. All filters run the IIR engine with no
  look-ahead, and the mixers/copies are builtin pass-throughs, so the branch
  adds zero latency. Evidence and the measured score are in
  the [DAX virtual-bass finding](research/virtual-bass.md#r-dax-virtual-bass).
- **Not alongside EasyEffects.** The chain is a replacement for the EasyEffects
  preset, not an add-on. In smart-filter mode EasyEffects' own output plays into
  the very sink the chain attaches to. Everything then carries the EE preset
  *and* the chain in series, because filters sharing a target run in series, as
  measured in "One smart filter per target sink". The converter warns at
  conf-write time when an `easyeffects` process is up. To switch back to
  EasyEffects, delete the conf and its `.irs` from
  `~/.config/pipewire/pipewire.conf.d/`, restart PipeWire and start EasyEffects
  again.
- **No `--launch` flag on this script.** PipeWire's standard reload path is
  `systemctl --user restart pipewire pipewire-pulse`. The converter prints it in
  its next-steps checklist and lets the user run it. Activation lives one layer
  up. `dolby_to_pipewire.py` restarts PipeWire and verifies the sink by default,
  with `--no-activate` to opt out. It passes `--skip-next-steps` here so the
  checklist isn't printed twice.
- **Small-quantum systems under load are an unvalidated regime.** The conf pins
  no quantum/latency properties, so the chain runs at whatever quantum the
  session picked. Perf validation to date is a laptop APU at 48 kHz / 1024
  quantum, xrun-free; `tools/measure_perf/README.md` has the reference numbers.
  A handheld APU running a game at a smaller session quantum is untested. One
  Ally X tester hit crackling with `spa.audioconvert: out of buffers` in the log
  (issue
  [#39](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/39)).
  To isolate DSP load, temporarily pin a larger quantum with
  `pw-metadata -n settings 0 clock.force-quantum 1024`, and revert with value
  `0`. That raises the whole session's base latency, a system-wide trade-off the
  user opts into. The chain itself still adds zero latency over whatever quantum
  runs.

  **The graph sample rate is the bigger lever of the two**, and the one to check
  first. Measured with `tools/measure_perf/`, this chain costs ~3x the cycles at
  192 kHz that it does at 48 kHz, and the EasyEffects one ~4.5x. `clock.rate`
  also sets the real-time duration of a quantum (`man pipewire.conf`). So a
  session whose *default* rate is high runs a correspondingly shorter cycle at
  the same quantum number: 1024 frames is 21.3 ms at 48 kHz and 2.7 ms at 384
  kHz. Pin it the same way with
  `pw-metadata -n settings 0 clock.force-rate 48000`, and revert with `0`. The
  same system-wide caveat applies, since someone running a high rate for an
  external DAC chose it. Issue
  [#84](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/84)
  is why the doctor reports the rate rather than changing it.

  Crackle can also originate below PipeWire entirely. Rule the kernel out before
  tuning the graph. The ROG Xbox Ally X case is in the research log,
  ["Bad sound with a perfect preset"](research/hardware-and-drivers.md#r-kernel-misconfigured-codec).
