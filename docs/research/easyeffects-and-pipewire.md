# EasyEffects and PipeWire: WirePlumber, Flatpak, paths and sample rate

## Where this stands

[reference.md](../reference.md) covers what the converter emits, and
[ee-to-pipewire.md](../ee-to-pipewire.md) the PipeWire path.

- **EasyEffects 7** loads the filter as nothing ([#93](#r-flatpak-xdg-roots)).
- **The chain selected as the output** is two sinks in series, so two volume
  controls, the chain's ahead of the tuning ([#63](#r-chain-as-system-output)).
- **A graph above 48 kHz** makes the EasyEffects preset run hot by the
  sample-rate ratio in dB ([#84](#r-convolver-resample-gain)).
- **Presets** go to EasyEffects 8's data tree: on the Flatpak, the `data` root
  of `~/.var/app/com.github.wwmm.easyeffects/{data,config}`. Every `lib/`
  subprocess runs under `tool_env.c_locale()` ([#93](#r-flatpak-xdg-roots)).
- **A regenerated FIR** gets a new impulse name, and one `load_preset` over
  the socket makes it audible ([impulse names](#r-irs-in-place-rewrite)).
- **`--doctor`** reads EasyEffects' live state over its local socket where the
  socket answers, never the `easyeffects` CLI ([CLI](#r-ee-cli-live-state)).

Open:

- Why #22's reporter hears nothing is not yet confirmed, awaiting his report
  ([#22](#r-preset-loads-but-inaudible)).
- Whether a hand-picked chain suppresses Bluetooth auto-switching is untested:
  the test needs a paired headset ([#63](#r-chain-as-system-output)).
- The convolver gain error is not reported upstream yet
  ([#84](#r-convolver-resample-gain)).
- `FLATPAK_USER_DIR` is knowingly unhandled ([#93](#r-flatpak-xdg-roots)).
- The upstream crash fix, wwmm/easyeffects#5306, is in no released version. The
  hide mitigation stays until the installed version is past 8.2.9, a bet that
  the next tag carries the fix ([#95](#r-irs-in-place-rewrite)).
- Parked: a flag to opt out of `--doctor`'s redactions
  ([#95](#r-irs-in-place-rewrite)).

<a id="r-preset-loads-but-inaudible"></a>

## A preset that loads but is inaudible (issue #22)

**Field follow-up — "loads but inaudible" (issue
[#22](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/22),
UNCONFIRMED root cause).** The reporter (X1 Carbon Gen 8) found the generated
preset loaded but produced no audible difference. What is *established* is that
the generator is correct for his hardware:

- Re-deriving from his actual XML (`DEV_0257_SUBSYS_17AA22B4_…`) shows the
  `gain_l`/`gain_r` curves are substantial: ~15 dB p-p, comparable to a
  full-schema device.
- `make_fir` on the combined IEQ+AO target yields a realized convolver
  magnitude of ~13 dB p-p across 100 Hz–16 kHz, strongly audible.
- The
  [simplified path](../design-notes.md#simplified-schema-xmls-gain_lgain_r-audio-optimizer-issue-22)
  shares the validated full-schema FIR/convolver code: the same `kernel-name`
  and the same min-phase FIR.

That the preset *should* be audible points away from the script. Why he hears
nothing is not yet confirmed, awaiting his report. Candidate environmental
causes, in rough order of likelihood:

- **EasyEffects 7.** The v8 preset format is incompatible, and the convolver
  key changed `kernel-path`→`kernel-name` between 7 and 8. On EE 7 the
  convolver, the dominant block, would therefore silently load no kernel while
  subtler blocks still appear "loaded".
- A Flatpak/native write-vs-run mismatch.
- A missing/misplaced `.irs`.
- No Dolby preset selected.
- Global bypass on.

`--doctor` and a proactive end-of-run warning in normal mode surface these
deterministically, so the user's own machine can confirm or rule out the
hypotheses without us hand-holding each user through GUI questions. The
`kernel-path`→`kernel-name` mechanism is intentionally kept out of user-facing
text. It lives in code, tests and this note. Users see a plain-language "install
EasyEffects 8" message.

<a id="r-chain-as-system-output"></a>

## Selecting the chain as the system output (issue #63)

This is a hazard characterisation, not a bug fix. A reporter described a
hand-selected chain sink whose volume "compounded" with the speaker's and
confused their audio UI across restarts. They then qualified it: both sinks were
at 100 %, they could not reproduce it, and it may have been an overconfident
reading of an initial quiet result. Measured 2026-08-15 on the dev device:
ALC287 `17AA:22E6`, PipeWire 1.6.8, WirePlumber 0.5.

**Selecting it is not fatal, and does not double-process.** With the chain as
the default output, the graph is identical to the normal case:
`pw-play → effect_input → chain → effect_output → speaker`. WirePlumber does not
re-insert the smart filter on the chain's own output link.

**It is two sinks in series, so two volume controls.** `pactl list sink-inputs`
shows the app as an input on the chain and the chain's own output as an input on
the speaker. The levels multiply. The speaker's setting is invisible from the
chain's slider: this machine's speaker sits at 40 % / −23.8 dB.

**The chain's control is ahead of the tuning; the speaker's is behind it.**
`tools/measure_pw/volume_stage_probe.py` captures the speaker sink's monitor,
which sits after the chain and before the hardware mixer. It runs four legs:
chain at unity, chain turned down, speaker turned down, and the stimulus
pre-scaled inside the file. Results on pink at −0.5 dBFS peak / −6.5 dBFS RMS:

| legs | S/R | reading |
|---|---|---|
| chain volume vs unity | 28.7 dB | not a plain gain |
| content pre-scaled vs unity | 28.7 dB | not a plain gain: the dynamics do engage |
| chain volume vs content pre-scaled | 729.9 dB | **the same thing** |
| speaker volume vs unity | inf | invisible: applied after everything |

So the chain's volume reaches the graph as a quieter input, and the MBC,
regulator and limiter engage differently. The speaker's hardware control cannot
affect the processing at all.

**The negative control is the load-bearing part.** The first two runs, on pink
at −5.4 and −1.0 dBFS peak, showed the chain's volume as an *exact* scalar (S/R
598 dB). That reads as "applied after the DSP", which is wrong. At those levels
the dynamics never engage, so both hypotheses predict a pure gain. The
pre-scaled leg distinguishes them, and it only separates once the stimulus is
hot enough. This extends the dormancy in the
[DAX response vs XML](../design-notes.md#r-dax-response-vs-xml) and the
[MBC ratio and time constants](../design-notes.md#r-mbc-ratio-time-constants):
still dormant at −13.9 dBFS RMS, active at −6.5.

**The chain's own control applies in smart-filter mode too.** Setting the chain
sink to 0.125 returned the same 7.9× / 28.7 dB S/R signature as when the chain
was selected. This was tested separately, with the speaker left as the selected
output and audio played to *it* so WirePlumber inserts the filter. The result
rules out its own confound: had the chain not been in the path, its volume could
not have changed anything. So "smart-filter routing means one volume layer" was
wrong. Smart-filter routing removes the *reason* to touch the chain's control,
not the control. A chain turned down once and then switched away from stays down
through reboots with nothing pointing at it. That is why `--doctor` has a "Chain
volume" check that fires regardless of which sink is selected.

Deleting the conf does not clear it, either. A freshly written conf read 50 %
before anything had touched it. WirePlumber persists a sink's volume by
`media.name` in `~/.local/state/wireplumber/stream-properties`. It restores that
volume onto any later node with that name, so a chain reinstalled under the same
description returns at the level it was left. This is the same
remembered-by-name shape as the selected output, with the same consequence:
reinstalling is not a reset.

**WirePlumber remembers the pick, and that is what outlives the mistake.**
`default-nodes/find-selected-default-node.lua` scores the current
`default.configured.audio.sink` at `30000 + priority.session`.
`state-default-nodes.lua` persists it and scores older entries at
`priority.session + 20001 − i`. Measured consequences:

- A hand-picked chain stays the default across three restarts.
- Picking the speaker again clears it across three more. It demotes the chain to
  `…audio.sink.0` rather than erasing it.
- The head entry survives the sink it names, so re-installing a chain under that
  name takes the default output straight back.
- A *demoted* name does not. A stored node at position *i* only beats the
  speaker at *j* when `j − i > 1000`, which no real stack reaches.

Shipped from this:

- the `--doctor` "Default output" check, with three states
- the remembered pick in the environment block
- a ` (speaker filter)` description suffix in smart-filter mode
- the install-time copy in both modes

### Open: does a hand-picked chain suppress Bluetooth auto-switching?

The arithmetic predicts a problem. `find-selected-default-node.lua` scores the
current configured pick at `30000 + priority`. `find-best-default-node.lua`
scores a freshly-connected Bluetooth sink at its own 1010
(`monitors/bluez.lua`). So a user who picked the chain *by hand* should keep the
chain as their default when a headset connects, rather than switching to it
automatically. That would make the "no automatic bypass on output switch"
problem in `docs/ee-to-pipewire.md` true for a different reason than that
section gives. The test is deferred to a run with a headset connected. No
Bluetooth device was paired for the session above, and the HDMI switch was the
proxy.

The prediction is untested. It does not apply to the default (smart-filter)
path, where the speaker stays selected and the reporter of issue #63 confirmed
Bluetooth bypasses correctly. Test: pair a headset, hand-pick the chain, connect
the headset, read `default.audio.sink` from `pw-dump`'s "default" Metadata.

### Rejected: `priority.session` on the v1 capture node

The idea was to make a v1 chain win the default automatically, so the user never
picks it by hand and never writes the sticky entry. Declined:

- It only feeds `find-best-default-node`, which runs after the selected/stored
  hooks and cannot beat `30000 + p`. So on any machine where an output was ever
  picked by hand, it changes nothing.
- There is no safe value. The speaker's own priority is readable, but the scale
  shifts for USB `+100`. The devices that need this most are already on the
  relaxed detection tier, issue #18.
- Where it did work, it would make the chain the default on every boot with
  nothing in the user's own history explaining why.

The offered `pactl set-default-sink` writes the same entry the desktop writes,
and is explicit and reversible.

### Rejected: pinning a single v1 chain's playback

A single unpinned chain does not follow the default anywhere, so there is
nothing to fix. `--target-object` is forced for multi-chain installs, which
would otherwise chain into each other. Extending it to a single chain was
measured and declined. Unpinned, its playback settled on the speaker sink. It
stayed there as the selected output, with the default switched to HDMI, and
across a PipeWire restart. That is identical to the pinned conf in all four
states. Bluetooth was not connected for this; the HDMI switch is the proxy.

<a id="r-convolver-resample-gain"></a>

## A preset that plays hot: EasyEffects resamples the kernel and keeps the gain (issue #84)

Measuring found a deterministic level error. The issue reported constant crackle
on every preset. It was clean the instant EasyEffects' effects were switched
off, and unchanged by the volume slider. The reporter's PipeWire graph ran at
192000 Hz, with `clock.rate 384000`; the ALC287 caps at 192 k. The first
diagnosis was lost CPU headroom, which is wrong, or rather second-order.

`tools/measure_perf/compare_paths.py --rate` captured the output level of each
path, with the same preset and the same content:

| Graph rate | EasyEffects | vs 48 kHz | `20*log10(rate/48000)` | PipeWire filter-chain |
|---|---|---|---|---|
| 48 kHz | −35.9 dBFS | — | — | −35.9 |
| 96 kHz | −30.0 | **+5.9** | +6.02 | −35.8 |
| 192 kHz | −24.1 | **+11.8** | +12.04 | −35.9 |

**Above 48 kHz the EasyEffects preset runs hot by exactly the sample-rate ratio
in dB**, within 0.2 dB at two independent rates. The filter-chain path does not.

**Isolated to the convolver.** Bypassing the convolver collapses the
rate-dependence. The re-measurement used a temporary preset with
`convolver#0.bypass = true`:

| | ee | pw | bypass |
|---|---|---|---|
| convolver ON, 48 k → 192 k | −35.9 → **−24.1** | −35.9 → −35.9 | −30.1 → −30.4 |
| convolver OFF, 48 k → 192 k | −28.5 → **−28.9** | −28.5 → −28.9 | −30.2 → −30.4 |

The shift is +11.8 dB with the convolver and −0.4 dB without. Re-derived from
the JSONs, the residual is −0.35 dB against a −0.20 dB bypass drift in the same
runs. That is larger than the drift, and negligible only against the +11.8 dB it
is compared with. The real control is the PipeWire column, constant at
−35.9/−35.8/−35.9. Two corroborations fall out. With the convolver off, `ee` and
`pw` agree exactly at both rates. With it on at 192 kHz, they diverge by the
same 11.8 dB.

**Mechanism, read in EasyEffects' source.** `Convolver::load_kernel_file`
resamples the impulse response to the server rate (`src/convolver.cpp`,
`ConvolverKernelManager::resampleKernel`). `resampleKernel`
(`src/convolver_kernel_manager.cpp`) resamples the *samples* through
`Resampler::process` and returns. `Resampler::process` is a thin
`speex_resampler_process_float` wrapper (`src/resampler.hpp`) that applies no
scaling. A `normalizeKernel` exists in the same file. The *load* path does reach
it, but only when the preset asks for autogain: `convolver.cpp:152` →
`zita.init(…, settings->autogain())` → `convolver_zita.cpp:302` →
`apply_kernel_autogain()` → `:216`. The resample path never calls it. Our
`make_convolver` writes `autogain = False`, because this tool owns the gain
budget. So nothing normalises the stretched kernel, and nothing downstream
compensates either. Speex preserves amplitude, so a kernel with 4× the taps sums
4× the signal: the ratio, exactly. `module-filter-chain` gets this right, and it
is the immune column. `man 7 libpipewire-module-filter-chain` documents a
`resample_quality` "in case the IR does not match the graph samplerate".

**The error is set by the rate ratio alone, not by the tuning.** Resampling each
profile's shipped kernel ×4 offline gives +11.30 dB for Balanced, +11.26 for
Detailed and +11.30 for Warm. They sit within 0.04 dB of each other, because
resampling scales the whole impulse response linearly whatever its shape. So the
figure is a function of the graph rate and nothing else. That is why the check
computes it rather than tabulating one, and why a message may only state a dB
figure alongside the rate it belongs to.

A third confirmation needs no audio. Resampling the shipped `.irs` offline with
`scipy.signal.resample_poly` raises its peak frequency response +5.56 dB at ×2
and +11.30 dB at ×4. Do not measure this as a sum of taps. The kernel is
minimum-phase and its taps sum near a cancellation point, so that ratio is
unstable and reads −38 dB at ×2.

**Why this explains #84 where CPU load did not.** 12 dB into the multiband
compressor and limiter is gross distortion. The reporter observed each of its
traits, which the dropout theory explained awkwardly:

- It is volume-independent, because it happens inside the chain.
- It stops dead when effects are switched off.
- Disabling the MBC only *partly* eases it, because the limiter still sees the
  same 12 dB.

The CPU cost is real but secondary. The chain costs ~4.5× the cycles at 192 kHz:
the EE marginal cost goes +0.33 → +1.49 Gcyc/s. That is not the 16× a naive
rate-squared estimate predicts, because zita partitions better than a uniform
estimate and the linearly-scaling plugins dominate. That cost is what the
reporter's `clock.force-quantum` change helped, and why Spotify improved while
the browser did not.

**What shipped:** the same finding in two places, each computing the error from
the observed rate so the sentence stays true at any rate.

- A `--doctor` WARN, `environment.graph_rate_status`.
- The end of every ordinary run, `graph_rate_finding`, raised by
  `warn_ee_environment`. Most readers will meet it here.

Not a workaround: flipping `autogain = True` fails twice over.
`apply_kernel_autogain` peak-normalises and then scales by
`min(1, 1/sqrt(power))` (`convolver_zita.cpp:211-249`). A resampled kernel's tap
energy grows with the same rate ratio as its gain. So autogain compensates
sqrt(L) against an error of L, and leaves roughly half the error still there: ~6
dB of the 12 dB at 192 kHz. It also moves the 48 kHz level by ~11 dB,
invalidating the whole
[gain-staging budget](../design-notes.md#gain-staging-budget). The residual
figures are an offline model of that arithmetic rather than a measurement. The
sqrt(L) mechanism is source-certain. Not reported upstream yet.

<a id="r-flatpak-xdg-roots"></a>

## Presets written where EasyEffects stopped reading: the Flatpak's two XDG roots (issue #93)

EasyEffects 8.0.0 (upstream `d8a50b529`) moved presets, impulse responses,
rnnoise models and autoload profiles from `XDG_CONFIG_HOME` to `XDG_DATA_HOME`.
Only its settings database stayed behind. On the Flatpak those roots are
`~/.var/app/com.github.wwmm.easyeffects/{data,config}`, and our base still named
the config one. The native base had been the data path all along, which is why
this never showed on the development machine.

The bug was not inert, which made it hard to see. `xdg_migration()` hauls what
we wrote into the data tree on the next EasyEffects start, so the presets
arrived. They never arrived in time for the end-of-run reload. Afterwards
`--doctor` reported "0 preset files", still watching the folder EasyEffects had
just emptied.

Three properties of that migration shaped the fix:

- **The config tree survives it.** Six named subdirectories move;
  `config/easyeffects/db/` stays. So `config/easyeffects/` exists either way and
  cannot say which layout wrote it. The base is therefore a constant rather than
  something derived from what is present.
- **It runs on every start**, from the `DirectoryManager` constructor, so old
  writes are rescued and nothing is stranded. It also means the config tree is
  not a fallback to *write* to: EasyEffects empties it.
- **The pre-8 layout is never a write target.** These presets need
  EasyEffects 8. Below that is a `--doctor` FAIL, because the 7.x format loads
  the correction filter as nothing. So no version both reads the config tree
  and can play them. A version-switched base was dropped for that reason.

A pre-8 Flatpak must still be *recognised*. Its only files are under `config/`,
and probing the data tree alone would read as "no Flatpak" and hand it the
native paths. Hence `flatpak_tree_exists()`: detection only, never a write
target.

The same report showed `--doctor` crediting `easyeffects --version` on a machine
whose only EasyEffects is a Flatpak. Three things had to hold at once:

- `run()` returned a bare `(None, None)` for a missing binary, so callers read
  installedness off how a command failed.
- `pgrep -x easyeffects` matches the Flatpak's own process, so the native probe
  called itself installed-but-silent.
- A silent probe could relabel the source of one that had answered.

The version also has a subprocess-free source: the first `<release>` of the
deployed app's metainfo XML, which is the `Version:` field `flatpak info`
prints. It answers with neither a `flatpak` binary nor a display. Starting the
sandbox (`flatpak run --command=easyeffects … --version`) is a `--doctor`-only
last resort. It needs strictly more than `flatpak info`. EasyEffects 8 builds
its `QApplication` before parsing `--version`, so it needs a display exactly as
the native probe does.

The round after that was a locale bug, not a timing one. `flatpak info` had
answered in 14 ms, in Chinese. The reporter's `zh_CN.UTF-8` shell made gettext
print `版本： 8.2.9`, and the `Version:` match found nothing. Nor was it only
flatpak. Every shell-out in `lib/` inherited the user's environment. Under
French, `apt-cache policy` prints `Installé :` / `Candidat :` the same way
(verified on Debian). So `_distro_easyeffects_major` skipped both lines and told
a French Debian user it couldn't ask their package manager.

Every `subprocess` call in `lib/` runs under `tool_env.c_locale()`:
`LC_ALL=C.UTF-8`, with `LANGUAGE` dropped. `tests/test_layout.py` holds the
next call to it. `C.UTF-8` over `C` keeps UTF-8 in paths and sink descriptions
intact. `LANGUAGE` has to be dropped rather than overridden, because GLib
consults it before `LC_ALL` and glibc ignores it only under a C locale. With it
left set, `flatpak info` came back translated in a live test here.

Rejected: switching to machine-readable output.
`flatpak list --app --columns=application,version` is locale-proof and prints
no header when piped. But `apt-cache policy` and `pacman -Si` have no such
mode, so the pin is needed regardless and the flatpak command stays as it was.
Loosening the parser to accept any label before an `N.N.N` was rejected too.
The `Version:` line is isolated because `Installed: 458.6 MB` and ref hashes
carry numbers of their own.

### The same bug on a native install: XDG_DATA_HOME / XDG_CONFIG_HOME

Fixing the Flatpak base left the *native* one hardcoded to `~/.local/share`
and `~/.config`. That is the same defect with the roles swapped. EasyEffects
asks Qt for both roots, and Qt reads the environment. So a user who has set
either variable had presets written to a tree their EasyEffects never reads.
The symptom is #93's, minus the rescue. Nothing migrates these, because from
EasyEffects' point of view the files simply aren't there.

Qt's behaviour was verified rather than assumed, since the fix is only worth it
if Qt really does what the spec says. Against `qtpaths` 6.10.2, the Qt the
packaged EasyEffects links, an absolute `XDG_DATA_HOME` is honoured. A relative
one and an empty one both fall back to `~/.local/share`. `lib/xdg.py`
implements that absolute-only rule. A "set, or the default" test would disagree
with Qt on exactly the inputs the spec tells both of them to discard.
EasyEffects holds up its end:

- `strings` on the 8.2.8 binary finds neither `.local/share` nor `.config`.
- `libQt6Core` holds all three variable names.
- Upstream's only `qEnvironmentVariable` calls are the two desktop-detection
  ones.

The leaf is `easyeffects`, unchanged across all of 8.x, because Qt appends no
organization component. It needed checking because Qt's `AppDataLocation` is
`<root>/<organizationName>/<applicationName>`, and an organization component
would have made every path here wrong in a second way. `CMakeLists.txt` does
set `ORGANIZATION_NAME "WWMM"`, and it does reach `config.h`. But nothing
references the macro. `KAboutData::setApplicationData` sets `applicationName`,
`applicationVersion` and `organizationDomain` and leaves `organizationName`
alone.

Three decisions worth keeping:

- **The Flatpak tree does not follow these variables.** `flatpak run`
  overrides all four XDG variables inside the sandbox to point at
  `~/.var/app/<app id>/`. It hands the app the host's values as `HOST_XDG_*`
  instead (`man flatpak-run`). So applying the host's `XDG_DATA_HOME` there
  would move our writes off the only tree that install reads. `_FLATPAK_APP`
  stays anchored to `$HOME` and says why. The per-user *install* root is the
  opposite case and does move: it is `$XDG_DATA_HOME/flatpak`.
- **The constants stay resolved at import.** They are argparse defaults, and
  re-deciding one per call is how a run would split its files across two
  trees. That makes them untestable in process, so the traps read them back
  out of a fresh interpreter instead.
- **`FLATPAK_USER_DIR` is knowingly unhandled.** It overrides the per-user
  install location outright, ahead of `$XDG_DATA_HOME/flatpak`. But it is
  effectively a flatpak-test-suite variable, and honouring it needs a second
  helper with a different fallback shape than the two XDG roots share.

The sharpest edge was in the tests, not the code. `_run_isolated` and
`_run_e2e` build a subprocess environment as `{**os.environ, "HOME": home}`.
That isolated a run completely while the defaults ignored the environment, and
stopped doing so the moment they didn't. A developer with `XDG_DATA_HOME` set
would have had the cases that omit `--output-dir` write into their own live
EasyEffects tree. Both drop the two variables. Same trap class as passing
`--output-dir` without `--irs-dir`.

A second trap turned up alongside it, older and not about XDG at all. The
`live_ee_tree` fixture redirected `DEFAULT_OUTPUT_DIR` and `DEFAULT_IRS_DIR`
but not the rc. So the one case in it that reaches `--autoload` patched the
Fallback Preset into the developer's real `easyeffectsrc`. That was invisible
on a machine that already had one configured, which is every machine that has
run the tool. `set_autoload_fallback` leaves an already-configured file alone.
The fixture redirects all three.

PipeWire's side has the same defect and got the same helper, in its own
commit. `lib/pipewire/checks.py` wrote drop-ins to `~/.config/pipewire`, and
PipeWire reads `XDG_CONFIG_HOME` (`man pipewire`; `man wireplumber` spells out
the fallback). So a conf could land where the daemon never scans: a filter that
silently does nothing, with no file out of place to notice. It is split from
the EasyEffects commit because `docs/code-organisation.md` names this exact
pair, `DEFAULT_OUTPUT_DIR` in `ee_paths.py` and in `checks.py`, as the two
definitions a reviewer will misread as one. They still resolve to different
trees. Only the root each starts from is shared.

## Rejected approaches

<a id="r-ee-cli-live-state"></a>

### The `easyeffects` CLI for `--doctor`'s live state

**The `easyeffects` CLI for `--doctor`'s live state.** `--doctor` wants the
preset and bypass state EasyEffects is *using*. Its config file only says what
it last *saved*: on quit, or on a 30 s autosave that runs while the window is
open (`lib/preset/autoload.py`). So in service mode the file can be hours
stale. The obvious live source is EE's own CLI (`easyeffects -a output`,
`-b 3`), declined for two side effects a diagnostic must not have:
- Through 8.2.8 its parser emits `onHideWindow` for every query, closing the
  running window out from under whoever is reading it. Upstream
  [8942fbc39][ee-hide-on-failure], after 8.2.8, narrows that to `-a`'s failure
  branch; `-b 3` still hides.
- With no daemon running, the binary becomes the *primary* instance and starts
  a second EasyEffects. A lock file alone chooses that branch, and a Flatpak
  keeps that lock file inside its sandbox.

The CLI is itself only a client of EE's [local socket server][ee-local-server].
So `lib/ee_socket.py` speaks to that socket directly, with typed calls, never a
caller's string. `--doctor` sends nothing over it but its two reads. The socket
is a documented interface, not an internal one: the page has sat in EE's
user-interface docs since 8.0.7. But it states no compatibility promise, and its
shape has already changed twice:
- In 8.0.7, `load_preset`'s pipeline argument became `input|output`.
- In 8.0.9, the socket moved from `/tmp` to
  `$XDG_RUNTIME_DIR/EasyEffectsServer`, Flatpak builds keeping the temp
  location.

And `get_global_bypass` exists only in the source tags
([tags_local_server.hpp][ee-server-tags]), not on the page. That is why a
daemon that connects but does not answer is reported as drift rather than
absorbed. It is also why `test_ee_query_contract_pins_the_request_strings`
pins both request strings. On 8.0.0–8.0.8 the socket sits in `/tmp`, so
`--doctor` reads those as "not running" and falls back to the config file.
That is the intended degradation.

<a id="r-irs-in-place-rewrite"></a>

### Rewriting `{preset}.irs` in place and reloading

**Rewriting `{preset}.irs` in place and reloading.** The obvious way to make a
regenerated FIR audible does nothing to the sound: overwrite the impulse under
its old name and load the preset again, over the socket, with `easyeffects -l`,
or by a GUI re-pick. EasyEffects' preset loader sets the convolver's kernel name
only when it differs from the current one
([convolver_preset.cpp][ee-conv-preset]). The generated KConfig setter
short-circuits on an equal value again. `kernelNameChanged` is the only thing
that makes the convolver re-read the file ([convolver.cpp][ee-conv-reload]). So
the in-memory kernel survives until the process restarts. The measurement
harness had already met this: see the unique per-variant prefixes in the
[XML-interpretation hypotheses](../design-notes.md#r-xml-interpretation-hypotheses).
Every FIR-changing release, `--enable level-restore`, `--endpoint` and a swapped
XML all rewrite the same name. Those include v2026.05's `ieq-amount`, v2026.07's
boosts and v2026.08's `audio-optimizer-enable`. Since 2026-08 the generator
names each impulse `{preset}-{8 hex of its samples}` instead. The name changes
exactly when the sound does. A plain preset load picks it up everywhere, Flatpak
and pre-8.0.9 installs included, where no socket is reachable. An unchanged FIR
keeps its name, so nothing reloads for nothing. *Rejected instead:* bouncing
`set_property:output:convolver:0:kernelName` through a stub before the load. It
does trigger the re-read, but only on the socket path; a GUI re-pick stays
stale. It is a mutating request on an interface whose shape has changed twice.
It races the load, and it leaves a bogus-kernel warning in EasyEffects' log per
run.

Stale impulses of the same preset are removed once the JSON is rewritten,
except when one of these still names the file:
- Another preset, since one saved from the GUI keeps its parent's kernel name.
  EasyEffects would load that preset as silence.
- A `--no-copy-irs` PipeWire conf, which PipeWire would then fail to load.
- EasyEffects' own `convolverrc`. It is what a *fresh* EasyEffects plays:
  EasyEffects restores the kernel name from its db on start, not from the
  preset JSON. The dev machine's log showed exactly that after the legacy file
  went: "Kernel 'Dolby-Balanced' not found … Entering passthrough mode".

Until a load names the new impulse, the old one stays.

With the name doing the work, the run's last step is one `load_preset` over
the socket (`lib/preset/reload.py`). It refreshes whatever of ours is playing,
else loads the starting preset. The starting-preset load is declined in two
states: EasyEffects is on the `Nothing` bypass preset, which is `--autoload`'s
non-speaker fallback state, or its default sink is visibly not an internal
speaker. A speaker tuning on a headset would be harm the run caused. An
unknown sink loads. The starting preset is one rule for bare `--autoload`,
this load and the closing copy (`autoload.starting_preset`):
`--autoload <name>`, else the first preset built. That is the profile a bare
run builds, so `--all-profiles` points where a bare run does.
`<default_profile>` stays reported, not acted on; the closing names its preset
under `--all-profiles`. A review caught bare `--autoload` and this load
following different rules for the starting preset, wiring one preset and
loading another. The receipt is `get_last_loaded_preset` plus the convolver's
`kernelName`, pipelined into the same write. A listening daemon that answers
nothing is reported as drift, and a load is never sent onto a state it could
not read. Cost: the convolver re-reads the impulse and runs its FFT in the
main thread, under the mutex the RT thread shares. That is a possible click,
the same one picking a preset in the GUI risks. Global bypass makes "is
playing" false, so it is read too and the copy demoted to "loaded" with a
hint.

One crash was reported on that load
([#95](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/95),
8.2.9 / qt6-declarative 6.11.2). It reproduced on the development machine
(8.2.8 / Qt 6.10.2, 2026-09-13): window open on the Convolver page, the run's
file writes, then a segfault in libQt6Qml about seven seconds later
(`easyeffects[…]: segfault at 58 ip … in libQt6Qml.so.6.10.2`). The trigger is
the run's burst of rewrites reaching a page that binds the impulse list model,
not the load. The discriminators, each with the page showing:

| Discriminator | Result |
|---|---|
| a socket load alone | fine |
| a run with `--no-reload` | crash |
| one new `.irs` or preset `.json` copied in | fine |
| a run with the window closed | fine |
| `hide_window` over the socket, then the run, then `show_window` | fine, window back |

`presets_irs_manager.cpp` watches the directory and rebuilds the model on
every change. The QML frames are the same as upstream
[wwmm/easyeffects#5120](https://github.com/wwmm/easyeffects/issues/5120). The
launch loop the report describes did not reproduce on 8.2.8.

The fault, chased in the EasyEffects source the same day, is in
`ListModel::update` (`src/presets_list_model.cpp`), which serves every
preset/impulse list. It wraps its `append`/`remove` calls in
`beginResetModel`/`endResetModel`, which Qt forbids. Each of those calls is a
`beginInsertRows`/`beginRemoveRows` pair plus a whole-range `dataChanged`.
`append` also announces the new row at size−1. The proxy model and the QML
combo boxes bound to it lose track of the rows, and the next delegate
incubation dereferences a stale list. It takes two directory events a few
milliseconds apart, so the second reset lands while delegates from the first
are still incubating. `touch a.irs; touch b.irs` in the irs directory with the
page shown crashed a debug build of 8.2.9-61 under gdb three times out of
three. One touch never did, nor one every 1.2 s. This tool's writes are that
shape: every `.irs` is a dotfile temp plus a rename, and the whole run's files
land within ~100 ms. A write pattern with one event per file still crashed in
a burst of three, so only sleeps between files would dodge it. Sleeps are
rejected, so the hide stays.

With `update` rewritten as plain remove-then-append row operations, the two
touches survived, and so did a full converter run with the hide step disabled.
That fix was merged upstream the same day as
[wwmm/easyeffects#5306](https://github.com/wwmm/easyeffects/pull/5306). It is
in no released version yet, and a user only gets it once their distro packages
a build carrying it. So the hide mitigation stays, gated on the installed
version being past 8.2.9, the last release that crashes
(`_LAST_EE_RELEASE_WITH_CONVOLVER_CRASH`). That assumes the next tag carries
the fix. It is a bet on how this project has cut releases, taken so the gate
cannot be forgotten; an intermediate release without it would need the
constant corrected. The gate fails closed: `easyeffects --version` wants a
display and Flatpak answers through `flatpak info`, so an unreadable version
hides rather than reading as fixed.

Mitigation: the run sends `hide_window` before writing whenever a daemon
answers (`reload.hide_window_before_writing`). It says so in a line that calls
the crash potential and names the way back, because a window vanishing
unannounced reads as this tool breaking EasyEffects. Hide only: nothing
reports whether the window was open, so a `show_window` after the run would
pop one up on every service-mode run. That same silence is why the line says
*asked to hide* and hedges the way back with "if it was showing".
`hide_window()` is true when the daemon took the request, and a service-mode
EasyEffects with no window takes it just the same.

The first cut hid only when the rc's `visiblePage`/`visiblePlugin` named the
Convolver, which is wrong in the direction that costs the crash. Those keys
reach disk from `db::Manager::saveAll()`. It runs on window hide, on close, at
quit, and on a timer whose schema label says it is active only while the
window is open (default 30 s). So a page opened moments before a run still
reads as the old one, and the run would skip the hide. Confirmed on the dev
machine: with the window closed, an external edit to those keys sat unread for
40 s. Nothing can repair the guess:
- The local socket answers only `get_property` (plugin databases, keyed
  `plugin#instance`), `get_last_loaded_preset` and `get_global_bypass`, so
  those two keys are unreachable.
- `hide_window` writes no reply, and its handler calls `hide()` without
  testing visibility.
- The rc's visibility key is unmaintained: the QML that would set it is
  commented out over a Qt warning.
- GNOME's `org.gnome.Shell.Introspect.GetWindows` refuses unlisted callers.

Hiding an already-hidden window costs nothing, so hiding always is strictly
safer than guessing. Rejected:
- A warning instead of hiding. The crash is deterministic, and nobody reads
  the terminal while looking at the window.
- Spacing the writes apart. One event per file still crashed in a burst of
  three (§ above).
- Asking upstream for a `hide_window` reply that reports what it hid. It would
  only exist in a release that also carries the fix, so it can never reach the
  versions this mitigation is for.

`--doctor` was the last place still treating that same state as a fault. Its
selected-preset check warned "the silent 'Nothing' bypass preset is selected"
whenever EasyEffects sat on the bypass. That included the bypass on a
Bluetooth headset, where `--autoload` puts it deliberately. The reader then
got a WARN, the "what to fix first" verdict, and an instruction to load a
speaker tuning onto a headset: the one action the reload path above refuses to
take. The fix matches the autoload entries against the speaker sinks. That
answers the question actually behind the check: not "is a tuning loaded now?",
which it correctly is not, but "will the speakers still be right?". It reuses
`sinks.sink_kind`, which the doctor already called for a closing bullet. Three
states, because the honest answer differs:
- PASS naming the preset when an entry maps a speaker sink to one this script
  generated.
- UNKNOWN when nothing does.
- The original WARN on the speakers or on an output that could not be
  classified.

Rejected:
- *Keeping the WARN and rewording it.* It still spends the verdict line and
  the summary's WARN count on a healthy machine, which is what made the line
  misleading rather than merely wordy.
- *A plain PASS without reading the autoload entries.* That asserts the
  speakers are fine when nothing looked, the same over-claim the UNKNOWN
  install-location case exists to avoid.
- *A new N/A status.* `UNKNOWN` already renders as "checks that couldn't run",
  and a fifth level would have to be threaded through `summarize`,
  `print_summary` and `print_verdict` for one caller.
- *Softening the other branches too*, since a foreign preset selected on a
  headset is equally "expected". No report has shown that misfiring, and each
  branch widened is a case where a real fault goes quiet.

The gate is `== "other"`, never `not is_internal_speaker(...)`. That helper
folds "don't know" into False. So a failed `pw-dump`, a disconnected pinned
sink or EasyEffects' own virtual sink would all have read as "not a speaker"
and dropped the warning on machines that needed it. The same reasoning had
already been learned once, on the reload gate (code review 2026-08-27).

A review caught two false all-clears in the first cut, both worth remembering
because both looked right:
- The autoload lookup matched an entry on `node.name` alone, but EasyEffects
  keys those files on the name *and* the active output route (issue #18). So
  an entry left behind by a route change reported a mapping EasyEffects will
  never act on as what the speakers autoload.
- It returned on the first device match, letting one speaker sink mapped to
  nothing hide another's real mapping in glob order.

A third: classifying the pinned sink made `output_is_speaker` true for it.
That dropped the closing block's "confirm system output is the speaker sink"
bullet for someone pinned to the speakers while the system default is HDMI,
the case that needs it most. The bullet asks about the system's output, and a
pinned sink answers about EasyEffects'. So that fact is a named property
that takes live readings only.

The same report's `Output sink:` row showed a node name and nothing else, for
example
`alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__HDMI1__sink`
or a redacted `bluez_output.<mac>.1`. That answers the tool's question (what
`--autoload-sink` takes, what a report is triaged on), not the reader's (what
is my sound coming out of). PipeWire already carries a description for every
sink, so the row leads with it and keeps the node name after. Only the
description wraps, because a node name split across lines stops being
greppable and stops being pasteable into the flag. Bluetooth is the exception.
That description is user-set and routinely carries a person's name; "<Name>'s
AirPods" is the stock spelling. And this block is what the issue form asks
people to paste whole. The model behind it has some triage value, but a name
has none and cannot be un-pasted. So the same reasoning that strips the
address (`doctor.no_bt_address`) replaces the description with one fixed
label. The refusal lives at the resolver, not the renderer, so no caller can
reach the name. Parked, if anyone asks: a flag to opt out of the redactions
wholesale, for a reporter who would rather send the real names.

[ee-local-server]: https://wwmm.github.io/easyeffects/user_interface/local_server.html
[ee-server-tags]: https://github.com/wwmm/easyeffects/blob/v8.2.8/src/tags_local_server.hpp
[ee-hide-on-failure]: https://github.com/wwmm/easyeffects/commit/8942fbc391440daa706bfd80e7d6887c523d363d
[ee-conv-preset]: https://github.com/wwmm/easyeffects/blob/v8.2.8/src/convolver_preset.cpp
[ee-conv-reload]: https://github.com/wwmm/easyeffects/blob/v8.2.8/src/convolver.cpp

## Elsewhere

- #84's deep-threshold regulator:
  [design-notes](../design-notes.md#second-deep-threshold-tuning-issue-84s-yoga-slim-7-pro-14ach5-2026-08-30).
- The unused EasyEffects built-ins and the convolver IR trim:
  [design-notes](../design-notes.md#rejected-approaches) "Rejected approaches".
- The PipeWire converter's autogain translation:
  [design-notes](../design-notes.md#translating-active-autogain-to-lsp-autogain_stereo-pw-converter).
