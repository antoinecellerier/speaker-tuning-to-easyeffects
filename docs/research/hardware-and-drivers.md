# Hardware and drivers: kernel, codec pins, amps and firmware

## Where this stands

Symptoms indistinguishable from a bad preset can originate a layer below
anything XML-derived. Check the drive path before re-litigating the mapping.
[reference.md](../reference.md) covers what the converter itself emits. This
project reports these faults and doesn't touch the kernel.

- **Old kernel**: a banner, a `--doctor` check and a `--speaker-info` note
  past 18 months ([#33](#r-kernel-misconfigured-codec)).
- **Hidden speaker pin**: a warning when a pin an upstream fixup declares is
  missing ([#53](#r-woofer-pin-hidden)).
- **Pin present, DAC source wrong**: a warning gated on a table match plus
  the observed fault ([routing](#r-speaker-dac-misrouted)).
- **No table lists the machine**: `find_fixed_level_speaker_pin`, a
  table-free *ask* read from the codec dump ([#95](#r-fixed-level-speaker-pin)).
- **Smart amps**: `_AMP_FAMILIES` counts a part with an on-chip DSP doing
  voicing or protection ([criterion](#r-smart-amp-families)).

Open:

- The exact #33 mechanism isn't identifiable from userspace; a PM regression
  or a mis-firing quirk is plausible ([#33](#r-kernel-misconfigured-codec)).
- We mirror `snd_hda_pick_fixup`'s matching but not its *ordering*, so another
  machine's earlier PCI entry shadowing this machine's codec entry stays
  invisible ([#53](#r-woofer-pin-hidden)).
- On a kernel too old for a signature that matches, the table-free ask
  misfires: the fault is real but the cause is kernel age, which only the
  signature tells apart. The parked pin-signature table waits for a second
  signature-keyed report ([#95](#r-fixed-level-speaker-pin)).
- The fixed-level ask leaves "Listed but unusable" unbuilt, a mixer's
  input-side amp unread and a smart amp carrying the volume a residual. Its
  false-positive evidence is thin: two real dumps, both silent
  ([#95](#r-fixed-level-speaker-pin)).
- GPIO amp-enables are recorded, not built. `alc290_fixup_mono_speakers` waits
  for a report of the mono symptom ([routing](#r-speaker-dac-misrouted)).
- TAC5XX2's firmware-name guess is unseen, and its watchlist entry waits on a
  machine ([smart amps](#r-smart-amp-families)).
- `lib/hardware/speakers.py` states that SOF zeroes the PCI subsystem id, and
  `_quirk_for_codec` skips PCI-SSID matches on SOF because of it. That is
  unverified. A SOF-card report with a "picked fixup … PCI SSID" kernel log
  line, or a read of SOF's `hda.c` against a live SSID, would settle it.

<a id="r-kernel-misconfigured-codec"></a>

## Bad sound with a perfect preset: the kernel layer below (issue #33)

A kernel upgrade fixed the IdeaPad Pro 5 14APH8 report ([#33]), not any preset
change: Debian's 6.12 LTS → 7.0. On 6.12 the sound was "a lot worse than
Windows" with *every* tuning XML in its driver store. Bass was mostly missing
and the rest garbled.

The fix was not new hardware support. The reporter's `--speaker-info` output is
identical on the broken 6.12 and the working 7.0: same ALC287 codec, one stereo
speaker pin, no smart-amp driver bound. Lenovo's spec lists plain 2 W × 2
stereo. The older kernel was mis-*configuring* the same codec/speaker path, and
7.0 repaired it.

The exact mechanism isn't identifiable from userspace. A PM regression or a
mis-firing quirk is plausible. The reporter's own research points at
power-management changes breaking 6.6-era codec/amp setup ("flat sound"). His
analog controller's PCI SSID `17AA:3881` matches the kernel's "YB9 dual power
mode2" quirk entry. That SSID is also the TAS2781 fixup's match key, but a
mis-driven TAS2781 smart amp is ruled out: the working kernel shows the
identical topology, so this SKU very likely has no smart amp at all. Either way,
the preset's treble-forward correction on top made the broken baseline sound
*worse* than stock.

Symptoms indistinguishable from a bad preset can originate a layer below
anything XML-derived. Check the drive path before re-litigating the mapping.
This case motivated the old-kernel hint: an end-of-run banner, a `--doctor`
check and a `--speaker-info` annotation. Design choices:

- Kernel series ages come from a release-month table
  (`_KERNEL_SERIES_RELEASES`), not a "latest known kernel" constant. Release
  dates are historical facts, so an aging copy of the tool still ages old
  kernels correctly. A series newer than the table is assumed recent, so a
  brand-new kernel is never flagged.
- Cutoff `_KERNEL_OLD_MONTHS = 18`. A stable distro's kernel is at most ~9
  months old on the distro's release day: Debian 13 shipped 6.12 at 9 months,
  Ubuntu LTS GA kernels at ~1 month. So 18 months keeps every fresh install
  quiet for 9+ months and never fires for rolling/HWE users. It still catches
  the one real case: #33 fired at 6.12 + 20 months, which a 24-month cutoff
  would have missed. LTS point releases backport one-line `Cc: stable` quirks,
  but not the driver-rework / power-management fixes of the class seen here.
- Keeping the table current is automated:
  `.github/workflows/kernel-release-table.yml` runs
  `tools/update_kernel_releases.py` weekly and opens one PR per new series.
  Staleness is the failure mode worth engineering against: since a series above
  the table's max counts as recent, a table that stops growing doesn't warn
  *less accurately*; it silently stops warning at all. The month is the `vX.Y`
  tag's tagger date on Linus' tree, the only source checked that reproduces all
  32 hand-entered rows exactly. The `cdn.kernel.org` tarball mtime was rejected:
  it lags the tag by up to a day, which lands 5.19 in `2022-08` and 6.18 in
  `2025-12`, both a month late. The updater is append-only and refuses an
  implausible batch, an out-of-order date or a partial parse. So a hand
  correction below the newest entry survives, and a broken run fails loudly
  instead of writing a plausible-looking wrong table.

Check the drive path for crackle too, as in issue
[#39](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/39).
`docs/ee-to-pipewire.md` "Small-quantum systems under load" covers that
report's userspace suspect, DSP load at a small quantum.

Crackle can also originate below PipeWire entirely (issue #39, status as of
2026-07-24). The ROG Xbox Ally X (subsys 1043:1384) had playback dropouts tied
to TAS2781 UEFI-calibration handling. It was first quirked to skip the unit's
calibration
([b7e26c8bdae70832d7c4b31ec2995b1812a60169](https://github.com/torvalds/linux/commit/b7e26c8bdae70832d7c4b31ec2995b1812a60169)),
which is still what vanilla 6.18-stable ships. Mainline later superseded that
with TI's root-cause fix
([05ac3846ffe5](https://github.com/torvalds/linux/commit/05ac3846ffe5)), which
Valve backported into its SteamOS 6.16/6.18 kernels. So calibration handling
differs by kernel lineage: vanilla-stable skips it, and SteamOS/mainline apply
it with the fix. Rule the kernel out before tuning the graph.

[#33]: https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/33

<a id="r-woofer-pin-hidden"></a>

## Half the speakers, silently: a woofer pin the firmware hides (issue #53)

On the Lenovo Yoga 7 16IAH7 (82UF, ALC287 codec SSID `17AA:386A`), the reporter
found that Linux drives only the tweeters until they install a modprobe line.
They added this after filing a working device report. Their `--speaker-info`
showed one internal speaker pin (`0x14`). Lenovo PSREF lists the machine as "4
stereo speakers, 3W x2 (woofers), 2W x2 (tweeters)". The preset was being
applied to half the speaker set. This is a third member of the #33 family, where
the fault sits a layer below anything XML-derived.

No tooling told the reporter, and nothing in our output could have: that is the
gap this closes. A self-described new Linux user, they deduced it from "the
complete lack of bass" and reached the modprobe line through "a lot of
troubleshooting" with an LLM. They derived their fix independently of the
upstream commit below, which they had not seen.

**Mechanism**, from upstream commit
[`b70f007a9fc6`](https://github.com/torvalds/linux/commit/b70f007a9fc6): the
BIOS reports pin complex `0x17` as unconnected, so the kernel configures only
`0x14`: "mono/tinny audio", in the commit's own words. The fixup
(`alc287_fixup_yoga9_14iap7_bass_spk_pin`) rewrites `0x17`'s default config
*and* reassigns DACs, and the second half matters for triage. The reassignment
avoids `0x06`/`0x08`, which have no volume controls, and pairs `0x14`+`0x17` on
DAC `0x02`. A `hdajackretask` pin override does only the first half, so it is
*not* a substitute. Don't suggest one.

The commit also notes that under SOF the PCI subsystem id the HDA layer saw was
`17aa:0000`. It took this to mean `SND_PCI_QUIRK` entries cannot match and
`HDA_CODEC_QUIRK` is required. `snd_hda_pick_fixup`
(`sound/hda/common/auto_parser.c`) says otherwise. Since
[`0aacce7c32e4`](https://github.com/torvalds/linux/commit/0aacce7c32e4631c3634df5d19d30c72a3614ec9)
(7.1), a PCI id with either half zero skips the PCI comparison, and *every*
entry, `SND_PCI_QUIRK` included, is compared against the codec SSID. The lookup
ends on a codec-SSID pass regardless. So on such a machine a PCI-keyed entry
matches on the codec's id, never the PCI one. `find_hidden_speaker_pin` mirrors
exactly that, so we never claim a match the kernel could not make.

**Two discriminators checked and rejected**, so they aren't re-litigated:

- *The DAX XML.* Across the full corpus (2837 XMLs, re-derived 2026-08-05), all
  4429 `internal_speaker` endpoints carry `total_count="2"` +
  `has_subwoofer="0"` or `ch_count="2"`, with `<subwoofer-count>` `0`
  everywhere. There are zero exceptions, including the development device, which
  physically has `0x14`+`0x17`. Those attributes are logical channels, not
  drivers. Nothing in the repo reads them and nothing here reaches an emitted
  parameter, so the XML-only invariant is untouched. The quirk table is
  host-hardware data, the same category as `_KERNEL_SERIES_RELEASES`.
- *Raw pin scanning.* An output-capable pin with no default config is
  indistinguishable from a hidden woofer. The development machine has two such
  spare pins (`0x1b`, `0x1e`). They are printed as evidence under "HDA internal
  speakers", never warned on.

**The manufacturer's spec is the discriminator**, and it settles which reports
are affected. Lenovo PSREF publishes static spec PDFs
(`psref.lenovo.com/syspool/Sys/PDF/<Family>/<Slug>/<Slug>_Spec.pdf`) that
extract cleanly. The `/Product/…` page is a JS app that fetches as an empty
shell. The spec was validated in both directions. Three machines known to expose
two pins, Yoga Pro 7 14ASP9 (#51), Yoga Pro 7 14APH8 (#30) and the development
X1 Yoga Gen 7, all name woofers *and* tweeters. The single-pin reports below all
publish "Stereo speakers, 2W x2".

**Caveat:** judge by whether the line names woofers/tweeters, never by the
leading count. The X1 Yoga reads "Stereo speakers, 2W x2 woofers and 0.8W x2
tweeters": four drivers behind a "Stereo speakers" prefix.

On that basis #53 is the only affected device in the tracker. Every other
single-pin report is a genuine 2-driver laptop: #33 and #18 (IdeaPad Pro 5
14APH8 / 14AHP9), #36 (14IMH9), #44 (Yoga Slim 7 14ARE05), #46 (T495) and #50
(Yoga 7 2-in-1 16IML9) all publish "Stereo speakers, 2W x2". #50's missing
`38dc` quirk-table entry concerns its smart amp, not a bass pin. PSREF does list
"Smart Amplifier (AMP)" for it. Its level-restore loud-content verdict, heard on
the dev device 2026-08-18
([level-restore](loudness-and-limiting.md#r-level-restore)), is unaffected.

Design choices:

- **Table-driven, exact-SSID only.** The warning fires when the codec an
  upstream pin-adding fixup names is missing a pin that fixup declares. A
  sibling-SSID heuristic ("your neighbour model has a quirk") was considered and
  dropped: PSREF refuted the case that motivated it.
- **Which fixups qualify is derived, not listed.** A fixup counts when every pin
  it touches is an internal speaker (`0x9017xxxx`) and there are at most two.
  That is a surgical add, not a whole-machine pin remap, where "the quirk isn't
  applied" implies nothing about any one pin. The derivation found the class far
  wider than the ALC287 Lenovos that prompted it: 53 machines when first derived
  (2026-08-05, `2e564b3`), across Lenovo, HP, Dell, ASUS, Acer, Medion, Infinix,
  MECHREVO and Lunnen, from Dell Vostro subwoofers to HP Spectre x360 and ASUS
  ROG rear speakers. The generated table held 84 on 2026-09-24, most of the
  growth from the chain fix below.
  `HDA_FIXUP_FUNC` fixups run C the parser can't read, so their target pins are
  listed explicitly in the generator, each verified against the helper. An
  unlisted helper is uncovered, which is the safe direction.
- **A fixup delivers its whole chain.** `snd_hda_apply_fixup` walks `.chain_id`,
  and upstream extends a machine by wrapping its speaker fixup rather than
  editing it. Reading each fixup's own body alone lost those wrappers.
  `42597bb78a34` moved `17aa:390d` (Yoga Pro 7 14ASP10) onto
  `ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN_HEADSET`, a headset step chaining to
  the pin fixup. It then dropped out of the table, while every kernel
  carrying it went on setting `0x17`. Following the chain restored it and added
  28 further machines, mostly Dell and Acer, that reach `ALC289_FIXUP_DELL_SPK1`
  / `_SPK2` or `ALC255_FIXUP_PREDATOR_SUBWOOFER` one hop away. Each link is
  still filtered on its own terms, so a headset link's mic pins contribute
  nothing. The wrapper usually has no name in the models table, so such a row
  correctly loses its `hda_model=`. Forcing the inner fixup by hand would give
  the user the pin and skip the wrapper's own step.
- **A quirk that changes match kind is re-dated, not carried.** `since` is
  carried forward because what a released kernel contains cannot change. But an
  entry re-keyed from `SND_PCI_QUIRK` to `HDA_CODEC_QUIRK` starts reaching the
  machine through a different id, so a date recorded against the old kind
  describes a fix that never applied. Upstream `75dc2eda659f` (7.3,
  `Cc: stable`) found exactly that on the Yoga Slim 7 14AKP10. Its PCI SSID is
  `17aa:38b4`, shared with the Legion Slim 7 16IRH8, whose PCI quirk matched
  first. So the `17aa:391a` entry added for the 14AKP10 had been dead since it
  landed. Carried forward, our table would have gone on telling a 14AKP10 owner
  on 7.2 that "Linux 7.0 carries this fix … something on this machine is
  stopping it". The residual limitation: we mirror `snd_hda_pick_fixup`'s
  matching but not its *ordering*. So another machine's earlier PCI entry
  shadowing this machine's codec entry stays invisible, and that shadowing is
  what made `391a` dead. Modelling it needs the whole ordered table, including
  the entries that add no pins. Upstream fixes these one machine at a time, so
  the cheap guard is that a re-keying re-dates the row.
- **The matching codec must own the pins.** A PCI-keyed entry identifies the
  *machine*, not a codec. Lending that id to whichever codec was being iterated
  let an HDMI codec with one spare output pin raise a warning naming the wrong
  codec, one no user action could clear. Two conditions fix it: the PCI id may
  only stand in for a codec that already owns speaker pins, and every pin the
  fixup declares must exist on that codec, configured or spare.
- **SOF is read from the card's driver field, not by substring.** The first
  version tested `"sof" in card.lower()` over each `/proc/asound/cards` line,
  and *microsoft* contains *sof*. A plugged-in webcam or headset silently
  disabled the PCI-keyed half of the detector, so the same machine reported
  differently between runs.
- **The modprobe line names the driver that owns the codec.** Picking whichever
  module merely exposes `hda_model` writes the option to a module driving
  nothing. SOF modules sit loaded beside `snd_hda_intel` on ordinary Intel
  machines, this one included.
- **Target the named pin, don't count pins.** The first draft fired when a codec
  had "fewer than two" speaker pins. That breaks on the wider family: several of
  these fixups declare a machine's *only* speaker pin (HP Spectre x360, ASUS
  ROG). The count predicate would have kept firing forever after the user
  applied the fix. It would also have skipped those machines entirely
  beforehand, since a codec with no speaker pins never entered the loop.
  Node-targeting is correct in both directions and also sidesteps the ALSA
  control name, which varies ("Bass Speaker" on one machine, "Speaker Front" on
  #50's) while the fixup's effect does not.
- **A pin fixup is invisible in `/proc` — read the driver's override instead.**
  The detector merges `/sys/class/sound/hwC<card>D<addr>/driver_pin_configs`
  over the printed value before classifying. It also merges `user_pin_configs`,
  which outranks `driver_pin_configs`, exactly as the kernel resolves them. The
  printed value is the `Pin Default` line of `/proc/asound/card*/codec#*`, which
  the kernel fills from `AC_VERB_GET_CONFIG_DEFAULT`
  (`sound/hda/common/proc.c`). That is the *hardware* register, holding the
  firmware's own value. A fixup never writes that register.
  `snd_hda_apply_pincfgs` stores an override in `codec->driver_pins`
  (`sound/hda/common/auto_parser.c`), and only the driver-side lookup,
  `snd_hda_codec_get_pincfg`, consults it. That is why the fix works while the
  printed line goes on saying "not connected". The first version classified pins
  from that line alone, so a machine that had applied the modprobe fix read
  exactly like one that hadn't. The warning would have fired forever, and step 2
  of its own procedure asked the user to confirm something that could never
  happen, pushing them toward the undo. Both files exist on every HDA codec;
  only `user_pin_configs` is gated behind `CONFIG_SND_HDA_RECONFIG`. The card
  index plus codec address name both views, so the two line up without parsing
  either. `--speaker-info` tags such a pin `[kernel fixup]`. Without the tag, a
  pin driven against the firmware and one the firmware declared are the same
  line, leaving the user's verification step nothing to look at. #53's own
  machine exposed it: three pasted runs, one with the fix off and two with it
  on, were all byte-identical. Confirmed on that machine (2026-08-06): after the
  change, `0x17` reads
  `Bass Speaker Playback Switch (woofer, stereo) [kernel fixup]` and has left
  the unconfigured list. The development machine, whose 0x1b/0x1e really are
  spare, still gets the plain "spare pins are normal" note. Both halves matter:
  the detector has to go quiet on a fixed machine without going quiet on an
  unfixed one.
- **Never substitute a related fixup's name.** Where the kernel gives a fixup no
  name in its models table, there is nothing a user can force. The message says
  so and offers the upgrade route alone. Borrowing the sibling IAP7 name for the
  Yoga 9 14IMH9 machines would set the pin and skip the Cirrus amplifier setup
  their chain also performs: a half-fix presented as a fix. This is why the
  table carries a `model` that can be empty.
- **The negative signal is collected too** (`unlisted_speaker_pin_finding`). The
  table only knows machines upstream has already been told about. So a hidden
  woofer nobody has reported yet is indistinguishable from a plain stereo pair,
  which is precisely the ambiguity that cost a triage pass on
  #33/#36/#44/#46/#50. When a machine shows exactly one speaker pin, has spare
  output-capable pins, and matches no fixup, an `ask` invites the one check only
  its owner can do quickly: does the laptop actually have more speakers? The
  gate is tight: spare pins alone are ordinary, and a matched quirk means the
  run already has a real fix to offer. The wording is bounded to what we
  actually do: suggest a modprobe setting to test. Not "we'll add the fix": this
  project doesn't touch the kernel. Not "we'll get it upstreamed": nobody here
  commits to that. A fix landing in Linux is what the generated table then picks
  up on its own.
- **No message asserts a pin count, and none promises a step it won't print.**
  The detector fires with one pin missing, with two, and with none configured at
  all. So wording like "one pin configured … a fix that configures a second one
  — the woofers" was false on machines the tests deliberately cover. For the
  same reason, `--doctor` prints the procedure only where a forcible name
  exists. The closing-block finding carries an ask only when a procedure was
  printed to ask about.
- **A check that can't print a command sends people away, so the printer had to
  change.** `CheckResult.steps` carries `(style, text)` lines printed verbatim,
  so a check states its whole fix in place. Commands soft-wrap in the terminal,
  which still pastes. Before this change, both surfaces held their own copy of
  the fix. `--doctor`'s ended in "Re-run without `--doctor` for the one-line
  modprobe fix", because `emit_check` wraps a check's detail to the terminal,
  and a command folded across two lines does not run. That is a printer
  limitation, not a stance on what a diagnosis should contain. The same report
  gives *prose* fixes, since prose survives wrapping: the Background service
  check walks the reader through EasyEffects' preferences. One builder
  (`speaker_pin_fix_steps`, the role `amixer_enable_cmd` plays for the amp gate)
  feeds both the end-of-run block and the check. A test asserts the two print
  the same command. The change turned up a *third* copy: the converter had its
  own `emit_check` shadowing the shared one. That is why `steps` would have
  reached the PipeWire doctor and not this one.
- **A report may not contradict its own fix further down.** Role-played
  first-time readers caught two places where it did, both on an affected
  machine. `--speaker-info`'s pin list called the flagged woofer an ordinary
  spare ("spare pins are normal…"). The layout estimate printed "2 speakers →
  full-range stereo", the exact count the warning above says is wrong. Read as
  the bottom line, each one talks the reader out of the fix they were just
  handed. Both sections are computed from *configured* pins, so both ask the
  detector and mark what it flagged. The counterpart matters as much: on a
  machine with genuinely spare pins, most of them, the plain "spare pins are
  normal" note has to stay, or the section becomes a fault report about nothing.
- **The fixup's name is not the reader's model, and saying so is load-bearing.**
  The step says the name is the kernel's label and the match is by hardware id.
  Without that, `hda_model=alc287-yoga9-bass-spk-pin` on a Yoga 7 reads like
  someone else's fix, in a line the reader is about to run with sudo, the one
  place a wrong-looking detail stops them. Upstream's own entry for this codec
  id reads "Lenovo Yoga 7 16IAP7".
- **The confirmation is audible first, and hedged.** The step says hear a
  difference, *usually* the bass, with the tag as the mechanical cross-check.
  "Re-run `--speaker-info` and look for the tag" proves the kernel took the pin,
  not that anything plays. The tag exists because /proc can't show it (above).
  The pin usually drives woofers, but several fixups in the table declare a
  machine's *only* speaker pin, where the change is sound where there was none.
- **Cost is scoped to the question.** The default run builds a
  `_gather_speaker_pins()` SpeakerInfo, a few /proc reads. It skips the full
  `--doctor` gather, which shells out to `amixer` per card and to
  `journalctl`/`dmesg`, and globs `/lib/firmware`, for an amp report a
  conversion never prints.
- **The generator checks each hand-listed helper, not just the table size.**
  Twenty rows hang off one `HDA_FIXUP_FUNC` helper. Renaming it upstream would
  drop them all while the total stayed inside its rails and the weekly PR looked
  clean. The check runs against mainline only. A helper legitimately does not
  exist in releases older than the one that introduced it, which aborted the
  first real run until scoped. It is one-sided: it catches a listed
  helper disappearing, not an unlisted one appearing.
  `alc285_fixup_hp_envy_x360` had written two speaker pincfgs since at least
  6.11 and was never listed. So the HP Envy x360 13-ar0xxx sat outside
  the table until a 2026-09-01 audit. The audit swept `alc269.c` for every
  helper writing a `0x9017xxxx` config and diffed the result against
  `_FUNC_FIXUP_PINS`; the seven others matched exactly. Re-run that sweep when
  triaging a kernel pull. It is step-by-step in the `/kernel-watch-triage`
  skill.
- **`since` is a kernel version, not a boolean.** `upgrade_prospect` picks
  between three situations that need different advice, and is shared with
  `--doctor` so the two can't drift:
  - No release carries the fix yet. This is #53's own case as of 7.2-rc6: merged
    for 7.2, so upgrading is a dead end today.
  - A release carries it and the user is behind.
  - The user is *already past* it. The fix is reaching them and something else
    is stopping it, so "upgrade" would send them after what they have.
- **The modprobe module is derived, not hardcoded.** It is scanned from
  `/sys/module/*/parameters/hda_model`. SOF exposes it on
  `snd_sof_intel_hda_generic` today and on `snd_sof_intel_hda_common` before the
  generic split. The legacy path is `snd_hda_intel model=`.
- **Regenerated wholesale, weekly** (`tools/update_speaker_pin_quirks.py`,
  `.github/workflows/speaker-quirks.yml`), unlike the append-only kernel table.
  Entries can disappear upstream, and a stale one would tell a user to force a
  fixup their kernel no longer has. The script fails closed on a partial parse.
  It did so on its first run here, correctly refusing to edit a table whose
  format had changed under it.
- **`since` is the one field carried forward, not re-derived.** It is resolved
  by walking release tags newest-first until one lacks the entry. So an entry
  older than the oldest tag walked to is recorded as *that* tag: a lower bound,
  and an understatement that can only make the advice more conservative.
  Re-deriving it weekly against a *rolling* window made that understatement
  drift. When 7.2 shipped, 6.10 fell out of the newest-12, and 25 rows were
  rewritten `6.10` → `6.11` with nothing changed upstream. That is stricter than
  the truth, since those quirks are in 6.10. It is also enough noise to hide the
  five entries that genuinely reached a release that week. A released kernel's
  contents cannot change, so a recorded value is a fact worth keeping. Only
  entries never yet dated are looked up, which in a normal week is a single
  release fetched. `--rescan` re-derives everything, for an audit after a parser
  change.
  - Scanning *every* release instead was measured and rejected. A fixed floor
    would make the values true first-releases. But the googlesource mirror
    starts returning HTTP 429 above ~30 rapid blob fetches, and a weekly
    unattended job must not sit on that boundary. Old sources parse fine
    otherwise (v5.10 → 9 pin-adding entries, v6.0 → 17, v6.10 → 27, v7.2 → 53),
    so this is a rate limit, not a parsing limit. Exactness below the window
    buys nothing anyway: a kernel that old already trips the issue #33 age hint.
- **`commit` is the link that lets a reader check the claim** (2026-09-01).
  Every warning built on these tables asserts "upstream carries a fix for this
  exact model", and before this field nothing printed let anyone verify it. The
  field is *blame*, not a birthday: the commit that last wrote the entry's line
  in `alc269.c`. That is usually the one that added it or, after an upstream
  edit of the line, the edit. This is what "whatever `git blame` says" resolves
  to, and the warning is honest about it: it says "the upstream change that
  *lists* this model". GitHub's GraphQL blame resolves it, one query per file,
  because the googlesource mirror the blobs come from refuses history pages
  (`+log` → 403) and a bisect over commits would have been dozens of fetches per
  row. It is carried forward like `since` while the row's content is unchanged,
  since blame can only move when the line moved. `--rescan` re-derives it. A
  blame outage leaves `""`, and the warning links the table file with the id to
  search for instead, so a table update is never held back by a link.
  - **One hop through the 2025 file split.** Blame follows a rename but not a
    split. On 2026-09-01, 1003 of the 1251 quirk lines on master blamed to
    `aeeb85f26c3b` ("ALSA: hda: Split Realtek HD-audio codec driver",
    2025-07-11), which carved `alc269.c` out of `sound/hda/codecs/realtek.c`.
    Blaming that pre-split file at the split's parent `6014e9021b28` resolves
    all 1360 of its quirk lines to real authors: 662 distinct, none on the move.
    That parent is the move from `sound/pci/hda/patch_realtek.c`, a pure rename
    blame does follow. The largest genuine owner is a 2011 bulk rewrite with 52
    lines. `_FILE_MOVES` in the pin updater records the hop.
  - **A mass-edit rail catches the next split.** An owner commit holding more
    than 100 of a file's quirk lines is refused and named on stderr. Otherwise
    the symptom of a future move would be hundreds of rows quietly linking one
    commit. The split owns 1003; nothing genuine comes near the rail.
  - The link prints as its own unwrapped line on every surface: end-of-run
    block, `--doctor`, `--speaker-info`. It is the one carve-out from the
    one-link rule in `.claude/rules/user-messages.md`. A `Finding` still never
    carries a URL.

<a id="r-speaker-dac-misrouted"></a>

### The class next door: pin present, DAC source wrong

Upstream commit
[`41d60cbfde10`](https://github.com/torvalds/linux/commit/41d60cbfde10b9f01ae6e2d3195463fbad6e54a8)
(Lenovo Yoga Pro 9 16IAH10, 7.3) names a third shape, between the
[hidden pin](#r-woofer-pin-hidden) above and the
[missing amp](#r-smart-amp-families) below. Pin `0x17` is present and correctly
declared a Bass Speaker, so nothing looks wrong. It is routed to DAC `0x03`,
which has no volume amplifier. That leaves "the right-side woofer barely audible
while only the tweeter plays". The fixup, `alc285_fixup_speaker2_to_dac1`, is
one `snd_hda_override_conn_list(codec, 0x17, …)` call and writes no pin config.

**The fault is observable**, so the table only supplies the authority and the
codec dump supplies the finding. This section first recorded the class as
deliberately unmodelled: no device and no report showed the symptom, and a
`hda_model=` line offered on a table match alone sends a user after a fixup that
may not be their problem. The build (2026-09-01, user-authorized, prioritized on
audible impact) was designed around that objection.

**What the dump shows.** `parse_hda_codec_routing` reads three things from
`/proc/asound/card*/codec#*`, in the text the pin scan already fetched, so a
default run pays nothing new.

- Each pin's selected source is starred on its `Connection:` list. A one-entry
  list is never starred, because the sole entry is the source.
- `In-driver Connection:` is printed only when something called
  `snd_hda_override_conn_list`.
- A widget carries an output volume amp iff its `Amp-Out caps:` line exists, is
  not `N/A`, and has `nsteps` ≠ 0. Pins read `nsteps=0x00, mute=1`, which is
  mute only. ALC287's 0x06 has no Amp-Out line at all.

**What the star means, settled against the source (2026-09-13).** The star is
self-consistent: it always names the widget the hardware has selected, which is
what the user hears, whatever `snd_hda_override_conn_list` did to the cache.
Both halves of `Connection:` are the *hardware's*. `print_conn_list` is handed
the list `snd_hda_get_raw_connections()` returned and stars it at the raw
`AC_VERB_GET_CONNECT_SEL` index (`sound/hda/common/proc.c`). The driver's cached
list is the separate `In-driver Connection:` line, printed only when it differs.

An earlier note here had the star index the *cached* list while the printed list
is the hardware's, so the two would agree only on a prefix. That is wrong, and
it is not inert. A review round reasoned from it that `_dead_volume_source`'s
membership test should be the sibling's prefix test. That would have started
warning on exactly the case the membership test exists to skip: a cached list
that no longer holds the starred widget, where driver and hardware disagree and
our reading is about to stop describing the machine. The suite caught it
(`test_fixed_level_silent_when_the_driver_list_omits_the_starred_widget`). The
prefix guard in `find_misrouted_speaker_pin` stays, reread as conservative
rather than corrective.

**The gate** (`find_misrouted_speaker_pin`) needs all three legs, and every leg
fails closed to silence:

- a table row matched through the same `snd_hda_pick_fixup` mirror the pin table
  uses (`_quirk_for_codec`, shared so the two cannot drift);
- the quirk's pin is a configured internal speaker on that codec;
- its selected source is readable, off the fixup's allowed list, and known to
  carry no volume amp.

The configured-speaker precondition is load-bearing twice. First, a genuinely
spare pin parked on an ampless converter is normal: the dev machine's 0x1e sits
on 0x06. Second, it resolves the 28 machines listed in both tables with no
suppression code:

- pin missing → pin warning alone;
- fixup applied → both quiet;
- a `hdajackretask`-style pin override without the reroute → the routing warning
  takes over. That is the machine-checkable form of the pin-override caveat in
  "Half the speakers, silently" above.

An `In-driver Connection` equal to the fixup's list silences the warning,
because the kernel outranks our parse. Its *presence* proves nothing. The dev
machine (`17aa:22e6`, no `alc269_fixup_tbl` entry at all) carries one because
its fixup arrives by pin-signature match (`snd_hda_pin_quirk`). The log line is
`ALC287: picked fixup  (pin match)`. That is a third match path neither table
models, and the recorded scope limit comes with it: a machine reached only by a
pin-signature entry gets no warning from an SSID-keyed table when its fixup goes
missing.

**Membership** (`_FUNC_FIXUP_ROUTES` in `tools/update_speaker_route_quirks.py`,
every body hand-read): a helper qualifies when it fixes a speaker-pin
volume-path fault and constrains one internal speaker pin's source, or, like
GA401 below, is modelled on that one pin. That admits all 12 conn-override
helpers that write no pincfg, minus one and plus one:

- **Minus** `alc290_fixup_mono_speakers`. It constrains two pins, and its fault
  is mono output, not a lost volume control. 4 of its 5 Dell rows are in the pin
  table anyway.
- **Plus one** `preferred_dacs` helper, `alc289_fixup_asus_ga401`. Its own
  comment reads "avoid DAC 0x06 for bass speaker 0x17; it has no volume
  control". It also pins speaker 0x14 to 0x02, which has volume either way. So
  the row models only the bass half, and the gate observes that pin directly.

The wrapper-shaped helpers are in on purpose: bind_dacs, the HP TAS2781 mute-LED
trio, the Legion AW88399 and ZBook ones. Each contains the identical reroute for
the identical stated reason. Their LED/amp extras are not modelled. The
observed-fault gate means breadth adds no speculative claim.

The other four `preferred_dacs` helpers were swept and rejected:

- `alc_fixup_tpt470_dacs` fixes a *level regression* on a DAC that has volume.
  Upstream ships an opt-out model (`tpt470-dock-fix`,
  [`399c01aa49e5`](https://github.com/torvalds/linux/commit/399c01aa49e5)) for
  the very same SSIDs. It will keep looking admissible to anyone re-deriving
  this table, and must stay out.
- `alc295_fixup_asus_dacs` fixes a silent *headphone*.
- `alc274_fixup_bind_dacs` steers DACs "for EQ".
- `alc288_fixup_surface_swap_dacs` is a plain swap.

`preferred_dacs` in general is the weaker signal: a preference the parser
may override, where a conn override removes the ampless DAC outright. That is
why the sweep keys on `snd_hda_override_conn_list`, and the GA401 admission is
the exception.

**The table** (`lib/data/speaker_route_quirks.py`) held 144 rows on 2026-09-01.
The pin table's `speaker-quirks.yml` workflow regenerates it weekly, and it
shares that table's parser primitives, `resolve_since` walk and blame-derived
`commit` link by import (see the pin-table bullets above).

- By vendor: `1043` 48, `17aa` 44, `1028` 25, `103c` 25, one each `1f4c`/`2782`.
- 12 rows are `HDA_CODEC_QUIRK`-keyed.
- Pin `0x17` is on 142 rows, `0x15` on 2.
- Only 13 rows carry a forcible name, from 4 model names:
  `alc287-lenovo-legion-aw88399`, `alc298-spk-volume`, `alc295-disable-dac3`,
  `alc245-fixup-bass-hp-dac`. The 131 others reach their helper through an
  unnamed wrapper, so unlike the pin table the model-less upgrade-only branch is
  the *majority* here.

Targets are *widgets*, not DACs: `alc298_fixup_speaker_volume` routes to mixer
`0x0c`, and no copy may say "DAC". `alc285-speaker2-to-dac1`, the name that
prompted this section, labels no row at all. No quirk entry points at that fixup
directly, only chains do, and "never substitute a related fixup's name" (above)
holds with the same force.

Two rows (`17aa:3802`, `17aa:386e`) exist only through the generator's
filter-then-dedup order. That is the ordering residual recorded above,
reproduced deliberately so one piece of reasoning covers both tables.
`1043:1ee2` sits undated because mainline re-keyed it from codec- to PCI-SSID:
the match-kind-is-identity rule firing on real data.

Both fix procedures write the same `/etc/modprobe.d/speaker-pin-fix.conf`. Two
files setting one module option would race, and the warnings are mutually
exclusive anyway. `DEMO_SPEAKER_ROUTE=17AA3906` previews the copy the way the
pin demo does.

**Neighbours judged and left**, recorded so the next audit starts here, not from
zero:

- The 7 helpers that write a pincfg *and* reroute belong to the pin table,
  deliberately. Same machines, same remedy. Pre-fix their pin is unconfigured,
  so the routing gate could never fire.
- COEF/verb all-in-one volume fixes are unobservable in `/proc`:
  `Processing caps` prints counts, never values. Any warning would be the
  table-match-alone shape this build exists to avoid, so skip them.
- GPIO amp-enables are readable from `GPIO: io=…` lines but per-machine in
  meaning, so the corroborating half of the gate is missing. Recorded, not
  built.
- `alc290_fixup_mono_speakers` waits for a report of the mono symptom.

<a id="r-fixed-level-speaker-pin"></a>

### When no table lists the machine (issue #95)

The scope limit recorded above arrived as a report within two weeks: a machine
reached only by a pin-signature entry gets no warning when its fixup goes
missing. The X1 Carbon Gen 11 (21HN, ALC287, 17aa:2315) has no `SND_PCI_QUIRK`
row. Its fixup comes only from
`SND_HDA_PIN_QUIRK(0x10ec0287, 0x17aa, …, ALC285_FIXUP_THINKPAD_HEADSET_JACK, {0x14, 0x90170110}, {0x17, 0x90170111}, {0x19, 0x03a11030}, {0x21, 0x03211020})`
→ `alc285_fixup_thinkpad_x1_gen7()`, which excludes DAC 0x06 from pin 0x17.
Neither of those two fixups has an `hda_model=` alias. The development X1 Yoga
(17aa:22e6) reaches the same row.

**Cause: a firmware setting.** The laptop was bought used with
`MicrophoneAccess=Disable` in the BIOS, and a factory reset keeps it. The
firmware then reports pin 0x19 as unconnected (`0x411111f0`), so the signature
fails. In `pin_config_match()`, listed pins are compared with the low byte
masked, two `0x4…` values are equal, and the primary table requires unlisted
pins to read `0x4…`. The vendor fallback `ALC269_FIXUP_LENOVO_XPAD_ACPI` applies
instead. The generic parser then lands 0x17 on DAC 0x06, which has no output
amp. The same setting drops the DMIC endpoint from NHLT, so the card comes up on
`snd_hda_intel` ("HDA Intel PCH") instead of SOF, with no internal mic. The
setting is worth checking on any Intel ThinkPad report showing the legacy
driver. Re-enabling the microphone fixed both on a stock kernel.

**What shipped: `find_fixed_level_speaker_pin`, a table-free reading of the
fault.** The gate is the kernel's own rule, `look_for_out_vol_nid()`: the volume
control goes on the first widget between pin and converter with `nsteps > 0`,
and with none there is no control. Legs:

- HDA.
- No other speaker warning fired.
- Neither table lists the machine, looked up with the SOF restriction off. The
  copy says "no upstream fix is listed", which must hold for a SOF laptop whose
  PCI-keyed row the kernel can't use. "Listed but unusable" is a different
  message, left unbuilt.
- A configured speaker pin with a readable star.
- The source is an `[Audio Output]` (the `kinds` field) with no volume amp. A
  mixer could carry the amp on its input side, which is unread.
- The pin has none either. Conexant/IDT put the amp on the pin.
- The driver's list, if printed, still holds the star.
- Another source of the pin carries volume, so a remedy exists.

`look_for_dac()` takes the first free reachable DAC with no amp preference. So a
healthy machine lands on an ampless one only by connection order, which is what
all 144 routing rows exist to undo. The warning is an *ask*: the dump proves the
path, and nothing proves the cause.

Masks above it:

- PipeWire's `api.alsa.soft-mixer` silences the run and `--doctor`. Checking it
  costs one `pw-dump`, read only once a fault was found.
- A smart amp carrying the volume stays a residual, since the normal run has no
  amp probe.

False-positive evidence is thin: two real dumps (17AA22E6, the ALC294 Xbox
Ally), both silent. `DEMO_SPEAKER_ROUTE=1D059999` previews it. `--speaker-info`
also prints the firmware defaults of the codec the speakers are on.

**"No fixup name to hand you" is a claim about our tables, not about the kernel
(copy audit 2026-09-13).** The warning first read "no kernel setting to force",
which is false. `alc285_fixup_speaker2_to_dac1` overrides pin 0x17's connection
list to DAC 0x02, this fault's exact remedy. It carries the forcible alias
`alc285-speaker2-to-dac1`, so a #95 user could have tried `hda_model=` after
all. We still don't offer it, because forcing a name replaces the whole fixup
chain the machine would otherwise be given (the headset jack, here), and nothing
in the dump says that alias suits this codec. The sentence says what is true:
the fixup tables we carry are Realtek's, and none of them lists this id. That
also keeps it honest on the Conexant and IDT machines the gate admits and the
generators have never read.

**The fault reproduced on the dev machine (2026-09-13).** Setting
`MicrophoneAccess` to Disable in ThinkPad firmware setup, then a reboot, turns
the dev X1 Yoga into #95's broken shape. That is the positive control the class
was built without.

- Pin `0x19` read `0x411111f0` while `0x14`, `0x17` and `0x21` kept the values
  the fixup lists, so exactly one pin broke the match.
- The woofer pin `0x17` starred `0x06`, a converter with no `Amp-Out caps` line
  at all.
- The pin itself reported `nsteps=0x00`, mute only, which is why its control is
  a *Switch*.
- The card came up as `HDA Intel PCH` with every microphone pin blanked, both
  details the reporter described.
- The warning, the `--doctor` row and the fix steps fired and read correctly,
  and the firmware attribute the steps name read `Disable`.

Re-enabling it and rebooting undid all of it on a stock kernel, the half of the
report nothing here had verified.

- `0x19` returned to `0x03a11030`.
- Pin `0x17` starred `0x03` with an in-driver connection list of `0x02 0x03`.
  That is the fixup's own override, so the match was back.
- The card came up as `sof-hda-dsp` again.
- Both the warning and the `--doctor` row went silent on their own.

So the remedy step 2 promises ("pin 0x17 should read driven from a widget other
than 0x06") is what happens. The SOF detail is a consequence of the
same firmware switch rather than a coincidence. The parked signature build
printed "(matches)" here, its other control.

The reproduction also caught a test that only passed on a healthy host.
`test_the_unlisted_level_scenario_renders_the_warn` proved two preview scenarios
distinct by asserting the other one renders no "Speaker level" row. A machine
that genuinely has the fault renders that row for real. The test asserts the
injected machine's id is absent instead, the same host-neutral move the file
already makes for `soft_mixer_in_use`.

**A misfire the table-free copy cannot see:** a kernel too old for a signature
that matches. The fault is real, and the cause is kernel age. Only reading the
signature tells the two apart.

**A pin-signature table, built and parked.** A third machine-written table was
built, measured and taken out of the tree the same day. It held
`SND_HDA_PIN_QUIRK` rows whose chain reaches a routing or pin-adding fixup,
matched like `pin_config_match`. Census at 7.3-rc: 99 rows (91 primary, 8
fallback), 4 admitted:

- three `0x10ec0287/0x17aa`: `ALC285_FIXUP_THINKPAD_HEADSET_JACK` (blame
  c72b9bfe0f91, 2020, #95's and the dev machine's) and two
  `ALC287_FIXUP_THINKPAD_I2S_SPK` (2023);
- one `0x10ec0298/0x1028`: `ALC298_FIXUP_SPK_VOLUME` (2017, the only forcible
  name).

The rest are headset-mic fixups. The table bought precision, naming pin 0x19 and
the expected value, not a different remedy. The kernel-age shape it could tell
apart has 2017–2023 rows behind it. Against that stand a generator, a workflow
leg, a data module and ~80 tests for four rows. The build lives on a local
branch, ready if a second signature-keyed report arrives.

On the dev machine, the parked build is validated end to end, not only
unit-tested. Run against the dev machine in the broken state above, it named the
pin unaided, with the upstream commit link: "pin 0x19 reads 0x411111f0, which
the firmware calls unconnected, where the fix expects 0x03a11030". That sharpens
what it buys without changing the remedy, which is still the firmware check the
table-free warning already prints.

**Route updater rule changed on the way.** The guard keeps the last route on the
same pin and still raises for different pins. 7.3-rc's
`ALC287_FIXUP_YOGA9_SPEAKER2_TO_DAC1` (41d60cbfde10) applies
`alc285_fixup_speaker2_to_dac1` and chains to `alc285_fixup_thinkpad_x1_gen7`.
That puts two `_FUNC_FIXUP_ROUTES` helpers on one chain, which the guard
refused. The kernel applies them in that order, and the later conn-list override
wins. Without the change the weekly workflow would have failed.

<a id="r-smart-amp-families"></a>

## What counts as a smart amp, and which ones we watch for

`_AMP_FAMILIES` (`lib/hardware/amps.py`) is the single source of amp-family
identity. It decides which loaded modules and which SoundWire peripherals count
as amplifiers. The membership criterion is **an on-chip DSP doing voicing or
protection**: the thing whose absence leaves the speakers playing but quiet,
flat and unprotected. The bar is not "ships a firmware blob", since two rows
have no blob at all. A jack codec, a mic codec, or a dumb Class-D amp is out
however similar its driver name looks.

That bar is load-bearing rather than tidy-minded. A match on the SoundWire path
appends a `SpeakerPin`, so a wrongly-included part inflates the reported speaker
count and the layout line. That is the issue-#27 "six mono amps read as twelve
speakers" failure, reached by another route.

**Awinic AW88399** (added 2026-08-24). The first Awinic part on a laptop HDA
path. An HDA side codec landed in 7.3 for the woofer amps on eight Lenovo Legion
codec SSIDs (Pro 7i 16IAX10H / Y9000P IAX10, R9000P ADR10(H), Pro 7 16AFR10H).
Upstream's framing is the familiar one: "without a driver for these amplifiers,
only the tweeters produce sound, resulting in quiet and tinny audio". But the
cause is a missing driver, not a hidden pin.
`alc287_fixup_legion_16iax10h_aw88399` writes no pin config, only a DAC reroute
and a stereo cap. So it needed a family row, and the speaker-pin table correctly
ignores it.

**Qualcomm WSA88xx** (added 2026-08-24). A whole laptop class we saw no amp on.
The ThinkPad X13s ships a WSA8830 (`wsa883x`). The ThinkPad T14s,
Yoga Slim 7x, ThinkBook 16, ASUS Zenbook A14 and HP OmniBook X14 ship WSA8845
(`wsa884x`), the Slim 7x driving separate woofers and tweeters. They are
register-configured, with VISENSE feedback and on-chip temperature and no blob.
So the row carries no globs and no failure marker. A failure here shows up as
an unbound peripheral, which the enumerator already reports as its one hard
verdict. The single token `wsa88` also covers the wsa885x that arrived in 7.3.

**TI TAC5XX2** (added 2026-08-24). A SoundWire smart amp in Intel's Meteor
Lake ACPI match table (`soc-acpi-intel-mtl-match.c`, `tac5572_0_adr` /
`tac5672_0_adr`), so recent Intel laptops can bind it. No named model has been
reported to us. It is included anyway because `tac5` matches nothing
else in the kernel tree. So the row either finds a real amp or stays silent,
and there is no third outcome to weigh it against. The one thing it asserts
that we have not seen is the firmware-name guess in its globs. The driver
builds the name per machine, and only the prefixed form is greppable. The
watchlist entry waits on a machine, not on the driver.

<a id="r-amp-parts-rejected"></a>

### Swept and rejected

From a sweep of every codec driver in mainline against the criterion above,
recorded so the next audit starts here rather than re-deriving it:

| Part | Why not |
|---|---|
| MAX98388 | Smart, but its only machine binding is AMD Van Gogh. Van Gogh's DMI quirks are `Valve/Jupiter` and `Valve/Galileo`: the Steam Deck, which ships no Dolby tuning. |
| MAX98927 | Kaby Lake match table and AVS legacy boards (2017-era). |
| SSM4567 | AVS legacy boards only (Skylake-era). |
| RT1011 | Comet Lake and `sof_rt5682` boards: a Chromebook pairing. |
| RT1019, RT1015 | No firmware, no sense controls, one analog protect register. Dumb Class-D, which is why `rt13` is narrow enough to skip them. |
| AW87390 | Loads firmware, and `aw88` does *not* catch it (aw**87**). But nothing binds it on Intel, AMD, a Qualcomm DT or HDA. |
| TAS675x, TAS5805M, RTQ9124, fs-amp-lib | No laptop binding of any kind. |
| CS42L43, RT712/721/722, ES9356 | Combo jack-plus-amp codecs. Counting them would inflate the speaker count. CS42L43 is the Zenbook S14's jack codec (issue #29) and is excluded on purpose. |

The HDA side-codec set is complete: `sound/hda/codecs/side-codecs/` holds only
cs35l41, cs35l56, tas2781 and aw88399, all covered.

The sweep also found and fixed one artefact. `max98512` had been in the token
list since the Maxim row was written. It appears nowhere in the kernel tree, so
it had never matched anything. It was dropped rather than corrected to
`max98520`, because no laptop board binds that part either.

## Elsewhere

- The #95 EasyEffects crash:
  [easyeffects-and-pipewire.md#r-irs-in-place-rewrite](easyeffects-and-pipewire.md#r-irs-in-place-rewrite).
