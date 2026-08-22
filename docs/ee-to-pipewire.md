# `ee_to_pipewire.py` — design notes

Companion converter that turns the EasyEffects `.json` preset
[`dolby_to_easyeffects.py`](../dolby_to_easyeffects.py) emits into a
PipeWire `filter-chain` `.conf` for users who'd rather not run
EasyEffects (lower CPU, no GUI, set-and-forget). It walks
`plugins_order`, dispatches each EE plugin key to a stage emitter,
copies the FIR impulse response next to the generated conf, and renders
the result as PipeWire SPA-JSON. Stage parameters round-trip back to
the source preset's dB values to four decimals (verified by
`tests/test_ee_to_pipewire.py::test_mbc_round_trip_4_decimals`).

The pre-implementation design exploration — including the rationale
for shipping a separate converter rather than a `--pipewire` flag on
the main script — is in
[`alternative-pipelines.md` § 3 "Companion converter"](alternative-pipelines.md#companion-converter);
this file is the current architecture the script actually ships and
the load-bearing decisions behind it.

**Where the code lives.** `ee_to_pipewire.py` itself is the CLI: the
argparse builders it shares with the wrapper, the completers, and the
`main()` that orders the run. Everything under it is `lib/pipewire/`, four
layers each importing only the one below — `plugins.py` (one EE plugin block
→ its LV2 node, plus the `EE_*` enum tables), `conf.py` (`build_chain`,
`emit_links`, `format_conf` and the SPA-JSON writer), `install.py` (where
the conf and `.irs` go, the smart-filter target sink, the `lv2info`
self-check, the closing messages) and `checks.py` (`--doctor`, and the
stacked-chain warning that shares its machinery). The split rules are in
[code-organisation.md](code-organisation.md) "Splitting the single-file scripts".

## One-command wrapper (`dolby_to_pipewire.py`)

[`dolby_to_pipewire.py`](../dolby_to_pipewire.py) chains generation and
conversion for users who never touch EasyEffects: it runs
`dolby_to_easyeffects.py` with `--output-dir`/`--irs-dir` pointed into a
`TemporaryDirectory` (so no preset or `.irs` lands under the EasyEffects
tree), converts the selected variant(s) in-process via this script's
`main()`, then restarts PipeWire and polls `pw-cli` until the sink
appears (`--no-activate` opts out). It adds **no conversion logic of its
own** — everything in this document (output layout, smart-filter
routing, plugin coverage, validation, limitations) applies unchanged to
wrapper-produced confs. The staging tempdir is disposable *because* of
the default IRS-copy behavior described under Output layout; the wrapper
therefore never passes `--no-copy-irs`. Its CLI is composed from the two
converters' shared argparse group builders (`add_*_args`), so inherited
flags can't drift from the scripts that own them.

## Output layout

| File | Path |
|---|---|
| Filter-chain conf | `~/.config/pipewire/pipewire.conf.d/<node-name>.conf` |
| Copied IRS | `~/.config/pipewire/pipewire.conf.d/<node-name>.irs` |

`<node-name>` defaults to the preset filename stem, sanitised to
`[A-Za-z0-9_]` (e.g. `Dolby-Balanced.json` → `Dolby_Balanced`,
`Dolby-Music-Warm.json` → `Dolby_Music_Warm`). Converting multiple
presets in succession therefore produces distinct sinks rather than
clobbering a single fixed name. `--node-name <name>` overrides;
`--node-description <desc>` overrides the human-readable label
(default: the preset stem unmodified, e.g. `Dolby-Balanced`).

`pipewire.conf.d/` (not `filter-chain.conf.d/`) is the directory the
daemon's stock `pipewire.conf` auto-includes; `filter-chain.conf.d/`
is the overlay set for the standalone `pipewire -c filter-chain.conf`
invocation pattern (which the measurement rig under
[`tools/measure_pw/`](../tools/measure_pw/) uses, but no end user ever
will). Drop the conf in the wrong dir and PipeWire silently doesn't
load it.

The IRS is copied in by default (not symlinked or referenced
in-place) so the chain has no runtime dependency on the EasyEffects
directory layout once converted — uninstalling EasyEffects, moving
its `irs/` tree, or regenerating presets won't break the PW chain.
`--no-copy-irs` reverts to a cross-tree absolute reference for users
who want EE preset regenerations to propagate automatically.

## Smart-filter routing (the load-bearing UX choice)

The naive PipeWire filter-chain pattern (`media.class = "Audio/Sink"`)
creates a *virtual sink* apps target. That gives every user two
problems:

1. **Volume stacking.** The chain sink and the hardware sink are two
   sinks in series, and each applies its own volume to its own inputs —
   `pactl list sink-inputs` shows the app as an input on the chain and
   the chain's own output as an input on the speaker — so the two
   levels multiply. Plain series gain is the whole mechanism; this is
   *not* PulseAudio's flat-volume mode, which nothing here runs: no
   sink reports the `FLAT_VOLUME` flag in `pactl list sinks`, and
   pipewire-pulse offers no such option. The loss is also larger than
   the percentages suggest, because a desktop's 0–100 % is cube-mapped
   on the way to `channelVolumes` (measured: `wpctl set-volume … 0.5`
   lands 0.125). So a hardware sink left at 56 % is –15 dB, and a
   virtual-sink chain at 100 % on top of it keeps every one of them.
2. **No automatic bypass on output switch.** Plugging in HDMI or
   pairing Bluetooth headphones leaves the chain as the default
   sink, processing audio destined for hardware it was never tuned
   for, until the user manually changes the default.

EasyEffects-autoload sidesteps both by attaching its filter graph
*directly* to the hardware sink (apps still target Speaker__sink,
single volume layer, no chain on HDMI). PipeWire's
`module-filter-chain` doesn't expose that mode natively — there's no
`media.class` value that means "wrap an existing sink as a
processing filter".

WirePlumber 0.5+ provides the missing piece via its **smart-filter**
linking pattern (`/usr/share/wireplumber/scripts/lib/filter-utils.lua`
+ `linking/find-filter-target.lua` + `linking/get-filter-from-target.lua`).
A node that declares:

```
node.link-group       = "<group>"
filter.smart          = true
filter.smart.target   = { node.name = "<hardware-sink-node-name>" }
priority.session      = -1
```

is treated as a filter rather than a destination. WirePlumber's link
resolver intercepts streams targeting the matching hardware sink
(via the `node.name` rule) and inserts the filter into the path
automatically. `filter.smart.targetable` defaults to false, so apps
can't pick the chain's capture sink directly — they target the
hardware speaker as usual. `priority.session = -1` nudges WP's
"best default node" tiebreaker away from the chain — belt-and-braces
rather than load-bearing: a `module-filter-chain` node declares neither
`priority.session` nor `priority.driver`, so `lib/node-utils.lua` scores
it 0 anyway, against 1000 for an ALSA speaker sink
(`monitors/alsa.lua`), 1010 for Bluetooth (`monitors/bluez.lua`) and the
600s for HDMI. It has no bearing at all on a sink the user picked by
hand — see "The selected output is remembered" below.

Net result:

- Speaker sink stays the system default.
- Apps targeting the speaker get audio routed through the chain
  transparently (one volume slider in practice — the speaker's; the
  chain keeps a control of its own, and it still attenuates, but
  nothing puts a user on it. See "A chain can still be turned down").
- HDMI / Bluetooth / USB outputs aren't matched by `filter.smart.target`,
  so the chain bypasses on its own when audio routes elsewhere.

The hardware speaker sink's `node.name` is auto-detected at conversion
time via the same `pw-dump` probe `dolby_to_easyeffects.py --autoload`
uses: Audio/Sink nodes tagged `device.icon_name == audio-speakers`
(which excludes HDMI / BT / USB). If nothing is tagged — some laptops
fall back to a generic UCM2 profile that omits the speaker icon
(issue [#18](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/18)) — it falls back to a relaxed tier of internal analog sinks;
a single relaxed candidate is used as the target with a warning, and an
ambiguous one leaves the target unset (pass `--target-sink`).
`--target-sink <node.name>` overrides; `--target-sink ''` falls back to
a v1 virtual-sink emission for users on WirePlumber < 0.5 or with a
non-standard policy. Separately, `--target-object <node.name>` binds the
chain's *playback* to an explicit downstream node (e.g. a measurement
null sink) instead of letting WirePlumber choose — a measurement-route
override; end users want `--target-sink`.

### The selected output is remembered

The chain's sink stays visible in sound settings (see Limitations), so it
can be picked as the system output. Doing so is not fatal — measured on
WirePlumber 0.5, the graph is *identical* either way
(`app → effect_input → chain → effect_output → speaker`), and nothing is
processed twice. What changes is that the chain and the speaker are then
two sinks in series, each with its own volume control: `pactl list
sink-inputs` shows the app on the chain and the chain's own output on the
speaker, so the levels multiply. A speaker left at 40 % takes −23.8 dB off
a chain that reads 100 %.

The chain's control is also on the *wrong side of the tuning*. Measured
with `tools/measure_pw/volume_stage_probe.py`, capturing the speaker sink's
monitor: turning the chain's volume down is indistinguishable from scaling
the source content itself (S/R 730 dB — the float64 noise floor), so it
reaches the graph as a quieter input and the MBC, regulator and limiter
engage differently. The speaker's own control is the opposite — a hardware
mixer element, invisible in that capture, applied after everything PipeWire
does. This only bites on loud material: on pink noise at −13.9 dBFS RMS the
dynamics stay dormant and both are a plain gain; at −6.5 dBFS RMS they do
not.

The part that outlives the mistake is WirePlumber's memory of it.
`default-nodes/find-selected-default-node.lua` scores the *current*
`default.configured.audio.sink` at `30000 + priority.session`, which no
`priority.session` a conf could declare competes with, and
`state-default-nodes.lua` persists it to
`~/.local/state/wireplumber/default-nodes`. Consequences, all measured:

- A chain picked by hand stays the default across PipeWire restarts.
- Picking the speaker again clears it, and *that* sticks. It replaces
  rather than erases: the chain drops to `…audio.sink.0` in the state file.
- The entry survives the sink it names. Delete the chain and the head entry
  still says `effect_input.<name>`; install a chain under that name again
  and it takes the default output straight back — a conf that is now
  correct, routed wrongly by a year-old click.
- Only the **head** entry does this. A name demoted down the stack does not
  come back: a stored node scores `priority.session + 20001 − i`, so a
  chain at position *i* only beats the speaker at *j* when `j − i > 1000`.

`--doctor`'s "Default output" check reports all three states, and the
environment block names the remembered pick — nothing else can show it.

### A chain can still be turned down

Smart-filter routing removes the *reason* to touch the chain's volume, not the
control. The chain sink keeps one, and it still applies: measured with the
speaker selected as the output and the chain set to 0.125, the captured output
came back 7.9× down, with the same pre-graph signature as before — so the
tuning's dynamics see the attenuated signal too.

Nothing in a desktop's sound settings puts a user on that slider once the
speaker is selected, which is exactly what makes it worth a check. The way in
is the sequence issue #63 describes: select the chain, turn it down, switch
back to the speaker. The level stays, survives reboots, and has no visible
cause. `--doctor`'s "Chain volume" check reports it.

Deleting the conf does not clear it either. WirePlumber persists a sink's
volume by `media.name` in `~/.local/state/wireplumber/stream-properties` and
restores it onto any later node with that name, so a chain reinstalled under
the same description comes back at the level it was left — observed on a
freshly written conf reading 50 % before anything had touched it. Same
remembered-by-name shape as the selected output above, and the same
consequence: reinstalling is not a reset.

### What a v1 install can and cannot get back

`--target-sink ''` cannot have what smart-filter routing gives: one control,
applied after the graph. The chain is the selected sink there, so the desktop's
volume keys act on it, and its control is upstream of the tuning. Hiding or
proxying the chain was explored twice and rejected (Limitations), and
`priority.session` cannot help — any value derived from the sinks in play
(0–2010) loses to the remembered pick's `30000 + priority`.

What is worth knowing is that the loss is narrower than it looks. The speaker
correction is linear, so where the attenuation lands does not change it at all:
at −13.9 dBFS RMS the whole chain measured as exactly linear. Only the dynamics
move, and only on loud material. So the practical advice — leave the speaker at
100 %, use the chain's control, because that is the one the volume keys reach —
costs compressor behaviour on loud content, not the tuning.

The half a reader cannot see is the level *underneath*. Once the chain is the
selected output, the sink it feeds is invisible from the slider they are moving,
survives reboots, and is subtracted from everything. Both the run (v1 mode) and
`--doctor` therefore read that sink's volume and print it — "your speakers are
at 40 % (−23.8 dB) right now" — rather than only advising that it be raised.

### One smart filter per target sink

Smart filters that share a `filter.smart.target` are **chained, not
offered as alternatives**. `get_filter_from_target` returns the *first*
filter matching a target and `get_filter_target` returns "the next
filter with matching target" (`filter-utils.lua`), so two installed
confs put both chains in the path in series. Measured on WirePlumber
0.5.15 with three voicings installed: `app → Balanced → Detailed → Warm
→ speaker sink`, running every stage — convolver, dialog enhancer,
MBC, regulator, limiter — three times over. Setting
`filter.smart.targetable = true` does *not* turn them into choices:
picking one in sound settings resolves it back to its target, and the
link resolver re-enters at the first filter anyway.

So more than one sink means smart-filter routing has to be off.
`dolby_to_pipewire.py` refuses `--variant all` and `--all-profiles`
unless `--target-sink ''` is passed, and in that mode it pins each
chain's playback with `--target-object`. The pin is not optional: a v1
virtual sink whose playback stream has no target follows the *default*
sink, so choosing one of several in sound settings makes the others
follow it and chain into it (measured: `Balanced → Warm`,
`Detailed → Warm`). Pinning each to the hardware sink keeps them
independent, which is the whole point of installing more than one.

A conversion also warns on the spot when the conf it just wrote joins
another aimed at the same sink. That is the state a user reaches by
trying a second voicing or profile — `--force` guards a single output
path, so a differently-named conf lands beside the first with no
collision — and neither the run nor the audio would otherwise say so.

### Diagnosing an installed chain (`--doctor`)

`ee_to_pipewire.py --doctor` (also reached as `dolby_to_pipewire.py --doctor`)
reports the state of what is installed rather than converting anything: chains
stacked on one target sink, a conf on disk with no node in the graph (a
missing LSP/Calf plugin makes `module-filter-chain` drop the whole file), an
`.irs` a conf names but that isn't there, a `filter.smart.target` naming a sink
that no longer exists, which output is selected (the chain itself, a
virtual-sink chain nothing is playing through, or a remembered pick naming a
chain that is gone — see "The selected output is remembered"), confs under
`filter-chain.conf.d/`, WirePlumber older than 0.5, EasyEffects processing the
same audio, and confs written by another version of the tool. It ends with an
environment block to paste into an issue.

It deliberately reports the *EasyEffects* side as a conflict only. On this path
EasyEffects is an intermediate format staged in a tempdir, so the generator's
preset/autoload checks would describe directories this path never writes to.

Probing and judging are separate: every check is a pure function over gathered
data, so states this developer machine can't reach are unit-tested in
`tests/test_pw_doctor.py`.

## Plugin coverage

| EE plugin key | Translated as | Notes |
|---|---|---|
| `convolver#0` | `type=builtin label=convolver` × 2 | PW's builtin convolver is mono, so EE's stereo convolver expands to one node per channel. `gain` config field carries `output-gain`. |
| `equalizer#0` (PEQ) | LSP `para_equalizer_x16_lr` | `xm` is **MUTE** (default 0 = active), not enable — see `emit_peq` in `lib/pipewire/plugins.py`. EE writes filter type / mode / slope as enum **strings** (`"Bell"`, `"Hi-pass"`, `"RLC (BT)"`, `"x1"` etc.); LSP expects integers — translated via `EE_FTYPE_TO_LSP` / `EE_FMODE_TO_LSP` / `EE_FSLOPE_TO_LSP`. Same pattern recurs for MBC global mode (`EE_MBC_GLOBAL_MODE`), MBC envelope boost (`EE_MBC_ENVB`), MBC sidechain mode (`EE_MBC_SCMODE`), and limiter mode (`EE_LIMITER_MODE`). |
| `equalizer#1` (dialog) | Same plugin as PEQ | Disambiguated by position in `plugins_order`, not by shape. `_assert_positional` fails loud if reordered. |
| `multiband_compressor#0` (MBC) | LSP `mb_compressor_stereo` | Per-band linear values round-trip to source dB to 1e-4. Per-control mapping in the table below. |
| `multiband_compressor#1` (regulator) | Same plugin | Carries `volmax_boost` (typically +6 dB) on `input-gain` (the default slot since issue [#23](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/23); `--volmax-slot output-gain` moves it) when present; if the regulator stage is absent, `make_preset` puts the boost on `limiter#0`'s `input-gain` instead — readers walking the gain stages must check both. |
| `limiter#0` | LSP `limiter_stereo` | `slink` is U_PERCENT (0–100), not 0–1. |
| `bass_enhancer#0` | Calf `BassEnhancer` | EE wraps Calf BassEnhancer (`src/bass_enhancer.cpp:67-74`). `amount` is dB in the EE preset, linear in Calf — converted via `db_to_linear` (the `BIND_LV2_PORT_DB` macro). `harmonics`→`drive`, `scope`→`freq`, `floor`/`blend` direct. Triggers on SoundWire devices with small drivers. |
| `stereo_tools#0` | Calf `StereoTools` | EE wraps Calf StereoTools (`src/stereo_tools.cpp:65-80`). Mode strings → ints via `EE_ST_MODE` (7 labels, 0..6). `slev`/`mlev` are dB→linear; `sbal`/`mpan`/`stereo_base` direct linear; `sc_level` (1..100), `stereo_phase` (0..360°), `delay` (-20..+20 ms) direct. **Translator retained but no longer triggered:** since 2026-06 the *generator* (`dolby_to_easyeffects.py`) emits no `stereo_tools#0` block (the `surround-boost → stereo_tools` widening was falsified by a DAX capture — design-notes entry 2), so generated presets never reach this translator. This row still applies to any hand-edited or legacy preset that carries a `stereo_tools` block. |
| `autogain#0` (bypassed) | *(silent skip)* | HDA default is bypass=true (unless the preset was generated with `--enable autogain`); emitting a bypassed node would just clutter. |
| `_vbe` (top-level metadata, `--enable virtual-bass` only) | LSP `filter_stereo` ×7 + Calf `Saturator` ×2 + builtin `copy`/`mixer` | Not an EE plugin key: EasyEffects cannot express the parallel branch, so the generator records the XML's virtual-bass values top-level (the `_generator` contract) and `lib/pipewire/vbe.py` sandwiches the whole translated chain between a copy fan-out and a dry+wet mixer. See "No VBE by default" under limitations. |
| `autogain#0` (active) | LSP `autogain_stereo` | EE's autogain is native libebur128 (`src/autogain.cpp`); `autogain_stereo` is the LV2 equivalent — a K-weighted (LUFS) loudness AGC. `target`→`level` and `silence-threshold`→`silence` are dB-domain ports passed **directly** (no `db_to_lin`), clamped to the port ranges (−60..0 / −84..−36); `weight=5` (K-weighting = EBU R 128); `lkahead=0` (zero added latency). EE's `maximum-history` (s) drives the gain-ride time-constants asymmetrically — `tfall_l` (gain down, 200 ms/s) faster than `tgrow_l` (gain up, 500 ms/s, anti-pumping) — matching EE's measured behaviour (on-device EE-vs-PW proof in design-notes). The mapping was validated at a 20 s history; shorter windows (HDA `--enable autogain` always; SoundWire when `volume-leveler-amount` > 5) surface a warning — on the console and in the conf-header warning block — that the PW ride may be faster than EE's. EE `input-gain`/`output-gain` are always 0.0 and have no main-path port, so they are not written. Active by default on SoundWire; on HDA only for presets generated with `--enable autogain`. |

Anything the converter cannot express warns on the console instead
of dropping silently: unknown plugin keys (a non-Dolby preset fed in
surfaces as warnings, not a traceback), bypassed plugins (except
autogain on HDA, where bypass is the expected default), unknown enum
labels (translated to the fallback integer, with a pointer at the
`EE_*` table to extend), presets declaring more than 16 EQ bands
(excess dropped), a nonzero convolver `input-gain` (no builtin port),
and plugin objects missing from `plugins_order` (never visited by the
chain builder).

The XML→preset mapping invariant (CLAUDE.md "every parameter must
trace to an XML field") applies here too: this script translates
what EE's preset already encoded; it does not introduce new
parameters or hand-tuned offsets.

### MBC per-control translation

Reference table for `mb_compressor_stereo` controls — the load-bearing
mapping for both `multiband_compressor#0` and `#1`. dB → linear is
`10**(dB/20)` (note `0 dB → 1.0`, not 0); identity passes the EE value
through unchanged. Round-trips to four decimals (locked in by
`tests/test_ee_to_pipewire.py::test_mbc_round_trip_4_decimals`).

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
| `compression-mode`            | `cm_N`      | enum (`Downward`=0; full table in `EE_MBC_CM`) — written explicitly because LSP's boost path (below) is live in the other modes |
| `boost-threshold` (dB)        | `bth_N`     | `10**(dB/20)` — LV2 default is −72 dB; the generator pins −60 dB |
| `boost-amount` (dB)           | `bsa_N`     | `10**(dB/20)` — LV2 default is +6 dB; the generator pins 0 dB |
| `sidechain-custom-lowcut-filter` / `-highcut-filter` (bool) | `sclc_N` / `schc_N` | 1 / 0 |
| `sidechain-lowcut-frequency` / `-highcut-frequency` (Hz) | `sclf_N` / `schf_N` | identity (inert while the custom-filter toggles above stay off) |
| `stereo-split` (bool)         | `ssplit`    | 1, written only when true (global, not per band) — the port doesn't exist on lsp-plugins < 1.2.3, and the generator always emits false == the port default |

### Known approximations & untranslated parameters

A few EE preset params are deliberately **not** written into the conf.
For each one the drop is faithful only because the generator-pinned
value equals the LV2 port default the conf silently inherits — both
sides are locked by tests (`_INTENTIONALLY_UNTRANSLATED` in
`tests/test_ee_to_pipewire.py` pins the generator value and the LV2
default; a slow-tier test cross-checks the pinned defaults against the
installed plugins via `lv2info`). If either side moves, the build goes
red and the param must be translated instead.

| EE param | Pinned value | LV2 port (default) |
|---|---|---|
| MBC/regulator `mute` / `solo` | `False` | `bm_N` / `bs_N` (0) |
| MBC/regulator `sidechain-type` | `"Internal"` | `sce_N` (0) — external sidechain is unwired in a filter-chain graph |
| MBC/regulator `sidechain-source` | `"Middle"` | `scs_N` (0) |
| MBC/regulator `stereo-split-source` | `"Left/Right"` | `sscs_N` (0); only read when `ssplit` is on |
| MBC/regulator `sidechain-reactivity` | `10.0` ms | `scr_N` (10) |
| limiter `oversampling` / `dithering` | `"None"` | `ovs` / `dith` (0) |
| limiter `sidechain-type` | `"Internal"` | `extsc` (0) |
| limiter `sidechain-preamp` | `0.0` dB | `scp` (1.0 linear) |
| convolver `ir-width` / `autogain` | `100` / `False` | *(no port — EE-internal IR preprocessing)* |
| PEQ/dialog `split-channels` | `True` / `False` | *(no port — the `_lr` plugin always takes explicit L/R bands)* |

Known **approximation** (not default-equivalence): the autogain
translation. EE's autogain is native libebur128; LSP `autogain_stereo`
is a different implementation, and only the long-window gain ride is
derived from the preset (`maximum-history` → `tgrow_l`/`tfall_l`). The
short-window ride (`tgrow_s`/`tfall_s`), loudness periods, drift limit
and amplification cap stay at LSP defaults, and EE's `reference`
loudness-statistic selector (`"Geometric Mean (MSI)"`) has no
equivalent port. The mapping is on-device validated at a 20 s history
(design-notes); re-deriving more of it is gated on device measurement
(CLAUDE.md "Validating audio changes").

## Equivalence to the EE chain

The MBC/regulator/limiter linear values round-trip to the source
preset's dB values to 4 decimals. The full chain measures equivalent
to the live EasyEffects pipeline on the development device (X1 Yoga
Gen 7, HDA, `Dolby-Balanced`):

- Frequency-domain: max |Δ| ≤ 0.5 dB across 50 Hz–18 kHz on every
  stimulus in the battery (sweep, sweep_quiet, pink, pink_quiet,
  multitone, plus the asymmetric `stereo_pink` for stereo-aspect
  validation). Real measurements on the dev device land in the
  0.00–0.03 dB range.
- Time-domain: signal-to-residual ≥ 30 dB on every stimulus as the
  PASS threshold; real measurements with the full LSP+Calf chain
  run at +70..+73 dB on mono-symmetric stimuli, and the asymmetric
  `stereo_pink` (per-channel comparison) lands in the same band —
  so a sub-30 result is a real regression rather than a metrology
  ceiling.

Measurement workflow + thresholds in
[`tools/measure_pw/README.md`](../tools/measure_pw/README.md). A
deterministic schema check (`lib/pipewire/validate.py`, called in
process) runs automatically after conversion (skip with
`--no-validate`) — it shells out to `lv2info` for every URI in the
conf and validates the `control = { ... }` block against each port's
`Symbol`/`Min`/`Max`/`Default`/`Properties`. Catches unknown port
symbols, out-of-range values, and the `xm`-MUTE-inversion trap. The
same check has a command-line front end at
`tools/measure_pw/validate_conf.py`, for a conf already on disk — same
verdicts, exit 1 on a conf naming a plugin `lv2info` will not resolve,
exit 2 when the check could not run at all.

The same pass decides whether the conf is written at all. `lv2info`
and the filter-chain both resolve plugins through lilv, so a URI
`lv2info` exits non-zero for is one the daemon will not load either
(the plugin is missing, or its TTL won't parse) — those URIs come back
in `Report.unloadable`, the status is `ERRORS`, and the run names the
package instead of writing a conf that cannot work. An exec that never
*answered* — a timeout, a fork that failed — is the opposite case and
stays a warning: it says nothing about the plugin, so that plugin's
ports simply go unchecked.

`lilv-utils` is not required. PipeWire needs the lilv library, not the
command, so demanding it would block a machine whose LSP and Calf are
correctly installed. Without it the check cannot run at all
(`NO_TOOLING`), the conf is written unchecked, and the run says what
that costs and names the package that buys the check back.

Package names differ per distribution, and the LV2 build is not always
the base package — `lsp-plugins` on Fedora and Arch does not ship the
`.lv2` bundle PipeWire loads; `lsp-plugins-lv2` does. `lib/packages.py`
holds that table (verified against repology) along with the
`/etc/os-release` family detection, so every message prints the one row
that matches the reader's machine, and falls back to all four when it
cannot place them. The README's "Plugin dependencies and validation"
lists the same rows for someone reading before they run anything.

Independently of any of that, the emitted module carries
`flags = [ ifexists nofail ]` (`conf.format_conf`). The conf is a
`pipewire.conf.d/` drop-in, so it loads in the daemon's own context:
without the flag one unresolvable plugin aborts context creation and
`pipewire.service` will not start at all, which is
[#71](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/71).
With it the chain is skipped and playback continues unprocessed —
reported by the activation step's sink check and by `--doctor`'s
"Chains loaded".

## Validation in pytest

- `tests/test_ee_to_pipewire.py` — DSP math, schema invariants, the
  load-bearing 4-decimal MBC round-trip, IRS-copy semantics, smart-filter
  property emission, validator-via-`main()` smoke. Plus the systematic
  coverage guard: `make_preset` is swept across every emission-relevant
  flag combination, and every plugin key / leaf param / enum label the
  generator can emit must be translated by the converter or carried in
  `_INTENTIONALLY_UNTRANSLATED` with a default-equivalence proof (see
  "Known approximations & untranslated parameters" above). A new
  generator feature that is neither fails the fast tier.
- `tests/corpus/test_ee_to_pipewire_corpus.py` — runs the full
  XML→preset→PW-conf pipeline against every discovered DAX3 XML
  (auto-discovered from NTFS mounts and CWD; override via
  `ATMOS_CORPUS_DIR`). Asserts every link endpoint resolves, that the
  converter emits **zero warnings** on generated presets (the coverage
  guard's drift check re-run against real XMLs), and — when `lv2info`
  and `spa-json-dump` are installed — schema-checks each rendered conf
  through `lib/pipewire/validate.py` in process, with every URI's
  `lv2info` output memoized for the session. One further test runs the
  `validate_conf.py` CLI over a single rendered conf, since nothing
  else exercises the wrapper end to end. Catches "converter crashes on
  a non-X1-Yoga XML shape" before it reaches a tester.

## Limitations / known gaps

- **WirePlumber 0.5+ required** for smart-filter routing. Debian
  trixie / Fedora 41+ / Arch all ship 0.5+. Older systems (Debian
  bookworm, Ubuntu 24.04 native) need `--target-sink ''` to fall
  back to the v1 virtual-sink emission, with the volume-stacking
  and HDMI-bypass caveats noted above.
- **The chain sink stays visible** in pavucontrol / GNOME's sound
  output picker as a separate entry alongside the hardware speaker,
  distinguished only by a `HARDWARE` flag no picker shows. Picking it
  works (the chain auto-routes to the speaker) but
  the per-sink volume slider is then on the chain, not the speaker —
  reintroducing the v1 stacking, and WirePlumber remembers the choice
  ("The selected output is remembered"). Two mitigations ship instead
  of a fix: in smart-filter mode the description carries a
  ` (speaker filter)` suffix so the entry reads as what it is, and
  `--doctor` reports it when it is selected. The desired UX (one sink
  per hardware output, chain transparently inserted) requires hiding
  the chain from PA enumeration; we explored two paths and neither
  is viable on this class of hardware:
  - `media.class = "Audio/Sink/Internal"` does suppress the chain
    from PA's sink list, but it also breaks pipewire-pulse
    bridging — apps targeting the speaker block on writes to a
    sink-input PA can't represent (`pa_sink_input.sink ==
    PA_INVALID_INDEX`).
  - WirePlumber's Software DSP policy
    (`/usr/share/wireplumber/scripts/node/software-dsp.lua`,
    [docs](https://pipewire.pages.freedesktop.org/wireplumber/policies/software_dsp.html))
    inverts the visibility — it hides the *speaker* and exposes
    the chain in its place via `hide-parent: true`. That works for
    embedded devices where the speaker is the only port on its
    card, but on multi-port HDA cards (laptops with HDMI / BT /
    Headphones sharing an `alsa_card.*` with the Speaker port),
    GNOME's gvc-mixer-control enumerates outputs from the active
    profile's port list — not just from PA's sink list — so a
    phantom "Speaker - <hw-description>" entry stays in the picker
    even with the sink hidden. Selecting the phantom fails because
    `pactl set-default-sink` on the hidden sink returns "No such
    entity"; the user-visible result is worse than just having two
    working entries. There is no card profile on the dev hardware
    that excludes the Speaker port without also unplugging
    headphones, so we can't profile-swap our way out either.
- **No 4-channel upmix** for Snapdragon-class laptops (Yoga Slim 7x,
  X13s Gen 1). Every XML in the corpus reports
  `total_count=2`, including the X13s sibling — the upmix is device
  wisdom encoded outside the XML.  See cross-device-findings.md §14.
- **No VBE (virtual bass enhancement) by default.** DAX synthesises
  missing-fundamental bass harmonics on HDA laptops (issue [#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14)) that
  neither the EE preset nor the default conf reproduces — the converter is
  a faithful 1:1 translation, and the stage is a parallel graph
  EasyEffects cannot express, so building it by default would make the PW
  conf deliberately diverge from EE. The experimental opt-in: generate the
  preset with `--enable virtual-bass` (or pass it to
  `dolby_to_pipewire.py`) and the generator embeds a top-level `_vbe`
  metadata block — EasyEffects ignores unknown top-level keys, the same
  contract as `_generator` — which this converter turns into the measured
  wet branch (`lib/pipewire/vbe.py`): the translated chain is sandwiched
  between a `copy` fan-out and a dry+wet `mixer`, with two LSP brick-wall
  band-passed arms into Calf Saturators between them. All filters run the
  IIR engine with no look-ahead and the mixers/copies are builtin
  pass-throughs, so the branch adds zero latency. Evidence and the
  measured score are in `docs/design-notes.md` Finding 8.
- **Not alongside EasyEffects.** The chain is a replacement for the
  EasyEffects preset, not an add-on: in smart-filter mode EasyEffects'
  own output plays into the very sink the chain attaches to, so
  everything then carries the EE preset *and* the chain in series
  (filters sharing a target run in series — measured, see "One smart
  filter per target sink"). The converter warns at conf-write time when
  an `easyeffects` process is up. Switching back to EasyEffects: delete
  the conf and its `.irs` from `~/.config/pipewire/pipewire.conf.d/`,
  restart PipeWire, start EasyEffects again.
- **No `--launch` flag on this script.** PipeWire's standard reload
  path is `systemctl --user restart pipewire pipewire-pulse`; the
  converter prints it in its next-steps checklist and lets the user run
  it. Activation lives one layer up: `dolby_to_pipewire.py` restarts
  PipeWire and verifies the sink by default (with `--no-activate` to
  opt out), passing `--skip-next-steps` here so the checklist isn't
  printed twice.
- **Small-quantum systems under load are an unvalidated regime.** The
  conf pins no quantum/latency properties — the chain runs at whatever
  quantum the session picked. Perf validation to date is a laptop APU at
  48 kHz / 1024 quantum, xrun-free (`tools/measure_perf/README.md`
  reference numbers); a handheld APU running a game at a smaller
  session quantum is untested, and one Ally X tester hit
  crackling with `spa.audioconvert: out of buffers` in the log
  (issue [#39](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/39)). To isolate DSP load, temporarily pin a larger quantum —
  `pw-metadata -n settings 0 clock.force-quantum 1024` (revert with
  value `0`). That raises the whole session's base latency, a
  system-wide trade-off the user opts into; the chain itself still adds
  zero latency over whatever quantum runs. Crackle can also originate
  below PipeWire entirely: the ROG Xbox Ally X (subsys 1043:1384) had
  playback dropouts tied to TAS2781 UEFI-calibration handling, first
  quirked to skip the unit's calibration
  ([b7e26c8bdae70832d7c4b31ec2995b1812a60169](https://github.com/torvalds/linux/commit/b7e26c8bdae70832d7c4b31ec2995b1812a60169),
  still what vanilla 6.18-stable ships), later superseded in mainline by
  TI's root-cause fix
  ([05ac3846ffe5](https://github.com/torvalds/linux/commit/05ac3846ffe5))
  which Valve backported into its SteamOS 6.16/6.18 kernels — so
  calibration handling differs by kernel lineage (vanilla-stable skips
  it, SteamOS/mainline apply it with the fix). Rule the kernel out
  before tuning the graph.
