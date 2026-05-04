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

1. **Volume stacking.** PulseAudio flat-volume mode multiplies
   along the chain — the chain sink × hardware sink. If the user's
   hardware sink was at 56 % when EasyEffects-autoload was wrapping
   the speaker directly, switching to a virtual-sink chain at 100 %
   silently adds another –15 dB of attenuation on top.
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
hardware speaker as usual. `priority.session = -1` keeps WP's
"best default node" tiebreaker from picking the chain over the
speaker on a fresh session.

Net result:

- Speaker sink stays the system default.
- Apps targeting the speaker get audio routed through the chain
  transparently (one volume slider — the speaker's).
- HDMI / Bluetooth / USB outputs aren't matched by `filter.smart.target`,
  so the chain bypasses on its own when audio routes elsewhere.

The hardware speaker sink's `node.name` is auto-detected at conversion
time via the same `pw-dump` probe `dolby_to_easyeffects.py --autoload`
uses (Audio/Sink nodes whose `device.icon_name == audio-speakers`,
which excludes HDMI / BT / USB). `--target-sink <node.name>` overrides;
`--target-sink ''` falls back to a v1 virtual-sink emission for users
on WirePlumber < 0.5 or with a non-standard policy.

## Plugin coverage

| EE plugin key | Translated as | Notes |
|---|---|---|
| `convolver#0` | `type=builtin label=convolver` × 2 | PW's builtin convolver is mono, so EE's stereo convolver expands to one node per channel. `gain` config field carries `output-gain`. |
| `equalizer#0` (PEQ) | LSP `para_equalizer_x16_lr` | `xm` is **MUTE** (default 0 = active), not enable — see CLAUDE.md. EE writes filter type / mode / slope as enum **strings** (`"Bell"`, `"Hi-pass"`, `"RLC (BT)"`, `"x1"` etc.); LSP expects integers — translated via `EE_FTYPE_TO_LSP` / `EE_FMODE_TO_LSP` / `EE_FSLOPE_TO_LSP`. Same pattern recurs for MBC global mode (`EE_MBC_GLOBAL_MODE`), MBC envelope boost (`EE_MBC_ENVB`), MBC sidechain mode (`EE_MBC_SCMODE`), and limiter mode (`EE_LIMITER_MODE`). |
| `equalizer#1` (dialog) | Same plugin as PEQ | Disambiguated by position in `plugins_order`, not by shape. `_assert_positional` fails loud if reordered. |
| `multiband_compressor#0` (MBC) | LSP `mb_compressor_stereo` | Per-band linear values round-trip to source dB to 1e-4. Per-control mapping in the table below. |
| `multiband_compressor#1` (regulator) | Same plugin | Carries `volmax_boost` on `output-gain` (typically +6 dB) when present; if the regulator stage is absent, `make_preset` puts the boost on `limiter#0`'s `input-gain` instead — readers walking the gain stages must check both. |
| `limiter#0` | LSP `limiter_stereo` | `slink` is U_PERCENT (0–100), not 0–1. |
| `autogain#0` (bypassed) | *(silent skip)* | HDA default is bypass=true; emitting a bypassed node would just clutter. |
| `autogain#0` (active) | *(warn + skip)* | SoundWire's conservative-leveler path needs an `autogain_stereo` mapping that v1 doesn't ship. Affects SoundWire devices. |
| `bass_enhancer#0` | *(warn + skip)* | Bankstown vs Calf BassEnhancer choice unresolved; needs at least one real-world report to anchor on a specific plugin URI. |
| `stereo_tools#0` | *(warn + skip)* | Calf StereoTools mapping is non-trivial (M/S level/balance, side level, phase). Triggers only on devices whose XML enables surround virt. |

Each skipped plugin emits a stderr warning with rationale. The dispatch
table (`EE_KEY_DISPATCH` in `ee_to_pipewire.py`) marks unknown keys with
a generic warning rather than crashing — a non-Dolby preset accidentally
fed in surfaces as warnings, not a traceback.

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

## Equivalence to the EE chain

The MBC/regulator/limiter linear values round-trip to the source
preset's dB values to 4 decimals. The full chain measures equivalent
to the live EasyEffects pipeline on the development device (X1 Yoga
Gen 7, HDA, `Dolby-Balanced`):

- Frequency-domain: max |Δ| ≤ 0.5 dB across 50 Hz–18 kHz on every
  stimulus in the battery (sweep, sweep_quiet, pink, pink_quiet,
  multitone).
- Time-domain: signal-to-residual ≥ 30 dB on every stimulus (the
  ~35 dB ceiling is from FFT-block-size differences between EE's LSP
  convolver and PW's builtin convolver, not a chain divergence).

Measurement workflow + thresholds in
[`tools/measure_pw/README.md`](../tools/measure_pw/README.md). A
deterministic schema check (`tools/measure_pw/validate_conf.py`)
runs automatically after conversion (skip with `--no-validate`) —
it shells out to `lv2info` for every URI in the conf and validates
the `control = { ... }` block against each port's
`Symbol`/`Min`/`Max`/`Default`/`Properties`. Catches unknown port
symbols, out-of-range values, and the `xm`-MUTE-inversion trap.

## Validation in pytest

- `tests/test_ee_to_pipewire.py` — DSP math, schema invariants, the
  load-bearing 4-decimal MBC round-trip, IRS-copy semantics, smart-filter
  property emission, validator-via-`main()` smoke.
- `tests/corpus/test_ee_to_pipewire_corpus.py` — runs the full
  XML→preset→PW-conf pipeline against every discovered DAX3 XML
  (auto-discovered from NTFS mounts and CWD; override via
  `ATMOS_CORPUS_DIR`). Asserts every link endpoint resolves and
  shells out to `validate_conf.py` when `lv2info` and `spa-json-dump`
  are installed. Catches "converter crashes on a non-X1-Yoga XML
  shape" before it reaches a tester.

## Limitations / known gaps

- **WirePlumber 0.5+ required** for smart-filter routing. Debian
  trixie / Fedora 41+ / Arch all ship 0.5+. Older systems (Debian
  bookworm, Ubuntu 24.04 native) need `--target-sink ''` to fall
  back to the v1 virtual-sink emission, with the volume-stacking
  and HDMI-bypass caveats noted above.
- **No 4-channel upmix** for Snapdragon-class laptops (Yoga Slim 7x,
  X13s Gen 1). Every XML in the 1050-file corpus reports
  `total_count=2`, including the X13s sibling — the upmix is device
  wisdom encoded outside the XML.  See cross-device-findings.md §14.
- **Time-domain residual capped at ~35 dB S/R** between EE and PW
  chains. Not audible (well below content-coupled noise) but worth
  knowing if you measure rather than listen. Recoverable by tuning
  PW's builtin convolver `blocksize` / `tailsize` to match LSP's
  defaults; not yet attempted.
- **No `--launch` flag.** PipeWire's standard reload path is
  `systemctl --user restart pipewire pipewire-pulse`; the script
  prints that as a "[next]" line and lets the user run it.
